"""构建绿色版：程序单文件 + 同目录 tools 外置媒体工具。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"
TOOLS = ROOT / "tools"
THIRD_PARTY_NOTICES = ROOT / "THIRD_PARTY_NOTICES.txt"
LICENSES_DIR = ROOT / "licenses"
ASSETS_DIR = ROOT / "assets"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feichuan_downloader.config import APP_NAME, VERSION


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def locate_tool(name: str) -> Path | None:
    configured = os.environ.get(name.upper() + "_PATH", "").strip()
    # Prefer the reviewed, project-pinned payload.  In particular, do not
    # silently replace it with an unrelated FFmpeg that happens to be
    # installed elsewhere on the build machine.
    candidates = [TOOLS / name]
    if configured:
        candidates.insert(0, Path(configured))
    candidates.extend(
        [
            Path(r"C:\ffmpeg\bin") / name,
        ]
    )
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def run() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    TOOLS.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--paths",
        str(SRC),
        "--specpath",
        str(ROOT / "scripts"),
        "--hidden-import",
        "websocket",
        "--hidden-import",
        "websocket._abnf",
        "--hidden-import",
        "feichuan_downloader.douyin_enumerator",
        "--hidden-import",
        "feichuan_downloader.douyin_session",
        "--hidden-import",
        "feichuan_downloader.douyin_downloader",
        "--hidden-import",
        "feichuan_downloader.youtube_enumerator",
        "--hidden-import",
        "feichuan_downloader.youtube_downloader",
        str(SRC / "main.py"),
    ]
    print("Building PyInstaller executable...")
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        return completed.returncode

    ytdlp = TOOLS / "yt-dlp.exe"
    if not ytdlp.exists():
        raise SystemExit(f"Missing {ytdlp}; obtain the official release before building.")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        source = locate_tool(name)
        if not source:
            raise SystemExit(f"Cannot locate {name}; set {name[:-4].upper()}_PATH or install ffmpeg.")
        destination = TOOLS / name
        if source.resolve() != destination.resolve():
            print(f"Copying {source} -> {destination}")
            shutil.copy2(source, destination)

    output_exe = DIST / f"{APP_NAME}.exe"
    if not output_exe.exists():
        raise SystemExit(f"PyInstaller did not produce {output_exe}")
    # 绿色版运行时以 exe 所在目录为根；把外置工具复制到 dist/tools。
    # 不把开发机运行时产生的日志/每日检查状态带进交付目录。
    runtime_logs = DIST / "logs"
    if runtime_logs.exists():
        for stale in runtime_logs.iterdir():
            if stale.is_file():
                try:
                    stale.unlink(missing_ok=True)
                except PermissionError:
                    print(f"Warning: could not remove locked runtime file {stale}")
    for stale_dir in (DIST / "src", DIST / "dist"):
        if stale_dir.exists() and not any(stale_dir.iterdir()):
            stale_dir.rmdir()
    bundled_tools = DIST / "tools"
    bundled_tools.mkdir(parents=True, exist_ok=True)
    for stale in bundled_tools.iterdir():
        if stale.name.startswith(".yt-dlp-") or stale.name in {
            "yt-dlp.exe.previous",
            "yt-dlp.exe.bad",
        }:
            if stale.is_file():
                stale.unlink(missing_ok=True)
    for name in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "SHA2-256SUMS"):
        source = TOOLS / name
        if not source.exists():
            raise SystemExit(f"Missing {source}")
        destination = bundled_tools / name
        print(f"Copying {source} -> {destination}")
        shutil.copy2(source, destination)

    build_kind = "发布构建"

    if not THIRD_PARTY_NOTICES.exists():
        raise SystemExit(f"Missing {THIRD_PARTY_NOTICES}")
    notices_destination = DIST / THIRD_PARTY_NOTICES.name
    print(f"Copying {THIRD_PARTY_NOTICES} -> {notices_destination}")
    shutil.copy2(THIRD_PARTY_NOTICES, notices_destination)

    required_licenses = (
        "FFmpeg-LGPL-3.0.txt",
        "GNU-GPL-3.0.txt",
    )
    bundled_licenses = DIST / "licenses"
    bundled_licenses.mkdir(parents=True, exist_ok=True)
    for name in required_licenses:
        source = LICENSES_DIR / name
        if not source.exists():
            raise SystemExit(f"Missing third-party license text: {source}")
        destination = bundled_licenses / name
        print(f"Copying {source} -> {destination}")
        shutil.copy2(source, destination)

    required_assets = ("donation_qr.jpg",)
    bundled_assets = DIST / "assets"
    bundled_assets.mkdir(parents=True, exist_ok=True)
    for name in required_assets:
        source = ASSETS_DIR / name
        if not source.exists():
            raise SystemExit(f"Missing application asset: {source}")
        destination = bundled_assets / name
        print(f"Copying {source} -> {destination}")
        shutil.copy2(source, destination)

    hash_targets = [
        output_exe,
        bundled_tools / "yt-dlp.exe",
        bundled_tools / "ffmpeg.exe",
        bundled_tools / "ffprobe.exe",
        bundled_tools / "SHA2-256SUMS",
        notices_destination,
        *(bundled_licenses / name for name in required_licenses),
        *(bundled_assets / name for name in required_assets),
    ]
    hash_lines = [
        f"{path.relative_to(DIST)} SHA-256：{sha256(path).upper()}"
        for path in hash_targets
        if path is not None and path.exists()
    ]
    manifest = DIST / "版本与校验.txt"
    manifest.write_text(
        f"{APP_NAME}\n"
        f"版本：{VERSION}\n"
        f"构建类型：{build_kind}\n"
        + "\n".join(hash_lines)
        + "\n",
        encoding="utf-8",
    )
    print(f"Built: {output_exe} ({output_exe.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
