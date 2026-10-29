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
from .sources import outlets_for_region, publisher_group, source_tier, source_weight
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
    "the indian express": "indianexpress.com",
    "indian express": "indianexpress.com",
    # BUG FIX: "The New Indian Express" is newindianexpress.com — a completely
    # different newspaper from "The Indian Express" (indianexpress.com).
    # Previously mapped incorrectly to indianexpress.com, which caused:
    #   (a) The New Indian Express to be mistakenly flagged as the same outlet
    #       as indianexpress.com and therefore self-excluded when thehindu.com
    #       articles were analyzed, and
    #   (b) Its source weight to be wrongly elevated to Tier-2 (0.85) instead
    #       of its correct Unknown (0.40) weight.
    "the new indian express": "newindianexpress.com",
    "new indian express": "newindianexpress.com",
    "news18": "news18.com",
    "livemint": "livemint.com",
    "mint": "livemint.com",
    "business today": "businesstoday.in",
    "pti": "ptinews.com",
    "ani": "aninews.in",
    "ani news": "aninews.in",
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
    # Major Indian English outlets frequently appearing in Google News RSS
    # but missing from the original map (fell through to unknown weight 0.40).
    "the economic times": "economictimes.indiatimes.com",
    "economic times": "economictimes.indiatimes.com",
    "the print": "theprint.in",
    "theprint": "theprint.in",
    "the wire": "thewire.in",
    "scroll": "scroll.in",
    "scroll.in": "scroll.in",
    "firstpost": "firstpost.com",
    "the quint": "thequint.com",
    "quint": "thequint.com",
    "deccan herald": "deccanherald.com",
    "business standard": "business-standard.com",
    "zee news": "zeenews.india.com",
    "india tv": "indiatvnews.com",
    "wion": "wionews.com",
    "moneycontrol": "moneycontrol.com",
    "moneycontrol.com": "moneycontrol.com",
    "the week": "theweek.in",
    "theweek": "theweek.in",
    "theweek.in": "theweek.in",
    "aaj tak": "aajtak.in",
    "tv9": "tv9bharatvarsh.com",
    "news9": "news9live.com",
    "the statesman": "thestatesman.com",
    "telegraph india": "telegraphindia.com",
    "the telegraph india": "telegraphindia.com",
    "outlook": "outlookindia.com",
    "outlook india": "outlookindia.com",
    "cnbctv18": "cnbctv18.com",
    "cnbc tv18": "cnbctv18.com",
    "news laundry": "newslaundry.com",
    "the diplomat": "thediplomat.com",
    # Regional Indian outlets (user fix 5): Google News RSS reports these as
    # display names; without a mapping they fall through to weight 0.40.
    "dainik bhaskar": "dainikbhaskar.com",
    "bhaskar": "bhaskar.com",
    "divya bhaskar": "divyabhaskar.com",
    "amar ujala": "amarujala.com",
    "dainik jagran": "jagran.com",
    "jagran": "jagran.com",
    "rajasthan patrika": "patrika.com",
    "patrika": "patrika.com",
    "live hindustan": "livehindustan.com",
    "navbharat times": "navbharattimes.indiatimes.com",
    "punjab kesari": "punjabkesari.in",
    "jansatta": "jansatta.com",
    "eenadu": "eenadu.net",
    "sakshi": "sakshi.com",
    "andhra jyothy": "andhrajyothy.com",
    "dinamalar": "dinamalar.com",
    "dinamani": "dinamani.com",
    "daily thanthi": "dailythanthi.com",
    "vikatan": "vikatan.com",
    "mathrubhumi": "mathrubhumi.com",
    "malayala manorama": "manoramaonline.com",
    "manorama": "manoramaonline.com",
    "loksatta": "loksatta.com",
    "maharashtra times": "maharashtratimes.com",
    "anandabazar patrika": "anandabazar.com",
    "bartaman": "bartamanpatrika.com",
    "ei samay": "ei-samay.com",
    "prajavani": "prajavani.net",
    "udayavani": "udayavani.com",
    "kannada prabha": "kannadaprabha.com",
    "gujarat samachar": "gujaratsamachar.com",
    "sambad": "sambad.net",
    "dharitri": "dharitri.com",
    "the tribune": "tribuneindia.com",
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


def _outlet_search_domains(region: str, count: int) -> List[str]:
    """Choose distinct publisher groups for the compulsory outlet checks.

    A site: query is an *attempt* to find reporting from that publisher. It
    cannot guarantee coverage, so lack of a result remains insufficient
    evidence rather than evidence against the claim.
    """
    selected: List[str] = []
    groups = set()
    for domain in outlets_for_region(region):
        group = publisher_group(domain) or domain
        if group in groups:
            continue
        groups.add(group)
        selected.append(domain)
        if len(selected) >= count:
            break
    return selected


def _outlet_search_query(claim_text: str, domain: str) -> str:
    """Google News RSS query restricted to one requested publisher.
    Uses entity-action keyword extraction (no quotes) to ensure high Google News hit rate.
    """
    try:
        from .query_builder import _outlet_search_query as _qb_outlet
        return _qb_outlet(claim_text, domain)
    except Exception:
        compact = " ".join((claim_text or "").split())[:300]
        return f"{compact} site:{domain}"


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
            source_elem = item.find("source")
            source = (item.findtext("source") or "").strip()
            source_url = (
                source_elem.attrib.get("url", "").strip()
                if source_elem is not None
                else ""
            )
            pub = (item.findtext("pubDate") or "").strip()
            if title and link:
                items.append(
                    {
                        "title": title,
                        "link": link,
                        "outlet": source,
                        "source_url": source_url,
                        "pub_date": pub,
                    }
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


def _title_fingerprint(title: str) -> str:
    """Stable title fingerprint for obvious wire/repost duplicates.

    This is intentionally narrow: it catches exact or near-exact syndicated
    headlines but does not collapse independently worded reporting about the
    same event into one source.
    """
    return " ".join(sorted(set(_tokenize(_strip_source_suffix(title)))))


def _likely_syndicated(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """Whether two candidates look like copies rather than independent work."""
    lg = left.get("independence_group") or left.get("outlet")
    rg = right.get("independence_group") or right.get("outlet")
    if lg and rg and lg != rg:
        return False
    lf, rf = _title_fingerprint(left.get("title", "")), _title_fingerprint(right.get("title", ""))
    if not lf or not rf:
        return False
    if lf == rf:
        return True
    left_tokens, right_tokens = set(lf.split()), set(rf.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    ratio = difflib.SequenceMatcher(None, lf, rf).ratio()
    return overlap >= 0.82 and ratio >= 0.90


def _clean_host(host: str) -> str:
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    elif h.startswith("m."):
        h = h[2:]
    return h


def _resolve_outlet_domain(it: Dict[str, str], region: str) -> str:
    """Best-effort outlet domain for weighting/own-outlet exclusion.

    Google News RSS links are JS redirects that cannot be fetched
    server-side, so the outlet is resolved from its source_url attribute,
    display name, article URL (if non-google), or title suffix.
    Returns "" when the outlet is unknown (weight 0.40 downstream).
    """
    try:
        from app.outlets import resolve_outlet
        resolved = resolve_outlet(
            source_name=it.get("outlet", ""),
            source_url=it.get("source_url", ""),
            title=it.get("title", ""),
        )
        if resolved.domain:
            return resolved.domain
    except Exception:
        pass

    source_url = (it.get("source_url") or "").strip()
    if source_url:
        host = _clean_host(urllib.parse.urlparse(source_url).netloc.split(":")[0])
        if host and "news.google.com" not in host:
            return host

    name = (it.get("outlet") or "").strip().lower()
    if name in OUTLET_NAME_DOMAINS:
        return OUTLET_NAME_DOMAINS[name]

    title = it.get("title", "")
    if "-" in title or "–" in title or "—" in title:
        parts = re.split(r"\s+[-–—]\s+", title)
        if len(parts) > 1:
            suffix_name = parts[-1].strip().lower()
            if suffix_name in OUTLET_NAME_DOMAINS:
                return OUTLET_NAME_DOMAINS[suffix_name]

    link = (it.get("link") or "").strip()
    if link and "news.google.com" not in link:
        host = _clean_host(urllib.parse.urlparse(link).netloc.split(":")[0])
        if host:
            return host

    blob = (link + " " + name + " " + source_url + " " + title).lower()
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


class CandidateList(list):
    """List subclass that retains retrieval diagnostics as an attribute."""

    def __init__(self, items=None, diagnostics=None):
        super().__init__(items or [])
        self.diagnostics = diagnostics or {}


def retrieve_reference_sources(
    claim_text: str,
    queries: Optional[List[str]] = None,
    region: str = "in",
    page_domain: str = "",
    cfg=None,
    fetch_func: Optional[Callable[..., Optional[bytes]]] = None,
    now: Optional[datetime] = None,
) -> CandidateList:
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

    if queries is None and fetch_func is None:
        try:
            from app.retrieval import Candidate, adaptive_retrieve

            def _q_of(dom: str, nm: str) -> float:
                return source_weight(dom, cfg)

            def _same(a: Any, b: Any) -> bool:
                t1 = a.title if isinstance(a, Candidate) else getattr(a, "title", str(a))
                t2 = b.title if isinstance(b, Candidate) else getattr(b, "title", str(b))
                d1 = a.domain if isinstance(a, Candidate) else getattr(a, "domain", "")
                d2 = b.domain if isinstance(b, Candidate) else getattr(b, "domain", "")
                return _likely_syndicated({"title": t1, "domain": d1}, {"title": t2, "domain": d2})

            def _grp(dom: str) -> str:
                return publisher_group(dom) or ""

            res = adaptive_retrieve(claim_text, cfg, _q_of, _same, _grp)
            items = []
            for c in res.candidates:
                sim = title_similarity(claim_text, c.title)
                pub_iso = datetime.fromtimestamp(c.published, tz=timezone.utc).isoformat() if c.published else None
                age_h = round(max((now.timestamp() - c.published) / 3600.0, 0.0), 1) if c.published else None
                items.append({
                    "outlet": c.outlet,
                    "domain": c.domain,
                    "url": c.link,
                    "title": c.title[:200],
                    "similarity": round(max(sim, 0.40), 3),
                    "published": pub_iso,
                    "age_hours": age_h,
                    "independence_group": c.domain,
                    "syndication_cluster_size": 1,
                    "clustered_outlets": [c.outlet],
                    "is_wire_syndication": c.is_wire_syndication,
                    "wire_agency": c.wire_agency,
                    "independence_weight": c.independence_weight,
                    "tier": source_tier(c.domain),
                })
            cl = CandidateList(items, diagnostics={
                "queries_run": res.queries_run,
                "raw_items": res.raw_items,
                "distinct_outlets": res.distinct_outlets,
                "independent_origins": res.independent_origins,
                "escalated": res.escalated,
                "early_stopped": res.early_stopped,
                "latency_ms": res.latency_ms,
                "status": res.status,
            })
            cl.raw_items = res.raw_items
            cl.distinct_outlets = res.distinct_outlets
            cl.independent_origins = res.independent_origins
            cl.escalated = res.escalated
            return cl
        except Exception:
            pass

    queries = list(queries or [claim_text])
    own_base = _registrable(page_domain) if page_domain else ""
    own_slug = _slug(page_domain)
    rss_cache = get_cache("rss", maxsize=512, ttl=cfg.rss_cache_ttl)
    candidates: Dict[str, Dict[str, Any]] = {}
    target = claim_text or ""

    diag: Dict[str, Any] = {
        "queries_generated": list(queries[: cfg.max_queries]),
        "rss_requests": [],
        "total_rss_items": 0,
        "total_candidate_urls": 0,
        "domain_resolution": {"resolved": 0, "unresolved": 0},
        "normalized_publisher_domains": [],
        "self_outlet_candidates_removed": 0,
        "untrusted_candidates_count": 0,
        "fetch_failures": 0,
        "successfully_fetched": 0,
        "semantically_relevant": 0,
        "irrelevant_candidates": 0,
        "same_owner_candidates_removed": 0,
        "syndicated_candidates_removed": 0,
        "independent_candidates": 0,
    }

    # The broad claim searches maximize recall. The separately scoped outlet
    # searches make the 5-10 publisher policy observable and prevent a result
    # from appearing before the configured set of publishers has been tried.
    outlet_domains = _outlet_search_domains(region, cfg.outlet_checks)
    diag["outlet_checks_requested"] = list(outlet_domains)
    diag["outlet_checks_attempted"] = 0
    diag["outlet_checks_with_results"] = 0
    diag["off_outlet_results_rejected"] = 0

    plan: List[Any] = []          # (query, cache_key, items, expected_domain, kind)
    uncached: List[str] = []
    for query in queries[: cfg.max_queries]:
        if not query or not _tokenize(query):
            continue
        cache_key = f"{region}:{query}"
        hit, items = rss_cache.get(cache_key)
        plan.append((query, cache_key, items if hit else None, "", "broad"))
        if not hit:
            uncached.append(query)

    for domain in outlet_domains:
        query = _outlet_search_query(target, domain)
        cache_key = f"{region}:{query}"
        hit, items = rss_cache.get(cache_key)
        plan.append((query, cache_key, items if hit else None, domain, "outlet"))
        if not hit:
            uncached.append(query)

    bodies: Dict[str, Optional[bytes]] = {}
    if uncached:
        with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
            futures = {
                pool.submit(fetch, _google_news_url(q, region)): q for q in uncached
            }
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    res = fut.result()
                    bodies[q] = res
                except Exception:
                    bodies[q] = None

    for query, cache_key, items, expected_domain, kind in plan:
        if kind == "outlet":
            diag["outlet_checks_attempted"] += 1
        if items is None:
            raw_body = bodies.get(query)
            if raw_body is None:
                diag["fetch_failures"] += 1
                diag["rss_requests"].append({"query": query, "status": "failed", "item_count": 0})
            else:
                diag["successfully_fetched"] += 1
            items = _parse_rss_items(raw_body, cfg.max_query_sources)
            if items:
                rss_cache.set(cache_key, items)
                diag["rss_requests"].append({"query": query, "status": "success", "item_count": len(items)})
        else:
            diag["rss_requests"].append({"query": query, "status": "cache_hit", "item_count": len(items)})

        diag["total_rss_items"] += len(items)
        outlet_result_found = False

        for it in items:
            diag["total_candidate_urls"] += 1
            domain = _resolve_outlet_domain(it, region)
            if domain:
                diag["domain_resolution"]["resolved"] += 1
                if domain not in diag["normalized_publisher_domains"]:
                    diag["normalized_publisher_domains"].append(domain)
            else:
                diag["domain_resolution"]["unresolved"] += 1

            # Google News normally honours site:, but treat an off-outlet
            # result as a retrieval miss rather than misrepresenting it as a
            # check of the requested news publisher.
            if expected_domain and (
                not domain or _registrable(domain) != _registrable(expected_domain)
            ):
                diag["off_outlet_results_rejected"] += 1
                continue
            if expected_domain:
                outlet_result_found = True

            if own_base and domain and _registrable(domain) == own_base:
                diag["self_outlet_candidates_removed"] += 1
                continue
            outlet = it.get("outlet", "") or domain or it.get("link", "")
            if not domain and own_slug:
                out_slug = _slug(outlet)
                if out_slug and (out_slug in own_slug or own_slug in out_slug):
                    diag["self_outlet_candidates_removed"] += 1
                    continue

            sim = title_similarity(target, it["title"])
            shared = len(set(_tokenize(target)) & set(_tokenize(it["title"])))
            if sim < cfg.retrieval_floor or shared < 1:
                diag["irrelevant_candidates"] += 1
                continue
            diag["semantically_relevant"] += 1

            if source_tier(domain) == 0:
                diag["untrusted_candidates_count"] += 1

            # One outlet group is one piece of independent corroboration.  A
            # documented ownership relationship is stronger evidence than a
            # display-name match, so it comes before the normal per-domain
            # duplicate handling.
            independence_group = publisher_group(domain) if domain else ""
            key = independence_group or (outlet or "").strip().lower() or (it.get("link") or it["title"]).lower()
            prev = candidates.get(key)
            if prev is not None:
                diag["same_owner_candidates_removed"] += 1
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
                    "independence_group": independence_group or domain,
                    "syndication_cluster_size": 1,
                    "clustered_outlets": [outlet],
                }

        if kind == "outlet" and outlet_result_found:
            diag["outlet_checks_with_results"] += 1

    ranked = sorted(candidates.values(), key=lambda c: c["similarity"], reverse=True)
    independent: List[Dict[str, Any]] = []
    for candidate in ranked:
        representative = next((item for item in independent if _likely_syndicated(candidate, item)), None)
        if representative is None:
            independent.append(candidate)
            continue
        # Keep the strongest match as the representative, but retain explicit
        # provenance that copies were collapsed so reviewers can see why the
        # source count is lower than the raw RSS count.
        diag["syndicated_candidates_removed"] += 1
        representative["syndication_cluster_size"] += 1
        outlets = representative.setdefault("clustered_outlets", [])
        if candidate.get("outlet") and candidate["outlet"] not in outlets:
            outlets.append(candidate["outlet"])
    diag["independent_candidates"] = len(independent)
    return CandidateList(independent, diagnostics=diag)
