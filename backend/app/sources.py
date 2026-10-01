"""Registry of trusted news outlets, keyed by region.

The extension user picks a region (or the backend infers it from the TLD);
analysis then cross-checks the story against the top outlets for that region.
Extend the lists as you localize for more countries.
"""

# Signature marker 002/013 · designer-dash

from __future__ import annotations

from typing import Dict, List

TRUSTED_SOURCES: Dict[str, Dict[str, List[str]]] = {
    "in": {
        "name": "India",
        "outlets": [
            "timesofindia.indiatimes.com",
            "indiatoday.in",
            "thehindu.com",
            "hindustantimes.com",
            "ndtv.com",
            "indianexpress.com",
            "news18.com",
            "livemint.com",
            "businesstoday.in",
            "reuters.com",
            "apnews.com",
            "bbc.com",
        ],
    },
    "us": {
        "name": "United States",
        "outlets": [
            "reuters.com",
            "apnews.com",
            "nytimes.com",
            "washingtonpost.com",
            "wsj.com",
            "npr.org",
            "bbc.com",
            "pbs.org",
        ],
    },
    "uk": {
        "name": "United Kingdom",
        "outlets": [
            "bbc.com",
            "reuters.com",
            "theguardian.com",
            "ft.com",
            "independent.co.uk",
            "telegraph.co.uk",
            "standard.co.uk",
        ],
    },
    "global": {
        "name": "Global",
        "outlets": [
            "reuters.com",
            "apnews.com",
            "bbc.com",
            "aljazeera.com",
            "theguardian.com",
            "nytimes.com",
        ],
    },
}

DEFAULT_REGION = "in"

# ---------------------------------------------------------------------------
# Source weighting tiers (configurable via app.config VerificationConfig)
#
# The weight a source contributes to the evidence calculation -- it is one
# input to the score, never a standalone true/false verdict:
#   tier 1 = trusted/reference source  (VERITAS_TIER1_WEIGHT, default 1.00)
#   tier 2 = established source        (VERITAS_TIER2_WEIGHT, default 0.85)
#   tier 3 = lower-confidence source   (VERITAS_TIER3_WEIGHT, default 0.65)
#   unknown/unlisted source            (VERITAS_UNKNOWN_WEIGHT, default 0.40)
# Single source of truth: scoring code must not hard-code weights anywhere.
# ---------------------------------------------------------------------------

TIER1_DOMAINS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bbc.com",
        "theguardian.com",
        "nytimes.com",
        "washingtonpost.com",
        "ptinews.com",
        "thehindu.com",
    }
)

TIER2_DOMAINS = frozenset(
    {
        "timesofindia.indiatimes.com",
        "indiatoday.in",
        "hindustantimes.com",
        "ndtv.com",
        "indianexpress.com",
        "news18.com",
        "livemint.com",
        "businesstoday.in",
        "aninews.in",
        "wsj.com",
        "npr.org",
        "pbs.org",
        "ft.com",
        "independent.co.uk",
        "telegraph.co.uk",
        "aljazeera.com",
        "standard.co.uk",
    }
)

TIER3_DOMAINS = frozenset(
    {
        "dailymail.co.uk",
        "newyorkpost.com",
        "ibtimes.co.uk",
    }
)


def source_tier(domain: str) -> int:
    """Trust tier (1/2/3) for a domain; 0 = unknown. Matches the domain
    itself or a subdomain of a listed domain (never a lookalike suffix)."""
    d = (domain or "").lower().strip()
    if not d:
        return 0
    for tier, domains in ((1, TIER1_DOMAINS), (2, TIER2_DOMAINS), (3, TIER3_DOMAINS)):
        if any(d == t or d.endswith("." + t) for t in domains):
            return tier
    return 0


def source_weight(domain: str, cfg=None) -> float:
    """Configurable weight (0-1) a source contributes to evidence scoring."""
    if cfg is None:
        from .config import get_config

        cfg = get_config()
    tier = source_tier(domain)
    if tier == 1:
        return cfg.tier_weight_1
    if tier == 2:
        return cfg.tier_weight_2
    if tier == 3:
        return cfg.tier_weight_3
    return cfg.unknown_weight


def outlets_for_region(region: str) -> List[str]:
    """Return trusted outlet domains for a region, falling back to India/global."""
    entry = TRUSTED_SOURCES.get(region.lower())
    if entry:
        return list(entry["outlets"])
    return TRUSTED_SOURCES[DEFAULT_REGION]["outlets"]


def region_name(region: str) -> str:
    entry = TRUSTED_SOURCES.get(region.lower())
    return entry["name"] if entry else TRUSTED_SOURCES[DEFAULT_REGION]["name"]
