"""文件日志配置，GUI 通过自己的回调显示同样的状态。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock


_configure_lock = Lock()
_configured: set[Path] = set()


def get_logger(log_dir: Path) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("feichuan_downloader")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with _configure_lock:
        if log_dir not in _configured:
            handler = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            logger.addHandler(handler)
            _configured.add(log_dir)
    return logger

