"""Lightweight text analysis used to score AI-generation cues and writing quality.

Deliberately dependency-free so it runs anywhere. It computes:
  - burstiness (variation of sentence lengths)      -> low  = AI-like
  - vocabulary richness (type-token ratio)          -> low  = AI-like
  - punctuation diversity                           -> low  = AI-like
  - emoji/hashtag density                           -> high = low-credibility
  - hedging / clickbait phrase hits                 -> high = suspicious
  - ALL-CAPS and exclamation abuse                  -> high = suspicious

Signature marker 003/013 · designer-dash
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

AI_STOCK_PHRASES = [
    "it is important to note",
    "it's important to note",
    "it is worth noting",
    "in today's fast-paced world",
    "in an era of",
    "in the digital age",
    "delve into",
    "navigating the complexities",
    "a testament to",
    "underscores the importance",
    "plays a crucial role",
    "in conclusion",
    "moreover",
    "furthermore",
    "it goes without saying",
    "at the end of the day",
    "landscape",
    "unlocking",
    "unleash",
    "embark",
    "treasure trove",
]

CLICKBAIT_PATTERNS = [
    r"\b(you won'?t believe|shocking|unbelievable|jaw[- ]dropping)\b",
    r"\b(what happened next|goes viral|breaks the internet)\b",
    r"\b(exposed|truth about|they don'?t want you to know)\b",
    r"\b(must read|must-see|share (this|before))\b",
    r"\b(breaking:?\s*.{0,40}!)",
    r"\b(sources say|insiders reveal|leaked)\b",
]

HEDGE_PATTERNS = [
    r"\b(reportedly|allegedly|rumor[s]? has it|unverified|sources claim)\b",
    r"\b(claims|suggests|may have|could be linked|is said to)\b",
]

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F02F"
    "\U00002600-\U000026FF]",
    flags=re.UNICODE,
)
HASHTAG_RE = re.compile(r"#\w+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+|[^.!?]+$")


def _words(text: str) -> List[str]:
    return WORD_RE.findall(text)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in SENTENCE_RE.findall(text) if len(s.strip()) > 2]


def burstiness(text: str) -> float:
    """Variation of sentence lengths (0..1 scale). Human prose scores high."""
    sents = _sentences(text)
    if len(sents) < 2:
        return 0.5
    lens = [len(_words(s)) for s in sents]
    mean = sum(lens) / len(lens)
    if mean == 0:
        return 0.5
    var = sum((n - mean) ** 2 for n in lens) / len(lens)
    cv = (var ** 0.5) / mean
    return min(cv / 0.8, 1.0)


def type_token_ratio(text: str, window: int = 100) -> float:
    """Moving-average type-token ratio (MATTR).

    Raw TTR is length-biased: longer texts always score lower even when
    vocabulary richness is identical. MATTR slides a fixed `window` across
    the token stream and averages per-window TTR, making scores comparable
    across article lengths. Falls back to raw TTR below one window.
    """
    words = [w.lower() for w in _words(text)]
    if not words:
        return 0.5
    if len(words) <= window:
        return len(set(words)) / len(words)
    ttrs = []
    for i in range(0, len(words) - window + 1):
        win = words[i : i + window]
        ttrs.append(len(set(win)) / window)
    return sum(ttrs) / len(ttrs)


def punctuation_diversity(text: str) -> float:
    marks = re.findall(r"[;:—–\-\(\)\"'?!.,]", text)
    if not marks:
        return 0.3
    uniq = len(set(marks))
    return min(uniq / 6, 1.0)


def emoji_hashtag_density(text: str) -> float:
    words = max(len(_words(text)), 1)
    hits = len(EMOJI_RE.findall(text)) + len(HASHTAG_RE.findall(text))
    return min(hits / (words / 25), 1.0)


def phrase_hits(text: str, patterns: List[str]) -> List[str]:
    hits: List[str] = []
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            hits.append(m.group(0)[:60])
    return hits


def caps_exclaims(text: str) -> float:
    words = _words(text)
    if not words:
        return 0.0
    # Only long ALL-CAPS words read as "shouting"; short ones are acronyms
    # (CWC, BJP, CISF) -- live testing on The Hindu showed acronym-heavy
    # political copy scoring 1.0 without any shouting at all.
    caps = sum(1 for w in words if len(w) > 5 and w.isupper())
    excl = text.count("!")
    return min((caps * 2 + excl) / (len(words) / 20), 1.0)


def extract_headline(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:300]
    return ""


def extract_text_from_html(html: str) -> str:
    """Crude HTML -> text; good enough for scoring without extra dependencies."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


_ARTICLE_BLOCK_RE = re.compile(
    r"<article\b[^>]*>(.*?)</article>", flags=re.IGNORECASE | re.DOTALL
)
_MAIN_BLOCK_RE = re.compile(r"<main\b[^>]*>(.*?)</main>", flags=re.IGNORECASE | re.DOTALL)

# Common article-body container class names across news sites.
_BODY_CLASS_RE = re.compile(
    r"<div\b[^>]*\bclass=[\"\'][^\"\']*(?:articlebodycontent|article-body|article__body|"
    r"articleBody|story-body|storybody|entry-content|post-content|post-body|"
    r"article-details|detail__body)[^\"\']*[\"\'][^>]*>",
    flags=re.IGNORECASE,
)
_ITEMPROP_RE = re.compile(
    r"<div\b[^>]*\bitemprop=[\"\']articleBody[\"\'][^>]*>", flags=re.IGNORECASE
)
_TAG = re.compile(r"<div\b[^>]*>|</div\s*>", flags=re.IGNORECASE)


def _balanced_div_block(html: str, open_tag_match: "re.Match") -> str:
    """Return the inner HTML of a <div> open tag, honouring nesting depth."""
    start = open_tag_match.end()
    depth = 1
    for m in _TAG.finditer(html, start):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return html[start : m.start()]
    return html[start : start + 60000]


def extract_article_text(html: str) -> str:
    """Text from the article content block only.

    Whole-page extraction swallows navigation/footers (menus, newsletter
    promos), which polluted stylometry and sensationalism scoring on live
    pages (The Hindu keeps its body in a plain <div class="articlebodycontent">
    with no <article>/<main> at all). Falls back to whole-page text when no
    article container exists.
    """
    for pattern in (_ARTICLE_BLOCK_RE, _MAIN_BLOCK_RE):
        m = pattern.search(html)
        if m:
            text = extract_text_from_html(m.group(1))
            if len(_words(text)) >= 40:  # sanity: a real article, not a stub
                return text

    for open_re in (_ITEMPROP_RE, _BODY_CLASS_RE):
        m = open_re.search(html)
        if m:
            text = extract_text_from_html(_balanced_div_block(html, m))
            if len(_words(text)) >= 40:
                return text

    return extract_text_from_html(html)


def ai_stylometry(text: str) -> Dict[str, Any]:
    """Composite stylometric profile with an ai_like score in 0..1."""
    sentences = _sentences(text)
    burst = burstiness(text)
    ttr = type_token_ratio(text)
    punct = punctuation_diversity(text)
    stock = phrase_hits(text, AI_STOCK_PHRASES)

    # Low burstiness + low vocabulary richness + stock phrases => template-like text.
    ai_like = (1 - burst) * 0.4 + (1 - min(ttr * 1.6, 1.0)) * 0.35 + min(len(stock) / 3, 1.0) * 0.25
    low_signal = len(_words(text)) < 80

    return {
        "ai_like": round(min(max(ai_like, 0.0), 1.0), 3),
        "burstiness": round(burst, 3),
        "type_token_ratio": round(ttr, 3),
        "punctuation_diversity": round(punct, 3),
        "stock_phrase_count": len(stock),
        "stock_phrases": stock[:5],
        "low_signal": low_signal,
        "sentences": len(sentences),
    }
