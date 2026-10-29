# backend/app/retrieval.py
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Callable, Optional

from app.outlets import WIRE_DOMAINS, outlet_key, resolve_outlet, split_title
from app.query_builder import _compose, _extract_terms, _top_nouns, build_query_cascade
from app.rss_fetch import RssItem, fetch_rss

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="veritas-rss")
TOP_DOMAINS = ("thehindu.com", "ndtv.com", "indianexpress.com", "reuters.com", "bbc.com")
_WIRE_TAG = re.compile(r"\b(PTI|ANI|IANS|UNI|Reuters|AFP)\b")
_DATEISH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\b(today|yesterday|tomorrow)\b", re.I)


@dataclass
class Candidate:
    title: str
    link: str
    outlet: str
    domain: str
    published: Optional[float]
    quality: float = 0.40
    is_wire_syndication: bool = False
    wire_agency: Optional[str] = None
    syndication_cluster: int = -1
    independence_weight: float = 1.0


@dataclass
class RetrievalResult:
    candidates: list[Candidate]
    raw_items: int
    distinct_outlets: int
    independent_origins: int
    queries_run: int
    escalated: bool
    early_stopped: bool
    latency_ms: int
    status: str            # "ok" | "insufficient"


# ----------------------------------------------------------------- dedupe (§2)
def annotate_syndication(cands, same_story, group_of, wire_w=0.35, copy_w=0.7, group_w=0.6):
    """One entry per outlet; wire republishers kept but down-weighted, with provenance."""
    best: dict[str, Candidate] = {}
    for c in cands:
        k = c.domain or "name:" + c.outlet.lower()
        cur = best.get(k)
        if cur is None or (c.quality, -(c.published or 9e18)) > (cur.quality, -(cur.published or 9e18)):
            best[k] = c
    uniq = sorted(best.values(), key=lambda c: c.published or 9e18)      # earliest first

    clusters: list[list[Candidate]] = []
    for c in uniq:
        for cl in clusters:
            if same_story(cl[0], c):
                cl.append(c)
                break
        else:
            clusters.append([c])

    seen_groups: dict[str, int] = {}
    for idx, cl in enumerate(clusters):
        origin = next((m for m in cl if m.domain in WIRE_DOMAINS), cl[0])
        agency = WIRE_DOMAINS.get(origin.domain)
        if agency is None:
            for m in cl:
                tag = _WIRE_TAG.search(m.title)
                if tag:
                    agency = tag.group(1)
                    break
        for m in cl:
            m.syndication_cluster = idx
            if m is origin or len(cl) == 1:
                m.independence_weight = 1.0
            else:
                m.is_wire_syndication = agency is not None
                m.wire_agency = agency
                m.independence_weight = wire_w if agency else copy_w
            g = group_of(m.domain) if m.domain else None
            if g:
                n = seen_groups.get(g, 0)
                seen_groups[g] = n + 1
                if n:
                    m.independence_weight *= group_w
    return uniq, len(clusters)


def select_visible(cands, floor, lo, hi):
    ranked = sorted(cands, key=lambda c: c.quality * c.independence_weight, reverse=True)
    good = [c for c in ranked if c.quality >= floor][:hi]
    rest = [c for c in ranked if c.quality < floor]
    return good + rest[: max(0, lo - len(good))]     # backfill shown with Tier 3 badge, weighted by quality


def weighted_support(cands) -> float:
    return sum(c.quality * c.independence_weight for c in cands)


# ------------------------------------------------------------------- planning
def plan_queries(claim, cfg):
    cascade = {t.tier: t.query for t in build_query_cascade(claim)}
    primary = [q for q in (cascade.get("A"), cascade.get("C")) if q]
    for d in TOP_DOMAINS[: cfg.outlet_checks]:
        if len(primary) >= cfg.max_queries:
            break
        b = next((t.query for t in build_query_cascade(claim, d) if t.tier == "B"), None)
        if b and b not in primary:
            primary.append(b)
    return primary[: cfg.max_queries]


def plan_relaxed(claim, exclude):
    t = _extract_terms(_DATEISH.sub(" ", claim))
    nouns = _top_nouns(t.nouns, 3)
    e1 = t.entities[0] if t.entities else ""
    out = []
    for q in (_compose(nouns, max_words=3), _compose([e1, *nouns[:1]], max_words=3)):
        if q and q not in exclude and q not in out:
            out.append(q)
    return out[:2]


# ------------------------------------------------------------------ the loop
class _State:
    def __init__(self, quality_of, keywords, min_hits):
        self.by_outlet: dict[str, Candidate] = {}
        self.raw = 0
        self.q, self.kw, self.min_hits = quality_of, keywords, min_hits

    def _relevant(self, headline: str) -> bool:
        h = headline.lower()
        return sum(k in h for k in self.kw) >= self.min_hits

    def absorb(self, items: list[RssItem]):
        self.raw += len(items)
        for it in items:
            headline, _ = split_title(it.title)
            if not self._relevant(headline):          # relaxed queries must not import unrelated stories
                continue
            o = resolve_outlet(it.source_name, it.source_url, it.title)
            if not (o.domain or o.name):
                continue
            key = outlet_key(o)
            cand = Candidate(headline, it.link, o.name, o.domain, it.published, self.q(o.domain, o.name))
            cur = self.by_outlet.get(key)
            if cur is None or (cand.published or 9e18) < (cur.published or 9e18):
                self.by_outlet[key] = cand

    def good(self, floor):
        return sum(c.quality >= floor for c in self.by_outlet.values())


def adaptive_retrieve(claim: str, cfg, quality_of: Callable[[str, str], float],
                      same_story: Callable, group_of: Callable) -> RetrievalResult:
    t0 = time.monotonic()
    deadline = t0 + getattr(cfg, "retrieval_deadline_s", 2.5)
    terms = _extract_terms(claim)
    kw = {w.lower() for w in (terms.entities + terms.nouns + terms.verbs) for w in w.split() if len(w) > 2}
    state = _State(quality_of, kw, 2 if len(kw) > 2 else 1)
    stats = {"queries": 0, "early": False}

    def wave(queries, base_attempt):
        futs = {_POOL.submit(fetch_rss, q, base_attempt + i): q for i, q in enumerate(queries)}
        stats["queries"] += len(futs)
        try:
            for f in as_completed(futs, timeout=max(0.05, deadline - time.monotonic())):
                state.absorb(f.result())
                if state.good(cfg.retrieval_floor) >= cfg.max_sources:
                    stats["early"] = True              # early-stop: in-flight fetches still warm the cache
                    return
        except FuturesTimeout:
            pass
        finally:
            for f in futs:
                f.cancel()

    primary = plan_queries(claim, cfg)
    wave(primary, 0)
    escalated = False
    if not stats["early"] and len(state.by_outlet) < cfg.min_sources and time.monotonic() < deadline:
        escalated = True
        wave(plan_relaxed(claim, set(primary)), len(primary))

    uniq, origins = annotate_syndication(list(state.by_outlet.values()), same_story, group_of)
    visible = select_visible(uniq, cfg.retrieval_floor, cfg.min_sources, cfg.max_sources)
    return RetrievalResult(
        candidates=visible, raw_items=state.raw, distinct_outlets=len(uniq), independent_origins=origins,
        queries_run=stats["queries"], escalated=escalated, early_stopped=stats["early"],
        latency_ms=int((time.monotonic() - t0) * 1000),
        status="ok" if len(visible) >= cfg.min_sources else "insufficient",
    )
