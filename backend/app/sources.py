"""Registry of trusted news outlets, keyed by region.

The extension user picks a region (or the backend infers it from the TLD);
analysis then cross-checks the story against the top outlets for that region.
Extend the lists as you localize for more countries.
"""

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


def outlets_for_region(region: str) -> List[str]:
    """Return trusted outlet domains for a region, falling back to India/global."""
    entry = TRUSTED_SOURCES.get(region.lower())
    if entry:
        return list(entry["outlets"])
    return TRUSTED_SOURCES[DEFAULT_REGION]["outlets"]


def region_name(region: str) -> str:
    entry = TRUSTED_SOURCES.get(region.lower())
    return entry["name"] if entry else TRUSTED_SOURCES[DEFAULT_REGION]["name"]
