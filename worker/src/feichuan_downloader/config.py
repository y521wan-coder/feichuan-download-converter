"""集中管理路径、版本和不会随环境变化的应用设置。"""

from __future__ import annotations

import os
import re
import sys
import json
import threading
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


APP_NAME = "飞船下载转换工具"
VERSION = "1.0"
PRODUCT_KEY = "feichuan_download_tool"
UPDATE_PLATFORM = "windows"
UPDATE_CHANNEL = "stable"

# 自动更新可以通过环境变量关闭，便于在某个 yt-dlp 发布版本出现兼容性问题
# 时保留稳定的旧核心。默认开启“每天检查一次”，更新模块仍会校验并可回滚。
# 无法从理论上证明新核心对所有站点都兼容，因此第一阶段默认只检查不自动替换。
# 完成充分的站点回归测试后，可设置 FEICHUAN_AUTO_UPDATE_CORE=1 开启自动替换。
AUTO_UPDATE_CORE = os.environ.get("FEICHUAN_AUTO_UPDATE_CORE", "0").lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def app_root() -> Path:
    override = os.environ.get("FEICHUAN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # .../飞船下载工具开发/src/feichuan_downloader/config.py
    return Path(__file__).resolve().parents[2]


APP_ROOT = app_root()
SRC_DIR = APP_ROOT / "src"
TOOLS_DIR = APP_ROOT / "tools"
LOG_DIR = APP_ROOT / "logs"
DIST_DIR = APP_ROOT / "dist"
YTDLP_PATH = TOOLS_DIR / "yt-dlp.exe"
FFMPEG_PATH = TOOLS_DIR / "ffmpeg.exe"
FFPROBE_PATH = TOOLS_DIR / "ffprobe.exe"
STATE_PATH = LOG_DIR / "core_update_state.json"
CORE_BACKUP_PATH = TOOLS_DIR / "yt-dlp.exe.previous"

LOCAL_APP_DATA_DIRNAME = "FeichuanDownloader"
DOUYIN_CHROMIUM_PROFILE_DIRNAME = "DouyinChromeProfile"
SETTINGS_FILENAME = "settings.json"
_SETTINGS_LOCK = threading.RLock()

# 环境变量仅用于开发/自动化测试显式覆盖；正常默认跟随当前 Windows 用户的“下载”文件夹。
DEFAULT_DOWNLOAD_DIR = Path(
    os.environ.get("FEICHUAN_DOWNLOAD_DIR", str(Path.home() / "Downloads"))
)
# 兼容旧模块和第三方导入；应用自身在实际操作时调用 get_download_dir()。
DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR
QUALITY_MODE_BEST = "best"
QUALITY_MODE_ASK_EACH_TIME = "ask_each_time"
QUALITY_MODES = {QUALITY_MODE_BEST, QUALITY_MODE_ASK_EACH_TIME}

SOFTWARE_UPDATE_ENDPOINT = os.environ.get(
    "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT",
    "https://update.327802521.xyz/api/v1/updates/check",
).strip()
GITHUB_RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".weba",
}


def local_app_data_root() -> Path:
    """返回当前用户的 LocalAppData 根目录，不读取浏览器配置。"""

    configured = os.environ.get("LOCALAPPDATA", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "AppData" / "Local"


def app_data_dir() -> Path:
    """返回飞船下载工具的当前用户应用数据目录。"""

    return local_app_data_root() / LOCAL_APP_DATA_DIRNAME


def douyin_chromium_profile_dir() -> Path:
    """返回抖音登录专用 Chrome profile；它与用户日常 Chrome 完全独立。"""

    return app_data_dir() / DOUYIN_CHROMIUM_PROFILE_DIRNAME


def settings_path() -> Path:
    override = os.environ.get("FEICHUAN_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else app_data_dir() / SETTINGS_FILENAME


def _read_settings_unlocked() -> dict[str, object]:
    path = settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_settings() -> dict[str, object]:
    """Return a shallow copy of non-sensitive per-user settings."""

    with _SETTINGS_LOCK:
        return dict(_read_settings_unlocked())


def _write_settings_unlocked(payload: dict[str, object]) -> None:
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _absolute_directory(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def default_download_dir() -> Path:
    """Return the current user's Windows Downloads folder."""

    environment_override = os.environ.get("FEICHUAN_DOWNLOAD_DIR", "").strip()
    if environment_override:
        return _absolute_directory(environment_override)
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                for name in (
                    "{374DE290-123F-4565-9164-39C4925E467B}",
                    "Downloads",
                ):
                    try:
                        value, _kind = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    expanded = os.path.expandvars(str(value))
                    if expanded.strip():
                        return _absolute_directory(expanded)
        except Exception:
            pass
    return _absolute_directory(Path.home() / "Downloads")


def get_download_dir() -> Path:
    """Return the user-selected media directory, defaulting to Downloads."""

    environment_override = os.environ.get("FEICHUAN_DOWNLOAD_DIR", "").strip()
    if environment_override:
        return _absolute_directory(environment_override)
    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
    value = payload.get("download_dir") if isinstance(payload, dict) else None
    if isinstance(value, str) and value.strip():
        candidate = _absolute_directory(value)
        if not candidate.exists() or candidate.is_dir():
            return candidate
    return default_download_dir()


def set_download_dir(value: str | os.PathLike[str]) -> Path:
    """Persist a non-sensitive download directory using an atomic JSON replace."""

    raw = str(os.fspath(value)).strip()
    if not raw:
        raise ValueError("下载目录不能为空。")
    directory = _absolute_directory(raw)
    if directory.exists() and not directory.is_dir():
        raise ValueError("所选下载位置不是文件夹。")
    directory.mkdir(parents=True, exist_ok=True)
    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
        payload["download_dir"] = str(directory)
        _write_settings_unlocked(payload)
    return directory


def get_quality_mode() -> str:
    """Return the persisted quality selection mode."""

    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
    value = payload.get("quality_mode")
    return value if isinstance(value, str) and value in QUALITY_MODES else QUALITY_MODE_BEST


def set_quality_mode(value: str) -> str:
    """Persist whether downloads use best quality or prompt each time."""

    selected = str(value or "").strip()
    if selected not in QUALITY_MODES:
        raise ValueError("未知的品质和格式设置。")
    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
        payload["quality_mode"] = selected
        _write_settings_unlocked(payload)
    return selected


def usage_guide_seen(version: str = VERSION) -> bool:
    """Return whether the first-run usage guide was already shown for version."""

    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
    seen = payload.get("usage_guide_seen_versions")
    if isinstance(seen, list):
        return str(version) in {str(item) for item in seen}
    return False


def mark_usage_guide_seen(version: str = VERSION) -> None:
    """Record that the first-run usage guide has been shown for version."""

    with _SETTINGS_LOCK:
        payload = _read_settings_unlocked()
        raw = payload.get("usage_guide_seen_versions")
        seen = [str(item) for item in raw] if isinstance(raw, list) else []
        if str(version) not in seen:
            seen.append(str(version))
        payload["usage_guide_seen_versions"] = seen[-12:]
        _write_settings_unlocked(payload)


def ensure_layout() -> None:
    """创建应用自身的目录；不主动创建或清理用户下载目录。"""

    directories = (APP_ROOT, TOOLS_DIR, LOG_DIR)
    if not getattr(sys, "frozen", False):
        directories = (*directories, SRC_DIR, DIST_DIR)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


def sanitize_filename(value: str, fallback: str = "下载文件") -> str:
    """清理 Windows 文件名，同时保留中文和其它可打印 Unicode 字符。"""

    value = unicodedata.normalize("NFC", value or "").strip()
    value = INVALID_FILENAME_CHARS.sub("_", value)
    value = "".join(char for char in value if char.isprintable())
    value = TRAILING_DOTS_SPACES.sub("", value).strip()
    if not value:
        value = fallback
    if value.split(".", 1)[0].upper() in RESERVED_WINDOWS_NAMES:
        value = f"_{value}"
    # Windows 常见路径长度限制下给扩展名留出余量。
    return value[:180].rstrip(". ") or fallback


def safe_url_for_log(url: str) -> str:
    """日志只保留 URL 的来源和路径，不把 query 中可能存在的 token 写入磁盘。"""

    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return "<无效链接>"


def is_audio_url(url: str) -> bool:
    try:
        suffix = Path(urlsplit(url).path).suffix.lower()
    except Exception:
        suffix = ""
    return suffix in AUDIO_EXTENSIONS


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return ""
