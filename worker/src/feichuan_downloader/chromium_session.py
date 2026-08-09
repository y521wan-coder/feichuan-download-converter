"""Owned Chromium sessions for anonymous scans and an isolated Douyin login."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import requests
from websocket import create_connection

from .cdp_client import CdpClient
from .config import app_data_dir, douyin_chromium_profile_dir

try:
    import msvcrt
except ImportError:  # pragma: no cover - the application is distributed for Windows.
    msvcrt = None  # type: ignore[assignment]


_PROFILE_GUARD = threading.RLock()
_ACTIVE_PERSISTENT_PROFILES: set[str] = set()


def _absolute_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(_absolute_path(path)))


def _validated_douyin_profile_path(path: Path | str | None = None) -> Path:
    """Accept only the dedicated profile and reject symlink/path traversal escapes."""

    expected = _absolute_path(douyin_chromium_profile_dir())
    target = _absolute_path(path) if path is not None else expected
    if _path_key(target) != _path_key(expected):
        raise ValueError("只能操作飞船下载工具自己的抖音登录资料目录。")

    root = _absolute_path(app_data_dir())
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("抖音登录资料目录超出应用数据目录。") from exc
    if target == root:
        raise ValueError("不能把应用数据根目录作为浏览器资料目录。")

    if target.exists():
        if target.is_symlink():
            raise ValueError("抖音登录资料目录不能是符号链接。")
        resolved_root = root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("抖音登录资料目录解析后超出应用数据目录。") from exc
    return target


def _profile_lock_path() -> Path:
    return _absolute_path(app_data_dir()) / ".douyin-profile.lock"


class _ProfileLease:
    """Hold an in-process and, on Windows, cross-process profile lock."""

    def __init__(self, profile: Path, lock_file: BinaryIO | None) -> None:
        self.profile = profile
        self.lock_file = lock_file
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        key = _path_key(self.profile)
        with _PROFILE_GUARD:
            _ACTIVE_PERSISTENT_PROFILES.discard(key)
            lock_file = self.lock_file
            self.lock_file = None
            if lock_file is not None:
                if msvcrt is not None:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except (OSError, ValueError):
                        pass
                try:
                    lock_file.close()
                except OSError:
                    pass


def _acquire_profile_lease(profile: Path) -> _ProfileLease:
    key = _path_key(profile)
    with _PROFILE_GUARD:
        if key in _ACTIVE_PERSISTENT_PROFILES:
            raise RuntimeError("抖音登录专用浏览器正在运行，不能同时操作其资料目录。")

        lock_file: BinaryIO | None = None
        if msvcrt is not None:
            lock_path = _profile_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+b")
            try:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                lock_file.close()
                raise RuntimeError(
                    "抖音登录专用浏览器正在其它软件进程中运行，不能操作资料目录。"
                ) from exc

        _ACTIVE_PERSISTENT_PROFILES.add(key)
        return _ProfileLease(profile, lock_file)


def clear_douyin_chromium_profile(path: Path | str | None = None) -> bool:
    """安全清除专用登录 profile；运行中的 profile 和其它路径一律拒绝。"""

    target = _validated_douyin_profile_path(path)
    lease = _acquire_profile_lease(target)
    try:
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True
    finally:
        lease.release()


def find_browser() -> Path | None:
    configured = os.environ.get("FEICHUAN_BROWSER", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    for name in ("chrome.exe", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def local_json(url: str, *, timeout: float = 5.0) -> Any:
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    finally:
        session.close()


class ChromiumSession:
    """Own a browser process and either a temporary or dedicated profile."""

    def __init__(
        self,
        browser: Path | None = None,
        *,
        startup_timeout: float = 20.0,
        headless: bool = True,
        incognito: bool = False,
        persistent_douyin_profile: bool = False,
    ) -> None:
        if persistent_douyin_profile and incognito:
            raise ValueError("持久抖音登录资料不能使用无痕模式。")
        self.browser = browser or find_browser()
        self.startup_timeout = startup_timeout
        self.headless = headless
        self.incognito = incognito
        self.persistent_douyin_profile = bool(persistent_douyin_profile)
        self.profile: Path | None = None
        self.port: int | None = None
        self.process: subprocess.Popen[Any] | None = None
        self._clients: list[CdpClient] = []
        self._temporary_profile: Path | None = None
        self._profile_lease: _ProfileLease | None = None

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None and self.port)

    def start(self, cancel_event: Any = None) -> "ChromiumSession":
        if self.running:
            return self
        if not self.browser or not self.browser.exists():
            raise RuntimeError("未找到 Chrome 或 Edge。")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("任务已取消。")

        if self.persistent_douyin_profile:
            profile = _validated_douyin_profile_path()
            self._profile_lease = _acquire_profile_lease(profile)
            try:
                profile.mkdir(parents=True, exist_ok=True)
            except Exception:
                self._profile_lease.release()
                self._profile_lease = None
                raise
        else:
            profile = Path(tempfile.mkdtemp(prefix="feichuan-chromium-"))
            self._temporary_profile = profile
        self.profile = profile
        self.port = free_loopback_port()
        args = [
            str(self.browser),
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1200,900",
            "about:blank",
        ]
        if self.incognito:
            args.insert(4, "--incognito")
        if self.headless:
            args.insert(4, "--headless=new")
        try:
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._wait_until_ready(cancel_event)
            return self
        except Exception:
            self.close()
            raise

    def open_page(self, url: str, *, timeout: float = 10.0) -> CdpClient:
        if not self.running or self.port is None:
            raise RuntimeError("Chromium 会话尚未启动。")
        if not url.startswith(("http://", "https://", "about:")):
            raise ValueError("页面地址必须是 http、https 或 about URL。")
        page = self._existing_page(url) if url == "about:blank" else None
        if page is None:
            page = self._create_page(url, timeout=timeout)
        websocket_url = page.get("webSocketDebuggerUrl")
        if not isinstance(websocket_url, str) or not websocket_url:
            raise RuntimeError("浏览器页面没有返回 DevTools 地址。")
        websocket = create_connection(
            websocket_url,
            timeout=1,
            suppress_origin=True,
            enable_multithread=True,
            http_proxy_host=None,
            http_proxy_port=None,
            http_no_proxy=["127.0.0.1", "localhost"],
        )
        client = CdpClient(websocket)
        self._clients.append(client)
        return client

    def _existing_page(self, url: str) -> dict[str, Any] | None:
        if self.port is None:
            return None
        try:
            pages = local_json(f"http://127.0.0.1:{self.port}/json/list", timeout=5)
        except Exception:
            return None
        if not isinstance(pages, list):
            return None
        for page in pages:
            if (
                isinstance(page, dict)
                and page.get("type") == "page"
                and str(page.get("url") or "") == url
                and page.get("webSocketDebuggerUrl")
            ):
                return page
        return None

    @staticmethod
    def cookie_header(client: CdpClient, url: str) -> str:
        result = client.command("Network.getCookies", {"urls": [url]}, timeout=5)
        cookies = result.get("cookies", [])
        pairs: list[str] = []
        if isinstance(cookies, list):
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                name = str(cookie.get("name", ""))
                value = str(cookie.get("value", ""))
                if name:
                    pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def close(self) -> None:
        process = self.process
        graceful_close_requested = False
        if process and process.poll() is None and self._clients:
            try:
                self._clients[0].command("Browser.close", timeout=3)
                graceful_close_requested = True
            except Exception:
                pass

        for client in reversed(self._clients):
            client.close()
        self._clients.clear()

        self.process = None
        if process and process.poll() is None:
            if graceful_close_requested:
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
        temporary_profile = self._temporary_profile
        self._temporary_profile = None
        self.profile = None
        self.port = None
        if temporary_profile:
            for _ in range(5):
                shutil.rmtree(temporary_profile, ignore_errors=True)
                if not temporary_profile.exists():
                    break
                time.sleep(0.1)
        profile_lease = self._profile_lease
        self._profile_lease = None
        if profile_lease is not None:
            profile_lease.release()

    def clear_persistent_profile(self) -> bool:
        """Clear this app's dedicated Douyin profile while no session is active."""

        if not self.persistent_douyin_profile:
            raise RuntimeError("当前会话没有使用持久抖音登录资料。")
        if self._profile_lease is not None or self.running:
            raise RuntimeError("抖音登录专用浏览器正在运行，不能清除资料目录。")
        return clear_douyin_chromium_profile()

    def _wait_until_ready(self, cancel_event: Any = None) -> dict[str, Any]:
        assert self.port is not None
        deadline = time.monotonic() + self.startup_timeout
        endpoint = f"http://127.0.0.1:{self.port}/json/version"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消。")
            if self.process and self.process.poll() is not None:
                raise RuntimeError("Chromium 在调试端口就绪前退出。")
            try:
                value = local_json(endpoint, timeout=2)
                if isinstance(value, dict):
                    return value
            except Exception as exc:
                last_error = exc
                time.sleep(0.15)
        raise RuntimeError(f"浏览器调试端口未启动：{last_error or '超时'}")

    def _create_page(self, url: str, *, timeout: float) -> dict[str, Any]:
        assert self.port is not None
        encoded = quote(url, safe=":/?=&%")
        endpoint = f"http://127.0.0.1:{self.port}/json/new?{encoded}"
        session = requests.Session()
        session.trust_env = False
        try:
            for method in ("PUT", "GET"):
                try:
                    response = session.request(method, endpoint, timeout=timeout)
                    if response.ok:
                        value = response.json()
                        if isinstance(value, dict):
                            return value
                except Exception:
                    continue
        finally:
            session.close()
        pages = local_json(f"http://127.0.0.1:{self.port}/json/list", timeout=5)
        if isinstance(pages, list):
            for page in pages:
                if isinstance(page, dict) and page.get("type") == "page":
                    return page
        raise RuntimeError("无法创建浏览器页面。")

    def __enter__(self) -> "ChromiumSession":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()
