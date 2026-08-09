"""Confirmed YouTube batch-item downloads using the existing yt-dlp core."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .config import FFPROBE_PATH, sanitize_filename
from .downloader import DownloadError, Downloader
from .models import DownloadMode, Platform, WorkItem
from .quality import QualityPreference, coerce_quality_preference


@dataclass(frozen=True, slots=True)
class YoutubeDownloadResult:
    item: WorkItem
    media_paths: tuple[Path, ...]
    skipped: bool = False

    @property
    def path(self) -> Path:
        if not self.media_paths:
            raise DownloadError("YouTube 下载没有生成文件。")
        return self.media_paths[0]

    @property
    def files(self) -> tuple[Path, ...]:
        return self.media_paths


class YoutubeDownloader:
    """Download one already-enumerated item and validate it with ffprobe."""

    def __init__(
        self,
        *,
        download_dir: str | Path | None = None,
        quality_preference: QualityPreference | str | None = None,
    ) -> None:
        self._downloader: Downloader | None = None
        self._lock = threading.RLock()
        self.download_dir = Path(download_dir) if download_dir is not None else None
        self.quality_preference = coerce_quality_preference(quality_preference)

    def cancel(self) -> None:
        with self._lock:
            downloader = self._downloader
        if downloader:
            downloader.cancel()

    def download(
        self,
        item: WorkItem,
        _media: Iterable[object] = (),
        *,
        cookie_header: str = "",
        mode: DownloadMode | str = DownloadMode.INCREMENTAL,
        cancel_event: threading.Event | None = None,
        on_progress: object = None,
        quality_preference: QualityPreference | str | None = None,
    ) -> YoutubeDownloadResult:
        if not isinstance(item, WorkItem) or item.platform is not Platform.YOUTUBE:
            raise TypeError("item 必须是 YouTube WorkItem。")
        if cookie_header:
            raise DownloadError("YouTube 批量任务不接受跨进程 Cookie。")
        try:
            selected_mode = DownloadMode(mode)
        except (TypeError, ValueError) as exc:
            raise DownloadError("未知的 YouTube 下载模式。") from exc
        if cancel_event and cancel_event.is_set():
            raise DownloadError("下载已取消。")
        selected_quality = (
            coerce_quality_preference(quality_preference)
            if quality_preference is not None
            else self.quality_preference
        )

        downloader = Downloader()
        with self._lock:
            self._downloader = downloader
        try:
            result = downloader.download(
                item.canonical_url,
                on_progress=on_progress if callable(on_progress) else None,
                cancel_event=cancel_event,
                output_name_template=self._output_template(item),
                force_overwrites=selected_mode is DownloadMode.REDOWNLOAD_ALL,
                download_dir=self.download_dir,
                quality_preference=selected_quality,
            )
        finally:
            with self._lock:
                self._downloader = None

        self._validate(result.path)
        return YoutubeDownloadResult(item=item, media_paths=(result.path,))

    @staticmethod
    def _output_template(item: WorkItem) -> str:
        author = item.author or "未知作者"
        published = _date_token(item.published_at)
        prefix = sanitize_filename(
            f"YouTube_{author}_{published}_{item.work_id}",
            fallback=f"YouTube_{item.work_id}",
        )
        # yt-dlp treats percent signs as template syntax; escape user metadata.
        prefix = prefix.replace("%", "%%")
        return f"{prefix}_%(title)s.%(ext)s"

    @staticmethod
    def _validate(path: Path) -> None:
        if not path.is_file() or path.stat().st_size <= 0:
            raise DownloadError("YouTube 下载结果为空。")
        if not FFPROBE_PATH.is_file():
            raise DownloadError("找不到 ffprobe，无法校验 YouTube 下载结果。")
        try:
            completed = subprocess.run(
                [
                    str(FFPROBE_PATH),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DownloadError("ffprobe 校验 YouTube 文件失败。") from exc
        if completed.returncode != 0 or not completed.stdout.strip():
            raise DownloadError("YouTube 文件未通过 ffprobe 校验。")


def _date_token(value: date | datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    digits = "".join(character for character in text[:10] if character.isdigit())
    return digits[:8] if len(digits) >= 8 else "未知日期"
