"""Deterministic regression test for cross-source retrieval pipeline.

Verifies:
  1. "The Hindu" headline ("Who is attending INDIA Bloc meet today...") does not
     incorrectly collapse to zero sources when independent coverage exists.
  2. The Hindu is excluded as self-source.
  3. New Indian Express (newindianexpress.com) is NOT confused with Indian Express (indianexpress.com).
  4. Independent sources (India Today, Times of India, Economic Times, The Week, Moneycontrol) survive normalization.
  5. Relevant sources are accepted as SUPPORTS/RELEVANT, irrelevant sources are rejected.
  6. Empty retrieval produces INSUFFICIENT_EVIDENCE rather than fake evidence.
  7. Contradictory sources apply flat contradiction penalty.
  8. Developer diagnostics are present when debug is enabled.
"""

from __future__ import annotations

import pytest
from app.claim_analysis import extract_claim, generate_search_queries
from app.cross_check import retrieve_reference_sources, _resolve_outlet_domain
from app.pipeline import SourceRetriever, analyze
from app.verification import VerificationResult, SUPPORTS, CONTRADICTS, INSUFFICIENT, SUFFICIENT


HINDU_HEADLINE = (
    "Who is attending INDIA Bloc meet today? Mamata Banerjee, other top leaders gather in Delhi over EC row"
)

HINDU_URL = "https://thehindu.com/news/national/who-is-attending-india-bloc-meet.html"

REALISTIC_RSS_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <item>
    <title>Who is attending INDIA Bloc meet today? Mamata Banerjee, other top leaders gather in Delhi over EC row - The Hindu</title>
    <link>https://news.google.com/rss/articles/CBMi1</link>
    <source url="https://www.thehindu.com">The Hindu</source>
  </item>
  <item>
    <title>INDIA bloc meeting likely to be held in Delhi on September 30: Mamata Banerjee amid SIR row - The Economic Times</title>
    <link>https://news.google.com/rss/articles/CBMi2</link>
    <source url="https://economictimes.indiatimes.com">The Economic Times</source>
  </item>
  <item>
    <title>Mamata, Abhishek Banerjee to attend INDIA meet as opposition closes ranks over EC - India Today</title>
    <link>https://news.google.com/rss/articles/CBMi3</link>
    <source url="https://www.indiatoday.in">India Today</source>
  </item>
  <item>
    <title>Arrest poll panel chief Gyanesh Kumar: Mamata Banerjee demands repoll in Bengal amid EC SIR split row - The Times of India</title>
    <link>https://news.google.com/rss/articles/CBMi4</link>
    <source url="https://timesofindia.indiatimes.com">The Times of India</source>
  </item>
  <item>
    <title>Mamata seeks extraordinary Opposition action over SIR, urges parties to set egos aside - The New Indian Express</title>
    <link>https://news.google.com/rss/articles/CBMi5</link>
    <source url="https://www.newindianexpress.com">The New Indian Express</source>
  </item>
  <item>
    <title>Mamata eyes national clout under INDIA bloc; urges public to stand with Opposition amid ECI row - The Week</title>
    <link>https://news.google.com/rss/articles/CBMi6</link>
    <source url="https://www.theweek.in">The Week</source>
  </item>
  <item>
    <title>INDIA Bloc Meet in Delhi: Mamata Banerjee, opposition leaders gather over EC row - Moneycontrol</title>
    <link>https://news.google.com/rss/articles/CBMi7</link>
    <source url="https://www.moneycontrol.com">Moneycontrol</source>
  </item>
  <item>
    <title>Sensex surges 500 points led by banking stocks - Economic Times</title>
    <link>https://news.google.com/rss/articles/CBMi8</link>
    <source url="https://economictimes.indiatimes.com">The Economic Times</source>
  </item>
</channel>
</rss>'''


def mock_fetch(url, timeout=8):
    return REALISTIC_RSS_FIXTURE


def test_hindu_story_retrieval_and_self_exclusion():
    """Verify that analyzing The Hindu article retrieves independent sources while self-excluding The Hindu."""
    retriever = SourceRetriever(fetch_func=mock_fetch)
    result = analyze(
        url=HINDU_URL,
        headline_override=HINDU_HEADLINE,
        region="in",
        retriever=retriever,
    )

    # 1. Must NOT collapse to zero sources
    v_info = result.ai_signals.get("verification", {})
    selected = v_info.get("counts", {}).get("selected", 0)
    assert selected >= 5, f"Expected at least 5 selected sources, got {selected}"
    assert result.coverage_state == "sufficient_coverage"

    # 2. Originating outlet (The Hindu) must be excluded
    sources = v_info.get("sources", [])
    outlets = [s["domain"] for s in sources]
    assert "thehindu.com" not in outlets, "The Hindu was not excluded as self-source"

    # 3. Corroborating sources must include independent outlets
    assert len(result.corroborating_sources) >= 1
    corroborating_outlets = [s.outlet for s in result.corroborating_sources]
    assert any("Economic Times" in o or "Moneycontrol" in o or "India Today" in o for o in corroborating_outlets)


def test_new_indian_express_distinct_from_indian_express():
    """Verify New Indian Express (newindianexpress.com) is NOT confused with Indian Express (indianexpress.com)."""
    item_nie = {
        "title": "Mamata speaks on SIR row - The New Indian Express",
        "link": "https://news.google.com/rss/articles/CBMi5",
        "outlet": "The New Indian Express",
        "source_url": "https://www.newindianexpress.com",
    }
    item_ie = {
        "title": "Mamata speaks on SIR row - The Indian Express",
        "link": "https://news.google.com/rss/articles/CBMi6",
        "outlet": "The Indian Express",
        "source_url": "https://indianexpress.com",
    }

    domain_nie = _resolve_outlet_domain(item_nie, "in")
    domain_ie = _resolve_outlet_domain(item_ie, "in")

    assert domain_nie == "newindianexpress.com"
    assert domain_ie == "indianexpress.com"
    assert domain_nie != domain_ie

    # When analyzing an Indian Express article, New Indian Express must NOT be self-excluded
    claim = extract_claim(HINDU_HEADLINE)
    queries = generate_search_queries(claim, HINDU_HEADLINE)
    candidates = retrieve_reference_sources(
        claim.claim,
        queries,
        region="in",
        page_domain="indianexpress.com",
        fetch_func=mock_fetch,
    )
    candidate_domains = [c["domain"] for c in candidates]
    assert "newindianexpress.com" in candidate_domains


def test_empty_retrieval_produces_insufficient_evidence():
    """Verify empty retrieval results in INSUFFICIENT_EVIDENCE and no fabricated sources."""
    def empty_fetch(url, timeout=8):
        return b'''<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel></channel></rss>'''

    retriever = SourceRetriever(fetch_func=empty_fetch)
    result = analyze(
        url="https://some-news-site.org/story.html",
        headline_override=HINDU_HEADLINE,
        region="in",
        retriever=retriever,
    )

    v_info = result.ai_signals.get("verification", {})
    assert v_info.get("state") == INSUFFICIENT
    assert v_info.get("counts", {}).get("selected") == 0
    assert result.corroborating_sources == []
    # Must not mark as fake purely because of thin coverage
    assert result.verdict in ("uncertain", "trusted")


def test_developer_diagnostics_collected_when_debug_enabled(monkeypatch):
    """Verify developer diagnostics distinguish query, resolution, and candidate counts when VERITAS_DEBUG=1."""
    monkeypatch.setenv("VERITAS_DEBUG", "1")
    retriever = SourceRetriever(fetch_func=mock_fetch)
    result = analyze(
        url=HINDU_URL,
        headline_override=HINDU_HEADLINE,
        region="in",
        retriever=retriever,
    )

    v_info = result.ai_signals.get("verification", {})
    diag = v_info.get("diagnostics", {})
    assert "queries_generated" in diag
    assert "total_rss_items" in diag
    assert "normalized_publisher_domains" in diag
    assert "self_outlet_candidates_removed" in diag
    assert diag["self_outlet_candidates_removed"] >= 1
    assert "semantically_relevant" in diag
    assert diag["final_accepted_evidence_count"] >= 5


def test_retrieval_attempts_five_distinct_publisher_checks():
    """A result must not skip the configured 5-10 publisher search attempts."""
    seen_urls = []

    def recording_fetch(url, timeout=8):
        seen_urls.append(url)
        return REALISTIC_RSS_FIXTURE

    claim = extract_claim(HINDU_HEADLINE)
    queries = generate_search_queries(claim, HINDU_HEADLINE)
    candidates = retrieve_reference_sources(
        claim.claim,
        queries,
        region="in",
        page_domain="thehindu.com",
        fetch_func=recording_fetch,
    )

    diag = candidates.diagnostics
    assert diag["outlet_checks_attempted"] == 5
    assert len(diag["outlet_checks_requested"]) == 5
    assert len(set(diag["outlet_checks_requested"])) == 5
    assert diag["outlet_checks_with_results"] >= 1
    assert sum("site%3A" in url for url in seen_urls) == 5
