"""Offline source routing and incomplete-confirmation checks."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import (
    CoordinatorError,
    DownloadCoordinator,
    classify_url,
    extract_url,
    normalize_confirmation_choice,
)
from feichuan_downloader.downloader import DownloadResult
from feichuan_downloader.models import (
    ContentKind,
    DownloadMode,
    IncompleteScanAction,
    ScanResult,
    SourceKind,
    Platform,
    WorkItem,
)


class FakeSingleNoteBundle:
    def __init__(self) -> None:
        self.item = WorkItem(
            platform=Platform.DOUYIN,
            work_id="7341234567890123456",
            content_type=ContentKind.IMAGE,
            title="离线图文",
            author="作者",
            published_at=date(2026, 8, 14),
            canonical_url="https://www.douyin.com/note/7341234567890123456",
        )
        self.result = ScanResult(
            source=SourceKind.SINGLE_LINK,
            author="作者",
            reported_count=1,
            unique_count=1,
            content_counts={ContentKind.IMAGE: 1},
            enumeration_complete=True,
            items=(self.item,),
        )
        self.media_by_work_id = {self.item.work_id: ()}
        self.cookie_header = ""
        self.cleared = False

    def clear_sensitive(self) -> None:
        self.cleared = True


class FakeSingleNoteScanner:
    def __init__(self, resolved_url: str) -> None:
        self.resolved_url = resolved_url
        self.scan_calls = 0
        self.bundle = FakeSingleNoteBundle()

    def identify(self, _text: str) -> SimpleNamespace:
        return SimpleNamespace(source=SourceKind.SINGLE_LINK, url=self.resolved_url)

    def scan_single_note(self, _url: str, **_kwargs: object) -> FakeSingleNoteBundle:
        self.scan_calls += 1
        return self.bundle


class FakeImageDownloader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def download(self, _item: WorkItem, _media: object, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        assert kwargs["include_description"] is False
        if self.fail:
            raise RuntimeError("offline image failure")
        return SimpleNamespace(
            media_paths=(Path(r"D:\output\note_001.webp"), Path(r"D:\output\note_002.webp"))
        )


class FakeAudioDownloader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.force_audio_only: list[bool] = []

    def download(self, _url: str, **_kwargs: object) -> DownloadResult:
        self.calls += 1
        self.force_audio_only.append(bool(_kwargs.get("force_audio_only")))
        if self.fail:
            raise RuntimeError("offline audio failure")
        return DownloadResult(Path(r"D:\output\note.m4a"), title="背景音频")


def check_note_content_routing_and_partial_success() -> None:
    note_url = "https://www.douyin.com/share/note/7341234567890123456"
    scanner = FakeSingleNoteScanner(note_url)
    images = FakeImageDownloader()
    audio = FakeAudioDownloader()
    coordinator = DownloadCoordinator(
        douyin_scanner_factory=lambda: scanner,
        douyin_downloader_factory=lambda: images,
        downloader_factory=lambda: audio,
    )
    result = coordinator.scan_or_download(
        note_url,
        douyin_note_content="images_and_audio",
    )
    assert scanner.scan_calls == 1 and images.calls == 1 and audio.calls == 1
    assert audio.force_audio_only == [True]
    assert len(result.paths) == 3
    assert not result.partial_success and not result.warnings
    assert scanner.bundle.cleared

    audio_only_scanner = FakeSingleNoteScanner(note_url)
    audio_only = FakeAudioDownloader()
    result = DownloadCoordinator(
        douyin_scanner_factory=lambda: audio_only_scanner,
        downloader_factory=lambda: audio_only,
    ).scan_or_download(note_url, douyin_note_content="audio_only")
    assert result.path.name == "note.m4a"
    assert audio_only_scanner.scan_calls == 0 and audio_only.calls == 1
    assert audio_only.force_audio_only == [True]

    image_failure = FakeImageDownloader(fail=True)
    partial = DownloadCoordinator(
        douyin_scanner_factory=lambda: FakeSingleNoteScanner(note_url),
        douyin_downloader_factory=lambda: image_failure,
        downloader_factory=lambda: FakeAudioDownloader(),
    ).scan_or_download(note_url, douyin_note_content="images_and_audio")
    assert partial.partial_success and partial.path.suffix == ".m4a"
    assert partial.warnings and "图片" in partial.warnings[0]

    audio_failure = FakeAudioDownloader(fail=True)
    partial = DownloadCoordinator(
        douyin_scanner_factory=lambda: FakeSingleNoteScanner(note_url),
        douyin_downloader_factory=lambda: FakeImageDownloader(),
        downloader_factory=lambda: audio_failure,
    ).scan_or_download(note_url, douyin_note_content="images_and_audio")
    assert partial.partial_success and all(path.suffix == ".webp" for path in partial.paths)
    assert any("音频" in warning for warning in partial.warnings)

    video_url = "https://www.douyin.com/video/7341234567890123456"
    video_scanner = FakeSingleNoteScanner(video_url)
    video_audio = FakeAudioDownloader()
    DownloadCoordinator(
        douyin_scanner_factory=lambda: video_scanner,
        downloader_factory=lambda: video_audio,
    ).scan_or_download(video_url, douyin_note_content="images_and_audio")
    assert video_scanner.scan_calls == 0 and video_audio.calls == 1
    assert video_audio.force_audio_only == [False]


def main() -> None:
    check_note_content_routing_and_partial_success()
    shared = "复制此消息打开抖音 https://www.douyin.com/user/abc?token=secret 立即查看"
    assert extract_url(shared).startswith("https://www.douyin.com/user/abc")
    assert classify_url(extract_url(shared)) is SourceKind.DOUYIN_PROFILE
    assert (
        classify_url("https://www.douyin.com/collection/123")
        is SourceKind.DOUYIN_COLLECTION
    )
    assert classify_url("https://v.douyin.com/abc/") is SourceKind.SINGLE_LINK
    assert (
        classify_url("https://www.youtube.com/playlist?list=PL_TEST")
        is SourceKind.YOUTUBE_PLAYLIST
    )
    assert (
        classify_url("https://music.youtube.com/playlist?list=PL_MUSIC")
        is SourceKind.YOUTUBE_PLAYLIST
    )
    assert (
        classify_url("https://www.youtube.com/@OpenAI/videos")
        is SourceKind.YOUTUBE_CHANNEL
    )
    assert (
        classify_url("https://www.youtube.com/channel/UC_TEST")
        is SourceKind.YOUTUBE_CHANNEL
    )
    # A video URL remains a one-item task even when YouTube includes playlist
    # context in the query string.
    assert (
        classify_url("https://www.youtube.com/watch?v=one&list=PL_TEST")
        is SourceKind.SINGLE_LINK
    )
    assert classify_url("https://youtu.be/one?list=PL_TEST") is SourceKind.SINGLE_LINK
    assert classify_url("https://example.test/watch/1") is SourceKind.SINGLE_LINK
    try:
        classify_url("https://channels.weixin.qq.com/web/pages/feed")
    except CoordinatorError:
        pass
    else:
        raise AssertionError("retired source unexpectedly remained routable")

    incomplete = ScanResult(
        source=SourceKind.DOUYIN_PROFILE,
        author="作者",
        reported_count=None,
        unique_count=0,
        enumeration_complete=False,
        incomplete_reason="验证码",
    )
    assert normalize_confirmation_choice(
        incomplete, IncompleteScanAction.DISCOVERED_ONLY
    ) is IncompleteScanAction.DISCOVERED_ONLY
    try:
        normalize_confirmation_choice(incomplete, DownloadMode.REDOWNLOAD_ALL)
    except CoordinatorError:
        pass
    else:
        raise AssertionError("incomplete scan accepted redownload_all")
    print("coordinator smoke passed")


if __name__ == "__main__":
    main()
