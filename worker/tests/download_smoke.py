"""通过本地媒体验证 yt-dlp 子进程和输出定位。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.downloader import Downloader


def main() -> None:
    source = ROOT / "tests" / "media" / "feichuan_smoke.mp4"
    target = Path("D:/feichuan_smoke.mp4")
    existed = target.exists()
    lines: list[str] = []
    progress: list[float] = []
    result = Downloader().download(
        "http://127.0.0.1:8765/feichuan_smoke.mp4",
        on_line=lines.append,
        on_progress=lambda value: progress.append(value) if value is not None else None,
    )
    assert result.path.exists() and result.path.stat().st_size > 0
    assert result.path.suffix.lower() == ".mp4"
    assert progress and max(progress) >= 99, progress
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
    stream_types = set(probe.stdout.split())
    assert {"video", "audio"}.issubset(stream_types), probe.stdout
    print(f"download smoke passed: {result.path} {result.path.stat().st_size} bytes")
    if not existed:
        result.path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
