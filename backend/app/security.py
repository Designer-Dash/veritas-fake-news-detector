"""Centralized security controls for the Veritas backend.

Everything untrusted -- extension payloads, page URLs, RSS item links,
image URLs -- flows through this module. Design rules:

  * URL validation is scheme+host+port+IP based (SSRF defense): only https
    (http only behind an explicit flag for the local test backend), public
    hosts, and standard ports are allowed; every resolved IP must be public.
  * All network I/O goes through safe_fetch() which enforces size caps,
    timeouts, redirects re-validated per hop, and a hard redirect limit.
  * Per-client rate limiting (sliding window, in-memory).
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

USER_AGENT = "Veritas-FND/0.1 (educational fake-news research; contact: dev@example.org)"

DEFAULT_MAX_BYTES = 1_500_000
DEFAULT_TIMEOUT = 8
MAX_REDIRECTS = 3
ALLOW_HTTP = False  # flipped on only for the local test backend


class SecurityError(ValueError):
    """Raised when an untrusted input fails validation. Fail closed."""


# ---------------------------------------------------------------------------
# URL validation (SSRF defense)
# ---------------------------------------------------------------------------

_SCHEME_ALLOWLIST = {"https"}
_PORT_ALLOWLIST = {443, 80}
_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}


def _resolve_ips(hostname: str) -> List[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(hostname, None)]


def validate_url(
    url: str,
    *,
    allow_http: Optional[bool] = None,
    resolve: bool = True,
    context: str = "url",
) -> str:
    """Return the URL unchanged if it is safe to fetch; raise SecurityError otherwise.

    Checks: scheme allowlist, explicit host+port present, no userinfo, port
    allowlist, and -- for fetchable URLs -- every resolved IP is globally
    public (blocks localhost, RFC1918, link-local, CGNAT, and cloud metadata).
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SecurityError(f"{context}: missing or oversized URL")

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise SecurityError(f"{context}: unparseable URL") from exc

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = _SCHEME_ALLOWLIST | ({"http"} if (allow_http if allow_http is not None else ALLOW_HTTP) else set())
    if scheme not in allowed_schemes:
        raise SecurityError(f"{context}: scheme {scheme!r} not allowed")

    if not parsed.hostname:
        raise SecurityError(f"{context}: missing hostname")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise SecurityError(f"{context}: userinfo in URL not allowed")

    try:
        port = parsed.port
    except ValueError as exc:
        raise SecurityError(f"{context}: invalid port") from exc
    if port is not None and port not in _PORT_ALLOWLIST:
        raise SecurityError(f"{context}: port {port} not allowed")

    host = parsed.hostname.lower()
    if host in _FORBIDDEN_HOSTS:
        raise SecurityError(f"{context}: host not allowed")

    try:
        ipaddress.ip_address(host)  # literal IP?
        ips = [host]
    except ValueError:
        if resolve:
            try:
                ips = _resolve_ips(host)
            except socket.gaierror as exc:
                raise SecurityError(f"{context}: host does not resolve") from exc
            if not ips:
                raise SecurityError(f"{context}: host does not resolve")
        else:
            ips = []

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or addr.is_global is False
        ):
            raise SecurityError(f"{context}: non-public IP {ip} not allowed")

    return url


def validate_display_url(url: str, context: str = "url") -> str:
    """Validate a URL that will be *shown* to the user (not fetched).

    Scheme must be https and the host must be a real public DNS name.
    """
    return validate_url(url, resolve=False, context=context)


# ---------------------------------------------------------------------------
# Safe fetching
# ---------------------------------------------------------------------------

def safe_fetch(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    method: str = "GET",
    extra_headers: Optional[Dict[str, str]] = None,
    allow_http: Optional[bool] = None,
    _depth: int = 0,
) -> Optional[bytes]:
    """Fetch a URL with SSRF-validated redirects, size caps and timeouts.

    Returns None on *any* network or validation failure -- callers treat None
    as "signal unavailable", never as "content is clean".
    """
    try:
        current = validate_url(url, allow_http=allow_http, context="fetch")
    except SecurityError:
        return None

    if _depth > MAX_REDIRECTS:
        return None

    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(current, headers=headers, method=method)

    class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            try:
                validate_url(newurl, allow_http=allow_http, context="redirect")
            except SecurityError:
                return None  # urllib treats None as "do not follow"
            if _depth + 1 > MAX_REDIRECTS:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(max_bytes)
    except Exception:
        return None


def fetch_media_head(
    url: str,
    max_bytes: int = 262_144,
    timeout: float = 6,
) -> Optional[bytes]:
    """First `max_bytes` of an image (Range request) for metadata scanning."""
    return safe_fetch(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        extra_headers={"Range": f"bytes=0-{max_bytes - 1}"},
    )


# ---------------------------------------------------------------------------
# Rate limiting (per-client sliding window, in-memory)
# ---------------------------------------------------------------------------

class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: float, max_concurrent: int = 4):
        self.max_requests = max_requests
        self.window = window_seconds
        self.max_concurrent = max_concurrent
        self._hits: Dict[str, List[float]] = {}
        self._inflight: Dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> Tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            # Concurrency rejection must not consume rate budget, or retries
            # compound the penalty on the client.
            if self._inflight.get(key, 0) >= self.max_concurrent:
                self._hits[key] = hits
                return False, 0.0
            if len(hits) >= self.max_requests:
                retry = self.window - (now - hits[0])
                self._hits[key] = hits
                return False, max(retry, 0.0)
            hits.append(now)
            self._hits[key] = hits
            self._inflight[key] = self._inflight.get(key, 0) + 1
            return True, 0.0

    def release(self, key: str) -> None:
        with self._lock:
            self._inflight[key] = max(0, self._inflight.get(key, 0) - 1)


def client_key(ip: str, api_key_hash: str = "") -> str:
    """Rate-limit identity: authenticated identity if present, else IP."""
    return api_key_hash or (ip or "unknown")
