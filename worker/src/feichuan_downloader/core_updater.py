"""yt-dlp 官方 stable release 检查、SHA-256 校验和可回滚替换。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import requests

from .config import (
    AUTO_UPDATE_CORE,
    CORE_BACKUP_PATH,
    GITHUB_RELEASE_API,
    STATE_PATH,
    TOOLS_DIR,
    VERSION,
    YTDLP_PATH,
)


LineCallback = Callable[[str], None]


@dataclass(frozen=True)
class CoreUpdateResult:
    ok: bool
    updated: bool
    current_version: str
    latest_version: str = ""
    available: bool = False
    message: str = ""


class CoreUpdater:
    """更新器不使用 yt-dlp 自带的 ``-U``，避免替换中的文件被占用。"""

    def __init__(self, line_callback: LineCallback | None = None) -> None:
        self.line_callback = line_callback
        self._lock = threading.Lock()

    def check_and_update(
        self,
        *,
        manual: bool = False,
        install: bool | None = None,
    ) -> CoreUpdateResult:
        """检查 stable release；``install`` 为 False 时只检查不替换。"""

        if install is None:
            install = manual or AUTO_UPDATE_CORE
        with self._lock:
            current = self.current_version()
            if not manual and not self._should_check_today():
                return CoreUpdateResult(
                    ok=True,
                    updated=False,
                    current_version=current,
                    message="今天已经检查过下载核心更新。",
                )
            self._mark_checked_today()
            try:
                release = self._fetch_release()
                latest = self._release_version(release)
                if not latest:
                    return CoreUpdateResult(
                        ok=False,
                        updated=False,
                        current_version=current,
                        message="GitHub 返回的版本信息不完整。",
                    )
                if self._version_key(latest) <= self._version_key(current):
                    return CoreUpdateResult(
                        ok=True,
                        updated=False,
                        current_version=current,
                        latest_version=latest,
                        message=f"下载核心已是最新稳定版 {current}。",
                    )
                if not install:
                    return CoreUpdateResult(
                        ok=True,
                        updated=False,
                        current_version=current,
                        latest_version=latest,
                        available=True,
                        message=(
                            f"发现下载核心新版本 {latest}（当前 {current}），"
                            "自动替换已关闭，请点击“检查下载核心更新”手动更新。"
                        ),
                    )
                self._emit(f"发现 yt-dlp 新版本 {latest}，正在下载并校验……")
                self._install_release(release, latest)
                new_version = self.current_version()
                return CoreUpdateResult(
                    ok=True,
                    updated=True,
                    current_version=new_version,
                    latest_version=latest,
                    message=f"下载核心已更新到 {new_version}，旧版本已保存在回滚备份中。",
                )
            except Exception as exc:
                self._emit(f"下载核心更新失败，继续使用当前版本 {current}：{exc}")
                return CoreUpdateResult(
                    ok=False,
                    updated=False,
                    current_version=current,
                    message=f"下载核心更新失败，已保留当前版本 {current}：{exc}",
                )

    def current_version(self) -> str:
        if not YTDLP_PATH.exists():
            return "未安装"
        try:
            return self._executable_version(YTDLP_PATH)
        except Exception:
            # 损坏或被误删的核心仍应进入更新流程，而不是阻止 GUI 启动。
            return "未知"

    @staticmethod
    def has_rollback_backup() -> bool:
        return CORE_BACKUP_PATH.is_file()

    def rollback(self) -> CoreUpdateResult:
        """手动将上一次成功更新前的核心恢复回来。"""

        temporary: Path | None = None
        with self._lock:
            if not CORE_BACKUP_PATH.exists():
                return CoreUpdateResult(
                    ok=False,
                    updated=False,
                    current_version=self.current_version(),
                    message="没有可用的下载核心回滚备份。",
                )
            try:
                temporary = self._temp_path("rollback")
                shutil.copy2(CORE_BACKUP_PATH, temporary)
                self._validate_executable(temporary)
                os.replace(temporary, YTDLP_PATH)
                return CoreUpdateResult(
                    ok=True,
                    updated=True,
                    current_version=self.current_version(),
                    message=f"下载核心已回滚到 {self.current_version()}。",
                )
            except Exception as exc:
                try:
                    if temporary:
                        temporary.unlink(missing_ok=True)
                except Exception:
                    pass
                return CoreUpdateResult(
                    ok=False,
                    updated=False,
                    current_version=self.current_version(),
                    message=f"下载核心回滚失败：{exc}",
                )

    def _fetch_release(self) -> dict[str, Any]:
        self._emit("正在查询 yt-dlp 官方 stable release……")
        response = requests.get(
            GITHUB_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"FeichuanDownloadTool/{VERSION}",
            },
            timeout=(10, 30),
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        release = response.json()
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise RuntimeError("没有可用的 stable release。")
        return release

    @staticmethod
    def _release_version(release: dict[str, Any]) -> str:
        value = str(release.get("tag_name") or release.get("name") or "").strip()
        return value[1:] if value.lower().startswith("v") else value

    @staticmethod
    def _version_key(value: str) -> tuple[int, ...]:
        numbers = [int(item) for item in re.findall(r"\d+", value)]
        return tuple(numbers[:6]) or (0,)

    @staticmethod
    def _asset_map(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
        assets = release.get("assets") or []
        result: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if isinstance(asset, dict) and asset.get("name"):
                result[str(asset["name"]).lower()] = asset
        return result

    def _install_release(self, release: dict[str, Any], version: str) -> None:
        assets = self._asset_map(release)
        executable = assets.get("yt-dlp.exe")
        sums = assets.get("sha2-256sums")
        if not executable or not sums:
            raise RuntimeError("官方 release 缺少 yt-dlp.exe 或 SHA2-256SUMS。")
        executable_url = str(executable.get("browser_download_url") or "")
        sums_url = str(sums.get("browser_download_url") or "")
        self._validate_github_url(executable_url)
        self._validate_github_url(sums_url)
        checksum_text = self._download_text(sums_url)
        expected = self._checksum_for(checksum_text, "yt-dlp.exe")
        if not expected:
            raise RuntimeError("SHA2-256SUMS 中没有 yt-dlp.exe 条目。")

        temporary = self._temp_path("new")
        try:
            self._download_file(executable_url, temporary)
            actual = self._sha256(temporary)
            if actual.lower() != expected.lower():
                raise RuntimeError(
                    f"SHA-256 校验不一致（期望 {expected.lower()}，实际 {actual.lower()}）。"
                )
            self._validate_executable(temporary)
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            if YTDLP_PATH.exists():
                try:
                    # 只有确认当前版本可运行时才更新 previous，避免用损坏文件覆盖
                    # 最后一个好版本的回滚备份。
                    self._validate_executable(YTDLP_PATH)
                except Exception:
                    self._emit("当前 yt-dlp 核心已损坏，保留现有回滚备份。")
                else:
                    backup_temporary = self._temp_path("backup")
                    shutil.copy2(YTDLP_PATH, backup_temporary)
                    os.replace(backup_temporary, CORE_BACKUP_PATH)
            os.replace(temporary, YTDLP_PATH)
            try:
                self._validate_executable(YTDLP_PATH)
            except Exception:
                failed = self._temp_path("failed")
                try:
                    os.replace(YTDLP_PATH, failed)
                    if CORE_BACKUP_PATH.exists():
                        os.replace(CORE_BACKUP_PATH, YTDLP_PATH)
                finally:
                    failed.unlink(missing_ok=True)
                raise
            self._emit(f"yt-dlp {version} 校验通过，更新完成。")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_github_url(url: str) -> None:
        parts = requests.utils.urlparse(url)
        if parts.scheme != "https" or parts.hostname not in {"github.com", "objects.githubusercontent.com"}:
            raise RuntimeError("官方更新地址不是受信任的 GitHub HTTPS 地址。")

    @staticmethod
    def _download_text(url: str) -> str:
        response = requests.get(
            url,
            headers={"User-Agent": f"FeichuanDownloadTool/{VERSION}"},
            timeout=(10, 30),
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        return response.content.decode("utf-8", errors="replace")

    @staticmethod
    def _download_file(url: str, destination: Path) -> None:
        with requests.get(
            url,
            headers={"User-Agent": f"FeichuanDownloadTool/{VERSION}"},
            stream=True,
            timeout=(10, 120),
            proxies={"http": None, "https": None},
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)

    @staticmethod
    def _checksum_for(text: str, filename: str) -> str | None:
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            listed_name = parts[-1].lstrip("*")
            if Path(listed_name).name.lower() == filename.lower() and re.fullmatch(
                r"[0-9a-fA-F]{64}", parts[0]
            ):
                return parts[0].lower()
        return None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _executable_version(path: Path) -> str:
        try:
            completed = subprocess.run(
                [str(path), "--version"],
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (completed.stdout or completed.stderr).decode(
                "utf-8", errors="replace"
            )
            for line in output.splitlines():
                if line.strip():
                    if completed.returncode != 0:
                        raise RuntimeError(line.strip())
                    return line.strip()
        except Exception as exc:
            raise RuntimeError(f"无法运行 {path.name} --version：{exc}") from exc
        raise RuntimeError(f"无法读取 {path.name} 版本。")

    def _validate_executable(self, path: Path) -> str:
        if not path.exists() or path.stat().st_size < 1024:
            raise RuntimeError("下载的 yt-dlp 文件为空或不完整。")
        return self._executable_version(path)

    @staticmethod
    def _temp_path(kind: str) -> Path:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f".yt-dlp-{kind}-", suffix=".exe", dir=TOOLS_DIR)
        os.close(handle)
        path = Path(name)
        path.unlink(missing_ok=True)
        return path

    @staticmethod
    def _read_state() -> dict[str, Any]:
        try:
            value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def _should_check_today(cls) -> bool:
        return cls._read_state().get("last_check_date") != date.today().isoformat()

    @classmethod
    def _mark_checked_today(cls) -> None:
        state = cls._read_state()
        state["last_check_date"] = date.today().isoformat()
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATE_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, STATE_PATH)
        except OSError:
            pass

    def _emit(self, message: str) -> None:
        if self.line_callback:
            self.line_callback(message)
