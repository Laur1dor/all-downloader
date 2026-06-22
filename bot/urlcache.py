"""Short-token store mapping callback tokens back to source URLs.

Telegram limits callback_data to 64 bytes, so the full URL is kept in memory
and referenced by a short random token in the "Скачать аудио" button.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict


class UrlCache:
    def __init__(self, max_size: int = 1000) -> None:
        self._items: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size

    def store(self, url: str) -> str:
        token = secrets.token_urlsafe(9)
        self._items[token] = url
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> str | None:
        return self._items.get(token)
