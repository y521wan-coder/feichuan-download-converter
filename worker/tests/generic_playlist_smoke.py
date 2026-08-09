"""Offline checks for ordinary-site playlist inspection and confirmation routing."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import feichuan_downloader.downloader as downloader_module  # noqa: E402
from feichuan_downloader.coordinator import (  # noqa: E402
    CoordinatorError,
    DownloadCoordinator,
    PreparedGenericDownload,
)
from feichuan_downloader.downloader import (  # noqa: E402
    DownloadError,
    DownloadResult,
    Downloader,
    UrlInspection,
)
from feichuan_downloader.models import (  # noqa: E402
    DownloadStage,
    SourceKind,
)


class StaticProcess:
    """Popen-compatible process backed by static UTF-8 output."""

    def __init__(self, output: bytes, *, return_code: int = 0) -> None:
        self._output = output
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO()
        self.returncode = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        del timeout
        return self._output, b""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def __enter__(self) -> StaticProcess:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class PopenRecorder:
    def __init__(self, process: StaticProcess) -> None:
        self.process = process
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], **_kwargs: object) -> StaticProcess:
        self.commands.append([str(value) for value in command])
        return self.process


def playlist_payload(
    count: int,
    *,
    reported_count: object = None,
    preview_count: int | None = None,
) -> dict[str, object]:
    entry_count = count if preview_count is None else preview_count
    payload: dict[str, object] = {
        "_type": "playlist",
        "id": f"offline-{count}",
        "title": f"离线分类页 {count}",
        "webpage_url": "https://media.invalid/page?token=PAGE_SECRET",
        "entries": [
            {
                "id": f"video-{index}",
                "title": f"预览视频 {index}",
                "url": (
                    "https://media.invalid/videoplayback"
                    f"?token=MEDIA_SECRET_{index}"
                ),
            }
            for index in range(1, entry_count + 1)
        ],
    }
    if reported_count is not None:
        payload["playlist_count"] = reported_count
    return payload


def inspect_payload(
    payload: dict[str, object],
    *,
    url: str,
) -> tuple[UrlInspection, list[str], list[str]]:
    process = StaticProcess(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    runner = PopenRecorder(process)
    public_lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="feichuan-generic-inspect-") as temp:
        root = Path(temp)
        core = root / "yt-dlp.exe"
        core.write_bytes(b"offline fake executable")
        download_dir = root / "downloads"
        download_dir.mkdir()
        marker = download_dir / "must-remain-unchanged.txt"
        marker.write_text("unchanged", encoding="utf-8")
        before = {
            path.name: (path.stat().st_size, path.read_bytes())
            for path in download_dir.iterdir()
        }
        with patch.object(downloader_module, "YTDLP_PATH", core), patch.object(
            downloader_module.subprocess, "Popen", runner
        ), patch.object(
            downloader_module, "get_download_dir", return_value=download_dir
        ):
            result = Downloader(log_callback=public_lines.append).inspect_url(url)
        after = {
            path.name: (path.stat().st_size, path.read_bytes())
            for path in download_dir.iterdir()
        }
        assert after == before
    assert runner.commands
    return result, runner.commands[0], public_lines


def check_inspection_counts_and_read_only_command() -> None:
    cases = (
        (304, 304, 3),
        (48, None, 48),
        (421, None, 421),
    )
    for expected_count, reported_count, entry_count in cases:
        url = (
            f"https://example.test/category/{expected_count}"
            "?token=INPUT_SECRET"
        )
        inspection, command, lines = inspect_payload(
            playlist_payload(
                expected_count,
                reported_count=reported_count,
                preview_count=entry_count,
            ),
            url=url,
        )
        assert isinstance(inspection, UrlInspection)
        assert inspection.original_url == url
        assert inspection.is_playlist
        assert inspection.count == expected_count
        assert inspection.title == f"离线分类页 {expected_count}"
        assert inspection.entries_preview
        assert inspection.entries_preview[0] == "预览视频 1"
        assert len(inspection.entries_preview) <= 10
        assert "--flat-playlist" in command
        assert "--dump-single-json" in command
        assert "--skip-download" in command
        assert "-o" not in command
        assert "--progress" not in command
        assert command[-1] == url
        public_text = "\n".join(lines) + repr(inspection)
        assert "INPUT_SECRET" not in public_text
        assert "PAGE_SECRET" not in public_text
        assert "MEDIA_SECRET" not in public_text

    single_url = "https://example.test/watch/one?token=SINGLE_SECRET"
    single, command, lines = inspect_payload(
        {
            "id": "single-one",
            "title": "普通单视频",
            "webpage_url": single_url,
            "url": "https://media.invalid/video?token=MEDIA_SINGLE_SECRET",
        },
        url=single_url,
    )
    assert single.count == 1
    assert not single.is_playlist
    assert single.title == "普通单视频"
    assert command[-1] == single_url
    assert "SINGLE_SECRET" not in ("\n".join(lines) + repr(single))


class DownloadPopenRecorder(PopenRecorder):
    def __init__(self, output_dir: Path, *, total: int) -> None:
        self.output_dir = output_dir
        self.total = total
        self.paths = tuple(output_dir / f"offline-{index}.mp4" for index in range(1, 3))
        super().__init__(StaticProcess(b""))

    def __call__(self, command: Sequence[str], **kwargs: object) -> StaticProcess:
        for path in self.paths:
            path.write_bytes(b"offline generic media")
        output_lines = [
            f"[download] Downloading item 1 of {self.total}",
            "[download] 100.0% of 1.00MiB at 1.00MiB/s",
            str(self.paths[0]),
            f"[download] Downloading video 2 of {self.total}",
            "[download] 100.0% of 1.00MiB at 1.00MiB/s",
            str(self.paths[1]),
        ]
        self.process = StaticProcess(("\n".join(output_lines) + "\n").encode("utf-8"))
        return super().__call__(command, **kwargs)


def run_download_mode(
    mode: str,
    expected_count: int,
) -> tuple[list[str], DownloadResult, list[tuple[int, int, str]]]:
    with tempfile.TemporaryDirectory(prefix="feichuan-generic-download-") as temp:
        root = Path(temp)
        core = root / "yt-dlp.exe"
        core.write_bytes(b"offline fake executable")
        output_dir = root / "downloads"
        output_dir.mkdir()
        runner = DownloadPopenRecorder(output_dir, total=expected_count)
        item_progress: list[tuple[int, int, str]] = []
        with patch.object(downloader_module, "YTDLP_PATH", core), patch.object(
            downloader_module.subprocess, "Popen", runner
        ), patch.object(
            downloader_module, "get_download_dir", return_value=output_dir
        ):
            result = Downloader().download(
                "https://example.test/category/offline",
                playlist_mode=mode,
                expected_count=expected_count,
                on_playlist_progress=lambda current, total, title: item_progress.append(
                    (current, total, title)
                ),
            )
        assert runner.commands
        assert result.path.is_file()
        assert result.paths
        return runner.commands[0], result, item_progress


def check_download_modes_and_item_progress() -> None:
    single_command, _single_result, _single_progress = run_download_mode("single", 1)
    assert "--no-playlist" in single_command
    assert "--yes-playlist" not in single_command
    assert "-f" in single_command
    assert single_command[single_command.index("-f") + 1] == "bv*+ba/b"
    assert "-S" not in single_command
    assert "--merge-output-format" not in single_command

    all_command, all_result, all_progress = run_download_mode("all", 304)
    assert "--yes-playlist" in all_command
    assert "--no-playlist" not in all_command
    assert len(all_result.paths) == 2
    assert any(current == 1 and total == 304 for current, total, _title in all_progress)
    assert any(current == 2 and total == 304 for current, total, _title in all_progress)

    try:
        Downloader().download(
            "https://example.test/category/offline",
            playlist_mode="invalid",
        )
    except (DownloadError, ValueError):
        pass
    else:
        raise AssertionError("invalid playlist_mode unexpectedly started a download")


class FakeGenericDownloader:
    def __init__(
        self,
        directory: Path,
        inspection: UrlInspection | BaseException,
    ) -> None:
        self.directory = directory
        self.inspection = inspection
        self.inspect_calls: list[str] = []
        self.download_calls: list[dict[str, object]] = []
        self.cancelled = False

    def inspect_url(self, url: str, *, cancel_event=None, on_line=None) -> UrlInspection:
        del cancel_event, on_line
        self.inspect_calls.append(url)
        if isinstance(self.inspection, BaseException):
            raise self.inspection
        return self.inspection

    def download(
        self,
        url: str,
        on_line=None,
        on_progress=None,
        cancel_event=None,
        *,
        playlist_mode: str = "single",
        expected_count: int | None = None,
        on_playlist_progress=None,
        **_kwargs: object,
    ) -> DownloadResult:
        del on_line, cancel_event
        self.download_calls.append(
            {
                "url": url,
                "playlist_mode": playlist_mode,
                "expected_count": expected_count,
            }
        )
        total = max(1, int(expected_count or 1))
        if on_playlist_progress:
            on_playlist_progress(1, total, "第一条")
            if total > 1:
                on_playlist_progress(2, total, "第二条")
        if on_progress:
            on_progress(100.0)
        path = self.directory / f"generic-{len(self.download_calls)}.mp4"
        path.write_bytes(b"offline coordinator media")
        return DownloadResult(path=path, paths=(path,))

    def cancel(self) -> None:
        self.cancelled = True


def inspection(url: str, count: int) -> UrlInspection:
    return UrlInspection(
        original_url=url,
        is_playlist=count > 1,
        count=count,
        title=f"普通网站列表 {count}",
        entries_preview=("第一条", "第二条") if count > 1 else ("唯一视频",),
    )


def check_coordinator_confirmation_boundary_and_dynamic_counts() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-generic-coordinator-") as temp:
        directory = Path(temp)
        for count in (304, 48, 421):
            url = f"https://example.test/category/{count}"
            backend = FakeGenericDownloader(directory, inspection(url, count))
            events = []
            lines: list[str] = []
            coordinator = DownloadCoordinator(
                downloader_factory=lambda backend=backend: backend,
                on_event=events.append,
                on_line=lines.append,
            )
            prepared = coordinator.scan_or_download(url)
            assert isinstance(prepared, PreparedGenericDownload)
            assert prepared.url == url
            assert prepared.inspection is not None
            assert prepared.inspection.count == count
            assert not prepared.inspection_error
            assert backend.inspect_calls == [url]
            assert backend.download_calls == []  # confirmation is a hard boundary
            assert any(str(count) in line for line in lines)

            if count == 304:
                result = coordinator.download_generic(prepared, playlist_mode="all")
                assert isinstance(result, DownloadResult)
                assert backend.download_calls[-1]["playlist_mode"] == "all"
                assert backend.download_calls[-1]["expected_count"] == 304
                assert any(event.total == 304 for event in events)
                assert any(event.current == 2 and event.total == 304 for event in events)
                assert any(event.stage is DownloadStage.COMPLETED for event in events)
                assert any("下载全部" in line for line in lines)
            elif count == 48:
                coordinator.download_generic(prepared, playlist_mode="single")
                assert backend.download_calls[-1]["playlist_mode"] == "single"
                assert backend.download_calls[-1]["expected_count"] == 1
                assert any(event.total == 1 for event in events)

        single_url = "https://example.test/watch/one"
        single_backend = FakeGenericDownloader(
            directory,
            inspection(single_url, 1),
        )
        single_coordinator = DownloadCoordinator(
            downloader_factory=lambda: single_backend,
        )
        single_result = single_coordinator.scan_or_download(single_url)
        assert isinstance(single_result, DownloadResult)
        assert single_backend.inspect_calls == [single_url]
        assert len(single_backend.download_calls) == 1
        assert single_backend.download_calls[0]["playlist_mode"] == "single"
        assert single_backend.download_calls[0]["expected_count"] == 1


def check_inspection_failure_never_auto_downloads() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-generic-failure-") as temp:
        backend = FakeGenericDownloader(
            Path(temp),
            DownloadError("无法确认数量：离线预检失败"),
        )
        coordinator = DownloadCoordinator(downloader_factory=lambda: backend)
        prepared = coordinator.scan_or_download("https://example.test/category/failure")
        assert isinstance(prepared, PreparedGenericDownload)
        assert prepared.inspection is None
        assert "无法确认" in prepared.inspection_error
        assert backend.download_calls == []

        try:
            coordinator.download_generic(prepared, playlist_mode="all")
        except (CoordinatorError, DownloadError, ValueError):
            pass
        else:
            raise AssertionError("failed inspection unexpectedly allowed batch download")
        assert backend.download_calls == []

        coordinator.download_generic(prepared, playlist_mode="single")
        assert len(backend.download_calls) == 1
        assert backend.download_calls[0]["playlist_mode"] == "single"
        assert backend.download_calls[0]["expected_count"] == 1


class SingleDouyinScanner:
    def __init__(self, resolved_url: str | None = None) -> None:
        self.resolved_url = resolved_url

    def identify(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(source=SourceKind.SINGLE_LINK, url=self.resolved_url or text)

    def cancel(self) -> None:
        return None


def check_youtube_and_douyin_single_links_bypass_generic_inspection() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-generic-bypass-") as temp:
        directory = Path(temp)
        youtube_url = "https://www.youtube.com/watch?v=one&list=PL_TEST"
        youtube_backend = FakeGenericDownloader(
            directory,
            inspection(youtube_url, 304),
        )
        youtube = DownloadCoordinator(downloader_factory=lambda: youtube_backend)
        youtube_result = youtube.scan_or_download(youtube_url)
        assert isinstance(youtube_result, DownloadResult)
        assert youtube_backend.inspect_calls == []
        assert youtube_backend.download_calls[0]["playlist_mode"] == "single"

        douyin_url = "https://www.douyin.com/video/123456"
        douyin_backend = FakeGenericDownloader(
            directory,
            inspection(douyin_url, 421),
        )
        douyin = DownloadCoordinator(
            downloader_factory=lambda: douyin_backend,
            douyin_scanner_factory=SingleDouyinScanner,
        )
        douyin_result = douyin.scan_or_download(douyin_url)
        assert isinstance(douyin_result, DownloadResult)
        assert douyin_backend.inspect_calls == []
        assert douyin_backend.download_calls[0]["playlist_mode"] == "single"

        short_note = "https://v.douyin.com/offline-note/"
        resolved_note = "https://www.douyin.com/note/7665882913611394033"
        note_backend = FakeGenericDownloader(
            directory,
            inspection(resolved_note, 1),
        )
        note = DownloadCoordinator(
            downloader_factory=lambda: note_backend,
            douyin_scanner_factory=lambda: SingleDouyinScanner(resolved_note),
        )
        note_result = note.scan_or_download(short_note)
        assert isinstance(note_result, DownloadResult)
        assert note_backend.inspect_calls == []
        assert note_backend.download_calls[0]["url"] == resolved_note
        assert note_backend.download_calls[0]["playlist_mode"] == "single"


def main() -> None:
    check_inspection_counts_and_read_only_command()
    check_download_modes_and_item_progress()
    check_coordinator_confirmation_boundary_and_dynamic_counts()
    check_inspection_failure_never_auto_downloads()
    check_youtube_and_douyin_single_links_bypass_generic_inspection()
    print("generic playlist smoke passed")


if __name__ == "__main__":
    main()
