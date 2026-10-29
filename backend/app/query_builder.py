# backend/app/query_builder.py  (import from cross_check.py and claim_analysis.py)
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse

MAX_WORDS = 5  # hard cap per query, counting the site: operator as one word

_STOP = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for", "from",
    "by", "with", "as", "is", "are", "was", "were", "be", "been", "being", "has", "have",
    "had", "will", "would", "can", "could", "should", "may", "might", "that", "this",
    "these", "those", "it", "its", "their", "his", "her", "he", "she", "they", "them",
    "after", "before", "over", "into", "about", "against", "says", "said", "claims",
    "claim", "reports", "reported", "report", "new", "also", "than", "then", "there",
    "which", "who", "whom", "what", "when", "where", "while", "amid", "per", "via",
    "breaking", "viral", "fake", "true", "false", "news", "video", "post", "shared",
}
_TITLES = {
    "mr", "mrs", "ms", "dr", "prof", "president", "minister", "prime", "chief", "sir",
    "senator", "governor", "mayor", "ceo", "chairman", "secretary", "union", "former",
    "pm", "cm", "gen", "general", "justice", "judge",
}
_ORG_HINTS = {
    "ministry", "department", "bank", "university", "institute", "council", "commission",
    "party", "court", "committee", "association", "agency", "authority", "corporation",
    "company", "inc", "ltd", "group", "forum", "union", "league", "army", "police",
    "government", "congress", "parliament", "organisation", "organization", "board",
}
_CONNECTORS = {"of", "the", "for", "and", "de", "von", "van"}

# Base forms of verbs that usually carry a news claim; inflections are matched in code.
_ACTION_VERBS = {
    "announce", "launch", "ban", "arrest", "sign", "approve", "reject", "resign", "win",
    "lose", "die", "kill", "attack", "invest", "acquire", "buy", "sell", "merge", "cancel",
    "suspend", "raise", "cut", "cap", "hike", "reveal", "confirm", "deny", "sue", "charge",
    "convict", "release", "open", "close", "build", "start", "end", "visit", "meet",
    "elect", "appoint", "fire", "hire", "promise", "pass", "block", "declare", "impose",
    "lift", "recall", "hack", "leak", "fund", "reduce", "increase", "order", "warn",
    "withdraw", "join", "quit", "shut", "scrap", "unveil", "evacuate", "crash", "collapse",
}


def _verb_base(token: str) -> Optional[str]:
    t = token.lower()
    cands = [t, t[:-1], t[:-2], t[:-3], t[:-3] + "e", t[:-1] + "e", t[:-2] + "e"]
    cands += [c[:-1] for c in list(cands) if len(c) > 2 and c[-1] == c[-2]]  # banned -> ban
    for c in cands:
        if c in _ACTION_VERBS:
            return c
    return None


# ----------------------------------------------------------------------------- NLP
@lru_cache(maxsize=1)
def _nlp():
    try:
        import spacy
        return spacy.load("en_core_web_sm")
    except Exception:
        return None


@dataclass
class ClaimTerms:
    entities: list[str] = field(default_factory=list)  # ordered, compressed, deduped
    verbs: list[str] = field(default_factory=list)
    nouns: list[str] = field(default_factory=list)


def _clean_word(w: str) -> str:
    return re.sub(r"[^\w\-]", "", w.replace("’", "").replace("'", ""), flags=re.UNICODE)


def _compress_entity(text: str, label: Optional[str] = None) -> str:
    toks = [_clean_word(t) for t in text.split()]
    toks = [t for t in toks if t]
    while toks and toks[0].lower() in (_TITLES | _STOP):
        toks.pop(0)
    toks = [t for t in toks if t.lower() not in _CONNECTORS]
    if not toks:
        return ""
    if len(toks) == 1 or all(t.isupper() for t in toks):
        return toks[0] if len(toks) == 1 else " ".join(toks[:2])
    is_org = label in {"ORG", "FAC", "EVENT"} or any(t.lower() in _ORG_HINTS for t in toks)
    if label == "PERSON" or (label is None and not is_org and len(toks) <= 3):
        return toks[-1]                      # "Narendra Modi" -> "Modi"
    return " ".join(toks[:2])                # "Reserve Bank India" -> "Reserve Bank"


def _extract_terms(claim: str) -> ClaimTerms:
    claim = re.sub(r"https?://\S+", " ", claim)
    claim = re.sub(r"\s+", " ", claim).strip()
    nlp = _nlp()
    terms = ClaimTerms()
    ent_words: set[str] = set()

    def add_entity(e: str) -> None:
        if e and e.lower() not in {x.lower() for x in terms.entities} and e.lower() not in _STOP:
            terms.entities.append(e)
            ent_words.update(w.lower() for w in e.split())

    if nlp is not None:
        doc = nlp(claim)
        for ent in doc.ents:
            if ent.label_ in {"PERSON", "ORG", "GPE", "LOC", "FAC", "EVENT", "NORP"}:
                add_entity(_compress_entity(ent.text, ent.label_))
        for tok in doc:
            lemma = tok.lemma_.lower()
            if tok.pos_ == "VERB" and not tok.is_stop and len(lemma) > 2:
                terms.verbs.append(lemma if lemma in _ACTION_VERBS else lemma)
        ordered_candidates = [
            _clean_word(t.text) for t in doc
            if t.pos_ in {"NOUN", "PROPN"} and not t.ent_type_
        ]
    else:
        for m in re.finditer(r"\b(?:[A-Z][\w'’\-]*|[A-Z]{2,})(?:\s+(?:of\s+|the\s+)?[A-Z][\w'’\-]*)*", claim):
            add_entity(_compress_entity(m.group(0)))
        ordered_candidates = re.findall(r"[A-Za-z][A-Za-z\-]+", claim)

    # Prefer known news-action verbs; fall back to whatever verbs spaCy found.
    lex = [b for w in re.findall(r"[A-Za-z]+", claim) if (b := _verb_base(w))]
    terms.verbs = list(dict.fromkeys(lex + terms.verbs))

    seen = set()
    for w in ordered_candidates:
        lw = w.lower()
        if (len(lw) > 3 and lw not in _STOP and lw not in ent_words and lw not in seen
                and not any(c.isdigit() for c in lw) and not _verb_base(lw)):
            seen.add(lw)
            terms.nouns.append(w)
    return terms


# ------------------------------------------------------------------------ assembly
def _normalize_domain(domain: str) -> str:
    d = domain.strip().lower()
    if "//" in d:
        d = urlparse(d).netloc or d
    d = d.split("/")[0]
    return d[4:] if d.startswith("www.") else d


def _compose(parts: list[str], site: Optional[str] = None, max_words: int = MAX_WORDS) -> str:
    """Join parts into an unquoted query; whole parts only; <= max_words incl. site:."""
    budget = max_words - (1 if site else 0)
    words: list[str] = []
    seen: set[str] = set()
    for part in parts:
        new = [w for w in (_clean_word(x) for x in part.split()) if w and w.lower() not in seen]
        if not new or len(words) + len(new) > budget:
            continue
        words.extend(new)
        seen.update(w.lower() for w in new)
    if site:
        words.append(f"site:{site}")
    return " ".join(words)


def _top_nouns(nouns: list[str], k: int) -> list[str]:
    """k most distinctive (longest) nouns, kept in claim order."""
    idx = sorted(range(len(nouns)), key=lambda i: (-len(nouns[i]), i))[:k]
    return [nouns[i] for i in sorted(idx)]


@dataclass
class QueryTier:
    tier: str   # "A" | "B" | "C"
    query: str


def build_query_cascade(claim: str, domain: Optional[str] = None) -> list[QueryTier]:
    """
    Tier A: Entity1 + Entity2 + key action      -> "Modi Gujarat announce"
    Tier B: Entity/noun phrase + site:domain    -> "Modi semiconductor site:thehindu.com"
    Tier C: broad topic fallback (2-3 keywords) -> "semiconductor plant Gujarat"
    Tiers are deduped; B is omitted when no domain is given.
    """
    t = _extract_terms(claim)
    e1 = t.entities[0] if t.entities else ""
    e2 = t.entities[1] if len(t.entities) > 1 else ""
    verb = t.verbs[0] if t.verbs else ""
    top = _top_nouns(t.nouns, 2)

    tier_a = _compose([e1, e2, verb] if e2 else [e1, *top[:1], verb])
    tier_c = _compose(top + ([e2 or e1] if len(top) < 3 else []), max_words=3)

    out: list[QueryTier] = []
    if domain:
        site = _normalize_domain(domain)
        tier_b = _compose([e1, *top[:2]] if e1 else top[:2], site=site, max_words=4)
        if tier_b != f"site:{site}":
            out.append(QueryTier("B", tier_b))
    for tier, q in (("A", tier_a), ("C", tier_c)):
        if q and q not in {x.query for x in out}:
            out.append(QueryTier(tier, q))
    # last-resort so we never return an empty list
    if not out:
        fallback = _compose(re.findall(r"[A-Za-z]{4,}", claim)[:3], max_words=3)
        out.append(QueryTier("C", fallback))
    return out


# ------------------------------------------------- drop-in replacements for callers
def _outlet_search_query(claim: str, domain: str) -> str:
    """Backward-compatible: single targeted site query, no quotes."""
    for tq in build_query_cascade(claim, domain):
        if tq.tier == "B":
            return tq.query
    cascade = build_query_cascade(claim, domain)
    return cascade[0].query if cascade else f"site:{_normalize_domain(domain)}"


def _broad_search_queries(claim: str) -> list[str]:
    """Tier A and C only, for non-outlet-specific searches."""
    return [tq.query for tq in build_query_cascade(claim) if tq.tier in {"A", "C"}]


MIN_CANDIDATES = 15


def gather_candidates(
    claim: str,
    domain: str | None = None,
    fetch_func: Optional[Callable[[str], List[dict]]] = None,
) -> list[dict]:
    results, seen_links = [], set()
    if fetch_func is None:
        try:
            from .cross_check import _fetch, _google_news_url, _parse_rss_items

            def _default_fetcher(q: str) -> List[dict]:
                body = _fetch(_google_news_url(q, "in"))
                return _parse_rss_items(body, 15)

            fetch_func = _default_fetcher
        except Exception:
            fetch_func = lambda q: []

    for tq in build_query_cascade(claim, domain):
        for item in fetch_func(tq.query):  # URL-encoded query without surrounding quotes
            link = item.get("link", "")
            if link and link not in seen_links:
                seen_links.add(link)
                results.append({**item, "tier": tq.tier})
        if len(results) >= MIN_CANDIDATES:
            break
    return results
