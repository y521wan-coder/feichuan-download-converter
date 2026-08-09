"""对一个真实链接执行完整下载并用 ffprobe 检查输出轨道。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.downloader import Downloader


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: link_smoke.py URL")
    url = sys.argv[1]
    lines: list[str] = []

    def on_line(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    result = Downloader().download(url, on_line=on_line)
    print(f"RESULT={result.path}", flush=True)
    probe = subprocess.run(
        [
            str(ROOT / "tools" / "ffprobe.exe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "default=noprint_wrappers=1",
            str(result.path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print("FFPROBE_START", flush=True)
    print(probe.stdout, flush=True)
    print("FFPROBE_END", flush=True)
    if probe.returncode:
        print(probe.stderr, file=sys.stderr, flush=True)
        raise SystemExit(probe.returncode)


if __name__ == "__main__":
    main()

