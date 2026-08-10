"""Build the headless JSON Lines worker without the legacy wxPython GUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "headless-worker"
OUTPUT = DIST / "feichuan-worker.exe"


def _verify_worker(path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "FEICHUAN_AUTO_UPDATE_CORE": "0",
            "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    process = subprocess.Popen(
        [str(path)],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        hello = {
            "protocol": "feichuan-worker",
            "version": 1,
            "id": "build_verify_hello",
            "type": "hello",
            "payload": {},
        }
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(hello, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        if response.get("type") != "hello.result":
            raise RuntimeError("构建后的工作进程握手失败。")

        shutdown = {
            "protocol": "feichuan-worker",
            "version": 1,
            "id": "build_verify_shutdown",
            "type": "shutdown",
            "payload": {},
        }
        process.stdin.write(json.dumps(shutdown, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        if response.get("type") != "shutdown.result":
            raise RuntimeError("构建后的工作进程无法正常关闭。")
        process.wait(timeout=15)
        if process.returncode:
            raise RuntimeError(f"工作进程验证退出码为 {process.returncode}。")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def run() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        "feichuan-worker",
        "--paths",
        str(SRC),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
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
        str(SRC / "worker_main.py"),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        return completed.returncode
    if not OUTPUT.is_file():
        raise SystemExit(f"PyInstaller did not produce {OUTPUT}")
    _verify_worker(OUTPUT)
    print(f"Built and verified: {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
