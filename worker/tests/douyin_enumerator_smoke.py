"""纯离线抖音主页/合集枚举测试，使用 fake CDP 和 JSON fixtures。"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.douyin_enumerator import (  # noqa: E402
    _ACCESS_GATE_EXPRESSION,
    _CAPTCHA_EXPRESSION,
    _DOM_DISCOVERY_EXPRESSION,
    _DOM_METADATA_EXPRESSION,
    _OPEN_LOGIN_EXPRESSION,
    _SCROLL_EXPRESSION,
    DouyinEnumerator,
    identify_douyin_target,
)
from feichuan_downloader.chromium_session import (  # noqa: E402
    ChromiumSession,
    clear_douyin_chromium_profile,
)
from feichuan_downloader.config import douyin_chromium_profile_dir  # noqa: E402
from feichuan_downloader.douyin_session import DouyinSessionProvider  # noqa: E402
from feichuan_downloader.models import ContentKind, SourceKind  # noqa: E402
from feichuan_downloader.naming import publication_date  # noqa: E402


class FakeCdpClient:
    def __init__(
        self,
        events: list[dict[str, Any]],
        bodies: dict[str, Any],
        *,
        dom_items: list[dict[str, str]] | None = None,
        dom_author: str = "",
        dom_reported_count: str = "",
        captcha: bool = False,
        access_gate: str = "",
        delay_body_once: set[str] | None = None,
        scroll_observations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.events = deque(events)
        self.bodies = bodies
        self.dom_items = list(dom_items or [])
        self.dom_author = dom_author
        self.dom_reported_count = dom_reported_count
        self.captcha = captcha
        self.access_gate = access_gate
        self.scroll_count = 0
        self.delay_body_once = set(delay_body_once or ())
        self.scroll_observations = deque(scroll_observations or [])
        self.body_attempts: Counter[str] = Counter()
        self.finished_requests: set[str] = set()

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        del timeout
        event = self.events.popleft() if self.events else None
        if event and event.get("method") == "Network.loadingFinished":
            request_id = str(event.get("params", {}).get("requestId") or "")
            if request_id:
                self.finished_requests.add(request_id)
        return event

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> dict[str, Any]:
        del timeout
        params = params or {}
        if method == "Network.getResponseBody":
            request_id = str(params["requestId"])
            assert request_id in self.finished_requests, (
                "response body must not be read before loadingFinished"
            )
            self.body_attempts[request_id] += 1
            if (
                request_id in self.delay_body_once
                and self.body_attempts[request_id] == 1
            ):
                raise RuntimeError("response body is not ready")
            payload = self.bodies[request_id]
            if isinstance(payload, str):
                return {"body": payload}
            return {"body": json.dumps(payload, ensure_ascii=False)}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression") or "")
            if "__FEICHUAN_DOUYIN_CAPTCHA__" in expression:
                return {"result": {"value": self.captcha}}
            if "__FEICHUAN_DOUYIN_DOM__" in expression:
                return {"result": {"value": self.dom_items}}
            if "__FEICHUAN_DOUYIN_METADATA__" in expression:
                return {
                    "result": {
                        "value": {
                            "author": self.dom_author,
                            "reportedCountText": self.dom_reported_count,
                        }
                    }
                }
            if "__FEICHUAN_DOUYIN_ACCESS_GATE__" in expression:
                return {"result": {"value": self.access_gate}}
            if "__FEICHUAN_DOUYIN_SCROLL__" in expression:
                self.scroll_count += 1
                if self.scroll_observations:
                    observation = dict(self.scroll_observations.popleft())
                elif self.scroll_count == 1:
                    observation = {
                        "moved": True,
                        "before": 0,
                        "after": 1000,
                        "height": 2000,
                        "viewport": 1000,
                    }
                else:
                    observation = {
                        "moved": False,
                        "before": 1000,
                        "after": 1000,
                        "height": 2000,
                        "viewport": 1000,
                    }
                return {
                    "result": {
                        "value": observation
                    }
                }
        raise AssertionError(f"unexpected fake CDP command: {method}")


class CookieProbeClient:
    def __init__(self, cookies: Any, *, fail: bool = False) -> None:
        self.cookies = cookies
        self.fail = fail
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> dict[str, Any]:
        del timeout
        self.commands.append((method, dict(params or {})))
        if self.fail:
            raise RuntimeError("offline cookie probe failure")
        assert method == "Network.getCookies"
        return {"cookies": self.cookies}


class InteractiveLoginFakeCdpClient(FakeCdpClient):
    """Model a gate, a successful login, reload, and post-login pagination."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        bodies: dict[str, Any],
        *,
        reload_events: list[dict[str, Any]],
        pagination_events: list[dict[str, Any]],
        login_cookie: str,
    ) -> None:
        super().__init__(events, bodies, access_gate="login_required")
        self.reload_events = list(reload_events)
        self.pagination_events = list(pagination_events)
        self.login_cookie = login_cookie
        self.command_log: list[tuple[str, dict[str, Any]]] = []
        self.reload_count = 0
        self.drain_count = 0
        self.open_login_count = 0
        self.cookie_probe_count = 0
        self.pagination_injected = False
        self.reload_navigation_seen = False
        self.closed = False

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        event = super().next_event(timeout)
        if (
            event
            and event.get("method") == "Page.frameNavigated"
            and self.reload_count
        ):
            frame = event.get("params", {}).get("frame", {})
            if not frame.get("parentId"):
                self.reload_navigation_seen = True
                self.access_gate = ""
        return event

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> dict[str, Any]:
        copied_params = dict(params or {})
        self.command_log.append((method, copied_params))
        if method == "Runtime.evaluate":
            expression = str(copied_params.get("expression") or "")
            if "__FEICHUAN_DOUYIN_OPEN_LOGIN__" in expression:
                self.open_login_count += 1
                return {"result": {"value": True}}
            result = super().command(
                method,
                copied_params,
                timeout=timeout,
            )
            if (
                "__FEICHUAN_DOUYIN_SCROLL__" in expression
                and self.reload_count
                and not self.pagination_injected
            ):
                self.events.extend(self.pagination_events)
                self.pagination_injected = True
            return result
        if method == "Network.getCookies":
            self.cookie_probe_count += 1
            return {
                "cookies": [
                    {
                        "name": "sessionid_ss",
                        "value": self.login_cookie,
                        "domain": ".douyin.com",
                    }
                ]
            }
        if method == "Page.reload":
            assert copied_params == {"ignoreCache": True}
            self.reload_count += 1
            self.events.extend(self.reload_events)
            return {}
        return super().command(method, copied_params, timeout=timeout)

    def drain_events(self) -> list[dict[str, Any]]:
        self.drain_count += 1
        drained = list(self.events)
        self.events.clear()
        return drained


class RecordingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class NoisyFakeCdpClient(FakeCdpClient):
    """Continuously emits unrelated login/status events without idle gaps."""

    def __init__(self, *, dom_items: list[dict[str, str]]) -> None:
        super().__init__([], {}, dom_items=dom_items)
        self.next_calls = 0

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        self.next_calls += 1
        return {
            "method": "Network.responseReceived",
            "params": {
                "requestId": f"login-{self.next_calls}",
                "type": "XHR",
                "response": {
                    "url": "https://www.douyin.com/aweme/v1/web/login/status/"
                },
            },
        }


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class ProgressTimedFakeCdpClient(FakeCdpClient):
    def __init__(
        self,
        events: list[dict[str, Any]],
        bodies: dict[str, Any],
        *,
        clock: FakeMonotonicClock,
    ) -> None:
        super().__init__(events, bodies)
        self.clock = clock

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        del timeout
        self.clock.value += 0.6
        return super().next_event(0)


class FakeSession:
    def __init__(
        self,
        client: FakeCdpClient,
        *,
        cookie_header: str = "sessionid=temporary",
        canonical_cookie_header: str | None = None,
    ) -> None:
        self.client = client
        self.closed = False
        self._cookie_header = cookie_header
        self._canonical_cookie_header = canonical_cookie_header
        self.cookie_urls: list[str] = []

    def cookie_header(self, url: str | None = None) -> str:
        normalized_url = str(url or "")
        self.cookie_urls.append(normalized_url)
        if (
            normalized_url == "https://www.douyin.com/"
            and self._canonical_cookie_header is not None
        ):
            return self._canonical_cookie_header
        return self._cookie_header

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True


class FakeProvider:
    def __init__(
        self,
        client: FakeCdpClient,
        *,
        cookie_header: str = "sessionid=temporary",
        canonical_cookie_header: str | None = None,
    ) -> None:
        self.session = FakeSession(
            client,
            cookie_header=cookie_header,
            canonical_cookie_header=canonical_cookie_header,
        )
        self.opened_url = ""

    def open(self, url: str, *, cancel_event: Any = None) -> FakeSession:
        del cancel_event
        self.opened_url = url
        return self.session


class FakeSetupClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> dict[str, Any]:
        del timeout
        self.commands.append((method, dict(params or {})))
        if method == "Browser.getVersion":
            return {
                "userAgent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "HeadlessChrome/150.0.0.0 Safari/537.36"
                )
            }
        return {}


class FakeChromium:
    def __init__(self) -> None:
        self.client = FakeSetupClient()
        self.started = False
        self.closed = False
        self.opened_page = ""

    def start(self, cancel_event: Any = None) -> "FakeChromium":
        del cancel_event
        self.started = True
        return self

    def open_page(self, url: str) -> FakeSetupClient:
        self.opened_page = url
        return self.client

    def cookie_header(self, client: Any, url: str) -> str:
        assert client is self.client
        assert url.startswith("https://")
        return "temporary_cookie=value"

    def close(self) -> None:
        self.closed = True


class FakeBrowserProcess:
    def __init__(self, args: list[str]) -> None:
        self.args = list(args)
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.alive:
            raise TimeoutError("fake browser is still running")
        return 0


def response_event(
    request_id: str,
    endpoint: str,
    *,
    status: int = 200,
) -> dict[str, Any]:
    return {
        "method": "Network.responseReceived",
        "params": {
            "requestId": request_id,
            "response": {
                "url": f"https://www.douyin.com{endpoint}?fixture=1",
                "status": status,
            },
        },
    }


def loading_finished_event(request_id: str) -> dict[str, Any]:
    return {
        "method": "Network.loadingFinished",
        "params": {"requestId": request_id},
    }


def loading_failed_event(request_id: str) -> dict[str, Any]:
    return {
        "method": "Network.loadingFailed",
        "params": {"requestId": request_id, "errorText": "fixture failure"},
    }


def main_frame_navigated_event(url: str) -> dict[str, Any]:
    return {
        "method": "Page.frameNavigated",
        "params": {"frame": {"id": "main", "url": url}},
    }


def video_aweme(work_id: str, title: str, author: str) -> dict[str, Any]:
    return {
        "aweme_id": work_id,
        "desc": title,
        "create_time": 1_700_000_000,
        "author": {"nickname": author},
        "video": {
            "bit_rate": [
                {
                    "gear_name": "h265_2160p",
                    "is_h265": 1,
                    "bit_rate": 9_000_000,
                    "play_addr": {
                        "width": 3840,
                        "height": 2160,
                        "url_list": [
                            f"https://media.invalid/{work_id}/h265-2160.mp4"
                        ],
                    },
                },
                {
                    "gear_name": "h264_1080p",
                    "is_h265": 0,
                    "bit_rate": 6_000_000,
                    "play_addr": {
                        "width": 1920,
                        "height": 1080,
                        "url_list": [
                            f"https://media.invalid/{work_id}/h264-1080.mp4"
                        ],
                    },
                },
                {
                    "gear_name": "h264_720p",
                    "codec_type": "h264",
                    "bit_rate": 3_000_000,
                    "play_addr": {
                        "width": 1280,
                        "height": 720,
                        "url_list": [
                            f"https://media.invalid/{work_id}/h264-720.mp4"
                        ],
                    },
                },
            ]
        },
    }


def image_aweme(work_id: str, author: str) -> dict[str, Any]:
    return {
        "aweme_id": work_id,
        "desc": "图文作品",
        "create_time": 1_700_000_100,
        "author": {"nickname": author},
        "image_post_info": {
            "images": [
                {
                    "display_image": {
                        "width": 1440,
                        "height": 1920,
                        "url_list": [
                            f"https://media.invalid/{work_id}/image-01.webp"
                        ],
                    }
                },
                {
                    "display_image": {
                        "width": 1080,
                        "height": 1440,
                        "url_list": [
                            f"https://media.invalid/{work_id}/image-02.webp"
                        ],
                    }
                },
            ]
        },
    }


def enumerator_for(
    client: FakeCdpClient,
    *,
    short_url_resolver: Any = None,
    interactive_login: bool = False,
    login_timeout: float = 1,
    cookie_header: str = "sessionid=temporary",
    canonical_cookie_header: str | None = None,
    scan_timeout: float = 1,
) -> tuple[DouyinEnumerator, FakeProvider]:
    provider = FakeProvider(
        client,
        cookie_header=cookie_header,
        canonical_cookie_header=canonical_cookie_header,
    )
    enumerator = DouyinEnumerator(
        provider,
        short_url_resolver=short_url_resolver,
        scan_timeout=scan_timeout,
        event_timeout=0,
        max_idle_rounds=2,
        max_scrolls=4,
        interactive_login=interactive_login,
        login_timeout=login_timeout,
    )
    return enumerator, provider


def check_identification() -> None:
    target = identify_douyin_target(
        "复制口令 https://v.douyin.com/offline-short/ 打开抖音",
        short_url_resolver=lambda _url: "https://www.douyin.com/user/SEC_UID",
    )
    assert target.source is SourceKind.DOUYIN_PROFILE
    assert target.was_short_link
    assert "offline-short" not in repr(target)

    collection = identify_douyin_target(
        "https://www.douyin.com/collection/7350000000000000000"
    )
    assert collection.source is SourceKind.DOUYIN_COLLECTION

    single = identify_douyin_target("https://www.douyin.com/video/1234567890")
    assert single.source is SourceKind.SINGLE_LINK


def check_anonymous_session_provider() -> None:
    chromium = FakeChromium()
    provider = DouyinSessionProvider(lambda: chromium)
    session = provider.open("https://www.douyin.com/user/SESSION")
    assert chromium.started
    assert chromium.opened_page == "about:blank"
    assert [name for name, _params in chromium.client.commands] == [
        "Network.enable",
        "Page.enable",
        "Runtime.enable",
        "Network.setCacheDisabled",
        "Browser.getVersion",
        "Network.setUserAgentOverride",
        "Page.navigate",
    ]
    network_params = chromium.client.commands[0][1]
    assert network_params["maxResourceBufferSize"] >= 1_000_000
    ua_params = chromium.client.commands[-2][1]
    assert "HeadlessChrome" not in ua_params["userAgent"]
    assert "Chrome/150.0.0.0" in ua_params["userAgent"]
    assert session.cookie_header() == "temporary_cookie=value"
    assert "temporary_cookie" not in repr(session)
    session.close()
    assert chromium.closed
    assert session.cookie_header() == ""


def check_isolated_persistent_profile_lifecycle() -> None:
    launched: list[FakeBrowserProcess] = []

    def fake_popen(args: list[str], **_kwargs: Any) -> FakeBrowserProcess:
        process = FakeBrowserProcess(args)
        launched.append(process)
        return process

    with tempfile.TemporaryDirectory(prefix="feichuan-profile-smoke-") as temp:
        root = Path(temp)
        local_app_data = root / "LocalAppData"
        browser = root / "chrome.exe"
        browser.write_bytes(b"offline browser fixture")
        anonymous_profile = root / "anonymous-profile"
        anonymous_profile.mkdir()

        with (
            patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local_app_data)},
                clear=False,
            ),
            patch(
                "feichuan_downloader.chromium_session.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch.object(
                ChromiumSession,
                "_wait_until_ready",
                return_value={"Browser": "offline"},
            ),
        ):
            anonymous = ChromiumSession(browser=browser)
            with patch(
                "feichuan_downloader.chromium_session.tempfile.mkdtemp",
                return_value=str(anonymous_profile),
            ):
                anonymous.start()
            (anonymous_profile / "temporary-cookie-store").write_text(
                "ordinary-cookie-value",
                encoding="utf-8",
            )
            anonymous_args = launched[-1].args
            assert "--incognito" not in anonymous_args
            assert "--headless=new" in anonymous_args
            anonymous.close()
            assert not anonymous_profile.exists()

            persistent = ChromiumSession(
                browser=browser,
                headless=False,
                persistent_douyin_profile=True,
            )
            persistent.start()
            expected_profile = douyin_chromium_profile_dir()
            assert persistent.profile == expected_profile
            persistent_args = launched[-1].args
            assert "--incognito" not in persistent_args
            assert "--headless=new" not in persistent_args
            assert f"--user-data-dir={expected_profile}" in persistent_args
            assert not any("User Data" in arg for arg in persistent_args)

            encrypted_store = expected_profile / "Default" / "Cookies"
            encrypted_store.parent.mkdir(parents=True)
            encrypted_store.write_bytes(b"chrome-encrypted-session-fixture")
            try:
                clear_douyin_chromium_profile()
            except RuntimeError:
                pass
            else:
                raise AssertionError("running persistent profile must not be deleted")
            assert encrypted_store.exists()

            try:
                clear_douyin_chromium_profile(root / "outside-profile")
            except ValueError:
                pass
            else:
                raise AssertionError("out-of-scope profile path must be rejected")

            persistent.close()
            assert encrypted_store.exists(), "Chrome-owned login state must persist"
            assert persistent.clear_persistent_profile()
            assert not expected_profile.exists()
            assert not persistent.clear_persistent_profile()

            anonymous_provider = DouyinSessionProvider(interactive_login=False)
            anonymous_config = anonymous_provider._chromium_factory()
            assert anonymous_config.headless
            assert not anonymous_config.incognito
            assert not anonymous_config.persistent_douyin_profile

            login_provider = DouyinSessionProvider(interactive_login=True)
            login_config = login_provider._chromium_factory()
            assert not login_config.headless
            assert not login_config.incognito
            assert login_config.persistent_douyin_profile
            assert not login_provider.clear_saved_login_profile()

            try:
                ChromiumSession(
                    browser=browser,
                    incognito=True,
                    persistent_douyin_profile=True,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("persistent profile must reject incognito mode")


def check_complete_profile() -> None:
    first = {
        "aweme_list": [
            video_aweme("1001", "第一页视频", "匿名作者"),
            image_aweme("1002", "匿名作者"),
        ],
        "has_more": 1,
        "max_cursor": 10,
        "total": 3,
        "user": {"nickname": "匿名作者", "aweme_count": 3},
    }
    second = {
        "aweme_list": [
            video_aweme("1001", "置顶重复", "匿名作者"),
            video_aweme("1003", "第二页视频", "匿名作者"),
        ],
        "has_more": 0,
        "max_cursor": 20,
    }
    client = FakeCdpClient(
        [
            response_event("post-1", "/aweme/v1/web/aweme/post/"),
            loading_finished_event("post-1"),
            response_event("post-2", "/aweme/v1/web/aweme/post/"),
            loading_finished_event("post-2"),
        ],
        {"post-1": first, "post-2": second},
    )
    enumerator, provider = enumerator_for(client)
    progress: list[tuple[int, str]] = []
    bundle = enumerator.scan(
        "https://www.douyin.com/user/SEC_UID",
        source=SourceKind.DOUYIN_PROFILE,
        on_progress=lambda count, message: progress.append((count, message)),
    )
    result = bundle.result
    assert provider.session.closed
    assert result.enumeration_complete
    assert result.incomplete_reason == ""
    assert result.unique_count == 3
    assert result.reported_count == 3
    assert result.author == "匿名作者"
    assert result.content_counts[ContentKind.VIDEO.value] == 2
    assert result.content_counts[ContentKind.IMAGE.value] == 1
    assert [item.work_id for item in result.items] == ["1001", "1002", "1003"]
    assert progress[-1][0] == 3
    assert client.body_attempts["post-1"] == 1

    video_media = bundle.media_for("1001")
    assert video_media[0].codec == "h264"
    assert video_media[0].height == 1080
    assert bundle.media_for("1002")[0].quality == "original-01"
    assert len(bundle.media_for("1002")) == 2
    assert bundle.cookie_header == "sessionid=temporary"
    assert video_media[0].cookie == "sessionid=temporary"
    public_text = repr(bundle)
    assert "media.invalid" not in public_text
    assert "sessionid" not in public_text

    first_descriptor = video_media[0]
    bundle.clear_sensitive()
    assert bundle.cleared and not bundle.media_by_work_id
    assert first_descriptor.cleared


def check_collection_cursor_loop() -> None:
    detail = {
        "mix_info": {
            "author": {"nickname": "合集作者"},
            "statis": {"updated_to_episode": 16},
        }
    }
    first = {
        "aweme_list": [video_aweme("2001", "合集一", "合集作者")],
        "has_more": 1,
        "cursor": 10,
    }
    looped = {
        "aweme_list": [
            video_aweme("2001", "重复项", "合集作者"),
            video_aweme("2002", "合集二", "合集作者"),
        ],
        "has_more": 1,
        "cursor": 10,
    }
    client = FakeCdpClient(
        [
            response_event("mix-detail", "/aweme/v1/web/mix/detail/"),
            loading_finished_event("mix-detail"),
            response_event("mix-1", "/aweme/v1/web/mix/aweme/"),
            loading_finished_event("mix-1"),
            response_event("mix-2", "/aweme/v1/web/mix/aweme/"),
            loading_finished_event("mix-2"),
        ],
        {
            "mix-detail": detail,
            "mix-1": first,
            "mix-2": looped,
        },
    )
    enumerator, _provider = enumerator_for(client)
    bundle = enumerator.scan(
        "https://www.douyin.com/collection/7350000000000000000"
    )
    result = bundle.result
    assert not result.enumeration_complete
    assert "游标" in result.incomplete_reason
    assert result.unique_count == 2
    assert result.reported_count == 16
    assert result.author == "合集作者"
    bundle.clear_sensitive()


def check_timeout_incomplete() -> None:
    page = {
        "aweme_list": [video_aweme("3001", "未结束分页", "超时作者")],
        "has_more": 1,
        "max_cursor": 10,
    }
    client = FakeCdpClient(
        [
            response_event("timeout-1", "/aweme/v1/web/aweme/post/"),
            loading_finished_event("timeout-1"),
        ],
        {"timeout-1": page},
    )
    enumerator, _provider = enumerator_for(client, scan_timeout=0.03)
    bundle = enumerator.scan("https://www.douyin.com/user/TIMEOUT")
    assert not bundle.result.enumeration_complete
    assert "超时" in bundle.result.incomplete_reason
    assert bundle.result.unique_count == 1
    assert client.scroll_count == 4
    bundle.clear_sensitive()


def check_progress_driven_timeout_and_scroll_growth() -> None:
    endpoint = "/aweme/v1/web/aweme/post/"
    first = {
        "aweme_list": [video_aweme("progress-1", "Progress one", "Progress")],
        "has_more": 1,
        "max_cursor": 10,
    }
    final = {
        "aweme_list": [video_aweme("progress-2", "Progress two", "Progress")],
        "has_more": 0,
        "max_cursor": 20,
    }
    clock = FakeMonotonicClock()
    timed_client = ProgressTimedFakeCdpClient(
        [
            response_event("progress-first", endpoint),
            loading_finished_event("progress-first"),
            response_event("progress-final", endpoint),
            loading_finished_event("progress-final"),
        ],
        {"progress-first": first, "progress-final": final},
        clock=clock,
    )
    timed_enumerator, _provider = enumerator_for(timed_client, scan_timeout=1)
    with patch(
        "feichuan_downloader.douyin_enumerator.time.monotonic",
        new=clock,
    ):
        timed_bundle = timed_enumerator.scan(
            "https://www.douyin.com/user/PROGRESS_TIMEOUT"
        )
    assert timed_bundle.result.enumeration_complete
    assert timed_bundle.result.unique_count == 2
    assert clock.value > 1
    timed_bundle.clear_sensitive()

    growth_client = FakeCdpClient(
        [],
        {},
        scroll_observations=[
            {"moved": False, "before": 0, "after": 0, "height": 1000},
            {"moved": False, "before": 0, "after": 0, "height": 2000},
            {"moved": False, "before": 0, "after": 0, "height": 2000},
            {"moved": False, "before": 0, "after": 0, "height": 2000},
        ],
    )
    growth_enumerator, _provider = enumerator_for(growth_client)
    growth_bundle = growth_enumerator.scan(
        "https://www.douyin.com/user/SCROLL_GROWTH"
    )
    assert not growth_bundle.result.enumeration_complete
    assert growth_client.scroll_count == 4
    growth_bundle.clear_sensitive()


def check_pagination_loading_failure_stays_incomplete() -> None:
    final_page = {
        "aweme_list": [video_aweme("failure-final", "尾页", "失败作者")],
        "has_more": 0,
        "max_cursor": 0,
    }
    client = FakeCdpClient(
        [
            response_event("failed-page", "/aweme/v1/web/aweme/post/"),
            loading_failed_event("failed-page"),
            response_event("final-page", "/aweme/v1/web/aweme/post/"),
        ],
        {"final-page": final_page},
    )
    enumerator, _provider = enumerator_for(client)
    bundle = enumerator.scan("https://www.douyin.com/user/FAILED_PAGE")
    assert not bundle.result.enumeration_complete
    assert "分页接口加载失败" in bundle.result.incomplete_reason
    bundle.clear_sensitive()


def check_api_errors_cannot_claim_complete() -> None:
    endpoint = "/aweme/v1/web/aweme/post/"
    status_payload = {
        "status_code": 8,
        "status_msg": "temporary service error",
        "aweme_list": [],
        "has_more": 0,
    }
    payload_client = FakeCdpClient(
        [
            response_event("status-error", endpoint),
            loading_finished_event("status-error"),
        ],
        {"status-error": status_payload},
    )
    enumerator, _provider = enumerator_for(payload_client)
    payload_bundle = enumerator.scan("https://www.douyin.com/user/STATUS_ERROR")
    assert not payload_bundle.result.enumeration_complete
    assert "非成功状态" in payload_bundle.result.incomplete_reason
    payload_bundle.clear_sensitive()

    http_client = FakeCdpClient(
        [response_event("http-error", endpoint, status=429)],
        {},
    )
    enumerator, _provider = enumerator_for(http_client)
    http_bundle = enumerator.scan("https://www.douyin.com/user/HTTP_ERROR")
    assert not http_bundle.result.enumeration_complete
    assert "HTTP 429" in http_bundle.result.incomplete_reason
    http_bundle.clear_sensitive()


def check_unrelated_event_noise_reaches_idle_completion() -> None:
    client = NoisyFakeCdpClient(
        dom_items=[
            {
                "href": f"https://www.douyin.com/video/{5000 + index}",
                "title": f"DOM 作品 {index}",
            }
            for index in range(8)
        ]
    )
    enumerator, _provider = enumerator_for(client)
    bundle = enumerator.scan("https://www.douyin.com/user/NOISY_PAGE")
    assert not bundle.result.enumeration_complete
    assert bundle.result.unique_count == 8
    assert "DOM" in bundle.result.incomplete_reason
    assert client.next_calls <= 4, client.next_calls
    bundle.clear_sensitive()


def check_dom_only_and_captcha() -> None:
    dom_client = FakeCdpClient(
        [],
        {},
        dom_author="DOM 作者",
        dom_reported_count="作品 127",
        dom_items=[
            {
                "href": "https://www.douyin.com/note/4001",
                "title": "仅 DOM 图文",
            }
        ],
    )
    enumerator, _provider = enumerator_for(dom_client)
    dom_bundle = enumerator.scan("https://www.douyin.com/user/DOM_ONLY")
    assert not dom_bundle.result.enumeration_complete
    assert "DOM" in dom_bundle.result.incomplete_reason
    assert dom_bundle.result.unique_count == 1
    assert dom_bundle.result.author == "DOM 作者"
    assert dom_bundle.result.reported_count == 127
    assert dom_bundle.result.items[0].author == "DOM 作者"
    assert dom_bundle.result.items[0].content_type is ContentKind.IMAGE
    dom_bundle.clear_sensitive()

    captcha_client = FakeCdpClient([], {}, captcha=True)
    enumerator, _provider = enumerator_for(captcha_client)
    captcha_bundle = enumerator.scan("https://www.douyin.com/user/CAPTCHA")
    assert not captcha_bundle.result.enumeration_complete
    assert "验证码" in captcha_bundle.result.incomplete_reason
    captcha_bundle.clear_sensitive()


def check_empty_body_and_login_gate() -> None:
    endpoint = "/aweme/v1/web/aweme/post/"
    empty_client = FakeCdpClient(
        [
            response_event("empty-body", endpoint),
            loading_finished_event("empty-body"),
        ],
        {"empty-body": ""},
    )
    enumerator, _provider = enumerator_for(empty_client)
    empty_bundle = enumerator.scan("https://www.douyin.com/user/EMPTY_BODY")
    assert not empty_bundle.result.enumeration_complete
    assert empty_bundle.result.unique_count == 0
    assert "正文为空或无法解析" in empty_bundle.result.incomplete_reason
    empty_bundle.clear_sensitive()

    first_page = {
        "aweme_list": [video_aweme("login-1", "首批作品", "登录作者")],
        "has_more": 1,
        "max_cursor": 10,
        "user": {"nickname": "登录作者", "aweme_count": 336},
    }
    login_client = FakeCdpClient(
        [
            response_event("login-page", endpoint),
            loading_finished_event("login-page"),
        ],
        {"login-page": first_page},
        access_gate="login_required",
    )
    enumerator, _provider = enumerator_for(login_client)
    login_bundle = enumerator.scan("https://www.douyin.com/user/LOGIN_GATE")
    assert not login_bundle.result.enumeration_complete
    assert login_bundle.result.unique_count == 1
    assert login_bundle.result.reported_count == 336
    assert "登录后才能继续枚举" in login_bundle.result.incomplete_reason
    login_bundle.clear_sensitive()

    assert '[data-e2e="user-post-list"]' in _DOM_DISCOVERY_EXPRESSION
    assert '[data-e2e="page-footer"]' in _DOM_DISCOVERY_EXPRESSION
    assert ".route-scroll-container" in _SCROLL_EXPRESSION
    assert "login_required" in _ACCESS_GATE_EXPRESSION


def check_login_cookie_recognition() -> None:
    page_url = "https://www.douyin.com/user/COOKIE_PROBE"
    secret = "offline-login-cookie-secret"

    not_logged_in = CookieProbeClient(
        [
            {"name": "ttwid", "value": "ordinary-browser-cookie"},
            {"name": "sessionid", "value": ""},
        ]
    )
    assert not DouyinEnumerator._has_login_cookie(not_logged_in, page_url)

    logged_in = CookieProbeClient(
        [
            {
                "name": "sessionid_ss",
                "value": secret,
                "domain": ".douyin.com",
            }
        ]
    )
    assert DouyinEnumerator._has_login_cookie(logged_in, page_url)
    assert logged_in.commands == [
        (
            "Network.getCookies",
            {"urls": [page_url, "https://www.douyin.com/"]},
        )
    ]
    assert secret not in repr(logged_in.commands)

    malformed = CookieProbeClient({"name": "sessionid", "value": secret})
    assert not DouyinEnumerator._has_login_cookie(malformed, page_url)
    failed = CookieProbeClient([], fail=True)
    assert not DouyinEnumerator._has_login_cookie(failed, page_url)


def check_interactive_login_reload_and_cookie_redaction() -> None:
    endpoint = "/aweme/v1/web/aweme/post/"
    secret = "offline-interactive-cookie-secret"
    target_url = "https://www.iesdouyin.com/share/user/INTERACTIVE_LOGIN"
    canonical_cookie_header = (
        f"sessionid_ss={secret}; sid_guard=secondary-secret"
    )

    before_login = {
        "aweme_list": [
            video_aweme("anonymous-only", "Before login", "Anonymous author")
        ],
        "has_more": 1,
        "max_cursor": 10,
        "user": {"nickname": "Anonymous author", "aweme_count": 999},
    }
    after_reload_first = {
        "aweme_list": [video_aweme("login-1", "Reloaded first", "Login author")],
        "has_more": 1,
        "max_cursor": 10,
        "user": {"nickname": "Login author", "aweme_count": 2},
    }
    after_reload_final = {
        "aweme_list": [video_aweme("login-2", "After login", "Login author")],
        "has_more": 0,
        "max_cursor": 20,
    }
    client = InteractiveLoginFakeCdpClient(
        [
            response_event("pre-login", endpoint),
            loading_finished_event("pre-login"),
        ],
        {
            "pre-login": before_login,
            "stale-after-reload": before_login,
            "reload-first": after_reload_first,
            "reload-final": after_reload_final,
        },
        reload_events=[
            # These stale anonymous-document events must not be consumed before
            # the reload's new top-level frame has navigated.
            response_event("stale-after-reload", endpoint),
            loading_finished_event("stale-after-reload"),
            main_frame_navigated_event(
                "https://www.douyin.com/user/INTERACTIVE_LOGIN"
            ),
            response_event("reload-first", endpoint),
            loading_finished_event("reload-first"),
        ],
        pagination_events=[
            response_event("reload-final", endpoint),
            loading_finished_event("reload-final"),
        ],
        login_cookie=secret,
    )
    enumerator, provider = enumerator_for(
        client,
        interactive_login=True,
        login_timeout=0.1,
        cookie_header="ttwid=share-domain-cookie",
        canonical_cookie_header=canonical_cookie_header,
    )
    progress: list[tuple[int, str]] = []
    handler = RecordingLogHandler()
    package_logger = logging.getLogger("feichuan_downloader")
    root_logger = logging.getLogger()
    old_package_level = package_logger.level
    old_root_level = root_logger.level
    package_logger.addHandler(handler)
    root_logger.addHandler(handler)
    package_logger.setLevel(logging.DEBUG)
    root_logger.setLevel(logging.DEBUG)
    try:
        bundle = enumerator.scan(
            target_url,
            on_progress=lambda count, message: progress.append((count, message)),
        )
    finally:
        package_logger.removeHandler(handler)
        root_logger.removeHandler(handler)
        package_logger.setLevel(old_package_level)
        root_logger.setLevel(old_root_level)

    result = bundle.result
    assert provider.session.closed
    assert result.enumeration_complete
    assert result.incomplete_reason == ""
    assert result.unique_count == 2
    assert [item.work_id for item in result.items] == ["login-1", "login-2"]
    assert "anonymous-only" not in {item.work_id for item in result.items}
    assert result.author == "Login author"
    assert result.reported_count == 2
    assert client.open_login_count == 1
    assert client.cookie_probe_count == 1
    assert client.drain_count == 1
    assert client.reload_count == 1
    assert client.reload_navigation_seen
    assert client.pagination_injected
    assert client.scroll_count >= 2
    assert client.body_attempts == Counter(
        {"pre-login": 1, "reload-first": 1, "reload-final": 1}
    )
    assert client.body_attempts["stale-after-reload"] == 0
    assert progress[-1][0] == 2
    assert (0, "登录成功，正在重新扫描作者主页。") in progress

    open_login_index = next(
        index
        for index, (method, params) in enumerate(client.command_log)
        if method == "Runtime.evaluate"
        and "__FEICHUAN_DOUYIN_OPEN_LOGIN__"
        in str(params.get("expression") or "")
    )
    cookie_index = next(
        index
        for index, (method, _params) in enumerate(client.command_log)
        if method == "Network.getCookies"
    )
    reload_index = next(
        index
        for index, (method, _params) in enumerate(client.command_log)
        if method == "Page.reload"
    )
    assert open_login_index < cookie_index < reload_index
    assert "__FEICHUAN_DOUYIN_OPEN_LOGIN__" in _OPEN_LOGIN_EXPRESSION

    # The cookie remains available only through the sensitive in-memory fields.
    assert secret in bundle.cookie_header
    assert "ttwid=share-domain-cookie" in bundle.cookie_header
    assert secret in bundle.media_for("login-1")[0].cookie
    assert provider.session.cookie_urls == [target_url, "https://www.douyin.com/"]
    observable_text = "\n".join(
        [
            repr(bundle),
            repr(result),
            result.incomplete_reason,
            repr(client.command_log),
            *(message for _count, message in progress),
            *handler.messages,
        ]
    )
    assert secret not in observable_text
    assert "secondary-secret" not in observable_text

    descriptor = bundle.media_for("login-1")[0]
    bundle.clear_sensitive()
    assert bundle.cookie_header == ""
    assert descriptor.cookie == ""


def check_captcha_false_positive_guards() -> None:
    # Normal Douyin pages preload this SDK and keep a nocaptcha iframe hidden.
    # Neither is evidence that a user-visible challenge is active.
    sdk_event = {
        "method": "Network.responseReceived",
        "params": {
            "type": "Script",
            "response": {
                "url": (
                    "https://lf-cdn.example/rc-verifycenter/sec_sdk_build/"
                    "captcha/index.js"
                )
            },
        },
    }
    hidden_frame_navigation = {
        "method": "Page.frameNavigated",
        "params": {
            "frame": {
                "id": "child",
                "parentId": "main",
                "url": "https://verify.example/captcha/index.html",
            }
        },
    }
    challenge_navigation = {
        "method": "Page.frameNavigated",
        "params": {
            "frame": {
                "id": "main",
                "url": "https://verify.example/verifycenter/challenge",
            }
        },
    }
    assert not DouyinEnumerator._event_indicates_captcha(sdk_event)
    assert not DouyinEnumerator._event_indicates_captcha(hidden_frame_navigation)
    assert DouyinEnumerator._event_indicates_captcha(challenge_navigation)

    # Keep the DOM probe tied to actual visibility.  In particular, the old
    # blanket document.querySelector check matched #nocaptcha-container even
    # when it was display:none and 0x0.
    assert "getComputedStyle" in _CAPTCHA_EXPRESSION
    assert "getBoundingClientRect" in _CAPTCHA_EXPRESSION
    assert "style.display === 'none'" in _CAPTCHA_EXPRESSION
    assert "rect.width >= 2" in _CAPTCHA_EXPRESSION
    assert "iframe[src*=\"captcha\" i]" in _CAPTCHA_EXPRESSION


def check_publication_timezone() -> None:
    # 00:30 in China is still the previous UTC date.  Filenames must use the
    # date users see on Douyin, not the UTC calendar date.
    timestamp = int(datetime(2026, 7, 18, 16, 30, tzinfo=timezone.utc).timestamp())
    aweme = video_aweme("timezone-1", "跨日作品", "时区作者")
    aweme["create_time"] = timestamp
    parsed = DouyinEnumerator()._parse_aweme(aweme)
    assert parsed is not None
    item, _candidates = parsed
    assert item.published_at is not None
    assert item.published_at.utcoffset().total_seconds() == 8 * 60 * 60
    assert publication_date(item.published_at) == "20260719"


def check_dom_reported_count_parsing() -> None:
    assert DouyinEnumerator._reported_count_from_text("作品 127") == 127
    assert DouyinEnumerator._reported_count_from_text("1.2万") == 12_000
    assert DouyinEnumerator._reported_count_from_text("3.4w") == 34_000
    assert DouyinEnumerator._reported_count_from_text("1,234") == 1_234
    assert DouyinEnumerator._reported_count_from_text("暂无") is None
    assert '[data-e2e="user-info"] h1' in _DOM_METADATA_EXPRESSION
    assert '[data-e2e="user-tab-count"]' in _DOM_METADATA_EXPRESSION


def check_live_cards_are_not_treated_as_videos() -> None:
    live_aweme = video_aweme("live-1", "直播卡片", "直播作者")
    live_aweme["aweme_type"] = 101
    parsed = DouyinEnumerator()._parse_aweme(live_aweme)
    assert parsed is not None
    live_item, candidates = parsed
    assert live_item.content_type is ContentKind.LIVE
    assert not candidates

    recorded_video = video_aweme("video-zero-live", "普通视频", "普通作者")
    recorded_video["is_live"] = "0"
    parsed_video = DouyinEnumerator()._parse_aweme(recorded_video)
    assert parsed_video is not None
    assert parsed_video[0].content_type is ContentKind.VIDEO


def main() -> None:
    check_identification()
    check_anonymous_session_provider()
    check_isolated_persistent_profile_lifecycle()
    check_complete_profile()
    check_collection_cursor_loop()
    check_timeout_incomplete()
    check_progress_driven_timeout_and_scroll_growth()
    check_pagination_loading_failure_stays_incomplete()
    check_api_errors_cannot_claim_complete()
    check_unrelated_event_noise_reaches_idle_completion()
    check_dom_only_and_captcha()
    check_empty_body_and_login_gate()
    check_login_cookie_recognition()
    check_interactive_login_reload_and_cookie_redaction()
    check_captcha_false_positive_guards()
    check_publication_timezone()
    check_dom_reported_count_parsing()
    check_live_cards_are_not_treated_as_videos()
    print("douyin enumerator smoke passed")


if __name__ == "__main__":
    main()
