"""Google Fact Check Tools API client (Feature A).

Queries https://factchecktools.googleapis.com/v1alpha1/claims:search for
professional fact-check reviews (claimReview) matching the article's claim,
maps each review's textualRating into the same SUPPORTS / CONTRADICTS /
UNKNOWN vocabulary used by cross-source verification, and lets
app.verification.blend_fact_check() fold the result into the F (factual
consistency) component of the Evidence Score.

Design rules honored here:

  * stdlib-only networking through the same injectable fetch boundary as
    every other retrieval (``SourceRetriever.fetch`` / ``safe_fetch``):
    SSRF-guarded, size-capped, timeout-bounded.
  * The API key (VERITAS_FACT_CHECK_API_KEY) is used ONLY inside the
    request URL. It is never logged, never included in any returned
    structure, and never written to the debug payload.
  * Deterministic mapping only: a fact-check review never decides the
    verdict by itself, exactly like Gemini. It contributes a numeric
    adjustment to F and nothing else.
  * Aggressive caching: one cache entry per normalized claim with the same
    default TTL as the verification cache.
  * Dormant by default: with no API key configured every entry point
    returns [] without any network activity, so tests and offline use are
    unaffected.

Signature marker: fact-check integration (claim-based evidence scoring).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus

from .cache import get_cache
from .config import VerificationConfig, get_config
from .security import safe_fetch, validate_display_url
from .verification import CONTRADICTS, SUPPORTS, UNKNOWN

logger = logging.getLogger("veritas.factcheck")

_API_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

# Rating strings that indicate the reviewed claim is factually wrong.
_FALSE_MARKERS = (
    "pants on fire", "false", "fake", "hoax", "debunk", "incorrect",
    "baseless", "unfounded", "scam", "four pinocchios",
)
# Rating strings that indicate the claim distorts or cherry-picks facts
# (not flatly false, but not consistent either).
_MISLEADING_MARKERS = (
    "misleading", "mislabeled", "out of context", "distorts", "cherry",
    "half true", "partly", "mixture", "spins the facts", "exaggerat",
)
# Rating strings that indicate the claim checks out.
_TRUE_MARKERS = (
    "true", "accurate", "correct", "confirmed", "verified", "credible",
)

MIXED = "mixed"  # distinct from UNKNOWN: a review exists but is ambivalent


@dataclass
class FactCheckAssessment:
    """One professional fact-check review of (a variant of) the claim."""

    claimant: str
    rating: str                 # raw textualRating from the API, verbatim
    relationship: str           # SUPPORTS / CONTRADICTS / MIXED / UNKNOWN
    publisher: str = ""
    title: str = ""
    review_url: str = ""        # validated for display; "" when unusable
    weight: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claimant": self.claimant,
            "rating": self.rating,
            "relationship": self.relationship,
            "publisher": self.publisher,
            "title": self.title,
            "review_url": self.review_url,
            "weight": round(self.weight, 3),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FactCheckAssessment":
        return cls(
            claimant=str(d.get("claimant", "")),
            rating=str(d.get("rating", "")),
            relationship=str(d.get("relationship", UNKNOWN)),
            publisher=str(d.get("publisher", "")),
            title=str(d.get("title", "")),
            review_url=str(d.get("review_url", "")),
            weight=float(d.get("weight", 1.0)),
        )


def normalize_rating(rating: str) -> str:
    """Map a free-text textualRating to a relationship constant.

    Deterministic keyword matching on the lowercased rating. Ambiguous /
    misleading ratings are checked FIRST ("half true", "mixture of true and
    false" contain the words true/false but mean neither cleanly); then
    flatly false, then confirming; anything unrecognized is UNKNOWN.
    """
    r = str(rating or "").lower()
    if not r:
        return UNKNOWN
    if any(m in r for m in _MISLEADING_MARKERS):
        return MIXED
    if any(m in r for m in _FALSE_MARKERS):
        return CONTRADICTS
    if any(m in r for m in _TRUE_MARKERS):
        return SUPPORTS
    return UNKNOWN


def _cache_key(claim_text: str) -> str:
    return hashlib.sha256(claim_text.strip().lower().encode("utf-8", "ignore")).hexdigest()


def _parse_payload(
    payload: bytes, cfg: VerificationConfig
) -> Optional[List[FactCheckAssessment]]:
    """Extract up to factcheck_max_results reviews from the API response.

    Returns None when the payload is not valid JSON (transient error page):
    the caller then returns [] WITHOUT caching, so a momentary outage is
    retried on the next analysis. A well-formed response with zero reviews
    IS cached (aggressively, per the spec).
    """
    try:
        data = json.loads(payload.decode("utf-8", "ignore"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("fact-check: unparseable API response; ignoring (not cached)")
        return None
    reviews: List[FactCheckAssessment] = []
    claims = data.get("claims") if isinstance(data, dict) else None
    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        for review in claim.get("claimReview") or []:
            if not isinstance(review, dict):
                continue
            publisher = ""
            pub = review.get("publisher")
            if isinstance(pub, dict):
                publisher = str(pub.get("name", "")).strip()
            rating = str(review.get("textualRating", "")).strip()
            review_url = ""
            raw_url = str(review.get("url", "")).strip()
            if raw_url:
                try:
                    review_url = validate_display_url(raw_url, context="factcheck review")
                except Exception:
                    review_url = ""  # never surface an unvalidated URL
            reviews.append(
                FactCheckAssessment(
                    claimant=str(claim.get("claimant", "")).strip(),
                    rating=rating,
                    relationship=normalize_rating(rating),
                    publisher=publisher,
                    title=str(review.get("title", "")).strip()[:200],
                    review_url=review_url,
                    weight=float(cfg.factcheck_reviewer_weight),
                )
            )
            if len(reviews) >= cfg.factcheck_max_results:
                return reviews
    return reviews


def fetch_fact_checks(
    claim_text: str,
    *,
    cfg: Optional[VerificationConfig] = None,
    fetch_func: Optional[Callable[..., Optional[bytes]]] = None,
) -> List[FactCheckAssessment]:
    """Query the Fact Check Tools API for professional reviews of the claim.

    Returns [] when the feature is disabled (no API key), when the claim is
    empty, or on any network/parse failure — the caller must treat [] as
    "no external fact-check data", never as "the claim is fine".

    ``fetch_func`` follows the SourceRetriever contract
    ``(url, timeout=...) -> bytes | None`` so tests (and alternative HTTP
    stacks) can inject it; defaults to the SSRF-guarded ``safe_fetch``.
    """
    cfg = cfg or get_config()
    if not (cfg.factcheck_enabled and cfg.factcheck_api_key):
        return []
    claim_text = (claim_text or "").strip()
    if not claim_text:
        return []

    cache = get_cache("factcheck", maxsize=64, ttl=cfg.factcheck_cache_ttl)
    key = _cache_key(claim_text)
    hit, cached = cache.get(key)
    if hit:
        return [FactCheckAssessment.from_dict(d) for d in cached]

    # The key travels ONLY in this URL; it is never logged or returned.
    # Param names per the v1alpha1 claims:search reference (query, pageSize).
    url = (
        f"{_API_URL}?query={quote_plus(claim_text[:512])}"
        f"&pageSize={int(cfg.factcheck_max_results)}&key={quote_plus(cfg.factcheck_api_key)}"
    )
    fetch = fetch_func or safe_fetch
    raw = fetch(url, timeout=cfg.factcheck_timeout)
    if not raw:
        return []  # transient failure: do not cache, let the next try fetch

    reviews = _parse_payload(raw, cfg)
    if reviews is None:
        return []  # unparseable response: not cached, retried later
    cache.set(key, [r.to_dict() for r in reviews])
    return reviews


def summarize(fact_checks: List[FactCheckAssessment]) -> Dict[str, Any]:
    """JSON-safe summary for ai_signals (never contains the API key)."""
    counts = {SUPPORTS: 0, CONTRADICTS: 0, MIXED: 0, UNKNOWN: 0}
    for fc in fact_checks:
        counts[fc.relationship] = counts.get(fc.relationship, 0) + 1
    return {
        "checked": len(fact_checks),
        "supports": counts.get(SUPPORTS, 0),
        "contradicts": counts.get(CONTRADICTS, 0),
        "mixed": counts.get(MIXED, 0),
        "unknown": counts.get(UNKNOWN, 0),
        "reviews": [fc.to_dict() for fc in fact_checks[:5]],
    }
