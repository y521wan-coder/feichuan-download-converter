"""Offline coordinator queue/state/progress integration check."""

from __future__ import annotations

import os
import tempfile
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import DownloadCoordinator, PreparedScan
from feichuan_downloader.douyin_downloader import DouyinDownloadResult
from feichuan_downloader.models import (
    ContentKind,
    DownloadMode,
    MediaDescriptor,
    Platform,
    ScanResult,
    SourceKind,
    ValidationStatus,
    WorkItem,
)
from feichuan_downloader.naming import douyin_video_filename
from feichuan_downloader.state_store import StateStore


class FakeDownloader:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls = 0

    def download(self, item: WorkItem, _media: object, **_kwargs: object) -> DouyinDownloadResult:
        self.calls += 1
        target = self.directory / douyin_video_filename(item)
        self.directory.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"validated-by-fake-backend")
        return DouyinDownloadResult(item=item, media_paths=(target,))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-coordinator-") as directory:
        root = Path(directory)
        os.environ["FEICHUAN_DOWNLOAD_DIR"] = str(root)
        database = root / "state.sqlite3"
        batch_dir = root / "author"
        backend = FakeDownloader(batch_dir)
        events = []
        items = tuple(
            WorkItem(
                platform=Platform.DOUYIN,
                work_id=f"work-{index}",
                content_type=ContentKind.VIDEO,
                title=f"title-{index}",
                author="author",
                published_at=date(2026, 7, 18),
                canonical_url=f"https://www.douyin.com/video/{index}",
            )
            for index in range(1, 3)
        )
        descriptors = {
            item.work_id: (
                MediaDescriptor(
                    quality="1080p",
                    media_url=f"https://media.invalid/{item.work_id}.mp4?token=secret",
                    codec="h264",
                    width=1920,
                    height=1080,
                ),
            )
            for item in items
        }
        prepared = PreparedScan(
            url="https://www.douyin.com/user/example",
            result=ScanResult(
                source=SourceKind.DOUYIN_PROFILE,
                author="author",
                reported_count=2,
                unique_count=2,
                content_counts={ContentKind.VIDEO: 2},
                enumeration_complete=True,
                items=items,
            ),
            media_by_work_id=descriptors,
            cookie_header="session=must-not-persist",
        )
        coordinator = DownloadCoordinator(
            on_event=events.append,
            douyin_downloader_factory=lambda: backend,
            state_store_factory=lambda: StateStore(database),
        )
        first = coordinator.download_prepared(prepared, DownloadMode.INCREMENTAL)
        assert (first.succeeded, first.skipped, first.failed) == (2, 0, 0)
        assert backend.calls == 2
        assert events and all(
            event.scanned_count == prepared.result.unique_count for event in events
        )

        previews = {preview.choice: preview for preview in coordinator.preview_prepared(prepared)}
        assert previews[DownloadMode.INCREMENTAL].download_count == 0
        assert previews[DownloadMode.INCREMENTAL].skip_count == 2
        assert previews[DownloadMode.REDOWNLOAD_ALL].download_count == 2
        assert previews[DownloadMode.RETRY_FAILED].download_count == 0

        second = coordinator.download_prepared(prepared, DownloadMode.INCREMENTAL)
        assert (second.succeeded, second.skipped, second.failed) == (0, 2, 0)
        assert backend.calls == 2
        assert events and events[-1].overall_percent == 100.0
        assert all(
            event.scanned_count == prepared.result.unique_count for event in events
        )

        with StateStore(database) as store:
            store.record_artifact(
                items[0].platform,
                items[0].work_id,
                "video",
                batch_dir / douyin_video_filename(items[0]),
                ValidationStatus.FAILED,
                failure_reason="offline fixture failure",
            )
        other_author = PreparedScan(
            url="https://www.douyin.com/user/other",
            result=ScanResult(
                source=SourceKind.DOUYIN_PROFILE,
                author="other-author",
                reported_count=1,
                unique_count=1,
                content_counts={ContentKind.VIDEO: 1},
                enumeration_complete=True,
                items=(items[0],),
            ),
            media_by_work_id=descriptors,
        )
        coordinator.preview_prepared(other_author)
        assert not (root / "other-author").exists(), "仅预览不应创建任务子文件夹"

        failed_previews = {
            preview.choice: preview for preview in coordinator.preview_prepared(prepared)
        }
        assert failed_previews[DownloadMode.INCREMENTAL].download_count == 1
        assert failed_previews[DownloadMode.RETRY_FAILED].download_count == 1
        assert failed_previews[DownloadMode.RETRY_FAILED].skip_count == 1

        dom_image = WorkItem(
            platform=Platform.DOUYIN,
            work_id="dom-note",
            content_type=ContentKind.IMAGE,
            title="DOM-only note",
            author="author",
            published_at=date(2026, 7, 18),
            canonical_url="https://www.douyin.com/note/123",
        )
        incomplete = PreparedScan(
            url="https://www.douyin.com/user/example",
            result=ScanResult(
                source=SourceKind.DOUYIN_PROFILE,
                author="author",
                reported_count=None,
                unique_count=1,
                content_counts={ContentKind.IMAGE: 1},
                enumeration_complete=False,
                items=(dom_image,),
                incomplete_reason="仅从 DOM 发现作品。",
            ),
        )
        incomplete_preview = coordinator.preview_prepared(incomplete)[0]
        assert incomplete_preview.download_count == 0
        assert incomplete_preview.skip_count == 1
        assert incomplete_preview.unsupported_count == 1

        from feichuan_downloader.coordinator import DownloadCoordinator as _DC
        empty_dir = root / "empty-task"
        empty_dir.mkdir(parents=True, exist_ok=True)
        non_empty_dir = root / "non-empty-task"
        non_empty_dir.mkdir(parents=True, exist_ok=True)
        (non_empty_dir / "keep.txt").write_text("x", encoding="utf-8")
        _DC._prune_empty_dir(empty_dir)
        _DC._prune_empty_dir(non_empty_dir)
        assert not empty_dir.exists()
        assert non_empty_dir.exists() and (non_empty_dir / "keep.txt").exists()

        database_bytes = database.read_bytes()
        assert b"must-not-persist" not in database_bytes
        assert b"media.invalid" not in database_bytes
        prepared.clear_sensitive()
        assert not prepared.cookie_header and not prepared.media_by_work_id
    print("coordinator batch smoke passed")


if __name__ == "__main__":
    main()
