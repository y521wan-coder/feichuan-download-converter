"""yt-dlp 下载封装，以及抖音浏览器抓流兜底。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .config import (
    FFMPEG_PATH,
    TOOLS_DIR,
    YTDLP_PATH,
    get_download_dir,
    is_audio_url,
    safe_url_for_log,
)
from .douyin_capture import DouyinCapture, is_douyin_url
from .logging_utils import get_logger
from .quality import (
    QualityChoice,
    QualityPreference,
    choices_from_ytdlp_formats,
    ytdlp_format_selector,
)


LineCallback = Callable[[str], None]
ProgressCallback = Callable[[float | None], None]
PlaylistProgressCallback = Callable[[int, int, str], None]


class DownloadError(RuntimeError):
    """下载失败，消息已经适合直接显示给用户。"""


class DirectLinkResult:
    """A sensitive direct URL plus a public media-kind label."""

    __slots__ = ("media_kind", "_url", "_cleared")

    def __init__(self, url: str, media_kind: str) -> None:
        value = str(url or "").strip()
        kind = str(media_kind or "").strip()
        if not value:
            raise ValueError("直连不能为空。")
        if kind not in {"combined", "audio"}:
            raise ValueError("直连媒体类型无效。")
        self.media_kind = kind
        self._url = value
        self._cleared = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def cleared(self) -> bool:
        return self._cleared

    def clear_sensitive(self) -> None:
        self._url = ""
        self._cleared = True

    def __repr__(self) -> str:
        return f"DirectLinkResult(media_kind={self.media_kind!r}, sensitive=<in-memory-redacted>)"

    __str__ = __repr__

    def __getstate__(self) -> object:
        raise TypeError("直连结果含敏感临时字段，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("直连结果含敏感临时字段，禁止序列化。")


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str = ""
    used_douyin_fallback: bool = False
    paths: tuple[Path, ...] = ()
    partial_success: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        path = Path(self.path)
        normalized = tuple(Path(item) for item in self.paths)
        if not normalized:
            normalized = (path,)
        elif path not in normalized:
            normalized = (*normalized, path)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "paths", normalized)
        object.__setattr__(
            self,
            "warnings",
            tuple(
                str(value or "").strip()
                for value in self.warnings
                if str(value or "").strip()
            ),
        )


@dataclass(frozen=True, slots=True)
class UrlInspection:
    """只读预检得到的普通网页条目摘要。"""

    is_playlist: bool
    count: int
    title: str
    entries_preview: tuple[str, ...]
    original_url: str = field(repr=False)

    def __post_init__(self) -> None:
        count = int(self.count)
        if count < 0:
            raise ValueError("count 不能为负数。")
        object.__setattr__(self, "is_playlist", bool(self.is_playlist))
        object.__setattr__(self, "count", count)
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(
            self,
            "entries_preview",
            tuple(str(item or "").strip() for item in self.entries_preview if str(item or "").strip()),
        )
        object.__setattr__(self, "original_url", str(self.original_url or "").strip())


_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_PLAYLIST_MARKER = "__FEICHUAN_PLAYLIST_ITEM__"
_PLAYLIST_MARKER_RE = re.compile(
    rf"^{re.escape(_PLAYLIST_MARKER)}(?P<current>\d+)\s*/\s*(?P<total>\d+)"
    r"(?:\t(?P<title>.*))?$"
)
_PLAYLIST_PROGRESS_RE = re.compile(
    r"\[download\]\s+Downloading\s+(?:item|video)\s+"
    r"(?P<current>\d+)\s+of\s+(?P<total>\d+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\]\)>'\"]+")
_INSPECTION_PREVIEW_LIMIT = 8


def _safe_line(line: str) -> str:
    """不把签名 query/token 原样写入日志。"""

    def replace(match: re.Match[str]) -> str:
        return safe_url_for_log(match.group(0))

    return _URL_RE.sub(replace, line).strip()


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


class Downloader:
    def __init__(self, log_callback: LineCallback | None = None) -> None:
        self.logger = get_logger(TOOLS_DIR.parent / "logs")
        self.log_callback = log_callback
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def core_path(self) -> Path:
        return YTDLP_PATH

    def core_version(self) -> str:
        if not YTDLP_PATH.exists():
            return "未安装"
        try:
            completed = subprocess.run(
                [str(YTDLP_PATH), "--version"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (completed.stdout or completed.stderr).decode(
                "utf-8", errors="replace"
            )
            return next((line.strip() for line in output.splitlines() if line.strip()), "未知")
        except Exception as exc:
            self.logger.warning("读取下载核心版本失败: %s", exc)
            return "未知"

    def cancel(self) -> None:
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def inspect_url(
        self,
        url: str,
        *,
        cancel_event: threading.Event | None = None,
        on_line: LineCallback | None = None,
    ) -> UrlInspection:
        """用 yt-dlp 只读预检一个普通网址，不创建媒体文件。"""

        url = (url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise DownloadError("请输入有效的 http 或 https 下载链接。")
        if not YTDLP_PATH.exists():
            raise DownloadError(f"找不到下载核心：{YTDLP_PATH}")
        if cancel_event and cancel_event.is_set():
            raise DownloadError("检测已取消。")

        command = [
            str(YTDLP_PATH),
            "--ignore-config",
            "--flat-playlist",
            "--dump-single-json",
            "--skip-download",
            "--simulate",
            "--no-progress",
            "--encoding",
            "utf-8",
            url,
        ]
        self._emit("正在检测页面中的视频数量……", on_line)
        self.logger.info("开始预检 %s", safe_url_for_log(url))
        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        stderr = b""
        cancel_requested_at: float | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._process = process
            while True:
                if cancel_event and cancel_event.is_set():
                    if cancel_requested_at is None:
                        cancel_requested_at = time.monotonic()
                        self.cancel()
                    elif time.monotonic() - cancel_requested_at >= 2.0:
                        try:
                            process.kill()
                        except OSError:
                            pass
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            safe_error = _safe_line(str(exc))
            raise DownloadError(f"启动下载核心预检失败：{safe_error}") from exc
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        if cancel_event and cancel_event.is_set():
            raise DownloadError("检测已取消。")
        return_code = process.returncode if process is not None else None
        stdout_text = stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if return_code != 0:
            diagnostics = [line for line in stderr_text.splitlines() if line.strip()]
            reason = _safe_line(diagnostics[-1] if diagnostics else "下载核心返回错误")
            raise DownloadError(f"无法确认页面视频数量：{reason}")
        try:
            payload = json.loads(stdout_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DownloadError("无法确认页面视频数量：下载核心未返回有效列表信息。") from exc
        if not isinstance(payload, Mapping):
            raise DownloadError("无法确认页面视频数量：下载核心返回的数据格式无效。")

        entries_value = payload.get("entries")
        entries = entries_value if isinstance(entries_value, list) else []
        reported_count = self._inspection_count(payload.get("playlist_count"))
        if reported_count is not None and reported_count > 0:
            count = reported_count
        elif entries:
            count = len(entries)
        elif not isinstance(entries_value, list):
            count = 1
        else:
            count = 0
        if count <= 0:
            raise DownloadError("无法确认页面视频数量：页面未返回可用的视频条目。")
        entry_type = str(payload.get("_type") or "").strip().lower()
        is_playlist = (
            entry_type in {"playlist", "multi_video"}
            or isinstance(entries_value, list)
            or count > 1
        )
        title = self._inspection_text(
            payload.get("title")
            or payload.get("playlist_title")
            or payload.get("fulltitle"),
            limit=300,
        )
        preview: list[str] = []
        for index, entry in enumerate(entries, start=1):
            if len(preview) >= _INSPECTION_PREVIEW_LIMIT:
                break
            if isinstance(entry, Mapping):
                entry_title = self._inspection_text(
                    entry.get("title") or entry.get("fulltitle") or entry.get("id"),
                    limit=180,
                )
            else:
                entry_title = ""
            preview.append(entry_title or f"第 {index} 个视频")

        inspection = UrlInspection(
            is_playlist=is_playlist,
            count=count,
            title=title,
            entries_preview=tuple(preview),
            original_url=url,
        )
        self.logger.info(
            "预检完成 %s：标题=%s，数量=%d，列表=%s",
            safe_url_for_log(url),
            title or "未知",
            count,
            is_playlist,
        )
        return inspection

    def inspect_quality_choices(
        self,
        url: str,
        *,
        playlist_mode: str = "single",
        cancel_event: threading.Event | None = None,
        on_line: LineCallback | None = None,
    ) -> tuple[QualityChoice, ...]:
        """Inspect available media formats without downloading content."""

        url = (url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise DownloadError("请输入有效的 http 或 https 下载链接。")
        if not YTDLP_PATH.exists():
            raise DownloadError(f"找不到下载核心：{YTDLP_PATH}")
        if cancel_event and cancel_event.is_set():
            raise DownloadError("检测已取消。")
        mode = str(getattr(playlist_mode, "value", playlist_mode) or "").strip().lower()
        command = [
            str(YTDLP_PATH),
            "--ignore-config",
            "--dump-single-json",
            "--skip-download",
            "--simulate",
            "--no-progress",
            "--encoding",
            "utf-8",
        ]
        if mode == "all":
            command.extend(["--yes-playlist", "--playlist-items", "1"])
        else:
            command.extend(["--no-playlist", "--playlist-items", "1"])
        command.append(url)
        self._emit("正在检测本次可选的画质和格式……", on_line)
        self.logger.info("开始检测可选品质 %s", safe_url_for_log(url))
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._process = process
            while True:
                if cancel_event and cancel_event.is_set():
                    self.cancel()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            safe_error = _safe_line(str(exc))
            raise DownloadError(f"启动下载核心品质检测失败：{safe_error}") from exc
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None
        if cancel_event and cancel_event.is_set():
            raise DownloadError("检测已取消。")
        stdout_text = stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            diagnostics = [line for line in stderr_text.splitlines() if line.strip()]
            reason = _safe_line(diagnostics[-1] if diagnostics else "下载核心返回错误")
            raise DownloadError(f"无法检测可选品质：{reason}")
        try:
            payload = json.loads(stdout_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DownloadError("无法检测可选品质：下载核心未返回有效格式信息。") from exc
        formats = self._extract_formats(payload)
        return choices_from_ytdlp_formats(formats)

    def resolve_direct_link(
        self,
        url: str,
        *,
        cancel_event: threading.Event | None = None,
        on_line: LineCallback | None = None,
    ) -> DirectLinkResult:
        """Resolve one media URL without downloading or exposing it to logs."""

        url = (url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise DownloadError("请输入有效的 http 或 https 下载链接。")
        if not YTDLP_PATH.exists():
            raise DownloadError(f"找不到下载核心：{YTDLP_PATH}")
        if cancel_event and cancel_event.is_set():
            raise DownloadError("直连解析已取消。")

        command = [
            str(YTDLP_PATH),
            "--ignore-config",
            "--dump-single-json",
            "--skip-download",
            "--simulate",
            "--no-progress",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--encoding",
            "utf-8",
            "-f",
            "b/bv*+ba",
            url,
        ]
        self._emit("正在解析单视频直连；不会下载媒体文件……", on_line)
        self.logger.info("开始解析单视频直连 %s", safe_url_for_log(url))
        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        stderr = b""
        cancel_requested_at: float | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._process = process
            while True:
                if cancel_event and cancel_event.is_set():
                    if cancel_requested_at is None:
                        cancel_requested_at = time.monotonic()
                        self.cancel()
                    elif time.monotonic() - cancel_requested_at >= 2.0:
                        try:
                            process.kill()
                        except OSError:
                            pass
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            raise DownloadError("启动下载核心解析直连失败。") from exc
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        if cancel_event and cancel_event.is_set():
            raise DownloadError("直连解析已取消。")
        if process is None or process.returncode != 0:
            if is_douyin_url(url):
                try:
                    direct_url, media_kind = DouyinCapture().resolve_direct_link(
                        url,
                        on_line=on_line,
                        cancel_event=cancel_event,
                    )
                    return DirectLinkResult(direct_url, media_kind)
                except Exception:
                    pass
            diagnostics = stderr.decode("utf-8", errors="replace").splitlines()
            reason = _safe_line(next((line for line in reversed(diagnostics) if line.strip()), ""))
            raise DownloadError(f"无法解析单视频直连：{reason or '下载核心返回错误'}")
        try:
            payload = json.loads(stdout.decode("utf-8", errors="strict").lstrip("\ufeff").strip())
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DownloadError("无法解析单视频直连：下载核心返回的数据格式无效。") from exc
        if not isinstance(payload, Mapping):
            raise DownloadError("无法解析单视频直连：下载核心返回的数据格式无效。")

        try:
            resolved = self._select_direct_link(payload)
        except DownloadError:
            if not is_douyin_url(url):
                raise
            try:
                direct_url, media_kind = DouyinCapture().resolve_direct_link(
                    url,
                    on_line=on_line,
                    cancel_event=cancel_event,
                )
                resolved = DirectLinkResult(direct_url, media_kind)
            except Exception as exc:
                raise DownloadError("当前单视频没有可复制的音画合一直连或音频直连。") from exc
        self.logger.info("单视频直连解析完成：类型=%s，地址未记录", resolved.media_kind)
        return resolved

    @classmethod
    def _select_direct_link(cls, payload: Mapping[str, Any]) -> DirectLinkResult:
        live_status = str(payload.get("live_status") or "").strip().lower()
        if payload.get("is_live") is True or live_status in {"is_live", "is_upcoming"}:
            raise DownloadError("获取解析直连不支持直播。")
        selected: list[Mapping[str, Any]] = []
        requested_downloads = payload.get("requested_downloads")
        if isinstance(requested_downloads, list):
            for item in requested_downloads:
                if not isinstance(item, Mapping):
                    continue
                nested = item.get("requested_formats")
                if isinstance(nested, list):
                    selected.extend(value for value in nested if isinstance(value, Mapping))
                else:
                    selected.append(item)
        requested_formats = payload.get("requested_formats")
        if isinstance(requested_formats, list):
            selected.extend(value for value in requested_formats if isinstance(value, Mapping))
        if not selected:
            selected.append(payload)

        combined = [item for item in selected if cls._has_video(item) and cls._has_audio(item)]
        if not combined:
            formats = payload.get("formats")
            if isinstance(formats, list):
                combined = [
                    item
                    for item in formats
                    if isinstance(item, Mapping) and cls._has_video(item) and cls._has_audio(item)
                ]
        candidate = cls._best_direct_candidate(combined)
        if candidate is not None:
            return DirectLinkResult(candidate, "combined")

        audio = [item for item in selected if cls._has_audio(item) and not cls._has_video(item)]
        if not audio:
            formats = payload.get("formats")
            if isinstance(formats, list):
                audio = [
                    item
                    for item in formats
                    if isinstance(item, Mapping) and cls._has_audio(item) and not cls._has_video(item)
                ]
        candidate = cls._best_direct_candidate(audio)
        if candidate is not None:
            return DirectLinkResult(candidate, "audio")
        raise DownloadError("当前单视频没有可复制的音画合一直连或音频直连。")

    @staticmethod
    def _has_video(format_info: Mapping[str, Any]) -> bool:
        value = str(format_info.get("vcodec") or "").strip().lower()
        return bool(value and value != "none")

    @staticmethod
    def _has_audio(format_info: Mapping[str, Any]) -> bool:
        value = str(format_info.get("acodec") or "").strip().lower()
        return bool(value and value != "none")

    @staticmethod
    def _best_direct_candidate(candidates: Iterable[Mapping[str, Any]]) -> str | None:
        valid: list[tuple[tuple[float, float, float, float], str]] = []
        for item in candidates:
            value = str(item.get("url") or "").strip()
            try:
                parts = urlsplit(value)
            except Exception:
                continue
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                continue

            def number(key: str) -> float:
                raw = item.get(key)
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    return 0.0

            score = (number("quality"), number("height"), number("tbr"), number("abr"))
            valid.append((score, value))
        return max(valid, default=None, key=lambda item: item[0])[1] if valid else None

    def download(
        self,
        url: str,
        on_line: LineCallback | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        *,
        output_name_template: str | None = None,
        force_overwrites: bool = False,
        playlist_mode: str = "single",
        expected_count: int | None = None,
        on_playlist_progress: PlaylistProgressCallback | None = None,
        download_dir: str | os.PathLike[str] | None = None,
        quality_preference: QualityPreference | str | None = None,
        force_audio_only: bool = False,
    ) -> DownloadResult:
        url = (url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise DownloadError("请输入有效的 http 或 https 下载链接。")
        raw_playlist_mode = getattr(playlist_mode, "value", playlist_mode)
        selected_playlist_mode = str(raw_playlist_mode or "").strip().lower()
        if selected_playlist_mode not in {"single", "all"}:
            raise DownloadError("playlist_mode 必须是 single 或 all。")
        if expected_count is not None:
            if isinstance(expected_count, bool):
                raise DownloadError("expected_count 必须是非负整数。")
            try:
                expected_count = int(expected_count)
            except (TypeError, ValueError) as exc:
                raise DownloadError("expected_count 必须是非负整数。") from exc
            if expected_count < 0:
                raise DownloadError("expected_count 必须是非负整数。")
        if not YTDLP_PATH.exists():
            if is_douyin_url(url) and not (cancel_event and cancel_event.is_set()):
                return self._douyin_fallback(url, on_line, on_progress, cancel_event)
            raise DownloadError(f"找不到下载核心：{YTDLP_PATH}")
        download_dir = Path(download_dir) if download_dir is not None else get_download_dir()
        download_dir.mkdir(parents=True, exist_ok=True)

        started_at = time.time()
        before_snapshot = self._snapshot_download_dir(download_dir)
        audio_only = bool(force_audio_only or is_audio_url(url))
        name_template = str(output_name_template or "%(title)s.%(ext)s").strip()
        if (
            not name_template
            or "/" in name_template
            or "\\" in name_template
            or name_template in {".", ".."}
        ):
            raise DownloadError("输出文件名模板无效。")
        output_template = str(download_dir / name_template)
        command: list[str] = [
            str(YTDLP_PATH),
            "--newline",
            "--encoding",
            "utf-8",
            "--windows-filenames",
            "--trim-filenames",
            "180",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--socket-timeout",
            "20",
            "--print",
            "after_move:filepath",
            "--progress",
            "-o",
            output_template,
        ]
        if selected_playlist_mode == "all":
            command.extend(
                [
                    "--yes-playlist",
                    "--print",
                    (
                        "before_dl:"
                        f"{_PLAYLIST_MARKER}%(playlist_index)s/%(playlist_count)s"
                        "\t%(title)s"
                    ),
                ]
            )
        else:
            command.extend(["--no-playlist", "--playlist-items", "1"])
        if force_overwrites:
            command.append("--force-overwrites")
        if FFMPEG_PATH.exists():
            command.extend(["--ffmpeg-location", str(TOOLS_DIR)])
        if audio_only:
            command.extend(
                [
                    "-f",
                    ytdlp_format_selector(quality_preference, audio_only=True),
                ]
            )
        else:
            command.extend(
                [
                    "-f",
                    ytdlp_format_selector(quality_preference, audio_only=False),
                ]
            )
        command.append(url)

        self._emit("正在解析链接……", on_line)
        self.logger.info("开始下载 %s", safe_url_for_log(url))
        lines: list[str] = []
        printed_paths: list[Path] = []
        error_lines: list[str] = []
        playlist_current = 0
        playlist_total = expected_count or 0
        playlist_title = ""
        last_playlist_progress: tuple[int, int, str] | None = None

        def emit_playlist_progress(current: int, total: int, title: str = "") -> None:
            nonlocal playlist_current, playlist_total, playlist_title
            nonlocal last_playlist_progress
            if selected_playlist_mode != "all" or not on_playlist_progress:
                return
            current = max(0, int(current))
            total = max(0, int(total)) or (expected_count or 0)
            if total and current > total:
                total = current
            clean_title = self._inspection_text(title, limit=300)
            snapshot = (current, total, clean_title)
            playlist_current, playlist_total, playlist_title = snapshot
            if snapshot == last_playlist_progress:
                return
            last_playlist_progress = snapshot
            try:
                on_playlist_progress(*snapshot)
            except Exception as exc:
                self.logger.warning("列表进度回调失败: %s", _safe_line(str(exc)))

        if selected_playlist_mode == "all" and expected_count:
            emit_playlist_progress(0, expected_count)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._process = process
            assert process.stdout is not None
            for raw in iter(process.stdout.readline, b""):
                if cancel_event and cancel_event.is_set():
                    self.cancel()
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                lines.append(line)
                marker_match = _PLAYLIST_MARKER_RE.match(line.strip())
                if marker_match:
                    emit_playlist_progress(
                        int(marker_match.group("current")),
                        int(marker_match.group("total")),
                        marker_match.group("title") or "",
                    )
                    self.logger.info(
                        "yt-dlp 列表进度: %d/%d",
                        playlist_current,
                        playlist_total,
                    )
                    continue
                if line.strip().startswith(_PLAYLIST_MARKER):
                    # 内部结构化标记不显示给用户；缺失字段时再依赖自然语言进度。
                    continue
                safe_line = _safe_line(line)
                self.logger.info("yt-dlp: %s", safe_line)
                self._emit(safe_line, on_line)
                playlist_match = _PLAYLIST_PROGRESS_RE.search(line)
                if playlist_match:
                    emit_playlist_progress(
                        int(playlist_match.group("current")),
                        int(playlist_match.group("total")),
                    )
                match = _PROGRESS_RE.search(line)
                if match and on_progress:
                    try:
                        on_progress(float(match.group(1)))
                    except (TypeError, ValueError):
                        pass
                if "ERROR:" in line.upper():
                    error_lines.append(safe_line)
                candidate = self._path_from_line(line)
                if candidate:
                    printed_paths.append(candidate)
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.cancel()
            if is_douyin_url(url) and not (cancel_event and cancel_event.is_set()):
                return self._douyin_fallback(url, on_line, on_progress, cancel_event)
            raise DownloadError("下载进程停止超时，已终止。")
        except OSError as exc:
            if is_douyin_url(url) and not (cancel_event and cancel_event.is_set()):
                return self._douyin_fallback(url, on_line, on_progress, cancel_event)
            safe_error = _safe_line(str(exc))
            raise DownloadError(f"启动下载核心失败：{safe_error}") from exc
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        if cancel_event and cancel_event.is_set():
            raise DownloadError("下载已取消。")
        if return_code == 0:
            paths = self._find_results(
                printed_paths,
                started_at,
                before_snapshot,
                download_dir,
            )
            if paths:
                path = paths[-1]
                if on_progress:
                    on_progress(100.0)
                if selected_playlist_mode == "all":
                    final_total = playlist_total or expected_count or len(paths)
                    emit_playlist_progress(final_total, final_total, playlist_title)
                self.logger.info("下载完成，共定位到 %d 个文件，最后文件: %s", len(paths), path)
                return DownloadResult(
                    path=path,
                    used_douyin_fallback=False,
                    paths=paths,
                )
            # yt-dlp 某些插件不会输出 after_move 路径，抖音仍可尝试浏览器兜底。
            if is_douyin_url(url) and not (cancel_event and cancel_event.is_set()):
                return self._douyin_fallback(url, on_line, on_progress, cancel_event)
            raise DownloadError("下载命令已结束，但没有找到输出文件。")

        reason = _safe_line(error_lines[-1] if error_lines else "；".join(lines[-3:]))
        if is_douyin_url(url):
            return self._douyin_fallback(url, on_line, on_progress, cancel_event)
        raise DownloadError(f"下载失败：{reason or '下载核心返回错误'}")

    def _douyin_fallback(
        self,
        url: str,
        on_line: LineCallback | None,
        on_progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> DownloadResult:
        if cancel_event and cancel_event.is_set():
            raise DownloadError("下载已取消。")
        self._emit("yt-dlp 处理抖音链接失败，正在启用浏览器抓流兜底……", on_line)
        self.logger.info("浏览器抓流兜底开始: %s", safe_url_for_log(url))
        try:
            result = DouyinCapture().capture(
                url,
                on_line=on_line,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self.logger.info("浏览器抓流兜底完成: %s", result.path)
            return DownloadResult(
                path=result.path,
                title=result.title,
                used_douyin_fallback=True,
            )
        except Exception as exc:
            safe_error = _safe_line(str(exc))
            self.logger.error("浏览器抓流兜底失败：%s", safe_error)
            raise DownloadError(f"抖音下载失败：{safe_error}") from exc

    def _emit(self, message: str, callback: LineCallback | None) -> None:
        target = callback or self.log_callback
        if target:
            target(message)

    @staticmethod
    def _path_from_line(line: str) -> Path | None:
        value = line.strip().strip('"')
        if not re.match(r"^[A-Za-z]:[\\/]", value):
            return None
        candidate = Path(value)
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    @staticmethod
    def _inspection_count(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    @classmethod
    def _extract_formats(cls, payload: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(payload, Mapping):
            return ()
        direct = payload.get("formats")
        if isinstance(direct, list):
            return tuple(item for item in direct if isinstance(item, Mapping))
        entries = payload.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                extracted = cls._extract_formats(entry)
                if extracted:
                    return extracted
        return ()

    @staticmethod
    def _inspection_text(value: Any, *, limit: int) -> str:
        text = " ".join(str(value or "").replace("\x00", "").split())
        return text[: max(0, int(limit))]

    @staticmethod
    def _snapshot_download_dir(
        download_dir: Path | None = None,
    ) -> dict[Path, tuple[int, int]]:
        directory = download_dir or get_download_dir()
        try:
            return {
                item: (item.stat().st_mtime_ns, item.stat().st_size)
                for item in directory.iterdir()
                if item.is_file()
            }
        except OSError:
            return {}

    @classmethod
    def _find_results(
        cls,
        paths: Iterable[Path],
        started_at: float,
        before_snapshot: dict[Path, tuple[int, int]] | None = None,
        download_dir: Path | None = None,
    ) -> tuple[Path, ...]:
        results: list[Path] = []
        seen: set[Path] = set()

        def append_if_valid(path: Path) -> None:
            candidate = Path(path)
            if candidate in seen:
                return
            try:
                if not candidate.is_file() or candidate.stat().st_size <= 0:
                    return
            except OSError:
                return
            seen.add(candidate)
            results.append(candidate)

        for path in paths:
            append_if_valid(Path(path))
        if results:
            return tuple(results)
        try:
            directory = download_dir or get_download_dir()
            candidates = [
                item
                for item in directory.iterdir()
                if item.is_file()
                and not item.name.endswith((".part", ".ytdl", ".temp"))
                and item.stat().st_mtime >= started_at - 2
                and item.stat().st_size > 0
                and (
                    before_snapshot is None
                    or before_snapshot.get(item)
                    != (item.stat().st_mtime_ns, item.stat().st_size)
                )
            ]
            for candidate in sorted(candidates, key=lambda item: item.stat().st_mtime):
                append_if_valid(candidate)
        except OSError:
            pass
        return tuple(results)

    @classmethod
    def _find_result(
        cls,
        paths: Iterable[Path],
        started_at: float,
        before_snapshot: dict[Path, tuple[int, int]] | None = None,
        download_dir: Path | None = None,
    ) -> Path | None:
        results = cls._find_results(
            paths,
            started_at,
            before_snapshot,
            download_dir,
        )
        return results[-1] if results else None
