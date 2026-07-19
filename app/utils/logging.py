from __future__ import annotations

import gzip
import logging
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings


def _gzip_rotator(source: str, destination: str) -> None:
    with open(source, "rb") as source_file:
        with gzip.open(destination, "wb") as destination_file:
            shutil.copyfileobj(source_file, destination_file)
    Path(source).unlink()


def configure_logging(settings: Settings) -> Path:
    log_dir = Path(settings.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bot.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max(1, settings.log_max_bytes),
        backupCount=max(1, settings.log_backup_count),
        encoding="utf-8",
    )
    if settings.log_compress_backups:
        file_handler.namer = lambda name: f"{name}.gz"
        file_handler.rotator = _gzip_rotator
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.info(
        "Logging initialized: file=%s max_bytes=%s backups=%s compressed=%s",
        log_path,
        file_handler.maxBytes,
        file_handler.backupCount,
        settings.log_compress_backups,
    )
    return log_path
