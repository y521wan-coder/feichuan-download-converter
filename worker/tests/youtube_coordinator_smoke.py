"""YouTube batch URLs must scan and wait for confirmation before downloading."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import DownloadCoordinator, PreparedScan
from feichuan_downloader.models import (
    ContentKind,
    DownloadMode,
    Platform,
    ScanResult,
    SourceKind,
    WorkItem,
)
from feichuan_downloader.state_store import StateStore


class FakeYoutubeScanner:
    def __init__(self, items: tuple[WorkItem, ...]) -> None:
        self.items = items
        self.calls = 0

    def scan(self, _url: str, *, source: SourceKind, on_progress=None, **_kwargs):
        self.calls += 1
        if on_progress:
            on_progress(len(self.items), f"已发现 {len(self.items)} 个唯一视频。")
        return ScanResult(
            source=source,
            author="playlist owner",
            reported_count=len(self.items),
            unique_count=len(self.items),
            content_counts={ContentKind.VIDEO: len(self.items)},
            enumeration_complete=True,
            items=self.items,
        )


@dataclass(frozen=True)
class FakeResult:
    media_paths: tuple[Path, ...]
    skipped: bool = False

    @property
    def path(self) -> Path:
        return self.media_paths[0]

    @property
    def files(self) -> tuple[Path, ...]:
        return self.media_paths


class FakeYoutubeDownloader:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls: list[str] = []

    def download(self, item: WorkItem, _media=(), **_kwargs) -> FakeResult:
        self.calls.append(item.work_id)
        path = self.directory / f"{item.work_id}.mp4"
        path.write_bytes(b"fake validated youtube media")
        return FakeResult((path,))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-youtube-coordinator-") as temp:
        directory = Path(temp)
        items = tuple(
            WorkItem(
                platform=Platform.YOUTUBE,
                work_id=f"video-{index}",
                content_type=ContentKind.VIDEO,
                title=f"Video {index}",
                author="channel",
                published_at=date(2026, 7, 18),
                canonical_url=f"https://www.youtube.com/watch?v=video-{index}",
            )
            for index in range(1, 4)
        )
        scanner = FakeYoutubeScanner(items)
        backend = FakeYoutubeDownloader(directory)
        coordinator = DownloadCoordinator(
            youtube_scanner_factory=lambda: scanner,
            youtube_downloader_factory=lambda: backend,
            state_store_factory=lambda: StateStore(directory / "state.sqlite3"),
        )

        prepared = coordinator.scan_or_download(
            "https://www.youtube.com/playlist?list=PL_TEST"
        )
        assert isinstance(prepared, PreparedScan)
        assert scanner.calls == 1
        assert backend.calls == []  # confirmation is a hard boundary
        preview = {item.choice: item for item in coordinator.preview_prepared(prepared)}
        assert preview[DownloadMode.INCREMENTAL].download_count == 3
        assert preview[DownloadMode.INCREMENTAL].skip_count == 0

        summary = coordinator.download_prepared(prepared, DownloadMode.INCREMENTAL)
        assert (summary.succeeded, summary.skipped, summary.failed) == (3, 0, 0)
        assert backend.calls == ["video-1", "video-2", "video-3"]

    print("youtube coordinator smoke passed")


if __name__ == "__main__":
    main()
