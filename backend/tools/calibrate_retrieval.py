# backend/tools/calibrate_retrieval.py
"""Live-network calibration. Not for CI/pytest. Usage:
   python -m tools.calibrate_retrieval [--sweep] [--pause 0.3] [--json out.json]"""
from __future__ import annotations

import argparse, contextlib, itertools, json, statistics, time

from app.config import settings
from app.cross_check import retrieve_reference_sources
from app.rss_fetch import clear_cache

# Refresh the "news" items before each run; live coverage drifts with the news cycle.
DATASET = [
    ("politics", "Parliament passes new telecom bill amid opposition walkout"),
    ("politics", "Supreme Court hearing on electoral bonds verdict compliance"),
    ("politics", "Cabinet approves new semiconductor manufacturing incentive scheme"),
    ("politics", "State assembly election results announced for major southern state"),
    ("international", "UN Security Council votes on Gaza ceasefire resolution"),
    ("international", "European Union agrees new sanctions package on Russia"),
    ("international", "Earthquake strikes northern Japan, tsunami warning issued"),
    ("international", "G20 leaders summit ends with joint declaration on climate"),
    ("business", "RBI keeps repo rate unchanged in latest monetary policy"),
    ("business", "Sensex and Nifty close at record high on foreign inflows"),
    ("business", "Tata Motors reports quarterly profit rise on electric vehicle sales"),
    ("sports", "India beat Australia in final cricket test series decider"),
    ("sports", "FIFA announces host cities for upcoming World Cup"),
    ("science", "ISRO successfully launches earth observation satellite"),
    ("science", "WHO declares new health emergency over disease outbreak"),
    ("viral", "Viral post claims government will ban 500 rupee notes next month"),
    ("viral", "Video claims celebrity died in helicopter crash this morning"),
    ("viral", "Forward claims free laptops distributed to all students under new scheme"),
    ("hoax", "WhatsApp will start charging users for messages"),
    ("hoax", "5G towers spread coronavirus claim"),
]


@contextlib.contextmanager
def override(**kw):
    old = {k: getattr(settings, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


def run_one(category, claim):
    t0 = time.perf_counter()
    res = retrieve_reference_sources(claim)
    ms = int((time.perf_counter() - t0) * 1000)
    cands = getattr(res, "candidates", res)
    return dict(
        category=category, claim=claim, final=len(cands), latency_ms=ms,
        raw=getattr(res, "raw_items", -1), distinct=getattr(res, "distinct_outlets", -1),
        origins=getattr(res, "independent_origins", -1), escalated=getattr(res, "escalated", None),
        ok=settings.min_sources <= len(cands) <= settings.max_sources,
    )


def run_all(pause):
    rows = []
    for cat, claim in DATASET:
        rows.append(run_one(cat, claim))
        time.sleep(pause)                       # be polite to the RSS providers
    return rows


def report(rows, label=""):
    n = len(rows)
    rate = 100 * sum(r["ok"] for r in rows) / n
    lat = sorted(r["latency_ms"] for r in rows)
    print(f"\n=== {label or 'current config'} ===")
    print(f"{'cat':13} {'raw':>4} {'dist':>4} {'orig':>4} {'final':>5} {'ms':>6}  ok  claim")
    for r in rows:
        print(f"{r['category']:13} {r['raw']:>4} {r['distinct']:>4} {r['origins']:>4} {r['final']:>5} "
              f"{r['latency_ms']:>6}  {'Y' if r['ok'] else 'N'}   {r['claim'][:50]}")
    print(f"success (5..10): {rate:.0f}%   p50={statistics.median(lat)}ms   p95={lat[int(0.95 * (n - 1))]}ms")
    return rate


def diagnose(rows):
    fails = [r for r in rows if not r["ok"]]
    if not fails:
        return print("No failures.")
    starved = [r for r in fails if 0 <= r["raw"] < 15]
    narrowed = [r for r in fails if r["raw"] >= 15 and 0 <= r["distinct"] < settings.min_sources]
    deduped = [r for r in fails if r["distinct"] >= settings.min_sources and r["final"] < settings.min_sources]
    over = [r for r in fails if r["final"] > settings.max_sources]
    slow = [r for r in rows if r["latency_ms"] > 2500]
    print("\nDiagnosis:")
    if starved:
        print(f"- {len(starved)} fails had <15 raw items: queries too narrow -> raise max_queries/outlet_checks "
              f"or inspect query_builder output for those claims.")
    if narrowed:
        print(f"- {len(narrowed)} fails had plenty of items but few distinct outlets: relevance gate or outlet "
              f"resolution is dropping them -> extend the registry / loosen the keyword gate.")
    if deduped:
        print(f"- {len(deduped)} fails lost outlets after distinct>=min: retrieval_floor too high, or dedupe still merging.")
    if over:
        print(f"- {len(over)} results exceeded max_sources: the cap is not applied; that is a bug.")
    if slow:
        print(f"- {len(slow)} runs exceeded 2.5s: lower outlet_checks/max_queries or retrieval_deadline_s.")
    print("- min_sources is a quality contract, not a tuning knob: do not lower it to hit the target.")


def sweep(pause):
    best = None
    for floor, mq, oc in itertools.product((0.40, 0.30, 0.20), (3, 4), (3, 6)):
        clear_cache()
        with override(retrieval_floor=floor, max_queries=mq, outlet_checks=oc):
            rows = run_all(pause)
            rate = report(rows, f"floor={floor} max_queries={mq} outlet_checks={oc}")
        p95 = sorted(r["latency_ms"] for r in rows)[int(0.95 * (len(rows) - 1))]
        if rate >= 95 and (best is None or p95 < best[0]):
            best = (p95, floor, mq, oc)
    print("\nBest config reaching >=95%:", best or "none; see diagnosis for the current config")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.sweep:
        sweep(a.pause)
    else:
        rows = run_all(a.pause)
        report(rows)
        diagnose(rows)
        if a.json:
            json.dump(rows, open(a.json, "w"), indent=2)
