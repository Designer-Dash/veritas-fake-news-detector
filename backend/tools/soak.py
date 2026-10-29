"""Hour-long extreme-scenario soak test for the Veritas analysis engine.

Runs app.pipeline.analyze() directly (no server, no extension) for a
configurable duration (default 60 minutes) across every failure surface the
pipeline exposes, asserting structural invariants on every single result:

  INV-1  verdict in {trusted, uncertain, suspicious, fake}
  INV-2  0.0 <= suspicion <= 1.0 and 0.0 <= confidence <= 1.0
  INV-3  ai_signals.cross_check.score == 0.0 whenever checked == 0
  INV-4  exactly one Cross-source evidence item, and its weight equals the
         cross_weight reported in the VERITAS_DEBUG payload (ERR-1 invariant)
  INV-5  debug payload exists whenever VERITAS_DEBUG=1 and parses as JSON
  INV-6  corroborating_sources contain no entry from the page's own domain
  INV-7  verification evidence_score (when present) is within 0..100
  INV-8  breaking is False unless the article carried a publication date
  INV-9  result.to_dict() round-trips through json.dumps (API contract)

Chaos retrieval (ChaosRSS) randomizes per query: returning None, b"",
garbage bytes, slow responses, and valid RSS with 0..12 sources whose
pubDates swing from 5 minutes to 30 days old -- including stale pools
behind today-dated URLs (the recycled-coverage trap) and fresh pools
(the true breaking-news case). Pagination loads all permutations of the
scenario bank and re-rotates the chaos seed every loop, so a 1-hour run
covers far more combination space than any hand-written test list.

Usage:
    python tools/soak.py               # 60 minutes
    python tools/soak.py --duration 60 # seconds override
    Ctrl+C prints the partial summary; exit code 1 if any invariant broke.

Stdlib only; signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["VERITAS_DEBUG"] = "1"  # payloads asserted by INV-4b/INV-5/INV-7

import app.pipeline as pl  # noqa: E402
from app.cache import clear_caches  # noqa: E402
from app.pipeline import SourceRetriever, analyze  # noqa: E402

logger = logging.getLogger("veritas.soak")

TRUSTED_DOMAINS = (
    "reuters.com", "apnews.com", "bbc.com", "thehindu.com", "indiatoday.in",
    "hindustantimes.com", "ndtv.com", "indianexpress.com", "timesofindia.indiatimes.com",
)
OUTLET_NAMES = (
    "Reuters", "Associated Press", "BBC News", "The Hindu", "India Today",
    "Hindustan Times", "NDTV", "The Times of India", "Indian Express",
)

SUPPORT_TITLES = (
    "Rs 3,000 crore drugs worth 526 kg seized from Iranian boat, say officials",
    "526 kg of drugs seized from Iranian boat in Arabian Sea operation",
    "Officials confirm Rs 3,000 crore drug seizure from Iranian fishing boat",
)
CONTRADICT_TITLES = (
    "Fact-check: no drugs seized from any Iranian boat, claim is false",
    "Coast guard denies reports of a 3,000 crore drug haul from Iranian boat",
)
UNKNOWN_TITLES = (
    "Iranian boat crew questioned at Mumbai port in a separate customs case",
    "Fishing community in Gujarat protests fuel price rise",
)


class ChaosRSS:
    """Fetch_func standing in for the network: returns garbage, nothing, or
    an RSS page built from the scenario pool, at random. Tracks every
    anomaly it injects so the report can show what the engine survived."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.stats = {
            "none": 0, "empty": 0, "garbage": 0, "slow": 0, "rss": 0,
            "slowest_ms": 0.0,
        }

    def _rss(self, n_sources: int, age_minutes: Optional[int]) -> bytes:
        items = []
        for i in range(n_sources):
            kind = self.rng.random()
            if kind < 0.55:
                title = self.rng.choice(SUPPORT_TITLES)
            elif kind < 0.75:
                title = self.rng.choice(CONTRADICT_TITLES)
            else:
                title = self.rng.choice(UNKNOWN_TITLES)
            if self.rng.random() < 0.5:
                title = f"{title} - {self.rng.choice(OUTLET_NAMES)}"
            outlet = self.rng.choice(OUTLET_NAMES)
            pub = ""
            if age_minutes is not None:
                dt = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
                pub = f"<pubDate>{dt.strftime('%d %b %Y %H:%M:%S GMT')}</pubDate>"
            items.append(
                f"<item><title>{title}</title>"
                f"<link>https://news.google.com/rss/articles/{i}</link>"
                f"<source>{outlet}</source>{pub}</item>"
            )
        return f"<rss><channel>{''.join(items)}</channel></rss>".encode()

    def __call__(self, url: str, **kwargs) -> Optional[bytes]:
        r = self.rng.random()
        if r < 0.08:
            self.stats["none"] += 1
            return None                      # network failure
        if r < 0.14:
            self.stats["empty"] += 1
            return b""                       # empty body
        if r < 0.19:
            self.stats["garbage"] += 1
            return b"<rss><channel><item><title>\xff\xfe garbage no close"
        if r < 0.24:
            self.stats["slow"] += 1
            delay = self.rng.uniform(0.05, 0.30)
            self.stats["slowest_ms"] = max(self.stats["slowest_ms"], delay * 1000)
            time.sleep(delay)                # slow endpoint
        self.stats["rss"] += 1
        n = self.rng.choice([0, 1, 2, 3, 5, 8, 12])
        age = self.rng.choice([5, 30, 60, 90, 240, 1440, 43200, 43200, None])
        return self._rss(n, age)


class ScenarioBank:
    """Every URL x article x retrieval-mode combination the extension can
    produce. Iterated as full permutations so an hour touches them all."""

    def __init__(self):
        self.urls = [
            ("https://city-voice.in/2026/10/01/drugs-boat.html", "city-voice.in"),
            ("https://city-voice.in/drugs-boat.html", "city-voice.in"),
            ("https://daily-hindu.blogspot.com/2026/10/d scoop.html", "daily-hindu.blogspot.com"),
            ("https://www.thehindu.com/news/national/article71526673.ece", "thehindu.com"),
            ("https://truth-leaks-daily.xyz/2026/09/scientists-vanish.html", "truth-leaks-daily.xyz"),
            ("https://ndtv.com", "ndtv.com"),
        ]
        self.articles = [
            (
                "Rs 3,000 crore drugs seized from Iranian boat",
                "<article><p>At least 526 kg of drugs worth Rs 3,000 crore was seized "
                "from an Iranian boat, officials said. " + "Rescue work is going on. " * 12 + "</p></article>",
            ),
            (
                "Scientists vanish after time machine demo",
                "<article><p>Shocking! You won't believe this. Reportedly, insiders "
                "reveal a secret lab demo. " + "Officials allegedly denied everything. " * 12 + "</p></article>",
            ),
            (
                "Weather update: heavy rainfall expected across Kerala this week",
                "<article><p>" + "The forecast was issued on Monday. " * 15 + "</p></article>",
            ),
        ]
        self.links = [
            "",
            '<a href="https://www.ndtv.com/another-story">see also</a>',
            '<a href="https://www.ndtv.com/a">a</a><a href="https://www.reuters.com/b">b</a>'
            '<a href="https://www.thehindu.com/c">c</a>',
        ]

    def __iter__(self):
        for (url, dom), (headline, body), links in itertools.product(
            self.urls, self.articles, self.links
        ):
            yield url, dom, headline, f"<html><head><title>{headline}</title></head><body>{body}{links}</body></html>"


class Soak:
    def __init__(self, duration_s: float, seed: int):
        self.duration = duration_s
        self.seed = seed
        self.iterations = 0
        self.violations: List[str] = []
        self.verdicts = {"trusted": 0, "uncertain": 0, "suspicious": 0, "fake": 0}
        self.exceptions: Dict[str, int] = {}
        self.chaos_totals: Dict[str, float] = {
            "none": 0, "empty": 0, "garbage": 0, "slow": 0, "rss": 0, "slowest_ms": 0.0,
        }
        self._log_records: List[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)  # type: ignore[attr-defined]

        self._handler = _Collect()
        self._handler.records = self._log_records  # type: ignore[attr-defined]
        logging.getLogger("veritas.pipeline").addHandler(self._handler)
        logging.getLogger("veritas.pipeline").setLevel(logging.DEBUG)

    # -- invariants ---------------------------------------------------------

    def _payloads(self) -> List[Dict[str, Any]]:
        out = []
        for rec in self._log_records:
            msg = rec.getMessage()
            if "debug_payload: " in msg:
                try:
                    out.append(json.loads(msg.split("debug_payload: ", 1)[1]))
                except Exception as exc:
                    self.violations.append(f"INV-5 payload not JSON: {exc}")
        return out

    def check(self, result, url: str, dom: str, chaos: ChaosRSS) -> None:
        n = self.iterations
        if result.verdict not in self.verdicts:
            self.violations.append(f"INV-1 #{n} bad verdict {result.verdict!r} ({url})")
        if not (0.0 <= result.suspicion <= 1.0):
            self.violations.append(f"INV-2 #{n} suspicion {result.suspicion} ({url})")
        if not (0.0 <= result.confidence <= 1.0):
            self.violations.append(f"INV-2 #{n} confidence {result.confidence} ({url})")
        cc = result.ai_signals.get("cross_check", {})
        if cc.get("checked", 0) == 0 and cc.get("score") != 0.0:
            self.violations.append(f"INV-3 #{n} score={cc.get('score')} with checked=0 ({url})")
        cross = [e for e in result.evidence if e.source == "Cross-source check"]
        if len(cross) != 1:
            self.violations.append(f"INV-4a #{n} {len(cross)} cross-source evidence items ({url})")
        payloads = self._payloads()
        if len(payloads) != 1:
            self.violations.append(f"INV-5a #{n} {len(payloads)} debug payloads")
        else:
            p = payloads[0]
            if cross and p.get("cross_weight") is not None:
                if abs(cross[0].weight - p["cross_weight"]) > 1e-9:
                    self.violations.append(
                        f"INV-4b #{n} evidence weight {cross[0].weight} != cross_weight {p['cross_weight']} ({url})"
                    )
            v = p.get("evidence") or {}
            if v.get("E") is not None and not (0 <= v["E"] <= 100):
                self.violations.append(f"INV-7 #{n} evidence score {v['E']} out of range ({url})")
        for m in result.corroborating_sources:
            own = dom.split(".")[-2] if dom.count(".") >= 1 else dom
            if own and own in m.outlet.lower():
                self.violations.append(f"INV-6 #{n} own-outlet source {m.outlet} ({url})")
        vfull = result.ai_signals.get("verification") or {}
        if vfull.get("breaking") and not vfull.get("breaking_info", {}).get("published"):
            self.violations.append(f"INV-8 #{n} breaking without a published date ({url})")
        try:
            json.dumps(result.to_dict(), default=str)
        except Exception as exc:
            self.violations.append(f"INV-9 #{n} to_dict not serializable: {exc} ({url})")

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        deadline = time.monotonic() + self.duration
        bank = ScenarioBank()
        combos = len(list(bank))
        print(f"soak: {self.duration:.0f}s, scenario bank={combos} combos", flush=True)
        started = time.monotonic()
        last_report = started
        while time.monotonic() < deadline:
            rng = random.Random(self.seed + self.iterations // combos)
            chaos = ChaosRSS(rng)
            original_fetch = pl.SourceRetriever.fetch
            pl.SourceRetriever.fetch = staticmethod(chaos)
            try:
                for url, dom, headline, html in bank:
                    if time.monotonic() >= deadline:
                        break
                    self._log_records.clear()
                    try:
                        # Alternate cache discipline: most iterations run the
                        # full pipeline against fresh chaos (caches cleared);
                        # every 7th keeps caches warm so the cache-hit path
                        # and its freshness fallback are exercised too.
                        if self.iterations % 7 != 0:
                            clear_caches()
                        result = analyze(url, region="in", html=html)
                        self.check(result, url, dom, chaos)
                        self.verdicts[result.verdict] += 1
                    except Exception as exc:
                        key = f"{type(exc).__name__}: {str(exc)[:80]}"
                        self.exceptions[key] = self.exceptions.get(key, 0) + 1
                        if self.exceptions[key] <= 3:
                            import traceback
                            self.violations.append(
                                f"EXC #{self.iterations} {key} ({url})\n"
                                + traceback.format_exc(limit=3)
                            )
                    self.iterations += 1
            finally:
                pl.SourceRetriever.fetch = original_fetch
            # Merge this rotation's chaos stats into the run totals.
            for k, v in chaos.stats.items():
                if k == "slowest_ms":
                    self.chaos_totals[k] = max(self.chaos_totals[k], v)
                else:
                    self.chaos_totals[k] += v
            elapsed = time.monotonic() - started
            # Progress on a time gate (every 15s) or at end of run -- never
            # keyed on iteration counts, which only line up by luck.
            if elapsed - (last_report - started) >= 15.0 or time.monotonic() >= deadline:
                last_report = time.monotonic()
                print(
                    f"  [{elapsed:6.0f}s] it={self.iterations} "
                    f"v={self.verdicts} chaos={self.chaos_totals} "
                    f"exc={sum(self.exceptions.values())}",
                    flush=True,
                )
        return 1 if self.violations else 0

    def report(self) -> None:
        print("\n" + "=" * 62)
        print(f"SOAK REPORT  iterations={self.iterations}")
        print(f"verdicts:    {self.verdicts}")
        print(f"chaos:       {self.chaos_totals}")
        print(f"exceptions:  {self.exceptions or 'none'}")
        if self.violations:
            print(f"VIOLATIONS:  {len(self.violations)}")
            for v in self.violations[:20]:
                print("  -", v)
        else:
            print("VIOLATIONS:  none -- all invariants held")


def main() -> int:
    ap = argparse.ArgumentParser(description="Veritas extreme-scenario soak test")
    ap.add_argument("--duration", type=float, default=3600.0, help="seconds to run")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args()

    soak = Soak(args.duration, args.seed)
    import signal

    def _stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _stop)
    code = 0
    try:
        code = soak.run()
    except KeyboardInterrupt:
        print("\ninterrupted -- partial summary below", flush=True)
        code = 1 if soak.violations else 0
    soak.report()
    return code


if __name__ == "__main__":
    sys.exit(main())
