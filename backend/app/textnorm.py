"""Dependency-free text/date primitives shared by the claim and retrieval layers.

This module exists to break an import cycle: ``claim_analysis`` used to import
``STOPWORDS`` and ``_strip_source_suffix`` from ``cross_check``, which forbade
``cross_check`` from ever reusing ``claim_analysis``'s URL-date parsing (a
top-level import would have been circular). Both layers now import from here
instead; neither imports the other.

Everything is stdlib-only. Signature marker: verification rework
(claim-based evidence scoring).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# Stopwords for search-token / claim normalization (kept identical to the
# historical cross_check list so query generation and similarity scoring
# do not shift).
STOPWORDS = set(
    """a about above after again against all am an and any are aren't as at be
because been before being below between both but by can't cannot could couldn't
did didn't do does doesn't doing don't down during each few for from further had
hadn't has hasn't have haven't having he her here hers herself him himself his
how i if in into is isn't it its itself let's me more most mustn't my myself no
nor not of off on once only or other ought our ours ourselves out over own same
shan't she should shouldn't so some such than that the their theirs them
themselves then there these they this those through to too under until up very
was wasn't we were weren't what when where which while who whom why with won't
would wouldn't you your yours yourself yourselves""".split()
)


def strip_source_suffix(title: str) -> str:
    """Drop tailing outlet names from titles (e.g., ' - Outlet' or ' | Outlet').

    Outlet-name membership is intentionally *not* checked against a known-outlet
    list here: every entry in cross_check.OUTLET_NAME_DOMAINS is shorter than
    20 characters, so the length rule alone reproduces the historical behaviour
    exactly while keeping this module dependency-free.
    """
    for sep in [" - ", " | "]:
        parts = title.rsplit(sep, 1)
        if len(parts) == 2 and len(parts[0]) > 20:
            # If the suffix is just short (almost certainly an outlet tail), strip it
            if len(parts[1]) < 20:
                return parts[0]
    return title


# Historical private name imported by claim_analysis/tests.
_strip_source_suffix = strip_source_suffix


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_iso_date(raw: str) -> Optional[datetime]:
    """'2026-10-01' / '2026/10/01' / ISO datetime -> aware datetime (UTC if naive)."""
    raw = raw.strip().replace("Z", "+00:00")
    if "/" in raw and re.match(r"^\d{4}/\d{2}/\d{2}", raw):
        raw = raw.replace("/", "-")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_rfc822(raw: str) -> Optional[datetime]:
    """RSS ``pubDate`` ('Wed, 01 Oct 2026 11:00:00 GMT') -> aware datetime.

    Returns None for missing/garbage values -- unknown freshness is never
    guessed.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


_ISO_IN_URL_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_DATE_SEGMENT_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$|\.)")


def extract_url_date(url: str) -> Optional[datetime]:
    """Full date embedded in a URL path: ISO anywhere, or ``/2026/10/01/``.

    The extension sends plain article text without meta tags, so the URL path
    is often the only available publication date. None when the URL states no
    date (callers must treat that as *unknown*, never as recent/old).
    """
    if not url:
        return None
    m = _ISO_IN_URL_RE.search(url)
    if m:
        return parse_iso_date(m.group(1))
    m = _DATE_SEGMENT_RE.search(url)
    if m:
        return parse_iso_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return None
