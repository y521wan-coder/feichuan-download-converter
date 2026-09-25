"""Offline checks for update prompts, confirmations, and focus behavior."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import wx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["FEICHUAN_AUTO_UPDATE_CORE"] = "0"
os.environ["FEICHUAN_SOFTWARE_UPDATE_ENDPOINT"] = ""
_SETTINGS_ROOT = tempfile.TemporaryDirectory(prefix="feichuan-gui-update-settings-")
_SETTINGS_PATH = Path(_SETTINGS_ROOT.name) / "settings.json"
_SETTINGS_PATH.write_text(
    json.dumps({"usage_guide_seen_versions": ["1.1"]}, ensure_ascii=False),
    encoding="utf-8",
)
os.environ["FEICHUAN_SETTINGS_PATH"] = str(_SETTINGS_PATH)

import feichuan_downloader.gui as gui  # noqa: E402
from feichuan_downloader.core_updater import CoreUpdateResult  # noqa: E402
from feichuan_downloader.models import DownloadEvent, DownloadStage  # noqa: E402
from feichuan_downloader.software_updater import SoftwareUpdateResult  # noqa: E402


def assert_title(message_box: object, expected: str) -> None:
    assert message_box.call_count == 1
    assert message_box.call_args.args[1] == expected


class FakeCloseEvent:
    def __init__(self) -> None:
        self.skipped = False
        self.vetoed = False

    def Skip(self) -> None:  # noqa: N802 - wx-compatible test double
        self.skipped = True

    def Veto(self) -> None:  # noqa: N802 - wx-compatible test double
        self.vetoed = True


def main() -> None:
    app = wx.App(False)
    with patch.object(gui.MainFrame, "_startup_core_check"), patch.object(
        gui.MainFrame, "_startup_software_update_check"
    ), patch.object(gui.CoreUpdater, "current_version", return_value="2026.07.11"):
        frame = gui.MainFrame()
        frame.Show()
        wx.Yield()
        try:
            latest_software = SoftwareUpdateResult(
                ok=True,
                available=False,
                message="当前已是最新版本。",
            )
            frame.software_button.SetFocus()
            wx.Yield()
            frame._software_checking = True
            with patch.object(gui.wx, "MessageBox") as message_box:
                frame._software_update_checked(latest_software, manual=False)
                message_box.assert_not_called()
            wx.Yield()
            assert wx.Window.FindFocus() is frame.software_button

            frame._software_checking = True
            with patch.object(gui.wx, "MessageBox", return_value=wx.OK) as message_box:
                frame._software_update_checked(latest_software, manual=True)
                assert_title(message_box, "软件更新检查")

            failed_software = SoftwareUpdateResult(
                ok=False,
                available=False,
                message="检查软件更新失败：测试错误",
            )
            frame._software_checking = True
            with patch.object(gui.wx, "MessageBox", return_value=wx.OK) as message_box:
                frame._software_update_checked(failed_software, manual=True)
                assert_title(message_box, "软件更新检查失败")

            latest_core = CoreUpdateResult(
                ok=True,
                updated=False,
                current_version="2026.07.11",
                latest_version="2026.07.11",
                message="下载核心已是最新稳定版 2026.07.11。",
            )
            frame.core_button.SetFocus()
            wx.Yield()
            frame._core_checking = True
            with patch.object(gui.wx, "MessageBox") as message_box:
                frame._core_update_finished(latest_core, False, False, False)
                message_box.assert_not_called()
            wx.Yield()
            assert wx.Window.FindFocus() is frame.core_button

            frame._core_checking = True
            frame._core_installing = True
            with patch.object(gui.wx, "MessageBox", return_value=wx.OK) as message_box:
                frame._core_update_finished(latest_core, True, False, False)
                assert_title(message_box, "下载核心更新检查")

            available_core = CoreUpdateResult(
                ok=True,
                updated=False,
                current_version="2026.07.11",
                latest_version="2026.07.12",
                available=True,
                message="发现下载核心新版本 2026.07.12。",
            )
            frame._core_checking = True
            frame._core_installing = True
            with patch.object(gui.wx, "MessageBox", return_value=wx.NO) as message_box:
                frame._core_update_finished(available_core, True, False, False)
                assert_title(message_box, "发现下载核心更新")

            frame._core_checking = True
            frame._core_installing = True
            with patch.object(gui.wx, "MessageBox", return_value=wx.YES), patch.object(
                gui.threading, "Thread"
            ) as thread_class:
                frame._core_update_finished(available_core, True, False, False)
                thread_class.return_value.start.assert_called_once()
                assert frame._core_checking and frame._core_installing
            frame._core_checking = False
            frame._core_installing = False
            frame._refresh_controls()

            installed_core = CoreUpdateResult(
                ok=True,
                updated=True,
                current_version="2026.07.12",
                latest_version="2026.07.12",
                message="下载核心已更新到 2026.07.12。",
            )
            with patch.object(gui.wx, "MessageBox", return_value=wx.OK) as message_box:
                frame._core_update_finished(installed_core, True, True, False)
                assert_title(message_box, "下载核心更新完成")

            frame._set_busy(True)
            close_event = FakeCloseEvent()
            with patch.object(gui.wx, "MessageBox") as message_box:
                frame._on_close(close_event)
                message_box.assert_not_called()
            assert close_event.skipped and not close_event.vetoed
            assert not frame._busy
            frame._closing = False

            frame._set_busy(True)
            frame._coordinator._busy = True
            close_event = FakeCloseEvent()
            with patch.object(gui.wx, "MessageBox", return_value=wx.YES) as message_box:
                frame._on_close(close_event)
                assert_title(message_box, gui.APP_NAME)
            assert close_event.skipped and not close_event.vetoed
            assert not frame._busy
            assert frame._closing
            assert frame._coordinator.cancel_event.is_set()
            frame._coordinator._busy = False
            frame._closing = False

            frame._set_busy(True)
            frame._apply_download_event(
                DownloadEvent(
                    stage=DownloadStage.COMPLETED,
                    current=1,
                    total=1,
                    succeeded=1,
                    overall_percent=100.0,
                    message="下载完成。",
                )
            )
            assert not frame._busy
            assert not frame.cancel_button.IsEnabled()
            close_event = FakeCloseEvent()
            with patch.object(gui.wx, "MessageBox") as message_box:
                frame._on_close(close_event)
                message_box.assert_not_called()
            assert close_event.skipped and not close_event.vetoed
            frame._closing = False
        finally:
            frame.Destroy()
            app.Destroy()
    print("gui update prompt smoke passed")


if __name__ == "__main__":
    main()
