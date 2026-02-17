from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    expires_at: float
    etag: str | None = None


class MemoryTTLCache:
    def __init__(self, max_entries: int = 4000) -> None:
        self._max_entries = max_entries
        self._store: dict[str, CacheEntry] = {}
        self._lock = RLock()

    def get(self, key: str, allow_stale: bool = False) -> CacheEntry | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if allow_stale or entry.expires_at > now:
                return entry
            self._store.pop(key, None)
            return None

    def set(self, key: str, value: Any, ttl_seconds: int, etag: str | None = None) -> None:
        with self._lock:
            if len(self._store) >= self._max_entries:
                self._evict_one()
            self._store[key] = CacheEntry(
                value=value,
                expires_at=time.monotonic() + ttl_seconds,
                etag=etag,
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def _evict_one(self) -> None:
        oldest_key = None
        oldest_expiry = float("inf")
        for key, entry in self._store.items():
            if entry.expires_at < oldest_expiry:
                oldest_expiry = entry.expires_at
                oldest_key = key
        if oldest_key:
            self._store.pop(oldest_key, None)
