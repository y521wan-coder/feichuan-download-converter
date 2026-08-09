"""wxPython native single-window download UI."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import wx

from .config import (
    APP_NAME,
    APP_ROOT,
    AUTO_UPDATE_CORE,
    LOG_DIR,
    VERSION,
    douyin_chromium_profile_dir,
    ensure_layout,
    get_download_dir,
    get_quality_mode,
    format_bytes,
    mark_usage_guide_seen,
    QUALITY_MODE_ASK_EACH_TIME,
    QUALITY_MODE_BEST,
    safe_url_for_log,
    set_download_dir,
    set_quality_mode,
    usage_guide_seen,
)
from .coordinator import (
    BatchDownloadSummary,
    BatchPlanPreview,
    DownloadCoordinator,
    PreparedGenericDownload,
    PreparedScan,
    normalize_confirmation_choice,
)
from .core_updater import CoreUpdateResult, CoreUpdater
from .downloader import DownloadResult, Downloader, UrlInspection
from .douyin_session import DouyinSessionProvider
from .logging_utils import get_logger
from .models import (
    ContentKind,
    DownloadEvent,
    DownloadMode,
    DownloadStage,
    IncompleteScanAction,
    ScanResult,
    SourceKind,
)
from .quality import (
    NO_ALTERNATIVE_MESSAGE,
    QualityChoice,
    QualityPreference,
    choices_from_media_descriptors,
    preference_label,
)
from .software_updater import (
    SoftwareUpdateResult,
    check_software_update,
    download_update_installer,
    launch_update_installer,
)


class ScanConfirmDialog(wx.Dialog):
    """Choose a repeat-run policy without overstating an incomplete scan."""

    def __init__(
        self,
        parent: wx.Window,
        result: ScanResult,
        previews: tuple[BatchPlanPreview, ...] = (),
    ) -> None:
        super().__init__(parent, title="确认批量下载", style=wx.DEFAULT_DIALOG_STYLE)
        self.result = result
        self._choices: list[DownloadMode | IncompleteScanAction] = []
        preview_by_choice = {preview.choice: preview for preview in previews}

        def count(kind: ContentKind) -> int:
            return int(result.content_counts.get(kind.value, 0))

        content_summary = (
            f"视频 {count(ContentKind.VIDEO)}，图文 {count(ContentKind.IMAGE)}，"
            f"直播 {count(ContentKind.LIVE)}，未知 {count(ContentKind.UNKNOWN)}"
        )

        root = wx.BoxSizer(wx.VERTICAL)
        summary = (
            f"作者：{result.author or '未知作者'}\n"
            f"页面报告数量："
            f"{result.reported_count if result.reported_count is not None else '未知'}\n"
            f"实际发现数量：{result.unique_count}\n"
            f"内容分类：{content_summary}"
        )
        root.Add(wx.StaticText(self, label=summary), 0, wx.ALL, 12)

        if result.enumeration_complete:
            complete_text = (
                "扫描已收到 has_more=false，请选择本次下载方式："
                if result.source
                in {SourceKind.DOUYIN_PROFILE, SourceKind.DOUYIN_COLLECTION}
                else "扫描已到达列表结尾，请选择本次下载方式："
            )
            label = wx.StaticText(self, label=complete_text)
            root.Add(label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            options = (
                (
                    DownloadMode.INCREMENTAL,
                    "增量下载（默认）",
                    "下载新增、上次失败及数据库标记有效但文件缺失的作品。",
                ),
                (
                    DownloadMode.REDOWNLOAD_ALL,
                    "全量重新下载",
                    "下载全部作品；每个新文件校验成功后才原子替换旧文件。",
                ),
                (
                    DownloadMode.RETRY_FAILED,
                    "仅重试失败",
                    "只处理上次失败或校验无效的作品。",
                ),
            )
        else:
            reason = result.incomplete_reason or "页面未明确返回 has_more=false"
            warning = wx.StaticText(
                self,
                label=(
                    "枚举不完整，不能确认已获取全部作品。\n"
                    f"原因：{reason}"
                ),
            )
            warning.SetForegroundColour(wx.Colour(160, 70, 0))
            root.Add(warning, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            options = (
                (
                    IncompleteScanAction.DISCOVERED_ONLY,
                    "下载已发现内容",
                    "仅处理本次已经发现的项目；不会声称列表完整。",
                ),
            )

        self.radio_buttons: list[wx.RadioButton] = []
        for index, (choice, title, description) in enumerate(options):
            preview = preview_by_choice.get(choice)
            if preview is not None:
                title += f" — 将下载 {preview.download_count} 条"
                description += (
                    f"\n按当前数据库和文件状态：将下载 {preview.download_count} 条，"
                    f"跳过 {preview.skip_count} 条。"
                )
                if preview.unsupported_count:
                    description += (
                        f"其中 {preview.unsupported_count} 条为直播、未知内容或缺少可校验媒体信息，"
                        "本次会明确跳过。"
                    )
            style = wx.RB_GROUP if index == 0 else 0
            radio = wx.RadioButton(self, label=title, style=style)
            radio.SetName(f"批量模式-{choice.value}")
            radio.SetValue(index == 0)
            self.radio_buttons.append(radio)
            self._choices.append(choice)
            root.Add(radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 18)
            detail = wx.StaticText(self, label=description)
            detail.Wrap(520)
            root.Add(detail, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 34)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(root)
        self.SetMinSize((600, self.GetSize().height))
        self.CentreOnParent()
        if self.radio_buttons:
            self.radio_buttons[0].SetFocus()

    @property
    def choice(self) -> DownloadMode | IncompleteScanAction:
        for radio, choice in zip(self.radio_buttons, self._choices, strict=True):
            if radio.GetValue():
                return choice
        return self._choices[0]


GENERIC_DOWNLOAD_ALL_ID = wx.NewIdRef()
GENERIC_DOWNLOAD_FIRST_ID = wx.NewIdRef()
GENERIC_CONTINUE_SINGLE_ID = wx.NewIdRef()


def _donation_qr_path() -> Path:
    return APP_ROOT / "assets" / "donation_qr.jpg"


class DonationDialog(wx.Dialog):
    """Small keyboard-friendly QR dialog."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title="谢谢喜欢", style=wx.DEFAULT_DIALOG_STYLE)
        root = wx.BoxSizer(wx.VERTICAL)
        message = wx.StaticText(self, label="喜欢就随心支持一下")
        message.SetName("打赏提示")
        root.Add(message, 0, wx.ALIGN_CENTER | wx.ALL, 12)

        qr_path = _donation_qr_path()
        if qr_path.is_file():
            image = wx.Image(str(qr_path), wx.BITMAP_TYPE_ANY)
            max_side = 320
            width = max(1, image.GetWidth())
            height = max(1, image.GetHeight())
            scale = min(max_side / width, max_side / height, 1.0)
            if scale < 1.0:
                image = image.Scale(
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                    wx.IMAGE_QUALITY_HIGH,
                )
            bitmap = wx.StaticBitmap(self, bitmap=wx.Bitmap(image))
            bitmap.SetName("打赏二维码")
            root.Add(bitmap, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        else:
            missing = wx.StaticText(self, label="二维码图片缺失，请重新安装。")
            missing.SetName("打赏二维码缺失提示")
            root.Add(missing, 0, wx.ALIGN_CENTER | wx.ALL, 16)

        close_button = wx.Button(self, wx.ID_CANCEL, "关闭")
        close_button.SetName("关闭打赏窗口按钮")
        root.Add(close_button, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetSizerAndFit(root)
        self.SetMinSize((360, self.GetSize().height))
        self.CentreOnParent()
        close_button.SetFocus()


class GenericPlaylistConfirmDialog(wx.Dialog):
    """Confirm a dynamically inspected generic website playlist."""

    def __init__(self, parent: wx.Window, inspection: UrlInspection) -> None:
        super().__init__(
            parent,
            title="确认普通网站列表下载",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.inspection = inspection
        root = wx.BoxSizer(wx.VERTICAL)

        summary = wx.StaticText(
            self,
            label=(
                f"页面标题：{inspection.title or '未知标题'}\n"
                f"来源网址：{inspection.original_url}\n"
                f"本次实际检测到的视频总数：{inspection.count}"
            ),
        )
        summary.SetName("普通网站列表扫描摘要")
        summary.Wrap(660)
        root.Add(summary, 0, wx.EXPAND | wx.ALL, 14)

        preview_title = wx.StaticText(self, label="标题预览：")
        preview_title.SetName("视频标题预览标签")
        root.Add(preview_title, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        preview_lines = tuple(inspection.entries_preview)
        preview_text = "\n".join(
            f"{index}. {title}" for index, title in enumerate(preview_lines, start=1)
        ) or "（页面没有返回可预览的标题）"
        preview = wx.TextCtrl(
            self,
            value=preview_text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE,
            size=(680, 150),
        )
        preview.SetName("视频标题预览")
        preview.SetHelpText(f"前 {len(preview_lines)} 个视频标题预览")
        root.Add(preview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        warning = wx.StaticText(
            self,
            label="为避免误下大量视频，默认操作是取消。请选择本次下载方式。",
        )
        warning.SetName("普通网站列表安全提示")
        root.Add(warning, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        download_all = wx.Button(self, int(GENERIC_DOWNLOAD_ALL_ID), "下载全部")
        download_all.SetName("下载全部按钮")
        download_first = wx.Button(
            self,
            int(GENERIC_DOWNLOAD_FIRST_ID),
            "只下载第一个视频",
        )
        download_first.SetName("只下载第一个视频按钮")
        cancel = wx.Button(self, wx.ID_CANCEL, "取消")
        cancel.SetName("取消普通网站列表下载按钮")
        buttons.AddStretchSpacer(1)
        buttons.Add(download_all, 0, wx.RIGHT, 8)
        buttons.Add(download_first, 0, wx.RIGHT, 8)
        buttons.Add(cancel, 0)
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 14)

        download_all.Bind(
            wx.EVT_BUTTON,
            lambda _event: self.EndModal(int(GENERIC_DOWNLOAD_ALL_ID)),
        )
        download_first.Bind(
            wx.EVT_BUTTON,
            lambda _event: self.EndModal(int(GENERIC_DOWNLOAD_FIRST_ID)),
        )
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetSizerAndFit(root)
        self.SetMinSize((740, 430))
        self.CentreOnParent()
        cancel.SetDefault()
        cancel.SetFocus()


class GenericInspectionFailureDialog(wx.Dialog):
    """Offer only a one-item fallback when a safe count cannot be confirmed."""

    def __init__(self, parent: wx.Window, url: str, error: str) -> None:
        super().__init__(
            parent,
            title="无法确认视频数量",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        root = wx.BoxSizer(wx.VERTICAL)
        message = wx.StaticText(
            self,
            label=(
                "只读扫描未能确认这个页面包含多少个视频。\n"
                "为避免误触批量下载，软件尚未开始下载。\n\n"
                f"来源网址：{url}\n"
                f"原因：{error}\n\n"
                "你可以按单链接安全模式继续（最多处理第一个视频），或取消。"
            ),
        )
        message.SetName("普通网站预检失败说明")
        message.Wrap(650)
        root.Add(message, 0, wx.EXPAND | wx.ALL, 16)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        continue_single = wx.Button(
            self,
            int(GENERIC_CONTINUE_SINGLE_ID),
            "继续单链接下载",
        )
        continue_single.SetName("继续单链接下载按钮")
        cancel = wx.Button(self, wx.ID_CANCEL, "取消")
        cancel.SetName("取消普通网站单链接下载按钮")
        buttons.AddStretchSpacer(1)
        buttons.Add(continue_single, 0, wx.RIGHT, 8)
        buttons.Add(cancel, 0)
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 14)

        continue_single.Bind(
            wx.EVT_BUTTON,
            lambda _event: self.EndModal(int(GENERIC_CONTINUE_SINGLE_ID)),
        )
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetSizerAndFit(root)
        self.SetMinSize((700, 300))
        self.CentreOnParent()
        cancel.SetDefault()
        cancel.SetFocus()


QUALITY_MODE_LABELS = {
    QUALITY_MODE_BEST: "默认最高品质下载",
    QUALITY_MODE_ASK_EACH_TIME: "每次下载前选择品质或格式",
}
QUALITY_MODE_BY_LABEL = {label: value for value, label in QUALITY_MODE_LABELS.items()}

USAGE_GUIDE_TEXT = """飞船下载工具使用说明

1. 把下载链接或平台分享文本粘贴到“下载链接或平台分享文本”输入框，按回车或点“开始下载”。
2. 要下载单个视频或音频，请复制这个资源本身的分享链接；要下载合集、主页、频道或分类页，请复制对应合集、主页、频道或分类页的分享链接，再粘贴到软件的输入框。
3. 单个视频会直接下载。合集、主页、频道或分类页会先扫描数量，扫描完成后按提示选择下载方式。
4. 如果没有选择下载文件夹，文件会保存到系统“下载”文件夹。下载合集时，软件会在下载文件夹里自动新建对应文件夹。
5. 需要指定保存位置时，点“选择下载文件夹”，选好后以后都会保存到这里。
6. 抖音主页、合集或系列需要登录时，可勾选“使用软件专用 Chrome 登录抖音”，按提示登录。这个登录只给本软件使用。
7. 下载过程中可查看“当前进度摘要”。要停止任务，点“取消任务”。
8. 下载完成后，点“打开下载文件夹”查看文件。
9. 第一次看到本说明时，按 Alt+F4 或关闭按钮即可关闭并进入主界面。"""


class UsageGuideDialog(wx.Dialog):
    """First-run, keyboard-friendly usage guide."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(
            parent,
            title="飞船下载工具使用说明",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        root = wx.BoxSizer(wx.VERTICAL)
        guide = wx.TextCtrl(
            self,
            value=USAGE_GUIDE_TEXT,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE,
            size=(680, 360),
        )
        guide.SetName("飞船下载工具使用说明")
        root.Add(guide, 1, wx.EXPAND | wx.ALL, 12)
        close_button = wx.Button(self, wx.ID_CANCEL, "关闭")
        close_button.SetName("关闭使用说明按钮")
        root.Add(close_button, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetSizerAndFit(root)
        self.SetMinSize((720, 460))
        self.CentreOnParent()
        close_button.SetFocus()


class QualitySelectDialog(wx.Dialog):
    """Choose one simple quality or format rule for the current task."""

    def __init__(
        self,
        parent: wx.Window,
        choices: tuple[QualityChoice, ...],
    ) -> None:
        super().__init__(
            parent,
            title="选择本次品质或格式",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._choices = choices or ()
        root = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label="本次下载使用的品质或格式")
        label.SetName("品质或格式标签")
        root.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        values = [choice.label for choice in self._choices]
        if len(values) <= 1:
            values = [NO_ALTERNATIVE_MESSAGE]
        self.combo = wx.ComboBox(
            self,
            choices=values,
            value=values[0],
            style=wx.CB_READONLY,
        )
        self.combo.SetName("本次品质或格式")
        self.combo.SetHelpText("用上下光标选择本次下载的品质或格式。")
        root.Add(self.combo, 0, wx.EXPAND | wx.ALL, 14)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(root)
        self.SetMinSize((640, self.GetSize().height))
        self.CentreOnParent()
        self.combo.SetFocus()

    @property
    def preference(self) -> QualityPreference:
        index = self.combo.GetSelection()
        if 0 <= index < len(self._choices):
            return self._choices[index].preference
        return QualityPreference.BEST


class MainFrame(wx.Frame):
    def __init__(self) -> None:
        ensure_layout()
        super().__init__(None, title=APP_NAME, size=(940, 720), style=wx.DEFAULT_FRAME_STYLE)
        self.logger = get_logger(LOG_DIR)
        self._busy = False
        self._closing = False
        self._core_checking = False
        self._core_installing = False
        self._software_checking = False
        self._software_update_blocks_ui = False
        self._prepared_scan: PreparedScan | None = None
        self._last_event_message = ""
        self._task_started_at: float | None = None
        self._last_progress_summary = "当前没有下载任务。"
        self._current_quality_preference = QualityPreference.BEST
        self._coordinator = DownloadCoordinator(
            on_event=self._coordinator_event_threadsafe,
            on_line=self._append_threadsafe,
        )

        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        self.download_panel = self._build_download_panel(panel)
        main_sizer.Add(self.download_panel, 0, wx.EXPAND | wx.ALL, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(panel, label="开始下载")
        self.start_button.SetName("开始下载按钮")
        self.cancel_button = wx.Button(panel, label="取消任务")
        self.cancel_button.SetName("取消任务按钮")
        self.open_button = wx.Button(panel, label="打开下载文件夹(&O)")
        self.open_button.SetName("打开下载文件夹按钮")
        self.software_button = wx.Button(panel, label="检查软件更新")
        self.software_button.SetName("检查软件更新按钮")
        self.core_button = wx.Button(panel, label="检查下载核心更新")
        self.core_button.SetName("检查下载核心更新按钮")
        self.exit_button = wx.Button(panel, label="退出")
        self.exit_button.SetName("退出按钮")
        for button in (
            self.start_button,
            self.cancel_button,
            self.open_button,
            self.software_button,
            self.core_button,
            self.exit_button,
        ):
            buttons.Add(button, 0, wx.RIGHT, 8)
        main_sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        progress_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "任务进度")
        status_style = wx.TE_READONLY | wx.BORDER_NONE
        progress_grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=5)
        progress_grid.AddGrowableCol(1, 1)

        scan_label = wx.StaticText(panel, label="扫描数量")
        self.scan_status = wx.TextCtrl(panel, value="0", style=status_style)
        self.scan_status.SetName("扫描数量")
        progress_grid.Add(scan_label, 0, wx.ALIGN_CENTER_VERTICAL)
        progress_grid.Add(self.scan_status, 0, wx.EXPAND)

        queue_label = wx.StaticText(panel, label="任务计数")
        self.queue_status = wx.TextCtrl(
            panel,
            value="当前：0/0　成功：0　跳过：0　失败：0",
            style=status_style,
        )
        self.queue_status.SetName("任务计数")
        progress_grid.Add(queue_label, 0, wx.ALIGN_CENTER_VERTICAL)
        progress_grid.Add(self.queue_status, 0, wx.EXPAND)

        current_file_label = wx.StaticText(panel, label="当前文件")
        self.current_file_status = wx.TextCtrl(
            panel,
            value="",
            style=status_style,
        )
        self.current_file_status.SetName("当前文件")
        progress_grid.Add(current_file_label, 0, wx.ALIGN_CENTER_VERTICAL)
        progress_grid.Add(self.current_file_status, 0, wx.EXPAND)

        summary_label = wx.StaticText(panel, label="当前进度摘要")
        self.progress_summary_ctrl = wx.TextCtrl(
            panel,
            value=self._last_progress_summary,
            style=wx.TE_READONLY | wx.BORDER_SIMPLE,
        )
        self.progress_summary_ctrl.SetName("当前进度摘要")
        self.progress_summary_ctrl.SetHelpText("只读任务进度摘要")
        progress_grid.Add(summary_label, 0, wx.ALIGN_CENTER_VERTICAL)
        progress_grid.Add(self.progress_summary_ctrl, 0, wx.EXPAND)

        progress_label = wx.StaticText(panel, label="整体进度")
        self.progress = wx.Gauge(panel, range=100, style=wx.GA_HORIZONTAL)
        self.progress.SetName("下载进度")
        self.progress.SetValue(0)
        progress_grid.Add(progress_label, 0, wx.ALIGN_CENTER_VERTICAL)
        progress_grid.Add(self.progress, 0, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
        progress_box.Add(progress_grid, 0, wx.EXPAND | wx.ALL, 8)
        main_sizer.Add(progress_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        log_label = wx.StaticText(panel, label="状态和日志")
        log_label.SetName("状态和日志标签")
        main_sizer.Add(log_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.log_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_RICH2,
        )
        self.log_ctrl.SetName("状态和日志区域")
        self.log_ctrl.SetHelpText("只读下载状态和日志")
        main_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 12)

        donation_row = wx.BoxSizer(wx.HORIZONTAL)
        donation_row.AddStretchSpacer(1)
        self.donation_button = wx.Button(panel, label="喜欢这个作品")
        self.donation_button.SetName("喜欢这个作品按钮")
        donation_row.Add(self.donation_button, 0)
        main_sizer.Add(donation_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(main_sizer)
        self.CreateStatusBar()
        self.SetStatusText(f"版本 {VERSION}")

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.link_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_start)
        self.start_button.Bind(wx.EVT_BUTTON, self._on_start)
        self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.open_button.Bind(wx.EVT_BUTTON, self._on_open_drive)
        self.software_button.Bind(wx.EVT_BUTTON, self._on_software_update)
        self.core_button.Bind(wx.EVT_BUTTON, self._on_core_update)
        self.clear_douyin_login_button.Bind(
            wx.EVT_BUTTON,
            self._on_clear_douyin_login,
        )
        self.choose_download_button.Bind(
            wx.EVT_BUTTON,
            self._on_choose_download_dir,
        )
        self.quality_mode_combo.Bind(wx.EVT_COMBOBOX, self._on_quality_mode_changed)
        self.donation_button.Bind(wx.EVT_BUTTON, self._on_donation)
        self.exit_button.Bind(wx.EVT_BUTTON, self._on_exit_button)

        self._append("飞船下载工具已启动。")
        self._append(f"下载目标：{get_download_dir()}")
        self._append(f"下载核心：{self._core_version_text()}")
        self._append("软件与下载核心更新均为手动检查，打开软件不会自动检查更新。")
        self._refresh_controls()
        wx.CallAfter(self._show_usage_guide_if_needed)
        wx.CallAfter(self._focus_link_input)
        wx.CallLater(600, self._focus_link_input)

    def _build_download_panel(self, parent: wx.Window) -> wx.Panel:
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(page, label="下载链接或平台分享文本")
        label.SetName("下载链接标签")
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.link_ctrl = wx.TextCtrl(page, style=wx.TE_PROCESS_ENTER)
        self.link_ctrl.SetName("下载链接输入框")
        self.link_ctrl.SetHelpText(
            "输入普通链接、抖音个人主页、合集或系列子合集分享文本，按回车开始；"
            "普通网站多视频页面会先扫描数量并等待确认"
        )
        sizer.Add(self.link_ctrl, 0, wx.EXPAND | wx.ALL, 10)
        hint = wx.StaticText(
            page,
            label=(
                "普通网站多视频页面、抖音主页/合集/系列子合集和 YouTube 列表/频道"
                "都会先扫描并显示实际数量，再由你确认下载方式。"
            ),
        )
        sizer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.douyin_login_checkbox = wx.CheckBox(
            page,
            label=(
                "使用软件专用 Chrome 登录抖音"
                "（不读取日常浏览器；首次登录后保存 Chrome 加密会话）"
            ),
        )
        self.douyin_login_checkbox.SetName("抖音专用登录选项")
        self.douyin_login_checkbox.SetValue(douyin_chromium_profile_dir().exists())
        sizer.Add(
            self.douyin_login_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10,
        )
        self.clear_douyin_login_button = wx.Button(
            page,
            label="清除软件内抖音登录",
        )
        self.clear_douyin_login_button.SetName("清除抖音登录按钮")
        sizer.Add(
            self.clear_douyin_login_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10,
        )
        download_row = wx.BoxSizer(wx.HORIZONTAL)
        download_label = wx.StaticText(page, label="当前下载文件夹")
        download_label.SetName("当前下载文件夹标签")
        self.download_dir_ctrl = wx.TextCtrl(
            page,
            value=str(get_download_dir()),
            style=wx.TE_READONLY,
        )
        self.download_dir_ctrl.SetName("当前下载文件夹")
        self.download_dir_ctrl.SetHelpText("当前视频和图文保存位置，可按 Ctrl+C 复制。")
        self.choose_download_button = wx.Button(
            page,
            label="选择下载文件夹(&D)",
        )
        self.choose_download_button.SetName("选择下载文件夹按钮")
        self.choose_download_button.SetHelpText(
            "按 Alt+D 打开文件夹选择窗口，设置视频和图文的保存位置。"
        )
        download_row.Add(
            download_label,
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        download_row.Add(self.download_dir_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        download_row.Add(self.choose_download_button, 0)
        sizer.Add(download_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        quality_row = wx.BoxSizer(wx.HORIZONTAL)
        quality_label = wx.StaticText(page, label="品质和格式")
        quality_label.SetName("品质和格式标签")
        current_quality_mode = get_quality_mode()
        quality_choices = [
            QUALITY_MODE_LABELS[QUALITY_MODE_BEST],
            QUALITY_MODE_LABELS[QUALITY_MODE_ASK_EACH_TIME],
        ]
        self.quality_mode_combo = wx.ComboBox(
            page,
            choices=quality_choices,
            value=QUALITY_MODE_LABELS.get(
                current_quality_mode,
                QUALITY_MODE_LABELS[QUALITY_MODE_BEST],
            ),
            style=wx.CB_READONLY,
        )
        self.quality_mode_combo.SetName("品质和格式")
        self.quality_mode_combo.SetHelpText(
            "用上下光标选择默认最高品质下载，或每次下载前选择品质或格式。"
        )
        quality_row.Add(quality_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        quality_row.Add(self.quality_mode_combo, 1, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(quality_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        page.SetSizer(sizer)
        return page

    def _core_version_text(self) -> str:
        try:
            return CoreUpdater().current_version()
        except Exception as exc:
            self.logger.warning("读取核心版本失败: %s", exc)
            return "未知"

    def _append(self, message: str) -> None:
        if not message:
            return
        if not message.endswith("\n"):
            message += "\n"
        try:
            self.log_ctrl.AppendText(message)
            if self.log_ctrl.GetLastPosition() > 120000:
                value = self.log_ctrl.GetValue()
                self.log_ctrl.ChangeValue(value[-90000:])
            self.log_ctrl.ShowPosition(self.log_ctrl.GetLastPosition())
        except (RuntimeError, wx.PyDeadObjectError):
            pass

    def _append_threadsafe(self, message: str) -> None:
        wx.CallAfter(self._append, message)

    def _set_busy(self, busy: bool, *, reset_progress: bool = False) -> None:
        self._busy = busy
        if busy and reset_progress:
            self._task_started_at = time.monotonic()
            self.progress.SetValue(0)
            self.scan_status.ChangeValue("0")
            self.queue_status.ChangeValue("当前：0/0　成功：0　跳过：0　失败：0")
            self.current_file_status.ChangeValue("")
            self._set_progress_summary("当前任务已开始，正在准备。")
        if not busy and not reset_progress:
            self._task_started_at = None
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        workflow_locked = self._busy or self._software_update_blocks_ui
        start_locked = workflow_locked or self._core_installing
        self.start_button.Enable(not start_locked)
        self.cancel_button.Enable(self._busy)
        self.open_button.Enable(not workflow_locked)
        self.software_button.Enable(not workflow_locked)
        self.core_button.Enable(not workflow_locked and not self._core_checking)
        self.link_ctrl.Enable(not workflow_locked)
        self.douyin_login_checkbox.Enable(not workflow_locked)
        self.clear_douyin_login_button.Enable(not workflow_locked)
        self.choose_download_button.Enable(not workflow_locked)
        self.quality_mode_combo.Enable(not workflow_locked)

    def _set_progress_summary(self, message: str) -> None:
        message = str(message or "").strip() or "当前没有下载任务。"
        self._last_progress_summary = message
        try:
            self.progress_summary_ctrl.ChangeValue(message)
        except (RuntimeError, wx.PyDeadObjectError):
            pass

    def _focus_link_input(self) -> None:
        if self._closing or self._busy:
            return
        try:
            if self.link_ctrl.IsEnabled() and self.link_ctrl.IsShownOnScreen():
                self.link_ctrl.SetFocus()
        except (RuntimeError, wx.PyDeadObjectError):
            pass

    def _show_usage_guide_if_needed(self) -> None:
        if self._closing or usage_guide_seen(VERSION):
            return
        dialog = UsageGuideDialog(self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        mark_usage_guide_seen(VERSION)
        self._focus_link_input()

    def _on_quality_mode_changed(self, _event: wx.Event) -> None:
        label = self.quality_mode_combo.GetValue()
        mode = QUALITY_MODE_BY_LABEL.get(label, QUALITY_MODE_BEST)
        try:
            set_quality_mode(mode)
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self._append(f"设置品质和格式失败：{safe_error}")
            self.quality_mode_combo.SetValue(QUALITY_MODE_LABELS[QUALITY_MODE_BEST])
            set_quality_mode(QUALITY_MODE_BEST)
            return
        self._append(f"品质和格式已设置为：{QUALITY_MODE_LABELS[mode]}")

    def _quality_mode(self) -> str:
        label = self.quality_mode_combo.GetValue()
        return QUALITY_MODE_BY_LABEL.get(label, get_quality_mode())

    def _choose_quality_for_url(
        self,
        text: str,
        *,
        playlist_mode: str = "single",
    ) -> QualityPreference:
        if self._quality_mode() != QUALITY_MODE_ASK_EACH_TIME:
            return QualityPreference.BEST
        try:
            _source, url = DownloadCoordinator.classify(text)
            choices = Downloader().inspect_quality_choices(
                url,
                playlist_mode=playlist_mode,
                on_line=self._append,
            )
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self._append(f"无法检测可选品质，将使用默认最高品质：{safe_error}")
            return QualityPreference.BEST
        return self._choose_quality_from_choices(choices)

    def _choose_quality_from_choices(
        self,
        choices: tuple[QualityChoice, ...],
    ) -> QualityPreference:
        dialog = QualitySelectDialog(self, choices)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return QualityPreference.BEST
            return dialog.preference
        finally:
            dialog.Destroy()

    def _choose_quality_for_prepared(self, prepared: PreparedScan) -> QualityPreference:
        if self._quality_mode() != QUALITY_MODE_ASK_EACH_TIME:
            return QualityPreference.BEST
        descriptors = tuple(
            descriptor
            for items in prepared.media_by_work_id.values()
            for descriptor in items
        )
        if descriptors:
            return self._choose_quality_from_choices(
                choices_from_media_descriptors(descriptors)
            )
        try:
            choices = Downloader().inspect_quality_choices(
                prepared.url,
                playlist_mode="all",
                on_line=self._append,
            )
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self._append(f"无法检测可选品质，将使用默认最高品质：{safe_error}")
            return QualityPreference.BEST
        return self._choose_quality_from_choices(choices)

    def _sync_completed_task_state(self) -> bool:
        if not self._busy or self._prepared_scan is not None:
            return False
        try:
            coordinator_busy = self._coordinator.busy
        except Exception as exc:
            self.logger.warning("读取任务状态失败: %s", exc)
            return False
        if coordinator_busy:
            return False
        self._set_busy(False)
        return True

    def _on_start(self, _event: wx.Event) -> None:
        if self._busy or self._core_installing or self._software_checking:
            return
        text = self.link_ctrl.GetValue().strip()
        if not text:
            self._append("请输入下载链接或分享文本。")
            self.link_ctrl.SetFocus()
            return
        if self._prepared_scan:
            self._prepared_scan.clear_sensitive()
            self._prepared_scan = None
        self._current_quality_preference = self._choose_quality_for_url(
            text,
            playlist_mode="single",
        )
        self._append(f"本次品质和格式：{preference_label(self._current_quality_preference)}")
        self._set_busy(True, reset_progress=True)
        self._last_event_message = ""
        self._append("开始处理任务。")
        interactive_login = self.douyin_login_checkbox.GetValue()
        threading.Thread(
            target=self._link_worker,
            args=(text, interactive_login, self._current_quality_preference),
            daemon=True,
        ).start()

    def _link_worker(
        self,
        text: str,
        interactive_login: bool = False,
        quality_preference: QualityPreference = QualityPreference.BEST,
    ) -> None:
        outcome: DownloadResult | PreparedScan | PreparedGenericDownload | None = None
        try:
            outcome = self._coordinator.scan_or_download(
                text,
                interactive_douyin_login=interactive_login,
                quality_preference=quality_preference,
            )
            previews = (
                self._coordinator.preview_prepared(outcome)
                if isinstance(outcome, PreparedScan)
                else ()
            )
        except Exception as exc:
            if isinstance(outcome, PreparedScan):
                outcome.clear_sensitive()
            safe_error = self._safe_message(str(exc))
            self.logger.error("任务失败：%s", safe_error)
            wx.CallAfter(self._task_finished, None, f"任务失败：{safe_error}")
        else:
            wx.CallAfter(self._link_phase_finished, outcome, previews)

    def _link_phase_finished(
        self,
        outcome: DownloadResult | PreparedScan | PreparedGenericDownload,
        previews: tuple[BatchPlanPreview, ...] = (),
    ) -> None:
        if isinstance(outcome, DownloadResult):
            self._task_finished(outcome, "")
            return
        if isinstance(outcome, PreparedGenericDownload):
            self._confirm_generic_download(outcome)
            return
        if self._coordinator.cancel_event.is_set():
            outcome.clear_sensitive()
            self._task_finished(None, "任务已取消。")
            return
        self._prepared_scan = outcome
        if (
            self._quality_mode() == QUALITY_MODE_ASK_EACH_TIME
            and self._current_quality_preference is QualityPreference.BEST
        ):
            self._current_quality_preference = self._choose_quality_for_prepared(outcome)
            self._append(
                f"本次品质和格式：{preference_label(self._current_quality_preference)}"
            )
        dialog = ScanConfirmDialog(self, outcome.result, previews)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                outcome.clear_sensitive()
                self._prepared_scan = None
                self._task_finished(None, "任务已取消。")
                return
            choice = normalize_confirmation_choice(outcome.result, dialog.choice)
        finally:
            dialog.Destroy()
        self._append(f"已选择批量模式：{choice.value}")
        threading.Thread(
            target=self._batch_worker,
            args=(outcome, choice, self._current_quality_preference),
            daemon=True,
        ).start()

    def _confirm_generic_download(self, prepared: PreparedGenericDownload) -> None:
        if self._coordinator.cancel_event.is_set():
            self._task_finished(None, "任务已取消。")
            return

        if prepared.inspection is not None:
            dialog: wx.Dialog = GenericPlaylistConfirmDialog(
                self,
                prepared.inspection,
            )
            try:
                answer = dialog.ShowModal()
            finally:
                dialog.Destroy()
            if answer == int(GENERIC_DOWNLOAD_ALL_ID):
                mode = "all"
            elif answer == int(GENERIC_DOWNLOAD_FIRST_ID):
                mode = "single"
            else:
                self._record_generic_choice("取消")
                self._task_finished(None, "任务已取消。")
                return
        else:
            dialog = GenericInspectionFailureDialog(
                self,
                prepared.url,
                prepared.inspection_error,
            )
            try:
                answer = dialog.ShowModal()
            finally:
                dialog.Destroy()
            if answer != int(GENERIC_CONTINUE_SINGLE_ID):
                self._record_generic_choice("取消")
                self._task_finished(None, "任务已取消。")
                return
            mode = "single"

        threading.Thread(
            target=self._generic_worker,
            args=(prepared, mode, self._current_quality_preference),
            daemon=True,
        ).start()

    def _record_generic_choice(self, label: str) -> None:
        message = f"用户选择：{label}。"
        self.logger.info("%s", message)
        self._append(message)

    def _generic_worker(
        self,
        prepared: PreparedGenericDownload,
        mode: str,
        quality_preference: QualityPreference,
    ) -> None:
        try:
            result = self._coordinator.download_generic(
                prepared,
                mode,
                quality_preference=quality_preference,
            )
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self.logger.error("普通网站下载失败：%s", safe_error)
            wx.CallAfter(self._task_finished, None, f"任务失败：{safe_error}")
        else:
            wx.CallAfter(self._task_finished, result, "")

    def _batch_worker(
        self,
        prepared: PreparedScan,
        choice: DownloadMode | IncompleteScanAction,
        quality_preference: QualityPreference,
    ) -> None:
        try:
            summary = self._coordinator.download_prepared(
                prepared,
                choice,
                quality_preference=quality_preference,
            )
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self.logger.error("批量任务失败：%s", safe_error)
            wx.CallAfter(self._task_finished, None, f"批量任务失败：{safe_error}")
        else:
            wx.CallAfter(self._batch_finished, summary)

    def _batch_finished(self, summary: BatchDownloadSummary) -> None:
        message = (
            f"批量任务完成：成功 {summary.succeeded}，跳过 {summary.skipped}，"
            f"失败 {summary.failed}。"
        )
        self._append(message)
        self.SetStatusText(message)
        self._set_progress_summary(message)
        self._task_finished(summary, "")

    def _task_finished(
        self,
        result: DownloadResult | BatchDownloadSummary | None,
        error: str,
    ) -> None:
        prepared = self._prepared_scan
        self._prepared_scan = None
        if prepared:
            prepared.clear_sensitive()
        self._set_busy(False)
        if isinstance(result, DownloadResult):
            paths = tuple(getattr(result, "paths", ()) or (result.path,))
            if len(paths) > 1:
                self._append(f"列表下载完成，共保存 {len(paths)} 个文件。")
                self._append(f"最后保存文件：{result.path}")
            else:
                self._append(f"下载完成，保存文件：{result.path}")
            if result.used_douyin_fallback:
                self._append("本次使用了浏览器抓流兜底。")
            self.SetStatusText(f"已保存：{result.path.name}")
            self._set_progress_summary("下载完成。")
        elif error:
            self._append(error)
            self.SetStatusText("任务未完成")
            self._set_progress_summary(error)
        elif result is None:
            self._set_progress_summary("当前没有下载任务。")
        self.link_ctrl.SetFocus()
        if self._closing:
            self.Destroy()

    def _on_cancel(self, _event: wx.Event) -> None:
        if self._sync_completed_task_state():
            self.SetStatusText("任务已结束")
            self._focus_link_input()
            return
        if not self._busy:
            return
        self._append("正在取消任务……")
        self._coordinator.cancel()

    def _coordinator_event_threadsafe(self, event: DownloadEvent) -> None:
        wx.CallAfter(self._apply_download_event, event)

    def _apply_download_event(self, event: DownloadEvent) -> None:
        try:
            self.scan_status.ChangeValue(str(event.scanned_count))
            self.queue_status.ChangeValue(
                f"当前：{event.current}/{event.total}　成功：{event.succeeded}　"
                f"跳过：{event.skipped}　失败：{event.failed}"
            )
            self.current_file_status.ChangeValue(event.current_file)
            if event.overall_percent is not None:
                self.progress.SetValue(max(0, min(100, int(event.overall_percent))))
            summary = self._format_progress_summary(event)
            self._set_progress_summary(summary)
            self.SetStatusText(summary)
            if event.message and event.message != self._last_event_message:
                self._last_event_message = event.message
                self._append(event.message)
            if event.stage is DownloadStage.COMPLETED:
                self._set_busy(False)
                self.SetStatusText(summary or event.message or "任务已完成")
                self._focus_link_input()
            if event.stage is DownloadStage.CANCELLED:
                self.SetStatusText(summary)
        except (RuntimeError, wx.PyDeadObjectError):
            pass

    def _format_progress_summary(self, event: DownloadEvent) -> str:
        if event.stage is DownloadStage.SCANNING:
            if event.scanned_count:
                return f"正在扫描，已发现 {event.scanned_count} 个。"
            return event.message or "正在扫描。"
        if event.stage is DownloadStage.AWAITING_CONFIRMATION:
            total = event.total or event.scanned_count
            return f"扫描完成，发现 {total} 个，等待确认。"
        if event.stage is DownloadStage.CANCELLED:
            return "正在取消任务并清理临时资源。"
        if event.stage is DownloadStage.FAILED:
            return event.message or "任务失败。"
        if event.stage is DownloadStage.COMPLETED:
            total = event.total or event.current
            if total > 1:
                return (
                    f"批量任务完成，共 {total} 个，成功 {event.succeeded} 个，"
                    f"跳过 {event.skipped} 个，失败 {event.failed} 个。"
                )
            return event.message or "下载完成。"

        current = event.current if event.current > 0 else (1 if event.total else 0)
        total = event.total
        percent = self.progress.GetValue()
        if event.overall_percent is not None:
            percent = max(0, min(100, int(event.overall_percent)))
        eta = self._eta_text(percent)
        if total > 1:
            waiting = max(0, total - current)
            return (
                f"正在下载第 {current}/{total} 个，已成功 {event.succeeded} 个，"
                f"跳过 {event.skipped} 个，失败 {event.failed} 个，等待 {waiting} 个，"
                f"进度 {percent}%，{eta}。"
            )
        if total == 1:
            return f"正在下载：1/1，进度 {percent}%，{eta}。"
        return event.message or "正在处理任务。"

    def _eta_text(self, percent: int) -> str:
        if percent <= 0 or percent >= 100 or self._task_started_at is None:
            return "预计剩余时间 正在估算"
        elapsed = max(0.0, time.monotonic() - self._task_started_at)
        remaining = elapsed * (100 - percent) / percent
        if remaining < 60:
            return "预计剩余时间 不到 1 分钟"
        minutes = max(1, int(round(remaining / 60)))
        return f"预计剩余时间 约 {minutes} 分钟"

    def _on_clear_douyin_login(self, _event: wx.Event) -> None:
        if self._busy:
            return
        answer = wx.MessageBox(
            "这会删除飞船下载工具自己的抖音专用 Chrome 资料。\n"
            "不会影响你日常使用的 Chrome；下次批量扫描需要重新登录。",
            "确认清除抖音登录",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self,
        )
        if answer != wx.YES:
            return
        try:
            removed = DouyinSessionProvider.clear_saved_login_profile()
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self._append(f"清除抖音登录失败：{safe_error}")
            self.SetStatusText("清除抖音登录失败")
            return
        self.douyin_login_checkbox.SetValue(False)
        message = "已清除软件内抖音登录。" if removed else "当前没有保存的抖音登录。"
        self._append(message)
        self.SetStatusText(message)

    def _on_choose_download_dir(self, _event: wx.Event) -> None:
        if self._busy:
            return
        dialog = wx.DirDialog(
            self,
            "选择下载文件夹",
            defaultPath=str(get_download_dir()),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST | wx.DD_NEW_DIR_BUTTON,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            selected = set_download_dir(dialog.GetPath())
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self._append(f"设置下载文件夹失败：{safe_error}")
            self.SetStatusText("设置下载文件夹失败")
            return
        finally:
            dialog.Destroy()
        self.download_dir_ctrl.ChangeValue(str(selected))
        self._append(f"下载目标已改为：{selected}")
        self.SetStatusText(f"下载文件夹：{selected}")
        self.download_dir_ctrl.SetFocus()

    def _on_open_drive(self, _event: wx.Event) -> None:
        try:
            directory = get_download_dir()
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except Exception as exc:
            self._append(f"打开下载文件夹失败：{exc}")

    def _on_donation(self, _event: wx.Event) -> None:
        dialog = DonationDialog(self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _on_software_update(self, _event: wx.Event) -> None:
        if self._software_checking or self._busy:
            return
        self._start_software_update_check(manual=True)

    def _startup_software_update_check(self) -> None:
        if self._closing or self._busy or self._software_checking:
            return
        self._start_software_update_check(manual=False)

    def _start_software_update_check(self, *, manual: bool) -> None:
        self._software_checking = True
        self._software_update_blocks_ui = manual
        self._refresh_controls()
        self._append("正在检查软件更新……")
        threading.Thread(
            target=self._software_update_worker,
            args=(manual,),
            daemon=True,
        ).start()

    def _software_update_worker(self, manual: bool) -> None:
        try:
            result = check_software_update()
        except Exception as exc:
            result = SoftwareUpdateResult(
                ok=False,
                available=False,
                message=f"检查软件更新失败：{self._safe_message(str(exc))}",
            )
        wx.CallAfter(self._software_update_checked, result, manual)

    def _software_update_checked(
        self,
        result: SoftwareUpdateResult,
        manual: bool,
    ) -> None:
        if self._closing:
            self._software_checking = False
            self._software_update_blocks_ui = False
            return
        self._append(result.message)
        if not result.available:
            self._software_checking = False
            self._software_update_blocks_ui = False
            self._refresh_controls()
            if manual:
                self.SetStatusText(result.message)
                wx.MessageBox(
                    result.message,
                    "软件更新检查" if result.ok else "软件更新检查失败",
                    wx.OK | (wx.ICON_INFORMATION if result.ok else wx.ICON_ERROR),
                    self,
                )
                self.software_button.SetFocus()
            return

        details = [f"发现新版本 {result.latest_version}。"]
        if result.file_size:
            details.append(f"安装包大小：{format_bytes(result.file_size)}")
        if result.release_notes:
            details.append(f"更新说明：\n{result.release_notes[:1200]}")
        details.append("是否下载、校验 SHA-256 并启动安装程序？")
        answer = wx.MessageBox(
            "\n\n".join(details),
            "发现软件更新",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
            self,
        )
        if answer != wx.YES:
            self._software_checking = False
            self._refresh_controls()
            self._append("已取消本次软件更新。")
            self.SetStatusText("已取消软件更新")
            self.software_button.SetFocus()
            return
        self._append("正在下载软件更新安装包……")
        self.progress.SetValue(0)
        self.SetStatusText("正在下载软件更新安装包")
        threading.Thread(
            target=self._software_update_download_worker,
            args=(result,),
            daemon=True,
        ).start()

    def _software_update_download_worker(self, result: SoftwareUpdateResult) -> None:
        def progress(received: int, total: int) -> None:
            if total > 0:
                percent = max(0, min(100, int(received * 100 / total)))
                wx.CallAfter(self.progress.SetValue, percent)
            wx.CallAfter(
                self.SetStatusText,
                f"正在下载更新：{format_bytes(received)} / {format_bytes(total) if total else '未知'}",
            )

        try:
            downloaded = download_update_installer(result, on_progress=progress)
            launch_update_installer(
                downloaded.path,
                expected_sha256=downloaded.sha256,
            )
        except Exception as exc:
            wx.CallAfter(
                self._software_update_install_failed,
                f"软件更新失败：{self._safe_message(str(exc))}",
            )
            return
        wx.CallAfter(self._software_update_installer_started, downloaded.path)

    def _software_update_install_failed(self, message: str) -> None:
        self._software_checking = False
        self._refresh_controls()
        self._append(message)
        self.SetStatusText("软件更新失败")
        wx.MessageBox(message, "软件更新失败", wx.OK | wx.ICON_ERROR, self)
        self.software_button.SetFocus()

    def _software_update_installer_started(self, installer: os.PathLike[str]) -> None:
        self._software_checking = False
        self._append(f"安装包已校验并启动：{installer}")
        self.SetStatusText("安装程序已启动，正在退出当前版本")
        self.Close()

    def _startup_core_check(self) -> None:
        if self._core_checking or self._closing:
            return
        self._core_checking = True
        self._core_installing = AUTO_UPDATE_CORE
        self._refresh_controls()
        threading.Thread(
            target=self._core_update_worker,
            kwargs={"manual": False, "install": AUTO_UPDATE_CORE, "rollback": False},
            daemon=True,
        ).start()

    def _on_core_update(self, _event: wx.Event) -> None:
        if self._core_checking or self._busy or self._software_checking:
            return
        self._core_checking = True
        self._core_installing = True
        self._refresh_controls()
        self._append("正在检查下载核心更新……")
        threading.Thread(
            target=self._core_update_worker,
            kwargs={"manual": True, "install": False, "rollback": False},
            daemon=True,
        ).start()

    def _core_update_worker(self, *, manual: bool, install: bool, rollback: bool = False) -> None:
        updater = CoreUpdater(line_callback=self._append_threadsafe)
        try:
            result = (
                updater.rollback()
                if rollback
                else updater.check_and_update(manual=manual, install=install)
            )
        except Exception as exc:
            safe_error = self._safe_message(str(exc))
            self.logger.error("核心更新任务未处理异常：%s", safe_error)
            result = CoreUpdateResult(
                ok=False,
                updated=False,
                current_version=updater.current_version(),
                message=f"检查下载核心更新失败：{safe_error}",
            )
        wx.CallAfter(self._core_update_finished, result, manual, install, rollback)

    def _core_update_finished(
        self,
        result: CoreUpdateResult,
        manual: bool,
        install: bool,
        rollback: bool,
    ) -> None:
        self._core_checking = False
        self._core_installing = False
        self._refresh_controls()
        self._append(result.message)
        self.SetStatusText(f"版本 {VERSION}，核心 {self._core_version_text()}")
        if self._closing:
            return

        if manual and result.available and not install and not rollback:
            answer = wx.MessageBox(
                f"发现下载核心新版本 {result.latest_version}。\n"
                f"当前版本：{result.current_version}\n\n"
                "是否现在下载、校验 SHA-256 并安装？",
                "发现下载核心更新",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
                self,
            )
            if answer == wx.YES:
                self._core_checking = True
                self._core_installing = True
                self._refresh_controls()
                self._append("正在下载并安装下载核心更新……")
                threading.Thread(
                    target=self._core_update_worker,
                    kwargs={"manual": True, "install": True, "rollback": False},
                    daemon=True,
                ).start()
                return
            self._append("已取消本次下载核心更新。")
            self.SetStatusText("已取消下载核心更新")
            self.core_button.SetFocus()
            return

        if manual:
            if install:
                title = "下载核心更新完成" if result.ok else "下载核心更新失败"
                focus_target = self.core_button
            else:
                title = "下载核心更新检查" if result.ok else "下载核心更新检查失败"
                focus_target = self.core_button
            wx.MessageBox(
                result.message,
                title,
                wx.OK | (wx.ICON_INFORMATION if result.ok else wx.ICON_ERROR),
                self,
            )
            focus_target.SetFocus()

    @staticmethod
    def _safe_message(message: str) -> str:
        value = re.sub(
            r"https?://[^\s\]>)'\"]+",
            lambda match: safe_url_for_log(match.group(0)),
            message,
        )
        value = re.sub(
            r"(?i)\b(cookie|token|decode[_-]?key|authorization)(\s*[:=]\s*)\S+",
            r"\1\2<redacted>",
            value,
        )
        return value

    def _on_exit_button(self, _event: wx.Event) -> None:
        self.Close()

    def _on_close(self, event: wx.CloseEvent) -> None:
        self._sync_completed_task_state()
        if self._core_installing or self._software_update_blocks_ui:
            wx.MessageBox(
                "更新检查正在进行，请等待它完成后再退出。",
                APP_NAME,
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            event.Veto()
            return
        if self._busy:
            answer = wx.MessageBox(
                "任务正在进行，是否取消任务并退出？",
                APP_NAME,
                wx.YES_NO | wx.ICON_WARNING,
                self,
            )
            if answer != wx.YES:
                event.Veto()
                return
            self._closing = True
            self._coordinator.cancel()
            if self._prepared_scan:
                self._prepared_scan.clear_sensitive()
                self._prepared_scan = None
            self._set_busy(False)
            self._append("正在取消任务并退出。")
            event.Skip()
            return
        self._closing = True
        if self._prepared_scan:
            self._prepared_scan.clear_sensitive()
            self._prepared_scan = None
        event.Skip()


class FeichuanApp(wx.App):
    def __init__(self, *args: object, single_instance_guard: object = None, **kwargs: object) -> None:
        self._single_instance_guard = single_instance_guard
        super().__init__(*args, **kwargs)

    def OnInit(self) -> bool:  # noqa: N802 - wx API name
        self.SetAppName(APP_NAME)
        frame = MainFrame()
        self.SetTopWindow(frame)
        frame.Show()
        return True


def run(single_instance_guard: object = None) -> int:
    app = FeichuanApp(False, single_instance_guard=single_instance_guard)
    app.MainLoop()
    return 0
