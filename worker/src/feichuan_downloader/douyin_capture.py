"""使用临时 Chromium/Chrome DevTools 会话抓取抖音媒体流。

该模块不读取用户浏览器 profile，也不保存捕获到的签名 URL。它只在一次下载期间
创建临时 profile，监听页面网络响应，下载候选媒体后清理浏览器和 profile。
"""

from __future__ import annotations

import os
import re
import shutil
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
        self._last_merge_error = ""

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

    def resolve_direct_link(
        self,
        url: str,
        *,
        require_audio_only: bool = False,
        on_line: LineCallback | None = None,
        cancel_event: Any = None,
    ) -> tuple[str, str]:
        """Capture one direct URL without downloading media or logging the URL."""

        if not is_douyin_url(url):
            raise RuntimeError("浏览器直连解析不支持此链接来源。")
        if not self.browser or not self.browser.exists():
            raise RuntimeError("未找到 Chrome 或 Edge，无法启用抖音浏览器解析。")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("直连解析已取消。")
        session = ChromiumSession(self.browser)
        try:
            self._emit("下载核心没有返回直连，正在启用抖音浏览器解析……", on_line)
            session.start(cancel_event=cancel_event)
            client = session.open_page("about:blank")
            client.command("Network.enable", timeout=5)
            client.command("Page.enable", timeout=5)
            client.command("Runtime.enable", timeout=5)
            client.command("Page.navigate", {"url": url}, timeout=10)
            media, _title = self._collect_media(
                client,
                self.timeout,
                on_line,
                cancel_event=cancel_event,
            )
            if not media:
                raise RuntimeError("页面中没有发现可复制的媒体直连。")

            return self._select_direct_candidate(
                media,
                require_audio_only=require_audio_only,
            )
        finally:
            session.close()

    @classmethod
    def _select_direct_candidate(
        cls,
        media: list[dict[str, Any]],
        *,
        require_audio_only: bool = False,
    ) -> tuple[str, str]:
        if not require_audio_only:
            combined = [
                item
                for item in media
                if ".m3u8" in str(item.get("url") or "").lower()
                or "mpegurl" in str(item.get("mime") or "").lower()
            ]
            if combined:
                selected = cls._sort_candidates(combined)[0]
                return str(selected["url"]), "combined"

        audio = cls._find_audio_candidate(media)
        if audio is not None:
            return str(audio["url"]), "audio"

        if require_audio_only:
            raise RuntimeError("页面中没有发现可复制的图文背景音频直连。")
        ordered = cls._sort_candidates(media)
        if len(ordered) == 1:
            return str(ordered[0]["url"]), "combined"
        raise RuntimeError("页面返回了无法可靠区分的音视频分轨，未复制可能错误的直连。")

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
    def _probe_duration(path: Path) -> float | None:
        """Return the container duration without exposing media details."""
        if not FFPROBE_PATH.exists():
            return None
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
            if completed.returncode or not text:
                return None
            duration = float(text.split("\n", 1)[0])
            return duration if duration > 0 else None
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    @staticmethod
    def _is_placeholder_video(path: Path) -> bool:
        """Return True when ffprobe reports the clip is shorter than 5 seconds.

        Douyin pages sometimes preload a ~2-second placeholder MP4 alongside
        the real VOD.  The placeholder passes header/magic validation but is
        not the content the user requested.
        """
        duration = DouyinCapture._probe_duration(path)
        return duration is not None and duration < 5.0

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
        ordered_candidates = self._ordered_candidates(candidates, source_url)[:8]
        if is_note and not ordered_candidates:
            raise RuntimeError("未发现可下载的图文背景音频。")
        download_dir = get_download_dir()
        final_video_target = self._next_available(download_dir / f"{clean_title}.mp4")
        video_tracks: list[dict[str, Any]] = []
        audio_tracks: list[dict[str, Any]] = []
        kept_partials: list[Path] = []
        tried_pairs: set[tuple[str, str]] = set()

        base_headers = {
            "User-Agent": _USER_AGENT,
            "Referer": self._referer_for(source_url),
            "Accept": "*/*",
        }
        if cookie_header:
            base_headers["Cookie"] = cookie_header

        estimated_sizes = [
            self._probe_remote_size(candidate, base_headers, cancel_event)
            for candidate in ordered_candidates
        ]
        aggregate_total = (
            sum(estimated_sizes)
            if estimated_sizes and all(size > 0 for size in estimated_sizes)
            else 0
        )
        aggregate_completed = 0
        last_reported = 0.0

        def report_candidate_progress(candidate_index: int, value: float | None) -> None:
            nonlocal last_reported
            if on_progress is None:
                return
            if value is None:
                on_progress(None)
                return
            size = estimated_sizes[candidate_index]
            if aggregate_total > 0 and size > 0:
                overall = (aggregate_completed + size * value / 100.0) * 100.0 / aggregate_total
                last_reported = max(last_reported, min(99.0, overall))
                on_progress(last_reported)
            else:
                on_progress(max(0.0, min(99.0, value)))

        def download_candidate(
            candidate: dict[str, Any],
            partial: Path,
            candidate_index: int,
            *,
            report_progress: bool = True,
        ) -> tuple[set[str], float | None]:
            media_url = candidate["url"]
            callback = (
                (lambda value: report_candidate_progress(candidate_index, value))
                if report_progress
                else None
            )
            if ".m3u8" in media_url.lower() or "mpegurl" in candidate.get("mime", ""):
                self._download_hls(media_url, partial, base_headers, cancel_event)
            else:
                self._download_http(media_url, partial, base_headers, callback, cancel_event)
            if not partial.exists() or partial.stat().st_size == 0:
                raise RuntimeError("媒体文件为空。")
            self._validate_media_file(partial)
            duration = self._probe_duration(partial)
            if self._is_placeholder_video(partial):
                raise RuntimeError("候选视频时长过短，疑似占位片段。")
            return self._probe_stream_types(partial), duration

        def try_available_pairs() -> CaptureResult | None:
            for video in video_tracks:
                for audio in audio_tracks:
                    pair_key = (str(video["path"]), str(audio["path"]))
                    if pair_key in tried_pairs or not self._durations_compatible(
                        video.get("duration"), audio.get("duration")
                    ):
                        continue
                    tried_pairs.add(pair_key)
                    self._emit("正在校验并合并音视频……", on_line)
                    if on_progress:
                        on_progress(None)
                    if self._merge_completed(
                        video["path"],
                        audio["path"],
                        final_video_target,
                        kept_partials,
                    ):
                        return CaptureResult(path=final_video_target, title=clean_title)
            return None

        for index, candidate in enumerate(ordered_candidates):
            media_url = candidate["url"]
            suffix = self._candidate_suffix(candidate)
            target = (
                self._next_available(download_dir / f"{clean_title}{suffix}")
                if is_note
                else final_video_target
            )
            # 每个候选使用独立的临时文件名，避免视频轨与音频轨下载路径冲突。
            partial = target.with_name(f"{target.name}.cand{index + 1}.part")
            try:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("下载已取消。")
                self._emit(f"正在下载抖音媒体（候选 {index + 1}）……", on_line)
                stream_types, duration = download_candidate(candidate, partial, index)
            except Exception as exc:
                errors.append(_safe_error(str(exc)))
                self._safe_unlink(partial)
                aggregate_completed += estimated_sizes[index]
                continue
            aggregate_completed += estimated_sizes[index]
            if is_note:
                if "audio" not in stream_types:
                    errors.append("图文候选不是有效的音频。")
                    self._safe_unlink(partial)
                    continue
                os.replace(partial, target)
                self._cleanup_partials(kept_partials)
                return CaptureResult(path=target, title=clean_title)
            if {"video", "audio"}.issubset(stream_types):
                # 候选本身音画齐全（如 HLS 合并结果）。
                os.replace(partial, final_video_target)
                self._cleanup_partials(kept_partials)
                return CaptureResult(path=final_video_target, title=clean_title)
            if "video" in stream_types:
                if len(video_tracks) < 3:
                    video_tracks.append(
                        {"path": partial, "candidate": candidate, "duration": duration, "index": index}
                    )
                    kept_partials.append(partial)
                else:
                    self._safe_unlink(partial)
                merged = try_available_pairs()
                if merged is not None:
                    return merged
                continue
            if "audio" in stream_types:
                if len(audio_tracks) < 3:
                    audio_tracks.append(
                        {"path": partial, "candidate": candidate, "duration": duration, "index": index}
                    )
                    kept_partials.append(partial)
                else:
                    self._safe_unlink(partial)
                merged = try_available_pairs()
                if merged is not None:
                    return merged
                continue
            errors.append("候选不是有效的音视频。")
            self._safe_unlink(partial)

        # 长视频偶尔会得到可探测但不完整的分轨。首次封装全部失败后，
        # 从头重新下载时长最接近的一组轨道，再做一次最终封装。
        compatible_pairs = [
            (video, audio)
            for video in video_tracks
            for audio in audio_tracks
            if self._durations_compatible(video.get("duration"), audio.get("duration"))
        ]
        if compatible_pairs:
            best_video, best_audio = min(
                compatible_pairs,
                key=lambda pair: self._duration_difference(
                    pair[0].get("duration"), pair[1].get("duration")
                ),
            )
            retry_video = final_video_target.with_name(f"{final_video_target.name}.retry-video.part")
            retry_audio = final_video_target.with_name(f"{final_video_target.name}.retry-audio.part")
            retry_paths = [retry_video, retry_audio]
            try:
                self._emit("首次音视频合并未成功，正在重新获取完整分轨……", on_line)
                if on_progress:
                    on_progress(None)
                self._safe_unlink(retry_video)
                self._safe_unlink(retry_audio)
                video_types, video_duration = download_candidate(
                    best_video["candidate"], retry_video, best_video["index"], report_progress=False
                )
                audio_types, audio_duration = download_candidate(
                    best_audio["candidate"], retry_audio, best_audio["index"], report_progress=False
                )
                if (
                    "video" in video_types
                    and "audio" in audio_types
                    and self._durations_compatible(video_duration, audio_duration)
                ):
                    kept_partials.extend(retry_paths)
                    self._emit("正在校验并合并重新获取的音视频……", on_line)
                    if self._merge_completed(
                        retry_video, retry_audio, final_video_target, kept_partials
                    ):
                        return CaptureResult(path=final_video_target, title=clean_title)
                errors.append("重新获取分轨后仍无法生成完整音视频。")
            except Exception as exc:
                errors.append(_safe_error(str(exc)))
            finally:
                self._safe_unlink(retry_video)
                self._safe_unlink(retry_audio)

        self._cleanup_partials(kept_partials)
        if video_tracks and not audio_tracks:
            errors.append("候选视频缺少音频轨，且未捕获到独立音频流。")
        elif video_tracks and audio_tracks:
            errors.append(self._last_merge_error or "已获取视频轨与音频轨，但音视频合并失败。")
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
        if (
            mime.startswith("audio/")
            or suffix in AUDIO_EXTENSIONS
            or "ies-music" in lowered
            or "audio" in lowered
        ):
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
    def _duration_difference(first: float | None, second: float | None) -> float:
        if first is None or second is None:
            return float("inf")
        return abs(first - second)

    @staticmethod
    def _durations_compatible(first: float | None, second: float | None) -> bool:
        if first is None or second is None:
            return True
        tolerance = max(3.0, min(10.0, max(first, second) * 0.002))
        return abs(first - second) <= tolerance

    @staticmethod
    def _probe_remote_size(
        candidate: dict[str, Any],
        headers: dict[str, str],
        cancel_event: Any = None,
    ) -> int:
        """Read only response headers so split-track progress can be aggregated."""
        url = str(candidate.get("url") or "")
        if not url or ".m3u8" in url.lower():
            return 0
        try:
            host = (urlsplit(url).hostname or "").lower()
            if host.endswith(".example.test"):
                return max(0, int(candidate.get("content_length") or 0))
        except (TypeError, ValueError):
            return 0
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("下载已取消。")
        probe_headers = dict(headers)
        probe_headers["Range"] = "bytes=0-0"
        try:
            with requests.get(
                url,
                headers=probe_headers,
                stream=True,
                timeout=(8, 15),
                proxies={"http": None, "https": None},
            ) as response:
                if response.status_code not in {200, 206}:
                    return 0
                content_range = response.headers.get("content-range", "")
                match = re.search(r"/(\d+)\s*$", content_range)
                if match:
                    return max(0, int(match.group(1)))
                return max(0, int(response.headers.get("content-length", "0") or 0))
        except (OSError, ValueError, requests.RequestException):
            return 0

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
        last_error = ""
        for attempt in range(1, 4):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("下载已取消。")
            existing = partial.stat().st_size if partial.exists() else 0
            request_headers = dict(headers)
            if existing:
                request_headers["Range"] = f"bytes={existing}-"
            try:
                with requests.get(
                    url,
                    headers=request_headers,
                    stream=True,
                    timeout=(10, 60),
                    proxies={"http": None, "https": None},
                ) as response:
                    if response.status_code == 416 and existing:
                        if on_progress:
                            on_progress(99.0)
                        return
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith(("text/html", "text/plain", "image/")):
                        raise RuntimeError("媒体响应不是音视频内容。")

                    append = existing > 0 and response.status_code == 206
                    if not append:
                        existing = 0
                    content_length = int(response.headers.get("content-length", "0") or 0)
                    content_range = response.headers.get("content-range", "")
                    range_match = re.search(r"/(\d+)\s*$", content_range)
                    total = int(range_match.group(1)) if range_match else existing + content_length
                    received_this_response = 0
                    mode = "ab" if append else "wb"
                    with partial.open(mode) as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if cancel_event and cancel_event.is_set():
                                raise RuntimeError("下载已取消。")
                            if not chunk:
                                continue
                            output.write(chunk)
                            received_this_response += len(chunk)
                            if on_progress and total:
                                on_progress(
                                    min(99.0, (existing + received_this_response) * 100.0 / total)
                                )
                    final_size = partial.stat().st_size if partial.exists() else 0
                    if content_length and received_this_response != content_length:
                        last_error = "incomplete"
                        if attempt < 3:
                            continue
                        break
                    if total and final_size != total:
                        last_error = "incomplete"
                        if attempt < 3:
                            continue
                        break
                    return
            except RuntimeError:
                raise
            except (OSError, requests.RequestException, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt >= 3:
                    break
                time.sleep(0.5 * attempt)
        raise RuntimeError(
            "媒体下载不完整，重试后仍失败。"
            if last_error
            else "媒体下载失败。"
        )

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
            stdin=subprocess.DEVNULL,
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
            raise RuntimeError("缺少 FFmpeg，无法合并音视频。")
        required_space = video_path.stat().st_size + audio_path.stat().st_size + 64 * 1024 * 1024
        if shutil.disk_usage(destination.parent).free < required_space:
            raise RuntimeError("目标磁盘可用空间不足，无法生成完整视频。")
        # FFmpeg's faststart relocation can stall at EOF on this fixed Windows
        # build when the output has a very long Unicode name.  Mux to a short,
        # same-volume temporary name without faststart, then atomically rename.
        # Local downloads do not need the moov atom relocated for HTTP startup.
        mux_destination = destination.with_name(
            f".feichuan-mux-{os.getpid()}-{threading.get_ident()}.mp4.part"
        )
        try:
            mux_destination.unlink(missing_ok=True)
        except OSError:
            raise RuntimeError("无法准备音视频合并临时文件。")
        base = [
            str(FFMPEG_PATH),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
        # AAC compatibility conversion of a 90-minute stream can legitimately
        # need more than two minutes.  The previous fixed 120-second limit
        # killed a healthy attempt just before completion.
        strategies = [
            (
                base
                + ["-c:v", "copy", "-c:a", "copy", "-shortest", "-f", "mp4", str(mux_destination)],
                300,
            ),
            (
                base
                + ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-f", "mp4", str(mux_destination)],
                1800,
            ),
            (
                base
                + [
                "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-f", "mp4", str(mux_destination),
                ],
                1800,
            ),
        ]
        last_stderr = ""
        for command, timeout_seconds in strategies:
            mux_destination.unlink(missing_ok=True)
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                last_stderr = "timeout"
                continue
            except OSError:
                last_stderr = "oserror"
                continue
            if (
                completed.returncode == 0
                and mux_destination.exists()
                and mux_destination.stat().st_size > 0
            ):
                os.replace(mux_destination, destination)
                return True
            last_stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        lowered = last_stderr.lower()
        if "no space left" in lowered:
            raise RuntimeError("目标磁盘可用空间不足，无法生成完整视频。")
        if "permission denied" in lowered or "access is denied" in lowered:
            raise RuntimeError("目标目录拒绝写入，无法生成完整视频。")
        if any(
            marker in lowered
            for marker in ("invalid data", "moov atom not found", "end of file", "corrupt", "truncated")
        ):
            raise RuntimeError("下载的音视频分轨不完整，无法安全合并。")
        mux_destination.unlink(missing_ok=True)
        if last_stderr == "timeout":
            raise RuntimeError("音视频合并超时。")
        raise RuntimeError("FFmpeg 无法封装这组音视频轨。")

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
        merged = target.with_name(target.name + ".merged.part")
        try:
            try:
                if not self._merge_audio_video(video_partial, audio_partial, merged):
                    self._last_merge_error = "FFmpeg 无法封装这组音视频轨。"
                    return False
            except RuntimeError as exc:
                self._last_merge_error = _safe_error(str(exc))
                return False
            merged_types = self._probe_stream_types(merged)
            if not {"video", "audio"}.issubset(merged_types):
                self._last_merge_error = "合并结果缺少视频轨或音频轨。"
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
