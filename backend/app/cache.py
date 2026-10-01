"""Tiny thread-safe TTL cache used to avoid duplicate Gemini/search/verification
work across requests (API efficiency: never repeat the same call for the same
claim/URL while the result is still fresh).

Signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Tuple


class TTLCache:
    """Bounded key/value cache with per-entry expiry and a global hit-clear
    helper so tests can isolate state between cases."""

    def __init__(self, maxsize: int = 256, ttl: float = 600.0):
        self.maxsize = max(1, int(maxsize))
        self.ttl = float(ttl)
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Tuple[bool, Any]:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return False, None
            expires, value = item
            if now >= expires:
                self._data.pop(key, None)
                return False, None
            return True, value

    def set(self, key: str, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._data) >= self.maxsize and key not in self._data:
                # Drop the oldest entry (insertion order in CPython dicts).
                try:
                    self._data.pop(next(iter(self._data)))
                except StopIteration:  # pragma: no cover - empty cache
                    pass
            self._data[key] = (now + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_REGISTRY: Dict[str, TTLCache] = {}
_REG_LOCK = threading.Lock()


def get_cache(name: str, maxsize: int = 256, ttl: float = 600.0) -> TTLCache:
    """Named process-wide cache (created once; first caller wins on sizing)."""
    with _REG_LOCK:
        cache = _REGISTRY.get(name)
        if cache is None:
            cache = TTLCache(maxsize=maxsize, ttl=ttl)
            _REGISTRY[name] = cache
        return cache


def clear_caches() -> None:
    """Clear every named cache (used by tests to guarantee isolation)."""
    with _REG_LOCK:
        for cache in _REGISTRY.values():
            cache.clear()
