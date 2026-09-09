"""统一运行不访问公网的快速冒烟测试。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    ROOT / "tests" / "smoke_test.py",
    ROOT / "tests" / "cdp_client_smoke.py",
    ROOT / "tests" / "coordinator_smoke.py",
    ROOT / "tests" / "generic_playlist_smoke.py",
    ROOT / "tests" / "coordinator_batch_smoke.py",
    ROOT / "tests" / "coordinator_lifecycle_smoke.py",
    ROOT / "tests" / "youtube_coordinator_smoke.py",
    ROOT / "tests" / "youtube_enumerator_smoke.py",
    ROOT / "tests" / "gui_dialog_smoke.py",
    ROOT / "tests" / "naming_smoke.py",
    ROOT / "tests" / "state_store_smoke.py",
    ROOT / "tests" / "douyin_enumerator_smoke.py",
    ROOT / "tests" / "douyin_playlet_smoke.py",
    ROOT / "tests" / "douyin_downloader_smoke.py",
    ROOT / "tests" / "gui_update_prompt_smoke.py",
    ROOT / "tests" / "software_update_smoke.py",
    ROOT / "tests" / "core_transaction_smoke.py",
    ROOT / "tests" / "worker_protocol_smoke.py",
    ROOT / "tests" / "direct_link_smoke.py",
    ROOT / "tests" / "xiaoe_wechat_capture_smoke.py",
)


def run() -> int:
    environment = os.environ.copy()
    environment.update(
        {
            "FEICHUAN_AUTO_UPDATE_CORE": "0",
            "FEICHUAN_SOFTWARE_UPDATE_ENDPOINT": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    for test_path in TESTS:
        if not test_path.exists():
            print(f"Missing smoke test: {test_path}", file=sys.stderr)
            return 2
        print(f"\n=== {test_path.name} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, str(test_path)],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode:
            print(
                f"Offline smoke failed: {test_path.name} "
                f"(exit code {completed.returncode})",
                file=sys.stderr,
            )
            return completed.returncode

    print(f"\nOffline smoke passed: {len(TESTS)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
