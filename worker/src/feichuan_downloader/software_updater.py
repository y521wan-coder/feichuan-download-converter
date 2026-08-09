"""软件更新检查、安装包下载、SHA-256 校验与安装器启动。"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .config import (
    PRODUCT_KEY,
    SOFTWARE_UPDATE_ENDPOINT,
    UPDATE_CHANNEL,
    UPDATE_PLATFORM,
    VERSION,
    app_data_dir,
    safe_url_for_log,
    sanitize_filename,
)


ProgressCallback = Callable[[int, int], None]
MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024


def _safe_error(message: str) -> str:
    return re.sub(
        r"https?://[^\s\]>)'\"]+",
        lambda match: safe_url_for_log(match.group(0)),
        message,
    )


def _valid_download_url(value: str) -> bool:
    """Accept only an absolute HTTP(S) URL supplied by the update service."""

    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme.lower() in {"https", "http"}
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


@dataclass(frozen=True)
class SoftwareUpdateResult:
    ok: bool
    available: bool
    latest_version: str = ""
    download_url: str = ""
    sha256: str = ""
    file_size: int | None = None
    release_notes: str = ""
    message: str = ""


@dataclass(frozen=True)
class DownloadedSoftwareUpdate:
    path: Path
    sha256: str
    version: str


def check_software_update(
    endpoint: str | None = None,
    *,
    current_version: str | None = None,
) -> SoftwareUpdateResult:
    endpoint = (endpoint if endpoint is not None else SOFTWARE_UPDATE_ENDPOINT).strip()
    if not endpoint:
        return SoftwareUpdateResult(
            ok=True,
            available=False,
            message="软件更新服务尚未配置，暂无可用软件更新或产品未发布。",
        )
    if not endpoint.lower().startswith(("https://", "http://")):
        return SoftwareUpdateResult(False, False, message="软件更新服务地址无效。")
    effective_version = str(current_version or VERSION)
    params = {
        "product_key": PRODUCT_KEY,
        "platform": UPDATE_PLATFORM,
        "channel": UPDATE_CHANNEL,
        "current_version": effective_version,
    }
    try:
        response = requests.get(
            endpoint,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": f"{PRODUCT_KEY}/{effective_version}",
            },
            timeout=(8, 20),
        )
    except requests.RequestException as exc:
        return SoftwareUpdateResult(
            False,
            False,
            message=f"检查软件更新失败：{_safe_error(str(exc))}",
        )
    if response.status_code in {404, 410}:
        return SoftwareUpdateResult(
            True,
            False,
            message="暂无可用软件更新或产品未发布。",
        )
    if response.status_code >= 400:
        return SoftwareUpdateResult(
            False,
            False,
            message=f"软件更新服务返回 HTTP {response.status_code}。",
        )
    try:
        raw: Any = response.json()
    except ValueError:
        return SoftwareUpdateResult(False, False, message="软件更新服务返回的内容不是 JSON。")
    if not isinstance(raw, dict):
        return SoftwareUpdateResult(False, False, message="软件更新服务返回格式不正确。")

    code = raw.get("code")
    if code not in (None, 0, "0"):
        if code in (404, 4041, "404", "4041"):
            return SoftwareUpdateResult(
                True,
                False,
                message="暂无可用软件更新或产品未发布。",
            )
        return SoftwareUpdateResult(
            False,
            False,
            message=f"软件更新服务返回业务错误 {code}。",
        )
    production_envelope = "code" in raw or "data" in raw
    payload = raw.get("data")
    if production_envelope:
        if not isinstance(payload, dict):
            return SoftwareUpdateResult(
                False,
                False,
                message="软件更新服务返回的 data 格式不正确。",
            )
        data = payload
    else:
        # Compatibility for the earliest development endpoint. Production uses
        # the strict {code, message, data} envelope above.
        data = raw
    if data.get("product_exists") is False or data.get("published") is False:
        return SoftwareUpdateResult(
            True,
            False,
            message="暂无可用软件更新或产品未发布。",
        )

    latest = str(data.get("latest_version") or data.get("version") or "").strip()
    download_url = str(data.get("download_url") or "").strip()
    checksum = str(data.get("sha256") or data.get("sha256_checksum") or "").strip().lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        checksum = ""
    raw_file_size = data.get("file_size")
    try:
        file_size = int(raw_file_size) if raw_file_size is not None else None
    except (TypeError, ValueError):
        file_size = None
    notes = str(data.get("release_notes") or "").strip()
    update_flag = data.get("update_available")
    if update_flag is False:
        return SoftwareUpdateResult(
            True,
            False,
            latest_version=latest,
            file_size=file_size,
            release_notes=notes,
            message="当前已是最新版本。",
        )
    if production_envelope and update_flag is not True:
        return SoftwareUpdateResult(
            False,
            False,
            latest_version=latest,
            message="软件更新服务未返回有效的 update_available 状态。",
        )
    if not latest:
        return SoftwareUpdateResult(
            False,
            False,
            message="更新服务器未提供新版本号，已拒绝更新。",
        )
    if not download_url or not _valid_download_url(download_url):
        return SoftwareUpdateResult(
            False,
            False,
            latest_version=latest,
            message="更新服务器未提供有效的 download_url，已拒绝更新。",
        )
    if not checksum:
        return SoftwareUpdateResult(
            False,
            False,
            latest_version=latest,
            message="更新服务器未提供有效的 SHA-256，已拒绝更新。",
        )
    if raw_file_size is not None and (
        file_size is None or file_size <= 0 or file_size > MAX_INSTALLER_BYTES
    ):
        return SoftwareUpdateResult(
            False,
            False,
            latest_version=latest,
            message="更新服务器提供的安装包大小无效，已拒绝更新。",
        )
    return SoftwareUpdateResult(
        True,
        True,
        latest_version=latest,
        download_url=download_url,
        sha256=checksum,
        file_size=file_size,
        release_notes=notes,
        message=f"发现软件新版本 {latest}。",
    )


def download_update_installer(
    update: SoftwareUpdateResult,
    destination_dir: str | os.PathLike[str] | None = None,
    *,
    on_progress: ProgressCallback | None = None,
) -> DownloadedSoftwareUpdate:
    """Download only the server-provided URL and verify it before rename."""

    if not update.available or not update.download_url or not update.latest_version:
        raise RuntimeError("没有可下载的软件更新。")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", update.sha256 or ""):
        raise RuntimeError("更新服务器没有提供有效的 SHA-256，已拒绝下载。")
    if not _valid_download_url(update.download_url):
        raise RuntimeError("更新服务器返回的 download_url 无效。")
    if update.file_size is not None and (
        update.file_size <= 0 or update.file_size > MAX_INSTALLER_BYTES
    ):
        raise RuntimeError("更新服务器返回的安装包大小无效。")

    directory = Path(destination_dir) if destination_dir is not None else app_data_dir() / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    safe_version = sanitize_filename(update.latest_version, fallback="update")
    target = directory / f"飞船下载工具-Setup-{safe_version}.exe"
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)

    digest = hashlib.sha256()
    received = 0
    session = requests.Session()
    response: requests.Response | None = None
    try:
        response = session.get(
            update.download_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": f"{PRODUCT_KEY}/{VERSION}",
            },
            stream=True,
            timeout=(10, 120),
            allow_redirects=True,
        )
        response.raise_for_status()
        try:
            total = int(response.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise RuntimeError("更新服务器返回的 Content-Length 无效。") from None
        expected_size = update.file_size or 0
        if total < 0:
            raise RuntimeError("更新服务器返回的 Content-Length 无效。")
        if total > MAX_INSTALLER_BYTES or expected_size > MAX_INSTALLER_BYTES:
            raise RuntimeError("更新安装包超过 2 GiB，已拒绝下载。")
        if total and expected_size and total != expected_size:
            raise RuntimeError("安装包大小与服务器元数据不一致，已拒绝下载。")
        with partial.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if received > MAX_INSTALLER_BYTES:
                    raise RuntimeError("更新安装包超过 2 GiB，已拒绝下载。")
                if expected_size and received > expected_size:
                    raise RuntimeError("更新安装包大小超过服务器元数据，已拒绝下载。")
                if callable(on_progress):
                    on_progress(received, total or expected_size)
            output.flush()
            os.fsync(output.fileno())
    except requests.RequestException as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"下载安装包失败：{_safe_error(str(exc))}") from None
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()
        session.close()

    if total and received != total:
        partial.unlink(missing_ok=True)
        raise RuntimeError("更新安装包大小与 Content-Length 不一致，已删除临时文件。")
    if update.file_size is not None and received != update.file_size:
        partial.unlink(missing_ok=True)
        raise RuntimeError("更新安装包大小与服务器元数据不一致，已删除临时文件。")
    actual = digest.hexdigest().lower()
    if not hmac.compare_digest(actual, update.sha256.lower()):
        partial.unlink(missing_ok=True)
        raise RuntimeError("更新安装包 SHA-256 校验失败，已删除临时文件。")
    try:
        with partial.open("rb") as source:
            if source.read(2) != b"MZ":
                raise RuntimeError("更新文件不是有效的 Windows EXE。")
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return DownloadedSoftwareUpdate(target, actual, update.latest_version)


def launch_update_installer(
    installer: str | os.PathLike[str],
    *,
    expected_sha256: str,
) -> None:
    """Re-verify the downloaded installer immediately before execution."""

    path = Path(installer)
    if path.suffix.lower() != ".exe" or not path.is_file():
        raise RuntimeError("更新安装包不存在或不是 EXE。")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        raise RuntimeError("缺少有效的安装包 SHA-256，已拒绝启动。")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_INSTALLER_BYTES:
        raise RuntimeError("更新安装包大小无效，已拒绝启动。")
    with path.open("rb") as source:
        if source.read(2) != b"MZ":
            raise RuntimeError("更新安装包不是有效的 Windows EXE。")
    actual = _sha256_file(path)
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise RuntimeError("更新安装包在启动前 SHA-256 校验失败，已拒绝执行。")
    subprocess.Popen(
        [str(path), "/SP-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        ),
    )
