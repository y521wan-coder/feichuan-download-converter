"""Construct the scan dialog and verify exact queue counts are visible."""

from __future__ import annotations

import sys
from pathlib import Path

import wx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.coordinator import BatchPlanPreview  # noqa: E402
from feichuan_downloader.downloader import UrlInspection  # noqa: E402
from feichuan_downloader.gui import (  # noqa: E402
    GenericInspectionFailureDialog,
    GenericPlaylistConfirmDialog,
    ScanConfirmDialog,
)
from feichuan_downloader.models import (  # noqa: E402
    ContentKind,
    DownloadMode,
    Platform,
    ScanResult,
    SourceKind,
    WorkItem,
)


def labels(window: wx.Window) -> list[str]:
    result: list[str] = []
    for child in window.GetChildren():
        getter = getattr(child, "GetLabel", None)
        if callable(getter):
            value = str(getter() or "")
            if value:
                result.append(value)
        value_getter = getattr(child, "GetValue", None)
        if callable(value_getter):
            value = str(value_getter() or "")
            if value:
                result.append(value)
        result.extend(labels(child))
    return result


def buttons(window: wx.Window) -> list[wx.Button]:
    result: list[wx.Button] = []
    for child in window.GetChildren():
        if isinstance(child, wx.Button):
            result.append(child)
        result.extend(buttons(child))
    return result


def assert_cancel_is_safe_default(dialog: wx.Dialog) -> None:
    dialog.Show()
    dialog.Raise()
    wx.Yield()
    by_label = {button.GetLabelText(): button for button in buttons(dialog)}
    assert "取消" in by_label, by_label
    cancel = by_label["取消"]
    assert dialog.GetDefaultItem() is cancel
    assert wx.Window.FindFocus() is cancel
    for label, button in by_label.items():
        if label:
            assert str(button.GetName() or "").strip(), (label, button.GetName())


def check_generic_playlist_dialog(frame: wx.Frame) -> None:
    inspection = UrlInspection(
        original_url="https://example.test/category/offline?page=2",
        is_playlist=True,
        count=304,
        title="离线普通网站分类页",
        entries_preview=(
            "第一条预览视频",
            "第二条预览视频",
            "第三条预览视频",
        ),
    )
    dialog = GenericPlaylistConfirmDialog(frame, inspection)
    try:
        text = "\n".join(labels(dialog))
        assert "普通网站" in dialog.GetTitle()
        assert "离线普通网站分类页" in text
        assert inspection.original_url in text
        assert "304" in text
        for title in inspection.entries_preview:
            assert title in text
        button_labels = {button.GetLabelText() for button in buttons(dialog)}
        assert button_labels == {"下载全部", "只下载第一个视频", "取消"}
        assert_cancel_is_safe_default(dialog)
    finally:
        dialog.Destroy()
        wx.Yield()


def check_generic_failure_dialog(frame: wx.Frame) -> None:
    url = "https://example.test/category/unknown"
    error = "无法确认数量：离线 yt-dlp 预检失败"
    dialog = GenericInspectionFailureDialog(frame, url, error)
    try:
        text = "\n".join(labels(dialog))
        assert "无法确认" in dialog.GetTitle()
        assert url in text
        assert error in text
        button_labels = {button.GetLabelText() for button in buttons(dialog)}
        assert button_labels == {"继续单链接下载", "取消"}
        assert_cancel_is_safe_default(dialog)
    finally:
        dialog.Destroy()
        wx.Yield()


def item(work_id: str, kind: ContentKind) -> WorkItem:
    return WorkItem(
        platform=Platform.DOUYIN,
        work_id=work_id,
        content_type=kind,
        title=work_id,
        author="测试作者",
        published_at=None,
        canonical_url=f"https://www.douyin.com/video/{work_id}",
    )


def main() -> None:
    app = wx.App(False)
    frame = wx.Frame(None)
    items = (
        item("video-1", ContentKind.VIDEO),
        item("video-2", ContentKind.VIDEO),
        item("image-1", ContentKind.IMAGE),
        item("live-1", ContentKind.LIVE),
    )
    result = ScanResult(
        source=SourceKind.DOUYIN_PROFILE,
        author="测试作者",
        reported_count=5,
        unique_count=4,
        content_counts={
            ContentKind.VIDEO: 2,
            ContentKind.IMAGE: 1,
            ContentKind.LIVE: 1,
        },
        enumeration_complete=True,
        items=items,
    )
    previews = (
        BatchPlanPreview(DownloadMode.INCREMENTAL, 2, 2, 1),
        BatchPlanPreview(DownloadMode.REDOWNLOAD_ALL, 3, 1, 1),
        BatchPlanPreview(DownloadMode.RETRY_FAILED, 1, 3, 1),
    )
    dialog = ScanConfirmDialog(frame, result, previews)
    try:
        text = "\n".join(labels(dialog))
        assert "内容分类：视频 2，图文 1，直播 1，未知 0" in text
        assert "增量下载（默认） — 将下载 2 条" in text
        assert "全量重新下载 — 将下载 3 条" in text
        assert "仅重试失败 — 将下载 1 条" in text
        normalized = " ".join(text.split())
        assert "其中 1 条为直播、未知内容或缺少可校验媒体信息" in normalized
    finally:
        dialog.Destroy()
        wx.Yield()
    check_generic_playlist_dialog(frame)
    check_generic_failure_dialog(frame)
    try:
        frame.Show()
        frame.Raise()
        wx.Yield()
    finally:
        frame.Destroy()
        app.Destroy()
    print("gui dialog smoke passed")


if __name__ == "__main__":
    main()
