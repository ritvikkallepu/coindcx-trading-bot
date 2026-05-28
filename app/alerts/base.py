from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AlertInterface(ABC):
    @abstractmethod
    def alert(self, message: str, level: str = "INFO", **kwargs: Any) -> None:
        pass
