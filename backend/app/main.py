"""FastAPI app exposing POST /analyze and GET /health for the extension.

Security posture:
  * Every request field is length- and content-bounded (pydantic).
  * Per-client rate limiting (sliding window + concurrency cap).
  * CORS restricted to the extension's own origins (chrome-extension://).
  * URLs are validated by the centralized security layer; invalid URLs fail
    closed with a generic 4xx (no internal details leak).
  * Unknown errors return a generic message; details stay in server logs
    (without page content or credentials).
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

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
    allow_origins=_allowed_origins,  # empty = same-origin only until configured
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
    max_age=600,
)

# Abuse protection: per-client (identity or IP) sliding window.
_limiter = SlidingWindowLimiter(
    max_requests=int(os.environ.get("VERITAS_RATE_MAX", "30")),
    window_seconds=float(os.environ.get("VERITAS_RATE_WINDOW_SEC", "60")),
    max_concurrent=int(os.environ.get("VERITAS_MAX_CONCURRENT", "4")),
)

MAX_HTML_BYTES = 400_000


class AnalyzeRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    region: str = Field("in", min_length=2, max_length=12)
    html: Optional[str] = Field(None, max_length=MAX_HTML_BYTES)
    headline: Optional[str] = Field(None, max_length=512)
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


@app.post("/analyze")
def analyze_endpoint(req: AnalyzeRequest, request: Request):
    key = client_key(
        request.client.host if request.client else "unknown",
        api_key_hash="",  # no auth in local/dev mode; see README auth section
    )
    allowed, _retry = _limiter.check(key)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    try:
        result = analyze(
            url=req.url,
            region=req.region,
            html=req.html,
            headline_override=req.headline,
            include_cross_check=req.include_cross_check,
        )
        return result.to_dict()
    except SecurityError:
        raise HTTPException(status_code=400, detail="invalid request")
    except Exception:
        # Log server-side without page content; return a generic error.
        logger.exception("analyze failed")  # traceback stays in server logs
        raise HTTPException(status_code=502, detail="analysis failed")
    finally:
        _limiter.release(key)
