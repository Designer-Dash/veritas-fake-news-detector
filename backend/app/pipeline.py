"""Analysis pipeline: combine domain heuristics, stylometry, AI signals and
claim-based cross-source verification into one explainable verdict.

Signature marker 008/013 · designer-dash
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .ai_signals import scan_bytes, scan_html_for_media_flags, score_media_signals
from .cache import get_cache
from .claim_analysis import extract_claim, extract_published_date, generate_search_queries
from .config import DEFAULT_REGION, get_config
from .cross_check import links_to_trusted, median_source_age, retrieve_reference_sources
from .domain import AnalysisResult, Evidence, SourceMatch, verdict_from_suspicion
from .factcheck import fetch_fact_checks, summarize as summarize_fact_checks
from .gemini_client import CallBudget
from .security import SecurityError, fetch_media_head, safe_fetch, validate_url
from .sources import region_name
from .text_analysis import (
    CLICKBAIT_PATTERNS,
    ai_stylometry,
    caps_exclaims,
    extract_article_text,
    extract_headline,
    extract_text_from_html,
    phrase_hits,
)
from .text_model import classify_news_text
from .verification import VerificationResult, get_final_verification_result

logger = logging.getLogger("veritas.pipeline")

# Domains with strong editorial accountability start with a trust bonus.
KNOWN_TRUSTED_PATTERNS = (
    "reuters.com", "apnews.com", "bbc.", "thehindu.com", "indiatoday.in",
    "timesofindia.indiatimes.com", "hindustantimes.com", "ndtv.com",
    "indianexpress.com", "theguardian.com", "nytimes.com", "washingtonpost.com",
    "npr.org", "ptinews.com", "aninews.in",
)

# Brand-impersonation tricks: lookalike TLDs and misleading hyphens.
# Single source of truth for domain_reputation(); entries like ".com.co"
# and ".co.in.biz" are the classic fake-outlet pattern (abcnews.com.co).
# Deliberately EXCLUDES .org/.net so trusted outlets (npr.org, wpxi.com)
# are never penalized.
LOOKALIKE_TLDS = (".com.co", ".co.in.biz", ".news", ".live", ".xyz", ".top", ".buzz")
SUSPICIOUS_NAME_PARTS = ("blog", "wp", "wordpress", "blogger", "free", "fun", "clip", "buzz")

# ---------------------------------------------------------------------------
# Article-type detection (user fix 4): opinions, satire, press releases and
# forecasts are not factual reports — cross-checking them produces a
# meaningless "Unverified" and wastes the retrieval/Gemini budget.
# ---------------------------------------------------------------------------

# Known satire domains
_SATIRE_DOMAINS = {
    "theonion.com", "clickhole.com", "babylonbee.com",
    "fauxy.com", "thefauxy.com",  # Indian satire sites
}
# Signals that this is not a factual news report
_OPINION_SIGNALS_HEADLINE = (
    "opinion:", "editorial:", "column:", "analysis:", "commentary:",
    "perspective:", "view:", "review:", "letter to", "open letter",
    "राय:", "विचार:", "संपादकीय:",  # Hindi equivalents
)
_PRESS_RELEASE_SIGNALS = (
    "press release", "pr newswire", "business wire",
    "globe newswire", "accesswire",
)
_FUTURE_WORDS = (
    "will", "would", "may", "might", "could", "expected to",
    "predicted", "forecast", "likely to", "set to", "poised to",
)


def detect_article_type(headline: str, text: str, domain: str) -> str:
    """Returns: 'news' | 'opinion' | 'satire' | 'press_release' | 'prediction'.

    Cheap lexical checks only — runs before any network/Gemini work. Future
    words use word-boundary matching so "Mayawati" does not count as "may"
    and "Switzerland" not as "will"."""
    h_lower = (headline or "").lower()
    t_lower = (text or "")[:500].lower()
    if (domain or "").lower() in _SATIRE_DOMAINS:
        return "satire"
    if any(h_lower.startswith(s) for s in _OPINION_SIGNALS_HEADLINE):
        return "opinion"
    if any(s in t_lower for s in _PRESS_RELEASE_SIGNALS):
        return "press_release"
    # Prediction/forecast articles — future-tense heavy headline.
    future_count = sum(
        1 for w in _FUTURE_WORDS if re.search(rf"\b{re.escape(w)}\b", h_lower)
    )
    if future_count >= 2:
        return "prediction"
    return "news"


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.split("@")[-1].split(":")[0] if netloc else ""
    except Exception:
        return ""


def _registrable_domain(host: str) -> str:
    """Approximate registrable domain: the last two labels, or the last
    three when the second-level label is a multi-part public suffix
    (com.co, co.in, co.uk, com.au, ...)."""
    labels = [l for l in (host or "").split(".") if l]
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "edu"}:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host or ""


def domain_reputation(domain: str) -> Dict[str, Any]:
    cfg = get_config()
    d = domain or ""
    weight = 0.0
    reasons: List[str] = []
    # A recognized editorial outlet is never also a content farm: the
    # trusted-pattern match short-circuits the lookalike penalty, so a
    # hypothetical bbc.news is credited for BBC instead of being flagged
    # for its TLD.
    if any(p in d for p in KNOWN_TRUSTED_PATTERNS):
        weight -= 0.25
        reasons.append("Well-known editorial outlet")
    else:
        # Lookalike check anchored to the registrable domain so subdomains
        # cannot trigger or mask it, with an env-extendable whitelist for
        # legitimate outlets that really do live on a trendy TLD.
        registrable = _registrable_domain(d)
        if registrable.endswith(LOOKALIKE_TLDS) and registrable not in cfg.tld_whitelist:
            weight += 0.2
            reasons.append("Lookalike/Trend TLD often used by content farms")
    if any(part in d for part in SUSPICIOUS_NAME_PARTS) and "news" in d:
        weight += 0.15
        reasons.append("Domain mixes blog/hosting terms with 'news'")
    if d.count("-") >= 2:
        weight += 0.1
        reasons.append("Hyphen-heavy domain")
    return {"weight": max(weight, -0.25), "reasons": reasons}


def fetch_page(url: str, timeout: int = 8) -> Optional[bytes]:
    """Fetch a page through the centralized SSRF-guarded fetcher.

    Returns None on any validation or network failure; callers must treat
    that as "content unavailable", not "content is clean".
    """
    return safe_fetch(url, timeout=timeout)


def find_lead_image_url(html: str) -> Optional[str]:
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not m:
        m = re.search(r"<img[^>]+src=[\"']([^\"']+\.(?:jpe?g|png|webp))", html, re.I)
    return m.group(1) if m else None


class SourceRetriever:
    """The single, injectable retrieval boundary for reference sources.

    Everything network-facing about cross-source retrieval funnels through
    ``SourceRetriever.fetch`` (SSRF-guarded ``safe_fetch`` by default), so:

      * tests patch exactly one place
        (``monkeypatch.setattr('app.pipeline.SourceRetriever.fetch', mock)``)
        instead of re-patching ``safe_fetch`` in every consuming module, and
      * a future source backend (Bing News, a fact-check API) can be swapped
        in via ``fetch_func`` or a subclass without touching scoring logic.

    ``fetch_func`` follows the ``safe_fetch`` contract: ``(url, timeout=...) -> bytes | None``.
    """

    def __init__(self, fetch_func: Optional[Callable[..., Optional[bytes]]] = None):
        self.fetch_func = fetch_func

    @staticmethod
    def fetch(url: str, timeout: int = 8) -> Optional[bytes]:
        """Default network boundary; tests replace this at class level."""
        return safe_fetch(url, timeout=timeout)

    def retrieve(
        self,
        claim_text: str,
        queries: List[str],
        region: str,
        page_domain: str = "",
        cfg: Any = None,
    ) -> List[Dict[str, Any]]:
        # Resolved via the class (not ``self``) so a monkeypatched plain
        # function is called unbound -- ``fetch(url)`` -- never as a method.
        fetch = self.fetch_func or SourceRetriever.fetch
        return retrieve_reference_sources(
            claim_text,
            queries,
            region=region,
            page_domain=page_domain,
            cfg=cfg,
            fetch_func=fetch,
        )


def analyze(
    url: str,
    region: str = DEFAULT_REGION,
    html: Optional[str] = None,
    headline_override: Optional[str] = None,
    claim_override: Optional[str] = None,
    include_cross_check: bool = True,
    retriever: Optional[SourceRetriever] = None,
) -> AnalysisResult:
    """Full analysis of a news page. `html` may be supplied by the extension
    (content script DOM snapshot) to avoid a refetch; otherwise we fetch it.
    `retriever` optionally injects a SourceRetriever (its `fetch` is the
    retrieval boundary, or a custom `fetch_func` for an alternative source
    backend); defaults to the class-level SourceRetriever()."""

    # Fail closed on malformed/unsupported page URLs (defense in depth -- the
    # API layer validates too; this protects direct callers of analyze()).
    # Shape-level validation: scheme, host/port/userinfo, and literal-IP
    # checks (metadata/loopback/RFC1918 rejected). DNS resolution is skipped
    # here; the actual fetch re-validates fully inside safe_fetch().
    if not isinstance(url, str) or len(url) > 2048:
        raise SecurityError("analyze: invalid url")
    validate_url(url, allow_http=True, resolve=False, context="analyze")

    dom = domain_of(url)
    
    # Privacy by design (user fix 1): if the caller pre-extracted the claim,
    # we completely skip processing the full article text on the server.
    if claim_override and not html:
        headline = headline_override or claim_override[:120]
        text = ""
        html = ""
    else:
        if html is None:
            raw = fetch_page(url)
            html = raw.decode("utf-8", errors="ignore") if raw else ""
        text = extract_article_text(html)
        headline = headline_override or extract_headline(html) or url

    # Article-type gate: return early — don't cross-check opinions or press
    # releases (Gemini is never invoked on this path; nothing is fetched).
    article_type = detect_article_type(headline, text, dom)
    if article_type != "news":
        type_messages = {
            "opinion": "This appears to be an opinion or editorial piece, not a factual news report.",
            "satire": "This site publishes satire and parody content.",
            "press_release": "This appears to be a press release, not independent journalism.",
            "prediction": "This article contains predictions or forecasts about future events.",
        }
        return AnalysisResult(
            url=url,
            domain=dom,
            verdict="uncertain",
            suspicion=0.0,
            confidence=0.5,
            headline=headline[:280],
            summary=type_messages.get(article_type, ""),
            evidence=[Evidence("Article type", type_messages[article_type], 0.0)],
            corroborating_sources=[],
            ai_signals={"article_type": article_type},
            coverage_state="article_type_" + article_type,
            is_breaking=False,
        )

    suspicion = 0.0
    evidence: List[Evidence] = []
    ai_signals: Dict[str, Any] = {}

    # 1. Domain reputation -------------------------------------------------
    rep = domain_reputation(dom)
    suspicion += rep["weight"]
    for r in rep["reasons"]:
        evidence.append(Evidence("Domain reputation", r, rep["weight"] / max(len(rep["reasons"]), 1)))

    # 2. Stylometry / AI-writing cues --------------------------------------
    styl = ai_stylometry(text)
    styl_weight = 0.0
    if not styl["low_signal"]:
        styl_weight = styl["ai_like"] * 0.35
        if styl["ai_like"] >= 0.6:
            evidence.append(
                Evidence(
                    "AI-writing cues",
                    f"Template-like prose (burstiness {styl['burstiness']}, "
                    f"vocabulary richness {styl['type_token_ratio']}, "
                    f"{styl['stock_phrase_count']} stock phrases)",
                    styl_weight,
                    styl,
                )
            )
    else:
        evidence.append(
            Evidence("AI-writing cues", "Too little text on page to judge writing style", 0.0, styl)
        )
    suspicion += styl_weight

    # 2a. Trained text-risk classifier (advisory only) -----------------------
    # Deliberately a bounded, one-sided risk signal. The model learns text
    # style from labelled Indian news, not independent evidence about *this*
    # article, so it can raise review priority but never lower suspicion and
    # never decide a verdict. It is additionally gated by the trained
    # artifact's own deployment decision: a model that did not clear every
    # documented gate condition contributes exactly zero weight. Factual
    # determination stays in the deterministic cross-source branch below.
    cfg = get_config()
    text_model = classify_news_text(
        text,
        enabled=cfg.text_model_enabled,
        min_tokens=cfg.text_model_min_tokens,
        min_coverage=cfg.text_model_min_coverage,
    )
    model_weight = 0.0
    if text_model.get("available"):
        # Prefer the calibrated risk score when the artifact ships one; fall
        # back to the raw posterior so older artifacts keep working unchanged.
        risk_score = float(text_model.get("risk_score", text_model["false_probability"]))
        model_is_eligible = bool(text_model.get("scoring_eligible"))
        if not model_is_eligible and not cfg.text_model_allow_unqualified:
            text_model["scoring_gate"] = "shadow_only_deployment_gate"
        if (
            (model_is_eligible or cfg.text_model_allow_unqualified)
            and risk_score >= cfg.text_model_false_threshold
            and cfg.text_model_max_weight > 0
        ):
            scale = (risk_score - cfg.text_model_false_threshold) / (1.0 - cfg.text_model_false_threshold)
            model_weight = min(max(scale, 0.0) * cfg.text_model_max_weight, cfg.text_model_max_weight)
            text_model["applied"] = True
            text_model["weight"] = round(model_weight, 4)
            evidence.append(
                Evidence(
                    "ML text-risk signal",
                    f"Advisory {text_model.get('model_version', 'text')} text classifier flagged "
                    f"elevated false-claim risk ({risk_score:.0%}); cross-source evidence "
                    f"remains decisive",
                    model_weight,
                    text_model,
                )
            )
            suspicion += model_weight
    ai_signals["text_classifier"] = text_model

    # 2b. Clickbait / sensationalism ----------------------------------------
    clickbait = phrase_hits(text, CLICKBAIT_PATTERNS)
    sens = caps_exclaims(text)
    sens_weight = min(len(clickbait) * 0.08 + sens * 0.10, 0.25)
    if sens_weight >= 0.05:
        suspicion += sens_weight
        evidence.append(
            Evidence(
                "Clickbait / sensationalism",
                f"{len(clickbait)} clickbait phrase(s), sensational punctuation {sens:.2f}",
                sens_weight,
                {"hits": clickbait[:5]},
            )
        )

    # 3. Media provenance (C2PA / SynthID / generator markers) -------------
    html_media = scan_html_for_media_flags(html)
    media_weight = 0.0
    media_reasons: List[str] = []
    img_url = find_lead_image_url(html)
    if img_url:
        raw = fetch_media_head(img_url, timeout=6)
        if raw:
            byte_scan = scan_bytes(raw)
            merged = score_media_signals(byte_scan, html_media)
            media_weight = merged["weight"]
            media_reasons = merged["reasons"]
            ai_signals["media"] = merged
    else:
        merged = score_media_signals({"digital_source_type": None, "tool_markers": [], "has_c2pa": False, "synthid_markers": 0}, html_media)
        media_weight = merged["weight"] * 0.5
        media_reasons = merged["reasons"] or ["No lead image found to inspect"]
        ai_signals["media"] = merged
    if media_reasons:
        evidence.append(Evidence("Media provenance", "; ".join(media_reasons), media_weight))
    suspicion += media_weight

    # 4. Claim-based cross-source verification (strongest 5-10 sources) ----
    # Gemini handles structured analysis only (claim extraction + ONE batched
    # comparison call); retrieval, scoring and the final Evidence Score are
    # deterministic and happen in app.verification.
    trusted_links = links_to_trusted(html, region, page_domain=dom)
    verification: Optional[VerificationResult] = None
    claim: Any = None
    queries: List[str] = []
    median_age: Optional[float] = None
    fact_checks: List[Any] = []

    if include_cross_check and headline:
        budget = CallBudget(cfg.gemini_max_calls)
        retriever = retriever or SourceRetriever()
        try:
            # Extract the claim once (cached) and reuse it for queries,
            # retrieval and every pairwise comparison.
            if claim_override:
                from .claim_analysis import AnalyzedClaim
                claim = AnalyzedClaim(claim=claim_override[:512], summary="")
            else:
                claim = extract_claim(headline, text, budget=budget)
            queries = generate_search_queries(claim, headline)
            published_date = extract_published_date(html, url=url)
            # Avoid re-verifying the same claim/page while the result is fresh.
            # The article's published date is part of the key: a corrected or
            # day-rolled date must never reuse a verdict (breaking-news state
            # depends on it). Source-pool age drift is bounded by the TTL.
            vcache = get_cache("verification", maxsize=64, ttl=cfg.verification_cache_ttl)
            vkey = hashlib.sha256(
                "\x00".join(
                    (
                        claim.claim,
                        region,
                        dom,
                        published_date.isoformat() if published_date else "",
                    )
                ).encode("utf-8", "ignore")
            ).hexdigest()
            hit, cached = vcache.get(vkey)
            if hit:
                verification = cached
            else:
                candidates = retriever.retrieve(
                    claim.claim, queries, region=region, page_domain=dom, cfg=cfg
                )
                # Professional fact-check reviews (Feature A): same fetch
                # boundary as every other retrieval, cached per claim, and
                # dormant unless VERITAS_FACT_CHECK_API_KEY is configured.
                if cfg.factcheck_enabled and cfg.factcheck_api_key:
                    fact_checks = fetch_fact_checks(
                        claim.claim,
                        cfg=cfg,
                        fetch_func=retriever.fetch_func or SourceRetriever.fetch,
                    )
                # Source freshness: a stale pool never reads as "breaking".
                median_age = median_source_age(candidates, cfg=cfg)
                verification = get_final_verification_result(
                    claim,
                    candidates,
                    region=region,
                    published_date=published_date,
                    queries=queries,
                    budget=budget,
                    cfg=cfg,
                    median_source_age_hours=median_age,
                    fact_checks=fact_checks,
                )
                vcache.set(vkey, verification)
        except Exception:
            # Fail gracefully (e.g. rate limits, malformed data): keep the
            # existing heuristics and never surface an error as a verdict.
            logger.warning("cross-source verification failed; degrading gracefully", exc_info=True)
            verification = None

    # Cross-source evidence: existing evidence copy, new weighted scoring.
    region_label = region_name(region)
    selected = verification.selected if verification is not None else 0
    supports = verification.supports if verification is not None else []
    contradicts_n = verification.contradict_count if verification is not None else 0
    strength = (verification.e_final / 100.0) if verification is not None else 0.0
    # Outbound links to trusted outlets are a weak hint only (the page's own
    # footers can reference big outlets), never corroboration -- and they
    # must never offset a contradiction (spec item 5).
    link_credit = min(len(trusted_links) * 0.05, 0.30) * 0.2

    def _compact_verification() -> Dict[str, Any]:
        # Slim copy for evidence details; the full breakdown lives once in
        # ai_signals["verification"] (avoid duplicating it in the payload).
        return {
            "state": verification.state,
            "evidence_score": round(verification.e_final, 1),
            "counts": {
                "found": verification.examined,
                "selected": verification.selected,
                "supports": verification.support_count,
                "contradicts": verification.contradict_count,
                "unknown": verification.unknown_count,
            },
        }

    if verification is not None and selected >= 1:
        # Evidence score drives the suspicion delta: suspicion +=
        # (0.5 - E/100) * slope. E = 50 is neutral; below 50 (thin or
        # contradicting evidence) raises suspicion, above 50 lowers it.
        cross_weight = (0.5 - strength) * cfg.cross_weight_slope
        if supports:
            evidence.append(
                Evidence(
                    "Cross-source check",
                    f"Corroborated by {len(supports)} trusted {region_label} "
                    f"outlet(s), match strength {strength:.2f}",
                    cross_weight,
                    {
                        "matches": [s.to_dict() for s in supports[:4]],
                        "verification": _compact_verification(),
                    },
                )
            )
        elif trusted_links and contradicts_n == 0:
            # Unknown coverage only: the link hint may still nudge slightly --
            # and the nudge is applied for real: the Evidence weight below and
            # `suspicion += cross_weight` agree (README: every signed weight is
            # part of the sum). Applied ONLY inside this guard: the link hint
            # must never offset a contradiction (spec item 5).
            cross_weight -= link_credit
            evidence.append(
                Evidence(
                    "Cross-source check",
                    f"No direct coverage match, but the page references "
                    f"{len(trusted_links)} trusted outlet(s)",
                    cross_weight,
                    {"verification": _compact_verification()},
                )
            )
        else:
            # Contradicting or non-matching coverage: never dressed up as a
            # link-hint reduction; weight follows the evidence score.
            evidence.append(
                Evidence(
                    "Cross-source check",
                    f"No matching coverage found in trusted {region_label} outlets "
                    f"({selected} results examined)",
                    cross_weight,
                    {"verification": _compact_verification()},
                )
            )
    elif trusted_links:
        evidence.append(
            Evidence(
                "Cross-source check",
                f"No direct coverage match, but the page references "
                f"{len(trusted_links)} trusted outlet(s)",
                -link_credit,
            )
        )
        cross_weight = -link_credit
    else:
        # No usable reference source: never invent evidence and never label
        # the article fake for it. Very recent (breaking) stories are
        # penalized less for missing coverage (spec items 9 & 12).
        if verification is not None and verification.breaking:
            cross_weight = cfg.breaking_no_coverage_penalty
        else:
            cross_weight = cfg.no_coverage_penalty
        evidence.append(
            Evidence(
                "Cross-source check",
                f"No matching coverage found in trusted {region_label} outlets "
                f"(0 results examined)",
                cross_weight,
                _compact_verification() if verification is not None else None,
            )
        )
    suspicion += cross_weight

    ai_signals["cross_check"] = {
        # 0.0 with no selected sources: no data must not read as weak evidence
        "score": round(strength, 3) if selected >= 1 else 0.0,
        "checked": selected,
        "trusted_links_on_page": trusted_links[:5],
        "state": verification.state if verification is not None else "disabled",
        "evidence_score": (
            round(verification.e_final, 1) if verification is not None else None
        ),
        # Very recent story with thin coverage: surfaced to the extension so
        # breaking news never reads like fake news (user fix 2).
        "is_breaking": verification.breaking if verification is not None else False,
    }
    if fact_checks:
        # Structured fact-check summary (counts + validated review URLs).
        # F already absorbed the blend inside verification; this is
        # transparent bookkeeping only — never a separate suspicion term.
        ai_signals["fact_check"] = summarize_fact_checks(fact_checks)
    if verification is not None:
        # Full structured breakdown (components, per-source relationships,
        # weights, breaking-news state) for debugging and future UI use.
        ai_signals["verification"] = verification.to_dict()
        logger.debug(
            "verification: state=%s found=%d selected=%d supports=%d "
            "contradicts=%d unknown=%d M=%.1f C=%.1f F=%.1f S=%.1f "
            "E=%.1f e_final=%.1f penalty=%s breaking=%s",
            verification.state,
            verification.examined,
            verification.selected,
            verification.support_count,
            verification.contradict_count,
            verification.unknown_count,
            verification.M,
            verification.C,
            verification.F,
            verification.S,
            verification.E,
            verification.e_final,
            verification.penalty_applied,
            verification.breaking,
        )

    # 5. Final verdict ------------------------------------------------------
    suspicion = min(max(suspicion, 0.0), 1.0)
    verdict = verdict_from_suspicion(suspicion)

    # Confidence: more text + decisive corroboration => higher confidence.
    # All increments (and the cap, which equals the reachable maximum) are
    # env-tunable via app.config.
    confidence = cfg.confidence_base
    if not styl["low_signal"]:
        confidence += cfg.confidence_text_bonus
    if verification is not None and selected >= 1:
        confidence += cfg.confidence_verification_bonus
    if rep["reasons"]:
        confidence += cfg.confidence_domain_bonus
    confidence = min(confidence, cfg.confidence_cap)

    # Developer diagnostics: ONE JSON-serializable payload per analysis at
    # DEBUG level, gated on VERITAS_DEBUG=1 -- internal/dev-only, never part
    # of the response, so the zero-user-change rule still holds. Gives an
    # immediate "why" while tuning thresholds (fact_unmentioned_factor,
    # cross_weight_slope, breaking windows) against real stories.
    if cfg.debug:
        vdict = verification.to_dict() if verification is not None else None
        debug_payload: Dict[str, Any] = {
            "url": url,
            "region": region,
            "domain": dom,
            "claim": claim.claim if claim is not None else None,
            "claim_source": claim.source if claim is not None else None,
            "queries": list(queries),
            "sources_checked": selected,
            "sources": [
                {
                    "outlet": a.outlet,
                    "weight": round(a.weight, 3),
                    "similarity": round(a.similarity, 1),
                    "relationship": a.relationship,
                }
                for a in (verification.assessments if verification is not None else [])
            ],
            "evidence": {
                "M": vdict["components"]["semantic_M"] if vdict else None,
                "C": vdict["components"]["consensus_C"] if vdict else None,
                "F": vdict["components"]["facts_F"] if vdict else None,
                "S": vdict["components"]["source_quality_S"] if vdict else None,
                "E": vdict["evidence_score"] if vdict else None,
                "state": vdict["state"] if vdict else "disabled",
                "penalty": vdict["contradiction_penalty_applied"] if vdict else None,
                "breaking": vdict["breaking"] if vdict else False,
            },
            "median_source_age_hours": (
                median_age
                if median_age is not None
                else (
                    vdict["breaking_info"].get("median_source_age_hours")
                    if vdict
                    else None
                )
            ),
            "fact_check": summarize_fact_checks(fact_checks) if fact_checks else None,
            "cross_weight": round(cross_weight, 3),
            "suspicion": round(suspicion, 3),
            "confidence": round(confidence, 3),
            "verdict": verdict,
            "retrieval_diagnostics": vdict.get("diagnostics") if vdict else {},
        }
        logger.debug(
            "debug_payload: %s", json.dumps(debug_payload, ensure_ascii=False, default=str)
        )

    # Coverage context (user fix 1): "no major outlet has covered this local
    # story yet" is neutral — it must never look like "suspicious and
    # uncorroborated". Red flags here mirror the early signals: a lookalike
    # domain (unless the outlet is a recognized editorial one), heavy
    # clickbait/sensationalism, or strongly AI-like prose.
    if verification is None or selected == 0:
        trusted_domain = any(p in dom for p in KNOWN_TRUSTED_PATTERNS)
        has_red_flags = (
            (not trusted_domain and any(p in dom for p in LOOKALIKE_TLDS))
            or sens_weight >= 0.15
            or styl_weight >= 0.20
            or model_weight > 0
        )
        coverage_state = "suspicious_no_coverage" if has_red_flags else "no_local_coverage"
    elif selected < cfg.min_sources:
        coverage_state = "thin_coverage"
    else:
        coverage_state = "sufficient_coverage"

    matches = [
        SourceMatch(
            outlet=s.outlet,
            url=s.url,
            similarity=round(s.similarity / 100.0, 3),
            title=s.title,
            tier=getattr(s, "tier", 3) or 3,
            published=getattr(s, "published", None),
            is_wire_syndication=getattr(s, "is_wire_syndication", False),
            wire_agency=getattr(s, "wire_agency", None),
        )
        for s in supports[: cfg.max_sources]
    ]
    summary = build_summary(verdict, suspicion, evidence)

    return AnalysisResult(
        url=url,
        domain=dom,
        verdict=verdict,
        suspicion=suspicion,
        confidence=confidence,
        headline=headline[:280],
        summary=summary,
        evidence=evidence,
        corroborating_sources=matches,
        ai_signals=ai_signals,
        coverage_state=coverage_state,
        is_breaking=verification.breaking if verification is not None else False,
    )


def build_summary(verdict: str, suspicion: float, evidence: List[Evidence]) -> str:
    top = sorted(evidence, key=lambda e: abs(e.weight), reverse=True)[:3]
    reasons = "; ".join(e.claim for e in top) if top else "no strong signals"
    return f"Verdict: {verdict.upper()} (suspicion {suspicion:.2f}). Key factors: {reasons}."
