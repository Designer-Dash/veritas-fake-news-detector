"""Feature A (Google Fact Check Tools API) + bug-fix regression tests.

All offline: the fact-check HTTP call is mocked at the injected fetch
boundary (``fetch_func``, the same SourceRetriever contract as RSS
retrieval) — no test ever touches the network.

Covers:
  * rating normalization buckets (deterministic keyword mapping)
  * dormant-by-default behavior (no API key -> no fetch, no network)
  * payload parsing, review-URL display validation, count cap
  * per-claim caching (fetch once) and no caching of failures
  * blend_fact_check math (F' = (1-w)F + w*FC) and passthrough
  * end-to-end pipeline integration incl. key-never-leaks
  * lookalike-TLD fix (registrable domain + trusted short-circuit +
    VERITAS_TLD_WHITELIST)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache import clear_caches  # noqa: E402
from app.config import get_config  # noqa: E402
from app.factcheck import (  # noqa: E402
    CONTRADICTS as FC_CONTRADICTS,
    MIXED as FC_MIXED,
    SUPPORTS as FC_SUPPORTS,
    UNKNOWN as FC_UNKNOWN,
    FactCheckAssessment,
    fetch_fact_checks,
    normalize_rating,
    summarize,
)
from app.pipeline import analyze, domain_reputation  # noqa: E402
from app.verification import blend_fact_check  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _api_response(*reviews) -> bytes:
    """Build a canned claims:search payload.

    Each review is (claimant, textualRating, publisher, url).
    """
    claims = []
    for claimant, rating, publisher, url in reviews:
        claims.append(
            {
                "claimant": claimant,
                "text": "the reviewed claim",
                "claimReview": [
                    {
                        "publisher": {"name": publisher, "site": "example.com"},
                        "textualRating": rating,
                        "title": f"{publisher} review",
                        "url": url or "https://example.com/review",
                    }
                ],
            }
        )
    return json.dumps({"claims": claims}).encode()


def _fake_fetch(body, calls=None):
    def fetch(url, **kwargs):
        if calls is not None:
            calls.append(url)
        return body

    return fetch


def _cfg(monkeypatch, **env):
    monkeypatch.setenv("VERITAS_FACT_CHECK_API_KEY", "test-key-123")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    clear_caches()
    return get_config()


# --------------------------------------------------------------------------
# rating normalization
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rating,expected",
    [
        ("False", FC_CONTRADICTS),
        ("Pants on Fire!", FC_CONTRADICTS),
        ("Mostly false", FC_CONTRADICTS),
        ("Four Pinocchios", FC_CONTRADICTS),
        ("Debunked", FC_CONTRADICTS),
        ("True", FC_SUPPORTS),
        ("Mostly True", FC_SUPPORTS),
        ("Accurate attribution", FC_SUPPORTS),
        ("Confirmed", FC_SUPPORTS),
        ("Misleading", FC_MIXED),
        ("Out of context", FC_MIXED),
        ("Half true", FC_MIXED),
        ("Mixture of true and false", FC_MIXED),
        ("", FC_UNKNOWN),
        ("Something inexplicable", FC_UNKNOWN),
        (None, FC_UNKNOWN),
    ],
)
def test_normalize_rating_buckets(rating, expected):
    assert normalize_rating(rating) == expected


# --------------------------------------------------------------------------
# dormancy + parsing
# --------------------------------------------------------------------------

def test_dormant_without_api_key(monkeypatch):
    monkeypatch.delenv("VERITAS_FACT_CHECK_API_KEY", raising=False)
    clear_caches()
    calls = []
    got = fetch_fact_checks("some claim", cfg=get_config(), fetch_func=_fake_fetch(b"{}", calls))
    assert got == []
    assert calls == []  # no key -> no network activity at all


def test_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("VERITAS_FACT_CHECK_API_KEY", "test-key-123")
    monkeypatch.setenv("VERITAS_FACT_CHECK_ENABLED", "0")
    clear_caches()
    calls = []
    got = fetch_fact_checks("some claim", cfg=get_config(), fetch_func=_fake_fetch(b"{}", calls))
    assert got == []
    assert calls == []


def test_empty_claim_is_noop(monkeypatch):
    calls = []
    got = fetch_fact_checks(
        "   ", cfg=_cfg(monkeypatch), fetch_func=_fake_fetch(b"{}", calls)
    )
    assert got == []
    assert calls == []


def test_parses_reviews_and_maps_relationships(monkeypatch):
    cfg = _cfg(monkeypatch)
    body = _api_response(
        ("Ministry of Truth", "False", "PolitiCheck", "https://politifact.example/fact1"),
        ("Anonymous", "Mostly True", "TrueBlue Facts", ""),
    )
    got = fetch_fact_checks("some claim", cfg=cfg, fetch_func=_fake_fetch(body))
    assert len(got) == 2
    first, second = got
    assert first.relationship == FC_CONTRADICTS
    assert first.claimant == "Ministry of Truth"
    assert first.publisher == "PolitiCheck"
    assert first.rating == "False"  # verbatim
    assert first.review_url == "https://politifact.example/fact1"
    assert first.weight == pytest.approx(cfg.factcheck_reviewer_weight)
    assert second.relationship == FC_SUPPORTS


def test_count_respects_max_results(monkeypatch):
    cfg = _cfg(monkeypatch, VERITAS_FACT_CHECK_MAX_RESULTS="1")
    body = _api_response(
        ("A", "False", "P1", ""),
        ("B", "True", "P2", ""),
        ("C", "True", "P3", ""),
    )
    got = fetch_fact_checks("some claim", cfg=cfg, fetch_func=_fake_fetch(body))
    assert len(got) == 1


def test_unsafe_review_url_is_dropped(monkeypatch):
    cfg = _cfg(monkeypatch)
    body = _api_response(("A", "True", "P", "http://127.0.0.1/steal"))
    got = fetch_fact_checks("some claim", cfg=cfg, fetch_func=_fake_fetch(body))
    assert len(got) == 1
    assert got[0].review_url == ""  # never surface a non-https/private URL


def test_malformed_response_not_cached(monkeypatch):
    cfg = _cfg(monkeypatch)
    calls = []
    fetch = _fake_fetch(b"<html>502 Service Unavailable</html>", calls)
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    # A second attempt must retry (failure results are never cached).
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    assert len(calls) == 2


def test_fetch_failure_not_cached(monkeypatch):
    cfg = _cfg(monkeypatch)
    calls = []
    fetch = _fake_fetch(None, calls)
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    assert len(calls) == 2


def test_well_formed_empty_result_is_cached(monkeypatch):
    cfg = _cfg(monkeypatch)
    calls = []
    fetch = _fake_fetch(json.dumps({"claims": []}).encode(), calls)
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    assert fetch_fact_checks("some claim", cfg=cfg, fetch_func=fetch) == []
    assert len(calls) == 1  # served from the per-claim cache


def test_cache_hit_fetches_once(monkeypatch):
    cfg = _cfg(monkeypatch)
    calls = []
    body = _api_response(("A", "True", "P", ""))
    first = fetch_fact_checks("some claim", cfg=cfg, fetch_func=_fake_fetch(body, calls))
    second = fetch_fact_checks("some claim", cfg=cfg, fetch_func=_fake_fetch(body, calls))
    assert len(calls) == 1
    assert [f.to_dict() for f in first] == [f.to_dict() for f in second]


def test_roundtrip_from_dict_preserves_fields():
    fc = FactCheckAssessment(
        claimant="A", rating="False", relationship=FC_CONTRADICTS,
        publisher="P", title="T", review_url="https://x.example/", weight=1.0,
    )
    clone = FactCheckAssessment.from_dict(fc.to_dict())
    assert clone == fc


# --------------------------------------------------------------------------
# blend into F
# --------------------------------------------------------------------------

def test_blend_passthrough_without_reviews():
    assert blend_fact_check(62.0, None) == pytest.approx(62.0)
    assert blend_fact_check(62.0, []) == pytest.approx(62.0)


def test_blend_false_review_pulls_f_down():
    cfg = get_config()  # weight 0.30, reviewer_weight 1.0
    got = blend_fact_check(60.0, [FactCheckAssessment("A", "False", FC_CONTRADICTS)], cfg)
    # FC = 0 -> F' = 0.7*60 + 0.3*0 = 42
    assert got == pytest.approx(42.0)


def test_blend_true_review_raises_f():
    cfg = get_config()
    got = blend_fact_check(50.0, [FactCheckAssessment("A", "True", FC_SUPPORTS)], cfg)
    # FC = 100 -> F' = 0.7*50 + 0.3*100 = 65
    assert got == pytest.approx(65.0)


def test_blend_mixed_review_gets_half_credit():
    cfg = get_config()
    got = blend_fact_check(50.0, [FactCheckAssessment("A", "Misleading", FC_MIXED)], cfg)
    # FC = 50 -> F' unchanged (half credit equals neutral for a lone review)
    assert got == pytest.approx(50.0)

    got2 = blend_fact_check(
        50.0,
        [
            FactCheckAssessment("A", "True", FC_SUPPORTS),
            FactCheckAssessment("B", "Misleading", FC_MIXED),
        ],
        cfg,
    )
    # FC = (1 + 0.5)/2 * 100 = 75 -> F' = 0.7*50 + 0.3*75 = 57.5
    assert got2 == pytest.approx(57.5)


def test_blend_unknown_review_gets_half_credit():
    cfg = get_config()
    got = blend_fact_check(50.0, [FactCheckAssessment("A", "???", FC_UNKNOWN)], cfg)
    assert got == pytest.approx(50.0)


def test_blend_clamped_to_valid_range():
    cfg = get_config()
    got = blend_fact_check(
        100.0, [FactCheckAssessment("A", "False", FC_CONTRADICTS) for _ in range(5)], cfg
    )
    assert 0.0 <= got <= 100.0
    assert got == pytest.approx(70.0)  # 0.7*100 + 0.3*0


def test_blend_zero_reviewer_weight_is_passthrough():
    class Cfg:
        factcheck_weight = 0.30
        factcheck_reviewer_weight = 0.0

    got = blend_fact_check(50.0, [FactCheckAssessment("A", "False", FC_CONTRADICTS)], Cfg())
    assert got == pytest.approx(50.0)


def test_summarize_shape_and_counts():
    reviews = [
        FactCheckAssessment("A", "True", FC_SUPPORTS, publisher="P1"),
        FactCheckAssessment("B", "False", FC_CONTRADICTS, publisher="P2", review_url="https://x.example/"),
    ]
    s = summarize(reviews)
    assert s["checked"] == 2
    assert s["supports"] == 1
    assert s["contradicts"] == 1
    assert s["mixed"] == 0
    assert len(s["reviews"]) == 2
    assert s["reviews"][1]["review_url"] == "https://x.example/"
    json.dumps(s)  # must stay JSON-serializable


# --------------------------------------------------------------------------
# end-to-end through analyze() with the injected fetch boundary
# --------------------------------------------------------------------------

_HTML = (
    "<html><head><title>Rs 3,000 crore drugs seized from Iranian boat</title></head><body>"
    "<article><p>" + "Officials said the seizure happened on Monday. " * 30 + "</p></article>"
    "</body></html>"
)
_RSS = (
    "<rss><channel><item>"
    "<title>526 kg drugs worth Rs 3,000 crore seized from Iranian boat - India Today</title>"
    "<link>https://news.google.com/rss/articles/abc</link><source>India Today</source>"
    "</item></channel></rss>"
).encode()


def test_pipeline_blends_fact_check_into_f(monkeypatch):
    seen_urls = []

    def dispatcher(url, **kwargs):
        seen_urls.append(url)
        if "factchecktools.googleapis.com" in url:
            return _api_response(
                ("Government", "False", "PolitiCheck", "https://politifact.example/fact1")
            )
        return _RSS

    monkeypatch.setenv("VERITAS_FACT_CHECK_API_KEY", "test-key-123")
    clear_caches()

    from app.pipeline import SourceRetriever

    retriever = SourceRetriever(fetch_func=dispatcher)
    result = analyze(
        "https://city-voice.in/drugs-boat.html",
        region="in",
        html=_HTML,
        retriever=retriever,
    )
    fc = result.ai_signals.get("fact_check")
    assert fc is not None and fc["checked"] == 1 and fc["contradicts"] == 1
    # The API key is sent on the wire (only place it may appear)...
    assert any("factchecktools.googleapis.com" in u for u in seen_urls)
    # ...but NEVER leaks into the response payload.
    assert "test-key-123" not in json.dumps(result.to_dict())
    f_with = result.ai_signals["verification"]["components"]["facts_F"]
    assert result.verdict in ("trusted", "uncertain", "suspicious", "fake")
    assert 0.0 < result.confidence <= 0.90  # reachable confidence ceiling

    # Baseline run without fact-checking: F must be strictly higher.
    clear_caches()
    monkeypatch.setenv("VERITAS_FACT_CHECK_ENABLED", "0")
    baseline = analyze(
        "https://city-voice.in/drugs-boat.html",
        region="in",
        html=_HTML,
        retriever=retriever,
    )
    f_without = baseline.ai_signals["verification"]["components"]["facts_F"]
    assert f_with < f_without
    assert "fact_check" not in baseline.ai_signals


# --------------------------------------------------------------------------
# lookalike-TLD fix (registrable domain + trusted short-circuit + whitelist)
# --------------------------------------------------------------------------

def test_lookalike_penalty_uses_registrable_domain():
    # Classic fake-outlet pattern still flagged (multi-part suffix).
    assert domain_reputation("abcnews.com.co")["weight"] > 0
    # Subdomain cannot trigger or mask the check.
    assert domain_reputation("abcnews.com.co") == domain_reputation("www.abcnews.com.co")
    assert domain_reputation("story.truth-leaks-daily.xyz")["weight"] > 0


def test_trusted_outlet_never_flagged_for_tld():
    # bbc.news matches the trusted "bbc." pattern: full trust bonus, and no
    # lookalike reason even though the TLD is in LOOKALIKE_TLDS.
    rep = domain_reputation("bbc.news")
    assert rep["weight"] <= -0.25
    assert not any("Lookalike" in r for r in rep["reasons"])


def test_tld_whitelist_env_extension(monkeypatch):
    monkeypatch.setenv("VERITAS_TLD_WHITELIST", "artnet.news, example.news")
    cfg = get_config()
    assert cfg.tld_whitelist == ("artnet.news", "example.news")
    rep = domain_reputation("artnet.news")
    assert rep["weight"] == 0.0
    assert not any("Lookalike" in r for r in rep["reasons"])
    # ...but a different .news domain is still flagged.
    assert domain_reputation("spin-machine.news")["weight"] > 0
