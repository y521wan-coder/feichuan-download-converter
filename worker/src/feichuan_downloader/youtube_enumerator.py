"""Offline-safe YouTube playlist and channel enumeration through yt-dlp.

The enumerator deliberately asks yt-dlp for flat, one-JSON-object-per-line
metadata and never downloads media.  Only stable YouTube video IDs and public
metadata are retained.  URLs printed by yt-dlp are neither trusted nor copied
into :class:`~feichuan_downloader.models.WorkItem`; canonical watch URLs are
rebuilt from the validated ID instead.

The module does not write logs.  Text forwarded to ``on_line`` is scrubbed so
that URL queries, cookies, authorization values, and token-like fields cannot
leak into a GUI log maintained by the caller.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TextIO
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from .config import YTDLP_PATH
from .models import ContentKind, Platform, ScanResult, SourceKind, WorkItem


ProgressCallback = Callable[[int, str], None]
LineCallback = Callable[[str], None]

_URL_RE = re.compile(r"https?://[^\s<>\]\[)'\"]+", re.IGNORECASE)
_TRAILING_SHARE_PUNCTUATION = ".,!?;:，。！？；：)]}>》】」』'\""
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,128}$")
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|signature|sig|key)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_SENSITIVE_TAIL_RE = re.compile(
    r"(?i)\b(cookie|authorization|decode[_-]?key)(\s*[:=]\s*).*$"
)
_MAX_INPUT_URL_LENGTH = 4096
_MAX_JSON_LINE_LENGTH = 2 * 1024 * 1024
_MAX_REPORTED_COUNT = 10_000_000
_READER_POLL_SECONDS = 0.05
_CANCEL_DRAIN_SECONDS = 2.0


class _ProcessLike(Protocol):
    stdout: BinaryIO | TextIO | None
    stderr: BinaryIO | TextIO | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Runner = Callable[[Sequence[str]], _ProcessLike]


@dataclass(frozen=True, slots=True, repr=False)
class YouTubeTarget:
    """A validated YouTube batch source with a canonical, query-minimized URL."""

    source: SourceKind
    url: str = field(repr=False)

    def __repr__(self) -> str:
        parts = urlsplit(self.url)
        safe_location = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return (
            "YouTubeTarget("
            f"source={self.source.value!r}, url={safe_location!r}, query=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class YouTubeScanBundle:
    """A coordinator-friendly scan result plus non-sensitive process metadata.

    ``url`` is the canonical batch source that a coordinator can copy into a
    ``PreparedScan``.  It is excluded from ``repr`` because playlist IDs live
    in the query component.  There are no media URLs, cookies, or credentials
    in this object.
    """

    result: ScanResult
    url: str = field(repr=False)
    exit_code: int | None = None
    cancelled: bool = False
    rejected_lines: int = 0

    def __repr__(self) -> str:
        return (
            "YouTubeScanBundle("
            f"source={self.result.source.value!r}, works={self.result.unique_count}, "
            f"complete={self.result.enumeration_complete!r}, "
            f"exit_code={self.exit_code!r}, cancelled={self.cancelled!r}, "
            f"rejected_lines={self.rejected_lines})"
        )

    __str__ = __repr__


@dataclass(slots=True)
class _EnumerationState:
    target: YouTubeTarget
    items: dict[str, WorkItem] = field(default_factory=dict)
    author: str = ""
    source_title: str = ""
    reported_count: int | None = None
    rejected_lines: int = 0
    reader_errors: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _StreamMessage:
    kind: str
    value: str = ""


def _source_member(name: str) -> SourceKind:
    """Resolve enum members added by the 0.2.0 integration layer lazily."""

    member = getattr(SourceKind, name, None)
    if member is None:
        raise RuntimeError(f"SourceKind.{name} 尚未在共享模型中注册。")
    return member


def _youtube_platform() -> Platform:
    member = getattr(Platform, "YOUTUBE", None)
    if member is None:
        raise RuntimeError("Platform.YOUTUBE 尚未在共享模型中注册。")
    return member


def _normalize_source(source: SourceKind | str) -> SourceKind:
    raw_value = source.value if isinstance(source, SourceKind) else str(source or "")
    raw_value = raw_value.strip().lower()
    aliases = {
        "playlist": "YOUTUBE_PLAYLIST",
        "youtube_playlist": "YOUTUBE_PLAYLIST",
        "channel": "YOUTUBE_CHANNEL",
        "youtube_channel": "YOUTUBE_CHANNEL",
    }
    member_name = aliases.get(raw_value)
    if member_name is None:
        raise ValueError("YouTube 批量扫描仅支持 playlist 或 channel 来源。")
    return _source_member(member_name)


def _is_youtube_host(host: str) -> bool:
    normalized = str(host or "").lower().rstrip(".")
    return normalized == "youtube.com" or normalized.endswith(".youtube.com")


def extract_youtube_url(value: str) -> str:
    """Extract the first ordinary YouTube HTTP(S) URL from share text."""

    text = str(value or "")
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(_TRAILING_SHARE_PUNCTUATION)
        if len(candidate) > _MAX_INPUT_URL_LENGTH:
            continue
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            continue
        if (
            parts.scheme.lower() in {"http", "https"}
            and _is_youtube_host(parts.hostname or "")
            and not parts.username
            and not parts.password
            and port in {None, 80, 443}
        ):
            return candidate
    raise ValueError("分享文本中没有找到有效的 YouTube 链接。")


def identify_youtube_target(
    value: str,
    *,
    source: SourceKind | str,
) -> YouTubeTarget:
    """Validate and canonicalize a playlist or channel batch source.

    A ``watch`` URL remains a single-video task even when it contains a
    ``list`` query parameter.  The caller must pass an explicit ``/playlist``
    URL to request playlist enumeration.
    """

    normalized_source = _normalize_source(source)
    original = extract_youtube_url(value)
    parts = urlsplit(original)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("YouTube 链接端口无效。") from exc
    if parts.username or parts.password or port not in {None, 80, 443}:
        raise ValueError("YouTube 链接不能包含凭据或自定义端口。")

    playlist_source = _source_member("YOUTUBE_PLAYLIST")
    if normalized_source is playlist_source:
        canonical = _normalize_playlist_url(parts)
    else:
        canonical = _normalize_channel_url(parts)
    return YouTubeTarget(normalized_source, canonical)


def _normalize_playlist_url(parts: Any) -> str:
    path = unquote(parts.path or "")
    if path.rstrip("/").lower() != "/playlist":
        raise ValueError("播放列表批量扫描需要 YouTube /playlist 分享链接。")
    if parts.query.count("&") > 50:
        raise ValueError("YouTube 播放列表链接参数过多。")
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False)
    list_values = query.get("list", [])
    if len(list_values) != 1:
        raise ValueError("YouTube 播放列表链接必须且只能包含一个 list 参数。")
    playlist_id = list_values[0]
    if not _PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise ValueError("YouTube 播放列表 ID 无效。")
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _normalize_channel_url(parts: Any) -> str:
    raw_path = parts.path or ""
    if len(raw_path) > 1024:
        raise ValueError("YouTube 频道链接路径过长。")
    raw_segments = [segment for segment in raw_path.split("/") if segment]
    if not raw_segments:
        raise ValueError("YouTube 频道链接缺少频道标识。")
    try:
        segments = [unicodedata.normalize("NFC", unquote(item)) for item in raw_segments]
    except (UnicodeError, ValueError) as exc:
        raise ValueError("YouTube 频道链接编码无效。") from exc
    if any(
        not segment
        or "/" in segment
        or "\\" in segment
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in segment)
        for segment in segments
    ):
        raise ValueError("YouTube 频道标识包含无效字符。")

    first = segments[0]
    base_segments: list[str]
    if first.startswith("@"):
        handle = first[1:]
        if not 1 <= len(handle) <= 100:
            raise ValueError("YouTube 频道 handle 无效。")
        base_segments = [first]
        remaining = segments[1:]
    elif first.lower() in {"channel", "c", "user"}:
        if len(segments) < 2 or not _CHANNEL_ID_RE.fullmatch(segments[1]):
            raise ValueError("YouTube 频道 ID 无效。")
        base_segments = [first.lower(), segments[1]]
        remaining = segments[2:]
    else:
        raise ValueError("链接不是受支持的 YouTube 频道主页。")

    allowed_tabs = {
        "videos",
        "shorts",
        "streams",
        "featured",
        "playlists",
        "community",
        "about",
        "releases",
        "podcasts",
    }
    if len(remaining) > 1 or (remaining and remaining[0].lower() not in allowed_tabs):
        raise ValueError("YouTube 频道链接包含不受支持的子路径。")

    # Always scan the channel root.  This avoids interpreting a copied
    # /playlists or /about tab as the requested collection of channel videos.
    encoded_path = "/".join(quote(segment, safe="@._~-") for segment in base_segments)
    return f"https://www.youtube.com/{encoded_path}"


class YouTubeEnumerator:
    """Stream flat YouTube entries from yt-dlp without downloading media."""

    def __init__(
        self,
        yt_dlp_path: str | Path = YTDLP_PATH,
        *,
        runner: Runner | None = None,
        wait_timeout: float = 10.0,
    ) -> None:
        if wait_timeout <= 0:
            raise ValueError("wait_timeout 必须大于 0。")
        self.yt_dlp_path = Path(yt_dlp_path)
        self.runner = runner or self._default_runner
        self.wait_timeout = float(wait_timeout)
        self._lock = threading.RLock()
        self._active_process: _ProcessLike | None = None
        self._scanning = False
        self._cancel_requested = threading.Event()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._scanning

    def cancel(self) -> None:
        """Request cancellation and terminate the active yt-dlp process."""

        with self._lock:
            if not self._scanning:
                return
            self._cancel_requested.set()
            process = self._active_process
        if process is not None:
            self._request_terminate(process)

    def scan(
        self,
        value: str,
        *,
        source: SourceKind | str,
        cancel_event: Any = None,
        on_progress: ProgressCallback | None = None,
        on_line: LineCallback | None = None,
    ) -> YouTubeScanBundle:
        """Enumerate one playlist or channel and retain partial error results.

        Invalid input is rejected with ``ValueError``.  Once yt-dlp has
        started, process failures, malformed JSON, and cancellation return an
        incomplete bundle so every already discovered item remains available
        for an explicit "download discovered content" confirmation.
        """

        target = identify_youtube_target(value, source=source)
        state = _EnumerationState(target)
        self._begin_scan()
        try:
            if self._is_cancelled(cancel_event):
                return self._build_bundle(
                    state,
                    exit_code=None,
                    cancelled=True,
                    process_error="任务在扫描开始前已取消。",
                )

            command = self._build_command(target.url)
            self._emit_line(
                "正在扫描 YouTube 播放列表…"
                if target.source is _source_member("YOUTUBE_PLAYLIST")
                else "正在扫描 YouTube 频道…",
                on_line,
            )
            try:
                process = self.runner(command)
            except OSError as exc:
                return self._build_bundle(
                    state,
                    exit_code=None,
                    cancelled=False,
                    process_error=f"无法启动 yt-dlp：{_safe_diagnostic(str(exc))}",
                )

            with self._lock:
                self._active_process = process
            exit_code, cancelled, process_error = self._consume_process(
                process,
                state,
                cancel_event=cancel_event,
                on_progress=on_progress,
                on_line=on_line,
            )
            return self._build_bundle(
                state,
                exit_code=exit_code,
                cancelled=cancelled,
                process_error=process_error,
            )
        finally:
            self._end_scan()

    def _build_command(self, url: str) -> list[str]:
        return [
            str(self.yt_dlp_path),
            "--ignore-config",
            "--flat-playlist",
            "--lazy-playlist",
            "--dump-json",
            "--skip-download",
            "--simulate",
            "--no-progress",
            "--no-colors",
            "--encoding",
            "utf-8",
            "--yes-playlist",
            url,
        ]

    @staticmethod
    def _default_runner(command: Sequence[str]) -> _ProcessLike:
        return subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _consume_process(
        self,
        process: _ProcessLike,
        state: _EnumerationState,
        *,
        cancel_event: Any,
        on_progress: ProgressCallback | None,
        on_line: LineCallback | None,
    ) -> tuple[int | None, bool, str]:
        messages: queue.Queue[_StreamMessage] = queue.Queue()
        reader_threads = [
            threading.Thread(
                target=self._read_stream,
                args=(process.stdout, "stdout", messages),
                daemon=True,
                name="feichuan-youtube-stdout",
            ),
            threading.Thread(
                target=self._read_stream,
                args=(process.stderr, "stderr", messages),
                daemon=True,
                name="feichuan-youtube-stderr",
            ),
        ]
        for thread in reader_threads:
            thread.start()

        ended: set[str] = set()
        cancelled = False
        terminate_requested_at: float | None = None
        process_error = ""
        while len(ended) < 2:
            if self._is_cancelled(cancel_event):
                cancelled = True
                if terminate_requested_at is None:
                    terminate_requested_at = time.monotonic()
                    self._request_terminate(process)
                elif time.monotonic() - terminate_requested_at >= _CANCEL_DRAIN_SECONDS:
                    self._request_kill(process)
                    break

            try:
                message = messages.get(timeout=_READER_POLL_SECONDS)
            except queue.Empty:
                if all(not thread.is_alive() for thread in reader_threads):
                    break
                continue

            if message.kind == "stdout":
                self._consume_stdout_line(message.value, state, on_progress)
            elif message.kind == "stderr":
                safe_line = _safe_diagnostic(message.value)
                if safe_line:
                    state.stderr_lines.append(safe_line)
                    del state.stderr_lines[:-5]
                    self._emit_line(f"yt-dlp：{safe_line}", on_line)
            elif message.kind == "reader_error":
                safe_error = _safe_diagnostic(message.value)
                state.reader_errors.append(safe_error or "读取 yt-dlp 输出失败")
            elif message.kind.startswith("eof:"):
                ended.add(message.kind.partition(":")[2])

        for thread in reader_threads:
            thread.join(timeout=0.2)

        try:
            exit_code = process.wait(timeout=self.wait_timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            self._request_kill(process)
            process_error = "yt-dlp 扫描进程停止超时，已强制终止。"
            try:
                exit_code = process.wait(timeout=2.0)
            except Exception:
                exit_code = None
        except OSError as exc:
            exit_code = None
            process_error = f"读取 yt-dlp 退出状态失败：{_safe_diagnostic(str(exc))}"

        return exit_code, cancelled or self._is_cancelled(cancel_event), process_error

    @staticmethod
    def _read_stream(
        stream: BinaryIO | TextIO | None,
        kind: str,
        messages: queue.Queue[_StreamMessage],
    ) -> None:
        if stream is None:
            messages.put(_StreamMessage(f"eof:{kind}"))
            return
        try:
            while True:
                raw = stream.readline()
                if raw in {b"", ""}:
                    break
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace")
                else:
                    line = str(raw)
                messages.put(_StreamMessage(kind, line.rstrip("\r\n")))
        except Exception as exc:
            messages.put(_StreamMessage("reader_error", str(exc)))
        finally:
            messages.put(_StreamMessage(f"eof:{kind}"))

    def _consume_stdout_line(
        self,
        line: str,
        state: _EnumerationState,
        on_progress: ProgressCallback | None,
    ) -> None:
        value = line.lstrip("\ufeff").strip()
        if not value:
            return
        if len(value) > _MAX_JSON_LINE_LENGTH:
            state.rejected_lines += 1
            return
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            state.rejected_lines += 1
            return
        if not isinstance(payload, Mapping):
            state.rejected_lines += 1
            return

        item = self._parse_item(payload)
        if item is None:
            state.rejected_lines += 1
            return
        self._update_source_metadata(payload, state)
        if item.work_id in state.items:
            return
        state.items[item.work_id] = item
        self._emit_progress(
            len(state.items),
            f"已发现 {len(state.items)} 个唯一视频。",
            on_progress,
        )

    @staticmethod
    def _update_source_metadata(
        payload: Mapping[str, Any],
        state: _EnumerationState,
    ) -> None:
        if not state.author:
            for field_name in (
                "channel",
                "uploader",
                "playlist_uploader",
                "playlist_channel",
            ):
                author = _clean_text(payload.get(field_name), max_length=200)
                if author:
                    state.author = author
                    break
        if not state.source_title:
            for field_name in ("playlist_title", "playlist", "title", "channel"):
                title = _clean_text(payload.get(field_name), max_length=200)
                if title:
                    state.source_title = title
                    break
        for field_name in ("playlist_count", "n_entries"):
            count = payload.get(field_name)
            if (
                isinstance(count, int)
                and not isinstance(count, bool)
                and 0 <= count <= _MAX_REPORTED_COUNT
            ):
                if state.reported_count is None or count > state.reported_count:
                    state.reported_count = count

    @staticmethod
    def _parse_item(payload: Mapping[str, Any]) -> WorkItem | None:
        entry_type = payload.get("_type")
        if entry_type is not None and entry_type not in {"url", "video"}:
            return None
        work_id = payload.get("id")
        if not isinstance(work_id, str):
            return None
        work_id = work_id.strip()
        if not _VIDEO_ID_RE.fullmatch(work_id):
            return None

        title = _clean_text(payload.get("title"), max_length=500)
        if not title:
            title = f"YouTube视频_{work_id}"
        author = ""
        for field_name in ("channel", "uploader", "playlist_uploader"):
            author = _clean_text(payload.get(field_name), max_length=200)
            if author:
                break
        published_at = _publication_date(payload)
        canonical_url = f"https://www.youtube.com/watch?v={quote(work_id, safe='_-')}"
        return WorkItem(
            platform=_youtube_platform(),
            work_id=work_id,
            content_type=ContentKind.VIDEO,
            title=title,
            author=author,
            published_at=published_at,
            canonical_url=canonical_url,
        )

    def _build_bundle(
        self,
        state: _EnumerationState,
        *,
        exit_code: int | None,
        cancelled: bool,
        process_error: str,
    ) -> YouTubeScanBundle:
        complete = (
            exit_code == 0
            and not cancelled
            and not process_error
            and not state.reader_errors
            and state.rejected_lines == 0
        )
        reason = "" if complete else self._incomplete_reason(
            state,
            exit_code=exit_code,
            cancelled=cancelled,
            process_error=process_error,
        )
        items = tuple(state.items.values())
        counts = Counter(item.content_type.value for item in items)
        result = ScanResult(
            source=state.target.source,
            author=state.author,
            reported_count=state.reported_count,
            unique_count=len(items),
            content_counts=dict(counts),
            enumeration_complete=complete,
            items=items,
            incomplete_reason=reason,
            source_title=state.source_title,
        )
        return YouTubeScanBundle(
            result=result,
            url=state.target.url,
            exit_code=exit_code,
            cancelled=cancelled,
            rejected_lines=state.rejected_lines,
        )

    @staticmethod
    def _incomplete_reason(
        state: _EnumerationState,
        *,
        exit_code: int | None,
        cancelled: bool,
        process_error: str,
    ) -> str:
        if cancelled:
            return f"任务已取消；保留已发现的 {len(state.items)} 个视频。"
        reasons: list[str] = []
        if process_error:
            reasons.append(_safe_diagnostic(process_error))
        if state.reader_errors:
            reasons.append(state.reader_errors[-1])
        if state.rejected_lines:
            reasons.append(
                f"yt-dlp 输出中有 {state.rejected_lines} 条无效记录，无法确认枚举完整"
            )
        if exit_code not in {0, None}:
            detail = state.stderr_lines[-1] if state.stderr_lines else "yt-dlp 返回错误"
            reasons.append(f"yt-dlp 退出码 {exit_code}：{detail}")
        elif exit_code is None and not reasons:
            reasons.append("yt-dlp 未返回可确认的成功退出状态")
        return "；".join(reason.rstrip("。；") for reason in reasons if reason) + "。"

    def _begin_scan(self) -> None:
        with self._lock:
            if self._scanning:
                raise RuntimeError("YouTube 扫描器已有任务正在运行。")
            self._cancel_requested.clear()
            self._active_process = None
            self._scanning = True

    def _end_scan(self) -> None:
        with self._lock:
            self._active_process = None
            self._scanning = False
            self._cancel_requested.clear()

    def _is_cancelled(self, cancel_event: Any) -> bool:
        if self._cancel_requested.is_set():
            return True
        if cancel_event is None:
            return False
        try:
            return bool(cancel_event.is_set())
        except Exception:
            return False

    @staticmethod
    def _request_terminate(process: _ProcessLike) -> None:
        try:
            if process.poll() is None:
                process.terminate()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _request_kill(process: _ProcessLike) -> None:
        try:
            if process.poll() is None:
                process.kill()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _emit_progress(count: int, message: str, callback: ProgressCallback | None) -> None:
        if callback is None:
            return
        try:
            callback(count, message)
        except Exception:
            pass

    @staticmethod
    def _emit_line(message: str, callback: LineCallback | None) -> None:
        if callback is None:
            return
        try:
            callback(_safe_diagnostic(message))
        except Exception:
            pass


def _clean_text(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value)
    normalized = " ".join(normalized.split())
    normalized = "".join(
        char for char in normalized if not unicodedata.category(char).startswith("C")
    )
    return normalized[:max_length].strip()


def _publication_date(payload: Mapping[str, Any]) -> date | None:
    upload_date = payload.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").date()
        except ValueError:
            pass
    for field_name in ("timestamp", "release_timestamp"):
        raw_timestamp = payload.get(field_name)
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)):
            continue
        try:
            parsed = datetime.fromtimestamp(float(raw_timestamp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if 2005 <= parsed.year <= 2200:
            return parsed.date()
    return None


def _safe_diagnostic(value: str) -> str:
    """Remove URL queries and common secret assignments from callback text."""

    text = str(value or "").replace("\x00", " ")

    def replace_url(match: re.Match[str]) -> str:
        raw_url = match.group(0).rstrip(_TRAILING_SHARE_PUNCTUATION)
        trailing = match.group(0)[len(raw_url) :]
        try:
            parts = urlsplit(raw_url)
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            safe = urlunsplit((parts.scheme, host, parts.path, "", ""))
        except (TypeError, ValueError):
            safe = "<redacted-url>"
        return safe + trailing

    text = _URL_RE.sub(replace_url, text)
    text = _SENSITIVE_TAIL_RE.sub(r"\1\2<redacted>", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = " ".join(text.split())
    return text[:1000]


__all__ = [
    "YouTubeEnumerator",
    "YouTubeScanBundle",
    "YouTubeTarget",
    "extract_youtube_url",
    "identify_youtube_target",
]
