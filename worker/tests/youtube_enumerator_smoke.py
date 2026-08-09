"""Offline smoke coverage for flat YouTube playlist/channel enumeration."""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.models import (  # noqa: E402
    ContentKind,
    Platform,
    SourceKind,
)
from feichuan_downloader.youtube_enumerator import (  # noqa: E402
    YouTubeEnumerator,
    extract_youtube_url,
    identify_youtube_target,
)


def json_line(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


class StaticProcess:
    def __init__(
        self,
        stdout_lines: Sequence[bytes],
        *,
        stderr_lines: Sequence[bytes] = (),
        return_code: int = 0,
    ) -> None:
        self.stdout = io.BytesIO(b"".join(stdout_lines))
        self.stderr = io.BytesIO(b"".join(stderr_lines))
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9


class RecordingRunner:
    def __init__(self, process: StaticProcess) -> None:
        self.process = process
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> StaticProcess:
        self.commands.append(list(command))
        return self.process


class BlockingStdout:
    def __init__(self, first_line: bytes, stopped: threading.Event) -> None:
        self.first_line = first_line
        self.stopped = stopped
        self.sent_first = False

    def readline(self) -> bytes:
        if not self.sent_first:
            self.sent_first = True
            return self.first_line
        if not self.stopped.wait(timeout=5):
            raise TimeoutError("fake yt-dlp was not terminated")
        return b""


class BlockingProcess:
    def __init__(self, first_line: bytes) -> None:
        self.stopped = threading.Event()
        self.stdout = BlockingStdout(first_line, self.stopped)
        self.stderr = io.BytesIO()
        self.terminated = False
        self.killed = False
        self.return_code: int | None = None

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if not self.stopped.wait(timeout=timeout or 5):
            raise TimeoutError("fake process wait timed out")
        return self.return_code if self.return_code is not None else -15

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15
        self.stopped.set()

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9
        self.stopped.set()


def video_entry(
    work_id: str,
    title: object,
    *,
    author: object = "离线频道",
    playlist_count: object = 2,
) -> dict[str, object]:
    return {
        "_type": "url",
        "id": work_id,
        "title": title,
        "channel": author,
        "upload_date": "20260718",
        "playlist_count": playlist_count,
        # These are intentionally untrusted and must never become WorkItem URLs
        # or callback text.
        "url": "https://media.invalid/videoplayback?token=MEDIA_SECRET",
        "webpage_url": (
            "https://www.youtube.com/watch?v=wrong-id&signature=PAGE_SECRET"
        ),
    }


def assert_raises(expected: type[BaseException], callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}")


def check_target_validation() -> None:
    playlist = identify_youtube_target(
        "复制链接 https://www.youtube.com/playlist?list=PLabcdefghijklmno&si=TRACKING_SECRET",
        source=SourceKind.YOUTUBE_PLAYLIST,
    )
    assert playlist.source is SourceKind.YOUTUBE_PLAYLIST
    assert playlist.url == "https://www.youtube.com/playlist?list=PLabcdefghijklmno"
    assert "TRACKING_SECRET" not in repr(playlist)
    assert "PLabcdefghijklmno" not in repr(playlist)

    channel = identify_youtube_target(
        "https://m.youtube.com/@中文频道/videos?view=0&token=TRACKING_SECRET",
        source="channel",
    )
    assert channel.source is SourceKind.YOUTUBE_CHANNEL
    assert channel.url.startswith("https://www.youtube.com/@")
    assert channel.url.endswith("%E9%A2%91%E9%81%93")
    assert "?" not in channel.url
    assert "TRACKING_SECRET" not in repr(channel)

    extracted = extract_youtube_url(
        "先忽略 https://example.invalid/a 再打开 https://youtube.com/@offline"
    )
    assert extracted == "https://youtube.com/@offline"

    # A copied watch URL remains a single-video task even if YouTube appends a
    # playlist context.  It must not silently turn into a batch scan.
    assert_raises(
        ValueError,
        lambda: identify_youtube_target(
            "https://www.youtube.com/watch?v=AbCdEfGhI01&list=PLabcdefghijklmno",
            source="playlist",
        ),
    )
    assert_raises(
        ValueError,
        lambda: identify_youtube_target(
            "https://www.youtube.com/playlist?list=PLone&list=PLtwo",
            source="playlist",
        ),
    )
    assert_raises(
        ValueError,
        lambda: identify_youtube_target(
            "https://notyoutube.com/playlist?list=PLabcdefghijklmno",
            source="playlist",
        ),
    )
    assert_raises(
        ValueError,
        lambda: identify_youtube_target(
            "https://youtube.com/@offline/videos/unexpected",
            source="channel",
        ),
    )


def check_complete_playlist_stream() -> None:
    first = video_entry("AbCdEfGhI01", "第一\n条视频", playlist_count=3)
    duplicate = video_entry("AbCdEfGhI01", "重复视频", playlist_count=3)
    second = video_entry("AbCdEfGhI02", "第二条视频", playlist_count=3)
    process = StaticProcess(
        [json_line(first), json_line(duplicate), json_line(second)],
        stderr_lines=[
            b"WARNING: retried https://www.youtube.com/api?token=LOG_SECRET\n"
        ],
    )
    runner = RecordingRunner(process)
    progress: list[tuple[int, str]] = []
    lines: list[str] = []
    enumerator = YouTubeEnumerator("X:/offline/yt-dlp.exe", runner=runner)
    bundle = enumerator.scan(
        "https://www.youtube.com/playlist?list=PLabcdefghijklmno&si=DROP_ME",
        source=SourceKind.YOUTUBE_PLAYLIST,
        on_progress=lambda count, message: progress.append((count, message)),
        on_line=lines.append,
    )

    result = bundle.result
    assert result.enumeration_complete
    assert result.incomplete_reason == ""
    assert result.source is SourceKind.YOUTUBE_PLAYLIST
    assert result.unique_count == 2
    assert result.reported_count == 3
    assert result.author == "离线频道"
    assert result.content_counts[ContentKind.VIDEO.value] == 2
    assert [item.work_id for item in result.items] == [
        "AbCdEfGhI01",
        "AbCdEfGhI02",
    ]
    assert all(item.platform is Platform.YOUTUBE for item in result.items)
    assert result.items[0].title == "第一 条视频"
    assert str(result.items[0].published_at) == "2026-07-18"
    assert result.items[0].canonical_url == (
        "https://www.youtube.com/watch?v=AbCdEfGhI01"
    )
    assert "media.invalid" not in result.items[0].canonical_url
    assert [count for count, _message in progress] == [1, 2]
    assert not process.terminated and not process.killed
    assert not enumerator.busy

    command = runner.commands[0]
    for required in (
        "--ignore-config",
        "--flat-playlist",
        "--lazy-playlist",
        "--dump-json",
        "--skip-download",
        "--simulate",
        "--yes-playlist",
    ):
        assert required in command
    assert command[-1] == (
        "https://www.youtube.com/playlist?list=PLabcdefghijklmno"
    )
    public_text = "\n".join(lines) + repr(bundle)
    assert "LOG_SECRET" not in public_text
    assert "MEDIA_SECRET" not in public_text
    assert "DROP_ME" not in public_text
    assert "?token=" not in public_text
    assert "PLabcdefghijklmno" not in repr(bundle)


def check_nonzero_exit_retains_items_and_redacts() -> None:
    process = StaticProcess(
        [json_line(video_entry("ErRoRvId001", "错误前已发现"))],
        stderr_lines=[
            (
                "ERROR: https://rr1.googlevideo.com/videoplayback?token=QUERY_SECRET"
                " authorization=Bearer HEADER_SECRET cookie=session=COOKIE_SECRET\n"
            ).encode("utf-8")
        ],
        return_code=1,
    )
    lines: list[str] = []
    bundle = YouTubeEnumerator(r"X:\offline\yt-dlp.exe", runner=RecordingRunner(process)).scan(
        "https://www.youtube.com/@offline/videos",
        source=SourceKind.YOUTUBE_CHANNEL,
        on_line=lines.append,
    )
    assert not bundle.result.enumeration_complete
    assert bundle.result.unique_count == 1
    assert bundle.result.items[0].work_id == "ErRoRvId001"
    assert bundle.exit_code == 1
    assert "退出码 1" in bundle.result.incomplete_reason
    safe_text = "\n".join(lines) + bundle.result.incomplete_reason
    assert "QUERY_SECRET" not in safe_text
    assert "HEADER_SECRET" not in safe_text
    assert "COOKIE_SECRET" not in safe_text
    assert "?token=" not in safe_text
    assert "<redacted>" in safe_text


def check_malformed_output_is_incomplete() -> None:
    valid = video_entry("VaLiDvId001", "有效条目", author=123, playlist_count="2")
    invalid_id = video_entry("https://media.invalid/?token=BAD", "无效 ID")
    process = StaticProcess(
        [json_line(valid), b"not-json\n", json_line(invalid_id)],
        return_code=0,
    )
    bundle = YouTubeEnumerator("fake-yt-dlp", runner=RecordingRunner(process)).scan(
        "https://www.youtube.com/channel/UCabcdefghijklmno/videos",
        source="youtube_channel",
    )
    assert not bundle.result.enumeration_complete
    assert bundle.exit_code == 0
    assert bundle.rejected_lines == 2
    assert bundle.result.unique_count == 1
    assert bundle.result.author == ""
    assert bundle.result.reported_count is None
    assert "2 条无效记录" in bundle.result.incomplete_reason
    assert "media.invalid" not in bundle.result.incomplete_reason


def check_cancel_terminates_and_retains_progress() -> None:
    process = BlockingProcess(json_line(video_entry("CaNcElVid01", "取消前发现")))
    runner_calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> BlockingProcess:
        runner_calls.append(list(command))
        return process

    enumerator = YouTubeEnumerator("fake-yt-dlp", runner=runner, wait_timeout=1)

    def cancel_after_first(count: int, _message: str) -> None:
        if count == 1:
            enumerator.cancel()

    bundle = enumerator.scan(
        "https://www.youtube.com/playlist?list=PLcanceloffline",
        source="playlist",
        on_progress=cancel_after_first,
    )
    assert runner_calls
    assert process.terminated
    assert not process.killed
    assert bundle.cancelled
    assert not bundle.result.enumeration_complete
    assert bundle.result.unique_count == 1
    assert "取消" in bundle.result.incomplete_reason
    assert not enumerator.busy


def check_pre_cancel_and_start_failure() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    calls: list[list[str]] = []

    def unused_runner(command: Sequence[str]) -> StaticProcess:
        calls.append(list(command))
        return StaticProcess([])

    cancelled = YouTubeEnumerator("fake", runner=unused_runner).scan(
        "https://youtube.com/@cancelled",
        source="channel",
        cancel_event=cancel_event,
    )
    assert cancelled.cancelled
    assert not cancelled.result.enumeration_complete
    assert not calls

    def failing_runner(_command: Sequence[str]) -> StaticProcess:
        raise OSError(
            "cannot start https://download.invalid/core?token=START_SECRET "
            "token=SECOND_SECRET"
        )

    failed = YouTubeEnumerator("missing", runner=failing_runner).scan(
        "https://youtube.com/playlist?list=PLstartfailure",
        source="playlist",
    )
    assert failed.exit_code is None
    assert not failed.cancelled
    assert not failed.result.enumeration_complete
    assert failed.result.unique_count == 0
    assert "无法启动" in failed.result.incomplete_reason
    assert "START_SECRET" not in failed.result.incomplete_reason
    assert "SECOND_SECRET" not in failed.result.incomplete_reason
    assert "?token=" not in failed.result.incomplete_reason


def main() -> None:
    check_target_validation()
    check_complete_playlist_stream()
    check_nonzero_exit_retains_items_and_redacts()
    check_malformed_output_is_incomplete()
    check_cancel_terminates_and_retains_progress()
    check_pre_cancel_and_start_failure()
    print("youtube enumerator smoke passed")


if __name__ == "__main__":
    main()
