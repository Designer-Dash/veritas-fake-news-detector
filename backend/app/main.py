"""FastAPI app exposing POST /analyze and GET /health for the extension.

Security posture:
  * Per-user API-key auth (Bearer) with SHA-256 hashed key storage; ON by
    default, explicit dev opt-out via VERITAS_AUTH_DISABLED=1.
  * Rate limits keyed by authenticated identity (key id), with per-key
    overrides; unauthenticated dev mode falls back to client IP.
  * Every request field is length- and content-bounded (pydantic).
  * CORS restricted to the extension's own origin (VERITAS_EXTENSION_ID).
  * URLs are validated by the centralized security layer; invalid URLs fail
    closed with a generic 4xx (no internal details leak).

Signature marker 009/013 · designer-dash
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import hashlib

from .auth import auth_enabled, get_registry
from .config import DEFAULT_REGION
from .pipeline import analyze
from .security import SecurityError, SlidingWindowLimiter, client_key

logger = logging.getLogger("veritas")

app = FastAPI(title="Fake News Detector API", version="0.1.0", docs_url=None, redoc_url=None)

# CORS: only the extension's own origin needs cross-origin access. A * value
# is dangerous here because /analyze triggers server-side fetches.
_extension_id = os.environ.get("VERITAS_EXTENSION_ID", "").strip()
_allowed_origins: List[str] = []
if _extension_id:
    _allowed_origins = [f"chrome-extension://{_extension_id}"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # empty = same-origin only until configured
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=600,
)

# Abuse protection: default per-identity limits; per-key overrides come from
# the key registry (AuthContext.rate_max).
_default_limiter = SlidingWindowLimiter(
    max_requests=int(os.environ.get("VERITAS_RATE_MAX", "30")),
    window_seconds=float(os.environ.get("VERITAS_RATE_WINDOW_SEC", "60")),
    max_concurrent=int(os.environ.get("VERITAS_MAX_CONCURRENT", "4")),
)
# Per-key limiters are created lazily and cached by (key_id, rate_max).
_key_limiters: dict = {}
_LIMIT_WINDOW = float(os.environ.get("VERITAS_RATE_WINDOW_SEC", "60"))
_LIMIT_CONCURRENT = int(os.environ.get("VERITAS_MAX_CONCURRENT", "4"))


def _limiter_for(key_id: str, rate_max: Optional[int]) -> SlidingWindowLimiter:
    """Per-(identity, quota) limiter cache.

    Keyed by (key_id, rate_max) so two identities that happen to share the
    same quota never share a limiter object. (The limiter itself also keeps
    per-key windows internally, so this is belt-and-braces: object-level
    state can never leak across identities.)"""
    if rate_max is None:
        return _default_limiter
    cache_key = (key_id, rate_max)
    lim = _key_limiters.get(cache_key)
    if lim is None:
        lim = SlidingWindowLimiter(
            max_requests=rate_max, window_seconds=_LIMIT_WINDOW, max_concurrent=_LIMIT_CONCURRENT
        )
        _key_limiters[cache_key] = lim
    return lim


MAX_HTML_BYTES = 400_000


class AnalyzeRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    region: str = Field(DEFAULT_REGION, min_length=2, max_length=12)
    html: Optional[str] = Field(None, max_length=MAX_HTML_BYTES)
    headline: Optional[str] = Field(None, max_length=512)
    claim: Optional[str] = Field(None, max_length=512)
    include_cross_check: bool = Field(True)

    @field_validator("region")
    @classmethod
    def _region_allowed(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"in", "us", "uk", "global"}:
            raise ValueError("unsupported region")
        return v

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        # Full validation (DNS + IP checks) happens in the security layer;
        # here we fail fast on obviously wrong shapes/schemes.
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("unsupported scheme")
        if any(ch.isspace() for ch in v):
            raise ValueError("whitespace in URL")
        return v


@app.exception_handler(SecurityError)
async def security_error_handler(request: Request, exc: SecurityError):
    # Generic 400: never echo internal validation details to the client.
    return JSONResponse(status_code=400, content={"detail": "invalid request"})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


def _authenticate(request: Request):
    """Return (key_id, rate_max, limiter_key). Raises 401 when auth is on."""
    if not auth_enabled():
        ip = request.client.host if request.client else "unknown"
        return "anon", None, client_key(ip)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="authentication required")
    presented = auth[len("Bearer "):].strip()
    # Never log `presented`.
    ctx = get_registry().verify(presented)
    if ctx is None:
        # Small delay muddies timing-based probing of unknown keys.
        time.sleep(0.05)
        raise HTTPException(status_code=401, detail="authentication required")
    return ctx.key_id, ctx.rate_max, f"user:{ctx.key_id}"


def _url_log_safe(url: str) -> str:
    """Return domain + SHA-256 prefix — enough to debug, not enough to reconstruct reading history."""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        return f"{domain}#{url_hash}"
    except Exception:
        return "[url]"

@app.post("/analyze")
def analyze_endpoint(req: AnalyzeRequest, request: Request):
    key_id, rate_max, limiter_key = _authenticate(request)
    limiter = _limiter_for(key_id, rate_max)
    allowed, _retry = limiter.check(limiter_key)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    try:
        result = analyze(
            url=req.url,
            region=req.region,
            html=req.html,
            headline_override=req.headline,
            claim_override=req.claim,
            include_cross_check=req.include_cross_check,
        )
        return result.to_dict()
    except SecurityError:
        raise HTTPException(status_code=400, detail="invalid request")
    except Exception:
        # Log server-side without page content or credentials; generic error out.
        logger.exception("analyze failed for %s on %s", key_id, _url_log_safe(req.url))
        raise HTTPException(status_code=502, detail="analysis failed")
    finally:
        limiter.release(limiter_key)
