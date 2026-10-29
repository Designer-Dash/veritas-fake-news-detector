# backend/app/rss_fetch.py
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

try:
    from defusedxml import ElementTree as ET   # preferred for untrusted XML
except ImportError:                             # pragma: no cover
    import xml.etree.ElementTree as ET

from app.security import safe_fetch

JITTER_S = (0.05, 0.15)          # tests can set this to (0, 0)
TTL_S, NEG_TTL_S, CACHE_MAX = 600.0, 30.0, 512
_LOCALES = (("en-IN", "IN", "IN:en"), ("en-US", "US", "US:en"))

_cache: dict[tuple, tuple[float, list["RssItem"]]] = {}   # key -> (expires_at, items)
_inflight: dict[tuple, Future] = {}
_lock = threading.Lock()
_google_blocked_until = 0.0


@dataclass(frozen=True)
class RssItem:
    title: str
    link: str
    source_name: str
    source_url: str
    published: Optional[float]
    provider: str


def clear_cache() -> None:
    global _google_blocked_until
    with _lock:
        _cache.clear()
        _inflight.clear()
    _google_blocked_until = 0.0


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    """Adapter around safe_fetch: adjust to its real return/raise behaviour."""
    try:
        resp = safe_fetch(url, timeout=timeout)
        if resp is None:
            return 0, b""
        if isinstance(resp, bytes):
            return 200, resp
        return getattr(resp, "status_code", 200), getattr(resp, "content", resp)
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "code", None)
        return (int(status) if status else 0), b""


def _pick(kids: dict, *names: str):
    for n in names:                      # explicit None check: empty Elements are falsy
        if kids.get(n) is not None:
            return kids[n]
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_rss(body: bytes, provider: str) -> list[RssItem]:
    try:
        root = ET.fromstring(body)
    except Exception:
        return []
    out = []
    for item in root.iter("item"):
        kids = {_local(c.tag): c for c in item}
        title = ((_pick(kids, "title").text) if _pick(kids, "title") is not None else "") or ""
        link_el = _pick(kids, "link")
        link = (link_el.text or "").strip() if link_el is not None else ""
        src = _pick(kids, "source", "Source")
        s_name = (src.text or "").strip() if src is not None else ""
        s_url = src.attrib.get("url", "") if src is not None else ""
        if provider == "bing":                       # unwrap Bing's apiclick redirect
            link = parse_qs(urlparse(link).query).get("url", [link])[0]
            if not s_url and link:
                s_url = f"https://{urlparse(link).netloc}"
        published = None
        pd = _pick(kids, "pubDate")
        if pd is not None and pd.text:
            try:
                published = parsedate_to_datetime(pd.text).timestamp()
            except Exception:
                pass
        if title.strip() and link:
            out.append(RssItem(title.strip(), link, s_name, s_url, published, provider))
    return out


def _url(provider: str, query: str, locale: tuple) -> str:
    q = quote_plus(query)
    if provider == "google":
        hl, gl, ceid = locale
        return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    return f"https://www.bing.com/news/search?q={q}&format=rss&setmkt={locale[0]}"


def _request(provider: str, query: str, locale: tuple, timeout: float) -> list[RssItem]:
    global _google_blocked_until
    time.sleep(random.uniform(*JITTER_S))
    status, body = _get(_url(provider, query, locale), timeout)
    if provider == "google" and status in (429, 503):
        _google_blocked_until = time.time() + 60      # circuit breaker; honour Retry-After if safe_fetch exposes it
        return []
    return parse_rss(body, provider) if status == 200 else []


def _store(key: tuple, items: list[RssItem]) -> None:
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (time.time() + (TTL_S if items else NEG_TTL_S), items)


def fetch_rss(query: str, attempt: int = 0, timeout: float = 1.8) -> list[RssItem]:
    """Cached, de-duplicated in flight, Google first then Bing. Never raises."""
    locale = _LOCALES[attempt % len(_LOCALES)]
    key = (" ".join(sorted(query.lower().split())), locale[0])   # word order doesn't matter
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        fut = _inflight.get(key)
        owner = fut is None
        if owner:
            fut = _inflight[key] = Future()
    if not owner:
        try:
            return fut.result(timeout=timeout + 1)
        except Exception:
            return []
    items: list[RssItem] = []
    try:
        if time.time() >= _google_blocked_until:
            items = _request("google", query, locale, timeout)
        if not items:
            items = _request("bing", query, locale, timeout)
        _store(key, items)
    except Exception:
        items = []
    finally:
        fut.set_result(items)
        with _lock:
            _inflight.pop(key, None)
    return items
