"""Per-user API-key authentication for the Veritas backend.

Design (see README "Security & privacy"):

  * Keys look like ``vfnd_<43 url-safe chars>`` (32 bytes of ``secrets`` entropy).
  * Only SHA-256 **hashes** are stored -- a leaked registry reveals nothing
    usable. Plaintext keys exist only at creation time and in the client's
    own storage.
  * Key registry sources, merged (env wins on key-id conflict):
      1. ``VERITAS_API_KEYS`` env var:
         ``id1:<sha256hex>:rate_max[:id2:<sha256hex>:rate_max ...]``
         (hex hash of the *full* key string, incl. the ``vfnd_`` prefix).
      2. ``VERITAS_KEY_DB`` JSON file (default ``<project>/data/api_keys.json``),
         managed by ``tools/manage_keys.py``; mode 0600 enforced where possible.
  * Verification is constant-time (``hmac.compare_digest``) and the file
    registry hot-reloads on mtime change, so revocation takes effect without
    a restart.
  * Rate limits are per **key id** (not IP) with optional per-key overrides.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

KEY_PREFIX = "vfnd_"
KEY_BYTES = 32  # -> 43 url-safe chars


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_id, sha256_hex). Only call at creation time."""
    raw = secrets.token_urlsafe(KEY_BYTES)
    key = KEY_PREFIX + raw
    key_id = "k_" + secrets.token_hex(4)
    return key, key_id, hash_key(key)


def hash_key(key: str) -> str:
    """SHA-256 of the full key string (prefix included)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    rate_max: Optional[int]  # None -> global default
    source: str  # "env" | "file"


class KeyRegistry:
    """Merged env + file key registry with constant-time lookup."""

    def __init__(self, env_var: str = "VERITAS_API_KEYS", file_path: Optional[str] = None):
        self._env_var = env_var
        if file_path:
            self._file = Path(file_path)
        else:
            default_data_dir = Path(__file__).resolve().parents[2] / "data"
            self._file = default_data_dir / "api_keys.json"
        self._env_keys: Dict[str, AuthContext] = {}
        self._file_keys: Dict[str, AuthContext] = {}
        self._file_by_hash: Dict[str, AuthContext] = {}
        self._by_hash: Dict[str, AuthContext] = {}
        self._mtime: float = -1.0
        self._lock = threading.Lock()
        self.reload()

    # -- loading ----------------------------------------------------------

    def _parse_env(self) -> tuple:
        """Return (by_id, by_hash) from VERITAS_API_KEYS. Pure — no self mutation."""
        raw = os.environ.get(self._env_var, "").strip()
        by_id: Dict[str, AuthContext] = {}
        by_hash: Dict[str, AuthContext] = {}
        if not raw:
            return by_id, by_hash
        for entry in raw.split(","):
            parts = entry.strip().split(":")
            if len(parts) not in (3, 4):
                continue
            key_id, sha, rate = parts[0], parts[1], parts[2]
            if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha.lower()):
                continue  # malformed entries are skipped, never trusted
            try:
                rate_max = int(rate) if rate != "*" else None
            except ValueError:
                continue
            ctx = AuthContext(key_id=key_id, rate_max=rate_max, source="env")
            by_id[key_id] = ctx
            by_hash[sha.lower()] = ctx
        return by_id, by_hash

    def _parse_file(self) -> tuple:
        """Return (by_id, by_hash) from the JSON registry. Pure — no self mutation."""
        by_id: Dict[str, AuthContext] = {}
        by_hash: Dict[str, AuthContext] = {}
        try:
            if not self._file.exists():
                return by_id, by_hash
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return by_id, by_hash  # unreadable/corrupt registry -> no file keys (fail closed)
        for entry in data.get("keys", []):
            key_id = entry.get("key_id", "")
            sha = entry.get("sha256", "").lower()
            if not key_id or len(sha) != 64:
                continue
            if entry.get("revoked"):
                continue
            rate = entry.get("rate_max")
            ctx = AuthContext(
                key_id=key_id,
                rate_max=int(rate) if isinstance(rate, int) else None,
                source="file",
            )
            by_id[key_id] = ctx
            by_hash[sha] = ctx
        return by_id, by_hash

    def reload(self) -> None:
        """Rebuild the merged registry; cheap enough to call per-request.

        The file is re-parsed only when its mtime changes (cached), but the
        merged hash map is rebuilt on EVERY reload from the cached parse so
        file keys can never be silently dropped between reloads.
        """
        with self._lock:
            try:
                mtime = self._file.stat().st_mtime if self._file.exists() else -1.0
            except OSError:
                mtime = -1.0
            if mtime != self._mtime:
                self._mtime = mtime
                self._file_keys, self._file_by_hash = self._parse_file()
            # Env is cheap; re-parse every time so env changes apply live.
            self._env_keys, env_by_hash = self._parse_env()
            # Env wins on key-id/hash conflict.
            self._by_hash = {**self._file_by_hash, **env_by_hash}

    # -- verification ------------------------------------------------------

    def verify(self, presented_key: str) -> Optional[AuthContext]:
        """Constant-time lookup. Returns AuthContext or None (fail closed)."""
        if not isinstance(presented_key, str) or not presented_key.startswith(KEY_PREFIX):
            return None
        if len(presented_key) > 128:  # sanity bound before hashing
            return None
        presented_hash = hash_key(presented_key)
        self.reload()
        # The registry is keyed by hash; compare_digest guards against any
        # future transition to non-hash-indexed comparison paths.
        for sha, ctx in list(self._by_hash.items()):
            if hmac.compare_digest(sha, presented_hash):
                return ctx
        return None


_default_registry: Optional[KeyRegistry] = None


def get_registry() -> KeyRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = KeyRegistry()
    return _default_registry


def auth_enabled() -> bool:
    """Auth is ON unless explicitly disabled for local development."""
    return os.environ.get("VERITAS_AUTH_DISABLED", "") != "1"
