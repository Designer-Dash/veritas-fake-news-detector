"""Minimal Gemini client for *structured analysis only*.

Gemini never decides whether news is true. It is used for structured JSON
analysis (extracting/normalizing claims, comparing two claims, classifying
their relationship); the deterministic scoring in ``app.verification`` owns
the final Evidence Score.

Design rules for a limited/free API key:

  * Budgeted: at most ``VERITAS_GEMINI_MAX_CALLS`` (default 2) calls per
    verification, enforced with a per-request ``CallBudget`` object so
    concurrent analyses never starve each other.
  * Cached: identical prompts are answered from an in-memory TTL cache.
  * Fail-safe: missing key, rate limit (429), timeout or malformed output
    all return ``None`` so callers fall back to deterministic logic.
  * No key in logs: HTTP error URLs contain the API key, so only status
    codes are ever logged.

Enable by exporting ``VERITAS_GEMINI_API_KEY`` (or ``GEMINI_API_KEY``);
set ``VERITAS_GEMINI_ENABLED=0`` to force deterministic mode.

Signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

from .cache import get_cache
from .config import get_config
from .security import SecurityError, validate_url

logger = logging.getLogger("veritas.gemini")

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_KEY_ENV_VARS = ("VERITAS_GEMINI_API_KEY", "GEMINI_API_KEY")
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class CallBudget:
    """Per-request Gemini call budget (thread-local by construction: one
    budget object belongs to one analysis run)."""

    def __init__(self, max_calls: int):
        self.max_calls = max(1, int(max_calls))
        self.used = 0

    def allow(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def gemini_api_key() -> str:
    for name in _KEY_ENV_VARS:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    return ""


def gemini_enabled() -> bool:
    """Live check so tests (and ops) can disable Gemini without restarts."""
    if os.environ.get("VERITAS_GEMINI_ENABLED", "1").strip() == "0":
        return False
    return bool(gemini_api_key())


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def gemini_json(
    prompt: str,
    *,
    cache_key: Optional[str] = None,
    budget: Optional[CallBudget] = None,
) -> Optional[Any]:
    """Send one prompt, return parsed JSON, or None on any failure."""
    cfg = get_config()
    if not gemini_enabled():
        return None

    cache = get_cache("gemini", maxsize=256, ttl=cfg.gemini_cache_ttl)
    if cache_key:
        hit, value = cache.get(cache_key)
        if hit:
            logger.debug("gemini cache hit for %s", cache_key[:16])
            return value

    if budget is not None and not budget.allow():
        logger.debug("gemini call budget exhausted; using deterministic fallback")
        return None

    key = gemini_api_key()
    try:
        # SSRF guard on the fixed, code-allowlisted API host (shape-only:
        # the host is a constant, no DNS round-trip needed).
        validate_url(_API_BASE, resolve=False, context="gemini")
    except SecurityError:
        logger.warning("gemini API host failed validation; skipping call")
        return None

    url = f"{_API_BASE}/{cfg.gemini_model}:generateContent?key={key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 2048,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.gemini_timeout) as resp:
            raw = resp.read(512_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Never log the exception text: HTTPError strings include the request
        # URL, which carries the API key.
        if exc.code == 429:
            logger.warning("gemini rate limited (429); falling back to deterministic analysis")
        else:
            logger.warning("gemini HTTP %s; falling back to deterministic analysis", exc.code)
        return None
    except Exception:
        logger.warning("gemini request failed; falling back to deterministic analysis")
        return None

    try:
        payload = json.loads(raw)
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(_strip_fences(text))
    except Exception:
        logger.warning("gemini response was not valid structured JSON; falling back")
        return None

    if cache_key:
        cache.set(cache_key, data)
    return data
