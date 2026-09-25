"""在临时目录验证核心更新的备份、替换和可执行性校验。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import feichuan_downloader.core_updater as module


def main() -> None:
    source = ROOT / "tools" / "yt-dlp.exe"
    with tempfile.TemporaryDirectory(prefix="feichuan-core-test-") as directory:
        temp_root = Path(directory)
        target = temp_root / "yt-dlp.exe"
        backup = temp_root / "yt-dlp.exe.previous"
        shutil.copy2(source, target)
        expected_version = module.CoreUpdater._executable_version(source)
        release = {
            "tag_name": expected_version,
            "assets": [
                {"name": "yt-dlp.exe", "browser_download_url": "https://github.com/a"},
                {"name": "SHA2-256SUMS", "browser_download_url": "https://github.com/b"},
            ],
        }
        checksum = module.CoreUpdater._sha256(source)

        def copy_new(_url: str, destination: Path) -> None:
            shutil.copy2(source, destination)

        with patch.object(module, "TOOLS_DIR", temp_root), patch.object(
            module, "YTDLP_PATH", target
        ), patch.object(module, "CORE_BACKUP_PATH", backup), patch.object(
            module.CoreUpdater, "_validate_github_url", staticmethod(lambda _url: None)
        ), patch.object(
            module.CoreUpdater, "_download_text", staticmethod(lambda _url: f"{checksum}  yt-dlp.exe\n")
        ), patch.object(module.CoreUpdater, "_download_file", staticmethod(copy_new)):
            module.CoreUpdater()._install_release(release, expected_version)
        assert target.exists() and backup.exists()
        assert module.CoreUpdater()._executable_version(target) == expected_version
    print("core transaction smoke passed")


if __name__ == "__main__":
    main()
