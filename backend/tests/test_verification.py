"""Deterministic offline tests for the claim-based verification system:
claim extraction/normalization, semantic similarity, relationship
classification, weighted consensus, factual consistency, evidence score,
contradiction penalty, breaking-news detection, retrieval caps and source
weighting. No network: Gemini is disabled by conftest and RSS is mocked.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.claim_analysis import (  # noqa: E402
    Claim,
    extract_claim,
    generate_search_queries,
    normalize_claim,
)
from app.config import get_config  # noqa: E402
from app.sources import source_tier, source_weight  # noqa: E402
from app.verification import (  # noqa: E402
    CONTRADICTS,
    BREAKING,
    EARLY_REPORT,
    INSUFFICIENT,
    SUFFICIENT,
    SUPPORTS,
    UNKNOWN,
    VerificationResult,
    SourceAssessment,
    apply_contradiction_penalty,
    calculate_consensus,
    calculate_evidence_score,
    calculate_fact_consistency,
    calculate_semantic_similarity,
    calculate_source_quality,
    classify_relationship,
    detect_breaking_news,
    get_final_verification_result,
)


def _assessment(relationship=SUPPORTS, weight=1.0, similarity=90.0, **kw):
    return SourceAssessment(
        outlet=kw.pop("outlet", "Test Outlet"),
        title=kw.pop("title", "Some reference title"),
        weight=weight,
        similarity=similarity,
        relationship=relationship,
        **kw,
    )


# ---------------------------------------------------------------------------
# Semantic similarity (wording tolerance)
# ---------------------------------------------------------------------------

def test_semantic_similarity_identical_is_100():
    text = "India launches new education scheme for college students"
    assert calculate_semantic_similarity(text, text) == pytest.approx(100.0, abs=0.5)


def test_semantic_similarity_recognizes_paraphrase():
    a = "India launches new education scheme for college students"
    b = "New scheme introduced by India to support university students"
    sim = calculate_semantic_similarity(a, b)
    assert sim >= 40.0  # same underlying event, different wording


def test_semantic_similarity_unrelated_is_low():
    a = "CAFE norms tightened for new vehicles"
    b = "Scientists vanish after time machine demo"
    assert calculate_semantic_similarity(a, b) < 30.0


def test_semantic_similarity_prefers_gemini_value_when_provided():
    assert calculate_semantic_similarity("a", "b", gemini_similarity=87) == 87.0
    assert calculate_semantic_similarity("a", "b", gemini_similarity=150) == 100.0


def test_normalize_claim_strips_wording_noise():
    a = normalize_claim("India launches new education scheme for college students - The Hindu")
    b = normalize_claim("New scheme introduced by India to support university students")
    assert "the hindu" not in a
    assert a and b
    # stems align plural/tense variants
    assert "student" in a  # students -> student


# ---------------------------------------------------------------------------
# Relationship classification
# ---------------------------------------------------------------------------

def _claim(facts=None, text="India launches scheme for students"):
    return Claim(claim=text, important_facts=facts or [])


def test_classify_supports_similar_clean_source():
    assert classify_relationship(_claim(), "India launches scheme for students", 85) == SUPPORTS


def test_classify_unrelated_is_unknown():
    assert classify_relationship(_claim(), "Scientists vanish after demo", 20) == UNKNOWN


def test_classify_mid_similarity_without_conflict_is_unknown():
    assert classify_relationship(_claim(), "India launches programme for pupils", 50) == UNKNOWN


def test_classify_same_unit_different_number_contradicts():
    claim = _claim(facts=[{"type": "number", "value": "3,000 crore", "critical": True}])
    ref = "Centre raises allocation to Rs 2,000 crore for states"
    assert classify_relationship(claim, ref, 70) == CONTRADICTS


def test_classify_different_dimension_is_not_a_conflict():
    claim = _claim(facts=[{"type": "number", "value": "526 kg", "critical": True}])
    ref = "Drugs worth Rs 3,000 crore seized from Iranian boat"
    assert classify_relationship(claim, ref, 78) == SUPPORTS


def test_classify_denial_language_contradicts():
    ref = "Fact-check: viral claim about India scheme is false"
    assert classify_relationship(_claim(), ref, 55) == CONTRADICTS


def test_classify_gemini_relationship_wins():
    claim = _claim()
    assert (
        classify_relationship(claim, "Some very similar title", 90, gemini_relationship=CONTRADICTS)
        == CONTRADICTS
    )


# ---------------------------------------------------------------------------
# Weighted cross-source consensus
# ---------------------------------------------------------------------------

def test_consensus_all_supports_is_100():
    assessments = [_assessment(SUPPORTS, weight=1.0, similarity=90)]
    assert calculate_consensus(assessments) == pytest.approx(100.0, abs=0.5)


def test_consensus_all_unknown_is_half_weight_neutral():
    assessments = [_assessment(UNKNOWN, weight=0.4, similarity=10)]
    assert calculate_consensus(assessments) == pytest.approx(50.0, abs=0.5)


def test_consensus_all_contradicts_is_zero():
    assessments = [_assessment(CONTRADICTS, weight=1.0, similarity=90)]
    assert calculate_consensus(assessments) == pytest.approx(0.0, abs=0.5)


def test_consensus_mixed_weighted_formula():
    assessments = [
        _assessment(SUPPORTS, weight=1.0, similarity=90),
        _assessment(UNKNOWN, weight=0.4, similarity=10),
        _assessment(CONTRADICTS, weight=0.85, similarity=80),
    ]
    # A=0.9, U=0.4, D=0.68 -> ((0.9 + 0.2) / 1.98) * 100
    assert calculate_consensus(assessments) == pytest.approx(55.56, abs=0.5)


def test_consensus_thin_pool_pulled_toward_neutral():
    assessments = [_assessment(SUPPORTS, weight=1.0, similarity=100)]
    raw = calculate_consensus(assessments)
    damped = calculate_consensus(assessments, coverage_multiplier=0.4)
    assert raw == pytest.approx(100.0)
    assert damped == pytest.approx(70.0)  # 50 + 50 * 0.4: no pretend-strong consensus


def test_consensus_empty_is_neutral():
    assert calculate_consensus([]) == get_config().neutral_score


# ---------------------------------------------------------------------------
# Factual consistency
# ---------------------------------------------------------------------------

def test_fact_consistency_matching_number():
    claim = _claim(facts=[{"type": "number", "value": "3,000 crore", "critical": True}])
    assessments = [
        _assessment(SUPPORTS, similarity=85, title="Rs 3,000 crore drugs seized from boat")
    ]
    assert calculate_fact_consistency(claim, assessments) == pytest.approx(100.0)


def test_fact_consistency_partial_match_counts_unmentioned_half():
    claim = _claim(
        facts=[
            {"type": "number", "value": "3,000 crore", "critical": True},
            {"type": "number", "value": "12 killed", "critical": True},
        ]
    )
    assessments = [
        _assessment(SUPPORTS, similarity=85, title="Drugs worth Rs 3,000 crore seized")
    ]
    # confirmed (2.0) + unmentioned (2.0 * 0.5) / 4.0 -> 75
    assert calculate_fact_consistency(claim, assessments) == pytest.approx(75.0)


def test_fact_consistency_mismatched_number_scores_zero():
    claim = _claim(facts=[{"type": "number", "value": "12 killed", "critical": True}])
    assessments = [
        _assessment(SUPPORTS, similarity=85, title="Road accident kills 5 killed in UP")
    ]
    # same unit, different value -> critical mismatch, no credit
    assert calculate_fact_consistency(claim, assessments) == 0.0


def test_fact_consistency_no_facts_is_neutral():
    claim = _claim(facts=[])
    assert calculate_fact_consistency(claim, []) == get_config().neutral_score


def test_fact_consistency_unknown_source_cannot_confirm():
    # An UNKNOWN source (sim 40-60) may just be about the same topic --
    # it must not *confirm* facts, only count as unmentioned (half).
    claim = _claim(facts=[{"type": "number", "value": "3,000 crore", "critical": True}])
    assessments = [
        _assessment(UNKNOWN, similarity=45, title="Rs 3,000 crore allocated for roads")
    ]
    assert calculate_fact_consistency(claim, assessments) == pytest.approx(50.0)


def test_fact_consistency_contradicting_sources_cannot_confirm():
    claim = _claim(facts=[{"type": "number", "value": "3,000 crore", "critical": True}])
    # A contradicting source never counts as *confirmation*: F stays below
    # 100 (its title does not mismatch the number, so it is unmentioned).
    assessments = [
        _assessment(CONTRADICTS, similarity=85, title="Rs 3,000 crore drugs seized")
    ]
    assert calculate_fact_consistency(claim, assessments) == pytest.approx(50.0)
    # ...and with no assessable source at all, F stays neutral (no data).
    assert calculate_fact_consistency(claim, []) == get_config().neutral_score


# ---------------------------------------------------------------------------
# Evidence score + contradiction penalty
# ---------------------------------------------------------------------------

def test_evidence_score_formula():
    assert calculate_evidence_score(90, 80, 70, 85) == pytest.approx(82.0)  # .3/.3/.2/.2
    assert calculate_evidence_score(100, 100, 100, 100) == 100.0
    assert calculate_evidence_score(0, 0, 0, 0) == 0.0
    assert calculate_evidence_score(-50, 250, 10, 90) == pytest.approx(
        0.30 * 0 + 0.30 * 100 + 0.20 * 10 + 0.20 * 90, abs=0.5
    )


def test_source_quality_is_mean_weight():
    assessments = [
        _assessment(SUPPORTS, weight=1.0, outlet="A"),
        _assessment(SUPPORTS, weight=0.4, outlet="B"),
    ]
    assert calculate_source_quality(assessments) == pytest.approx(70.0)
    assert calculate_source_quality([]) == 0.0


def test_contradiction_penalty_from_trusted_source():
    assessments = [_assessment(CONTRADICTS, weight=1.0, similarity=70, outlet="Reuters")]
    e_final, applied = apply_contradiction_penalty(80.0, assessments)
    assert applied is True
    assert e_final == pytest.approx(65.0)


def test_contradiction_penalty_ignored_for_weak_source():
    assessments = [_assessment(CONTRADICTS, weight=0.4, similarity=70, outlet="Random Blog")]
    e_final, applied = apply_contradiction_penalty(80.0, assessments)
    assert applied is False
    assert e_final == pytest.approx(80.0)


def test_evidence_score_clamped_after_penalty():
    assessments = [_assessment(CONTRADICTS, weight=1.0, similarity=70)]
    e_final, _ = apply_contradiction_penalty(5.0, assessments)
    assert 0.0 <= e_final <= 100.0


# ---------------------------------------------------------------------------
# Breaking news
# ---------------------------------------------------------------------------

def test_breaking_news_recent_with_sparse_coverage():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    info = detect_breaking_news(now - timedelta(hours=6), 1, now=now)
    assert info["is_breaking"] is True


def test_breaking_news_not_triggered_for_old_articles():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    info = detect_breaking_news(now - timedelta(days=10), 0, now=now)
    assert info["is_breaking"] is False


def test_breaking_news_not_triggered_with_enough_sources():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    info = detect_breaking_news(now - timedelta(hours=6), 5, now=now)
    assert info["is_breaking"] is False


def test_breaking_news_never_guesses_unknown_or_future_dates():
    assert detect_breaking_news(None, 0)["is_breaking"] is False
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    info = detect_breaking_news(now + timedelta(days=3), 0, now=now)
    assert info["is_breaking"] is False


# ---------------------------------------------------------------------------
# Claim extraction / queries (offline fallback)
# ---------------------------------------------------------------------------

def test_extract_published_date_from_url_path():
    from app.claim_analysis import extract_published_date

    # The extension sends plain text (no meta tags): the URL is often the
    # only place a full date appears.
    dt = extract_published_date(
        "Plain article copy with no date information at all.",
        url="https://example.in/2026/10/01/big-story.html",
    )
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 10, 1)
    # Unknown recency stays unknown -- never guessed.
    assert extract_published_date("No dates here.", url="https://example.in/story.html") is None
    # A date inside the page wins over the URL.
    meta = '<meta property="article:published_time" content="2026-09-29T10:00:00Z">'
    dt2 = extract_published_date(meta, url="https://example.in/2026/10/01/story.html")
    assert dt2 is not None and dt2.day == 29


def test_extract_claim_deterministic_fallback():
    headline = "India launches Rs 3,000 crore education scheme for college students"
    claim = extract_claim(headline, "")
    assert claim.claim.strip() == headline
    assert claim.source == "deterministic"
    assert claim.numbers and "3,000 crore" in claim.numbers[0]
    assert any(f["type"] == "number" for f in claim.important_facts)


def test_extract_claim_caches_by_headline():
    a = extract_claim("Some brand new claim about space travel", "")
    b = extract_claim("Some brand new claim about space travel", "")
    assert a is b  # same object from the claim cache: extraction runs once


def test_generate_search_queries_returns_two_to_three():
    claim = extract_claim("India launches new education scheme for college students", "")
    queries = generate_search_queries(claim, claim.claim)
    assert 2 <= len(queries) <= 3
    assert all(q.strip() for q in queries)
    assert len(set(q.lower() for q in queries)) == len(queries)


def test_gemini_stays_disabled_without_key():
    from app.gemini_client import gemini_enabled, gemini_json

    assert gemini_enabled() is False
    assert gemini_json("should never hit the network") is None


# ---------------------------------------------------------------------------
# Full verification result (offline)
# ---------------------------------------------------------------------------

_DRUGS_CLAIM = "Rs 3,000 crore drugs seized from Iranian boat in Arabian Sea"


def _candidates():
    return [
        {
            "outlet": "India Today",
            "domain": "indiatoday.in",
            "url": "https://news.google.com/rss/articles/abc",
            "title": "526 kg drugs worth Rs 3,000 crore seized from Iranian boat",
            "similarity": 0.78,
        },
        {
            "outlet": "BBC",
            "domain": "bbc.com",
            "url": "https://news.google.com/rss/articles/def",
            "title": "Iranian boat seized in Arabian Sea with Rs 3,000 crore drugs",
            "similarity": 0.75,
        },
    ]


def test_full_verification_insufficient_but_supported():
    claim = extract_claim(_DRUGS_CLAIM, "")
    result = get_final_verification_result(claim, _candidates(), region="in")
    assert isinstance(result, VerificationResult)
    assert result.state == INSUFFICIENT          # 2 < min 5, not "fake"
    assert result.selected == 2
    assert result.support_count == 2
    assert 0 <= result.M <= 100 and result.M >= 40
    assert 0 <= result.C <= 100
    assert 0 <= result.e_final <= 100
    assert result.penalty_applied is False
    assert result.breaking is False              # no date -> never breaking
    # thin pool must not pretend consensus is strong
    assert result.C < 100.0
    d = result.to_dict()
    assert d["state"] == INSUFFICIENT
    assert len(d["sources"]) == 2
    assert d["claim"]["claim"]


def test_full_verification_breaking_news_state():
    claim = extract_claim("Blast reported at city market", "")
    now = datetime.now(timezone.utc)
    result = get_final_verification_result(
        claim,
        [
            {
                "outlet": "India Today",
                "domain": "indiatoday.in",
                "url": "https://news.google.com/rss/articles/abc",
                "title": "Blast reported at city market, several injured",
                "similarity": 0.8,
            }
        ],
        region="in",
        published_date=now - timedelta(hours=3),
    )
    assert result.breaking is True
    assert result.state == BREAKING
    assert result.support_count == 1
    # breaking news keeps raw consensus: no thin-pool dampening
    assert result.C == pytest.approx(100.0)


def test_full_verification_early_report_without_sources():
    claim = extract_claim("Blast reported at city market", "")
    now = datetime.now(timezone.utc)
    result = get_final_verification_result(
        claim, [], region="in", published_date=now - timedelta(hours=1)
    )
    assert result.state == EARLY_REPORT
    assert result.selected == 0


def test_similarity_and_consensus_treat_unknown_as_neutral():
    # A source that is about the same topic but not clearly the same claim
    # must neither pad the similarity score M nor count as support.
    claim = extract_claim(_DRUGS_CLAIM, "")
    result = get_final_verification_result(
        claim,
        [
            {
                "outlet": "NDTV",
                "domain": "ndtv.com",
                "url": "https://news.google.com/rss/articles/z",
                "title": "Iranian boat detained by coast guard in Arabian Sea",
                "similarity": 0.5,
            }
        ],
        region="in",
    )
    assert result.assessments[0].relationship == UNKNOWN
    assert result.M == 0.0                    # UNKNOWN never pads M
    assert result.C == pytest.approx(50.0)    # unknown-only: half-weight neutral
    assert result.support_count == 0


def test_full_verification_penalizes_trusted_contradiction():
    claim = extract_claim(_DRUGS_CLAIM, "")
    candidates = [
        {
            "outlet": "Reuters",
            "domain": "reuters.com",
            "url": "https://news.google.com/rss/articles/xyz",
            "title": "Rs 3,000 crore drug seizure from Iranian boat is false, officials deny claim",
            "similarity": 0.7,
        },
        {
            "outlet": "India Today",
            "domain": "indiatoday.in",
            "url": "https://news.google.com/rss/articles/abc",
            "title": "526 kg drugs worth Rs 3,000 crore seized from Iranian boat",
            "similarity": 0.78,
        },
    ]
    result = get_final_verification_result(claim, candidates, region="in")
    assert result.contradict_count >= 1
    if result.penalty_applied:
        assert result.e_final == pytest.approx(result.E - get_config().contradiction_penalty)
    assert 0 <= result.e_final <= 100


def test_full_verification_no_sources_invents_nothing():
    claim = extract_claim("Totally made up story about aliens in Delhi", "")
    result = get_final_verification_result(claim, [], region="in")
    assert result.state == INSUFFICIENT
    assert result.selected == 0
    assert result.support_count == 0
    assert result.e_final >= 0  # neutral-ish, no fabricated confidence


def test_max_sources_cap_respected():
    claim = extract_claim(_DRUGS_CLAIM, "")
    candidates = [
        {
            "outlet": f"Outlet {i}",
            "domain": "",
            "url": f"https://news.google.com/rss/articles/{i}",
            "title": f"Drugs worth Rs 3,000 crore seized from Iranian boat report {i}",
            "similarity": 0.7,
        }
        for i in range(30)
    ]
    cfg = get_config()
    result = get_final_verification_result(claim, candidates, region="in", cfg=cfg)
    assert result.examined == 30
    assert result.selected == cfg.max_sources == 10  # process at most 10


# ---------------------------------------------------------------------------
# Source weighting
# ---------------------------------------------------------------------------

def test_source_tiers():
    assert source_tier("www.reuters.com") == 1
    assert source_tier("news.bbc.com") == 1
    assert source_tier("timesofindia.indiatimes.com") == 2
    assert source_tier("dailymail.co.uk") == 3
    assert source_tier("random-blog.example") == 0
    assert source_tier("") == 0
    # lookalike suffixes must never inherit a higher tier
    assert source_tier("reuters.com.evil.example") == 0


def test_source_weights_are_configurable_defaults():
    cfg = get_config()
    assert source_weight("www.reuters.com", cfg) == cfg.tier_weight_1 == 1.00
    assert source_weight("indiatoday.in", cfg) == cfg.tier_weight_2 == 0.85
    assert source_weight("dailymail.co.uk", cfg) == cfg.tier_weight_3 == 0.65
    assert source_weight("unknown.example", cfg) == cfg.unknown_weight == 0.40


# ---------------------------------------------------------------------------
# Retrieval: 5-10 relevant sources, own outlet excluded, capped
# ---------------------------------------------------------------------------

RSS_ITEM = """
<rss><channel>
<item><title>{title}</title><link>{link}</link>
<source>{outlet}</source></item>
</channel></rss>
"""


def _mock_cc_fetch(monkeypatch, body):
    """Direct cross_check unit tests: patch the module's own fetch (its
    default boundary when no fetch_func is injected)."""
    import app.cross_check as cc

    monkeypatch.setattr(cc, "safe_fetch", lambda url, **kw: body)


def _mock_retrieval(monkeypatch, body):
    """Pipeline tests: ONE patch at the SourceRetriever boundary -- no
    module-by-module safe_fetch chasing, no cross_check patching."""
    import app.pipeline as pl

    monkeypatch.setattr(pl.SourceRetriever, "fetch", lambda url, **kw: body)


def test_retrieve_reference_sources_excludes_own_outlet_and_noise(monkeypatch):
    from app.cross_check import retrieve_reference_sources

    items = "".join(
        [
            RSS_ITEM.format(
                title="526 kg drugs worth Rs 3,000 crore seized from Iranian boat - India Today",
                link="https://news.google.com/rss/articles/1",
                outlet="India Today",
            ),
            RSS_ITEM.format(
                title="Rs 3,000 crore drugs seized from Iranian boat in Arabian Sea - The Hindu",
                link="https://news.google.com/rss/articles/2",
                outlet="The Hindu",
            ),
            RSS_ITEM.format(
                title="Weather update: heavy rainfall expected across Kerala this week",
                link="https://news.google.com/rss/articles/3",
                outlet="NDTV",
            ),
        ]
    ).encode()
    _mock_cc_fetch(monkeypatch, f"<rss><channel>{items}</channel></rss>".encode())

    results = retrieve_reference_sources(
        _DRUGS_CLAIM,
        ["rs 3000 crore drugs seized iranian boat"],
        region="in",
        page_domain="www.thehindu.com",
    )
    outlets = {r["outlet"] for r in results}
    assert "The Hindu" not in outlets      # own outlet never counts
    assert "India Today" in outlets
    assert "NDTV" not in outlets           # no shared claim tokens -> dropped
    assert all(r["similarity"] >= 0.15 for r in results)
    # ranked by relevance
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)


def test_retrieve_reference_sources_does_not_negative_cache(monkeypatch):
    from app.cross_check import retrieve_reference_sources

    # First call: network failure (empty RSS). Second call: network back.
    _mock_cc_fetch(monkeypatch, None)
    first = retrieve_reference_sources(
        _DRUGS_CLAIM, ["rs 3000 crore drugs seized iranian boat"],
        region="in", page_domain="example.com",
    )
    assert first == []

    item = RSS_ITEM.format(
        title="526 kg drugs worth Rs 3,000 crore seized from Iranian boat",
        link="https://news.google.com/rss/articles/1",
        outlet="India Today",
    )
    _mock_cc_fetch(monkeypatch, f"<rss><channel>{item}</channel></rss>".encode())
    second = retrieve_reference_sources(
        _DRUGS_CLAIM, ["rs 3000 crore drugs seized iranian boat"],
        region="in", page_domain="example.com",
    )
    assert len(second) == 1  # empty result was not served from cache


def test_retrieve_reference_sources_caps_and_stops_early(monkeypatch):
    from app.cross_check import retrieve_reference_sources

    outlets = [
        "India Today", "NDTV", "Hindustan Times", "Times of India",
        "Indian Express", "News18", "Livemint", "Business Today", "PTI", "ANI",
    ]
    many = "".join(
        RSS_ITEM.format(
            title=f"Rs 3,000 crore drugs seized from Iranian boat report {i} - {outlet}",
            link=f"https://news.google.com/rss/articles/{i}",
            outlet=outlet,
        )
        for i, outlet in enumerate(outlets)
    ).encode()
    _mock_cc_fetch(monkeypatch, f"<rss><channel>{many}</channel></rss>".encode())

    results = retrieve_reference_sources(
        _DRUGS_CLAIM, ["q1", "q2", "q3"], region="in", page_domain="example.com"
    )
    # one query already yields enough relevant outlets -> early stop,
    # and results never exceed the configured retrieval bound
    assert len(results) <= get_config().max_sources + 5
    assert len({r["outlet"] for r in results}) == len(results)  # one per outlet


# ---------------------------------------------------------------------------
# Pipeline flow: contradiction must not be masked by trusted-link hints
# ---------------------------------------------------------------------------

def test_pipeline_contradiction_not_masked_by_trusted_links(monkeypatch):
    from app.pipeline import analyze

    contras = (
        "<rss><channel>"
        "<item><title>Fact-check: Uttar Pradesh road accident death toll of 5 is false</title>"
        "<link>https://news.google.com/rss/articles/1</link><source>Reuters</source></item>"
        "<item><title>No casualties reported in Uttar Pradesh road accident</title>"
        "<link>https://news.google.com/rss/articles/2</link><source>NDTV</source></item>"
        "</channel></rss>"
    ).encode()
    _mock_retrieval(monkeypatch, contras)

    html = (
        "<html><head><title>5 killed in road accident in Uttar Pradesh</title></head><body>"
        "<article><p>At least 5 killed and 12 injured were reported after a road accident "
        "in Uttar Pradesh. "
        + "Officials said rescue work is going on. " * 12
        + "</p></article>"
        '<a href="https://www.ndtv.com/another-story">see also</a></body></html>'
    )
    result = analyze("https://city-voice.in/accident.html", region="in", html=html)

    cross = [e for e in result.evidence if e.source == "Cross-source check"]
    assert len(cross) == 1
    # Two contradicting sources + a footer link to a trusted outlet: the
    # link hint must NOT turn the cross-check into a suspicion *reduction*
    # (spec item 5 -- contradicting sources are never neutral/positive).
    assert cross[0].weight > 0
    v = result.ai_signals["verification"]
    assert v["counts"]["contradicts"] >= 1
    assert v["counts"]["supports"] == 0
    # tier-1 contradiction -> configurable penalty applied to the evidence score
    assert v["contradiction_penalty_applied"] is True
    assert v["evidence_score"] == pytest.approx(
        v["evidence_score_before_penalty"] - get_config().contradiction_penalty
    )


def test_pipeline_reports_zero_score_when_no_sources(monkeypatch):
    from app.pipeline import analyze

    _mock_retrieval(monkeypatch, b"<rss><channel></channel></rss>")
    html = (
        "<html><head><title>Completely made up story about aliens</title></head><body>"
        "<article><p>" + "Officials allegedly denied everything. " * 20 + "</p></article></body></html>"
    )
    result = analyze("https://voice-example.in/story.html", region="in", html=html)
    cc = result.ai_signals["cross_check"]
    assert cc["checked"] == 0
    assert cc["score"] == 0.0  # no data must not read as weak evidence
    assert cc["state"] == INSUFFICIENT
    # no invented confidence: evidence states the absence honestly
    cross = [e for e in result.evidence if e.source == "Cross-source check"]
    assert cross and cross[0].weight == pytest.approx(get_config().no_coverage_penalty)


# ---------------------------------------------------------------------------
# Gemini path (structured analysis only, HTTP mocked)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        import json as _json

        self._raw = _json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _enable_gemini(monkeypatch):
    monkeypatch.setenv("VERITAS_GEMINI_ENABLED", "1")
    monkeypatch.setenv("VERITAS_GEMINI_API_KEY", "test-key-123")


def test_gemini_extract_and_batch_compare_with_mocked_http(monkeypatch):
    import json as _json

    from app.gemini_client import CallBudget

    _enable_gemini(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        body = req.data.decode("utf-8")
        if "structured news-claim analysis engine" in body:
            text = _json.dumps(
                {
                    "claim": "India launched X scheme for college students",
                    "subject": "India",
                    "event": "launch",
                    "location": "India",
                    "date": "2026-09-30",
                    "numbers": [],
                    "entities": ["India", "X scheme"],
                    "important_facts": [
                        {"type": "event", "value": "launch", "critical": False}
                    ],
                    "search_queries": [
                        "India X scheme college students",
                        "India launches X scheme",
                    ],
                }
            )
        else:
            text = _json.dumps(
                [
                    {"id": 0, "semantic_similarity": 87, "relationship": "SUPPORTS"},
                    {"id": 1, "semantic_similarity": 12, "relationship": "UNKNOWN"},
                ]
            )
        return _FakeResponse({"candidates": [{"content": {"parts": [{"text": text}]}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    headline = "India launches X scheme for college students"
    claim = extract_claim(headline, "The scheme helps students.", budget=CallBudget(2))
    assert claim.source == "gemini"
    assert claim.claim == "India launched X scheme for college students"
    assert claim.event == "launch"
    assert claim.search_queries  # Gemini-provided optimized queries
    assert len(calls) == 1

    # Claim cache: re-extraction costs no extra API call.
    again = extract_claim(headline, "The scheme helps students.", budget=CallBudget(2))
    assert again is claim
    assert len(calls) == 1

    # Batch comparison: ONE call classifies every source; Gemini's structured
    # values feed the deterministic score (Gemini never decides the verdict).
    candidates = [
        {
            "outlet": "India Today",
            "domain": "indiatoday.in",
            "url": "https://news.google.com/rss/articles/a",
            "title": "India launches X scheme for college students",
            "similarity": 0.8,
        },
        {
            "outlet": "NDTV",
            "domain": "ndtv.com",
            "url": "https://news.google.com/rss/articles/b",
            "title": "Weather update across Kerala",
            "similarity": 0.2,
        },
    ]
    result = get_final_verification_result(
        claim, candidates, region="in", budget=CallBudget(2)
    )
    assert len(calls) == 2  # exactly one extra call for the whole batch
    assert result.gemini_compared is True
    assert result.assessments[0].similarity == 87.0
    assert result.assessments[0].relationship == SUPPORTS
    assert result.assessments[1].similarity == 12.0
    assert result.assessments[1].relationship == UNKNOWN
    # scoring remains deterministic and bounded
    assert 0 <= result.e_final <= 100


def test_gemini_budget_caps_calls_per_verification(monkeypatch):
    from app.gemini_client import CallBudget, gemini_json

    _enable_gemini(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        return _FakeResponse(
            {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    budget = CallBudget(get_config().gemini_max_calls)
    for _ in range(5):
        gemini_json("prompt", budget=budget)
    assert len(calls) == get_config().gemini_max_calls  # never more than budget


def test_gemini_rate_limit_falls_back_to_deterministic(monkeypatch):
    import io
    import urllib.error

    from app.gemini_client import CallBudget

    _enable_gemini(monkeypatch)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    claim = extract_claim(
        "Farmers block highway over new procurement rules", "", budget=CallBudget(2)
    )
    assert claim.source == "deterministic"  # graceful degradation, no raise
    assert claim.claim  # usable fallback claim


def test_full_pipeline_with_gemini_uses_exactly_two_calls(monkeypatch):
    """analyze(): one extraction call + ONE batched comparison call, sharing
    the per-request budget; Gemini values feed the deterministic score."""
    import json as _json

    from app.pipeline import analyze

    _enable_gemini(monkeypatch)
    rss = RSS_ITEM.format(
        title="Farmers block highway over new procurement rules across states",
        link="https://news.google.com/rss/articles/1",
        outlet="India Today",
    ).encode()
    _mock_retrieval(monkeypatch, f"<rss><channel>{rss}</channel></rss>".encode())

    calls = []

    def fake_urlopen(req, timeout=None):
        body = req.data.decode("utf-8")
        calls.append(body)
        if "structured news-claim analysis engine" in body:
            text = _json.dumps(
                {
                    "claim": "Farmers blocked a highway over new procurement rules",
                    "subject": "Farmers",
                    "event": "protest",
                    "location": "India",
                    "date": "",
                    "numbers": [],
                    "entities": ["Farmers"],
                    "important_facts": [
                        {"type": "event", "value": "protest", "critical": False}
                    ],
                    "search_queries": [
                        "farmers block highway procurement rules",
                        "farmers protest new procurement rules",
                    ],
                }
            )
        else:
            text = _json.dumps(
                [{"id": 0, "semantic_similarity": 88, "relationship": "SUPPORTS"}]
            )
        return _FakeResponse({"candidates": [{"content": {"parts": [{"text": text}]}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    html = (
        "<html><head><title>Farmers block highway over new procurement rules</title></head>"
        "<body><article><p>" + "Farmers blocked a national highway on Monday. " * 20
        + "</p></article></body></html>"
    )
    result = analyze(
        "https://city-voice.in/farmers-protest.html", region="in", html=html
    )
    v = result.ai_signals["verification"]
    assert len(calls) == 2  # exactly: 1 extraction + 1 batched compare
    assert v["gemini_compared"] is True
    assert v["claim"]["source"] == "gemini"
    assert v["sources"][0]["similarity"] == 88.0
    assert v["sources"][0]["relationship"] == SUPPORTS
    assert v["sources"][0]["compared_by"] == "gemini"
    assert result.ai_signals["cross_check"]["checked"] == 1
    assert 0 <= v["evidence_score"] <= 100


# ---------------------------------------------------------------------------
# SourceRetriever: ONE injectable retrieval boundary
# ---------------------------------------------------------------------------

_DRUGS_HTML = (
    "<html><head><title>Rs 3,000 crore drugs seized from Iranian boat</title></head>"
    "<body><article><p>"
    + "Officials said the seizure happened on Monday. " * 15
    + "</p></article></body></html>"
)

_DRUGS_RSS = (
    "<rss><channel>"
    "<item><title>526 kg drugs worth Rs 3,000 crore seized from Iranian boat - India Today</title>"
    "<link>https://news.google.com/rss/articles/1</link>"
    "<source>India Today</source></item>"
    "</channel></rss>"
).encode()


def test_source_retriever_needs_exactly_one_patch(monkeypatch):
    """Retrieval is mocked at a single boundary -- no cc.safe_fetch +
    pl.safe_fetch double patching -- and the deep page boundary stays unused."""
    import app.pipeline as pl
    from app.pipeline import SourceRetriever, analyze

    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return _DRUGS_RSS

    monkeypatch.setattr(SourceRetriever, "fetch", fake_fetch)

    def boom(*a, **kw):
        raise AssertionError("page fetch must not be reached when html is supplied")

    monkeypatch.setattr(pl, "safe_fetch", boom)

    result = analyze("https://city-voice.in/drugs-boat.html", region="in", html=_DRUGS_HTML)
    assert calls and all("news.google.com" in u for u in calls)  # went through the boundary
    assert result.ai_signals["cross_check"]["checked"] >= 1
    assert any(m.outlet == "India Today" for m in result.corroborating_sources)


def test_source_retriever_accepts_injected_fetch_func():
    """A future backend (Bing News, a fact-check API) plugs in via
    fetch_func -- no monkeypatching, no core-logic changes."""
    from app.pipeline import SourceRetriever

    retriever = SourceRetriever(fetch_func=lambda url, **kw: _DRUGS_RSS)
    results = retriever.retrieve(
        "rs 3000 crore drugs seized iranian boat",
        ["rs 3000 crore drugs seized iranian boat"],
        region="in",
    )
    assert results and results[0]["outlet"] == "India Today"


# ---------------------------------------------------------------------------
# Developer debug payload (VERITAS_DEBUG=1, internal only)
# ---------------------------------------------------------------------------

def test_debug_payload_json_only_when_veritas_debug_enabled(monkeypatch, caplog):
    import json as _json
    import logging as _logging

    from app.pipeline import analyze

    _mock_retrieval(monkeypatch, _DRUGS_RSS)

    with caplog.at_level(_logging.DEBUG, logger="veritas.pipeline"):
        analyze("https://city-voice.in/drugs-boat.html", region="in", html=_DRUGS_HTML)
    assert not [r for r in caplog.records if "debug_payload:" in r.getMessage()]
    caplog.clear()

    monkeypatch.setenv("VERITAS_DEBUG", "1")
    with caplog.at_level(_logging.DEBUG, logger="veritas.pipeline"):
        result = analyze("https://city-voice.in/drugs-boat.html", region="in", html=_DRUGS_HTML)

    raw = [r.getMessage().split("debug_payload: ", 1)[1]
           for r in caplog.records if "debug_payload: " in r.getMessage()]
    assert len(raw) == 1
    payload = _json.loads(raw[0])  # single JSON-serializable object
    for key in ("claim", "sources_checked", "sources", "evidence", "verdict",
                "suspicion", "cross_weight"):
        assert key in payload
    for key in ("M", "C", "F", "S", "E", "state", "breaking"):
        assert key in payload["evidence"]
    assert payload["verdict"] == result.verdict
    assert payload["sources_checked"] >= 1
    assert isinstance(payload["evidence"]["M"], (int, float))
    assert payload["sources"][0]["relationship"] in (SUPPORTS, CONTRADICTS, UNKNOWN)


# ---------------------------------------------------------------------------
# Breaking news: source freshness, not just article recency
# ---------------------------------------------------------------------------

def test_breaking_news_gated_by_stale_source_pool():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    # Recent article, thin coverage, but the pool is a day old: recycled
    # coverage of an older event -- not a fresh story.
    stale = detect_breaking_news(
        now - timedelta(hours=3), 1, now=now, median_source_age_hours=26.0
    )
    assert stale["is_breaking"] is False
    assert "stale" in stale["reason"]
    # Fresh pool: still breaking.
    fresh = detect_breaking_news(
        now - timedelta(hours=3), 1, now=now, median_source_age_hours=0.5
    )
    assert fresh["is_breaking"] is True
    # Unknown pool freshness never blocks (same rule as unknown article dates).
    unknown = detect_breaking_news(now - timedelta(hours=3), 1, now=now)
    assert unknown["is_breaking"] is True
    assert unknown["median_source_age_hours"] is None


def test_retrieval_tracks_source_age_and_median(monkeypatch):
    from app.cross_check import median_source_age, retrieve_reference_sources

    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    claim = "Rs 3,000 crore drugs seized from Iranian boat"
    rss = (
        "<rss><channel>"
        "<item><title>526 kg drugs worth Rs 3,000 crore seized from Iranian boat - India Today</title>"
        "<link>https://news.google.com/rss/articles/1</link>"
        "<source>India Today</source><pubDate>01 Oct 2026 11:00:00 GMT</pubDate></item>"
        "<item><title>Rs 3,000 crore drugs seized from Iranian boat in Arabian Sea - NDTV</title>"
        "<link>https://news.google.com/rss/articles/2</link>"
        "<source>NDTV</source><pubDate>01 Oct 2026 10:30:00 GMT</pubDate></item>"
        "<item><title>Rs 3,000 crore drugs seized from Iranian boat: police - Hindustan Times</title>"
        "<link>https://www.hindustantimes.com/2026/09/25/drugs-boat/story.html</link>"
        "<source>Hindustan Times</source></item>"
        "</channel></rss>"
    ).encode()
    _mock_cc_fetch(monkeypatch, rss)

    results = retrieve_reference_sources(
        claim, [claim], region="in", page_domain="example.com", now=now
    )
    ages = {r["outlet"]: r["age_hours"] for r in results}
    assert ages["India Today"] == pytest.approx(1.0)      # RSS pubDate (exact)
    assert ages["NDTV"] == pytest.approx(1.5)
    # No pubDate -> full date parsed from the article URL path.
    assert ages["Hindustan Times"] == pytest.approx(156.0)

    med = median_source_age(results, now=now)
    assert med is not None and med <= 2.0  # weighted median sits with the fresh majority

    # Unknown freshness is never judged.
    assert median_source_age([{"domain": "x.in", "age_hours": None}], now=now) is None
    assert median_source_age([], now=now) is None


def test_stale_coverage_never_becomes_breaking_news(monkeypatch):
    """End to end: URL says today, the pool was published 3 days ago ->
    INSUFFICIENT_EVIDENCE, never BREAKING_NEWS."""
    from app.pipeline import analyze

    now = datetime.now(timezone.utc)
    stale_pub = (now - timedelta(days=3)).strftime("%d %b %Y %H:%M:%S GMT")
    rss = (
        "<rss><channel>"
        "<item><title>526 kg drugs worth Rs 3,000 crore seized from Iranian boat</title>"
        "<link>https://news.google.com/rss/articles/1</link>"
        f"<source>India Today</source><pubDate>{stale_pub}</pubDate></item>"
        "</channel></rss>"
    ).encode()
    _mock_retrieval(monkeypatch, rss)

    today = now.strftime("%Y/%m/%d")
    result = analyze(
        f"https://city-voice.in/news/{today}/drugs-boat.html",
        region="in",
        html=_DRUGS_HTML,
    )
    v = result.ai_signals["verification"]
    assert v["breaking"] is False
    assert v["state"] == INSUFFICIENT
    info = v["breaking_info"]
    assert info["median_source_age_hours"] is not None
    assert info["median_source_age_hours"] > get_config().breaking_max_median_age_hours
    assert "stale" in info["reason"]
