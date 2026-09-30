"""Deterministic tests for text analysis, cross-check scoring, pipeline and API.

Network-touching code paths (RSS / article / media fetches) are mocked so the
suite runs offline and fast; live behavior was verified separately.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cross_check import (  # noqa: E402
    _normalize_text,
    _registrable,
    _strip_source_suffix,
    jaccard,
    links_to_trusted,
    title_similarity,
)
from app.domain import verdict_from_suspicion  # noqa: E402
from app.pipeline import analyze, domain_reputation  # noqa: E402
from app.text_analysis import (  # noqa: E402
    ai_stylometry,
    caps_exclaims,
    extract_article_text,
    extract_headline,
    phrase_hits,
    CLICKBAIT_PATTERNS,
)


# --------------------------------------------------------------------------
# text_analysis
# --------------------------------------------------------------------------

def test_normalize_text_keeps_digits_drops_punctuation():
    assert _normalize_text("₹3,000 crore!") == " 3000 crore "
    assert "2026" in _normalize_text("dated 2026-09-30")


def test_strip_source_suffix_removes_outlet_tail():
    title = "Big story about boats - The Hindu"
    assert _strip_source_suffix(title) == "Big story about boats"
    assert _strip_source_suffix("Just a plain headline") == "Just a plain headline"


def test_title_similarity_identical_paraphrase_unrelated():
    a = "Rs 3,000 crore drugs seized from Iranian boat in Arabian Sea"
    assert title_similarity(a, a.replace(",", "")) == pytest.approx(1.0)
    paraphrase = title_similarity(
        "5 detained after drugs seized from boat in Arabian Sea",
        "Drugs seized from Iranian boat in Arabian Sea; 5 held",
    )
    assert 0.5 <= paraphrase <= 0.95
    assert title_similarity("CAFE norms tightened", "Scientists vanish after demo") < 0.30


def test_jaccard_basic():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard(set(), {"a"}) == 0.0
    assert 0 < jaccard({"a", "b"}, {"a", "c"}) < 1


def test_extract_article_text_prefers_article_block_and_ignores_chrome():
    html = """
    <html><head><title>Real headline here today</title></head><body>
    <nav>LOGOUT SECTION NEWSLETTERS put the fun in!</nav>
    <article><p>%s</p></article>
    </body></html>
    """ % ("Plain sentence about policy. " * 40)
    text = extract_article_text(html)
    assert "NEWSLETTERS" not in text
    assert "Plain sentence about policy" in text


def test_extract_article_text_handles_div_body_classes():
    html = """
    <html><body>
    <nav>NEWSLETTERS! TOPICS! SECTION!</nav>
    <div class="articlebodycontent col-xl-9"><p>%s</p></div>
    </body></html>
    """ % ("Calm factual reporting sentence. " * 40)
    text = extract_article_text(html)
    assert "NEWSLETTERS" not in text
    assert "Calm factual reporting" in text


def test_extract_article_text_falls_back_to_page_text():
    html = "<html><body><p>Short page without containers. " + "filler " * 60 + "</p></body></html>"
    text = extract_article_text(html)
    assert "Short page" in text


def test_extract_headline_uses_title_tag():
    assert extract_headline("<html><head><title>Hello</title></head></html>") == "Hello"


def test_caps_exclaims_ignores_short_acronyms():
    text = "The CWC met the BJP and CISF officials on Monday. Plain sentence. " * 10
    assert caps_exclaims(text) < 0.1
    shouty = "GET RICH QUICK NOW!!! " * 20
    assert caps_exclaims(shouty) > 0.5


def test_clickbait_phrases_detected():
    hits = phrase_hits("Shocking! You won't believe what happened next", CLICKBAIT_PATTERNS)
    assert len(hits) >= 2


def test_ai_stylometry_flags_template_prose():
    template = ("It is important to note that the report underscores the importance. " * 30)
    styl = ai_stylometry(template)
    assert styl["stock_phrase_count"] >= 2
    assert styl["ai_like"] > 0.4


# --------------------------------------------------------------------------
# cross_check: pure functions
# --------------------------------------------------------------------------

def test_registrable_domain():
    assert _registrable("www.thehindu.com") == "thehindu.com"
    assert _registrable("epaper.thehindu.com") == "thehindu.com"
    assert _registrable("timesofindia.indiatimes.com") == "indiatimes.com"


def test_links_to_trusted_excludes_own_site_including_subdomains():
    html = (
        '<a href="https://www.thehindu.com/self">self</a>'
        '<a href="https://epaper.thehindu.com/x">epaper</a>'
        '<a href="https://www.ndtv.com/other">ndtv</a>'
        '<a href="https://ads.example.com/x">ad</a>'
    )
    links = links_to_trusted(html, "in", page_domain="www.thehindu.com")
    assert links == ["https://www.ndtv.com/other"]


def test_verdict_thresholds():
    assert verdict_from_suspicion(0.0) == "trusted"
    assert verdict_from_suspicion(0.30) == "uncertain"
    assert verdict_from_suspicion(0.50) == "suspicious"
    assert verdict_from_suspicion(0.90) == "fake"


def test_domain_reputation_penalises_lookalikes():
    bad = domain_reputation("truth-leaks-daily.xyz")
    assert bad["weight"] > 0
    good = domain_reputation("www.thehindu.com")
    assert good["weight"] < 0


# --------------------------------------------------------------------------
# pipeline (network mocked)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._payload


RSS_ITEM = """
<rss><channel>
<item><title>{title}</title><link>https://news.google.com/rss/articles/abc</link>
<source>{outlet}</source></item>
</channel></rss>
"""


def test_pipeline_corroborates_real_story(monkeypatch):
    html = (
        "<html><head><title>Rs 3,000 crore drugs seized from Iranian boat</title></head><body>"
        "<article><p>" + "Officials said the seizure happened on Monday. " * 30 + "</p></article>"
        "</body></html>"
    )

    rss = RSS_ITEM.format(
        title="526 kg drugs worth Rs 3,000 crore seized from Iranian boat - India Today",
        outlet="India Today",
    ).encode()

    import urllib.request as _ur

    def fake_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "news.google.com/rss" in url:
            return _FakeResponse(rss)
        raise OSError("unexpected fetch: " + url)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    result = analyze(
        "https://www.thehindu.com/news/national/drugs-boat/article71526673.ece",
        region="in",
        html=html,
        include_cross_check=True,
    )
    assert result.verdict == "trusted"
    assert result.suspicion == pytest.approx(0.0, abs=0.01)
    assert any(m.outlet == "India Today" for m in result.corroborating_sources)
    # The page's own outlet must never appear as corroboration.
    assert all(m.outlet != "The Hindu" for m in result.corroborating_sources)


def test_pipeline_flags_fabricated_story(monkeypatch):
    html = (
        "<html><head><title>Scientists vanish after time machine demo</title></head><body>"
        "<article><p>Shocking! You won't believe what happened next. Reportedly, sources claim "
        "a secret lab demonstrated a working time machine last night. Insiders reveal the truth. "
        + "Officials allegedly denied everything. " * 10
        + "</p></article></body></html>"
    )

    empty_rss = b"<rss><channel></channel></rss>"

    import urllib.request as _ur

    def fake_urlopen(req, timeout=5):
        return _FakeResponse(empty_rss)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    result = analyze(
        "http://truth-leaks-daily.xyz/2026/09/scientists-vanish.html",
        region="in",
        html=html,
        include_cross_check=True,
    )
    assert result.verdict in ("suspicious", "fake")
    assert result.suspicion >= 0.45
    sources = {e.source for e in result.evidence}
    assert "Clickbait / sensationalism" in sources
    assert "Cross-source check" in sources


def test_pipeline_handles_network_failure_gracefully(monkeypatch):
    import urllib.request as _ur

    def fake_urlopen(req, timeout=5):
        raise OSError("network down")

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    result = analyze(
        "https://www.ndtv.com/some/story.html",
        region="in",
        html="<html><head><title>T</title></head><body><article><p>" + "Text. " * 200 + "</p></article></body></html>",
        include_cross_check=True,
    )
    # Must still return a verdict, never raise.
    assert result.verdict in ("trusted", "uncertain", "suspicious", "fake")


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

def test_health_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_analyze_endpoint_contract(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    empty_rss = b"<rss><channel></channel></rss>"
    import urllib.request as _ur

    def fake_urlopen(req, timeout=5):
        return _FakeResponse(empty_rss)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    client = TestClient(app)
    resp = client.post(
        "/analyze",
        json={
            "url": "http://truth-leaks-daily.xyz/x.html",
            "region": "in",
            "headline": "Scientists vanish after time machine demo",
            "html": "<html><head><title>Scientists vanish after time machine demo</title></head>"
            "<body><article><p>" + "Shocking! Reportedly officials allegedly denied. " * 20 + "</p></article></body></html>",
            "include_cross_check": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    for key in ("verdict", "suspicion", "confidence", "evidence", "corroborating_sources", "ai_signals", "summary"):
        assert key in body
    assert body["verdict"] in ("trusted", "uncertain", "suspicious", "fake")
