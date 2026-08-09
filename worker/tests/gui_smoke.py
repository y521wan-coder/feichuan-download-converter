"""启动绿色版并检查原生控件可见、可访问，以及退出按钮行为。"""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
from pathlib import Path

from pywinauto import Desktop
from pywinauto.application import Application
from pywinauto.timings import wait_until, wait_until_passes
import win32gui


def main() -> None:
    exe = Path(__file__).resolve().parents[1] / "dist" / "飞船下载工具.exe"
    previous_environment = {
        name: os.environ.get(name)
        for name in (
            "FEICHUAN_AUTO_UPDATE_CORE",
            "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT",
            "FEICHUAN_SETTINGS_PATH",
        )
    }
    settings_root = tempfile.TemporaryDirectory(prefix="feichuan-gui-settings-")
    settings_path = Path(settings_root.name) / "settings.json"
    settings_path.write_text(
        json.dumps({"usage_guide_seen_versions": ["0.3.9"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    existing_window_pids = {
        window.process_id()
        for window in Desktop(backend="win32").windows(title="飞船下载工具")
    }
    app = None
    window_pid = 0
    os.environ["FEICHUAN_AUTO_UPDATE_CORE"] = "0"
    os.environ["FEICHUAN_SOFTWARE_UPDATE_ENDPOINT"] = "0"
    os.environ["FEICHUAN_SETTINGS_PATH"] = str(settings_path)
    try:
        app = Application(backend="win32").start(str(exe))
    finally:
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    def find_started_window_pid():
        candidates = [
            window
            for window in Desktop(backend="win32").windows(title="飞船下载工具")
            if window.process_id() not in existing_window_pids
        ]
        if not candidates:
            raise RuntimeError("started window is not visible yet")
        return candidates[0].process_id()

    window_pid = wait_until_passes(30, 0.2, find_started_window_pid)
    window = Desktop(backend="win32").window(
        title="飞船下载工具",
        process=window_pid,
    )
    names = [control.window_text() for control in window.descendants()]
    for expected in (
        "开始下载",
        "选择下载文件夹(&D)",
        "打开下载文件夹(&O)",
        "检查软件更新",
        "检查下载核心更新",
        "品质和格式",
        "默认最高品质下载",
        "退出",
        "状态和日志",
        "当前进度摘要",
        "喜欢这个作品",
    ):
        assert expected in names, (expected, names)
    for removed in ("链接下载", "视频号捕获", "启动视频号捕获"):
        assert removed not in names, (removed, names)
    assert "回退下载核心" not in names, names
    assert not window.descendants(class_name="SysTabControl32")
    edit_controls = window.descendants(class_name="Edit")
    assert edit_controls, names
    edit_controls[0].set_focus()
    window.child_window(
        title="检查软件更新",
        class_name="Button",
    ).wait("enabled", timeout=45)
    exit_button = window.child_window(title="退出", class_name="Button")
    handle = window.wrapper_object().handle
    # Post a button click directly; physical click_input can land on Codex when
    # Windows refuses to foreground the freshly spawned one-file executable.
    try:
        exit_button.click()
        wait_until(30, 0.2, lambda: not win32gui.IsWindow(handle))
    finally:
        if window_pid:
            subprocess.run(
                ["taskkill", "/PID", str(window_pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if app is not None:
            if app.process != window_pid:
                subprocess.run(
                    ["taskkill", "/PID", str(app.process), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            app.kill()
        settings_root.cleanup()
    print("gui smoke passed")


if __name__ == "__main__":
    main()
