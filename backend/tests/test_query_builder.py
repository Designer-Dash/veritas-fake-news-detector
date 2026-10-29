from __future__ import annotations

import pytest
from app.query_builder import (
    ClaimTerms,
    QueryTier,
    _clean_word,
    _compress_entity,
    _extract_terms,
    _normalize_domain,
    _outlet_search_query,
    _broad_search_queries,
    build_query_cascade,
    gather_candidates,
    MAX_WORDS,
)


def test_compress_entity():
    assert _compress_entity("Prime Minister Narendra Modi", "PERSON") == "Modi"
    assert _compress_entity("Reserve Bank of India", "ORG") == "Reserve Bank"
    assert _compress_entity("ISRO") == "ISRO"


def test_extract_terms_news_claim():
    claim = "Finance Minister Nirmala Sitharaman announced new budget tax exemption"
    terms = _extract_terms(claim)
    assert isinstance(terms, ClaimTerms)
    all_terms = terms.entities + terms.nouns
    assert any("Sitharaman" in e or "Nirmala" in e for e in all_terms)
    assert "announce" in terms.verbs or "announced" in terms.verbs


def test_build_query_cascade_tiers():
    claim = "Prime Minister Narendra Modi announced semiconductor plant in Gujarat"
    cascade = build_query_cascade(claim, domain="thehindu.com")

    tiers = [q.tier for q in cascade]
    assert "B" in tiers
    assert "A" in tiers

    # Check that site: operator query has no surrounding quotes and max words respected
    b_query = next(q.query for q in cascade if q.tier == "B")
    assert '"' not in b_query
    assert "site:thehindu.com" in b_query
    assert len(b_query.split()) <= MAX_WORDS

    a_query = next(q.query for q in cascade if q.tier == "A")
    assert '"' not in a_query
    assert len(a_query.split()) <= MAX_WORDS


def test_normalize_domain():
    assert _normalize_domain("https://www.thehindu.com/news") == "thehindu.com"
    assert _normalize_domain("indianexpress.com") == "indianexpress.com"
    assert _normalize_domain("http://timesofindia.indiatimes.com/") == "timesofindia.indiatimes.com"


def test_gather_candidates_deduplication():
    claim = "ISRO launches Chandrayaan mission"

    fake_items = [
        {"title": "ISRO launches lunar mission", "link": "https://example.com/1"},
        {"title": "Chandrayaan launch successful", "link": "https://example.com/2"},
        {"title": "ISRO moon probe details", "link": "https://example.com/1"},  # duplicate link
    ]

    def mock_fetch(query: str):
        return fake_items

    candidates = gather_candidates(claim, domain="thehindu.com", fetch_func=mock_fetch)
    assert len(candidates) == 2
    assert {c["link"] for c in candidates} == {"https://example.com/1", "https://example.com/2"}
