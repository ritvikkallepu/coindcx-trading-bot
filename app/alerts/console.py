from __future__ import annotations

import logging
from typing import Any

from app.alerts.base import AlertInterface


class ConsoleAlert(AlertInterface):
    def __init__(self, name: str = "app.alerts") -> None:
        self.logger = logging.getLogger(name)

    def alert(self, message: str, level: str = "INFO", **kwargs: Any) -> None:
        log_msg = f"[ALERT] {level}: {message}"
        if kwargs:
            log_msg += f" | {kwargs}"
        
        level_num = getattr(logging, level.upper(), logging.INFO)
        self.logger.log(level_num, log_msg)
