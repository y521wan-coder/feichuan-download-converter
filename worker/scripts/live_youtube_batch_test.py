"""Start one confirmed YouTube channel batch, then cancel on first progress."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import (  # noqa: E402
    DownloadCoordinator,
    PreparedScan,
)
from feichuan_downloader.models import (  # noqa: E402
    DownloadEvent,
    DownloadMode,
    DownloadStage,
    SourceKind,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--confirmed-all-public-videos", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirmed_all_public_videos:
        print("Refusing to start: explicit batch confirmation flag is missing.")
        return 2

    coordinator_holder: list[DownloadCoordinator] = []
    cancel_requested = False
    last_event: tuple[object, ...] | None = None

    def on_event(event: DownloadEvent) -> None:
        nonlocal cancel_requested, last_event
        snapshot = (
            event.stage,
            event.scanned_count,
            event.current,
            event.total,
            event.succeeded,
            event.skipped,
            event.failed,
            event.message,
        )
        if snapshot == last_event:
            return
        last_event = snapshot
        print(
            "EVENT",
            event.stage.value,
            f"scan={event.scanned_count}",
            f"item={event.current}/{event.total}",
            f"ok={event.succeeded}",
            f"skip={event.skipped}",
            f"fail={event.failed}",
            event.message,
            flush=True,
        )
        if (
            not cancel_requested
            and event.stage is DownloadStage.DOWNLOADING
            and event.current >= 1
            and event.message.startswith("正在下载第")
            and coordinator_holder
        ):
            cancel_requested = True
            print("TEST_CANCEL requesting cancellation after first progress", flush=True)
            coordinator_holder[0].cancel()

    coordinator = DownloadCoordinator(on_event=on_event)
    coordinator_holder.append(coordinator)
    prepared: PreparedScan | None = None
    try:
        outcome = coordinator.scan_or_download(args.url)
        if not isinstance(outcome, PreparedScan):
            print("The supplied URL did not produce a batch scan.")
            return 3
        prepared = outcome
        result = prepared.result
        print(
            "SCAN_RESULT",
            f"source={result.source.value}",
            f"author={result.author}",
            f"reported={result.reported_count}",
            f"unique={result.unique_count}",
            f"complete={result.enumeration_complete}",
            f"counts={dict(result.content_counts)}",
            f"reason={result.incomplete_reason}",
            flush=True,
        )
        if result.source is not SourceKind.YOUTUBE_CHANNEL:
            print("Refusing to download: URL was not classified as a YouTube channel.")
            return 4
        if not result.enumeration_complete or result.unique_count < 1:
            print("No media download started because channel enumeration is incomplete.")
            return 5

        preview = next(
            item
            for item in coordinator.preview_prepared(prepared)
            if item.choice is DownloadMode.INCREMENTAL
        )
        print(
            "CONFIRMED_PLAN",
            "mode=incremental",
            f"download={preview.download_count}",
            f"skip={preview.skip_count}",
            f"unsupported={preview.unsupported_count}",
            flush=True,
        )
        try:
            coordinator.download_prepared(prepared, DownloadMode.INCREMENTAL)
        except Exception as exc:
            if cancel_requested and "取消" in str(exc):
                print("EXPECTED_CANCEL batch stopped after first download progress", flush=True)
                return 0
            raise
        print("Unexpectedly completed without exercising cancellation.")
        return 6
    finally:
        if prepared is not None:
            prepared.clear_sensitive()


if __name__ == "__main__":
    raise SystemExit(main())
