"""Interactive end-to-end test for an explicitly authorized public profile.

The script never writes media before a complete scan has reached has_more=false.
Login happens only in a fresh temporary Chromium profile after the caller passes
the explicit confirmation flag.  Cookies and signed media URLs stay in memory.
"""

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
from feichuan_downloader.models import DownloadEvent, DownloadMode  # noqa: E402
from feichuan_downloader.models import DownloadStage  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--minimum", type=int, default=300)
    parser.add_argument(
        "--confirm-count",
        type=int,
        required=True,
        help="Exact unique count explicitly confirmed by the user.",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in DownloadMode],
        default=DownloadMode.INCREMENTAL.value,
    )
    parser.add_argument(
        "--confirmed-all-public-works",
        action="store_true",
        help="Required acknowledgement that the user confirmed this full batch.",
    )
    parser.add_argument(
        "--cancel-after-first-progress",
        action="store_true",
        help="Cancel as soon as the first item reports real download progress.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirmed_all_public_works:
        print("Refusing to start: explicit batch confirmation flag is missing.")
        return 2

    last_event: tuple[object, ...] | None = None
    coordinator_holder: list[DownloadCoordinator] = []
    cancel_requested = False

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
            args.cancel_after_first_progress
            and not cancel_requested
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
        outcome = coordinator.scan_or_download(
            args.url,
            interactive_douyin_login=True,
        )
        if not isinstance(outcome, PreparedScan):
            print("The supplied URL did not produce a batch scan.")
            return 3
        prepared = outcome
        result = prepared.result
        print(
            "SCAN_RESULT",
            f"author={result.author}",
            f"reported={result.reported_count}",
            f"unique={result.unique_count}",
            f"complete={result.enumeration_complete}",
            f"counts={dict(result.content_counts)}",
            f"reason={result.incomplete_reason}",
            flush=True,
        )
        if not result.enumeration_complete:
            print("No media download started because enumeration is incomplete.")
            return 4
        if result.unique_count < args.minimum:
            print(
                f"No media download started because only {result.unique_count} "
                f"works were found; minimum is {args.minimum}."
            )
            return 5
        if result.unique_count != args.confirm_count:
            print(
                "No media download started because the complete scan found "
                f"{result.unique_count} works, but the confirmed count is "
                f"{args.confirm_count}."
            )
            return 7

        mode = DownloadMode(args.mode)
        preview = next(
            item
            for item in coordinator.preview_prepared(prepared)
            if item.choice is mode
        )
        print(
            "CONFIRMED_PLAN",
            f"mode={mode.value}",
            f"download={preview.download_count}",
            f"skip={preview.skip_count}",
            f"unsupported={preview.unsupported_count}",
            flush=True,
        )
        try:
            summary = coordinator.download_prepared(prepared, mode)
        except Exception as exc:
            if args.cancel_after_first_progress and cancel_requested and "取消" in str(exc):
                print("EXPECTED_CANCEL batch stopped after first download progress", flush=True)
                return 0
            raise
        print(
            "DOWNLOAD_RESULT",
            f"total={summary.total}",
            f"success={summary.succeeded}",
            f"skip={summary.skipped}",
            f"failed={summary.failed}",
            f"files={len(summary.paths)}",
            flush=True,
        )
        return 0 if summary.failed == 0 else 6
    finally:
        if prepared is not None:
            prepared.clear_sensitive()


if __name__ == "__main__":
    raise SystemExit(main())
