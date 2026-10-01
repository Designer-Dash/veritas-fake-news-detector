"""Claim extraction, normalization and search-query generation.

One Gemini call (structured JSON, cached, budgeted) extracts the underlying
claim of an article; a deterministic fallback keeps the whole pipeline
functional offline and in tests. Downstream code only ever compares *claims*,
never raw headlines, so wording variations of the same event are recognized
as the same underlying claim.

Spec function map:
    extractClaim()        -> extract_claim()
    normalizeClaim()      -> normalize_claim()
    generateSearchQueries() -> generate_search_queries()

Signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .cache import get_cache
from .config import get_config
from .gemini_client import CallBudget, gemini_json
from .textnorm import STOPWORDS, extract_url_date
from .textnorm import parse_iso_date as _parse_iso_date
from .textnorm import strip_source_suffix as _strip_source_suffix

logger = logging.getLogger("veritas.claim")

# --------------------------------------------------------------------------- 
# Patterns for the deterministic fallback extractor
# ---------------------------------------------------------------------------

RE_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RE_TEXT_DATE = re.compile(
    r"\b(\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?),?\s+20\d{2}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2},?\s+20\d{2})\b",
    re.IGNORECASE,
)
# A number with an optional unit/measure word attached (kept together so
# "3,000 crore" and "12 killed" compare as one factual value).
RE_NUMBER_FACT = re.compile(
    r"\b(\d[\d,\.]*\d|\d+)\s*"
    r"(?:%|percent|crore|lakh|billion|million|trillion|kg|kilos?|km|ml|"
    r"seats|killed|died|dead|injured|hurt)?\b",
    re.IGNORECASE,
)
RE_MULTI_CAP = re.compile(r"\b([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+)+)\b")
RE_LOCATION = re.compile(
    r"\b(?:in|at|from|near)\s+((?:[A-Z][a-z']+\s*){1,3})"
)
_URL_NOISE = {
    "https", "http", "www", "com", "org", "net", "html", "htm", "php",
    "aspx", "index", "page", "articles", "news",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


@dataclass
class Claim:
    """Structured representation of a article's underlying claim."""

    claim: str                              # canonical restatement of the claim
    subject: str = ""
    event: str = ""
    location: str = ""
    date: str = ""                          # ISO date when available
    numbers: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    important_facts: List[Dict[str, Any]] = field(default_factory=list)
    search_queries: List[str] = field(default_factory=list)
    source: str = "deterministic"           # "gemini" | "deterministic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "subject": self.subject,
            "event": self.event,
            "location": self.location,
            "date": self.date,
            "numbers": list(self.numbers),
            "entities": list(self.entities),
            "important_facts": [dict(f) for f in self.important_facts],
            "search_queries": list(self.search_queries),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _stem(token: str) -> str:
    """Very light, symmetric stemmer: enough to align plural/tense variants
    ("students"/"student", "launches"/"launch") without a dependency."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 5 and token.endswith("ed"):
        return token[:-2]
    return token


def normalize_claim(text: str) -> str:
    """Canonical, wording-neutral form of a claim/headline:

    lowercased, source suffix stripped, punctuation removed, stopwords
    dropped and tokens lightly stemmed. Two different wordings of the same
    event end up with heavily overlapping token sets.
    """
    t = _strip_source_suffix(text or "").lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9\s-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    tokens = [
        _stem(w) for w in t.split()
        if w not in STOPWORDS and len(w) > 1
    ]
    return " ".join(tokens)


def _search_tokens(text: str) -> List[str]:
    """Tokens suitable for a news search query: order preserved, stopwords
    and URL noise dropped, digits/units kept."""
    t = _strip_source_suffix(text or "").lower()
    t = re.sub(r"[^a-z0-9\s-]", " ", t)
    out: List[str] = []
    for w in t.split():
        if w in STOPWORDS or w in _URL_NOISE:
            continue
        if len(w) < 3 and not w.isdigit():
            continue
        if w not in out:
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# Deterministic fallback extraction
# ---------------------------------------------------------------------------

def _headline_entities(headline: str) -> List[str]:
    entities: List[str] = []
    for m in RE_MULTI_CAP.finditer(headline):
        ent = m.group(1).strip()
        if ent.lower() not in STOPWORDS and len(ent) > 2:
            if ent not in entities:
                entities.append(ent)
    # Single capitalized words (people, places) — skip sentence-initial word.
    words = headline.split()
    for i, w in enumerate(words):
        clean = re.sub(r"[^A-Za-z']", "", w)
        if (
            len(clean) > 2
            and clean[0].isupper()
            and clean.lower() not in STOPWORDS
            and i > 0
            and clean not in entities
            and not any(clean in e for e in entities)
        ):
            # Only keep it if it looks like a proper noun used mid-sentence.
            if i > 0 and (words[i - 1][-1:] in ",;:"):
                entities.append(clean)
    return entities[:8]


def _find_date(headline: str, text: str) -> str:
    blob = f"{headline} {text[:600]}"
    m = RE_ISO_DATE.search(blob)
    if m:
        return m.group(1)
    m = RE_TEXT_DATE.search(blob)
    if m:
        parsed = _parse_human_date(m.group(1))
        if parsed:
            return parsed
    return ""


def _find_location(headline: str, text: str) -> str:
    for blob in (headline, text[:600]):
        m = RE_LOCATION.search(blob)
        if m:
            loc = m.group(1).strip().rstrip(".,;:")
            if len(loc) > 2:
                return loc
    return ""


def _deterministic_claim(headline: str, article_text: str = "") -> Claim:
    base = _strip_source_suffix(headline or "").strip()
    entities = _headline_entities(base)
    numbers: List[str] = []
    facts: List[Dict[str, Any]] = []

    for m in RE_NUMBER_FACT.finditer(f"{base} {article_text[:600]}"):
        value = m.group(0).strip()
        digits = re.sub(r"\D", "", value)
        if len(digits) < 1 or not digits:
            continue
        if value in numbers:
            continue
        numbers.append(value)
        facts.append({"type": "number", "value": value, "critical": True})
        if len(numbers) >= 4:
            break

    date = _find_date(base, article_text)
    if date:
        facts.append({"type": "date", "value": date, "critical": True})

    location = _find_location(base, article_text)
    if location:
        facts.append({"type": "location", "value": location, "critical": True})

    for ent in entities[:3]:
        facts.append({"type": "entity", "value": ent, "critical": True})

    return Claim(
        claim=base,
        subject=entities[0] if entities else "",
        event="",
        location=location,
        date=date,
        numbers=numbers[:8],
        entities=entities,
        important_facts=facts[:8],
        search_queries=[],
        source="deterministic",
    )


# ---------------------------------------------------------------------------
# Gemini-assisted extraction (structured analysis only)
# ---------------------------------------------------------------------------

_GEMINI_SYSTEM = (
    "You are a structured news-claim analysis engine. Extract the underlying "
    "factual claim of the article. Do NOT judge whether it is true or false, "
    "do NOT assign confidence, do NOT answer \"is this fake news\" — only "
    "analyze structure. Return ONLY a JSON object with exactly these keys:\n"
    "{\n"
    '  "claim": string — one canonical, wording-neutral restatement of the '
    "main claim (active voice, includes subject + event + key specifics),\n"
    '  "subject": string — main entity the claim is about,\n'
    '  "event": string — the core action/event (e.g. "launch", "seizure"),\n'
    '  "location": string — place of the event, or "",\n'
    '  "date": string — ISO 8601 date (YYYY-MM-DD) if the text states one, '
    'else "",\n'
    '  "numbers": [string] — important numbers with units, as written,\n'
    '  "entities": [string] — key people/organizations/places involved,\n'
    '  "important_facts": [{"type": "number|date|entity|location|event", '
    '"value": string, "critical": bool}] — facts whose mismatch would mean '
    "a different event (dates, numbers, people, locations),\n"
    '  "search_queries": [string] — 2-3 short news-search queries that would '
    "find independent coverage of this claim\n"
    "}\n"
)


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:12]
    return []


def _as_facts(value: Any) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            val = str(item.get("value", "")).strip()
            if not val:
                continue
            ftype = str(item.get("type", "entity")).strip().lower()
            if ftype not in ("number", "date", "entity", "location", "event"):
                ftype = "entity"
            facts.append(
                {"type": ftype, "value": val[:120], "critical": bool(item.get("critical", True))}
            )
    return facts[:12]


def extract_claim(
    headline: str,
    article_text: str = "",
    budget: Optional[CallBudget] = None,
) -> Claim:
    """Extract the underlying claim of an article (spec: extractClaim()).

    One Gemini call per unique headline (cached); deterministic fallback on
    any failure. The result is cached so repeated checks of the same
    article/claim never re-run extraction.
    """
    cfg = get_config()
    cache = get_cache("claim", maxsize=128, ttl=cfg.claim_cache_ttl)
    key = hashlib.sha256(
        ((headline or "").strip().lower() + "|" + (article_text or "")[:400]).encode(
            "utf-8", "ignore"
        )
    ).hexdigest()
    hit, cached = cache.get(key)
    if hit:
        return cached

    fallback = _deterministic_claim(headline or "", article_text or "")
    claim = fallback

    prompt = (
        _GEMINI_SYSTEM
        + f"\nHeadline: {(headline or '')[:512]}\n"
        + f"Article text (truncated): {(article_text or '')[:1500]}\n"
    )
    data = gemini_json(prompt, cache_key="claim:" + key, budget=budget)
    if isinstance(data, dict):
        gemini_claim = str(data.get("claim", "")).strip()
        if gemini_claim:
            # Overlay the structured analysis on the deterministic fallback so
            # facts/queries are never emptier than the offline path.
            claim = Claim(
                claim=gemini_claim[:400],
                subject=str(data.get("subject", "")).strip()[:120] or fallback.subject,
                event=str(data.get("event", "")).strip()[:80],
                location=str(data.get("location", "")).strip()[:120] or fallback.location,
                date=str(data.get("date", "")).strip()[:40] or fallback.date,
                numbers=_as_list(data.get("numbers")) or fallback.numbers,
                entities=_as_list(data.get("entities")) or fallback.entities,
                important_facts=_as_facts(data.get("important_facts")) or fallback.important_facts,
                search_queries=_as_list(data.get("search_queries")),
                source="gemini",
            )
    elif data is not None:
        logger.debug("gemini claim response had unexpected shape; using fallback")

    cache.set(key, claim)
    logger.debug("extracted claim (%s): %s", claim.source, claim.claim)
    return claim


# ---------------------------------------------------------------------------
# Search-query generation
# ---------------------------------------------------------------------------

def generate_search_queries(claim: Claim, headline: str = "") -> List[str]:
    """2-3 optimized, deduplicated search queries for the reference pool
    (spec: generateSearchQueries()). Gemini-provided queries win when
    available; otherwise deterministic queries from the claim are used."""
    cfg = get_config()
    out: List[str] = []
    seen = set()

    def _add(q: str) -> None:
        q = " ".join(str(q).split())[:160]
        key = q.lower()
        if q and key not in seen and len(out) < cfg.max_queries:
            seen.add(key)
            out.append(q)

    for q in claim.search_queries:
        _add(q)

    if len(out) < 2:
        base_tokens = _search_tokens(claim.claim or headline)
        if base_tokens:
            _add(" ".join(base_tokens[:10]))
            # Distinctive (longest) tokens catch paraphrased coverage.
            distinctive = sorted(set(base_tokens), key=lambda w: (-len(w), w))
            _add(" ".join(distinctive[:6]))
        head_tokens = _search_tokens(headline or claim.claim)
        if head_tokens:
            _add(" ".join(head_tokens[:6]))

    logger.debug("search queries: %s", out)
    return out


# ---------------------------------------------------------------------------
# Publication date (for breaking-news detection)
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    r'article:published_time["\']?\s+content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'name=["\'](?:pubdate|publishdate|publish-date)["\'][^>]*content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*name=["\'](?:pubdate|publishdate)',
    r'<time[^>]*datetime=["\']([^"\']+)',
    r'\b(20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\b',
]


def _parse_human_date(raw: str) -> Optional[str]:
    """'30 September 2026' / 'September 30, 2026' -> '2026-09-30'."""
    raw = raw.strip().lower().replace(",", " ")
    parts = [p for p in raw.split() if p]
    try:
        if parts and parts[0].isdigit():
            day, month, year = int(parts[0]), parts[1], int(parts[-1])
        else:
            month, day, year = parts[0], int(parts[1]), int(parts[-1])
        month_n = _MONTHS[month[:5] if month[:5] in _MONTHS else month[:3]]
        return datetime(year, month_n, day).date().isoformat()
    except (ValueError, KeyError, IndexError):
        return None


def extract_published_date(content: str, url: str = "") -> Optional[datetime]:
    """Publication datetime from meta tags / JSON-LD / <time> / dates in the
    copy, falling back to a full date embedded in the URL path
    (``/2026/10/01/slug.html`` -- common on news sites, and the extension
    sends plain text without meta tags). Returns None when nothing states a
    date; unknown recency is never treated as breaking news."""
    if not content and not url:
        return None
    content = content or ""
    for pattern in _DATE_PATTERNS:
        m = re.search(pattern, content, flags=re.IGNORECASE)
        if m:
            dt = _parse_iso_date(m.group(1))
            if dt:
                return dt
    # Human-readable date in the copy (or a full YYYY/MM/DD path segment).
    m = RE_TEXT_DATE.search(content[:4000])
    if m:
        iso = _parse_human_date(m.group(1))
        if iso:
            return _parse_iso_date(iso)
    m = re.search(r"/(20\d{2}/\d{2}/\d{2})/", content[:2048])
    if m:
        return _parse_iso_date(m.group(1))
    # URL fallback: ISO date anywhere in the URL, or a full date segment
    # (shared helper -- single source of truth with app.cross_check).
    if url:
        return extract_url_date(url)
    return None
