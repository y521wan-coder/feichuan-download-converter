"""Launch the current source tree and verify the 0.3.8 native controls."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pywinauto import Desktop


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    settings_root = tempfile.TemporaryDirectory(prefix="feichuan-gui-source-settings-")
    settings_path = Path(settings_root.name) / "settings.json"
    settings_path.write_text(
        json.dumps({"usage_guide_seen_versions": ["0.3.9"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "FEICHUAN_AUTO_UPDATE_CORE": "0",
            "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT": "0",
            "FEICHUAN_SETTINGS_PATH": str(settings_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "main.py")],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        window = Desktop(backend="win32").window(
            title="飞船下载工具",
            process=process.pid,
        )
        window.wait("visible", timeout=30)
        duplicate = subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "main.py")],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        duplicate.wait(timeout=10)
        assert duplicate.returncode == 0
        duplicate_windows = Desktop(backend="win32").windows(
            title="飞船下载工具",
            process=duplicate.pid,
        )
        assert not duplicate_windows, duplicate_windows
        names = [control.window_text() for control in window.descendants()]
        for expected in (
            "下载链接或平台分享文本",
            "开始下载",
            "取消任务",
            "选择下载文件夹(&D)",
            "打开下载文件夹(&O)",
            "当前下载文件夹",
            "品质和格式",
            "默认最高品质下载",
            "扫描数量",
            "任务计数",
            "当前文件",
            "当前进度摘要",
            "整体进度",
            "检查软件更新",
            "检查下载核心更新",
            "退出",
            "状态和日志",
            "喜欢这个作品",
        ):
            assert expected in names, (expected, names)
        for removed in ("链接下载", "视频号捕获", "启动视频号捕获"):
            assert removed not in names, (removed, names)
        assert "回退下载核心" not in names, names
        assert not window.descendants(class_name="SysTabControl32")
        assert not window.child_window(title="取消任务", class_name="Button").is_enabled()

        software_button = window.child_window(
            title="检查软件更新",
            class_name="Button",
        )
        time.sleep(2.2)
        software_button.wait("enabled", timeout=30)
        software_button.set_focus()
        software_button.click()
        software_dialog = Desktop(backend="win32").window(
            title="软件更新检查失败",
            process=process.pid,
        )
        software_dialog.wait("visible", timeout=10)
        software_dialog.child_window(class_name="Button").click()
        software_dialog.wait_not("exists", timeout=10)

        uia_windows = Desktop(backend="uia").windows(process=process.pid)
        uia_window = next(
            candidate
            for candidate in uia_windows
            if candidate.window_text() == "飞船下载工具"
        )
        controls = uia_window.descendants()
        choose_button = next(
            control
            for control in controls
            if control.element_info.control_type == "Button"
            and control.window_text().startswith("选择下载文件夹")
        )
        shortcut = str(
            choose_button.legacy_properties().get("KeyboardShortcut") or ""
        ).lower()
        assert shortcut == "alt+d", shortcut

        accessible_names = {
            str(control.legacy_properties().get("Name") or "")
            for control in controls
            if control.element_info.control_type in {"Edit", "ProgressBar", "ComboBox"}
        }
        for expected_name in (
            "当前下载文件夹",
            "扫描数量",
            "任务计数",
            "当前文件",
            "当前进度摘要",
            "整体进度",
            "品质和格式",
        ):
            assert expected_name in accessible_names, accessible_names

        donation_button = window.child_window(
            title="喜欢这个作品",
            class_name="Button",
        )
        donation_button.set_focus()
        donation_button.click()
        donation_dialog = Desktop(backend="win32").window(
            title="谢谢喜欢",
            process=process.pid,
        )
        donation_dialog.wait("visible", timeout=10)
        donation_names = [
            control.window_text() for control in donation_dialog.descendants()
        ]
        assert "喜欢就随心支持一下" in donation_names, donation_names
        donation_dialog.child_window(title="关闭", class_name="Button").click()
        donation_dialog.wait_not("exists", timeout=10)

        window.child_window(title="退出", class_name="Button").click()
        window.wait_not("exists", timeout=30)
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        settings_root.cleanup()
    print("source gui smoke passed")


if __name__ == "__main__":
    main()
