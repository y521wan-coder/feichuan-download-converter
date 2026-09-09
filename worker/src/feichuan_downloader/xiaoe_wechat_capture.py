"""Capture an owned Xiaoetong replay that the user opens in WeChat.

Only Chromium HTTP cache keys are inspected.  Cookies, local storage, history and
browser databases are never opened.  Signed media URLs remain in this object's
memory and are deliberately excluded from protocol payloads and representations.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen
import uuid

from .config import FFMPEG_PATH, FFPROBE_PATH, get_download_dir


_URL_PATTERN = re.compile(rb"https?://[^\x00-\x20\"<>]{8,4096}")
_MEDIA_SUFFIX_PATTERN = re.compile(r"\.(?:m3u8|ts)(?:$|[?#])", re.IGNORECASE)
_EXTINF_PATTERN = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_INVALID_FILENAME = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
_STRIP_FILENAME_PUNCTUATION = str.maketrans("", "", "「」『』“”‘’\"'")
_COURSE_COUNT_SUFFIX = re.compile(r"[（(]\s*\d+\s*讲\s*[）)]\s*$")
_ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".xet.tech",
    ".xiaoeknow.com",
    ".xet.citv.cn",
)


@dataclass(frozen=True, slots=True)
class XiaoeStreamCandidate:
    """An in-memory HLS choice; its URL must never be serialized."""

    _url: str
    duration_seconds: float

    @property
    def url(self) -> str:
        return self._url

    def __repr__(self) -> str:
        return f"XiaoeStreamCandidate(duration_seconds={self.duration_seconds!r}, url=<redacted>)"

    def __getstate__(self) -> object:
        raise TypeError("小鹅通媒体候选含签名地址，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("小鹅通媒体候选含签名地址，禁止序列化。")


@dataclass(frozen=True, slots=True)
class XiaoeEpisodeCaptureResult:
    path: Path
    skipped: bool
    duration_seconds: float


class XiaoeWechatCaptureSession:
    """A short-lived baseline and downloader for the current WeChat course."""

    def __init__(
        self,
        *,
        media_url_reader: Callable[[], tuple[str, ...]] | None = None,
        manifest_loader: Callable[[str, str], str] | None = None,
        ffmpeg_path: str | os.PathLike[str] = FFMPEG_PATH,
        ffprobe_path: str | os.PathLike[str] = FFPROBE_PATH,
        download_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        self._media_url_reader = media_url_reader or scan_wechat_media_cache_urls
        self._manifest_loader = manifest_loader or load_hls_manifest
        self._ffmpeg_path = Path(ffmpeg_path)
        self._ffprobe_path = Path(ffprobe_path)
        self._download_directory = (
            Path(download_directory) if download_directory is not None else get_download_dir()
        )
        self._baseline: set[str] = set()
        self._cleared = False
        self.arm()

    def arm(self) -> None:
        self._baseline.clear()
        self._baseline.update(self._media_url_reader())
        self._cleared = False

    def capture_episode(
        self,
        *,
        course_title: str,
        episode_title: str,
        episode_index: int,
        episode_total: int,
        page_url: str,
        cancel_event: threading.Event,
        on_progress: Callable[[float, str], None] | None = None,
        wait_seconds: float = 20.0,
    ) -> XiaoeEpisodeCaptureResult:
        if episode_index < 1 or episode_total < episode_index:
            raise ValueError("课程序号无效。")
        if not course_title.strip() or not episode_title.strip():
            raise ValueError("课程名称和视频名称不能为空。")

        target = episode_output_path(
            self._download_directory,
            course_title,
            episode_title,
            episode_index,
        )
        if target.is_file() and self._valid_video(target):
            return XiaoeEpisodeCaptureResult(target, True, self._file_duration(target))

        deadline = time.monotonic() + max(1.0, wait_seconds)
        new_urls: tuple[str, ...] = ()
        latest: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise InterruptedError("小鹅通课程下载已取消。")
            latest = self._media_url_reader()
            new_urls = tuple(url for url in latest if url not in self._baseline)
            if new_urls:
                break
            time.sleep(0.25)
        self._baseline.clear()
        self._baseline.update(latest)
        if not new_urls:
            raise RuntimeError(
                "没有捕获到当前课程视频，请确认微信播放页已经开始播放后重试。"
            )

        referer = public_referer(page_url)
        candidates: list[XiaoeStreamCandidate] = []
        seen: set[str] = set()
        for media_url in reversed(new_urls):
            manifest_url = manifest_url_for_media(media_url)
            if not manifest_url or manifest_url in seen:
                continue
            seen.add(manifest_url)
            try:
                manifest = self._manifest_loader(manifest_url, referer)
                duration = inspect_manifest_duration(manifest)
            except (OSError, ValueError, RuntimeError):
                continue
            candidates.append(XiaoeStreamCandidate(manifest_url, duration))

        if not candidates:
            raise RuntimeError(
                "捕获到播放请求，但没有找到可安全保存的完整 HLS 回放；加密或 DRM 内容不受支持。"
            )
        selected = max(candidates, key=lambda item: item.duration_seconds)

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = next_available_path(target)
        partial = target.with_name(
            f"{target.stem}.{uuid.uuid4().hex}.part{target.suffix}"
        )
        try:
            self._download_hls(
                selected.url,
                referer,
                partial,
                selected.duration_seconds,
                cancel_event,
                on_progress,
            )
            if not self._valid_video(partial):
                raise RuntimeError("下载结果没有同时通过视频和音频轨道校验。")
            os.replace(partial, target)
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        return XiaoeEpisodeCaptureResult(target, False, selected.duration_seconds)

    def clear_sensitive(self) -> None:
        self._baseline.clear()
        self._cleared = True

    def __repr__(self) -> str:
        return "XiaoeWechatCaptureSession(media_cache=<redacted>, signed_urls=<memory-only>)"

    __str__ = __repr__

    def __getstate__(self) -> object:
        raise TypeError("小鹅通捕获会话含签名地址，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("小鹅通捕获会话含签名地址，禁止序列化。")

    def _download_hls(
        self,
        url: str,
        referer: str,
        target: Path,
        duration_seconds: float,
        cancel_event: threading.Event,
        on_progress: Callable[[float, str], None] | None,
    ) -> None:
        if not self._ffmpeg_path.is_file():
            raise RuntimeError("缺少 FFmpeg，无法保存小鹅通课程视频。")
        headers = f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n"
        command = [
            str(self._ffmpeg_path),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-headers",
            headers,
            "-allowed_extensions",
            "ALL",
            "-i",
            url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            "-y",
            str(target),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                if cancel_event.is_set():
                    process.terminate()
                    raise InterruptedError("小鹅通课程下载已取消。")
                key, separator, value = raw_line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        elapsed = float(value) / 1_000_000.0
                    except ValueError:
                        continue
                    percent = min(99.0, elapsed * 100.0 / max(duration_seconds, 1.0))
                    if on_progress is not None:
                        on_progress(percent, "正在保存当前课程视频")
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if return_code != 0:
            raise RuntimeError("FFmpeg 保存小鹅通课程视频失败，请重新打开该节后重试。")

    def _valid_video(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < 1024 or not self._ffprobe_path.is_file():
            return False
        payload = self._probe_file(path)
        kinds = {
            str(stream.get("codec_type") or "")
            for stream in payload.get("streams", [])
            if isinstance(stream, dict)
        }
        return "video" in kinds and "audio" in kinds

    def _file_duration(self, path: Path) -> float:
        payload = self._probe_file(path)
        try:
            return float(payload.get("format", {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _probe_file(self, path: Path) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(self._ffprobe_path),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return {}
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


def scan_wechat_media_cache_urls() -> tuple[str, ...]:
    app_data = Path(os.environ.get("APPDATA", "")).expanduser()
    profiles = app_data / "Tencent" / "xwechat" / "radium" / "web" / "profiles"
    if not profiles.is_dir():
        raise RuntimeError("找不到微信网页缓存，请先在微信中播放自己已购买的课程。")
    ordered: list[str] = []
    for profile in profiles.glob("multitab_*"):
        cache_data = profile / "Cache" / "Cache_Data"
        if not cache_data.is_dir():
            continue
        for name in ("data_0", "data_1", "data_2", "data_3"):
            path = cache_data / name
            if not path.is_file():
                continue
            try:
                payload = read_file_shared(path)
            except OSError:
                continue
            ordered.extend(extract_media_urls(payload))
    if not ordered:
        raise RuntimeError("微信缓存中没有发现课程播放请求，请先开始播放一节课程。")
    return tuple(dict.fromkeys(ordered))


def extract_media_urls(payload: bytes) -> tuple[str, ...]:
    result: list[str] = []
    for match in _URL_PATTERN.findall(payload):
        value = match.decode("utf-8", "ignore").replace("\\u0026", "&").replace("&amp;", "&")
        if not _MEDIA_SUFFIX_PATTERN.search(value):
            continue
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not any(
            host.endswith(suffix) for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES
        ):
            continue
        result.append(value)
    return tuple(dict.fromkeys(result))


def manifest_url_for_media(media_url: str) -> str:
    parts = urlsplit(media_url)
    if parts.scheme != "https":
        return ""
    path = parts.path
    if path.lower().endswith(".m3u8"):
        return media_url
    if not path.lower().endswith(".ts") or "/" not in path:
        return ""
    parent = path.rsplit("/", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, parent + "/playlist_eof.m3u8", parts.query, ""))


def inspect_manifest_duration(manifest: str) -> float:
    if not manifest.lstrip().startswith("#EXTM3U"):
        raise ValueError("不是 HLS 清单。")
    if "#EXT-X-KEY" in manifest or "#EXT-X-SESSION-KEY" in manifest:
        raise ValueError("加密 HLS 不受支持。")
    if "#EXT-X-ENDLIST" not in manifest:
        raise ValueError("不是完整回放清单。")
    duration = sum(float(value) for value in _EXTINF_PATTERN.findall(manifest))
    if duration < 1.0:
        raise ValueError("HLS 清单没有有效时长。")
    return duration


def load_hls_manifest(url: str, referer: str) -> str:
    request = UrlRequest(
        url,
        headers={"Referer": referer, "User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - HTTPS host is allowlisted above.
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("HLS 清单过大。")
    return payload.decode("utf-8", "replace")


def public_referer(page_url: str) -> str:
    parts = urlsplit(page_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "https://xiaoeknow.com/"
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def episode_output_path(
    download_directory: str | os.PathLike[str],
    course_title: str,
    episode_title: str,
    episode_index: int,
) -> Path:
    clean_title = _COURSE_COUNT_SUFFIX.sub("", course_title.strip())
    # Preserve one separator between teacher and course, while removing title spacing.
    if "|" in clean_title:
        teacher, _, name = clean_title.partition("|")
        course = f"{_INVALID_FILENAME.sub('_', teacher).strip('_')}_{_INVALID_FILENAME.sub('', name)}"
    else:
        course = _INVALID_FILENAME.sub("_", clean_title).strip("_")
    folder = f"{course}_已购课程"
    episode = _INVALID_FILENAME.sub(
        "_",
        episode_title.strip().translate(_STRIP_FILENAME_PUNCTUATION),
    ).strip("_")
    return Path(download_directory) / folder / f"{episode_index:02d}_{episode}.mp4"


def next_available_path(path: Path) -> Path:
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名文件过多，无法分配安全输出名称。")


def read_file_shared(path: Path) -> bytes:
    if os.name != "nt":
        return path.read_bytes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_file_size = kernel32.GetFileSizeEx
    get_file_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    get_file_size.restype = wintypes.BOOL
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    read_file.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = ctypes.c_longlong()
        if not get_file_size(handle, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size.value)
        read = wintypes.DWORD()
        if not read_file(handle, buffer, size.value, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: read.value]
    finally:
        close_handle(handle)
