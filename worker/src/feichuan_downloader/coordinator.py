"""Task routing and structured progress shared by the native GUI."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .config import LOG_DIR, get_download_dir, sanitize_filename
from .downloader import DirectLinkResult, DownloadResult, Downloader, UrlInspection
from .logging_utils import get_logger
from .models import (
    ContentKind,
    DownloadEvent,
    DownloadMode,
    DownloadStage,
    IncompleteScanAction,
    MediaDescriptor,
    Platform,
    ScanResult,
    SourceKind,
    ValidationStatus,
)
from .naming import douyin_image_filename, douyin_video_filename
from .quality import QualityPreference, coerce_quality_preference
from .state_store import StateStore


EventCallback = Callable[[DownloadEvent], None]
LineCallback = Callable[[str], None]

_URL_RE = re.compile(r"https?://[^\s<>\]）)】》\"']+", re.IGNORECASE)
_TRAILING_SHARE_PUNCTUATION = "，。；：、！？…"
_DOUYIN_PLAYLET_PATH_RE = re.compile(
    r"^/share/playlet/detail/\d+/?$",
    re.IGNORECASE,
)


class CoordinatorError(RuntimeError):
    """A user-facing task routing or lifecycle error."""


def extract_url(text: str) -> str:
    """Extract the first HTTP(S) URL from ordinary platform share text."""

    match = _URL_RE.search(str(text or ""))
    if not match:
        raise CoordinatorError("没有在输入内容中找到有效的 http 或 https 链接。")
    return match.group(0).rstrip(_TRAILING_SHARE_PUNCTUATION)


def classify_url(url: str) -> SourceKind:
    """Classify stable batch URL shapes without performing a network request."""

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise CoordinatorError("请输入有效的 http 或 https 下载链接。")
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/"
    if host in {"channels.weixin.qq.com", "finder.video.qq.com"}:
        raise CoordinatorError("此链接来源已停止支持。")
    is_douyin = (
        host == "douyin.com"
        or host.endswith(".douyin.com")
        or host.endswith(".iesdouyin.com")
    )
    if is_douyin:
        if _DOUYIN_PLAYLET_PATH_RE.fullmatch(parts.path):
            return SourceKind.DOUYIN_COLLECTION
        if path == "/collection" or path.startswith("/collection/"):
            return SourceKind.DOUYIN_COLLECTION
        if path == "/user" or path.startswith("/user/"):
            return SourceKind.DOUYIN_PROFILE
        return SourceKind.SINGLE_LINK

    is_youtube = host == "youtube.com" or host.endswith(".youtube.com")
    if is_youtube:
        query = parse_qs(parts.query, keep_blank_values=False)
        if path == "/playlist" and any(
            str(value).strip() for value in query.get("list", ())
        ):
            return SourceKind.YOUTUBE_PLAYLIST
        segments = [segment for segment in path.split("/") if segment]
        if segments and (
            segments[0].startswith("@")
            or (segments[0] in {"channel", "c", "user"} and len(segments) >= 2)
        ):
            return SourceKind.YOUTUBE_CHANNEL
    return SourceKind.SINGLE_LINK


@dataclass(slots=True)
class PreparedScan:
    """A scan result plus sensitive, memory-only media descriptors."""

    url: str = field(repr=False)
    result: ScanResult
    media_by_work_id: dict[str, tuple[MediaDescriptor, ...]] = field(default_factory=dict)
    cookie_header: str = field(default="", repr=False)
    backend_context: Any = field(default=None, repr=False)

    def clear_sensitive(self) -> None:
        for descriptors in self.media_by_work_id.values():
            for descriptor in descriptors:
                descriptor.clear_sensitive()
        self.media_by_work_id.clear()
        self.cookie_header = ""
        context = self.backend_context
        self.backend_context = None
        clear_sensitive = getattr(context, "clear_sensitive", None)
        if callable(clear_sensitive):
            try:
                clear_sensitive()
            except Exception:
                pass
        close = getattr(context, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


@dataclass(frozen=True, slots=True)
class PreparedGenericDownload:
    """A generic URL whose read-only inspection requires a user decision."""

    url: str = field(repr=False)
    inspection: UrlInspection | None = None
    inspection_error: str = ""

    def __post_init__(self) -> None:
        url = str(self.url or "").strip()
        if not url:
            raise ValueError("url 不能为空。")
        object.__setattr__(self, "url", url)
        object.__setattr__(
            self,
            "inspection_error",
            str(self.inspection_error or "").strip(),
        )
        if self.inspection is None and not self.inspection_error:
            raise ValueError("普通链接待确认结果必须包含扫描结果或失败原因。")


@dataclass(frozen=True, slots=True)
class BatchDownloadSummary:
    total: int
    succeeded: int
    skipped: int
    failed: int
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchPlanPreview:
    """Current queue size for one confirmation choice.

    Counts are expressed in works rather than output files.  They are a
    snapshot of the SQLite state and filesystem immediately after scanning;
    the downloader rechecks the same rules when the user confirms.
    """

    choice: DownloadMode | IncompleteScanAction
    download_count: int
    skip_count: int
    unsupported_count: int = 0


class DownloadCoordinator:
    """Route single links and batch sources through one cancellable lifecycle."""

    def __init__(
        self,
        *,
        on_event: EventCallback | None = None,
        on_line: LineCallback | None = None,
        douyin_scanner_factory: Callable[[], Any] | None = None,
        douyin_downloader_factory: Callable[[], Any] | None = None,
        youtube_scanner_factory: Callable[[], Any] | None = None,
        youtube_downloader_factory: Callable[[], Any] | None = None,
        downloader_factory: Callable[[], Any] | None = None,
        state_store_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.logger = get_logger(LOG_DIR)
        self.on_event = on_event
        self.on_line = on_line
        self._douyin_scanner_factory = douyin_scanner_factory
        self._douyin_downloader_factory = douyin_downloader_factory
        self._youtube_scanner_factory = youtube_scanner_factory
        self._youtube_downloader_factory = youtube_downloader_factory
        self._downloader_factory = downloader_factory
        self._state_store_factory = state_store_factory or StateStore
        self._cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._busy = False
        self._downloader: Downloader | None = None
        self._active_backend: Any = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @staticmethod
    def classify(text: str) -> tuple[SourceKind, str]:
        url = extract_url(text)
        return classify_url(url), url

    def scan_or_download(
        self,
        text: str,
        *,
        interactive_douyin_login: bool = False,
        quality_preference: QualityPreference | str | None = None,
        douyin_note_content: str = "audio_only",
    ) -> DownloadResult | PreparedScan | PreparedGenericDownload:
        source, url = self.classify(text)
        self._begin()
        try:
            scanner: Any = None
            if source is SourceKind.SINGLE_LINK and self._is_douyin_url(url):
                try:
                    scanner = self._new_douyin_scanner(
                        interactive_login=interactive_douyin_login
                    )
                    with self._lock:
                        self._active_backend = scanner
                    target = scanner.identify(text)
                    url = target.url
                    if target.source in {
                        SourceKind.DOUYIN_PROFILE,
                        SourceKind.DOUYIN_COLLECTION,
                    }:
                        source = target.source
                except Exception:
                    # A short-link resolution failure must not break the existing
                    # single-link path; yt-dlp/CDP still gets its normal chance.
                    scanner = None
            if source in {SourceKind.DOUYIN_PROFILE, SourceKind.DOUYIN_COLLECTION}:
                return self._scan_douyin(
                    url,
                    source,
                    scanner=scanner,
                    interactive_login=interactive_douyin_login,
                )
            if source in {SourceKind.YOUTUBE_PLAYLIST, SourceKind.YOUTUBE_CHANNEL}:
                return self._scan_youtube(url, source)
            if self._is_douyin_note_url(url):
                if douyin_note_content == "images_and_audio":
                    return self._download_single_douyin_note_bundle(
                        url,
                        scanner=scanner,
                        interactive_login=interactive_douyin_login,
                        quality_preference=quality_preference,
                    )
                return self._download_single(
                    url,
                    quality_preference=quality_preference,
                    force_audio_only=True,
                )
            if self._uses_existing_single_link_flow(url):
                return self._download_single(url, quality_preference=quality_preference)
            return self._inspect_or_download_generic(
                url,
                quality_preference=quality_preference,
            )
        finally:
            self._end()

    def resolve_direct_link(
        self,
        text: str,
        *,
        interactive_douyin_login: bool = False,
    ) -> DirectLinkResult:
        """Resolve exactly one video and keep its direct URL in worker memory."""

        source, url = self.classify(text)
        self._begin()
        try:
            if source is SourceKind.SINGLE_LINK and self._is_douyin_url(url):
                try:
                    scanner = self._new_douyin_scanner(
                        interactive_login=interactive_douyin_login
                    )
                    with self._lock:
                        self._active_backend = scanner
                    target = scanner.identify(text)
                    url = target.url
                    source = target.source
                except Exception:
                    # yt-dlp and the browser capture path can still resolve the
                    # original short link without exposing its redirect target.
                    pass
            if source is not SourceKind.SINGLE_LINK:
                raise CoordinatorError("获取解析直连只支持单视频，不支持主页、合集、频道或播放列表。")
            try:
                direct_path = urlsplit(url).path.lower().rstrip("/") + "/"
            except Exception:
                direct_path = ""
            if "/note/" in direct_path or "/live/" in direct_path:
                raise CoordinatorError("获取解析直连只支持单视频，不支持直播或图文。")

            downloader = self._new_downloader()
            with self._lock:
                self._downloader = downloader
            if not self._uses_existing_single_link_flow(url):
                inspection = downloader.inspect_url(
                    url,
                    cancel_event=self._cancel_event,
                    on_line=self.on_line,
                )
                if inspection.is_playlist or inspection.count != 1:
                    raise CoordinatorError("获取解析直连只支持一个视频，当前页面包含多个条目。")

            self._emit(
                DownloadEvent(
                    stage=DownloadStage.SCANNING,
                    message="正在解析单视频直连；不会下载媒体文件。",
                )
            )
            result = downloader.resolve_direct_link(
                url,
                cancel_event=self._cancel_event,
                on_line=self.on_line,
            )
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.COMPLETED,
                    current=1,
                    total=1,
                    succeeded=1,
                    overall_percent=100.0,
                    message="直连解析完成，正在写入系统剪贴板。",
                )
            )
            return result
        finally:
            self._end()

    def download_generic(
        self,
        prepared: PreparedGenericDownload,
        playlist_mode: str,
        quality_preference: QualityPreference | str | None = None,
    ) -> DownloadResult:
        """Download a generic URL after the GUI records an explicit choice."""

        if not isinstance(prepared, PreparedGenericDownload):
            raise TypeError("prepared 必须是 PreparedGenericDownload。")
        mode = str(playlist_mode or "").strip().lower()
        if mode not in {"single", "all"}:
            raise CoordinatorError("未知的普通网站列表下载选项。")
        if mode == "all":
            if prepared.inspection is None or prepared.inspection.count <= 1:
                raise CoordinatorError("当前链接没有已确认的多视频扫描结果。")
        label = "下载全部" if mode == "all" else "只下载第一个视频"
        self._log_line(f"用户选择：{label}。")

        expected_count = (
            prepared.inspection.count
            if mode == "all" and prepared.inspection is not None
            else 1
        )
        target_dir = (
            self._task_download_dir(
                prepared.inspection.title,
                fallback="普通网站列表",
            )
            if mode == "all" and prepared.inspection is not None
            else get_download_dir()
        )
        self._begin()
        try:
            downloader = self._new_downloader()
            with self._lock:
                self._downloader = downloader

            current_item = 0
            current_title = ""
            initial_message = (
                f"预计总数 {expected_count}，正在下载列表。"
                if mode == "all"
                else "正在下载第一个视频。"
            )
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.DOWNLOADING,
                    scanned_count=(
                        prepared.inspection.count if prepared.inspection else 0
                    ),
                    current=0,
                    total=expected_count,
                    message=initial_message,
                )
            )

            def playlist_progress(index: int, _reported_total: int, title: str) -> None:
                nonlocal current_item, current_title
                current_item = max(0, min(expected_count, int(index)))
                current_title = str(title or "").strip()
                if current_item == 0:
                    message = f"预计总数 {expected_count}，正在下载列表。"
                else:
                    message = f"正在下载列表第 {current_item}/{expected_count} 个视频。"
                self._emit(
                    DownloadEvent(
                        stage=DownloadStage.DOWNLOADING,
                        scanned_count=(
                            prepared.inspection.count if prepared.inspection else 0
                        ),
                        current=current_item,
                        total=expected_count,
                        succeeded=max(0, current_item - 1),
                        current_file=current_title,
                        overall_percent=(
                            max(0, current_item - 1) * 100.0 / expected_count
                            if expected_count
                            else None
                        ),
                        message=message,
                    )
                )

            def progress(value: float | None) -> None:
                current = current_item or 1
                overall = value
                if mode == "all" and value is not None and expected_count:
                    overall = ((current - 1) + value / 100.0) * 100.0 / expected_count
                self._emit(
                    DownloadEvent(
                        stage=DownloadStage.DOWNLOADING,
                        scanned_count=(
                            prepared.inspection.count if prepared.inspection else 0
                        ),
                        current=current,
                        total=expected_count,
                        succeeded=max(0, current - 1) if mode == "all" else 0,
                        current_file=current_title,
                        overall_percent=overall,
                        message=(
                            f"正在下载列表第 {current}/{expected_count} 个视频。"
                            if mode == "all"
                            else "正在下载第一个视频。"
                        ),
                    )
                )

            result = downloader.download(
                prepared.url,
                on_line=self.on_line,
                on_progress=progress,
                cancel_event=self._cancel_event,
                playlist_mode=mode,
                expected_count=expected_count,
                on_playlist_progress=playlist_progress,
                download_dir=target_dir,
                quality_preference=quality_preference,
            )
            result_paths = tuple(getattr(result, "paths", ()) or (result.path,))
            succeeded = 1 if mode == "single" else min(expected_count, len(result_paths))
            skipped = 0 if mode == "single" else max(0, expected_count - succeeded)
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.COMPLETED,
                    scanned_count=(
                        prepared.inspection.count if prepared.inspection else 0
                    ),
                    current=expected_count,
                    total=expected_count,
                    succeeded=succeeded,
                    skipped=skipped,
                    overall_percent=100.0,
                    current_file=result.path.name,
                    message=(
                        f"列表下载完成，共处理 {expected_count} 个视频。"
                        if mode == "all"
                        else "第一个视频下载完成。"
                    ),
                )
            )
            return result
        finally:
            self._end()
            if target_dir != get_download_dir():
                self._prune_empty_dir(target_dir)

    def preview_prepared(self, prepared: PreparedScan) -> tuple[BatchPlanPreview, ...]:
        """Return exact work counts currently implied by every allowed choice."""

        if not isinstance(prepared, PreparedScan):
            raise TypeError("prepared 必须是 PreparedScan。")
        choices = prepared.result.confirmation_choices
        counters = {choice: [0, 0, 0] for choice in choices}
        download_dir = self._batch_download_dir(prepared.result)
        store = self._state_store_factory()
        try:
            for item in prepared.result.items:
                descriptors = prepared.media_by_work_id.get(item.work_id, ())
                plans = self._artifact_plan(item, len(descriptors), download_dir)
                unsupported = bool(self._unsupported_reason(item, descriptors))
                for choice in choices:
                    counts = counters[choice]
                    if unsupported:
                        counts[1] += 1
                        counts[2] += 1
                        continue
                    mode = self._mode_for_choice(choice)
                    should_download = any(
                        store.should_download(
                            item.platform,
                            item.work_id,
                            artifact_key,
                            mode,
                            expected_path=path,
                        )
                        for artifact_key, path in plans
                    )
                    counts[0 if should_download else 1] += 1
        finally:
            store.close()
        return tuple(
            BatchPlanPreview(
                choice=choice,
                download_count=counters[choice][0],
                skip_count=counters[choice][1],
                unsupported_count=counters[choice][2],
            )
            for choice in choices
        )

    def download_prepared(
        self,
        prepared: PreparedScan,
        choice: DownloadMode | IncompleteScanAction | str,
        quality_preference: QualityPreference | str | None = None,
    ) -> BatchDownloadSummary:
        """Download a confirmed batch scan and persist non-sensitive state."""

        if not isinstance(prepared, PreparedScan):
            raise TypeError("prepared 必须是 PreparedScan。")
        normalized = normalize_confirmation_choice(prepared.result, choice)
        mode = self._mode_for_choice(normalized)
        selected_quality = coerce_quality_preference(quality_preference)
        download_dir = self._batch_download_dir(prepared.result)
        self._begin()
        store: Any = None
        try:
            store = self._state_store_factory()
            if prepared.result.source in {
                SourceKind.YOUTUBE_PLAYLIST,
                SourceKind.YOUTUBE_CHANNEL,
            }:
                if self._youtube_downloader_factory:
                    backend = self._youtube_downloader_factory()
                else:
                    from .youtube_downloader import YoutubeDownloader

                    backend = YoutubeDownloader(
                        download_dir=download_dir,
                        quality_preference=selected_quality,
                    )
            elif self._douyin_downloader_factory:
                backend = self._douyin_downloader_factory()
            else:
                from .douyin_downloader import DouyinDownloader

                backend = DouyinDownloader(
                    download_dir=download_dir,
                    quality_preference=selected_quality,
                )
            with self._lock:
                self._active_backend = backend
            items = prepared.result.items
            total = len(items)
            succeeded = skipped = failed = 0
            paths: list[Path] = []

            for index, item in enumerate(items, start=1):
                if self._cancel_event.is_set():
                    raise CoordinatorError("下载已取消。")
                descriptors = prepared.media_by_work_id.get(item.work_id, ())
                plans = self._artifact_plan(item, len(descriptors), download_dir)
                store.upsert_work(item, prepared.result.source)

                unsupported_reason = self._unsupported_reason(item, descriptors)
                if unsupported_reason:
                    skipped += 1
                    for key, path in plans:
                        store.record_artifact(
                            item.platform,
                            item.work_id,
                            key,
                            path,
                            ValidationStatus.SKIPPED,
                            failure_reason=unsupported_reason,
                        )
                    self._emit_batch_event(
                        prepared.result.unique_count,
                        index,
                        total,
                        succeeded,
                        skipped,
                        failed,
                        self._display_path(plans),
                        f"已跳过：{unsupported_reason}",
                    )
                    continue

                should_download = any(
                    store.should_download(
                        item.platform,
                        item.work_id,
                        artifact_key,
                        mode,
                        expected_path=path if path.suffix else None,
                    )
                    for artifact_key, path in plans
                )
                if not should_download:
                    skipped += 1
                    self._emit_batch_event(
                        prepared.result.unique_count,
                        index,
                        total,
                        succeeded,
                        skipped,
                        failed,
                        self._display_path(plans),
                        "作品已存在且状态有效，已跳过。",
                    )
                    continue

                current_file = self._display_path(plans)

                def item_progress(value: float | None) -> None:
                    overall = None
                    if value is not None and total:
                        overall = ((index - 1) + value / 100.0) * 100.0 / total
                    self._emit(
                        DownloadEvent(
                            stage=DownloadStage.DOWNLOADING,
                            scanned_count=prepared.result.unique_count,
                            current=index,
                            total=total,
                            succeeded=succeeded,
                            skipped=skipped,
                            failed=failed,
                            current_file=current_file,
                            overall_percent=overall,
                            message=f"正在下载第 {index}/{total} 个作品。",
                        )
                    )

                try:
                    result = backend.download(
                        item,
                        descriptors,
                        cookie_header=prepared.cookie_header,
                        mode=mode,
                        cancel_event=self._cancel_event,
                        on_progress=item_progress,
                        quality_preference=selected_quality,
                    )
                except Exception as exc:
                    if self._cancel_event.is_set() or "取消" in str(exc):
                        self._emit(
                            DownloadEvent(
                                stage=DownloadStage.CANCELLED,
                                scanned_count=prepared.result.unique_count,
                                current=index,
                                total=total,
                                succeeded=succeeded,
                                skipped=skipped,
                                failed=failed,
                                current_file=current_file,
                                message="下载已取消。",
                            )
                        )
                        raise CoordinatorError("下载已取消。") from exc
                    failed += 1
                    safe_reason = self._safe_failure(str(exc))
                    for key, path in plans:
                        store.record_artifact(
                            item.platform,
                            item.work_id,
                            key,
                            path,
                            ValidationStatus.FAILED,
                            failure_reason=safe_reason,
                        )
                    self._emit_batch_event(
                        prepared.result.unique_count,
                        index,
                        total,
                        succeeded,
                        skipped,
                        failed,
                        current_file,
                        f"作品下载失败：{safe_reason}",
                    )
                    continue

                result_files = tuple(result.files)
                if result.skipped:
                    skipped += 1
                else:
                    succeeded += 1
                for result_index, path in enumerate(result_files):
                    key = (
                        plans[result_index][0]
                        if result_index < len(plans)
                        else f"artifact:{result_index + 1:03d}"
                    )
                    store.record_artifact(
                        item.platform,
                        item.work_id,
                        key,
                        path,
                        ValidationStatus.VALID,
                        file_size=path.stat().st_size if path.is_file() else None,
                    )
                    paths.append(path)
                self._emit_batch_event(
                    prepared.result.unique_count,
                    index,
                    total,
                    succeeded,
                    skipped,
                    failed,
                    result.path.name,
                    "作品已校验并保存。" if not result.skipped else "现有文件校验有效，已跳过。",
                )

            self._emit(
                DownloadEvent(
                    stage=DownloadStage.COMPLETED,
                    scanned_count=prepared.result.unique_count,
                    current=total,
                    total=total,
                    succeeded=succeeded,
                    skipped=skipped,
                    failed=failed,
                    overall_percent=100.0,
                    message="批量队列处理完成。",
                )
            )
            return BatchDownloadSummary(
                total=total,
                succeeded=succeeded,
                skipped=skipped,
                failed=failed,
                paths=tuple(paths),
            )
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                self._end()
                if download_dir != get_download_dir():
                    self._prune_empty_dir(download_dir)

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._lock:
            downloader = self._downloader
            backend = self._active_backend
        if downloader:
            downloader.cancel()
        cancel = getattr(backend, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        self._emit(
            DownloadEvent(
                stage=DownloadStage.CANCELLED,
                message="正在取消任务并清理临时资源……",
            )
        )

    def _begin(self) -> None:
        with self._lock:
            if self._busy:
                raise CoordinatorError("已有任务正在运行。")
            self._busy = True
            self._cancel_event.clear()

    def _end(self) -> None:
        with self._lock:
            self._busy = False
            self._downloader = None
            self._active_backend = None

    def _inspect_or_download_generic(
        self,
        url: str,
        *,
        quality_preference: QualityPreference | str | None = None,
    ) -> DownloadResult | PreparedGenericDownload:
        downloader = self._new_downloader()
        with self._lock:
            self._downloader = downloader
        self._emit(
            DownloadEvent(
                stage=DownloadStage.SCANNING,
                message="正在扫描普通网站链接并确认视频数量……",
            )
        )
        try:
            inspection = downloader.inspect_url(
                url,
                cancel_event=self._cancel_event,
                on_line=self.on_line,
            )
        except Exception as exc:
            if self._cancel_event.is_set() or "取消" in str(exc):
                raise CoordinatorError("扫描已取消。") from exc
            safe_reason = self._safe_failure(str(exc)) or "下载核心未返回可用数量"
            message = f"无法确认视频数量：{safe_reason}"
            self.logger.warning("普通网站列表预检失败：%s", safe_reason)
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.AWAITING_CONFIRMATION,
                    message="无法确认数量，尚未开始下载，等待用户确认。",
                )
            )
            return PreparedGenericDownload(
                url=url,
                inspection_error=message,
            )

        if inspection.count > 1:
            message = f"检测到普通网站列表：预计 {inspection.count} 个视频。"
            self._log_line(message)
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.AWAITING_CONFIRMATION,
                    scanned_count=inspection.count,
                    total=inspection.count,
                    message=f"扫描完成，实际检测到 {inspection.count} 个视频，等待确认。",
                )
            )
            return PreparedGenericDownload(url=url, inspection=inspection)
        return self._download_single(
            url,
            downloader=downloader,
            quality_preference=quality_preference,
        )

    def _download_single(
        self,
        url: str,
        *,
        downloader: Any = None,
        quality_preference: QualityPreference | str | None = None,
        force_audio_only: bool = False,
    ) -> DownloadResult:
        downloader = downloader or self._new_downloader()
        with self._lock:
            self._downloader = downloader
        self._emit(
            DownloadEvent(
                stage=DownloadStage.DOWNLOADING,
                current=1,
                total=1,
                message="正在下载单个链接。",
            )
        )

        def progress(value: float | None) -> None:
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.DOWNLOADING,
                    current=1,
                    total=1,
                    overall_percent=value,
                    message="正在下载单个链接。",
                )
            )

        result = downloader.download(
            url,
            on_line=self.on_line,
            on_progress=progress,
            cancel_event=self._cancel_event,
            playlist_mode="single",
            expected_count=1,
            quality_preference=quality_preference,
            force_audio_only=force_audio_only,
        )
        self._emit(
            DownloadEvent(
                stage=DownloadStage.COMPLETED,
                current=1,
                total=1,
                succeeded=1,
                overall_percent=100.0,
                current_file=result.path.name,
                message="下载完成。",
            )
        )
        return result

    def _download_single_douyin_note_bundle(
        self,
        url: str,
        *,
        scanner: Any = None,
        interactive_login: bool = False,
        quality_preference: QualityPreference | str | None = None,
    ) -> DownloadResult:
        """Download one note's atomic image set and background audio independently."""

        selected_quality = coerce_quality_preference(quality_preference)
        image_paths: tuple[Path, ...] = ()
        audio_paths: tuple[Path, ...] = ()
        image_error = ""
        audio_error = ""
        audio_title = ""
        used_fallback = False
        bundle: Any = None

        self._emit(
            DownloadEvent(
                stage=DownloadStage.SCANNING,
                total=2,
                message="正在解析单条抖音图文原图。",
            )
        )
        try:
            scanner = scanner or self._new_douyin_scanner(
                interactive_login=interactive_login
            )
            with self._lock:
                self._active_backend = scanner
            bundle = scanner.scan_single_note(
                url,
                cancel_event=self._cancel_event,
                on_progress=lambda count, message="": self._emit(
                    DownloadEvent(
                        stage=DownloadStage.SCANNING,
                        scanned_count=max(0, int(count)),
                        total=2,
                        message=message or "正在解析单条抖音图文原图。",
                    )
                ),
            )
            if self._cancel_event.is_set():
                raise CoordinatorError("下载已取消。")
            item = next(
                (
                    value
                    for value in bundle.result.items
                    if value.content_type is ContentKind.IMAGE
                ),
                None,
            )
            if item is None:
                raise CoordinatorError("作品详情没有返回图文原图。")
            descriptors = tuple(bundle.media_by_work_id.get(item.work_id, ()))
            if self._douyin_downloader_factory:
                image_backend = self._douyin_downloader_factory()
            else:
                from .douyin_downloader import DouyinDownloader

                image_backend = DouyinDownloader(quality_preference=selected_quality)
            with self._lock:
                self._active_backend = image_backend
            image_result = image_backend.download(
                item,
                descriptors,
                cookie_header=bundle.cookie_header,
                cancel_event=self._cancel_event,
                on_progress=lambda value: self._emit(
                    DownloadEvent(
                        stage=DownloadStage.DOWNLOADING,
                        current=1,
                        total=2,
                        overall_percent=(value or 0.0) * 0.55,
                        message="正在下载图文原图。",
                    )
                ),
                quality_preference=selected_quality,
                include_description=False,
            )
            image_paths = tuple(image_result.media_paths)
        except Exception as exc:
            if self._cancel_event.is_set() or "取消" in str(exc):
                raise CoordinatorError("下载已取消。") from exc
            image_error = self._safe_failure(str(exc)) or "图文原图下载失败"
        finally:
            clear_sensitive = getattr(bundle, "clear_sensitive", None)
            if callable(clear_sensitive):
                clear_sensitive()

        try:
            audio_downloader = self._new_downloader()
            with self._lock:
                self._downloader = audio_downloader
                self._active_backend = audio_downloader
            audio_result = audio_downloader.download(
                url,
                on_line=self.on_line,
                on_progress=lambda value: self._emit(
                    DownloadEvent(
                        stage=DownloadStage.DOWNLOADING,
                        current=2,
                        total=2,
                        overall_percent=55.0 + (value or 0.0) * 0.45,
                        message="正在下载图文背景音频。",
                    )
                ),
                cancel_event=self._cancel_event,
                playlist_mode="single",
                expected_count=1,
                quality_preference=selected_quality,
                force_audio_only=True,
            )
            audio_paths = tuple(
                getattr(audio_result, "paths", ()) or (audio_result.path,)
            )
            audio_title = audio_result.title
            used_fallback = audio_result.used_douyin_fallback
        except Exception as exc:
            if self._cancel_event.is_set() or "取消" in str(exc):
                raise CoordinatorError("下载已取消。") from exc
            audio_error = self._safe_failure(str(exc)) or "图文背景音频下载失败"

        paths = tuple(dict.fromkeys((*image_paths, *audio_paths)))
        warnings = tuple(
            value
            for value in (
                f"图片：{image_error}" if image_error else "",
                f"音频：{audio_error}" if audio_error else "",
            )
            if value
        )
        if not paths:
            reason = "；".join(warnings) or "没有得到可用的图片或背景音频。"
            raise CoordinatorError(f"抖音图文下载失败：{reason}")

        partial_success = bool(warnings)
        self._emit(
            DownloadEvent(
                stage=DownloadStage.COMPLETED,
                current=2,
                total=2,
                succeeded=int(bool(image_paths)) + int(bool(audio_paths)),
                failed=len(warnings),
                overall_percent=100.0,
                current_file=paths[0].name,
                message="图文图片和音频部分成功。" if partial_success else "图文图片和音频下载完成。",
            )
        )
        return DownloadResult(
            path=paths[0],
            title=audio_title,
            used_douyin_fallback=used_fallback,
            paths=paths,
            partial_success=partial_success,
            warnings=warnings,
        )

    def _new_downloader(self) -> Any:
        if self._downloader_factory:
            return self._downloader_factory()
        return Downloader(log_callback=self.on_line)

    def _scan_douyin(
        self,
        url: str,
        source: SourceKind,
        *,
        scanner: Any = None,
        interactive_login: bool = False,
    ) -> PreparedScan:
        self._emit(
            DownloadEvent(
                stage=DownloadStage.SCANNING,
                message="正在完整扫描抖音作品列表……",
            )
        )
        scanner = scanner or self._new_douyin_scanner(
            interactive_login=interactive_login
        )
        with self._lock:
            self._active_backend = scanner

        def scanned(count: int, message: str = "") -> None:
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.SCANNING,
                    scanned_count=max(0, int(count)),
                    message=message or f"已发现 {count} 个唯一作品。",
                )
            )

        value = scanner.scan(
            url,
            source=source,
            cancel_event=self._cancel_event,
            on_progress=scanned,
        )
        if self._cancel_event.is_set() or bool(getattr(value, "cancelled", False)):
            raise CoordinatorError("扫描已取消。")
        prepared = self._coerce_prepared_scan(url, value, scanner)
        self._emit(
            DownloadEvent(
                stage=DownloadStage.AWAITING_CONFIRMATION,
                scanned_count=prepared.result.unique_count,
                total=prepared.result.unique_count,
                message=self._scan_complete_message(prepared.result),
            )
        )
        return prepared

    def _new_douyin_scanner(self, *, interactive_login: bool = False) -> Any:
        if self._douyin_scanner_factory:
            return self._douyin_scanner_factory()
        try:
            from .douyin_enumerator import DouyinEnumerator
        except ImportError as exc:
            raise CoordinatorError("抖音批量扫描模块尚未安装完整。") from exc
        if interactive_login:
            return DouyinEnumerator(
                interactive_login=True,
                scan_timeout=180,
                max_idle_rounds=24,
                login_timeout=900,
            )
        return DouyinEnumerator()

    def _scan_youtube(self, url: str, source: SourceKind) -> PreparedScan:
        self._emit(
            DownloadEvent(
                stage=DownloadStage.SCANNING,
                message="正在扫描 YouTube 批量列表并统计实际条目……",
            )
        )
        scanner = self._new_youtube_scanner()
        with self._lock:
            self._active_backend = scanner

        def scanned(count: int, message: str = "") -> None:
            self._emit(
                DownloadEvent(
                    stage=DownloadStage.SCANNING,
                    scanned_count=max(0, int(count)),
                    message=message or f"已发现 {count} 个唯一视频。",
                )
            )

        value = scanner.scan(
            url,
            source=source,
            cancel_event=self._cancel_event,
            on_progress=scanned,
        )
        if self._cancel_event.is_set() or bool(getattr(value, "cancelled", False)):
            raise CoordinatorError("扫描已取消。")
        prepared = self._coerce_prepared_scan(url, value, scanner)
        self._emit(
            DownloadEvent(
                stage=DownloadStage.AWAITING_CONFIRMATION,
                scanned_count=prepared.result.unique_count,
                total=prepared.result.unique_count,
                message=self._scan_complete_message(prepared.result),
            )
        )
        return prepared

    def _new_youtube_scanner(self) -> Any:
        if self._youtube_scanner_factory:
            return self._youtube_scanner_factory()
        try:
            from .youtube_enumerator import YouTubeEnumerator
        except ImportError as exc:
            raise CoordinatorError("YouTube 批量扫描模块尚未安装完整。") from exc
        return YouTubeEnumerator()

    @staticmethod
    def _coerce_prepared_scan(url: str, value: Any, scanner: Any) -> PreparedScan:
        if isinstance(value, PreparedScan):
            return value
        result = value if isinstance(value, ScanResult) else getattr(value, "result", None)
        if not isinstance(result, ScanResult):
            raise CoordinatorError("批量扫描器没有返回有效的 ScanResult。")
        raw_media = getattr(value, "media_by_work_id", None)
        if raw_media is None:
            raw_media = getattr(scanner, "media_by_work_id", {})
        media: dict[str, tuple[MediaDescriptor, ...]] = {}
        if isinstance(raw_media, Mapping):
            for work_id, descriptors in raw_media.items():
                media[str(work_id)] = tuple(
                    item for item in descriptors if isinstance(item, MediaDescriptor)
                )
        cookie_header = str(
            getattr(value, "cookie_header", getattr(scanner, "cookie_header", "")) or ""
        )
        return PreparedScan(
            url=url,
            result=result,
            media_by_work_id=media,
            cookie_header=cookie_header,
            backend_context=(
                getattr(value, "backend_context", None)
                or (value if callable(getattr(value, "clear_sensitive", None)) else None)
            ),
        )

    @staticmethod
    def _is_douyin_url(url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except Exception:
            return False
        return (
            host == "douyin.com"
            or host.endswith(".douyin.com")
            or host.endswith(".iesdouyin.com")
        )

    @classmethod
    def _is_douyin_note_url(cls, url: str) -> bool:
        if not cls._is_douyin_url(url):
            return False
        try:
            path = urlsplit(url).path.lower().rstrip("/") + "/"
        except Exception:
            return False
        return "/note/" in path

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except Exception:
            return False
        return (
            host == "youtu.be"
            or host.endswith(".youtu.be")
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com"
            or host.endswith(".youtube-nocookie.com")
        )

    @classmethod
    def _uses_existing_single_link_flow(cls, url: str) -> bool:
        return cls._is_douyin_url(url) or cls._is_youtube_url(url)

    @staticmethod
    def _task_download_dir(label: str, *, fallback: str) -> Path:
        # 不提前创建任务子文件夹，避免只预览/扫描就留下空目录；
        # 目录由各下载后端在真正写入文件时创建，任务结束后再清理空目录。
        base = get_download_dir()
        name = sanitize_filename(label, fallback=fallback)
        return base / name

    @staticmethod
    def _prune_empty_dir(path: Path) -> None:
        """下载任务结束后移除仍为空的子文件夹；非空目录一律保留。"""
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass

    @classmethod
    def _batch_download_dir(cls, result: ScanResult) -> Path:
        if result.source is SourceKind.DOUYIN_COLLECTION:
            return cls._task_download_dir(
                result.source_title or result.author,
                fallback="抖音合集",
            )
        if result.source is SourceKind.DOUYIN_PROFILE:
            return cls._task_download_dir(
                result.source_title or result.author,
                fallback="抖音主页",
            )
        if result.source is SourceKind.YOUTUBE_PLAYLIST:
            return cls._task_download_dir(
                result.source_title or result.author,
                fallback="YouTube播放列表",
            )
        if result.source is SourceKind.YOUTUBE_CHANNEL:
            return cls._task_download_dir(
                result.source_title or result.author,
                fallback="YouTube频道",
            )
        return get_download_dir()

    @staticmethod
    def _artifact_plan(
        item: Any,
        descriptor_count: int,
        download_dir: Path | None = None,
    ) -> tuple[tuple[str, Path], ...]:
        download_dir = download_dir or get_download_dir()
        if item.platform is Platform.YOUTUBE:
            safe_id = sanitize_filename(str(item.work_id), fallback="video")
            return (("video", download_dir / f"YouTube_{safe_id}"),)
        if item.platform is not Platform.DOUYIN:
            return (("artifact", download_dir / f"{item.work_id}.bin"),)
        if item.content_type is ContentKind.IMAGE:
            count = max(1, int(descriptor_count))
            images = [
                (
                    f"image:{index:03d}",
                    download_dir / douyin_image_filename(item, index),
                )
                for index in range(1, count + 1)
            ]
            first_name = douyin_image_filename(item, 1)
            suffix = "_001.webp"
            description_name = (
                first_name[: -len(suffix)] + "_说明.txt"
                if first_name.endswith(suffix)
                else first_name + ".txt"
            )
            return (*images, ("description", download_dir / description_name))
        return (("video", download_dir / douyin_video_filename(item)),)

    @staticmethod
    def _display_path(plans: tuple[tuple[str, Path], ...]) -> str:
        return plans[0][1].name if plans else ""

    @staticmethod
    def _unsupported_reason(item: Any, descriptors: tuple[MediaDescriptor, ...]) -> str:
        if item.content_type in {ContentKind.LIVE, ContentKind.UNKNOWN}:
            return "首版不支持直播或未知内容类型。"
        if (
            item.platform is Platform.DOUYIN
            and item.content_type is ContentKind.IMAGE
            and not descriptors
        ):
            return "本次只发现图文入口，未取得可校验的原图地址。"
        return ""

    @staticmethod
    def _mode_for_choice(
        choice: DownloadMode | IncompleteScanAction,
    ) -> DownloadMode:
        return choice if isinstance(choice, DownloadMode) else DownloadMode.INCREMENTAL

    def _emit_batch_event(
        self,
        scanned_count: int,
        current: int,
        total: int,
        succeeded: int,
        skipped: int,
        failed: int,
        current_file: str,
        message: str,
    ) -> None:
        overall = (current * 100.0 / total) if total else 100.0
        self._emit(
            DownloadEvent(
                stage=DownloadStage.DOWNLOADING,
                scanned_count=scanned_count,
                current=current,
                total=total,
                succeeded=succeeded,
                skipped=skipped,
                failed=failed,
                current_file=current_file,
                overall_percent=overall,
                message=message,
            )
        )

    @staticmethod
    def _safe_failure(message: str) -> str:
        value = re.sub(r"https?://[^\s\]>)'\"]+", "<redacted-url>", message)
        return re.sub(
            r"(?i)\b(cookie|token|decode[_-]?key|authorization)(\s*[:=]\s*)\S+",
            r"\1\2<redacted>",
            value,
        )[:1000]

    @staticmethod
    def _scan_complete_message(result: ScanResult) -> str:
        if result.enumeration_complete:
            return f"扫描完成，实际发现 {result.unique_count} 个唯一作品，等待确认。"
        reason = result.incomplete_reason or "页面未明确返回 has_more=false"
        return (
            f"扫描得到 {result.unique_count} 个唯一作品，但枚举不完整：{reason}。"
            "只能选择下载已发现内容或取消。"
        )

    def _emit(self, event: DownloadEvent) -> None:
        if self.on_event:
            self.on_event(event)

    def _log_line(self, message: str) -> None:
        self.logger.info("%s", message)
        if self.on_line:
            self.on_line(message)


def normalize_confirmation_choice(
    result: ScanResult,
    choice: DownloadMode | IncompleteScanAction | str,
) -> DownloadMode | IncompleteScanAction:
    """Reject UI choices that would overstate an incomplete enumeration."""

    try:
        normalized: DownloadMode | IncompleteScanAction = DownloadMode(choice)
    except (TypeError, ValueError):
        try:
            normalized = IncompleteScanAction(choice)
        except (TypeError, ValueError) as exc:
            raise CoordinatorError("未知的下载确认选项。") from exc
    if normalized not in result.confirmation_choices:
        raise CoordinatorError("当前扫描结果不允许该下载模式。")
    return normalized
