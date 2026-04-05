from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.models import RawPost

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    @abstractmethod
    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...
