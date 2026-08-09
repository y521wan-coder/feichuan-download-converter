"""纯离线抖音 Playlet 子合集扫描与协调器路由测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import (  # noqa: E402
    DownloadCoordinator,
    PreparedScan,
    classify_url,
)
from feichuan_downloader.douyin_enumerator import (  # noqa: E402
    DouyinEnumerator,
    identify_douyin_target,
)
from feichuan_downloader.models import (  # noqa: E402
    ContentKind,
    Platform,
    ScanResult,
    SourceKind,
    WorkItem,
)


PLAYLET_ID = "7525000000000000000"
DIRECT_URL = f"https://www.douyin.com/share/playlet/detail/{PLAYLET_ID}"
CANONICAL_URL = f"https://www.iesdouyin.com/share/playlet/detail/{PLAYLET_ID}/"
SHORT_URL = "https://v.douyin.com/OFFLINE-PLAYLET/"


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = dict(payload or {})
        self.status_code = int(status_code)
        self.url = str(url)
        self.headers = dict(headers or {})
        self.text = json.dumps(self._payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")
        self.ok = self.status_code < 400
        self.closed = False

    def json(self) -> dict[str, Any]:
        return dict(self._payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"offline HTTP {self.status_code}",
                response=self,
            )

    def close(self) -> None:
        self.closed = True


class FakePlayletSession:
    """A requests-like session that never touches the network."""

    def __init__(
        self,
        detail: dict[str, Any],
        pages: dict[str, dict[str, Any]],
    ) -> None:
        self.detail = detail
        self.pages = pages
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.head_calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def __enter__(self) -> "FakePlayletSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(self, url: str, *args: object, **kwargs: Any) -> FakeResponse:
        del args
        copied = _copy_call_kwargs(kwargs)
        self.get_calls.append((str(url), copied))
        path = urlsplit(str(url)).path.lower()
        params = copied.get("params") or {}
        if path.endswith("/web/api/playlet/detail/"):
            return FakeResponse(self.detail, url=str(url))
        if path.endswith("/web/api/playlet/item/list/"):
            cursor = str(params.get("cursor") or "0")
            if cursor not in self.pages:
                raise AssertionError(f"unexpected offline cursor: {cursor}")
            return FakeResponse(self.pages[cursor], url=str(url))
        raise AssertionError(f"unexpected offline Playlet GET: {url}")

    def head(self, url: str, *args: object, **kwargs: Any) -> FakeResponse:
        del args
        copied = _copy_call_kwargs(kwargs)
        self.head_calls.append((str(url), copied))
        parsed = urlsplit(str(url))
        query = parse_qs(parsed.query)
        params = copied.get("params") or {}
        video_id = str(
            params.get("video_id")
            or (query.get("video_id") or [""])[0]
        )
        ratio = str(params.get("ratio") or (query.get("ratio") or [""])[0])
        if not video_id:
            raise AssertionError("media HEAD did not include video_id")

        # The first work exercises 1080p -> 720p fallback.  Every other work
        # resolves on the preferred 1080p no-watermark endpoint.
        is_watermarked = "/playwm/" in parsed.path.lower()
        if video_id == "VID-01" and ratio == "1080p" and not is_watermarked:
            return FakeResponse(status_code=404, url=str(url))
        resolved_ratio = ratio or "720p"
        final_url = (
            f"https://cdn.invalid/playlet/{video_id}-{resolved_ratio}.mp4"
        )
        return FakeResponse(
            status_code=200,
            url=final_url,
            headers={"Content-Type": "video/mp4"},
        )

    def close(self) -> None:
        self.closed = True


class FailingCdpProvider:
    def open(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("Playlet scan unexpectedly opened Chromium/CDP")


def _copy_call_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    copied = dict(kwargs)
    if isinstance(copied.get("params"), dict):
        copied["params"] = dict(copied["params"])
    if isinstance(copied.get("headers"), dict):
        copied["headers"] = dict(copied["headers"])
    return copied


def playlet_detail(
    *,
    total: int = 16,
    restricted: bool = False,
) -> dict[str, Any]:
    return {
        "status_code": 0,
        "playlet_info": {
            "playlet_id": PLAYLET_ID,
            "playlet_name": "没爱不行 情绪与价值",
            "total_episode": total,
            "is_charge_series": restricted,
            "charge_episodes": [1] if restricted else [],
            "author": {"nickname": "离线作者"},
            "statis": {
                "total_updated_to_episode": total,
                "updated_to_episode": total,
            },
        },
    }


def playlet_aweme(index: int, *, title: str | None = None) -> dict[str, Any]:
    return {
        "aweme_id": f"880000000000000{index:02d}",
        "desc": title or f"第 {index:02d} 集",
        "create_time": 1_720_000_000 + index,
        "author": {"nickname": "离线作者"},
        "video": {
            "vid": f"VID-{index:02d}",
            "width": 1080,
            "height": 1920,
        },
    }


def playlet_page(
    items: list[dict[str, Any]],
    *,
    cursor: int,
    has_more: int,
    status_code: int = 0,
) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "aweme_list": list(items),
        "cursor": cursor,
        "has_more": has_more,
    }


def enumerator_for(session: FakePlayletSession) -> DouyinEnumerator:
    return DouyinEnumerator(
        FailingCdpProvider(),
        playlet_session_factory=lambda: session,
        scan_timeout=1,
        event_timeout=0,
    )


def check_identification_and_normalization() -> None:
    direct = identify_douyin_target(
        DIRECT_URL + "?enter_from=share&token=must-not-survive"
    )
    assert direct.source is SourceKind.DOUYIN_COLLECTION
    assert direct.route_family == "playlet"
    assert direct.url == CANONICAL_URL
    assert "token" not in repr(direct)

    short = identify_douyin_target(
        f"复制口令 {SHORT_URL} 去抖音查看",
        short_url_resolver=lambda _url: DIRECT_URL + "?from=offline-secret",
    )
    assert short.source is SourceKind.DOUYIN_COLLECTION
    assert short.route_family == "playlet"
    assert short.was_short_link
    assert short.url == CANONICAL_URL
    assert "offline-secret" not in repr(short)
    assert classify_url(DIRECT_URL) is SourceKind.DOUYIN_COLLECTION


def check_complete_10_plus_6_scan_and_media_resolution() -> None:
    first_ten = [playlet_aweme(index) for index in range(1, 11)]
    duplicate_ten = playlet_aweme(10, title="重复项不应覆盖原题目")
    final_six = [playlet_aweme(index) for index in range(11, 17)]
    session = FakePlayletSession(
        playlet_detail(total=16),
        {
            "0": playlet_page(first_ten, cursor=10, has_more=1),
            "10": playlet_page(
                [duplicate_ten, *final_six],
                cursor=16,
                has_more=0,
            ),
        },
    )
    progress: list[tuple[int, str]] = []
    bundle = enumerator_for(session).scan(
        DIRECT_URL,
        source=SourceKind.DOUYIN_COLLECTION,
        on_progress=lambda count, message: progress.append((count, message)),
    )
    result = bundle.result

    assert session.closed
    assert result.source is SourceKind.DOUYIN_COLLECTION
    assert result.enumeration_complete
    assert result.incomplete_reason == ""
    assert result.reported_count == 16
    assert result.unique_count == 16
    assert result.author == "离线作者"
    assert result.content_counts[ContentKind.VIDEO.value] == 16
    expected_ids = [f"880000000000000{index:02d}" for index in range(1, 17)]
    assert [item.work_id for item in result.items] == expected_ids
    assert result.items[9].title == "第 10 集"
    assert progress and progress[-1][0] == 16

    list_calls = [
        (url, kwargs)
        for url, kwargs in session.get_calls
        if urlsplit(url).path.lower().endswith("/web/api/playlet/item/list/")
    ]
    assert len(list_calls) == 2
    assert [str(kwargs["params"].get("cursor") or "0") for _url, kwargs in list_calls] == [
        "0",
        "10",
    ]
    for _url, kwargs in list_calls:
        params = kwargs["params"]
        assert str(params["playlet_id"]) == PLAYLET_ID
        assert int(params["count"]) == 10
        assert int(params["aid"]) == 1128

    first_media = bundle.media_for(expected_ids[0])
    second_media = bundle.media_for(expected_ids[1])
    assert first_media and first_media[0].media_url.endswith("VID-01-720p.mp4")
    assert second_media and second_media[0].media_url.endswith("VID-02-1080p.mp4")
    assert bundle.cookie_header == ""
    assert "cdn.invalid" not in repr(bundle)

    assert session.head_calls
    for url, kwargs in session.head_calls:
        explicit_headers = kwargs.get("headers") or {}
        assert not any(key.lower() == "cookie" for key in explicit_headers)
        assert "cookie" not in url.lower()
        assert "sessionid" not in url.lower()
        assert kwargs.get("stream") in (None, False)

    bundle.clear_sensitive()
    assert first_media[0].cleared


def check_cursor_loop_and_count_mismatch_are_incomplete() -> None:
    first_ten = [playlet_aweme(index) for index in range(1, 11)]
    final_six = [playlet_aweme(index) for index in range(11, 17)]
    loop_session = FakePlayletSession(
        playlet_detail(total=16),
        {
            "0": playlet_page(first_ten, cursor=10, has_more=1),
            "10": playlet_page(final_six, cursor=10, has_more=1),
        },
    )
    loop_bundle = enumerator_for(loop_session).scan(DIRECT_URL)
    assert loop_bundle.result.unique_count == 16
    assert not loop_bundle.result.enumeration_complete
    assert "游标" in loop_bundle.result.incomplete_reason
    loop_bundle.clear_sensitive()

    only_fifteen = [playlet_aweme(index) for index in range(1, 16)]
    mismatch_session = FakePlayletSession(
        playlet_detail(total=16),
        {
            "0": playlet_page(only_fifteen[:10], cursor=10, has_more=1),
            "10": playlet_page(only_fifteen[10:], cursor=15, has_more=0),
        },
    )
    mismatch_bundle = enumerator_for(mismatch_session).scan(DIRECT_URL)
    assert mismatch_bundle.result.reported_count == 16
    assert mismatch_bundle.result.unique_count == 15
    assert not mismatch_bundle.result.enumeration_complete
    assert mismatch_bundle.result.incomplete_reason
    mismatch_bundle.clear_sensitive()


def check_api_status_failures_are_safely_incomplete() -> None:
    detail_failure = FakePlayletSession(
        {"status_code": 2190008, "status_msg": "offline detail denied"},
        {},
    )
    detail_bundle = enumerator_for(detail_failure).scan(DIRECT_URL)
    assert detail_bundle.result.unique_count == 0
    assert not detail_bundle.result.enumeration_complete
    assert detail_bundle.result.incomplete_reason
    assert detail_failure.head_calls == []
    detail_bundle.clear_sensitive()

    first_ten = [playlet_aweme(index) for index in range(1, 11)]
    list_failure = FakePlayletSession(
        playlet_detail(total=16),
        {
            "0": playlet_page(first_ten, cursor=10, has_more=1),
            "10": playlet_page(
                [],
                cursor=10,
                has_more=0,
                status_code=2190008,
            ),
        },
    )
    list_bundle = enumerator_for(list_failure).scan(DIRECT_URL)
    assert list_bundle.result.reported_count == 16
    assert list_bundle.result.unique_count == 10
    assert not list_bundle.result.enumeration_complete
    assert list_bundle.result.incomplete_reason
    list_bundle.clear_sensitive()


def check_restricted_series_stops_before_items_or_media() -> None:
    session = FakePlayletSession(
        playlet_detail(total=16, restricted=True),
        {},
    )
    bundle = enumerator_for(session).scan(DIRECT_URL)
    result = bundle.result
    assert result.unique_count == 0
    assert not result.enumeration_complete
    assert "付费" in result.incomplete_reason or "受限" in result.incomplete_reason
    assert len(session.get_calls) == 1
    assert urlsplit(session.get_calls[0][0]).path.lower().endswith(
        "/web/api/playlet/detail/"
    )
    assert session.head_calls == []
    bundle.clear_sensitive()


class CoordinatorPlayletScanner:
    def __init__(self) -> None:
        self.identify_calls: list[str] = []
        self.scan_calls: list[tuple[str, SourceKind]] = []

    def identify(self, value: str) -> SimpleNamespace:
        self.identify_calls.append(value)
        return SimpleNamespace(
            source=SourceKind.DOUYIN_COLLECTION,
            url=CANONICAL_URL,
            route_family="playlet",
        )

    def scan(
        self,
        url: str,
        *,
        source: SourceKind,
        cancel_event: object,
        on_progress: Any,
    ) -> ScanResult:
        del cancel_event
        self.scan_calls.append((url, source))
        item = WorkItem(
            platform=Platform.DOUYIN,
            work_id="88000000000000001",
            content_type=ContentKind.VIDEO,
            title="协调器离线作品",
            author="离线作者",
            published_at=None,
            canonical_url="https://www.douyin.com/video/88000000000000001",
        )
        on_progress(1, "已发现 1 个 Playlet 作品。")
        return ScanResult(
            source=SourceKind.DOUYIN_COLLECTION,
            author="离线作者",
            reported_count=1,
            unique_count=1,
            content_counts={ContentKind.VIDEO: 1},
            enumeration_complete=True,
            items=(item,),
        )

    def cancel(self) -> None:
        return None


def check_coordinator_routes_playlet_to_batch_scan() -> None:
    scanner = CoordinatorPlayletScanner()

    def forbidden_downloader() -> object:
        raise AssertionError("Playlet unexpectedly entered generic/single downloader")

    coordinator = DownloadCoordinator(
        douyin_scanner_factory=lambda: scanner,
        downloader_factory=forbidden_downloader,
    )
    prepared = coordinator.scan_or_download(
        f"分享口令 {SHORT_URL} 和我一起看合集"
    )
    assert isinstance(prepared, PreparedScan)
    assert scanner.identify_calls
    assert scanner.scan_calls == [(CANONICAL_URL, SourceKind.DOUYIN_COLLECTION)]
    assert prepared.result.source is SourceKind.DOUYIN_COLLECTION
    assert prepared.result.unique_count == 1
    prepared.clear_sensitive()


def main() -> None:
    check_identification_and_normalization()
    check_complete_10_plus_6_scan_and_media_resolution()
    check_cursor_loop_and_count_mismatch_are_incomplete()
    check_api_status_failures_are_safely_incomplete()
    check_restricted_series_stops_before_items_or_media()
    check_coordinator_routes_playlet_to_batch_scan()
    print("douyin playlet smoke passed")


if __name__ == "__main__":
    main()
