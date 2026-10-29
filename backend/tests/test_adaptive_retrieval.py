from __future__ import annotations

import pytest
from app.outlets import resolve_outlet, split_title, WIRE_DOMAINS
from app.rss_fetch import parse_rss, RssItem, clear_cache
from app.retrieval import (
    Candidate,
    annotate_syndication,
    select_visible,
    plan_queries,
    plan_relaxed,
)
from app.config import get_config


def test_outlets_resolution():
    o1 = resolve_outlet("The Hindu", "https://thehindu.com", "Headline - The Hindu")
    assert o1.domain == "thehindu.com"

    o2 = resolve_outlet("", "", "Major Earthquake in Tokyo - Reuters")
    assert o2.domain == "reuters.com"
    assert o2.via == "title"

    o3 = resolve_outlet("Unknown Blog", "https://randomnews.xyz", "Sample News")
    assert o3.domain == "randomnews.xyz"
    assert o3.via == "xml"


def test_split_title():
    h1, pub1 = split_title("Cabinet passes new policy - NDTV")
    assert h1 == "Cabinet passes new policy"
    assert pub1 == "NDTV"

    h2, pub2 = split_title("Mid-day report on monsoon")
    assert h2 == "Mid-day report on monsoon"
    assert pub2 == ""


def test_parse_rss():
    xml = b"""<rss version="2.0">
    <channel>
        <title>Google News</title>
        <item>
            <title>Government announces EV subsidy - Livemint</title>
            <link>https://news.google.com/rss/articles/123</link>
            <source url="https://livemint.com">Livemint</source>
            <pubDate>Thu, 29 Oct 2026 12:00:00 GMT</pubDate>
        </item>
    </channel>
    </rss>"""
    items = parse_rss(xml, "google")
    assert len(items) == 1
    assert items[0].title == "Government announces EV subsidy - Livemint"
    assert items[0].source_name == "Livemint"
    assert items[0].source_url == "https://livemint.com"
    assert items[0].published is not None


def test_select_visible_guarantees_bounds():
    cands = [
        Candidate(f"Title {i}", f"https://example{i}.com", f"Outlet {i}", f"domain{i}.com", published=100.0, quality=0.85 if i < 3 else 0.40)
        for i in range(12)
    ]
    visible = select_visible(cands, floor=0.50, lo=5, hi=10)
    # Guarantees between 5 and 10 items
    assert 5 <= len(visible) <= 10
    # First items are quality >= floor
    assert visible[0].quality >= 0.50


def test_annotate_syndication_wire():
    cands = [
        Candidate("Govt announces new policy - PTI", "https://ptinews.com/1", "PTI", "ptinews.com", published=10.0, quality=0.85),
        Candidate("Govt announces new policy - NDTV", "https://ndtv.com/1", "NDTV", "ndtv.com", published=20.0, quality=0.85),
    ]

    def same_story(a, b):
        return True

    def group_of(dom):
        return dom

    uniq, origins = annotate_syndication(cands, same_story, group_of)
    assert len(uniq) == 2
    assert origins == 1
    # Origin is PTI (full weight), NDTV is wire syndication (down-weighted)
    pti = next(c for c in uniq if c.domain == "ptinews.com")
    ndtv = next(c for c in uniq if c.domain == "ndtv.com")
    assert pti.independence_weight == 1.0
    assert ndtv.is_wire_syndication is True
    assert ndtv.wire_agency == "PTI"
    assert ndtv.independence_weight < 1.0


def test_plan_queries_cascade():
    cfg = get_config()
    queries = plan_queries("Prime Minister Narendra Modi announced AI initiative", cfg)
    assert len(queries) <= cfg.max_queries
    assert any("site:" in q or "Modi" in q for q in queries)
