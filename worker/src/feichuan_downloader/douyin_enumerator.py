"""抖音个人主页与官方合集的匿名批量枚举。

枚举器只把稳定作品元数据放进 ScanResult。带签名的媒体 URL 和本次匿名会话
Cookie 仅存在于 DouyinScanBundle/MediaDescriptor 的内存字段中。
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from .douyin_session import DouyinSessionProvider
from .models import (
    ContentKind,
    MediaDescriptor,
    Platform,
    ScanResult,
    SourceKind,
    WorkItem,
)


_DOUYIN_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_TRAILING_SHARE_PUNCTUATION = ".,!?;:，。！？；：)]}>》】」』'\""
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)
_DOUYIN_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_POST_PATH = "/aweme/v1/web/aweme/post/"
_AWEME_DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
_MIX_DETAIL_PATH = "/aweme/v1/web/mix/detail/"
_MIX_AWEME_PATH = "/aweme/v1/web/mix/aweme/"
_PLAYLET_DETAIL_PATH = "/web/api/playlet/detail/"
_PLAYLET_ITEMS_PATH = "/web/api/playlet/item/list/"
_PLAYLET_ROUTE_RE = re.compile(
    r"^/share/playlet/detail/(?P<playlet_id>\d+)/?$",
    re.IGNORECASE,
)
_PLAYLET_WEB_ORIGIN = "https://www.iesdouyin.com"
_PLAYLET_MEDIA_ORIGIN = "https://aweme.snssdk.com"
_PLAYLET_PAGE_SIZE = 10
_PLAYLET_MEDIA_ATTEMPTS = (
    ("play", "1080p", False),
    ("play", "720p", False),
    ("playwm", "720p", True),
)
_CAPTCHA_TOKENS = (
    "captcha",
    "verifycenter",
    "verify_center",
    "验证码",
    "完成验证",
    "安全验证",
    "风控",
)

_CAPTCHA_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_CAPTCHA__ */
  const isVisible = (element) => {
    if (!(element instanceof Element) || element.hidden ||
        element.getAttribute('aria-hidden') === 'true') {
      return false;
    }
    for (let node = element; node; node = node.parentElement) {
      const style = window.getComputedStyle(node);
      const opacity = Number.parseFloat(style.opacity || '1');
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' ||
          (Number.isFinite(opacity) && opacity <= 0.01)) {
        return false;
      }
    }
    const rect = element.getBoundingClientRect();
    return rect.width >= 2 && rect.height >= 2 && rect.bottom > 0 &&
      rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const selectors = [
    '#captcha_container',
    '.captcha_verify_container',
    '[class*="captcha"]',
    'iframe[src*="captcha" i]',
    'iframe[src*="verifycenter" i]',
    'iframe[src*="verify_center" i]'
  ];
  return selectors.some((selector) =>
    Array.from(document.querySelectorAll(selector)).some(isVisible)
  );
})()
"""

_RSC_NOTE_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_RSC_NOTE__ */
  return Array.isArray(window.__pace_f) ? window.__pace_f : [];
})()
"""

_DOM_DISCOVERY_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_DOM__ */
  const root = document.querySelector('[data-e2e="user-post-list"]');
  if (!root) {
    return [];
  }
  const links = Array.from(root.querySelectorAll(
    'a[href*="/video/"], a[href*="/note/"]'
  )).filter((link) => !link.closest('footer, [data-e2e="page-footer"]'));
  return links.slice(0, 5000).map((link) => ({
    href: link.href || link.getAttribute('href') || '',
    title: link.getAttribute('title') || link.getAttribute('aria-label') ||
      (link.innerText || '').trim().slice(0, 200)
  }));
})()
"""

_DOM_METADATA_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_METADATA__ */
  const authorSelectors = [
    '[data-e2e="user-info"] h1',
    '[data-e2e="user-detail"] h1'
  ];
  let author = '';
  for (const selector of authorSelectors) {
    const element = document.querySelector(selector);
    const value = (element && element.textContent || '').trim();
    if (value) {
      author = value;
      break;
    }
  }
  const countElement = document.querySelector('[data-e2e="user-tab-count"]');
  return {
    author,
    reportedCountText: (countElement && countElement.textContent || '').trim()
  };
})()
"""

_SCROLL_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_SCROLL__ */
  const root = document.querySelector('[data-e2e="user-post-list"]');
  let scroller = root;
  while (scroller && scroller !== document.body &&
         scroller !== document.documentElement) {
    const style = window.getComputedStyle(scroller);
    if (scroller.scrollHeight > scroller.clientHeight + 2 &&
        /(auto|scroll|overlay)/.test(style.overflowY || '')) {
      break;
    }
    scroller = scroller.parentElement;
  }
  if (!scroller || scroller === document.body ||
      scroller === document.documentElement) {
    const routeScroller = document.querySelector('.route-scroll-container');
    if (routeScroller &&
        routeScroller.scrollHeight > routeScroller.clientHeight + 2) {
      scroller = routeScroller;
    }
  }
  if (scroller && scroller !== document.body &&
      scroller !== document.documentElement) {
    const before = scroller.scrollTop;
    scroller.scrollTop = scroller.scrollHeight;
    scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
    return {
      moved: scroller.scrollTop > before,
      before,
      after: scroller.scrollTop,
      height: scroller.scrollHeight,
      viewport: scroller.clientHeight
    };
  }
  const before = window.scrollY;
  const height = Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0
  );
  window.scrollTo(0, height);
  return {
    moved: window.scrollY > before,
    before,
    after: window.scrollY,
    height,
    viewport: window.innerHeight
  };
})()
"""

_ACCESS_GATE_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_ACCESS_GATE__ */
  const root = document.querySelector('[data-e2e="user-post-list"]');
  if (!root) {
    return '';
  }
  const isVisible = (element) => {
    if (!(element instanceof Element) || element.hidden ||
        element.getAttribute('aria-hidden') === 'true') {
      return false;
    }
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width >= 2 && rect.height >= 2 && rect.bottom > 0 &&
      rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const visibleText = Array.from(root.querySelectorAll('div, p, span, button'))
    .filter(isVisible)
    .map((element) => (element.textContent || '').trim())
    .join('\n');
  if (visibleText.includes('登录后查看更多作品')) {
    return 'login_required';
  }
  if (visibleText.includes('打开抖音看更多内容')) {
    return 'open_app_required';
  }
  return '';
})()
"""

_OPEN_LOGIN_EXPRESSION = r"""
(() => {
  /* __FEICHUAN_DOUYIN_OPEN_LOGIN__ */
  const root = document.querySelector('[data-e2e="user-post-list"]') || document;
  const buttons = Array.from(root.querySelectorAll('button'));
  const button = buttons.find((candidate) =>
    (candidate.textContent || '').trim().includes('立即登录')
  ) || Array.from(document.querySelectorAll('button')).find((candidate) =>
    (candidate.textContent || '').trim() === '登录'
  );
  if (!button) {
    return false;
  }
  button.click();
  return true;
})()
"""


class _SessionLike(Protocol):
    client: Any

    def cookie_header(self, url: str | None = None) -> str: ...

    def __enter__(self) -> "_SessionLike": ...

    def __exit__(self, *_args: object) -> None: ...


class _SessionProviderLike(Protocol):
    def open(self, url: str, *, cancel_event: Any = None) -> _SessionLike: ...


@dataclass(frozen=True, slots=True)
class DouyinTarget:
    """从分享文本识别出的抖音任务目标。"""

    source: SourceKind
    url: str = field(repr=False)
    extracted_url: str = field(repr=False)
    was_short_link: bool = False
    route_family: str = "single"
    playlet_id: str = ""

    def __repr__(self) -> str:
        parts = urlsplit(self.url)
        safe_location = f"{parts.scheme}://{parts.hostname or ''}{parts.path}"
        return (
            "DouyinTarget("
            f"source={self.source.value!r}, url={safe_location!r}, "
            f"was_short_link={self.was_short_link!r}, "
            f"route_family={self.route_family!r})"
        )


class DouyinScanBundle:
    """扫描结果与只驻留内存的临时媒体信息。"""

    __slots__ = ("result", "_media", "_media_view", "_cookie_header", "_cleared")

    def __init__(
        self,
        result: ScanResult,
        media_by_work_id: Mapping[str, tuple[MediaDescriptor, ...]],
        *,
        cookie_header: str = "",
    ) -> None:
        self.result = result
        self._media = {
            str(work_id): tuple(descriptors)
            for work_id, descriptors in media_by_work_id.items()
        }
        self._media_view = MappingProxyType(self._media)
        self._cookie_header = str(cookie_header or "")
        self._cleared = False

    @property
    def media_by_work_id(self) -> Mapping[str, tuple[MediaDescriptor, ...]]:
        return self._media_view

    @property
    def cookie_header(self) -> str:
        return self._cookie_header

    @property
    def cleared(self) -> bool:
        return self._cleared

    def media_for(self, work_id: str) -> tuple[MediaDescriptor, ...]:
        return self._media.get(str(work_id), ())

    def clear_sensitive(self) -> None:
        if self._cleared:
            return
        for descriptors in self._media.values():
            for descriptor in descriptors:
                descriptor.clear_sensitive()
        self._media.clear()
        self._cookie_header = ""
        self._cleared = True

    def __enter__(self) -> "DouyinScanBundle":
        return self

    def __exit__(self, *_args: object) -> None:
        self.clear_sensitive()

    def __repr__(self) -> str:
        descriptor_count = sum(len(items) for items in self._media.values())
        return (
            "DouyinScanBundle("
            f"works={self.result.unique_count}, descriptors={descriptor_count}, "
            f"complete={self.result.enumeration_complete}, "
            "cookie=<in-memory-redacted>)"
        )

    __str__ = __repr__

    def __getstate__(self) -> object:
        raise TypeError("抖音扫描包包含临时媒体 URL 和 Cookie，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("抖音扫描包包含临时媒体 URL 和 Cookie，禁止序列化。")


@dataclass(frozen=True, slots=True)
class _MediaCandidate:
    quality: str
    url: str = field(repr=False)
    width: int | None = None
    height: int | None = None
    codec: str = ""
    container: str = ""
    bitrate: int = 0

    @property
    def sort_key(self) -> tuple[int, int, int]:
        h264_priority = 1 if self.codec == "h264" else 0
        pixels = int(self.width or 0) * int(self.height or 0)
        return (h264_priority, pixels, self.bitrate)


@dataclass(slots=True)
class _EnumerationState:
    source: SourceKind
    items: dict[str, WorkItem] = field(default_factory=dict)
    media: dict[str, list[_MediaCandidate]] = field(default_factory=dict)
    author: str = ""
    reported_count: int | None = None
    saw_endpoint_response: bool = False
    saw_api_response: bool = False
    expecting_more: bool = False
    complete: bool = False
    incomplete_reason: str = ""
    cursors: dict[str, set[str]] = field(default_factory=dict)
    page_counts: Counter[str] = field(default_factory=Counter)
    body_failures: int = 0
    pending_responses: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ScrollObservation:
    moved: bool = False
    height: float | None = None


def extract_douyin_url(value: str) -> str:
    """从分享文本中提取第一个抖音 HTTP(S) URL。"""

    for match in _DOUYIN_URL_RE.finditer(str(value or "")):
        candidate = match.group(0).rstrip(_TRAILING_SHARE_PUNCTUATION)
        if _is_douyin_host(urlsplit(candidate).hostname or ""):
            return candidate
    raise ValueError("分享文本中没有找到有效的抖音链接。")


def _is_douyin_host(host: str) -> bool:
    host = str(host or "").lower().rstrip(".")
    return (
        host == "douyin.com"
        or host.endswith(".douyin.com")
        or host == "iesdouyin.com"
        or host.endswith(".iesdouyin.com")
    )


def _default_short_url_resolver(url: str) -> str:
    """匿名跟随短链 HTTP 跳转；不复用或持久化 Cookie。"""

    session = requests.Session()
    try:
        response = session.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
            allow_redirects=True,
            stream=True,
            timeout=(8, 15),
        )
        response.raise_for_status()
        return str(response.url)
    except requests.RequestException as exc:
        raise ValueError("无法解析抖音短链。") from exc
    finally:
        session.close()


def identify_douyin_target(
    value: str,
    *,
    short_url_resolver: Callable[[str], str] | None = None,
) -> DouyinTarget:
    """识别分享文本、短链、个人主页、官方合集或普通单作品链接。"""

    extracted = extract_douyin_url(value)
    extracted_parts = urlsplit(extracted)
    short_link = (extracted_parts.hostname or "").lower() == "v.douyin.com"
    resolved = extracted
    if short_link:
        resolver = short_url_resolver or _default_short_url_resolver
        resolved = str(resolver(extracted) or "").strip()
        if not resolved:
            raise ValueError("抖音短链没有返回目标地址。")

    parts = urlsplit(resolved)
    if parts.scheme not in {"http", "https"} or not _is_douyin_host(parts.hostname or ""):
        raise ValueError("链接解析结果不是有效的抖音地址。")
    playlet_match = _PLAYLET_ROUTE_RE.fullmatch(parts.path)
    path = parts.path.lower().rstrip("/") + "/"
    route_family = "single"
    playlet_id = ""
    if playlet_match:
        playlet_id = playlet_match.group("playlet_id")
        source = SourceKind.DOUYIN_COLLECTION
        route_family = "playlet"
        resolved = (
            f"{_PLAYLET_WEB_ORIGIN}/share/playlet/detail/{playlet_id}/"
        )
    elif "/collection/" in path:
        source = SourceKind.DOUYIN_COLLECTION
        route_family = "mix"
    elif "/user/" in path:
        source = SourceKind.DOUYIN_PROFILE
        route_family = "profile"
    else:
        source = SourceKind.SINGLE_LINK
    return DouyinTarget(
        source=source,
        url=resolved,
        extracted_url=extracted,
        was_short_link=short_link,
        route_family=route_family,
        playlet_id=playlet_id,
    )


class DouyinEnumerator:
    """枚举抖音主页、官方合集或 Playlet 系列子合集。"""

    def __init__(
        self,
        session_provider: _SessionProviderLike | None = None,
        *,
        short_url_resolver: Callable[[str], str] | None = None,
        playlet_session_factory: Callable[[], Any] | None = None,
        scan_timeout: float = 60.0,
        event_timeout: float = 0.5,
        max_idle_rounds: int = 12,
        max_scrolls: int = 120,
        interactive_login: bool = False,
        login_timeout: float = 300.0,
    ) -> None:
        if scan_timeout <= 0:
            raise ValueError("scan_timeout 必须大于 0。")
        if event_timeout < 0:
            raise ValueError("event_timeout 不能为负数。")
        if max_idle_rounds < 1 or max_scrolls < 1:
            raise ValueError("扫描轮数必须为正数。")
        if login_timeout <= 0:
            raise ValueError("登录等待时间必须大于 0。")
        self.interactive_login = bool(interactive_login)
        self.session_provider = session_provider or DouyinSessionProvider(
            interactive_login=self.interactive_login
        )
        self.short_url_resolver = short_url_resolver
        self.playlet_session_factory = playlet_session_factory or requests.Session
        self.scan_timeout = float(scan_timeout)
        self.event_timeout = float(event_timeout)
        self.max_idle_rounds = int(max_idle_rounds)
        self.max_scrolls = int(max_scrolls)
        self.login_timeout = float(login_timeout)

    def identify(self, value: str) -> DouyinTarget:
        return identify_douyin_target(
            value,
            short_url_resolver=self.short_url_resolver,
        )

    def scan(
        self,
        value: str,
        *,
        source: SourceKind | str | None = None,
        cancel_event: Any = None,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> DouyinScanBundle:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("任务已取消。")
        target = self.identify(value)
        if source is not None:
            expected_source = SourceKind(source)
            if expected_source in {
                SourceKind.DOUYIN_PROFILE,
                SourceKind.DOUYIN_COLLECTION,
            } and target.source is not expected_source:
                raise ValueError("输入链接类型与抖音扫描任务类型不一致。")
        if target.source not in {
            SourceKind.DOUYIN_PROFILE,
            SourceKind.DOUYIN_COLLECTION,
        }:
            raise ValueError("该抖音链接是单作品链接，不是个人主页或官方合集。")
        if target.route_family == "playlet":
            return self._scan_playlet_http(
                target,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        session = self.session_provider.open(target.url, cancel_event=cancel_event)
        with session:
            return self._scan_session(
                session,
                target,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )

    def scan_single_note(
        self,
        value: str,
        *,
        cancel_event: Any = None,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> DouyinScanBundle:
        """Capture one ``/note/`` detail response without enumerating page images."""

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("任务已取消。")
        target = self.identify(value)
        path = urlsplit(target.url).path.lower().rstrip("/") + "/"
        if target.source is not SourceKind.SINGLE_LINK or "/note/" not in path:
            raise ValueError("该抖音链接不是单条图文作品。")
        match = re.search(r"/(?:share/)?note/([^/?#]+)/", path)
        expected_work_id = match.group(1) if match else ""

        session = self.session_provider.open(target.url, cancel_event=cancel_event)
        with session:
            state = _EnumerationState(SourceKind.SINGLE_LINK)
            deadline = time.monotonic() + self.scan_timeout
            rsc_ready_at = time.monotonic() + min(
                8.0,
                max(0.5, self.scan_timeout / 2.0),
            )
            rsc_attempted = False
            idle_rounds = 0
            login_attempted = False
            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("任务已取消。")
                event = session.client.next_event(
                    timeout=min(self.event_timeout, max(0.0, deadline - time.monotonic()))
                )
                if event is not None:
                    if self._event_indicates_captcha(event):
                        raise RuntimeError("页面触发验证码或安全验证。")
                    self._handle_event(session.client, event, state)
                    idle_rounds = 0
                else:
                    idle_rounds += 1

                selected = next(
                    (
                        item
                        for item in state.items.values()
                        if item.content_type is ContentKind.IMAGE
                        and (not expected_work_id or item.work_id == expected_work_id)
                        and state.media.get(item.work_id)
                    ),
                    None,
                )
                if selected is not None:
                    if on_progress:
                        on_progress(1, "已解析单条图文作品原图。")
                    cookie_header = self._cookie_header_for_urls(
                        session,
                        target.url,
                        "https://www.douyin.com/",
                    )
                    result = ScanResult(
                        source=SourceKind.SINGLE_LINK,
                        author=selected.author,
                        reported_count=1,
                        unique_count=1,
                        content_counts={ContentKind.IMAGE: 1},
                        enumeration_complete=True,
                        items=(selected,),
                    )
                    media = self._finalize_media(
                        {selected.work_id: state.media[selected.work_id]},
                        cookie_header=cookie_header,
                    )
                    return DouyinScanBundle(result, media, cookie_header=cookie_header)

                if not rsc_attempted and time.monotonic() >= rsc_ready_at:
                    rsc_attempted = True
                    rsc_bundle = self._single_note_rsc_bundle(
                        session,
                        target,
                        expected_work_id,
                    )
                    if rsc_bundle is not None:
                        if on_progress:
                            on_progress(1, "已从图文页面数据解析作品原图。")
                        return rsc_bundle

                if idle_rounds < self.max_idle_rounds:
                    continue
                access_gate = self._access_gate_reason(session.client)
                if access_gate and self.interactive_login and not login_attempted:
                    login_attempted = True
                    if on_progress:
                        on_progress(0, "抖音要求登录，请在临时浏览器中完成官方扫码登录。")
                    login_error = self._wait_for_interactive_login(
                        session.client,
                        target.url,
                        cancel_event=cancel_event,
                    )
                    if login_error:
                        raise RuntimeError(login_error)
                    drain_events = getattr(session.client, "drain_events", None)
                    if callable(drain_events):
                        drain_events()
                    state = _EnumerationState(SourceKind.SINGLE_LINK)
                    session.client.command(
                        "Page.reload",
                        {"ignoreCache": True},
                        timeout=15,
                    )
                    deadline = time.monotonic() + self.scan_timeout
                    idle_rounds = 0
                    continue
                if access_gate:
                    raise RuntimeError(access_gate)
                idle_rounds = 0

            rsc_bundle = self._single_note_rsc_bundle(
                session,
                target,
                expected_work_id,
            )
            if rsc_bundle is not None:
                if on_progress:
                    on_progress(1, "已从图文页面数据解析作品原图。")
                return rsc_bundle

        raise RuntimeError("没有从作品详情响应中解析到图文原图。")

    def _single_note_rsc_bundle(
        self,
        session: _SessionLike,
        target: DouyinTarget,
        expected_work_id: str,
    ) -> DouyinScanBundle | None:
        """Read the current note's first-party RSC data when no detail XHR fires."""

        try:
            evaluated = session.client.command(
                "Runtime.evaluate",
                {
                    "expression": _RSC_NOTE_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=10,
            )
            chunks = self._runtime_value(evaluated)
        except Exception:
            return None
        detail = self._rsc_note_detail(chunks, expected_work_id)
        if detail is None:
            return None
        parsed = self._parse_aweme(self._normalize_rsc_note(detail))
        if parsed is None:
            return None
        item, candidates = parsed
        if item.content_type is not ContentKind.IMAGE or not candidates:
            return None

        cookie_header = self._cookie_header_for_urls(
            session,
            target.url,
            "https://www.douyin.com/",
        )
        result = ScanResult(
            source=SourceKind.SINGLE_LINK,
            author=item.author,
            reported_count=1,
            unique_count=1,
            content_counts={ContentKind.IMAGE: 1},
            enumeration_complete=True,
            items=(item,),
        )
        media = self._finalize_media(
            {item.work_id: list(candidates)},
            cookie_header=cookie_header,
        )
        return DouyinScanBundle(result, media, cookie_header=cookie_header)

    @classmethod
    def _rsc_note_detail(
        cls,
        chunks: Any,
        expected_work_id: str,
    ) -> Mapping[str, Any] | None:
        if not isinstance(chunks, list):
            return None
        decoder = json.JSONDecoder()
        for entry in chunks:
            if (
                not isinstance(entry, list)
                or len(entry) < 2
                or not isinstance(entry[1], str)
            ):
                continue
            raw = entry[1]
            if '"aweme"' not in raw or '"images"' not in raw:
                continue
            for match in re.finditer(r"\{", raw):
                try:
                    value, _end = decoder.raw_decode(raw, match.start())
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                detail = cls._find_rsc_note_detail(value, expected_work_id)
                if detail is not None:
                    return detail
        return None

    @staticmethod
    def _find_rsc_note_detail(
        value: Any,
        expected_work_id: str,
    ) -> Mapping[str, Any] | None:
        stack: list[Any] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, Mapping):
                aweme = current.get("aweme")
                detail = aweme.get("detail") if isinstance(aweme, Mapping) else None
                if isinstance(detail, Mapping):
                    work_id = str(
                        detail.get("awemeId")
                        or detail.get("aweme_id")
                        or ""
                    ).strip()
                    images = detail.get("images")
                    if (
                        work_id == expected_work_id
                        and isinstance(images, list)
                        and images
                    ):
                        return detail
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return None

    @staticmethod
    def _normalize_rsc_note(detail: Mapping[str, Any]) -> dict[str, Any]:
        author_info = detail.get("authorInfo")
        author = (
            str(author_info.get("nickname") or "").strip()
            if isinstance(author_info, Mapping)
            else ""
        )
        images: list[dict[str, Any]] = []
        raw_images = detail.get("images")
        if isinstance(raw_images, list):
            for raw_image in raw_images:
                if not isinstance(raw_image, Mapping):
                    continue
                download_urls = raw_image.get("downloadUrlList")
                display_urls = raw_image.get("urlList")
                images.append(
                    {
                        "download_url_list": (
                            list(download_urls)
                            if isinstance(download_urls, list)
                            else []
                        ),
                        "url_list": (
                            list(display_urls)
                            if isinstance(display_urls, list)
                            else []
                        ),
                        "width": raw_image.get("width"),
                        "height": raw_image.get("height"),
                    }
                )
        return {
            "aweme_id": detail.get("awemeId") or detail.get("aweme_id"),
            "desc": detail.get("desc") or detail.get("caption"),
            "create_time": detail.get("createTime") or detail.get("create_time"),
            "author": {"nickname": author},
            "images": images,
        }

    def _scan_playlet_http(
        self,
        target: DouyinTarget,
        *,
        cancel_event: Any = None,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> DouyinScanBundle:
        """Enumerate one public Playlet without changing the existing CDP paths."""

        state = _EnumerationState(SourceKind.DOUYIN_COLLECTION)
        session = self.playlet_session_factory()
        restricted_series = False
        try:
            detail, error = self._playlet_json_request(
                session,
                _PLAYLET_DETAIL_PATH,
                {
                    "playlet_id": target.playlet_id,
                    "aid": 1128,
                },
                target.url,
                cancel_event=cancel_event,
            )
            if error:
                state.incomplete_reason = f"系列详情读取失败：{error}"
            elif detail is None:
                state.incomplete_reason = "系列详情响应为空。"
            else:
                state.saw_api_response = True
                detail_info = self._playlet_info(detail)
                if detail_info is None:
                    state.incomplete_reason = "系列详情没有返回有效的 playlet_info。"
                elif str(detail_info.get("playlet_id") or "").strip() not in {
                    "",
                    target.playlet_id,
                }:
                    state.incomplete_reason = "系列详情标识与目标链接不一致。"
                else:
                    self._update_metadata(detail, state)
                    restricted_series = self._playlet_is_restricted(detail_info)
                    if restricted_series:
                        state.incomplete_reason = (
                            "该系列包含付费或受限剧集；不会绕过平台权限。"
                        )

            cursor = "0"
            requested_cursors = {cursor}
            if not state.incomplete_reason:
                for _page_index in range(self.max_scrolls):
                    payload, error = self._playlet_json_request(
                        session,
                        _PLAYLET_ITEMS_PATH,
                        {
                            "playlet_id": target.playlet_id,
                            "count": _PLAYLET_PAGE_SIZE,
                            "cursor": cursor,
                            "aid": 1128,
                        },
                        target.url,
                        cancel_event=cancel_event,
                    )
                    if error:
                        state.complete = False
                        state.incomplete_reason = f"系列分页读取失败：{error}"
                        break
                    if payload is None:
                        state.complete = False
                        state.incomplete_reason = "系列分页响应为空。"
                        break
                    state.saw_endpoint_response = True
                    state.saw_api_response = True
                    if self._payload_indicates_captcha(payload):
                        state.complete = False
                        state.incomplete_reason = "系列接口返回验证码或安全验证状态。"
                        break
                    if self._payload_has_error_status(payload):
                        state.complete = False
                        state.incomplete_reason = "系列接口返回非成功状态。"
                        break

                    self._consume_payload(payload, "playlet_item", state)
                    self._resolve_playlet_page_media(
                        session,
                        payload,
                        state,
                        target.url,
                        cancel_event=cancel_event,
                    )
                    if on_progress:
                        on_progress(
                            len(state.items),
                            f"已发现 {len(state.items)} 个唯一作品。",
                        )
                    if state.incomplete_reason:
                        break

                    has_more = self._has_more(payload)
                    if has_more is False:
                        break
                    if has_more is not True:
                        state.complete = False
                        state.incomplete_reason = "系列分页没有返回明确的结束标志。"
                        break
                    next_cursor = self._cursor(payload)
                    if next_cursor is None:
                        state.complete = False
                        state.incomplete_reason = "系列分页游标缺失。"
                        break
                    if next_cursor == cursor or next_cursor in requested_cursors:
                        state.complete = False
                        state.incomplete_reason = "系列分页游标未前进或出现循环。"
                        break
                    requested_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    state.complete = False
                    state.incomplete_reason = "系列分页超过安全扫描上限。"

            if restricted_series:
                state.complete = False
                state.incomplete_reason = "该系列包含付费或受限剧集；不会绕过平台权限。"
            if (
                state.complete
                and state.reported_count is not None
                and len(state.items) != state.reported_count
            ):
                state.complete = False
                state.incomplete_reason = (
                    f"页面报告 {state.reported_count} 集，但实际发现 "
                    f"{len(state.items)} 集，无法确认列表完整。"
                )
            if not state.complete and not state.incomplete_reason:
                state.incomplete_reason = "未能确认系列列表已完整结束。"

            if state.author:
                for work_id, item in tuple(state.items.items()):
                    if not item.author:
                        state.items[work_id] = replace(item, author=state.author)

            items = tuple(state.items.values())
            counts = Counter(item.content_type.value for item in items)
            result = ScanResult(
                source=SourceKind.DOUYIN_COLLECTION,
                author=state.author,
                reported_count=state.reported_count,
                unique_count=len(items),
                content_counts=dict(counts),
                enumeration_complete=state.complete,
                items=items,
                incomplete_reason="" if state.complete else state.incomplete_reason,
            )
            media = self._finalize_media(state.media, cookie_header="")
            return DouyinScanBundle(result, media, cookie_header="")
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    def _playlet_json_request(
        self,
        session: Any,
        path: str,
        params: Mapping[str, Any],
        referer: str,
        *,
        cancel_event: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("任务已取消。")
        response: Any = None
        try:
            response = session.get(
                f"{_PLAYLET_WEB_ORIGIN}{path}",
                params=dict(params),
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": referer,
                },
                allow_redirects=False,
                timeout=(8, 20),
            )
            try:
                status = int(getattr(response, "status_code", 0) or 0)
            except (TypeError, ValueError):
                status = 0
            if status >= 400 or status == 0:
                return None, f"接口返回 HTTP {status or '未知'}"
            payload = response.json()
            if not isinstance(payload, dict):
                return None, "接口 JSON 不是对象"
            if self._payload_indicates_captcha(payload):
                return None, "接口要求验证码或安全验证"
            if self._payload_has_error_status(payload):
                return None, "接口返回非成功状态"
            return payload, ""
        except RuntimeError:
            raise
        except Exception:
            return None, "网络请求或 JSON 解析失败"
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _playlet_info(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        for container in (payload, payload.get("data")):
            if not isinstance(container, Mapping):
                continue
            info = container.get("playlet_info")
            if isinstance(info, Mapping):
                return info
        return None

    @staticmethod
    def _playlet_is_restricted(info: Mapping[str, Any]) -> bool:
        value = info.get("is_charge_series")
        normalized = value.strip().lower() if isinstance(value, str) else value
        if normalized in (True, 1, "1", "true"):
            return True
        charge_episodes = info.get("charge_episodes")
        return charge_episodes not in (None, False, 0, "", (), [], {})

    def _resolve_playlet_page_media(
        self,
        session: Any,
        payload: Mapping[str, Any],
        state: _EnumerationState,
        referer: str,
        *,
        cancel_event: Any = None,
    ) -> None:
        for aweme in self._aweme_list(payload):
            parsed = self._parse_aweme(aweme)
            if parsed is None:
                continue
            item, parsed_candidates = parsed
            if parsed_candidates or state.media.get(item.work_id):
                continue
            video = aweme.get("video")
            if item.content_type is not ContentKind.VIDEO or not isinstance(
                video,
                Mapping,
            ):
                continue
            candidate = self._resolve_playlet_video_candidate(
                session,
                video,
                referer,
                cancel_event=cancel_event,
            )
            if candidate is not None:
                state.media.setdefault(item.work_id, []).append(candidate)

    def _resolve_playlet_video_candidate(
        self,
        session: Any,
        video: Mapping[str, Any],
        referer: str,
        *,
        cancel_event: Any = None,
    ) -> _MediaCandidate | None:
        video_id = str(video.get("vid") or "").strip()
        if not video_id or len(video_id) > 256:
            return None
        width = self._integer(video.get("width"))
        height = self._integer(video.get("height"))
        for endpoint, ratio, watermarked in _PLAYLET_MEDIA_ATTEMPTS:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消。")
            response: Any = None
            try:
                response = session.head(
                    f"{_PLAYLET_MEDIA_ORIGIN}/aweme/v1/{endpoint}/",
                    params={
                        "line": 0,
                        "ratio": ratio,
                        "video_id": video_id,
                    },
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Accept": "*/*",
                        "Referer": referer,
                    },
                    allow_redirects=True,
                    timeout=(8, 25),
                )
                try:
                    status = int(getattr(response, "status_code", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                headers = getattr(response, "headers", {})
                content_type = str(
                    headers.get("Content-Type", headers.get("content-type", ""))
                    if isinstance(headers, Mapping)
                    else ""
                ).lower()
                final_url = str(getattr(response, "url", "") or "").strip()
                try:
                    final_parts = urlsplit(final_url)
                except Exception:
                    final_parts = None
                if (
                    status in {200, 206}
                    and content_type.startswith("video/")
                    and final_parts is not None
                    and final_parts.scheme in {"http", "https"}
                    and bool(final_parts.hostname)
                ):
                    quality = ratio + ("-watermarked" if watermarked else "")
                    return _MediaCandidate(
                        quality=quality,
                        url=final_url,
                        width=width,
                        height=height,
                        codec="h264",
                        container="mp4",
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        return None

    def _scan_session(
        self,
        session: _SessionLike,
        target: DouyinTarget,
        *,
        cancel_event: Any = None,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> DouyinScanBundle:
        client = session.client
        state = _EnumerationState(target.source)
        deadline = time.monotonic() + self.scan_timeout
        idle_rounds = 0
        scrolls = 0
        last_scroll_height: float | None = None
        login_attempted = False
        awaiting_reload = False
        next_maintenance = time.monotonic() + self.event_timeout

        while True:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if awaiting_reload:
                    state.incomplete_reason = (
                        "登录成功，但等待作者主页刷新超时，枚举可能不完整。"
                    )
                elif self._has_pending_pagination(state):
                    state.incomplete_reason = "部分分页接口响应正文读取超时。"
                else:
                    state.incomplete_reason = (
                        "扫描超时，未收到明确的分页结束标志。"
                    )
                break

            event = client.next_event(timeout=min(self.event_timeout, remaining))
            if awaiting_reload:
                if event is not None and self._event_indicates_captcha(event):
                    state.incomplete_reason = "页面触发验证码或安全验证。"
                    break
                if event is not None and self._event_is_main_douyin_navigation(event):
                    awaiting_reload = False
                    now = time.monotonic()
                    deadline = now + self.scan_timeout
                    idle_rounds = 0
                    scrolls = 0
                    last_scroll_height = None
                    next_maintenance = now + self.event_timeout
                elif self.event_timeout == 0:
                    # Offline fakes use a zero timeout.  Avoid a hot loop while
                    # waiting for the post-login top-frame navigation marker.
                    time.sleep(0.001)
                # Events queued by the anonymous document are deliberately
                # ignored until the reload's new top-level document exists.
                continue

            relevant_activity = False
            if event is not None:
                if self._event_indicates_captcha(event):
                    state.incomplete_reason = "页面触发验证码或安全验证。"
                    break
                previous_count = len(state.items)
                relevant_activity = self._handle_event(client, event, state)
                if on_progress and len(state.items) != previous_count:
                    on_progress(
                        len(state.items),
                        f"已发现 {len(state.items)} 个唯一作品。",
                    )
                if state.incomplete_reason or (
                    state.complete and not self._has_pending_pagination(state)
                ):
                    break
            now = time.monotonic()
            if relevant_activity:
                # ``scan_timeout`` is an inactivity timeout.  Large accounts
                # may legitimately need much longer in total while pages keep
                # arriving, so every authoritative API advance renews it.
                deadline = now + self.scan_timeout
                idle_rounds = 0
                next_maintenance = now + self.event_timeout
                continue

            # Login/status polling can keep the CDP queue permanently non-empty.
            # Maintenance and the idle deadline must advance on wall-clock time,
            # not only when ``next_event`` happens to return None.
            maintenance_due = (
                event is None
                or self.event_timeout == 0
                or now >= next_maintenance
            )
            if not maintenance_due:
                continue
            idle_rounds += 1
            next_maintenance = now + self.event_timeout
            if self._dom_has_captcha(client):
                state.incomplete_reason = "页面触发验证码或安全验证。"
                break
            if scrolls < self.max_scrolls:
                observation = self._scroll_observation(client)
                scrolls += 1
                height_grew = False
                if observation.height is not None:
                    height_grew = (
                        last_scroll_height is not None
                        and observation.height > last_scroll_height
                    )
                    if (
                        last_scroll_height is None
                        or observation.height > last_scroll_height
                    ):
                        last_scroll_height = observation.height
                if observation.moved or height_grew:
                    now = time.monotonic()
                    deadline = now + self.scan_timeout
                    idle_rounds = 0
                    next_maintenance = now + self.event_timeout
            access_gate = self._access_gate_reason(client)
            if access_gate:
                if self.interactive_login and not login_attempted:
                    login_attempted = True
                    if on_progress:
                        on_progress(
                            len(state.items),
                            "抖音要求登录，请在临时浏览器中完成官方扫码登录。",
                        )
                    login_error = self._wait_for_interactive_login(
                        client,
                        target.url,
                        cancel_event=cancel_event,
                    )
                    if login_error:
                        state.complete = False
                        state.incomplete_reason = login_error
                        break
                    try:
                        drain_events = getattr(client, "drain_events", None)
                        if callable(drain_events):
                            drain_events()
                    except Exception:
                        pass

                    # Anonymous results are only a preview.  Once login has
                    # succeeded, the reloaded page becomes the sole authority;
                    # retaining old cursors, pending requests, or gate state can
                    # cause reload storms and false pagination completion.
                    state = _EnumerationState(target.source)
                    awaiting_reload = True
                    idle_rounds = 0
                    scrolls = 0
                    last_scroll_height = None
                    now = time.monotonic()
                    deadline = now + self.scan_timeout
                    next_maintenance = now + self.event_timeout
                    try:
                        client.command(
                            "Page.reload",
                            {"ignoreCache": True},
                            timeout=15,
                        )
                    except Exception:
                        state.incomplete_reason = (
                            "登录成功，但刷新作者主页失败，枚举可能不完整。"
                        )
                        break
                    if on_progress:
                        on_progress(0, "登录成功，正在重新扫描作者主页。")
                    continue
                if self.interactive_login and login_attempted:
                    state.complete = False
                    state.incomplete_reason = (
                        "登录后页面仍要求登录，未重复刷新，枚举可能不完整。"
                    )
                    break
                state.complete = False
                state.incomplete_reason = access_gate
                break
            if idle_rounds >= self.max_idle_rounds:
                if state.expecting_more or self._has_pending_pagination(state):
                    # An explicit has_more=true (or an in-flight page) is more
                    # authoritative than a short DOM-idle streak.  Keep hitting
                    # the bottom and let the progress deadline decide instead.
                    if self.event_timeout == 0:
                        time.sleep(0.001)
                    continue
                state.incomplete_reason = "等待分页接口响应超时，枚举可能不完整。"
                break

        pending_pagination = self._has_pending_pagination(state)
        if state.pending_responses:
            state.body_failures += len(state.pending_responses)
            state.pending_responses.clear()
            if state.complete and pending_pagination:
                state.complete = False
                state.incomplete_reason = "部分分页接口响应正文无法读取。"

        if not awaiting_reload:
            self._add_dom_metadata(client, state)
        dom_added = 0
        if not state.complete and not awaiting_reload:
            dom_added = self._add_dom_discoveries(client, state)
            if on_progress and dom_added:
                on_progress(
                    len(state.items),
                    f"已发现 {len(state.items)} 个唯一作品。",
                )
            if not state.incomplete_reason:
                state.incomplete_reason = "未收到明确的分页结束标志。"
            if (
                dom_added
                and not state.saw_endpoint_response
                and (
                    not state.incomplete_reason
                    or state.incomplete_reason
                    in {
                        "未收到明确的分页结束标志。",
                        "扫描超时，未收到明确的分页结束标志。",
                        "等待分页接口响应超时，枚举可能不完整。",
                    }
                )
            ):
                state.incomplete_reason = (
                    "仅从页面 DOM 发现作品，未收到可证明分页结束的接口响应。"
                )
            elif dom_added and "DOM" not in state.incomplete_reason:
                state.incomplete_reason += "；页面 DOM 结果不能证明枚举完整。"
            elif state.body_failures and not state.items:
                state.incomplete_reason += "；部分接口响应正文无法读取。"

        cookie_header = ""
        try:
            cookie_header = self._cookie_header_for_urls(
                session,
                target.url,
                "https://www.douyin.com/",
            )
        except Exception:
            cookie_header = ""

        items = tuple(state.items.values())
        counts = Counter(item.content_type.value for item in items)
        result = ScanResult(
            source=target.source,
            author=state.author,
            reported_count=state.reported_count,
            unique_count=len(items),
            content_counts=dict(counts),
            enumeration_complete=state.complete,
            items=items,
            incomplete_reason="" if state.complete else state.incomplete_reason,
        )
        media = self._finalize_media(state.media, cookie_header=cookie_header)
        return DouyinScanBundle(result, media, cookie_header=cookie_header)

    def _handle_event(
        self,
        client: Any,
        event: Mapping[str, Any],
        state: _EnumerationState,
    ) -> bool:
        """Consume one CDP event and report whether it advances our scan."""

        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, Mapping):
            return False

        if method == "Network.loadingFinished":
            request_id = str(params.get("requestId") or "")
            endpoint = state.pending_responses.pop(request_id, None)
            if endpoint and not self._consume_response_body(
                client, request_id, endpoint, state
            ):
                state.body_failures += 1
                state.complete = False
                state.incomplete_reason = (
                    "分页接口响应正文为空或无法解析，枚举可能不完整。"
                )
            return endpoint is not None
        if method == "Network.loadingFailed":
            request_id = str(params.get("requestId") or "")
            endpoint = state.pending_responses.pop(request_id, None)
            if endpoint:
                state.body_failures += 1
                if endpoint in {"post", "mix_aweme"}:
                    state.complete = False
                    state.incomplete_reason = "分页接口加载失败，枚举可能不完整。"
            return endpoint is not None
        if method != "Network.responseReceived":
            return False

        response = params.get("response")
        if not isinstance(response, Mapping):
            return False
        endpoint = self._endpoint_kind(str(response.get("url") or ""))
        if endpoint is None:
            return False
        if not self._endpoint_relevant(endpoint, state.source):
            return False
        state.saw_endpoint_response = True
        try:
            http_status = int(response.get("status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if http_status >= 400:
            state.body_failures += 1
            state.complete = False
            state.incomplete_reason = (
                f"分页接口返回 HTTP {http_status}，枚举可能不完整。"
            )
            return True
        request_id = str(params.get("requestId") or "")
        if not request_id:
            state.body_failures += 1
            if endpoint in {"post", "mix_aweme"}:
                state.complete = False
                state.incomplete_reason = "分页接口缺少请求标识，枚举可能不完整。"
            return True
        state.pending_responses[request_id] = endpoint
        return True

    def _consume_response_body(
        self,
        client: Any,
        request_id: str,
        endpoint: str,
        state: _EnumerationState,
    ) -> bool:
        try:
            body_result = client.command(
                "Network.getResponseBody",
                {"requestId": request_id},
                timeout=10,
            )
            payload = self._decode_json_body(body_result)
        except Exception:
            return False
        state.saw_api_response = True
        if self._payload_indicates_captcha(payload):
            state.incomplete_reason = "接口返回验证码或安全验证状态。"
            return True
        if self._payload_has_error_status(payload):
            state.complete = False
            state.incomplete_reason = "接口返回非成功状态，枚举可能不完整。"
            return True
        self._consume_payload(payload, endpoint, state)
        return True

    @staticmethod
    def _endpoint_relevant(endpoint: str, source: SourceKind) -> bool:
        if source is SourceKind.SINGLE_LINK:
            return endpoint == "aweme_detail"
        if source is SourceKind.DOUYIN_PROFILE:
            return endpoint == "post"
        return endpoint in {"mix_detail", "mix_aweme"}

    @staticmethod
    def _has_pending_pagination(state: _EnumerationState) -> bool:
        return any(
            endpoint in {"post", "mix_aweme"}
            for endpoint in state.pending_responses.values()
        )

    @staticmethod
    def _endpoint_kind(url: str) -> str | None:
        try:
            path = urlsplit(url).path.lower()
        except Exception:
            return None
        if _AWEME_DETAIL_PATH in path:
            return "aweme_detail"
        if _POST_PATH in path:
            return "post"
        if _MIX_DETAIL_PATH in path:
            return "mix_detail"
        if _MIX_AWEME_PATH in path:
            return "mix_aweme"
        return None

    @staticmethod
    def _decode_json_body(result: Mapping[str, Any]) -> dict[str, Any]:
        raw_body = result.get("body")
        if not isinstance(raw_body, str):
            raise ValueError("响应正文为空。")
        if result.get("base64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("响应 JSON 不是对象。")
        return payload

    def _consume_payload(
        self,
        payload: Mapping[str, Any],
        endpoint: str,
        state: _EnumerationState,
    ) -> None:
        self._update_metadata(payload, state)
        if endpoint == "mix_detail":
            return

        for raw_aweme in self._aweme_list(payload):
            parsed = self._parse_aweme(raw_aweme)
            if parsed is None:
                continue
            item, candidates = parsed
            if item.work_id not in state.items:
                state.items[item.work_id] = item
            if not state.author and item.author:
                state.author = item.author
            if candidates:
                existing = state.media.setdefault(item.work_id, [])
                known_urls = {candidate.url for candidate in existing}
                existing.extend(
                    candidate
                    for candidate in candidates
                    if candidate.url not in known_urls
                )

        has_more = self._has_more(payload)
        if has_more is False:
            state.expecting_more = False
            state.complete = True
            return
        if has_more is not True:
            return

        state.expecting_more = True
        state.complete = False
        state.page_counts[endpoint] += 1
        cursor = self._cursor(payload)
        seen = state.cursors.setdefault(endpoint, set())
        if cursor is None:
            if state.page_counts[endpoint] > 1:
                state.incomplete_reason = "分页游标缺失或未前进，枚举已停止。"
            return
        if cursor in seen:
            state.incomplete_reason = "分页游标未前进或出现循环，枚举已停止。"
            return
        seen.add(cursor)

    @staticmethod
    def _aweme_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for container in (payload, payload.get("data")):
            if not isinstance(container, Mapping):
                continue
            detail = container.get("aweme_detail")
            if isinstance(detail, Mapping):
                return [detail]
            value = container.get("aweme_list")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _has_more(payload: Mapping[str, Any]) -> bool | None:
        for container in (payload, payload.get("data")):
            if not isinstance(container, Mapping) or "has_more" not in container:
                continue
            value = container.get("has_more")
            normalized = value.strip().lower() if isinstance(value, str) else value
            if normalized in (False, 0, "0", "false"):
                return False
            if normalized in (True, 1, "1", "true"):
                return True
        return None

    @staticmethod
    def _cursor(payload: Mapping[str, Any]) -> str | None:
        for container in (payload, payload.get("data")):
            if not isinstance(container, Mapping):
                continue
            for key in ("max_cursor", "cursor", "min_cursor"):
                if key not in container:
                    continue
                value = container.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        return None

    def _update_metadata(
        self,
        payload: Mapping[str, Any],
        state: _EnumerationState,
    ) -> None:
        data = payload.get("data")
        containers = [payload]
        if isinstance(data, Mapping):
            containers.append(data)

        for container in containers:
            user = container.get("user")
            if isinstance(user, Mapping):
                if not state.author:
                    state.author = str(user.get("nickname") or "").strip()
                self._set_reported_count(state, user.get("aweme_count"))

            mix = container.get("mix_info")
            if isinstance(mix, Mapping):
                author = mix.get("author")
                if isinstance(author, Mapping) and not state.author:
                    state.author = str(author.get("nickname") or "").strip()
                self._set_reported_count(state, mix.get("aweme_count"))
                statis = mix.get("statis")
                if isinstance(statis, Mapping):
                    for key in (
                        "updated_to_episode",
                        "current_episode",
                        "total",
                        "aweme_count",
                    ):
                        self._set_reported_count(state, statis.get(key))

            playlet = container.get("playlet_info")
            if isinstance(playlet, Mapping):
                author = playlet.get("author")
                if isinstance(author, Mapping) and not state.author:
                    state.author = str(author.get("nickname") or "").strip()
                self._set_reported_count(state, playlet.get("total_episode"))
                statis = playlet.get("statis")
                if isinstance(statis, Mapping):
                    for key in (
                        "total_updated_to_episode",
                        "updated_to_episode",
                        "current_episode",
                        "total",
                        "aweme_count",
                    ):
                        self._set_reported_count(state, statis.get(key))

            for key in ("total", "aweme_count"):
                self._set_reported_count(state, container.get(key))

    @staticmethod
    def _set_reported_count(state: _EnumerationState, raw_value: Any) -> None:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        if state.reported_count is None or value > state.reported_count:
            state.reported_count = value

    def _parse_aweme(
        self,
        aweme: Mapping[str, Any],
    ) -> tuple[WorkItem, tuple[_MediaCandidate, ...]] | None:
        work_id = str(aweme.get("aweme_id") or aweme.get("id") or "").strip()
        if not work_id:
            return None
        author_value = aweme.get("author")
        author = (
            str(author_value.get("nickname") or "").strip()
            if isinstance(author_value, Mapping)
            else ""
        )
        share_info = aweme.get("share_info")
        share_title = (
            str(share_info.get("share_title") or "").strip()
            if isinstance(share_info, Mapping)
            else ""
        )
        title = (
            str(aweme.get("desc") or aweme.get("preview_title") or "").strip()
            or share_title
            or f"抖音作品 {work_id}"
        )
        published_at: datetime | None = None
        try:
            timestamp = int(aweme.get("create_time"))
            if timestamp > 0:
                # Douyin displays publication dates in China Standard Time.
                # Keeping the platform-local offset here prevents filenames
                # near UTC midnight from being dated one day early.
                published_at = datetime.fromtimestamp(timestamp, tz=_DOUYIN_TIMEZONE)
        except (TypeError, ValueError, OSError, OverflowError):
            published_at = None

        image_entries = self._image_entries(aweme)
        if self._is_live_aweme(aweme):
            content_type = ContentKind.LIVE
            canonical_url = f"https://www.douyin.com/video/{work_id}"
            candidates = ()
        elif image_entries:
            content_type = ContentKind.IMAGE
            canonical_url = f"https://www.douyin.com/note/{work_id}"
            candidates = self._image_candidates(image_entries)
        elif isinstance(aweme.get("video"), Mapping):
            content_type = ContentKind.VIDEO
            canonical_url = f"https://www.douyin.com/video/{work_id}"
            candidates = self._video_candidates(aweme["video"])
        else:
            content_type = ContentKind.UNKNOWN
            canonical_url = f"https://www.douyin.com/video/{work_id}"
            candidates = ()

        item = WorkItem(
            platform=Platform.DOUYIN,
            work_id=work_id,
            content_type=content_type,
            title=title,
            author=author,
            published_at=published_at,
            canonical_url=canonical_url,
        )
        return item, tuple(candidates)

    @staticmethod
    def _is_live_aweme(aweme: Mapping[str, Any]) -> bool:
        live_value = aweme.get("is_live")
        if live_value in (True, 1, "1"):
            return True
        try:
            return int(aweme.get("aweme_type")) in {101, 105}
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _image_entries(aweme: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        post_info = aweme.get("image_post_info")
        if isinstance(post_info, Mapping):
            images = post_info.get("images")
            if isinstance(images, list):
                values = [item for item in images if isinstance(item, Mapping)]
                if values:
                    return values
        images = aweme.get("images")
        if isinstance(images, list):
            return [item for item in images if isinstance(item, Mapping)]
        return []

    def _image_candidates(
        self,
        images: list[Mapping[str, Any]],
    ) -> tuple[_MediaCandidate, ...]:
        candidates: list[_MediaCandidate] = []
        seen: set[str] = set()
        for index, image in enumerate(images, start=1):
            url, width, height = self._best_image_url(image)
            if not url or url in seen:
                continue
            seen.add(url)
            candidates.append(
                _MediaCandidate(
                    quality=f"original-{index:02d}",
                    url=url,
                    width=width,
                    height=height,
                    codec="image",
                    container=self._container_from_url(url, default="webp"),
                )
            )
        return tuple(candidates)

    @classmethod
    def _best_image_url(
        cls,
        image: Mapping[str, Any],
    ) -> tuple[str, int | None, int | None]:
        sources: list[Mapping[str, Any]] = []
        for key in ("origin_image", "display_image"):
            nested = image.get(key)
            if isinstance(nested, Mapping):
                sources.append(nested)
        sources.append(image)
        owner = image.get("owner_watermark_image")
        if isinstance(owner, Mapping):
            sources.append(owner)
        for source in sources:
            url = cls._first_url(source)
            if url:
                return url, cls._integer(source.get("width")), cls._integer(
                    source.get("height")
                )
        return "", None, None

    def _video_candidates(
        self,
        video: Mapping[str, Any],
    ) -> tuple[_MediaCandidate, ...]:
        candidates: list[_MediaCandidate] = []
        bit_rates = video.get("bit_rate")
        if isinstance(bit_rates, list):
            for entry in bit_rates:
                if not isinstance(entry, Mapping):
                    continue
                address = entry.get("play_addr")
                if not isinstance(address, Mapping):
                    continue
                candidate = self._video_candidate(entry, address, video)
                if candidate:
                    candidates.append(candidate)

        for key, forced_codec in (
            ("play_addr_h264", "h264"),
            ("play_addr", ""),
            ("download_addr", ""),
        ):
            address = video.get(key)
            if not isinstance(address, Mapping):
                continue
            candidate = self._video_candidate(
                {"gear_name": key, "codec_type": forced_codec},
                address,
                video,
            )
            if candidate:
                candidates.append(candidate)

        unique: dict[str, _MediaCandidate] = {}
        for candidate in candidates:
            existing = unique.get(candidate.url)
            if existing is None or candidate.sort_key > existing.sort_key:
                unique[candidate.url] = candidate
        return tuple(sorted(unique.values(), key=lambda item: item.sort_key, reverse=True))

    @classmethod
    def _video_candidate(
        cls,
        entry: Mapping[str, Any],
        address: Mapping[str, Any],
        video: Mapping[str, Any],
    ) -> _MediaCandidate | None:
        url = cls._first_url(address)
        if not url:
            return None
        width = cls._integer(address.get("width")) or cls._integer(entry.get("width"))
        width = width or cls._integer(video.get("width"))
        height = cls._integer(address.get("height")) or cls._integer(
            entry.get("height")
        )
        height = height or cls._integer(video.get("height"))
        codec = cls._codec(entry)
        quality = str(
            entry.get("gear_name")
            or entry.get("quality_type")
            or (f"{height}p" if height else "best")
        ).strip()
        bitrate = cls._integer(entry.get("bit_rate")) or cls._integer(
            entry.get("bitrate")
        )
        return _MediaCandidate(
            quality=quality or "best",
            url=url,
            width=width,
            height=height,
            codec=codec,
            container=cls._container_from_url(url, default="mp4"),
            bitrate=bitrate or 0,
        )

    @staticmethod
    def _codec(entry: Mapping[str, Any]) -> str:
        raw = " ".join(
            str(entry.get(key) or "")
            for key in ("codec_type", "codec", "format", "gear_name")
        ).lower()
        if entry.get("is_h265") in (True, 1, "1") or any(
            token in raw for token in ("h265", "hevc", "bytevc1")
        ):
            return "h265"
        if any(token in raw for token in ("h264", "avc")):
            return "h264"
        if entry.get("is_h265") in (False, 0, "0"):
            return "h264"
        return "h264"

    @staticmethod
    def _first_url(value: Mapping[str, Any]) -> str:
        for key in ("download_url_list", "url_list"):
            urls = value.get(key)
            if isinstance(urls, list):
                for raw_url in urls:
                    url = str(raw_url or "").strip()
                    if url.startswith(("http://", "https://")):
                        return url
        for key in ("url", "uri"):
            url = str(value.get(key) or "").strip()
            if url.startswith(("http://", "https://")):
                return url
        return ""

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _container_from_url(url: str, *, default: str) -> str:
        try:
            suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
        except Exception:
            suffix = ""
        if suffix in {"mp4", "m4v", "webm", "jpg", "jpeg", "png", "webp", "avif"}:
            return suffix
        return default

    @staticmethod
    def _finalize_media(
        media: Mapping[str, list[_MediaCandidate]],
        *,
        cookie_header: str,
    ) -> dict[str, tuple[MediaDescriptor, ...]]:
        result: dict[str, tuple[MediaDescriptor, ...]] = {}
        for work_id, candidates in media.items():
            ordered_candidates = (
                list(candidates)
                if all(candidate.codec == "image" for candidate in candidates)
                else sorted(candidates, key=lambda item: item.sort_key, reverse=True)
            )
            descriptors = tuple(
                MediaDescriptor(
                    quality=candidate.quality,
                    media_url=candidate.url,
                    width=candidate.width,
                    height=candidate.height,
                    codec=candidate.codec,
                    container=candidate.container,
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Referer": "https://www.douyin.com/",
                    },
                    cookie=cookie_header,
                )
                for candidate in ordered_candidates
            )
            if descriptors:
                result[work_id] = descriptors
        return result

    @staticmethod
    def _runtime_value(result: Mapping[str, Any]) -> Any:
        runtime_result = result.get("result")
        if not isinstance(runtime_result, Mapping):
            return None
        return runtime_result.get("value")

    def _dom_has_captcha(self, client: Any) -> bool:
        try:
            result = client.command(
                "Runtime.evaluate",
                {
                    "expression": _CAPTCHA_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
            return bool(self._runtime_value(result))
        except Exception:
            return False

    @staticmethod
    def _scroll(client: Any) -> bool:
        return DouyinEnumerator._scroll_observation(client).moved

    @staticmethod
    def _scroll_observation(client: Any) -> _ScrollObservation:
        try:
            result = client.command(
                "Runtime.evaluate",
                {
                    "expression": _SCROLL_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
            value = DouyinEnumerator._runtime_value(result)
            if not isinstance(value, Mapping):
                return _ScrollObservation()

            def number(raw: Any) -> float | None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            before = number(value.get("before"))
            after = number(value.get("after"))
            height = number(value.get("height"))
            moved = (
                after > before
                if before is not None and after is not None
                else bool(value.get("moved"))
            )
            return _ScrollObservation(moved=moved, height=height)
        except Exception:
            return _ScrollObservation()

    def _access_gate_reason(self, client: Any) -> str:
        try:
            result = client.command(
                "Runtime.evaluate",
                {
                    "expression": _ACCESS_GATE_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
        except Exception:
            return ""
        value = str(self._runtime_value(result) or "")
        if value == "login_required":
            return (
                "抖音匿名页面仅提供首批作品，登录后才能继续枚举；"
                "软件未绕过登录限制。"
            )
        if value == "open_app_required":
            return "抖音分享页要求在抖音客户端中继续查看，枚举可能不完整。"
        return ""

    @staticmethod
    def _cookie_header_for_urls(session: _SessionLike, *urls: str) -> str:
        cookies: dict[str, str] = {}
        for url in dict.fromkeys(str(value or "") for value in urls):
            if not url:
                continue
            try:
                header = str(session.cookie_header(url) or "")
            except Exception:
                continue
            for raw_cookie in header.split(";"):
                name, separator, value = raw_cookie.strip().partition("=")
                if separator and name:
                    # Updating a duplicate keeps its original stable ordering
                    # while allowing the final www.douyin.com scope to win.
                    cookies[name] = value
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    def _wait_for_interactive_login(
        self,
        client: Any,
        page_url: str,
        *,
        cancel_event: Any = None,
    ) -> str:
        try:
            client.command(
                "Runtime.evaluate",
                {
                    "expression": _OPEN_LOGIN_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
        except Exception:
            pass

        deadline = time.monotonic() + self.login_timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消。")
            if bool(getattr(client, "closed", False)):
                return "临时登录浏览器已关闭，未能继续枚举。"
            if self._has_login_cookie(client, page_url):
                return ""
            time.sleep(0.5)
        return "等待抖音扫码登录超时，未能继续枚举。"

    @staticmethod
    def _has_login_cookie(client: Any, page_url: str) -> bool:
        try:
            result = client.command(
                "Network.getCookies",
                {
                    "urls": [
                        str(page_url),
                        "https://www.douyin.com/",
                    ]
                },
                timeout=5,
            )
        except Exception:
            return False
        cookies = result.get("cookies")
        if not isinstance(cookies, list):
            return False
        login_names = {
            "sessionid",
            "sessionid_ss",
            "sid_guard",
            "sid_tt",
            "uid_tt",
            "uid_tt_ss",
        }
        return any(
            isinstance(cookie, Mapping)
            and str(cookie.get("name") or "") in login_names
            and bool(cookie.get("value"))
            for cookie in cookies
        )

    def _add_dom_discoveries(self, client: Any, state: _EnumerationState) -> int:
        try:
            result = client.command(
                "Runtime.evaluate",
                {
                    "expression": _DOM_DISCOVERY_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
        except Exception:
            return 0
        values = self._runtime_value(result)
        if not isinstance(values, list):
            return 0
        added = 0
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            href = str(raw.get("href") or "")
            match = re.search(r"/(video|note)/(\d+)", href)
            if not match:
                continue
            work_id = match.group(2)
            if work_id in state.items:
                continue
            kind = ContentKind.IMAGE if match.group(1) == "note" else ContentKind.VIDEO
            canonical = f"https://www.douyin.com/{match.group(1)}/{work_id}"
            state.items[work_id] = WorkItem(
                platform=Platform.DOUYIN,
                work_id=work_id,
                content_type=kind,
                title=str(raw.get("title") or "").strip() or f"抖音作品 {work_id}",
                author=state.author,
                published_at=None,
                canonical_url=canonical,
            )
            added += 1
        return added

    def _add_dom_metadata(self, client: Any, state: _EnumerationState) -> None:
        """Read stable public-page metadata without affecting completeness."""

        try:
            result = client.command(
                "Runtime.evaluate",
                {
                    "expression": _DOM_METADATA_EXPRESSION,
                    "returnByValue": True,
                },
                timeout=5,
            )
        except Exception:
            return
        value = self._runtime_value(result)
        if not isinstance(value, Mapping):
            return
        author = str(value.get("author") or "").strip()
        if author and not state.author:
            state.author = author
        effective_author = state.author or author
        if effective_author:
            for work_id, item in tuple(state.items.items()):
                if not item.author:
                    state.items[work_id] = replace(item, author=effective_author)
        reported_count = self._reported_count_from_text(
            str(value.get("reportedCountText") or "")
        )
        if reported_count is not None:
            self._set_reported_count(state, reported_count)

    @staticmethod
    def _reported_count_from_text(value: str) -> int | None:
        text = str(value or "").strip().lower().replace(",", "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿|w|k)?", text)
        if not match:
            return None
        try:
            number = float(match.group(1))
        except ValueError:
            return None
        multiplier = {
            "万": 10_000,
            "w": 10_000,
            "亿": 100_000_000,
            "k": 1_000,
        }.get(match.group(2) or "", 1)
        result = int(round(number * multiplier))
        return result if result >= 0 else None

    @staticmethod
    def _event_is_main_douyin_navigation(event: Mapping[str, Any]) -> bool:
        if event.get("method") != "Page.frameNavigated":
            return False
        params = event.get("params")
        if not isinstance(params, Mapping):
            return False
        frame = params.get("frame")
        if not isinstance(frame, Mapping) or frame.get("parentId"):
            return False
        try:
            parts = urlsplit(str(frame.get("url") or ""))
        except Exception:
            return False
        return parts.scheme in {"http", "https"} and _is_douyin_host(
            parts.hostname or ""
        )

    @staticmethod
    def _event_indicates_captcha(event: Mapping[str, Any]) -> bool:
        # Douyin normally loads the rc-verifycenter/captcha SDK even when no
        # challenge is active.  Network script/XHR URLs therefore cannot be
        # treated as proof of a captcha.  A top-level frame navigation to the
        # verification center is conclusive; visible iframe challenges are
        # detected separately by ``_dom_has_captcha``.
        if event.get("method") != "Page.frameNavigated":
            return False
        params = event.get("params")
        if not isinstance(params, Mapping):
            return False
        frame = params.get("frame")
        if not isinstance(frame, Mapping) or frame.get("parentId"):
            return False
        try:
            parts = urlsplit(str(frame.get("url") or ""))
            location = f"{parts.hostname or ''}{parts.path}".lower()
        except Exception:
            return False
        return any(token in location for token in _CAPTCHA_TOKENS[:3])

    @staticmethod
    def _payload_indicates_captcha(payload: Mapping[str, Any]) -> bool:
        values = [
            payload.get("status_msg"),
            payload.get("status_message"),
            payload.get("message"),
            payload.get("description"),
        ]
        data = payload.get("data")
        if isinstance(data, Mapping):
            values.extend(
                (
                    data.get("status_msg"),
                    data.get("message"),
                    data.get("description"),
                )
            )
        combined = " ".join(str(value or "") for value in values).lower()
        return any(token in combined for token in _CAPTCHA_TOKENS)

    @staticmethod
    def _payload_has_error_status(payload: Mapping[str, Any]) -> bool:
        containers: list[Mapping[str, Any]] = [payload]
        data = payload.get("data")
        if isinstance(data, Mapping):
            containers.append(data)
        for container in containers:
            for key in ("status_code", "statusCode"):
                if key not in container:
                    continue
                value = container.get(key)
                try:
                    if int(value) != 0:
                        return True
                except (TypeError, ValueError):
                    if str(value or "").strip():
                        return True
        return False
