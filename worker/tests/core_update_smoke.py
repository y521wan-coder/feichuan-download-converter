"""访问 GitHub stable release，验证检查路径不会替换本地核心。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.core_updater import CoreUpdater


def main() -> None:
    result = CoreUpdater().check_and_update(manual=True, install=False)
    print(result.message)
    if not result.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

