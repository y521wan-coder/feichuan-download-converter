"""Offline source routing and incomplete-confirmation checks."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import (
    CoordinatorError,
    classify_url,
    extract_url,
    normalize_confirmation_choice,
)
from feichuan_downloader.models import (
    DownloadMode,
    IncompleteScanAction,
    ScanResult,
    SourceKind,
)


def main() -> None:
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
