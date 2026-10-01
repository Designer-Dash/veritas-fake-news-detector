"""Deterministic tests: analysis pipeline, cross-check scoring, API contract,
and security regressions (SSRF, message validation, fail-closed behavior).

Network-touching code paths are mocked at the security-layer boundary
(`safe_fetch`) so the suite runs offline and fast.
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
from app.security import SecurityError, SlidingWindowLimiter, validate_url  # noqa: E402
from app.text_analysis import (  # noqa: E402
    CLICKBAIT_PATTERNS,
    ai_stylometry,
    caps_exclaims,
    extract_article_text,
    extract_headline,
    phrase_hits,
)


# --------------------------------------------------------------------------
# text_analysis
# --------------------------------------------------------------------------

def test_normalize_text_keeps_digits_drops_punctuation():
    assert _normalize_text("₹3,000 crore!") == " 3000 crore "
    assert "2026" in _normalize_text("dated 2026-09-30")


def test_strip_source_suffix_removes_outlet_tail():
    assert _strip_source_suffix("Big story about boats - The Hindu") == "Big story about boats"
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


def test_extract_article_text_prefers_article_block_and_ignores_chrome():
    html = "<nav>LOGOUT SECTION NEWSLETTERS put the fun in!</nav><article><p>%s</p></article>" % (
        "Plain sentence about policy. " * 40
    )
    text = extract_article_text(html)
    assert "NEWSLETTERS" not in text
    assert "Plain sentence about policy" in text


def test_extract_article_text_handles_div_body_classes():
    html = '<nav>NEWSLETTERS! TOPICS!</nav><div class="articlebodycontent col-xl-9"><p>%s</p></div>' % (
        "Calm factual reporting sentence. " * 40
    )
    text = extract_article_text(html)
    assert "NEWSLETTERS" not in text
    assert "Calm factual reporting" in text


def test_caps_exclaims_ignores_short_acronyms():
    assert caps_exclaims("The CWC met the BJP and CISF officials. Plain sentence. " * 10) < 0.1
    assert caps_exclaims("GET RICH QUICK NOW!!! " * 20) > 0.5


def test_clickbait_phrases_detected():
    assert len(phrase_hits("Shocking! You won't believe what happened next", CLICKBAIT_PATTERNS)) >= 2


def test_ai_stylometry_flags_template_prose():
    styl = ai_stylometry("It is important to note that the report underscores the importance. " * 30)
    assert styl["stock_phrase_count"] >= 2
    assert styl["ai_like"] > 0.4


# --------------------------------------------------------------------------
# cross_check: pure functions
# --------------------------------------------------------------------------

def test_registrable_domain():
    assert _registrable("www.thehindu.com") == "thehindu.com"
    assert _registrable("epaper.thehindu.com") == "thehindu.com"


def test_links_to_trusted_excludes_own_site_including_subdomains():
    html = (
        '<a href="https://www.thehindu.com/self">self</a>'
        '<a href="https://epaper.thehindu.com/x">epaper</a>'
        '<a href="https://www.ndtv.com/other">ndtv</a>'
        '<a href="https://ads.example.com/x">ad</a>'
    )
    assert links_to_trusted(html, "in", page_domain="www.thehindu.com") == ["https://www.ndtv.com/other"]


def test_verdict_thresholds():
    assert verdict_from_suspicion(0.0) == "trusted"
    assert verdict_from_suspicion(0.30) == "uncertain"
    assert verdict_from_suspicion(0.50) == "suspicious"
    assert verdict_from_suspicion(0.90) == "fake"


def test_domain_reputation_penalises_lookalikes():
    assert domain_reputation("truth-leaks-daily.xyz")["weight"] > 0
    assert domain_reputation("www.thehindu.com")["weight"] < 0


# --------------------------------------------------------------------------
# SECURITY: URL validation (SSRF) — fail closed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata (http + link-local)
        "http://127.0.0.1:8000/health",  # loopback
        "http://192.168.1.10/router",  # RFC1918
        "http://10.0.0.5/internal",  # RFC1918
        "https://user:pass@example.com/",  # userinfo
        "https://example.com:8080/",  # nonstandard port
        "",  # empty
        "x" * 3000,  # oversized
    ],
)
def test_validate_url_rejects_dangerous_urls(url):
    with pytest.raises(SecurityError):
        validate_url(url)


def test_validate_url_accepts_public_https():
    assert validate_url("https://www.thehindu.com/news/x", resolve=False) == (
        "https://www.thehindu.com/news/x"
    )


def test_safe_fetch_returns_none_for_ssrf_attempts(monkeypatch):
    import urllib.request

    def boom(*a, **k):  # any network attempt is a test failure
        raise AssertionError("network must not be touched for invalid URLs")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    from app.security import safe_fetch

    assert safe_fetch("http://169.254.169.254/latest/meta-data") is None
    assert safe_fetch("javascript:alert(1)") is None
    assert safe_fetch("http://127.0.0.1:8000/health") is None


def test_rate_limiter_blocks_burst_and_releases():
    lim = SlidingWindowLimiter(max_requests=3, window_seconds=60, max_concurrent=2)
    assert lim.check("c1")[0] is True  # 1 in flight
    assert lim.check("c1")[0] is True  # 2 in flight
    ok, _ = lim.check("c1")
    assert ok is False  # concurrency cap
    lim.release("c1")
    assert lim.check("c1")[0] is True  # slot freed
    lim.release("c1")
    lim.release("c1")
    ok, retry = lim.check("c1")
    assert ok is False and retry > 0  # rate cap reached for this client
    assert lim.check("other")[0] is True  # other clients unaffected


# --------------------------------------------------------------------------
# pipeline (network mocked at the security boundary)
# --------------------------------------------------------------------------

RSS_ITEM = """
<rss><channel>
<item><title>{title}</title><link>https://news.google.com/rss/articles/abc</link>
<source>{outlet}</source></item>
</channel></rss>
"""


def _mock_safe_fetch(monkeypatch, rss_by_query):
    """Canned RSS responder at the single retrieval boundary
    (SourceRetriever.fetch) -- one patch, no per-module safe_fetch."""

    def fake_fetch(url, **kwargs):
        for needle, body in rss_by_query.items():
            if needle in url:
                return body
        return b"" if "news.google.com" in url else None

    import app.pipeline as pl

    monkeypatch.setattr(pl.SourceRetriever, "fetch", fake_fetch)


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
    _mock_safe_fetch(monkeypatch, {"news.google.com": rss})

    result = analyze(
        "https://www.thehindu.com/news/national/drugs-boat/article71526673.ece",
        region="in",
        html=html,
        include_cross_check=True,
    )
    assert result.verdict == "trusted"
    assert result.suspicion == pytest.approx(0.0, abs=0.01)
    assert any(m.outlet == "India Today" for m in result.corroborating_sources)
    assert all(m.outlet != "The Hindu" for m in result.corroborating_sources)


def test_pipeline_flags_fabricated_story(monkeypatch):
    html = (
        "<html><head><title>Scientists vanish after time machine demo</title></head><body>"
        "<article><p>Shocking! You won't believe what happened next. Reportedly, sources claim "
        "a secret lab demonstrated a working time machine last night. Insiders reveal the truth. "
        + "Officials allegedly denied everything. " * 10
        + "</p></article></body></html>"
    )
    _mock_safe_fetch(monkeypatch, {"news.google.com": b"<rss><channel></channel></rss>"})

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
    import app.pipeline as pl

    def dead_fetch(url, **kwargs):
        return None  # security layer contract: failures come back as None

    monkeypatch.setattr(pl.SourceRetriever, "fetch", dead_fetch)
    monkeypatch.setattr(pl, "safe_fetch", dead_fetch)  # page fetch, if any
    result = analyze(
        "https://www.ndtv.com/some/story.html",
        region="in",
        html="<html><head><title>T</title></head><body><article><p>" + "Text. " * 200 + "</p></article></body></html>",
        include_cross_check=True,
    )
    assert result.verdict in ("trusted", "uncertain", "suspicious", "fake")


def test_pipeline_rejects_non_http_schemes():
    with pytest.raises(SecurityError):
        analyze("javascript:alert(1)", region="in", html="<p>x</p>", include_cross_check=False)
    with pytest.raises(SecurityError):
        analyze("file:///etc/passwd", region="in", html="<p>x</p>", include_cross_check=False)


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _api_auth_off(monkeypatch):
    """API-contract tests run in dev mode (auth off); auth has its own suite
    in test_auth.py which enables it explicitly."""
    monkeypatch.setenv("VERITAS_AUTH_DISABLED", "1")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_analyze_endpoint_contract(client, monkeypatch):
    _mock_safe_fetch(monkeypatch, {"news.google.com": b"<rss><channel></channel></rss>"})
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


def test_analyze_rejects_bad_scheme_and_region(client):
    assert client.post("/analyze", json={"url": "javascript:alert(1)", "region": "in"}).status_code in (400, 422)
    assert client.post("/analyze", json={"url": "https://example.com/a", "region": "mars"}).status_code == 422
    assert client.post("/analyze", json={"url": "https://example.com/a b", "region": "in"}).status_code in (400, 422)


def test_analyze_rejects_oversized_html(client):
    resp = client.post(
        "/analyze",
        json={"url": "https://example.com/a", "region": "in", "html": "x" * 500_000},
    )
    assert resp.status_code == 422  # pydantic bounds, fail closed


def test_analyze_fails_closed_on_ssrf_url(client):
    # Public-shaped request, but URL points at metadata IP: security layer
    # must refuse; API must answer with a generic 400, not an analysis.
    resp = client.post("/analyze", json={"url": "http://169.254.169.254/latest/meta-data", "region": "in", "include_cross_check": False})
    assert resp.status_code in (400, 422, 502)
    assert "169.254" not in resp.text
