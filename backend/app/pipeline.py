"""Analysis pipeline: combine domain heuristics, stylometry, AI signals and
cross-source corroboration into one explainable verdict.

Signature marker 008/013 · designer-dash
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .ai_signals import scan_bytes, scan_html_for_media_flags, score_media_signals
from .cross_check import links_to_trusted, search_trusted_coverage
from .domain import AnalysisResult, Evidence, SourceMatch, verdict_from_suspicion
from .security import SecurityError, fetch_media_head, safe_fetch, validate_url
from .sources import outlets_for_region
from .text_analysis import (
    CLICKBAIT_PATTERNS,
    ai_stylometry,
    caps_exclaims,
    extract_article_text,
    extract_headline,
    extract_text_from_html,
    phrase_hits,
)

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

    # 4. Cross-source corroboration ----------------------------------------
    cross: Dict[str, Any] = {"score": 0.0, "matches": [], "region": region}
    if include_cross_check and headline:
        try:
            cross = search_trusted_coverage(headline, region, page_domain=dom)
        except Exception:
            cross = {"score": 0.0, "matches": [], "region": region, "error": "cross-check failed"}

    trusted_links = links_to_trusted(html, region, page_domain=dom)
    matches = cross.get("matches", [])
    corroboration = cross.get("score", 0.0)
    # Outbound links to trusted outlets are a weak hint only (the page's own
    # network/footers can reference big outlets), never full corroboration.
    link_hint = min(len(trusted_links) * 0.05, 0.30)
    corroboration = min(max(corroboration, link_hint), 1.0)

    if matches:
        suspicion -= corroboration * 0.4
        evidence.append(
            Evidence(
                "Cross-source check",
                f"Corroborated by {len(matches)} trusted {cross.get('region', region)} "
                f"outlet(s), match strength {corroboration:.2f}",
                -corroboration * 0.4,
                {"matches": matches},
            )
        )
    elif trusted_links:
        suspicion -= corroboration * 0.2
        evidence.append(
            Evidence(
                "Cross-source check",
                f"No direct coverage match, but the page references "
                f"{len(trusted_links)} trusted outlet(s)",
                -corroboration * 0.2,
            )
        )
    else:
        suspicion += 0.15
        evidence.append(
            Evidence(
                "Cross-source check",
                f"No matching coverage found in trusted {cross.get('region', region)} outlets "
                f"({cross.get('checked', 0)} results examined)",
                0.15,
            )
        )
    ai_signals["cross_check"] = {
        "score": cross.get("score"),
        "checked": cross.get("checked"),
        "trusted_links_on_page": trusted_links[:5],
    }

    # 5. Final verdict ------------------------------------------------------
    suspicion = min(max(suspicion, 0.0), 1.0)
    verdict = verdict_from_suspicion(suspicion)

    # Confidence: more text + decisive corroboration => higher confidence.
    confidence = 0.55
    if not styl["low_signal"]:
        confidence += 0.15
    if cross.get("matches"):
        confidence += 0.15
    if rep["reasons"]:
        confidence += 0.05
    confidence = min(confidence, 0.95)

    matches = [SourceMatch(**m) for m in cross.get("matches", [])[:4]]
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
