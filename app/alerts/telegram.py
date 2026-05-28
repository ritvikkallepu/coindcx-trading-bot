from __future__ import annotations

import logging
from typing import Any
import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from app.alerts.base import AlertInterface


class TelegramAlert(AlertInterface):
    def __init__(self, bot_token: str, chat_id: str, name: str = "app.alerts.telegram") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger(name)

    def alert(self, message: str, level: str = "INFO", **kwargs: Any) -> None:
        emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🚨", "CRITICAL": "🔥"}.get(level.upper(), "📝")
        text = f"{emoji} <b>{level}</b>: {message}"
        if kwargs:
            text += f"\n\n<pre>{json.dumps(kwargs, indent=2)}</pre>"
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        req = Request(
            url, 
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        try:
            with urlopen(req, timeout=10) as response:
                pass
        except (URLError, HTTPError) as exc:
            self.logger.error("Failed to send Telegram alert: %s", exc)
