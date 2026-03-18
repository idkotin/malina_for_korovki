from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from host_monitor.config import LoggingCfg


def setup_logging(cfg: LoggingCfg) -> None:
    level = getattr(logging, cfg.level, logging.INFO)
    logger = logging.getLogger()
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    log_path = Path(cfg.dir) / cfg.file
    fh = RotatingFileHandler(
        log_path,
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(level)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level)

    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)

