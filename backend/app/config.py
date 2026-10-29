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


def _tld_whitelist() -> tuple:
    """Registrable domains exempt from the lookalike-TLD penalty, from the
    comma-separated VERITAS_TLD_WHITELIST env var."""
    raw = os.environ.get("VERITAS_TLD_WHITELIST", "")
    return tuple(sorted({d.strip().lower() for d in raw.split(",") if d.strip()}))


# Single source of truth for the default analysis region. Imported by
# app.main (AnalyzeRequest default) and app.pipeline (analyze default); the
# extension keeps its own mirrored constant (JS cannot import Python).
DEFAULT_REGION = "in"


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

    # -- Trained text-risk model (advisory signal; never a verdict) ----------
    # The bundled model is standard-library multinomial Naive Bayes trained on
    # labelled Indian news text (English headlines + full Hindi articles). It
    # is deliberately capped, only applies when its vocabulary covers enough of
    # the input, and is further gated by the artifact's own deployment decision
    # (a model that failed any gate condition contributes zero weight). The
    # original LIAR claim-level baseline is retained for comparison and can be
    # selected with VERITAS_TEXT_MODEL_PATH. Cross-source scoring remains the
    # system's factual-verification authority.
    text_model_enabled: bool = True
    text_model_min_tokens: int = 30
    text_model_min_coverage: float = 0.10
    text_model_false_threshold: float = 0.80
    text_model_max_weight: float = 0.18
    text_model_allow_unqualified: bool = False

    # -- Search / retrieval ---------------------------------------------------
    max_queries: int = 3            # 2-3 optimized search queries per article
    max_query_sources: int = 12     # RSS items fetched per search query
    # Always attempt this many separately scoped publisher searches before
    # returning a cross-source result. This is an attempt count, not a claim
    # that every outlet has covered the story: thin/local/breaking stories
    # must remain INSUFFICIENT_EVIDENCE rather than gaining invented sources.
    outlet_checks: int = 5          # bounded to 5-10; Google News site: queries
    retrieval_deadline_s: float = 2.5 # max latency for parallel multi-wave search

    # -- Caches (API efficiency: avoid duplicate searches/verification) -------
    claim_cache_ttl: float = 900.0
    rss_cache_ttl: float = 600.0
    verification_cache_ttl: float = 300.0
    gemini_cache_ttl: float = 900.0

    # -- Confidence display score (deterministic, env-tunable) ----------------
    # confidence = base + text bonus + verification bonus + domain bonus,
    # then capped. The cap equals the reachable maximum of those four terms
    # (0.55+0.15+0.15+0.05 = 0.90): no unreachable ceiling.
    confidence_base: float = 0.55
    confidence_text_bonus: float = 0.15           # enough text for stylometry
    confidence_verification_bonus: float = 0.15   # >=1 cross-checked source
    confidence_domain_bonus: float = 0.05         # known reputation signal
    confidence_cap: float = 0.90

    # -- Google Fact Check Tools API ------------------------------------------
    # Professional fact-check reviews (claimReview) blended into the F
    # component only — they never decide the verdict by themselves. The
    # whole feature is dormant unless VERITAS_FACT_CHECK_API_KEY is set;
    # the key is never logged and never appears in any response.
    factcheck_api_key: str = ""
    factcheck_enabled: bool = True
    factcheck_max_results: int = 5
    factcheck_timeout: float = 6.0
    factcheck_cache_ttl: float = 300.0    # same default as verification cache
    factcheck_weight: float = 0.30        # F' = (1-w)*F + w*FC, w in [0,1]
    factcheck_reviewer_weight: float = 1.0

    # -- Lookalike-TLD whitelist -----------------------------------------------
    # Registrable domains exempt from the lookalike-TLD suspicion (a
    # legitimate editorial outlet may live on a .news/.xyz-style domain).
    # Built-in default is empty; extend via VERITAS_TLD_WHITELIST.
    tld_whitelist: tuple = ()


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
        text_model_enabled=_b("VERITAS_TEXT_MODEL_ENABLED", default=True),
        text_model_min_tokens=max(1, _i("VERITAS_TEXT_MODEL_MIN_TOKENS", 30)),
        text_model_min_coverage=min(1.0, max(0.0, _f("VERITAS_TEXT_MODEL_MIN_COVERAGE", 0.10))),
        text_model_false_threshold=min(0.99, max(0.50, _f("VERITAS_TEXT_MODEL_FALSE_THRESHOLD", 0.80))),
        text_model_max_weight=min(0.25, max(0.0, _f("VERITAS_TEXT_MODEL_MAX_WEIGHT", 0.18))),
        text_model_allow_unqualified=_b("VERITAS_TEXT_MODEL_ALLOW_UNQUALIFIED", default=False),
        max_queries=max(1, _i("VERITAS_MAX_QUERIES", 3)),
        max_query_sources=max(1, _i("VERITAS_MAX_QUERY_SOURCES", 12)),
        outlet_checks=min(10, max(5, _i("VERITAS_OUTLET_CHECKS", 5))),
        claim_cache_ttl=_f("VERITAS_CLAIM_CACHE_TTL", 900.0),
        rss_cache_ttl=_f("VERITAS_RSS_CACHE_TTL", 600.0),
        verification_cache_ttl=_f("VERITAS_VERIFICATION_CACHE_TTL", 300.0),
        gemini_cache_ttl=_f("VERITAS_GEMINI_CACHE_TTL", 900.0),
        confidence_base=_f("VERITAS_CONFIDENCE_BASE", 0.55),
        confidence_text_bonus=_f("VERITAS_CONF_TEXT_BONUS", 0.15),
        confidence_verification_bonus=_f("VERITAS_CONF_VERIFICATION_BONUS", 0.15),
        confidence_domain_bonus=_f("VERITAS_CONF_DOMAIN_BONUS", 0.05),
        confidence_cap=_f("VERITAS_CONFIDENCE_CAP", 0.90),
        factcheck_api_key=os.environ.get("VERITAS_FACT_CHECK_API_KEY", "").strip(),
        factcheck_enabled=_b("VERITAS_FACT_CHECK_ENABLED", default=True),
        factcheck_max_results=max(1, _i("VERITAS_FACT_CHECK_MAX_RESULTS", 5)),
        factcheck_timeout=_f("VERITAS_FACT_CHECK_TIMEOUT", 6.0),
        factcheck_cache_ttl=_f("VERITAS_FACT_CHECK_CACHE_TTL", 300.0),
        factcheck_weight=min(1.0, max(0.0, _f("VERITAS_FACT_CHECK_WEIGHT", 0.30))),
        factcheck_reviewer_weight=max(0.0, _f("VERITAS_FACT_CHECK_REVIEWER_WEIGHT", 1.0)),
        retrieval_deadline_s=max(0.5, _f("VERITAS_RETRIEVAL_DEADLINE_S", 2.5)),
        tld_whitelist=_tld_whitelist(),
    )


settings = get_config()
