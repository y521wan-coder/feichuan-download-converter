"""抖音 Chromium 会话。

匿名模式使用一次性 profile；交互登录模式使用软件自己的独立持久 profile，绝不读取或
复制用户日常 Chrome 配置。Cookie header 只在当前 CDP 会话内存中使用，不写入日志；
登录状态由 Chrome 自身加密并保存在专用 profile 中。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .cdp_client import CdpClient
from .chromium_session import ChromiumSession, clear_douyin_chromium_profile


_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


class DouyinAnonymousSession:
    """已启用 Network/Page/Runtime 的一次性匿名页面会话。"""

    __slots__ = (
        "chromium",
        "client",
        "page_url",
        "_cookie_header",
        "_closed",
    )

    def __init__(
        self,
        chromium: ChromiumSession,
        client: CdpClient,
        page_url: str,
    ) -> None:
        self.chromium = chromium
        self.client = client
        self.page_url = str(page_url)
        self._cookie_header = ""
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def cookie_header(self, url: str | None = None) -> str:
        """返回当前页面的一次性 Cookie header，不写日志或磁盘。"""

        if self._closed:
            return ""
        self._cookie_header = self.chromium.cookie_header(
            self.client,
            str(url or self.page_url),
        )
        return self._cookie_header

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cookie_header = ""
        self.chromium.close()

    def __enter__(self) -> "DouyinAnonymousSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "DouyinAnonymousSession("
            f"closed={self._closed!r}, cookie=<in-memory-redacted>)"
        )

    def __getstate__(self) -> object:
        raise TypeError("匿名抖音会话含临时 Cookie，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("匿名抖音会话含临时 Cookie，禁止序列化。")


class DouyinSessionProvider:
    """创建匿名临时会话，或创建可复用登录状态的独立可见会话。"""

    def __init__(
        self,
        chromium_factory: Callable[[], ChromiumSession] | None = None,
        *,
        interactive_login: bool = False,
    ) -> None:
        self.interactive_login = bool(interactive_login)
        self._chromium_factory = chromium_factory or (
            lambda: ChromiumSession(
                headless=not self.interactive_login,
                incognito=False,
                persistent_douyin_profile=self.interactive_login,
            )
        )

    @staticmethod
    def clear_saved_login_profile() -> bool:
        """清除软件自己的抖音登录状态；浏览器运行时会拒绝。"""

        return clear_douyin_chromium_profile()

    def open(
        self,
        url: str,
        *,
        cancel_event: Any = None,
    ) -> DouyinAnonymousSession:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("抖音页面地址必须是 http 或 https URL。")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("任务已取消。")

        chromium = self._chromium_factory()
        try:
            chromium.start(cancel_event)
            client = chromium.open_page("about:blank")
            client.command(
                "Network.enable",
                {
                    "maxTotalBufferSize": 100_000_000,
                    "maxResourceBufferSize": 10_000_000,
                },
                timeout=10,
            )
            client.command("Page.enable", timeout=10)
            client.command("Runtime.enable", timeout=10)
            client.command(
                "Network.setCacheDisabled",
                {"cacheDisabled": True},
                timeout=10,
            )
            client.command(
                "Network.setUserAgentOverride",
                {
                    "userAgent": self._normal_user_agent(client),
                    "acceptLanguage": "zh-CN,zh;q=0.9",
                    "platform": "Win32",
                },
                timeout=10,
            )
            navigation = client.command(
                "Page.navigate",
                {"url": url},
                timeout=15,
            )
            error_text = str(navigation.get("errorText") or "").strip()
            if error_text:
                raise RuntimeError(f"打开抖音页面失败：{error_text}")
            if self.interactive_login:
                try:
                    client.command("Page.bringToFront", timeout=5)
                except Exception:
                    pass
            return DouyinAnonymousSession(chromium, client, url)
        except Exception:
            chromium.close()
            raise

    @staticmethod
    def _normal_user_agent(client: Any) -> str:
        """Hide only Chromium's headless UA token while preserving its version."""

        try:
            result = client.command("Browser.getVersion", timeout=5)
            user_agent = str(result.get("userAgent") or "").strip()
        except Exception:
            user_agent = ""
        if user_agent:
            user_agent = user_agent.replace("HeadlessChrome/", "Chrome/")
        return user_agent or _FALLBACK_USER_AGENT
