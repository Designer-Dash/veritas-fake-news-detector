"""Cross-source corroboration against trusted regional outlets.

For a given headline we:
  1. Build a news search query (Google News RSS -- no API key needed).
  2. Fetch top results, restricted to trusted outlet domains.
  3. Score similarity between the page text and each trusted article text.
  4. Aggregate into a corroboration score.

If no trusted outlet covers the story, that alone is weak evidence -- small
local stories exist -- so it contributes a small weight and lowers confidence.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from .sources import outlets_for_region, region_name

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


def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z]{3,}", text.lower())
    return [w for w in words if w not in STOPWORDS]


def _shingles(text: str, k: int = 5) -> set:
    toks = _tokenize(text)
    if len(toks) < k:
        return set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_external_links(html: str) -> List[str]:
    """External <a href> targets, used as a corroboration hint on the page itself."""
    hrefs = re.findall(r'<a[^>]+href=["\'](https?://[^"\']+)["\']', html, flags=re.I)
    return hrefs


def links_to_trusted(html: str, region: str, page_domain: str = "") -> List[str]:
    """External links from the page to *other* trusted outlets.

    Links to the page's own domain are excluded -- a news site linking to
    itself is not corroboration (this inflated match strength to 1.0 on
    thehindu.com during live testing).
    """
    trusted = outlets_for_region(region)
    # Registrable domain (last two labels) so subdomains of the page's own
    # site (epaper.thehindu.com etc.) are excluded as self-references too.
    host = (page_domain or "").lower().split("//")[-1].split("/")[0]
    own_labels = host.split(".") if host else []
    own_base = ".".join(own_labels[-2:]) if len(own_labels) >= 2 else ""
    out = []
    for h in extract_external_links(html):
        hl = h.lower()
        if own_base and own_base in hl:
            continue
        if any(t in hl for t in trusted):
            out.append(h)
    return out


def _google_news_url(query: str, region: str) -> str:
    lang_country = {"in": "en-IN", "us": "en-US", "uk": "en-GB"}.get(region, "en-IN")
    params = {
        "hl": lang_country,
        "gl": region.upper() if len(region) == 2 else "IN",
        "ceid": f"{region}:{'en' if region == 'in' else 'en'}",
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, **params}
    )


def _fetch(url: str, timeout: int = TIMEOUT) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(2_000_000)
    except Exception:
        return None


def _registrable(host: str) -> str:
    labels = host.lower().split("//")[-1].split("/")[0].split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def search_trusted_coverage(
    headline: str, region: str, limit: int = 12, page_domain: str = ""
) -> Dict[str, Any]:
    """Search trusted outlets for the story. Returns matches + aggregate score.

    `page_domain` excludes the analyzed page's own outlet from matches -- its
    own headline appearing in the news index is not corroboration.
    """
    from .text_analysis import extract_text_from_html  # local import to avoid cycle

    query = " ".join(_tokenize(headline)[:12])
    matches: List[Dict[str, Any]] = []
    corroborated = 0.0

    if not query:
        return {"matches": [], "score": 0.0, "checked": 0, "region": region_name(region)}

    rss = _fetch(_google_news_url(query, region))
    items: List[Dict[str, str]] = []
    if rss:
        try:
            root = ET.fromstring(rss)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                source = (item.findtext("source") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "outlet": source})
                if len(items) >= limit:
                    break
        except ET.ParseError:
            pass

    def outlet_domain(it: Dict[str, str]) -> str:
        """Resolve an RSS item to an outlet domain (name mapping first, then URL)."""
        name = it.get("outlet", "").strip().lower()
        if name in OUTLET_NAME_DOMAINS:
            return OUTLET_NAME_DOMAINS[name]
        blob = (it.get("link", "") + " " + name).lower()
        for t in trusted_domains:
            if t in blob:
                return t
        return ""

    trusted_domains = outlets_for_region(region)
    own_base = _registrable(page_domain) if page_domain else ""
    page_tokens = set(_tokenize(headline))

    for it in items:
        domain = outlet_domain(it)
        if not domain:
            continue
        if own_base and _registrable(domain) == own_base:
            continue  # the page's own outlet re-reporting itself is circular
        item_tokens = set(_tokenize(it["title"]))
        overlap = jaccard(item_tokens, page_tokens) if item_tokens and page_tokens else 0.0
        shared = len(item_tokens & page_tokens)

        # If RSS gave a direct (non-Google) article URL, body text similarity
        # is the stronger signal -- fetch and compare, keep the best of the two.
        body_sim = 0.0
        link = it["link"]
        if link and "news.google.com" not in link:
            body = _fetch(link)
            if body:
                from .text_analysis import extract_text_from_html
                body_sim = jaccard(_shingles(extract_text_from_html(body.decode("utf-8", errors="ignore"))), _shingles(headline))

        sim = max(overlap, body_sim)
        if sim >= 0.30 and shared >= 2:
            corroborated = max(corroborated, sim)
            matches.append(
                {
                    "outlet": it["outlet"] or domain,
                    "url": link,
                    "similarity": round(sim, 3),
                    "title": it["title"][:200],
                }
            )

    matches.sort(key=lambda m: m["similarity"], reverse=True)
    # Diminishing returns: one strong match counts a lot, extra matches less.
    score = 0.0
    for i, m in enumerate(matches[:4]):
        score += m["similarity"] * (0.5 ** i)
    return {
        "matches": matches[:5],
        "score": round(min(score, 1.0), 3),
        "checked": len(items),
        "region": region_name(region),
    }
