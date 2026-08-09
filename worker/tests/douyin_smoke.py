"""真实抖音链接回归测试；由外层脚本设置临时下载目录。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.douyin_capture import DouyinCapture


def main() -> None:
    result = DouyinCapture(timeout=45).capture("https://v.douyin.com/FJg6-8JXH-U/")
    assert result.path.exists() and result.path.stat().st_size > 100000
    probe = subprocess.run(
        [
            str(ROOT / "tools" / "ffprobe.exe"),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(result.path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    types = set(probe.stdout.split())
    assert {"video", "audio"}.issubset(types), probe.stdout
    print(f"douyin smoke passed: {result.path} {result.path.stat().st_size} bytes")


if __name__ == "__main__":
    main()

