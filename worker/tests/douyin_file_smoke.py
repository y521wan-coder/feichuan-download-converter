"""验证已下载抖音文件能被正式媒体校验器接受。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.douyin_capture import DouyinCapture


def main() -> None:
    candidates = sorted(Path("D:/").glob("douyin_*.mp4"), key=lambda item: item.stat().st_mtime)
    if not candidates:
        print("no existing douyin sample; skipped")
        return
    DouyinCapture._validate_media_file(candidates[-1])
    print(f"validated {candidates[-1]}")


if __name__ == "__main__":
    main()

