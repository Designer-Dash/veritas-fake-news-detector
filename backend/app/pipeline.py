"""Analysis pipeline: combine domain heuristics, stylometry, AI signals and
claim-based cross-source verification into one explainable verdict.

Signature marker 008/013 · designer-dash
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .ai_signals import scan_bytes, scan_html_for_media_flags, score_media_signals
from .cache import get_cache
from .claim_analysis import extract_claim, extract_published_date, generate_search_queries
from .config import get_config
from .cross_check import links_to_trusted, retrieve_reference_sources
from .domain import AnalysisResult, Evidence, SourceMatch, verdict_from_suspicion
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


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.split("@")[-1].split(":")[0] if netloc else ""
    except Exception:
        return ""


def domain_reputation(domain: str) -> Dict[str, Any]:
    d = domain or ""
    weight = 0.0
    reasons: List[str] = []
    if any(p in d for p in KNOWN_TRUSTED_PATTERNS):
        weight -= 0.25
        reasons.append("Well-known editorial outlet")
    if d.endswith(LOOKALIKE_TLDS):
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


def analyze(
    url: str,
    region: str = "in",
    html: Optional[str] = None,
    headline_override: Optional[str] = None,
    include_cross_check: bool = True,
) -> AnalysisResult:
    """Full analysis of a news page. `html` may be supplied by the extension
    (content script DOM snapshot) to avoid a refetch; otherwise we fetch it."""

    # Fail closed on malformed/unsupported page URLs (defense in depth -- the
    # API layer validates too; this protects direct callers of analyze()).
    # Shape-level validation: scheme, host/port/userinfo, and literal-IP
    # checks (metadata/loopback/RFC1918 rejected). DNS resolution is skipped
    # here; the actual fetch re-validates fully inside safe_fetch().
    if not isinstance(url, str) or len(url) > 2048:
        raise SecurityError("analyze: invalid url")
    validate_url(url, allow_http=True, resolve=False, context="analyze")

    dom = domain_of(url)
    if html is None:
        raw = fetch_page(url)
        html = raw.decode("utf-8", errors="ignore") if raw else ""
    text = extract_article_text(html)
    headline = headline_override or extract_headline(html) or url

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
    cfg = get_config()
    verification: Optional[VerificationResult] = None

    if include_cross_check and headline:
        budget = CallBudget(cfg.gemini_max_calls)
        try:
            # Extract the claim once (cached) and reuse it for queries,
            # retrieval and every pairwise comparison.
            claim = extract_claim(headline, text, budget=budget)
            queries = generate_search_queries(claim, headline)
            # Avoid re-verifying the same claim/page while the result is fresh.
            vcache = get_cache("verification", maxsize=64, ttl=cfg.verification_cache_ttl)
            vkey = hashlib.sha256(
                "\x00".join((claim.claim, region, dom)).encode("utf-8", "ignore")
            ).hexdigest()
            hit, cached = vcache.get(vkey)
            if hit:
                verification = cached
            else:
                candidates = retrieve_reference_sources(
                    claim.claim, queries, region=region, page_domain=dom, cfg=cfg
                )
                verification = get_final_verification_result(
                    claim,
                    candidates,
                    region=region,
                    published_date=extract_published_date(html, url=url),
                    queries=queries,
                    budget=budget,
                    cfg=cfg,
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
            # Unknown coverage only: the link hint may still nudge slightly.
            evidence.append(
                Evidence(
                    "Cross-source check",
                    f"No direct coverage match, but the page references "
                    f"{len(trusted_links)} trusted outlet(s)",
                    cross_weight - link_credit,
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
    }
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
    confidence = 0.55
    if not styl["low_signal"]:
        confidence += 0.15
    if verification is not None and selected >= 1:
        confidence += 0.15
    if rep["reasons"]:
        confidence += 0.05
    confidence = min(confidence, 0.95)

    matches = [
        SourceMatch(
            outlet=s.outlet,
            url=s.url,
            similarity=round(s.similarity / 100.0, 3),
            title=s.title,
        )
        for s in supports[:4]
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
    )


def build_summary(verdict: str, suspicion: float, evidence: List[Evidence]) -> str:
    top = sorted(evidence, key=lambda e: abs(e.weight), reverse=True)[:3]
    reasons = "; ".join(e.claim for e in top) if top else "no strong signals"
    return f"Verdict: {verdict.upper()} (suspicion {suspicion:.2f}). Key factors: {reasons}."
