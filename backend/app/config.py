"""Central configuration for the claim-based verification & scoring system.

Every threshold, weight and limit used by the evidence pipeline lives here
and can be tuned through environment variables (prefix ``VERITAS_``), so
tuning never requires rewriting scoring code.

Key knobs (defaults shown):

    VERITAS_W_SEMANTIC / VERITAS_W_CONSENSUS / VERITAS_W_FACTS / VERITAS_W_SOURCE
        Evidence Score weights: E = w_m*M + w_c*C + w_f*F + w_s*S
    VERITAS_TIER1_WEIGHT / VERITAS_TIER2_WEIGHT / VERITAS_TIER3_WEIGHT /
    VERITAS_UNKNOWN_WEIGHT
        Source-quality weights per trust tier (see app.sources).
    VERITAS_MIN_SOURCES / VERITAS_MAX_SOURCES
        Verification window: minimum 5, maximum 10 reference sources.
    VERITAS_CONTRADICTION_PENALTY
        Flat deduction applied when a trusted source contradicts the claim.
    VERITAS_BREAKING_WINDOW_HOURS / VERITAS_BREAKING_MIN_SOURCES
        Recency window for BREAKING_NEWS / EARLY_REPORT internal state.
    VERITAS_BREAKING_MAX_MEDIAN_AGE_HOURS
        Freshness gate: the reference pool's median source age must be at
        most this (stale coverage of an old event is never "breaking").
    VERITAS_DEBUG
        1/true -> emit one structured JSON debug_payload per analysis at
        DEBUG level (developer diagnostics; never exposed to users).

Signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _s(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class VerificationConfig:
    # -- Evidence Score weights: E = w_m*M + w_c*C + w_f*F + w_s*S --------
    w_semantic: float = 0.30      # M: semantic/claim similarity
    w_consensus: float = 0.30     # C: weighted cross-source consensus
    w_facts: float = 0.20         # F: factual consistency
    w_source: float = 0.20        # S: source-quality score

    # -- Source tiers (weights applied via app.sources.source_weight) ------
    tier_weight_1: float = 1.00   # reference/trusted source
    tier_weight_2: float = 0.85   # established source
    tier_weight_3: float = 0.65   # lower-confidence source
    unknown_weight: float = 0.40  # unknown source

    # -- Weighted cross-source consensus -----------------------------------
    unknown_factor: float = 0.5   # unknown/no-report evidence counts half
    min_sources: int = 5          # fewer -> INSUFFICIENT_EVIDENCE
    max_sources: int = 10         # hard cap on processed reference sources

    # -- Relationship classification thresholds (0-100 similarity) ---------
    supports_similarity: float = 60.0
    relevance_similarity: float = 40.0   # below this: not about our claim
    denial_similarity: float = 40.0      # min similarity for denial language
    retrieval_floor: float = 0.15        # RSS results below this are dropped

    # -- Factual consistency ------------------------------------------------
    fact_critical_weight: float = 2.0    # critical facts count double
    fact_unmentioned_factor: float = 0.5  # unverified != disproved (half credit)

    # -- Contradiction protection -------------------------------------------
    contradiction_penalty: float = 15.0
    strong_contradiction_min_weight: float = 0.85     # tier-1/tier-2 sources
    strong_contradiction_min_similarity: float = 40.0

    # -- Fallback behaviour --------------------------------------------------
    no_coverage_penalty: float = 0.15         # no reference source at all
    breaking_no_coverage_penalty: float = 0.05  # very recent story, no coverage yet
    neutral_score: float = 50.0     # used when a component has no data
    # E -> suspicion delta: suspicion += (0.5 - E_final/100) * slope.
    # E = 50 is neutral; slope 1.0 means each evidence point above/below 50
    # moves suspicion by 0.01 (symmetric, explainable, bounded by clamps).
    cross_weight_slope: float = 1.0

    # -- Breaking news --------------------------------------------------------
    # The window is in hours-to-days, not ~6h, on purpose: the extension
    # sends plain text, so article dates often come from a URL path segment
    # (midnight UTC, no time of day) -- a 6h window would disable breaking
    # detection for most of the day. Tighten only for payloads that carry a
    # full timestamp.
    breaking_window_hours: float = 48.0
    breaking_min_sources: int = 3
    # Source-freshness gate: article recency alone is not enough -- a stale
    # reference pool (median age above this) means recycled coverage of an
    # older event, not a fresh story. Unknown source dates never block.
    breaking_max_median_age_hours: float = 2.0

    # -- Developer diagnostics (internal only; never part of the UI) ----------
    debug: bool = False   # VERITAS_DEBUG=1 -> one JSON debug_payload per run

    # -- Gemini (structured analysis only; never the final decision) ----------
    gemini_model: str = "gemini-2.0-flash"
    gemini_max_calls: int = 2       # per verification: extract + batch-compare
    gemini_timeout: float = 8.0

    # -- Search / retrieval ---------------------------------------------------
    max_queries: int = 3            # 2-3 optimized search queries per article
    max_query_sources: int = 12     # RSS items fetched per search query

    # -- Caches (API efficiency: avoid duplicate searches/verification) -------
    claim_cache_ttl: float = 900.0
    rss_cache_ttl: float = 600.0
    verification_cache_ttl: float = 300.0
    gemini_cache_ttl: float = 900.0


def get_config() -> VerificationConfig:
    """Build the config from the environment. Cheap enough to call per request,
    so env changes (and test monkeypatching) take effect immediately."""
    return VerificationConfig(
        w_semantic=_f("VERITAS_W_SEMANTIC", 0.30),
        w_consensus=_f("VERITAS_W_CONSENSUS", 0.30),
        w_facts=_f("VERITAS_W_FACTS", 0.20),
        w_source=_f("VERITAS_W_SOURCE", 0.20),
        tier_weight_1=_f("VERITAS_TIER1_WEIGHT", 1.00),
        tier_weight_2=_f("VERITAS_TIER2_WEIGHT", 0.85),
        tier_weight_3=_f("VERITAS_TIER3_WEIGHT", 0.65),
        unknown_weight=_f("VERITAS_UNKNOWN_WEIGHT", 0.40),
        unknown_factor=_f("VERITAS_UNKNOWN_FACTOR", 0.5),
        min_sources=max(1, _i("VERITAS_MIN_SOURCES", 5)),
        max_sources=max(1, _i("VERITAS_MAX_SOURCES", 10)),
        supports_similarity=_f("VERITAS_SUPPORTS_SIMILARITY", 60.0),
        relevance_similarity=_f("VERITAS_RELEVANCE_SIMILARITY", 40.0),
        denial_similarity=_f("VERITAS_DENIAL_SIMILARITY", 40.0),
        retrieval_floor=_f("VERITAS_RETRIEVAL_FLOOR", 0.15),
        fact_critical_weight=_f("VERITAS_FACT_CRITICAL_WEIGHT", 2.0),
        fact_unmentioned_factor=_f("VERITAS_FACT_UNMENTIONED_FACTOR", 0.5),
        contradiction_penalty=_f("VERITAS_CONTRADICTION_PENALTY", 15.0),
        strong_contradiction_min_weight=_f("VERITAS_STRONG_CONTRA_WEIGHT", 0.85),
        strong_contradiction_min_similarity=_f("VERITAS_STRONG_CONTRA_SIM", 40.0),
        no_coverage_penalty=_f("VERITAS_NO_COVERAGE_PENALTY", 0.15),
        breaking_no_coverage_penalty=_f("VERITAS_BREAKING_PENALTY", 0.05),
        neutral_score=_f("VERITAS_NEUTRAL_SCORE", 50.0),
        cross_weight_slope=_f("VERITAS_CROSS_SLOPE", 1.0),
        breaking_window_hours=_f("VERITAS_BREAKING_WINDOW_HOURS", 48.0),
        breaking_min_sources=max(1, _i("VERITAS_BREAKING_MIN_SOURCES", 3)),
        breaking_max_median_age_hours=_f("VERITAS_BREAKING_MAX_MEDIAN_AGE_HOURS", 2.0),
        debug=_b("VERITAS_DEBUG"),
        gemini_model=_s("VERITAS_GEMINI_MODEL", "gemini-2.0-flash"),
        gemini_max_calls=max(1, _i("VERITAS_GEMINI_MAX_CALLS", 2)),
        gemini_timeout=_f("VERITAS_GEMINI_TIMEOUT", 8.0),
        max_queries=max(1, _i("VERITAS_MAX_QUERIES", 3)),
        max_query_sources=max(1, _i("VERITAS_MAX_QUERY_SOURCES", 12)),
        claim_cache_ttl=_f("VERITAS_CLAIM_CACHE_TTL", 900.0),
        rss_cache_ttl=_f("VERITAS_RSS_CACHE_TTL", 600.0),
        verification_cache_ttl=_f("VERITAS_VERIFICATION_CACHE_TTL", 300.0),
        gemini_cache_ttl=_f("VERITAS_GEMINI_CACHE_TTL", 900.0),
    )
