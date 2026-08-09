"""飞船下载工具 0.3.5 抖音 Playlet 的真实安装与下载验收。

这个脚本会覆盖当前用户安装、访问真实抖音页面并全量重新下载 16 个视频，
因此绝不加入离线 smoke。默认只显示安全说明；必须显式传入 ``--execute``。

安全边界：

* 只接受已校验 SHA-256 的 0.3.5 安装包，并要求当前用户版仍为 0.3.4；
* 发现飞船下载工具、安装器、专用 Chrome 或 D:\\ 根目录遗留 ``*.part`` 时拒绝执行；
* 安装器使用 ``/NOCLOSEAPPLICATIONS``，不会替用户关闭正在运行的程序；
* 不删除任何下载文件，不清空数据库，不调用软件更新或上传接口；
* 下载超时或状态未知时保留应用运行，禁止强杀可能仍在写文件的进程；
* 完成后只读核对 GUI、SQLite、D:\\ 文件和 ffprobe 结果。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import win32api
import win32con
import win32gui
import win32process
from pywinauto import Desktop


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "飞船下载工具"
APP_EXE_NAME = f"{APP_NAME}.exe"
FROM_VERSION = "0.3.4"
TO_VERSION = "0.3.5"
EXPECTED_COUNT = 16
SHARE_URL = "https://v.douyin.com/O10x-gBdj24/"
DOWNLOAD_ROOT = Path("D:\\")
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / APP_NAME
INSTALLER = ROOT / "release" / f"{APP_NAME}-Setup-{TO_VERSION}.exe"
DIST_EXE = ROOT / "dist" / APP_EXE_NAME
APP_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "FeichuanDownloader"
STATE_DB = APP_DATA / "state.sqlite3"
SETTINGS = APP_DATA / "settings.json"
DOUYIN_PROFILE = APP_DATA / "DouyinChromeProfile"
UNINSTALL_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{1E97F30E-8634-4032-A5A2-715394D9A471}_is1"
)
MIN_FREE_BYTES = 5 * 1024**3
WX_ID_OK = 5100
WX_ID_CANCEL = 5101


class AcceptanceError(RuntimeError):
    """验收因安全门或断言失败而停止。"""


@dataclass(frozen=True, slots=True)
class FileStamp:
    size: int
    mtime_ns: int
    mode: int


@dataclass(frozen=True, slots=True)
class ArtifactState:
    artifact_key: str
    file_path: str
    validation_status: str
    file_size: int | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class WorkState:
    database_id: int
    platform: str
    work_id: str
    source_kind: str
    content_type: str
    canonical_url: str
    updated_at: str
    artifacts: tuple[ArtifactState, ...]


@dataclass(slots=True)
class GuiSession:
    launcher: subprocess.Popen[bytes]
    pid: int
    win32_window: Any
    uia_window: Any


def _normalized(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _optional_digest(path: Path) -> tuple[bool, str]:
    return (path.is_file(), _sha256(path) if path.is_file() else "")


def _fixed_file_version(path: Path) -> str:
    try:
        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms = int(info["FileVersionMS"])
        ls = int(info["FileVersionLS"])
    except Exception as exc:  # pragma: no cover - Windows API diagnostic
        raise AcceptanceError(f"无法读取安装包版本资源：{path}: {exc}") from exc
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _validate_installer(installer: Path) -> str:
    if not installer.is_file():
        raise AcceptanceError(f"找不到 0.3.5 安装包：{installer}")
    if installer.name != f"{APP_NAME}-Setup-{TO_VERSION}.exe":
        raise AcceptanceError(f"安装包文件名不是预期的 0.3.5 产物：{installer.name}")
    sidecar = installer.with_name(installer.name + ".sha256")
    if not sidecar.is_file():
        raise AcceptanceError(f"缺少安装包 SHA-256 校验文件：{sidecar}")
    match = re.search(r"(?i)\b([0-9a-f]{64})\b", sidecar.read_text(encoding="utf-8"))
    if match is None:
        raise AcceptanceError(f"SHA-256 校验文件格式无效：{sidecar}")
    expected = match.group(1).upper()
    actual = _sha256(installer)
    if actual != expected:
        raise AcceptanceError(f"安装包 SHA-256 不匹配：期望 {expected}，实际 {actual}")
    version = _fixed_file_version(installer)
    if version != f"{TO_VERSION}.0":
        raise AcceptanceError(f"安装包版本资源应为 {TO_VERSION}.0，实际为 {version}")
    return actual


def _registry_values(root: int, subkey: str, view: int) -> dict[str, str] | None:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            values: dict[str, str] = {}
            for name in (
                "DisplayName",
                "DisplayVersion",
                "InstallLocation",
                "UninstallString",
            ):
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                except FileNotFoundError:
                    values[name] = ""
            return values
    except FileNotFoundError:
        return None


def _current_user_install() -> dict[str, str]:
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        values = _registry_values(winreg.HKEY_CURRENT_USER, UNINSTALL_SUBKEY, view)
        if values is not None:
            return values
    raise AcceptanceError("找不到当前用户版飞船下载工具的固定 AppId 安装记录。")


def _machine_install_snapshot() -> tuple[tuple[str, ...], ...]:
    base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    found: list[tuple[str, ...]] = []
    for view_name, view in (
        ("64", winreg.KEY_WOW64_64KEY),
        ("32", winreg.KEY_WOW64_32KEY),
    ):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                base,
                0,
                winreg.KEY_READ | view,
            ) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    name = winreg.EnumKey(parent, index)
                    values = _registry_values(
                        winreg.HKEY_LOCAL_MACHINE,
                        f"{base}\\{name}",
                        view,
                    )
                    if values and APP_NAME in values.get("DisplayName", ""):
                        found.append(
                            (
                                view_name,
                                name,
                                values.get("DisplayName", ""),
                                values.get("DisplayVersion", ""),
                                values.get("InstallLocation", ""),
                                values.get("UninstallString", ""),
                            )
                        )
        except FileNotFoundError:
            continue
    return tuple(sorted(found))


def _process_images() -> tuple[tuple[int, Path], ...]:
    rows: list[tuple[int, Path]] = []
    rights = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    for pid in win32process.EnumProcesses():
        if not pid:
            continue
        handle = None
        try:
            handle = win32api.OpenProcess(rights, False, int(pid))
            image = win32process.GetModuleFileNameEx(handle, 0)
            if image:
                rows.append((int(pid), Path(image)))
        except Exception:
            continue
        finally:
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
    return tuple(rows)


def _dedicated_chrome_pids() -> tuple[int, ...]:
    profile = str(DOUYIN_PROFILE).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if completed.returncode != 0:
        raise AcceptanceError("无法确认抖音专用 Chrome 是否正在运行，拒绝继续。")
    return tuple(
        int(line.strip())
        for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    )


def _assert_no_process_conflicts() -> None:
    conflicts: list[str] = []
    for pid, image in _process_images():
        name = image.name.casefold()
        if name == APP_EXE_NAME.casefold() or (
            name.startswith(f"{APP_NAME}-setup-".casefold()) and name.endswith(".exe")
        ):
            conflicts.append(f"PID {pid}: {image}")
    try:
        for window in Desktop(backend="win32").windows(title=APP_NAME):
            conflicts.append(f"窗口 PID {window.process_id()}: {APP_NAME}")
    except Exception as exc:
        raise AcceptanceError(f"无法检查现有飞船下载工具窗口：{exc}") from exc
    chrome_pids = _dedicated_chrome_pids()
    if chrome_pids:
        conflicts.append(f"抖音专用 Chrome PID：{', '.join(map(str, chrome_pids))}")
    if conflicts:
        raise AcceptanceError("发现可能正在工作的相关进程，未安装也未下载：\n" + "\n".join(conflicts))


def _tree_stamp(root: Path) -> tuple[tuple[str, FileStamp], ...]:
    if not root.exists():
        return ()
    rows: list[tuple[str, FileStamp]] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort(key=str.casefold)
        for name in sorted(filenames, key=str.casefold):
            path = Path(directory) / name
            try:
                info = path.stat()
            except OSError as exc:
                raise AcceptanceError(f"无法读取保留目录中的文件状态：{path}: {exc}") from exc
            rows.append(
                (
                    os.path.relpath(path, root),
                    FileStamp(info.st_size, info.st_mtime_ns, info.st_mode),
                )
            )
    return tuple(rows)


def _root_file_snapshot(root: Path) -> dict[str, tuple[Path, FileStamp]]:
    rows: dict[str, tuple[Path, FileStamp]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise AcceptanceError(f"无法读取下载根目录 {root}: {exc}") from exc
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise AcceptanceError(f"无法读取 D:\\ 根目录项：{entry.path}: {exc}") from exc
        if stat.S_ISDIR(info.st_mode):
            continue
        path = Path(entry.path)
        rows[_normalized(path)] = (
            path,
            FileStamp(info.st_size, info.st_mtime_ns, info.st_mode),
        )
    return rows


def _part_files() -> tuple[Path, ...]:
    return tuple(
        path
        for path, _stamp in _root_file_snapshot(DOWNLOAD_ROOT).values()
        if path.name.casefold().endswith(".part")
    )


def _database_snapshot(path: Path) -> dict[tuple[str, str], WorkState]:
    if not path.is_file():
        return {}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            work_rows = connection.execute(
                """
                SELECT id, platform, work_id, source_kind, content_type,
                       canonical_url, updated_at
                FROM works
                ORDER BY id
                """
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT work_database_id, artifact_key, file_path,
                       validation_status, file_size, updated_at
                FROM artifacts
                ORDER BY work_database_id, artifact_key
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise AcceptanceError(f"无法只读检查状态数据库：{path}: {exc}") from exc
    artifacts: dict[int, list[ArtifactState]] = {}
    for row in artifact_rows:
        artifacts.setdefault(int(row["work_database_id"]), []).append(
            ArtifactState(
                artifact_key=str(row["artifact_key"]),
                file_path=str(row["file_path"]),
                validation_status=str(row["validation_status"]),
                file_size=(int(row["file_size"]) if row["file_size"] is not None else None),
                updated_at=str(row["updated_at"]),
            )
        )
    result: dict[tuple[str, str], WorkState] = {}
    for row in work_rows:
        database_id = int(row["id"])
        state = WorkState(
            database_id=database_id,
            platform=str(row["platform"]),
            work_id=str(row["work_id"]),
            source_kind=str(row["source_kind"]),
            content_type=str(row["content_type"]),
            canonical_url=str(row["canonical_url"]),
            updated_at=str(row["updated_at"]),
            artifacts=tuple(artifacts.get(database_id, ())),
        )
        result[(state.platform, state.work_id)] = state
    return result


def _assert_initial_install() -> dict[str, str]:
    current = _current_user_install()
    if current.get("DisplayVersion") != FROM_VERSION:
        raise AcceptanceError(
            f"当前用户版必须是 {FROM_VERSION}，实际为 "
            f"{current.get('DisplayVersion') or '未知'}；为避免重复覆盖，拒绝继续。"
        )
    actual_dir = Path(current.get("InstallLocation", ""))
    if _normalized(actual_dir) != _normalized(INSTALL_DIR):
        raise AcceptanceError(f"当前用户安装目录不是计划路径：{actual_dir}")
    if not (actual_dir / APP_EXE_NAME).is_file():
        raise AcceptanceError(f"当前安装记录缺少主程序：{actual_dir / APP_EXE_NAME}")
    return current


def _run_installer(installer: Path) -> Path:
    log_path = Path(os.environ.get("TEMP", str(ROOT))) / (
        f"feichuan-live-acceptance-install-{TO_VERSION}-{int(time.time())}.log"
    )
    command = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/NOCLOSEAPPLICATIONS",
        "/NORESTARTAPPLICATIONS",
        f"/LOG={log_path}",
    ]
    print(f"[INSTALL] 静默覆盖当前用户版；安装日志：{log_path}", flush=True)
    process = subprocess.Popen(command, cwd=installer.parent)
    deadline = time.monotonic() + 20 * 60
    while process.poll() is None:
        if time.monotonic() >= deadline:
            raise AcceptanceError(
                f"安装器 20 分钟内未结束。为避免破坏安装，没有强杀 PID {process.pid}。"
            )
        time.sleep(1)
    if process.returncode != 0:
        raise AcceptanceError(
            f"安装器退出码为 {process.returncode}；请检查日志 {log_path}"
        )
    return log_path


def _wait_for_main_window(launcher: subprocess.Popen[bytes], timeout: float = 90) -> GuiSession:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            candidates = Desktop(backend="win32").windows(title=APP_NAME)
            if candidates:
                candidate = candidates[0]
                pid = int(candidate.process_id())
                window = Desktop(backend="win32").window(handle=candidate.handle)
                _wait_visible(window, timeout=5)
                uia = Desktop(backend="uia").window(title=APP_NAME, process=pid)
                _wait_visible(uia, timeout=15)
                return GuiSession(launcher, pid, window, uia)
        except Exception as exc:
            last_error = str(exc)
        if launcher.poll() is not None and not Desktop(backend="win32").windows(title=APP_NAME):
            raise AcceptanceError(f"主程序在显示窗口前退出，退出码 {launcher.returncode}。")
        time.sleep(0.5)
    raise AcceptanceError(f"等待主窗口超时：{last_error or '没有发现窗口'}")


def _legacy_name(control: Any) -> str:
    try:
        return str(control.legacy_properties().get("Name") or "")
    except Exception:
        return ""


def _window_handle(control: Any) -> int:
    try:
        return int(getattr(control, "handle", 0) or 0)
    except Exception:
        return 0


def _control_exists(control: Any) -> bool:
    handle = _window_handle(control)
    if handle:
        return bool(win32gui.IsWindow(handle))
    exists = getattr(control, "exists", None)
    if callable(exists):
        try:
            return bool(exists())
        except Exception:
            return False
    return True


def _wait_visible(control: Any, timeout: float) -> None:
    wait = getattr(control, "wait", None)
    if callable(wait):
        wait("visible", timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _control_exists(control) and bool(control.is_visible()):
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AcceptanceError("等待窗口可见超时。")


def _wait_gone(control: Any, timeout: float) -> None:
    wait_not = getattr(control, "wait_not", None)
    if callable(wait_not):
        wait_not("exists", timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _control_exists(control):
            return
        time.sleep(0.1)
    raise AcceptanceError("等待窗口关闭超时。")


def _named_uia(window: Any, name: str, control_type: str | None = None) -> Any:
    accepted_names = {
        "下载链接输入框": {"下载链接输入框", "下载链接或平台分享文本"},
        "状态和日志区域": {"状态和日志区域", "状态和日志"},
    }.get(name, {name})
    for control in window.descendants():
        actual_type = control.element_info.control_type
        type_matches = not control_type or actual_type == control_type
        if name == "状态和日志区域" and actual_type == "Document":
            type_matches = True
        if not type_matches:
            continue
        if _legacy_name(control) in accepted_names:
            return control
    raise AcceptanceError(f"找不到可访问控件：{name}")


def _control_value(control: Any) -> str:
    try:
        return str(control.get_value())
    except Exception:
        return str(control.window_text())


def _window_texts(window: Any) -> tuple[str, ...]:
    texts: list[str] = []
    try:
        own = str(window.window_text()).strip()
        if own:
            texts.append(own)
    except Exception:
        pass
    for control in window.descendants():
        try:
            value = str(control.window_text()).strip()
        except Exception:
            continue
        if value:
            texts.append(value)
    return tuple(texts)


def _launch_installed_app() -> GuiSession:
    executable = INSTALL_DIR / APP_EXE_NAME
    environment = os.environ.copy()
    environment.update(
        {
            "FEICHUAN_AUTO_UPDATE_CORE": "0",
            "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT": "0",
            "FEICHUAN_DOWNLOAD_DIR": str(DOWNLOAD_ROOT),
        }
    )
    launcher = subprocess.Popen(
        [str(executable)],
        cwd=INSTALL_DIR,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    session = _wait_for_main_window(launcher)
    if not any(f"版本 {TO_VERSION}" in text for text in _window_texts(session.win32_window)):
        raise AcceptanceError(f"已安装 GUI 没有显示版本 {TO_VERSION}。")
    displayed_dir = _control_value(_named_uia(session.uia_window, "当前下载文件夹", "Edit"))
    if _normalized(displayed_dir) != _normalized(DOWNLOAD_ROOT):
        raise AcceptanceError(f"GUI 下载目录不是 D:\\：{displayed_dir}")
    return session


def _set_link_and_start(session: GuiSession) -> None:
    input_box = _named_uia(session.uia_window, "下载链接输入框", "Edit")
    input_box.set_edit_text(SHARE_URL)
    if _control_value(input_box).strip() != SHARE_URL:
        raise AcceptanceError("GUI 下载链接输入框未能可靠写入验收短链。")

    profile_checkbox = next(
        (
            control
            for control in session.win32_window.descendants(class_name="Button")
            if control.window_text().startswith("使用软件专用 Chrome 登录抖音")
        ),
        None,
    )
    if profile_checkbox is None:
        raise AcceptanceError("找不到抖音专用登录选项。")
    if not DOUYIN_PROFILE.is_dir() or profile_checkbox.get_check_state() != 1:
        raise AcceptanceError("真实验收要求已有且已勾选的软件专用抖音登录资料。")

    start = session.win32_window.child_window(title="开始下载", class_name="Button")
    start.wait("enabled", timeout=45)
    start.click()


def _log_text(session: GuiSession) -> str:
    return _control_value(_named_uia(session.uia_window, "状态和日志区域", "Edit"))


def _wait_for_confirm_dialog(session: GuiSession, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    next_report = time.monotonic() + 30
    while time.monotonic() < deadline:
        dialogs = Desktop(backend="win32").windows(
            title="确认批量下载",
            process=session.pid,
        )
        if dialogs:
            dialog = Desktop(backend="win32").window(handle=dialogs[0].handle)
            _wait_visible(dialog, timeout=10)
            return dialog
        if not _control_exists(session.win32_window):
            raise AcceptanceError("扫描期间主窗口意外退出。")
        log = _log_text(session)
        if "任务失败：" in log or "任务已取消。" in log:
            raise AcceptanceError("扫描没有进入批量确认：" + log[-800:])
        if time.monotonic() >= next_report:
            print("[SCAN] 仍在等待抖音 Playlet 完整扫描和确认窗口……", flush=True)
            next_report = time.monotonic() + 30
        time.sleep(1)
    raise AcceptanceError("等待抖音批量确认窗口超时；没有强制结束仍可能活动的扫描任务。")


def _validate_and_accept_dialog(dialog: Any) -> None:
    summary = "\n".join(_window_texts(dialog))
    required = (
        "页面报告数量：16",
        "实际发现数量：16",
        "视频 16",
        "扫描已收到 has_more=false",
    )
    missing = [text for text in required if text not in summary]
    if missing:
        raise AcceptanceError(
            "批量确认摘要不符合 16 集完整视频验收；缺少：" + "、".join(missing)
        )

    buttons = dialog.descendants(class_name="Button")
    radio = next(
        (button for button in buttons if button.window_text().startswith("全量重新下载")),
        None,
    )
    if radio is None:
        raise AcceptanceError("确认窗口没有“全量重新下载”选项。")
    if "将下载 16 条" not in radio.window_text():
        raise AcceptanceError(f"全量模式预览不是 16 条：{radio.window_text()}")
    radio.click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and radio.get_check_state() != 1:
        time.sleep(0.2)
    if radio.get_check_state() != 1:
        raise AcceptanceError("无法可靠选中“全量重新下载”。")

    ok = dialog.child_window(control_id=WX_ID_OK, class_name="Button")
    ok.wait("enabled", timeout=10)
    ok.click()
    _wait_gone(dialog, timeout=30)


_FINISHED_RE = re.compile(
    r"批量任务完成：成功\s*(\d+)\s*，跳过\s*(\d+)\s*，失败\s*(\d+)\s*。"
)


def _wait_for_download(session: GuiSession, timeout: float) -> tuple[int, int, int]:
    deadline = time.monotonic() + timeout
    next_report = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not _control_exists(session.win32_window):
            raise AcceptanceError("下载期间主窗口意外退出。")
        log = _log_text(session)
        matches = list(_FINISHED_RE.finditer(log))
        if matches:
            match = matches[-1]
            return tuple(int(match.group(index)) for index in (1, 2, 3))
        if time.monotonic() >= next_report:
            try:
                queue = _control_value(_named_uia(session.uia_window, "任务计数", "Edit"))
            except Exception:
                queue = "状态暂不可读"
            print(f"[DOWNLOAD] {queue}", flush=True)
            next_report = time.monotonic() + 30
        time.sleep(1)
    raise AcceptanceError(
        "等待 16 个视频下载完成超时。为避免损坏仍在写入的文件，应用保持运行。"
    )


def _changed_works(
    before: dict[tuple[str, str], WorkState],
    after: dict[tuple[str, str], WorkState],
) -> tuple[WorkState, ...]:
    changed_keys = {
        key
        for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    }
    missing = [key for key in changed_keys if key not in after]
    if missing:
        raise AcceptanceError(f"本次运行删除了数据库作品记录：{missing}")
    changed = tuple(after[key] for key in sorted(changed_keys))
    if len(changed) != EXPECTED_COUNT:
        raise AcceptanceError(
            f"本次 SQLite 变化应只对应 16 个作品，实际为 {len(changed)} 个。"
        )
    for work in changed:
        if (
            work.platform != "douyin"
            or work.source_kind != "douyin_collection"
            or work.content_type != "video"
        ):
            raise AcceptanceError(
                f"本次出现非预期数据库作品：{work.platform}/{work.source_kind}/"
                f"{work.content_type}/{work.work_id}"
            )
        if not re.fullmatch(r"\d+", work.work_id):
            raise AcceptanceError(f"抖音作品 ID 不是稳定数字 ID：{work.work_id}")
        if "?" in work.canonical_url:
            raise AcceptanceError(f"SQLite 规范链接含 query：{work.work_id}")
        if len(work.artifacts) != 1 or work.artifacts[0].artifact_key != "video":
            raise AcceptanceError(f"作品 {work.work_id} 不是恰好一个视频文件记录。")
        artifact = work.artifacts[0]
        if artifact.validation_status != "valid":
            raise AcceptanceError(
                f"作品 {work.work_id} 的 SQLite 文件状态不是 valid："
                f"{artifact.validation_status}"
            )
    return changed


def _verify_files(
    works: Iterable[WorkState],
    root_before: dict[str, tuple[Path, FileStamp]],
) -> tuple[Path, ...]:
    files: list[Path] = []
    expected_paths: set[str] = set()
    root_norm = _normalized(DOWNLOAD_ROOT)
    for work in works:
        artifact = work.artifacts[0]
        path = Path(artifact.file_path)
        if _normalized(path.parent) != root_norm:
            raise AcceptanceError(f"作品 {work.work_id} 没有保存到 D:\\ 根目录：{path}")
        if path.suffix.casefold() != ".mp4" or f"_{work.work_id}_" not in path.name:
            raise AcceptanceError(f"作品 {work.work_id} 的文件名不符合稳定 ID 命名：{path.name}")
        if not path.is_file():
            raise AcceptanceError(f"SQLite 标记有效但文件不存在：{path}")
        size = path.stat().st_size
        if size <= 0 or artifact.file_size != size:
            raise AcceptanceError(
                f"作品 {work.work_id} 文件大小无效或与 SQLite 不一致：{size}/"
                f"{artifact.file_size}"
            )
        matching = [
            candidate
            for candidate in DOWNLOAD_ROOT.glob("*.mp4")
            if f"_{work.work_id}_" in candidate.name
        ]
        if len(matching) != 1 or _normalized(matching[0]) != _normalized(path):
            raise AcceptanceError(f"作品 {work.work_id} 在 D:\\ 中存在重复或路径不一致。")
        files.append(path)
        expected_paths.add(_normalized(path))

    if len(files) != EXPECTED_COUNT or len(expected_paths) != EXPECTED_COUNT:
        raise AcceptanceError("最终文件路径或作品 ID 不是 16 个互不重复的视频。")
    leftovers = _part_files()
    if leftovers:
        raise AcceptanceError("下载完成后仍有 .part 文件：" + "、".join(map(str, leftovers)))

    root_after = _root_file_snapshot(DOWNLOAD_ROOT)
    missing_baseline = sorted(set(root_before) - set(root_after))
    if missing_baseline:
        raise AcceptanceError(
            "下载期间有原 D:\\ 根目录文件消失："
            + "、".join(str(root_before[key][0]) for key in missing_baseline)
        )
    unexpected_new = sorted(set(root_after) - set(root_before) - expected_paths)
    if unexpected_new:
        raise AcceptanceError(
            "下载期间新增了非本次 16 个视频的根目录文件："
            + "、".join(str(root_after[key][0]) for key in unexpected_new)
        )
    unexpected_changed = sorted(
        key
        for key in set(root_before) & set(root_after)
        if root_before[key][1] != root_after[key][1] and key not in expected_paths
    )
    if unexpected_changed:
        raise AcceptanceError(
            "下载期间修改了非本次视频的根目录文件："
            + "、".join(str(root_after[key][0]) for key in unexpected_changed)
        )
    return tuple(files)


def _ffprobe_all(files: Iterable[Path]) -> None:
    ffprobe = INSTALL_DIR / "tools" / "ffprobe.exe"
    if not ffprobe.is_file():
        raise AcceptanceError(f"安装目录缺少 ffprobe：{ffprobe}")
    for index, path in enumerate(files, start=1):
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=90,
        )
        output = (completed.stdout or b"").decode("utf-8", errors="replace").split()
        if completed.returncode != 0 or "video" not in output:
            raise AcceptanceError(f"第 {index}/16 个文件未通过 ffprobe 视频轨道校验：{path}")
        print(f"[FFPROBE] {index:02d}/16 {path.name}", flush=True)


def _queue_value(session: GuiSession) -> str:
    return _control_value(_named_uia(session.uia_window, "任务计数", "Edit"))


def _assert_queue_success(session: GuiSession) -> None:
    value = _queue_value(session)
    pattern = re.compile(
        r"当前：16/16\s*成功：16\s*跳过：0\s*失败：0"
    )
    if pattern.search(value) is None:
        raise AcceptanceError(f"GUI 最终任务计数不符合验收：{value}")


def _cancel_waiting_dialog(dialog: Any) -> bool:
    try:
        if _control_exists(dialog):
            cancel = dialog.child_window(control_id=WX_ID_CANCEL, class_name="Button")
            cancel.click()
            _wait_gone(dialog, timeout=20)
            return True
    except Exception:
        return False
    return False


def _close_idle_app(session: GuiSession) -> bool:
    try:
        if not _control_exists(session.win32_window):
            return True
        start = session.win32_window.child_window(title="开始下载", class_name="Button")
        deadline = time.monotonic() + 20
        while session.win32_window.exists() and not start.is_enabled():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
        if not session.win32_window.exists():
            return True
        if not start.is_enabled():
            return False
        exit_button = session.win32_window.child_window(title="退出", class_name="Button")
        exit_button.click()
        _wait_gone(session.win32_window, timeout=45)
        return True
    except Exception:
        return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="明确执行覆盖安装和真实 16 集全量重新下载；不传时不做任何修改。",
    )
    parser.add_argument(
        "--resume-installed",
        action="store_true",
        help="安装已成功但 GUI 自动化中断时，从当前用户版 0.3.5 继续真实下载验收。",
    )
    parser.add_argument(
        "--installer",
        type=Path,
        default=INSTALLER,
        help="0.3.5 安装包路径；同目录必须有 .sha256 文件。",
    )
    parser.add_argument(
        "--scan-timeout-minutes",
        type=float,
        default=20.0,
        help="等待真实 Playlet 完整扫描的分钟数，默认 20。",
    )
    parser.add_argument(
        "--download-timeout-minutes",
        type=float,
        default=180.0,
        help="等待 16 个视频全部下载的分钟数，默认 180。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.execute:
        print(
            "安全门已生效：本次没有安装、没有启动程序、没有下载。\n"
            "确认当前没有飞船下载任务后，显式运行：\n"
            "  python tests\\live_douyin_playlet_acceptance.py --execute"
        )
        return 0
    if args.scan_timeout_minutes <= 0 or args.download_timeout_minutes <= 0:
        raise AcceptanceError("超时时间必须大于 0。")

    installer = args.installer.expanduser().resolve()
    installer_hash = _validate_installer(installer)
    _assert_no_process_conflicts()
    if args.resume_installed:
        initial_install = _current_user_install()
        if initial_install.get("DisplayVersion") != TO_VERSION:
            raise AcceptanceError(
                f"恢复验收要求当前用户版为 {TO_VERSION}，实际为 "
                f"{initial_install.get('DisplayVersion') or '未知'}。"
            )
        if _normalized(initial_install.get("InstallLocation", "")) != _normalized(
            INSTALL_DIR
        ):
            raise AcceptanceError("恢复验收时当前用户安装目录不是计划路径。")
    else:
        initial_install = _assert_initial_install()
    if not DOWNLOAD_ROOT.is_dir():
        raise AcceptanceError("D:\\ 下载根目录不存在或当前不可用。")
    free = shutil.disk_usage(DOWNLOAD_ROOT).free
    if free < MIN_FREE_BYTES:
        raise AcceptanceError(f"D:\\ 可用空间不足 5 GiB：{free} 字节")
    leftovers = _part_files()
    if leftovers:
        raise AcceptanceError("D:\\ 根目录已有 .part 文件，可能存在未完成任务：" + "、".join(map(str, leftovers)))
    if not DOUYIN_PROFILE.is_dir():
        raise AcceptanceError("缺少软件专用抖音登录资料，自动验收不会代替用户登录。")

    database_digest_before_install = _optional_digest(STATE_DB)
    settings_digest_before_install = _optional_digest(SETTINGS)
    profile_before_install = _tree_stamp(DOUYIN_PROFILE)
    machine_before = _machine_install_snapshot()
    database_before = _database_snapshot(STATE_DB)

    print(f"[PREFLIGHT] 当前用户版 {initial_install['DisplayVersion']}；无活动任务。")
    print(f"[PREFLIGHT] 安装包 SHA-256 {installer_hash}")
    print(f"[PREFLIGHT] D:\\ 可用空间 {free / 1024**3:.2f} GiB")
    if args.resume_installed:
        print("[INSTALL] 已确认当前用户版 0.3.5，跳过重复覆盖安装。", flush=True)
        installed = initial_install
    else:
        _run_installer(installer)
        installed = _current_user_install()
        if installed.get("DisplayVersion") != TO_VERSION:
            raise AcceptanceError(f"覆盖安装后注册表版本不是 {TO_VERSION}：{installed}")
        if _normalized(installed.get("InstallLocation", "")) != _normalized(INSTALL_DIR):
            raise AcceptanceError("覆盖安装改变了当前用户安装目录。")
    installed_exe = INSTALL_DIR / APP_EXE_NAME
    if not installed_exe.is_file() or not DIST_EXE.is_file():
        raise AcceptanceError("覆盖安装后缺少已安装主程序或本次 dist 主程序。")
    if _sha256(installed_exe) != _sha256(DIST_EXE):
        raise AcceptanceError("已安装主程序与本次 dist 主程序 SHA-256 不一致。")
    if not args.resume_installed:
        if _optional_digest(STATE_DB) != database_digest_before_install:
            raise AcceptanceError("安装阶段修改了用户状态数据库。")
        if _optional_digest(SETTINGS) != settings_digest_before_install:
            raise AcceptanceError("安装阶段修改了用户设置文件。")
        if _tree_stamp(DOUYIN_PROFILE) != profile_before_install:
            raise AcceptanceError("安装阶段修改了抖音专用登录资料。")
        if _machine_install_snapshot() != machine_before:
            raise AcceptanceError("覆盖当前用户版时改变了计算机范围的旧版安装记录。")
    _assert_no_process_conflicts()
    if _part_files():
        raise AcceptanceError("安装完成后 D:\\ 出现了非预期 .part 文件。")

    root_before = _root_file_snapshot(DOWNLOAD_ROOT)
    session: GuiSession | None = None
    dialog: Any | None = None
    batch_started = False
    batch_completed = False
    closed = False
    try:
        session = _launch_installed_app()
        print(f"[GUI] 已启动安装版 {TO_VERSION}，自动更新检查已禁用。", flush=True)
        _set_link_and_start(session)
        dialog = _wait_for_confirm_dialog(
            session,
            timeout=args.scan_timeout_minutes * 60,
        )
        _validate_and_accept_dialog(dialog)
        dialog = None
        batch_started = True
        print("[GUI] 已确认报告 16 / 发现 16 / 视频 16，并选择全量重新下载。", flush=True)

        summary = _wait_for_download(
            session,
            timeout=args.download_timeout_minutes * 60,
        )
        batch_completed = True
        if summary != (EXPECTED_COUNT, 0, 0):
            raise AcceptanceError(
                f"GUI 批量结果不是成功 16、跳过 0、失败 0：{summary}"
            )
        _assert_queue_success(session)

        database_after = _database_snapshot(STATE_DB)
        works = _changed_works(database_before, database_after)
        files = _verify_files(works, root_before)
        _ffprobe_all(files)
        if _machine_install_snapshot() != machine_before:
            raise AcceptanceError("真实验收期间改变了计算机范围的旧版安装记录。")
        closed = _close_idle_app(session)
        if not closed:
            raise AcceptanceError("验收完成但无法通过退出按钮正常关闭应用；没有强杀。")

        work_ids = ", ".join(work.work_id for work in works)
        print("[PASS] 飞船下载工具 0.3.5 抖音 Playlet 真实验收通过。")
        print("[PASS] GUI：报告 16，发现 16，视频 16；成功 16，跳过 0，失败 0。")
        print("[PASS] 16 个 .mp4 均非空、SQLite 状态 valid、ffprobe 含视频轨道。")
        print("[PASS] D:\\ 无 .part、无重复作品 ID、无非本次根目录文件变化。")
        print(f"[PASS] work_id：{work_ids}")
        print("[PASS] 未上传安装包，未创建或修改服务器更新记录。")
        return 0
    finally:
        if session is not None and not closed:
            if dialog is not None and not batch_started:
                _cancel_waiting_dialog(dialog)
            if batch_completed:
                closed = _close_idle_app(session)
            elif not batch_started:
                closed = _close_idle_app(session)
            if not closed:
                print(
                    f"[SAFETY] PID {session.pid} 可能仍有扫描或下载任务；"
                    "脚本没有强制结束它。",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt as exc:
        print("[INTERRUPTED] 用户中断；活动应用不会被强杀。", file=sys.stderr)
        raise SystemExit(130) from exc
