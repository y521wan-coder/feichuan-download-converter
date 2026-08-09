"""使用临时 Chromium/Chrome DevTools 会话抓取抖音媒体流。

该模块不读取用户浏览器 profile，也不保存捕获到的签名 URL。它只在一次下载期间
创建临时 profile，监听页面网络响应，下载候选媒体后清理浏览器和 profile。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .cdp_client import CdpClient
from .chromium_session import ChromiumSession, find_browser
from .config import (
    FFMPEG_PATH,
    FFPROBE_PATH,
    AUDIO_EXTENSIONS,
    get_download_dir,
    safe_url_for_log,
    sanitize_filename,
)


LineCallback = Callable[[str], None]
ProgressCallback = Callable[[float | None], None]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif")
_URL_RE = re.compile(r"https?://[^\s\]>)'\"]+")


def _safe_error(value: str) -> str:
    return _URL_RE.sub(lambda match: safe_url_for_log(match.group(0)), value).strip()


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    title: str = ""
    used_douyin_fallback: bool = True


def is_douyin_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(
        ".iesdouyin.com"
    )


class DouyinCapture:
    def __init__(self, browser: Path | None = None, timeout: float = 45) -> None:
        self.browser = browser or find_browser()
        self.timeout = timeout

    def capture(
        self,
        url: str,
        on_line: LineCallback | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_event: Any = None,
    ) -> CaptureResult:
        if not is_douyin_url(url):
            raise RuntimeError("浏览器抓流兜底不支持此链接来源。")
        if not self.browser or not self.browser.exists():
            raise RuntimeError("未找到 Chrome 或 Edge，无法启用抖音抓流兜底。")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("下载已取消。")
        get_download_dir().mkdir(parents=True, exist_ok=True)
        session = ChromiumSession(self.browser)
        close_synchronously = True
        try:
            self._emit("正在启动临时浏览器抓取媒体流……", on_line)
            session.start(cancel_event=cancel_event)
            client = session.open_page("about:blank")
            client.command("Network.enable", timeout=5)
            client.command("Page.enable", timeout=5)
            client.command("Runtime.enable", timeout=5)
            client.command("Page.navigate", {"url": url}, timeout=10)
            client.command(
                "Runtime.evaluate",
                {
                    "expression": (
                        "document.title || "
                        "document.querySelector('meta[property=\\\"og:title\\\"]')?.content || "
                        "document.querySelector('meta[name=\\\"description\\\"]')?.content || ''"
                    ),
                    "returnByValue": True,
                },
            )
            media, title = self._collect_media(
                client,
                self.timeout,
                on_line,
                cancel_event=cancel_event,
            )
            if not media:
                raise RuntimeError("页面中没有发现可下载的媒体流。")
            self._emit(f"已发现 {len(media)} 个媒体候选，正在选择清晰度……", on_line)
            cookie_header = ChromiumSession.cookie_header(client, url)
            result = self._download_candidates(
                media,
                title,
                url,
                on_line,
                on_progress,
                cancel_event=cancel_event,
                cookie_header=cookie_header,
            )
            if on_progress:
                on_progress(100.0)
            close_synchronously = False
            self._close_session_async(session)
            return result
        finally:
            if close_synchronously:
                session.close()

    @staticmethod
    def _close_session_async(session: ChromiumSession) -> None:
        def worker() -> None:
            session.close()

        threading.Thread(
            target=worker,
            name="douyin-capture-cleanup",
            daemon=True,
        ).start()

    @staticmethod
    def _collect_media(
        client: CdpClient,
        timeout: float,
        on_line: LineCallback | None,
        cancel_event: Any = None,
    ) -> tuple[list[dict[str, Any]], str]:
        deadline = time.monotonic() + timeout
        candidates: dict[str, dict[str, Any]] = {}
        title = ""
        last_title_request = 0.0
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("下载已取消。")
            if not title and time.monotonic() - last_title_request >= 1.0:
                try:
                    result = client.command(
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "document.title || "
                                "document.querySelector('meta[property=\\\"og:title\\\"]')?.content || "
                                "document.querySelector('meta[name=\\\"description\\\"]')?.content || ''"
                            ),
                            "returnByValue": True,
                        },
                        timeout=2,
                    )
                    value = result.get("result", {}).get("value")
                    if isinstance(value, str) and value and len(value) < 300:
                        title = value.strip()
                except Exception:
                    pass
                last_title_request = time.monotonic()
            message = client.next_event(
                timeout=min(1.0, max(0.1, deadline - time.monotonic()))
            )
            if not message:
                continue
            method = message.get("method")
            params = message.get("params", {})
            if method == "Network.responseReceived":
                response = params.get("response", {})
                media_url = str(response.get("url", ""))
                mime = str(response.get("mimeType", "")).lower()
                headers = response.get("headers", {}) or {}
                content_length = 0
                for key, value in headers.items():
                    if str(key).lower() == "content-length":
                        try:
                            content_length = int(str(value))
                        except ValueError:
                            pass
                lowered = media_url.lower()
                is_image = any(lowered.split("?", 1)[0].endswith(suffix) for suffix in _IMAGE_SUFFIXES)
                looks_media = (
                    mime.startswith("video/")
                    or mime.startswith("audio/")
                    or ".mp4" in lowered
                    or ".m3u8" in lowered
                    or ("douyinvod" in lowered and not is_image)
                )
                if looks_media and media_url.startswith(("http://", "https://")):
                    candidates[media_url] = {
                        "url": media_url,
                        "mime": mime,
                        "content_length": content_length,
                    }
            elif method == "Page.loadEventFired":
                # 页面加载完成后再留出一点时间给播放器请求媒体。
                if time.monotonic() + 4 < deadline:
                    deadline = min(deadline, time.monotonic() + 12)
            elif method == "Runtime.consoleAPICalled":
                continue
        ordered = DouyinCapture._sort_candidates(list(candidates.values()))
        if ordered and on_line:
            on_line("已捕获媒体响应，准备下载。")
        return ordered, title

    @staticmethod
    def _is_placeholder_video(path: Path) -> bool:
        """Return True when ffprobe reports the clip is shorter than 5 seconds.

        Douyin pages sometimes preload a ~2-second placeholder MP4 alongside
        the real VOD.  The placeholder passes header/magic validation but is
        not the content the user requested.
        """
        if not FFPROBE_PATH.exists():
            return False
        try:
            completed = subprocess.run(
                [
                    str(FFPROBE_PATH),
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
            if not text:
                return False
            duration = float(text.split("\n", 1)[0])
            return duration < 5.0
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return False

    def _download_candidates(
        self,
        candidates: list[dict[str, Any]],
        title: str,
        source_url: str,
        on_line: LineCallback | None,
        on_progress: ProgressCallback | None,
        cancel_event: Any = None,
        cookie_header: str = "",
    ) -> CaptureResult:
        fallback_title = f"douyin_{int(time.time())}"
        clean_title = sanitize_filename(title, fallback_title)
        errors: list[str] = []
        is_note = self._is_note_url(source_url)
        ordered_candidates = self._ordered_candidates(candidates, source_url)
        if is_note and not ordered_candidates:
            raise RuntimeError("未发现可下载的图文背景音频。")
        video_partial: Path | None = None
        audio_partial: Path | None = None
        kept_partials: list[Path] = []
        for index, candidate in enumerate(ordered_candidates[:8]):
            media_url = candidate["url"]
            suffix = self._candidate_suffix(candidate)
            target = self._next_available(get_download_dir() / f"{clean_title}{suffix}")
            # 每个候选使用独立的临时文件名，避免视频轨与音频轨下载路径冲突。
            partial = target.with_name(f"{target.name}.cand{index + 1}.part")
            headers = {
                "User-Agent": _USER_AGENT,
                "Referer": self._referer_for(source_url),
                "Accept": "*/*",
            }
            if cookie_header:
                headers["Cookie"] = cookie_header
            try:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("下载已取消。")
                self._emit(f"正在下载抖音媒体（候选 {index + 1}）……", on_line)
                if ".m3u8" in media_url.lower() or "mpegurl" in candidate.get("mime", ""):
                    self._download_hls(media_url, partial, headers, cancel_event)
                else:
                    self._download_http(media_url, partial, headers, on_progress, cancel_event)
                if not partial.exists() or partial.stat().st_size == 0:
                    raise RuntimeError("媒体文件为空。")
                self._validate_media_file(partial)
                if self._is_placeholder_video(partial):
                    raise RuntimeError("候选视频时长过短，疑似占位片段。")
            except Exception as exc:
                errors.append(_safe_error(str(exc)))
                self._safe_unlink(partial)
                continue
            stream_types = self._probe_stream_types(partial)
            is_video_kind = self._candidate_media_kind(candidate) == "video"
            if is_note or not is_video_kind:
                # 图文背景音频或非视频候选：保持原有行为直接保存。
                os.replace(partial, target)
                self._cleanup_partials(kept_partials)
                return CaptureResult(path=target, title=clean_title)
            if {"video", "audio"}.issubset(stream_types):
                # 候选本身音画齐全（如 HLS 合并结果）。
                os.replace(partial, target)
                self._cleanup_partials(kept_partials)
                return CaptureResult(path=target, title=clean_title)
            if "video" in stream_types:
                # 纯视频轨：暂存，等待独立音频轨配对合并。
                if video_partial is None:
                    video_partial = partial
                    kept_partials.append(partial)
                else:
                    self._safe_unlink(partial)
                if (
                    audio_partial is not None
                    and self._merge_completed(video_partial, audio_partial, target, kept_partials)
                ):
                    return CaptureResult(path=target, title=clean_title)
                continue
            if "audio" in stream_types:
                # 独立音频轨（抖音常把音频流 Content-Type 标为 video/mp4，需按内容识别）。
                if audio_partial is None:
                    audio_partial = partial
                    kept_partials.append(partial)
                else:
                    self._safe_unlink(partial)
                if (
                    video_partial is not None
                    and self._merge_completed(video_partial, audio_partial, target, kept_partials)
                ):
                    return CaptureResult(path=target, title=clean_title)
                continue
            errors.append("候选不是有效的音视频。")
            self._safe_unlink(partial)
        self._cleanup_partials(kept_partials)
        if video_partial is not None and audio_partial is None:
            errors.append("候选视频缺少音频轨，且未捕获到独立音频流。")
        elif video_partial is not None and audio_partial is not None:
            errors.append("已获取视频轨与音频轨，但音视频合并失败。")
        raise RuntimeError("；".join(errors[-3:]) or "所有媒体候选均下载失败。")

    @staticmethod
    def _sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Media candidates ordering: combined HLS first, then video, then size."""
        return sorted(
            candidates,
            key=lambda item: (
                ".m3u8" in str(item.get("url", "")).lower()
                or "mpegurl" in str(item.get("mime", "")).lower(),
                "video/" in str(item.get("mime", "")).lower(),
                item.get("content_length", 0),
            ),
            reverse=True,
        )

    @classmethod
    def _ordered_candidates(
        cls,
        candidates: list[dict[str, Any]],
        source_url: str,
    ) -> list[dict[str, Any]]:
        if cls._is_note_url(source_url):
            audio_candidates = [
                candidate
                for candidate in candidates
                if cls._candidate_media_kind(candidate) == "audio"
            ]
            return sorted(audio_candidates, key=cls._audio_score, reverse=True)
        return list(candidates)

    @staticmethod
    def _is_note_url(source_url: str) -> bool:
        try:
            path = urlsplit(source_url).path.lower().rstrip("/") + "/"
        except Exception:
            return False
        return "/note/" in path

    @classmethod
    def _candidate_media_kind(cls, candidate: dict[str, Any]) -> str:
        media_url = str(candidate.get("url") or "")
        mime = str(candidate.get("mime") or "").lower()
        suffix = Path(urlsplit(media_url).path).suffix.lower()
        lowered = media_url.lower()
        if mime.startswith("audio/") or suffix in AUDIO_EXTENSIONS or "ies-music" in lowered:
            return "audio"
        if mime.startswith("video/") or suffix in {".mp4", ".m4v", ".mov", ".webm", ".mkv"}:
            return "video"
        if ".m3u8" in lowered or "mpegurl" in mime:
            return "video"
        return ""

    @classmethod
    def _audio_score(cls, candidate: dict[str, Any]) -> tuple[int, int, int]:
        media_url = str(candidate.get("url") or "").lower()
        mime = str(candidate.get("mime") or "").lower()
        suffix = Path(urlsplit(media_url).path).suffix.lower()
        is_mp3 = int(mime == "audio/mpeg" or suffix == ".mp3")
        is_music = int("ies-music" in media_url)
        try:
            content_length = int(candidate.get("content_length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        return is_mp3, is_music, content_length

    @classmethod
    def _find_audio_candidate(
        cls, candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Best standalone audio candidate for merging, or None."""
        audio = [
            candidate
            for candidate in candidates
            if cls._candidate_media_kind(candidate) == "audio"
        ]
        if not audio:
            return None
        return max(audio, key=cls._audio_score)

    @classmethod
    def _candidate_suffix(cls, candidate: dict[str, Any]) -> str:
        media_url = str(candidate.get("url") or "")
        mime = str(candidate.get("mime") or "").lower()
        suffix = Path(urlsplit(media_url).path).suffix.lower()
        if suffix in {
            ".mp4",
            ".m4v",
            ".mov",
            ".webm",
            ".mkv",
            ".mp3",
            ".m4a",
            ".aac",
            ".ogg",
            ".opus",
            ".wav",
            ".weba",
        }:
            return suffix
        if mime == "audio/mpeg":
            return ".mp3"
        if mime in {"audio/mp4", "audio/x-m4a"}:
            return ".m4a"
        if mime.startswith("audio/"):
            return ".m4a"
        return ".mp4"

    @staticmethod
    def _referer_for(source_url: str) -> str:
        try:
            parts = urlsplit(source_url)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}/"
        except Exception:
            pass
        return "https://www.douyin.com/"

    @staticmethod
    def _download_http(
        url: str,
        partial: Path,
        headers: dict[str, str],
        on_progress: ProgressCallback | None,
        cancel_event: Any = None,
    ) -> None:
        with requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(10, 60),
            proxies={"http": None, "https": None},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type.startswith(("text/html", "text/plain", "image/")):
                raise RuntimeError("媒体响应不是音视频内容。")
            total = int(response.headers.get("content-length", "0") or 0)
            received = 0
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("下载已取消。")
                    if not chunk:
                        continue
                    output.write(chunk)
                    received += len(chunk)
                    if on_progress and total:
                        on_progress(min(99.0, received * 100.0 / total))

    @staticmethod
    def _download_hls(
        url: str,
        partial: Path,
        headers: dict[str, str],
        cancel_event: Any = None,
    ) -> None:
        if not FFMPEG_PATH.exists():
            raise RuntimeError("发现 HLS 媒体流，但 tools 中没有 ffmpeg.exe。")
        header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        command = [
            str(FFMPEG_PATH),
            "-y",
            "-loglevel",
            "error",
            "-headers",
            header_text,
            "-i",
            url,
            "-c",
            "copy",
            "-f",
            "mp4",
            str(partial),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise RuntimeError("下载已取消。")
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise RuntimeError("ffmpeg 合并 HLS 流失败。")
        finally:
            if process.poll() is None:
                process.kill()

    @staticmethod
    def _validate_media_file(path: Path) -> None:
        with path.open("rb") as source:
            head = source.read(4096)
        lowered = head.lower()
        if (
            head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8"))
            or (head.startswith(b"RIFF") and b"WEBP" in head[:16].upper())
        ):
            raise RuntimeError("候选响应是图片，不是视频。")
        if b"<html" in lowered[:512] or b"<!doctype" in lowered[:512]:
            raise RuntimeError("候选响应是网页错误页，不是媒体。")
        if not FFPROBE_PATH.exists():
            if not (b"ftyp" in head[:1024] or head.startswith(b"\x1a\x45\xdf\xa3")):
                raise RuntimeError("候选文件不是可识别的音视频格式。")
            return
        stream_types = DouyinCapture._probe_stream_types(path)
        if not stream_types.intersection({"video", "audio"}):
            raise RuntimeError("候选文件不是有效的音视频文件。")

    @staticmethod
    def _probe_stream_types(path: Path) -> set[str]:
        """Return the set of codec_type values ffprobe reports for a file."""
        if not FFPROBE_PATH.exists():
            return set()
        try:
            completed = subprocess.run(
                [
                    str(FFPROBE_PATH),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        return set((completed.stdout or b"").decode("utf-8", errors="replace").split())

    @staticmethod
    def _merge_audio_video(video_path: Path, audio_path: Path, destination: Path) -> bool:
        """Mux a video track and a separate audio track into one playable file."""
        if not FFMPEG_PATH.exists():
            return False
        base = [
            str(FFMPEG_PATH),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
        ]
        strategies = [
            base
            + ["-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart", "-f", "mp4", str(destination)],
            base
            + ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", "-f", "mp4", str(destination)],
        ]
        for command in strategies:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if (
                completed.returncode == 0
                and destination.exists()
                and destination.stat().st_size > 0
            ):
                return True
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        return False

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _cleanup_partials(paths: list[Path]) -> None:
        for stale in paths:
            DouyinCapture._safe_unlink(stale)

    def _merge_completed(
        self,
        video_partial: Path,
        audio_partial: Path,
        target: Path,
        kept_partials: list[Path],
    ) -> bool:
        """Mux the kept video and audio tracks and publish the complete file."""
        merged = target.with_name(target.name + ".merged.mp4")
        try:
            if not self._merge_audio_video(video_partial, audio_partial, merged):
                return False
            merged_types = self._probe_stream_types(merged)
            if not {"video", "audio"}.issubset(merged_types):
                return False
            os.replace(merged, target)
            self._cleanup_partials(kept_partials)
            return True
        finally:
            self._safe_unlink(merged)

    @staticmethod
    def _next_available(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError("同名文件过多，无法生成新的文件名。")

    @staticmethod
    def _emit(message: str, callback: LineCallback | None) -> None:
        if callback:
            callback(message)
