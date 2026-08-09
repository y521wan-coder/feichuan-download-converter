"""Offline cancellation and cleanup regressions for DownloadCoordinator."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import (  # noqa: E402
    DownloadCoordinator,
    PreparedScan,
)
from feichuan_downloader.models import (  # noqa: E402
    DownloadMode,
    ScanResult,
    SourceKind,
)


class BlockingIdentifyScanner:
    def __init__(self) -> None:
        self.identify_started = threading.Event()
        self.identify_release = threading.Event()
        self.cancel_called = False

    def identify(self, _text: str) -> SimpleNamespace:
        self.identify_started.set()
        if not self.identify_release.wait(timeout=5):
            raise RuntimeError("identify fixture timed out")
        return SimpleNamespace(
            source=SourceKind.DOUYIN_PROFILE,
            url="https://www.douyin.com/user/cancel-fixture",
        )

    def scan(self, _url: str, *, cancel_event: threading.Event, **_kwargs: object) -> None:
        if not cancel_event.is_set():
            raise RuntimeError("cancel event was cleared after short-link identification")
        raise RuntimeError("任务已取消。")

    def cancel(self) -> None:
        self.cancel_called = True


class CloseFailStore:
    def close(self) -> None:
        raise RuntimeError("close fixture failure")


def empty_prepared() -> PreparedScan:
    return PreparedScan(
        url="https://www.douyin.com/user/empty",
        result=ScanResult(
            source=SourceKind.DOUYIN_PROFILE,
            author="",
            reported_count=0,
            unique_count=0,
            enumeration_complete=True,
        ),
    )


def check_cancel_during_short_link_identification() -> None:
    scanner = BlockingIdentifyScanner()
    coordinator = DownloadCoordinator(douyin_scanner_factory=lambda: scanner)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            coordinator.scan_or_download("https://v.douyin.com/cancel-fixture/")
        except BaseException as exc:  # captured for assertion in the main thread
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert scanner.identify_started.wait(timeout=2)
    assert coordinator.busy
    coordinator.cancel()
    assert scanner.cancel_called
    scanner.identify_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and "取消" in str(errors[0]), errors
    assert not coordinator.busy


def check_factory_and_close_failures_release_busy_state() -> None:
    prepared = empty_prepared()

    def fail_factory() -> None:
        raise RuntimeError("state factory fixture failure")

    factory_failure = DownloadCoordinator(state_store_factory=fail_factory)
    try:
        factory_failure.download_prepared(prepared, DownloadMode.INCREMENTAL)
    except RuntimeError as exc:
        assert "factory fixture" in str(exc)
    else:
        raise AssertionError("state factory failure unexpectedly succeeded")
    assert not factory_failure.busy

    close_failure = DownloadCoordinator(
        state_store_factory=CloseFailStore,
        douyin_downloader_factory=object,
    )
    try:
        close_failure.download_prepared(prepared, DownloadMode.INCREMENTAL)
    except RuntimeError as exc:
        assert "close fixture" in str(exc)
    else:
        raise AssertionError("state close failure unexpectedly succeeded")
    assert not close_failure.busy


def main() -> None:
    check_cancel_during_short_link_identification()
    check_factory_and_close_failures_release_busy_state()
    print("coordinator lifecycle smoke passed")


if __name__ == "__main__":
    main()
