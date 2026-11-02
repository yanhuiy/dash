"""日志配置：控制台 + 按日期切分的文件日志。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(TZ)


def setup_logging(config: dict) -> logging.Logger:
    log_dir = Path(config["logging"]["dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mainline")
    logger.setLevel(getattr(logging, str(config["logging"].get("level", "INFO")).upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(
        log_dir / f"run_{now():%Y%m%d}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger
