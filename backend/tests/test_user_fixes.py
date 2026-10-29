"""Regression tests for the 7-day-simulation user fixes.

Offline and hermetic: RSS retrieval is mocked at the single retrieval
boundary (SourceRetriever.fetch); Gemini is disabled by conftest.

Covers:
  FIX 1  coverage_state distinguishes no-coverage from suspicious
  FIX 2  is_breaking surfaced in the response + ai_signals
  FIX 4  article-type gate short-circuits opinions/satire/PR/forecasts
  FIX 5  regional Indian outlets recognized as tier-3 sources
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cross_check import _resolve_outlet_domain  # noqa: E402
from app.pipeline import SourceRetriever, analyze, detect_article_type  # noqa: E402
from app.sources import outlets_for_region, source_tier, source_weight  # noqa: E402


def _mock_retrieval(monkeypatch, body):
    """Canned RSS responder at the single retrieval boundary."""

    def fake_fetch(url, **kwargs):
        return body

    import app.pipeline as pl

    monkeypatch.setattr(pl.SourceRetriever, "fetch", fake_fetch)


_EMPTY_RSS = b"<rss><channel></channel></rss>"

_PLAIN_HTML = (
    "<html><head><title>Flood water enters villages in Sitapur district</title></head>"
    "<body><article><p>"
    "Water levels in the Sharda river rose overnight after heavy rainfall in the "
    "hills. District officials moved families from two low-lying villages to "
    "government schools on higher ground. The irrigation department said gates "
    "were opened slowly to release pressure from the barrage. Power supply in "
    "three wards remained suspended through the morning as a precaution. "
    "Farmers in the area said standing crops are submerged in several fields. "
    "</p></article></body></html>"
)

_ELECTION_HTML = (
    "<html><head><title>Flood water enters villages in Sitapur district</title></head>"
    "<body><article><p>" + "District officials moved families to safety. " * 30 +
    "</p></article></body></html>"
)


def _rss_with(*items):
    body = "".join(
        f"<item><title>{t}</title>"
        f"<link>https://news.google.com/rss/articles/{i}</link>"
        f"<source>{o}</source></item>"
        for i, (t, o) in enumerate(items)
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


_DRUGS_TITLE = "526 kg drugs worth Rs 3,000 crore seized from Iranian boat"
_DRUGS_OUTLETS = (
    "India Today", "NDTV", "The Hindu", "Hindustan Times", "Times of India",
)
_DRUGS_RSS = _rss_with(*[(_DRUGS_TITLE, o) for o in _DRUGS_OUTLETS])
_DRUGS_HTML = (
    "<html><head><title>Rs 3,000 crore drugs seized from Iranian boat</title></head>"
    "<body><article><p>" + "Officials said the seizure happened on Monday. " * 30 +
    "</p></article></body></html>"
)


# --------------------------------------------------------------------------
# FIX 1 — coverage_state
# --------------------------------------------------------------------------

def test_local_story_without_coverage_is_neutral(monkeypatch):
    _mock_retrieval(monkeypatch, _EMPTY_RSS)
    result = analyze(
        "https://sitapurjournal.com/flood-water-villages.html",
        region="in",
        html=_PLAIN_HTML,
    )
    assert result.coverage_state == "no_local_coverage"
    assert result.verdict in ("trusted", "uncertain")
    assert result.to_dict()["coverage_state"] == "no_local_coverage"


def test_lookalike_domain_without_coverage_is_suspicious(monkeypatch):
    _mock_retrieval(monkeypatch, _EMPTY_RSS)
    result = analyze(
        "https://breakingnewz.xyz/flood-story.html",
        region="in",
        html=_ELECTION_HTML,
    )
    assert result.coverage_state == "suspicious_no_coverage"


def test_full_coverage_is_sufficient(monkeypatch):
    _mock_retrieval(monkeypatch, _DRUGS_RSS)
    result = analyze(
        "https://city-voice.in/drugs-boat.html",
        region="in",
        html=_DRUGS_HTML,
    )
    assert result.coverage_state == "sufficient_coverage"
    assert result.is_breaking is False


def test_partial_coverage_is_thin(monkeypatch):
    _mock_retrieval(monkeypatch, _rss_with((_DRUGS_TITLE, "India Today")))
    result = analyze(
        "https://city-voice.in/drugs-boat.html",
        region="in",
        html=_DRUGS_HTML,
    )
    assert result.coverage_state == "thin_coverage"


def test_no_cross_check_means_no_local_coverage(monkeypatch):
    _mock_retrieval(monkeypatch, _EMPTY_RSS)
    result = analyze(
        "https://sitapurjournal.com/flood-water-villages.html",
        region="in",
        html=_PLAIN_HTML,
        include_cross_check=False,
    )
    assert result.coverage_state == "no_local_coverage"


# --------------------------------------------------------------------------
# FIX 2 — is_breaking surfaced to the frontend
# --------------------------------------------------------------------------

def test_recent_thin_story_is_breaking(monkeypatch):
    today = datetime.now(timezone.utc)
    url = (
        f"https://sitapurjournal.com/{today:%Y}/"
        f"{today:%m}/{today:%d}/derailment-near-sitapur.html"
    )
    _mock_retrieval(monkeypatch, _rss_with((_DRUGS_TITLE, "India Today")))
    result = analyze(
        url,
        region="in",
        html=_ELECTION_HTML.replace("Flood water enters villages in Sitapur district",
                                    "Goods train derails near Sitapur junction"),
    )
    assert result.is_breaking is True
    assert result.ai_signals["cross_check"]["is_breaking"] is True
    assert result.to_dict()["is_breaking"] is True


def test_old_story_is_not_breaking(monkeypatch):
    _mock_retrieval(monkeypatch, _DRUGS_RSS)
    result = analyze(
        "https://city-voice.in/2026/01/01/drugs-boat.html",
        region="in",
        html=_DRUGS_HTML,
    )
    assert result.is_breaking is False
    assert result.ai_signals["cross_check"]["is_breaking"] is False


# --------------------------------------------------------------------------
# FIX 4 — article-type detection and early exit
# --------------------------------------------------------------------------

def test_detect_article_type_buckets():
    assert detect_article_type("RS 3,000 crore drugs seized", "body text", "thehindu.com") == "news"
    assert detect_article_type("Opinion: why the bill will fail", "text", "amarujala.com") == "opinion"
    assert detect_article_type("Editorial: a test for the coalition", "text", "x.com") == "opinion"
    assert detect_article_type("Anything", "text", "theonion.com") == "satire"
    assert detect_article_type("Anything", "text", "thefauxy.com") == "satire"
    assert detect_article_type(
        "Company launches product", "press release: the company announced today that",
        "x.com",
    ) == "press_release"
    assert detect_article_type(
        "The government will collapse and the opposition could gain",
        "text", "x.com",
    ) == "prediction"


def test_detect_article_type_word_boundaries_not_substrings():
    # Substring matching would count "will" inside "William" (2 hits ->
    # prediction); word boundaries count only the standalone "will".
    assert detect_article_type(
        "William will visit Switzerland", "text", "x.com"
    ) == "news"


def test_opinion_article_short_circuits_pipeline(monkeypatch):
    def boom(url, **kwargs):
        raise AssertionError("network fetch attempted for a non-news article")

    import app.pipeline as pl

    monkeypatch.setattr(pl.SourceRetriever, "fetch", boom)
    result = analyze(
        "https://amarujala.com/columns/opinion-why-the-bill-will-fail.html",
        region="in",
        html=(
            "<html><head><title>Opinion: why the bill will fail</title></head>"
            "<body><article><p>A column arguing about politics at length. " * 12 +
            "</p></article></body></html>"
        ),
    )
    assert result.ai_signals == {"article_type": "opinion"}
    assert result.coverage_state == "article_type_opinion"
    assert result.verdict == "uncertain"
    assert result.confidence == 0.5
    assert result.suspicion == 0.0
    assert result.evidence[0].source == "Article type"
    assert result.is_breaking is False
    json.dumps(result.to_dict())  # response stays JSON-serializable


# --------------------------------------------------------------------------
# FIX 5 — regional Indian outlets as tier-3 sources
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "domain",
    [
        "dainikbhaskar.com",
        "bhaskar.com",
        "divyamarathi.bhaskar.com",  # subdomain of a listed tier-3 domain
        "amarujala.com",
        "jagran.com",
        "navbharattimes.indiatimes.com",
        "eenadu.net",
        "dailythanthi.com",
        "mathrubhumi.com",
        "manoramaonline.com",
        "loksatta.com",
        "anandabazar.com",
        "prajavani.net",
        "gujaratsamachar.com",
        "tribuneindia.com",
    ],
)
def test_regional_outlets_are_tier3(domain):
    assert source_tier(domain) == 3
    assert source_weight(domain) == pytest.approx(0.65)


def test_hindustantimes_stays_tier2():
    assert source_tier("hindustantimes.com") == 2
    assert source_weight("hindustantimes.com") == pytest.approx(0.85)


def test_unknown_domain_still_unknown_weight():
    assert source_tier("random-webz.xyz") == 0
    assert source_weight("random-webz.xyz") == pytest.approx(0.40)


def test_outlet_display_names_resolve_to_regional_domains():
    cases = {
        "Dainik Bhaskar": "dainikbhaskar.com",
        "Bhaskar": "bhaskar.com",
        "Amar Ujala": "amarujala.com",
        "Dainik Jagran": "jagran.com",
        "Navbharat Times": "navbharattimes.indiatimes.com",
        "Eenadu": "eenadu.net",
        "Daily Thanthi": "dailythanthi.com",
        "Malayala Manorama": "manoramaonline.com",
        "The Tribune": "tribuneindia.com",
    }
    for name, domain in cases.items():
        assert _resolve_outlet_domain({"outlet": name}, "in") == domain


def test_regional_outlets_in_india_region_list():
    outlets = outlets_for_region("in")
    assert "dainikbhaskar.com" in outlets
    assert "eenadu.net" in outlets
    assert "mathrubhumi.com" in outlets


def test_regional_coverage_produces_supports(monkeypatch):
    _mock_retrieval(monkeypatch, _rss_with((_DRUGS_TITLE, "Dainik Bhaskar")))
    result = analyze(
        "https://city-voice.in/drugs-boat.html",
        region="in",
        html=_DRUGS_HTML,
    )
    assert any(m.outlet == "Dainik Bhaskar" for m in result.corroborating_sources)
    assessments = result.ai_signals["verification"]["sources"]
    bhaskar = [a for a in assessments if a["domain"] == "dainikbhaskar.com"]
    assert bhaskar and bhaskar[0]["tier"] == 3
    assert bhaskar[0]["weight"] == pytest.approx(0.65)
