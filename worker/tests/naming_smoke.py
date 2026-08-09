"""验证抖音视频和图文规范文件名。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.models import ContentKind, Platform, WorkItem
from feichuan_downloader.naming import (
    MAX_FILENAME_UNITS,
    douyin_image_filename,
    douyin_video_filename,
    filename_for,
    output_path,
    windows_name_units,
)


def work_item(
    platform: Platform,
    content_type: ContentKind,
    *,
    author: str = "张三",
    work_id: str = "7654321",
    title: str = "测试标题",
) -> WorkItem:
    return WorkItem(
        platform=platform,
        work_id=work_id,
        content_type=content_type,
        title=title,
        author=author,
        published_at=datetime(2026, 7, 18, 12, 30),
        canonical_url="https://example.test/work/7654321",
    )


def main() -> None:
    douyin_video = work_item(Platform.DOUYIN, ContentKind.VIDEO)
    assert douyin_video_filename(douyin_video) == (
        "抖音_张三_20260718_7654321_测试标题.mp4"
    )

    douyin_images = work_item(Platform.DOUYIN, ContentKind.IMAGE)
    assert douyin_image_filename(douyin_images, 2) == (
        "抖音图文_张三_20260718_7654321_002.webp"
    )
    assert filename_for(douyin_images, sequence=12) == (
        "抖音图文_张三_20260718_7654321_012.webp"
    )

    awkward = work_item(
        Platform.DOUYIN,
        ContentKind.VIDEO,
        author="CON",
        title='坏<>:"/\\|?*标题. ',
    )
    awkward_name = douyin_video_filename(awkward)
    assert awkward_name.startswith("抖音__CON_20260718_7654321_")
    assert awkward_name.endswith(".mp4")
    assert not any(character in awkward_name for character in '<>:"/\\|?*')

    long_item = work_item(
        Platform.DOUYIN,
        ContentKind.VIDEO,
        author="作者" * 180,
        work_id="stable-id-1234567890",
        title=("很长的中文标题" * 100) + "🚀🚀🚀",
    )
    long_name = douyin_video_filename(long_item)
    assert windows_name_units(long_name) <= MAX_FILENAME_UNITS
    assert long_name.startswith("抖音_") and long_name.endswith(".mp4")
    assert "stable-id-1234567890" in long_name
    assert long_name == douyin_video_filename(long_item)

    huge_id = work_item(
        Platform.DOUYIN,
        ContentKind.IMAGE,
        work_id="作品ID" * 150,
    )
    huge_id_name = douyin_image_filename(huge_id, 1001)
    assert windows_name_units(huge_id_name) <= MAX_FILENAME_UNITS
    assert huge_id_name.endswith("_1001.webp")

    unknown_date = WorkItem(
        platform=Platform.DOUYIN,
        work_id="unknown-date",
        content_type=ContentKind.VIDEO,
        title="标题",
        author="作者",
        published_at=None,
        canonical_url="https://example.test/unknown-date",
    )
    assert "_未知日期_" in douyin_video_filename(unknown_date)

    try:
        douyin_image_filename(douyin_images, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("sequence=0 unexpectedly succeeded")

    destination = output_path(douyin_video_filename(douyin_video), Path("X:/downloads"))
    assert destination == Path("X:/downloads") / douyin_video_filename(douyin_video)
    print("naming smoke passed")


if __name__ == "__main__":
    main()
