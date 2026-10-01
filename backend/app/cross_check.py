"""Cross-source corroboration against a pool of reference outlets.

For a given claim we:
  1. Build 2-3 optimized news search queries (Google News RSS -- no API key).
  2. Fetch results (capped per query, early-stopping once enough relevant
     results exist; repeated searches answered from a TTL cache; uncached
     queries are fetched in parallel so latency is one round trip).
  3. Resolve each result's outlet, exclude the analyzed page's own outlet,
     and rank by claim relevance (token Jaccard + difflib ratio on
     normalized strings -- wording-tolerant, never exact matching).
  4. Return the strongest relevant candidates ranked by relevance
     (retrieval early-stops once cfg.max_sources + 5 relevant outlets exist;
     the verification layer then keeps at most cfg.max_sources = 10) for the
     verification layer to classify and score.

If no outlet covers the story, that alone is weak evidence -- small local
stories exist -- so it contributes only a small weight downstream and never
labels an article fake by itself (see app.verification).

Spec: retrieveReferenceSources() lives here.

Signature marker 005/013 · designer-dash
"""

from __future__ import annotations

import difflib
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .cache import get_cache
from .config import get_config
from .security import safe_fetch
from .sources import outlets_for_region, source_weight
from .textnorm import STOPWORDS, extract_url_date, parse_rfc822
from .textnorm import strip_source_suffix as _strip_source_suffix

USER_AGENT = (
    "Veritas-FND/0.1 (educational fake-news research; contact: dev@example.org)"
)
TIMEOUT = 8

# Google News RSS reports the outlet as a display name ("The Hindu") and its
# article links are JS redirects on news.google.com that cannot be fetched
# server-side. Map known outlet names to their domains so trustedness can be
# decided from the name, and corroborate via headline overlap instead of
# fetching the redirect URL.
OUTLET_NAME_DOMAINS = {
    "the times of india": "timesofindia.indiatimes.com",
    "times of india": "timesofindia.indiatimes.com",
    "india today": "indiatoday.in",
    "the hindu": "thehindu.com",
    "hindustan times": "hindustantimes.com",
    "ndtv": "ndtv.com",
    "indian express": "indianexpress.com",
    "news18": "news18.com",
    "livemint": "livemint.com",
    "mint": "livemint.com",
    "business today": "businesstoday.in",
    "pti": "ptinews.com",
    "ani": "aninews.in",
    "reuters": "reuters.com",
    "associated press": "apnews.com",
    "ap news": "apnews.com",
    "bbc": "bbc.com",
    "bbc news": "bbc.com",
    "the guardian": "theguardian.com",
    "al jazeera": "aljazeera.com",
    "the new york times": "nytimes.com",
    "washington post": "washingtonpost.com",
    "npr": "npr.org",
    "pbs": "pbs.org",
    "financial times": "ft.com",
    "the independent": "independent.co.uk",
    "the telegraph": "telegraph.co.uk",
}


def _normalize_text(text: str) -> str:
    """Lowercase, drop thousands separators and punctuation; keep digits."""
    return re.sub(r"[^a-z0-9\s-]", " ", text.lower().replace(",", ""))


def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]{3,}", _normalize_text(text))
    return [w for w in words if w not in STOPWORDS]


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> float:
    """Blend token-set Jaccard with a character-level difflib ratio.

    Jaccard alone is brittle when outlets reorder or paraphrase a shared
    headline; the difflib ratio on normalized strings recovers those matches.
    """
    na, nb = _normalize_text(_strip_source_suffix(a)), _normalize_text(_strip_source_suffix(b))
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    jac = jaccard(ta, tb)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0
    return max(jac, 0.9 * ratio)


def extract_external_links(html: str) -> List[str]:
    """External <a href> targets, used as a corroboration hint on the page itself."""
    hrefs = re.findall(r'<a[^>]+href=["\'](https?://[^"\']+)["\']', html, flags=re.I)
    return hrefs


def links_to_trusted(html: str, region: str, page_domain: str = "") -> List[str]:
    """External links from the page to *other* trusted outlets.

    Links to the page's own registrable domain are excluded -- a news site
    linking to itself (including its own subdomains) is not corroboration.
    """
    trusted = outlets_for_region(region)
    host = (page_domain or "").lower().split("//")[-1].split("/")[0]
    labels = host.split(".") if host else []
    own_base = ".".join(labels[-2:]) if len(labels) >= 2 else ""
    out = []
    for h in extract_external_links(html):
        hl = h.lower()
        if own_base and own_base in hl:
            continue
        if any(t in hl for t in trusted):
            out.append(h)
    return out


def _registrable(host: str) -> str:
    labels = host.lower().split("//")[-1].split("/")[0].split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def _google_news_url(query: str, region: str) -> str:
    lang_country = {"in": "en-IN", "us": "en-US", "uk": "en-GB"}.get(region, "en-IN")
    gl = region.upper() if len(region) == 2 else "IN"
    ceid = f"{region}:en"
    params = {"hl": lang_country, "gl": gl, "ceid": ceid}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, **params}
    )


def _fetch(url: str, timeout: int = TIMEOUT) -> Optional[bytes]:
    """All cross-check fetches go through the SSRF-guarded security layer."""
    return safe_fetch(url, timeout=timeout)


def _parse_rss_items(rss: Optional[bytes], limit: int) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not rss:
        return items
    try:
        root = ET.fromstring(rss)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            source = (item.findtext("source") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title and link:
                items.append(
                    {"title": title, "link": link, "outlet": source, "pub_date": pub}
                )
            if len(items) >= limit:
                break
    except ET.ParseError:
        pass
    return items


def _parse_source_date(item: Dict[str, Any]) -> Optional[datetime]:
    """Publication time of a reference source: the RSS ``pubDate`` (exact)
    first, then a full date embedded in the article URL -- both parsed by
    the shared app.textnorm helpers (single source of truth with the claim
    layer). None = unknown freshness -- unknown ages are never used to judge
    (or block) recency.
    """
    pub = parse_rfc822(item.get("pub_date", ""))
    if pub is not None:
        return pub
    return extract_url_date(item.get("link", ""))


def median_source_age(
    candidates: Optional[List[Dict[str, Any]]],
    cfg=None,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Tier-weighted median age (hours) of the reference sources that state
    a publication time; None when none do (unknown freshness never blocks
    breaking-news handling downstream). A pool whose median is older than
    cfg.breaking_max_median_age_hours is recycled coverage, not a fresh event.
    """
    now = now or datetime.now(timezone.utc)
    cfg = cfg or get_config()
    aged: List[tuple] = []
    for c in candidates or []:
        age = c.get("age_hours")
        if age is None:
            continue
        aged.append((max(float(age), 0.0), source_weight(str(c.get("domain", "")), cfg)))
    if not aged:
        return None
    aged.sort(key=lambda t: t[0])
    total = sum(w for _, w in aged) or 1.0
    acc = 0.0
    for age, weight in aged:
        acc += weight
        if acc >= total / 2.0:
            return round(age, 1)
    return round(aged[-1][0], 1)


def _slug(text: str) -> str:
    """Alphanumeric slug for fuzzy outlet-vs-own-domain comparisons."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _resolve_outlet_domain(it: Dict[str, str], region: str) -> str:
    """Best-effort outlet domain for weighting/own-outlet exclusion.

    Google News RSS links are JS redirects that cannot be fetched
    server-side, so the outlet is resolved from its display name first,
    then from a direct article URL, then from the trusted-domain list.
    Returns "" when the outlet is unknown (weight 0.40 downstream).
    """
    name = (it.get("outlet") or "").strip().lower()
    if name in OUTLET_NAME_DOMAINS:
        return OUTLET_NAME_DOMAINS[name]
    link = it.get("link", "")
    if link and "news.google.com" not in link:
        host = urllib.parse.urlparse(link).netloc.lower().split(":")[0]
        if host:
            return host
    blob = (link + " " + name).lower()
    trusted = outlets_for_region(region)
    for t in trusted:
        if t in blob:
            return t
    slug = _slug(name)
    if slug:
        for t in trusted:
            if t.split(".")[0] and t.split(".")[0] in slug:
                return t
    return ""


def retrieve_reference_sources(
    claim_text: str,
    queries: List[str],
    region: str,
    page_domain: str = "",
    cfg=None,
    fetch_func: Optional[Callable[..., Optional[bytes]]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Search the reference-source pool for a claim (spec:
    retrieveReferenceSources()).

    Runs up to cfg.max_queries (2-3) optimized RSS queries with an early
    stop once enough relevant results exist, deduplicates per outlet,
    excludes the analyzed page's own outlet, ranks by relevance to the
    claim, and returns the strongest relevant candidates (uncapped here;
    the verification layer keeps at most cfg.max_sources = 10).

    ``fetch_func`` is the single network boundary (defaults to the SSRF-
    guarded fetcher); pipeline injects ``SourceRetriever.fetch`` here so
    tests patch one place and alternative backends can be swapped in.

    Each candidate also carries ``published`` / ``age_hours`` (RSS pubDate,
    else a date in the article URL; None = unknown) so callers can judge
    source freshness via median_source_age().

    Repeated searches are answered from a TTL cache, so re-checking the
    same claim costs zero extra requests.
    """
    cfg = cfg or get_config()
    fetch = fetch_func or _fetch
    now = now or datetime.now(timezone.utc)
    own_base = _registrable(page_domain) if page_domain else ""
    own_slug = _slug(page_domain)
    rss_cache = get_cache("rss", maxsize=512, ttl=cfg.rss_cache_ttl)
    candidates: Dict[str, Dict[str, Any]] = {}
    target = claim_text or ""

    # Fetch phase: every uncached query goes out in parallel (stdlib
    # threads), so a thin-coverage claim -- exactly the case that needs all
    # queries -- costs one round trip (~TIMEOUT) instead of
    # max_queries x TIMEOUT serially. Cached queries cost nothing and every
    # result is TTL-cached, so repeat checks stay at zero requests. The
    # merge below stays sequential and ordered: output is deterministic
    # regardless of which fetch finishes first. Trade-off: early-stopping
    # now discards (already-fetched) later results instead of skipping the
    # fetch -- at most cfg.max_queries RSS GETs per uncached claim.
    plan: List[Any] = []          # (query, cache_key, items | None on miss)
    uncached: List[str] = []
    for query in queries[: cfg.max_queries]:
        if not query or not _tokenize(query):
            continue
        cache_key = f"{region}:{query}"
        hit, items = rss_cache.get(cache_key)
        plan.append((query, cache_key, items if hit else None))
        if not hit:
            uncached.append(query)

    bodies: Dict[str, Optional[bytes]] = {}
    if uncached:
        with ThreadPoolExecutor(max_workers=min(len(uncached), 4)) as pool:
            futures = {
                pool.submit(fetch, _google_news_url(q, region)): q for q in uncached
            }
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    bodies[q] = fut.result()
                except Exception:
                    bodies[q] = None  # a failing worker behaves like a failed fetch

    for query, cache_key, items in plan:
        if items is None:
            items = _parse_rss_items(bodies.get(query), cfg.max_query_sources)
            # Never negative-cache: an empty result may be a transient
            # network failure, and caching it would suppress recovery on
            # the user's re-check.
            if items:
                rss_cache.set(cache_key, items)

        for it in items:
            domain = _resolve_outlet_domain(it, region)
            if own_base and domain and _registrable(domain) == own_base:
                continue  # the page's own outlet re-reporting itself is circular
            outlet = it.get("outlet", "") or domain or it.get("link", "")
            if not domain and own_slug:
                out_slug = _slug(outlet)
                if out_slug and (out_slug in own_slug or own_slug in out_slug):
                    continue
            sim = title_similarity(target, it["title"])
            # Relevance: above the floor AND shares at least one content
            # word with the claim (pure noise never enters the pool).
            shared = len(set(_tokenize(target)) & set(_tokenize(it["title"])))
            if sim < cfg.retrieval_floor or shared < 1:
                continue
            # One candidate per outlet (keeping its best match): independent
            # sources matter, the same outlet reporting twice does not.
            key = (outlet or "").strip().lower() or (it.get("link") or it["title"]).lower()
            prev = candidates.get(key)
            if prev is None or prev["similarity"] < sim:
                published = _parse_source_date(it)
                age_hours = (
                    round(max((now - published).total_seconds() / 3600.0, 0.0), 1)
                    if published
                    else None
                )
                candidates[key] = {
                    "outlet": outlet,
                    "domain": domain,
                    "url": it.get("link", ""),
                    "title": it["title"][:200],
                    "similarity": round(sim, 3),
                    "published": published.isoformat() if published else None,
                    "age_hours": age_hours,
                }

        # Early stop: enough relevant results to pick the strongest 5-10.
        if len(candidates) >= cfg.max_sources + 5:
            break

    ranked = sorted(candidates.values(), key=lambda c: c["similarity"], reverse=True)
    return ranked
