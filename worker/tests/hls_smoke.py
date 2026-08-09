"""验证 HLS 临时输出使用 MP4 封装且能被 ffprobe 识别。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.douyin_capture import DouyinCapture


def main() -> None:
    target = Path("D:/feichuan_hls_smoke.mp4.part")
    target.unlink(missing_ok=True)
    DouyinCapture._download_hls(
        "http://127.0.0.1:8766/playlist.m3u8",
        target,
        {"User-Agent": "FeichuanSmoke/1"},
    )
    DouyinCapture._validate_media_file(target)
    print(f"hls smoke passed: {target.stat().st_size} bytes")
    target.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

