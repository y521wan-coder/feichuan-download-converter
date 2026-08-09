"""验证领域模型、SQLite 多文件状态和敏感字段隔离。"""

from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.models import (
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
    WorkItem,
)
from feichuan_downloader.state_store import StateStore, default_state_path


def main() -> None:
    item = WorkItem(
        platform=Platform.DOUYIN,
        work_id="aweme-1001",
        content_type=ContentKind.IMAGE,
        title="夏日图文",
        author="测试作者",
        published_at=date(2026, 7, 18),
        canonical_url="https://www.douyin.com/note/aweme-1001?token=URL_SECRET",
    )
    incomplete = ScanResult(
        source=SourceKind.DOUYIN_PROFILE,
        author=item.author,
        reported_count=20,
        unique_count=1,
        content_counts={ContentKind.IMAGE: 1},
        enumeration_complete=False,
        items=(item,),
        incomplete_reason="cursor did not advance",
    )
    assert incomplete.allowed_download_modes == ()
    assert incomplete.incomplete_action is IncompleteScanAction.DISCOVERED_ONLY
    assert incomplete.confirmation_choices == (IncompleteScanAction.DISCOVERED_ONLY,)

    complete = ScanResult(
        source=SourceKind.DOUYIN_COLLECTION,
        author=item.author,
        reported_count=1,
        unique_count=1,
        content_counts={ContentKind.IMAGE: 1},
        enumeration_complete=True,
        items=(item,),
    )
    assert complete.allowed_download_modes == tuple(DownloadMode)
    assert complete.incomplete_action is None
    DownloadEvent(
        stage=DownloadStage.DOWNLOADING,
        scanned_count=1,
        current=1,
        total=2,
        succeeded=0,
        skipped=0,
        failed=0,
        current_file="D:/sample.webp",
        overall_percent=50,
    )

    descriptor = MediaDescriptor(
        quality="1080P",
        media_url="https://media.example.test/video.mp4?token=MEDIA_SECRET",
        width=1920,
        height=1080,
        codec="h264",
        container="mp4",
        headers={"Cookie": "HEADER_SECRET"},
        cookie="COOKIE_SECRET",
        token="TOKEN_SECRET",
    )
    rendered = repr(descriptor)
    for secret in (
        "MEDIA_SECRET",
        "HEADER_SECRET",
        "COOKIE_SECRET",
        "TOKEN_SECRET",
    ):
        assert secret not in rendered
        assert secret not in json.dumps(descriptor.to_public_dict())
    try:
        pickle.dumps(descriptor)
    except TypeError:
        pass
    else:
        raise AssertionError("MediaDescriptor unexpectedly allowed pickle serialization")
    try:
        json.dumps(descriptor)
    except TypeError:
        pass
    else:
        raise AssertionError("MediaDescriptor unexpectedly allowed JSON serialization")
    descriptor.clear_sensitive()
    assert descriptor.cleared
    assert not descriptor.media_url and not descriptor.headers

    with tempfile.TemporaryDirectory(prefix="feichuan-state-smoke-") as directory:
        temp_root = Path(directory)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(temp_root)}, clear=False):
            assert default_state_path() == (
                temp_root / "FeichuanDownloader" / "state.sqlite3"
            )

        database = temp_root / "state.sqlite3"
        first_file = temp_root / "image-001.webp"
        first_file.write_bytes(b"webp-smoke")
        second_file = temp_root / "image-002.webp"
        failure = (
            "download failed at "
            "https://media.example.test/a.webp?token=URL_SECRET "
            "Cookie=COOKIE_SECRET DecodeKey:DECODE_SECRET token=TOKEN_SECRET"
        )

        with StateStore(database) as store:
            first_work_id = store.upsert_work(item, SourceKind.DOUYIN_PROFILE)
            second_work_id = store.upsert_work(item, SourceKind.DOUYIN_COLLECTION)
            assert first_work_id == second_work_id
            store.record_artifact(
                item.platform,
                item.work_id,
                "image:001",
                first_file,
                ValidationStatus.VALID,
                file_size=first_file.stat().st_size,
            )
            store.record_artifact(
                item.platform,
                item.work_id,
                "image:002",
                second_file,
                ValidationStatus.FAILED,
                failure_reason=failure,
            )
            store.record_artifact(
                item.platform,
                item.work_id,
                "image:004",
                temp_root / "previously-unsupported.webp",
                ValidationStatus.SKIPPED,
                failure_reason="上次未取得媒体信息",
            )

            works = store.list_works()
            artifacts = store.list_artifacts(item.platform, item.work_id)
            assert len(works) == 1
            assert len(artifacts) == 3
            assert works[0].source_kind is SourceKind.DOUYIN_COLLECTION
            assert works[0].item.canonical_url == "https://www.douyin.com/note/aweme-1001"
            failed = store.get_artifact(item.platform, item.work_id, "image:002")
            assert failed and failed.validation_status is ValidationStatus.FAILED
            assert "<redacted>" in failed.failure_reason
            for secret in ("URL_SECRET", "COOKIE_SECRET", "DECODE_SECRET", "TOKEN_SECRET"):
                assert secret not in failed.failure_reason

            assert not store.should_download(
                item.platform, item.work_id, "image:001", DownloadMode.INCREMENTAL
            )
            assert store.should_download(
                item.platform, item.work_id, "image:002", DownloadMode.RETRY_FAILED
            )
            assert not store.should_download(
                item.platform, item.work_id, "image:003", DownloadMode.RETRY_FAILED
            )
            assert store.should_download(
                item.platform, item.work_id, "image:003", DownloadMode.INCREMENTAL
            )
            assert store.should_download(
                item.platform, item.work_id, "image:001", DownloadMode.REDOWNLOAD_ALL
            )
            assert store.should_download(
                item.platform,
                item.work_id,
                "image:001",
                DownloadMode.INCREMENTAL,
                expected_path=temp_root / "new-folder" / "image-001.webp",
            )
            assert store.should_download(
                item.platform, item.work_id, "image:004", DownloadMode.INCREMENTAL
            )
            assert not store.should_download(
                item.platform, item.work_id, "image:004", DownloadMode.RETRY_FAILED
            )

            first_file.unlink()
            assert store.mark_missing_files(platform=Platform.DOUYIN) == 1
            missing = store.get_artifact(item.platform, item.work_id, "image:001")
            assert missing and missing.validation_status is ValidationStatus.MISSING

        with closing(sqlite3.connect(database)) as connection:
            work_count = connection.execute("SELECT COUNT(*) FROM works").fetchone()[0]
            artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
            }
        assert work_count == 1 and artifact_count == 3
        assert {"work_database_id", "file_path", "validation_status", "failure_reason"} <= columns
        assert not {"media_url", "cookie", "token", "decode_key"}.intersection(columns)

        database_bytes = database.read_bytes()
        for secret in (
            b"URL_SECRET",
            b"COOKIE_SECRET",
            b"DECODE_SECRET",
            b"TOKEN_SECRET",
        ):
            assert secret not in database_bytes

        with StateStore(database) as reopened:
            assert len(reopened.list_artifacts(item.platform, item.work_id)) == 3

    print("state store smoke passed")


if __name__ == "__main__":
    main()
