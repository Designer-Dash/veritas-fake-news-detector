"""Deterministic, explainable claim-based verification & evidence scoring.

Flow (spec items 1-12):

    article -> extractClaim() -> generateSearchQueries() (2-3 queries)
            -> retrieveReferenceSources() (ranked, strongest 5-10 kept)
            -> calculateSemanticSimilarity()  -> M   (0-100)
            -> classifyRelationship()         -> SUPPORTS / CONTRADICTS / UNKNOWN
            -> calculateConsensus()           -> C   (0-100, weighted A/U/D)
            -> calculateFactConsistency()     -> F   (0-100)
            -> calculateSourceQuality()       -> S   (0-100)
            -> calculateEvidenceScore()       -> E   = 0.30M + 0.30C + 0.20F + 0.20S
            -> applyContradictionPenalty()    -> E_final (E - 15 on strong contradiction)
            -> detectBreakingNews()           -> internal BREAKING_NEWS / EARLY_REPORT
            -> getFinalVerificationResult()   -> state + components + per-source detail

E is an EVIDENCE SCORE (0-100), NOT the mathematical probability that the
news is true. Gemini is only ever used for structured analysis (claim
extraction, pairwise similarity/relationship in ONE batched call); every
number in the final score is computed here, deterministically.

Internal states (the extension's UI vocabulary is unchanged):
    SUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | BREAKING_NEWS | EARLY_REPORT

Signature marker: verification rework (claim-based evidence scoring).
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .claim_analysis import Claim, normalize_claim
from .config import VerificationConfig, get_config
from .gemini_client import CallBudget, gemini_json
from .sources import source_tier, source_weight

logger = logging.getLogger("veritas.verification")

# Relationship vocabulary (spec item 5).
SUPPORTS = "SUPPORTS"
CONTRADICTS = "CONTRADICTS"
UNKNOWN = "UNKNOWN"
RELATIONSHIPS = (SUPPORTS, CONTRADICTS, UNKNOWN)

# Internal evidence states (spec items 9 & 12) -- never rendered by the UI.
SUFFICIENT = "SUFFICIENT_EVIDENCE"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
BREAKING = "BREAKING_NEWS"
EARLY_REPORT = "EARLY_REPORT"

# Explicit denial/refutation language in a reference title.
_DENIAL_RE = re.compile(
    r"\b(den(?:ies|ied)|debunk(?:ed|ing)?|hoax|false(?:hood)?|misleading|"
    r"fact[\s-]?check(?:ed)?|misinformation|disinformation|not true|"
    r"no evidence|misattributed|fake|"
    r"no casualt(?:y|ies)|no deaths?|no victims?)\b",
    re.IGNORECASE,
)

_UNIT_RE = (
    r"%|percent|crore|lakh|billion|million|trillion|kg|kilos?|km|ml|"
    r"seats|killed|died|dead|injured|hurt"
)
_NUM_UNIT_RE = re.compile(rf"(\d[\d,\.]*)\s*({_UNIT_RE})\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_HUMAN_DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SourceAssessment:
    """One reference source, classified against the target claim."""

    outlet: str
    title: str
    url: str = ""
    domain: str = ""
    tier: int = 0
    weight: float = 0.4          # configurable tier weight (0-1)
    similarity: float = 0.0      # M contribution, 0-100
    relationship: str = UNKNOWN  # SUPPORTS | CONTRADICTS | UNKNOWN
    compared_by: str = "deterministic"  # or "gemini"

    @property
    def relevant(self) -> bool:
        return self.similarity >= 40.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outlet": self.outlet,
            "domain": self.domain,
            "url": self.url,
            "title": self.title,
            "tier": self.tier,
            "weight": round(self.weight, 3),
            "similarity": round(self.similarity, 1),
            "relationship": self.relationship,
            "compared_by": self.compared_by,
        }


@dataclass
class VerificationResult:
    """Full, explainable output of the verification layer."""

    state: str
    claim: Claim
    M: float                     # semantic similarity score
    C: float                     # weighted cross-source consensus
    F: float                     # factual consistency
    S: float                     # source-quality score
    E: float                     # evidence score before penalty
    e_final: float               # after contradiction penalty, clamped 0-100
    penalty_applied: bool
    breaking: bool
    breaking_info: Dict[str, Any]
    assessments: List[SourceAssessment]
    examined: int                # relevant candidates found
    selected: int                # candidates actually processed (<= max 10)
    queries: List[str] = field(default_factory=list)
    region: str = ""
    gemini_compared: bool = False

    # -- convenience views -------------------------------------------------
    @property
    def supports(self) -> List[SourceAssessment]:
        return [a for a in self.assessments if a.relationship == SUPPORTS]

    @property
    def contradicts(self) -> List[SourceAssessment]:
        return [a for a in self.assessments if a.relationship == CONTRADICTS]

    @property
    def unknowns(self) -> List[SourceAssessment]:
        return [a for a in self.assessments if a.relationship == UNKNOWN]

    @property
    def support_count(self) -> int:
        return len(self.supports)

    @property
    def contradict_count(self) -> int:
        return len(self.contradicts)

    @property
    def unknown_count(self) -> int:
        return len(self.unknowns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "breaking": self.breaking,
            "breaking_info": dict(self.breaking_info),
            "components": {
                "semantic_M": round(self.M, 1),
                "consensus_C": round(self.C, 1),
                "facts_F": round(self.F, 1),
                "source_quality_S": round(self.S, 1),
            },
            "evidence_score": round(self.e_final, 1),
            "evidence_score_before_penalty": round(self.E, 1),
            "contradiction_penalty_applied": self.penalty_applied,
            "counts": {
                "found": self.examined,
                "selected": self.selected,
                "supports": self.support_count,
                "contradicts": self.contradict_count,
                "unknown": self.unknown_count,
            },
            "sources": [a.to_dict() for a in self.assessments],
            "claim": self.claim.to_dict(),
            "queries": list(self.queries),
            "region": self.region,
            "gemini_compared": self.gemini_compared,
        }


# ---------------------------------------------------------------------------
# 2. Semantic similarity (wording-tolerant)
# ---------------------------------------------------------------------------

def calculate_semantic_similarity(
    claim_text: str,
    ref_text: str,
    gemini_similarity: Optional[float] = None,
) -> float:
    """Similarity (0-100) between the normalized claim and a reference
    article title (spec: calculateSemanticSimilarity()).

    High similarity means "same underlying claim/event" -- it says nothing
    about whether the claim is true. Gemini's judgment wins when provided;
    the deterministic blend (normalized token Jaccard + character ratio)
    keeps the system fully functional with no API key.
    """
    if gemini_similarity is not None:
        try:
            return _clamp(float(gemini_similarity))
        except (TypeError, ValueError):
            pass
    a = normalize_claim(claim_text)
    b = normalize_claim(ref_text)
    if not a or not b:
        return 0.0
    # Wording-tolerant blend of three views of the same question "do these
    # two discuss the same underlying claim?":
    #   * jaccard — exact normalized-token overlap (conservative),
    #   * recall  — share of the smaller claim's content words covered
    #               (paraphrases reuse the key words in new wording),
    #   * ratio   — character-level sequence ratio (catches reordering).
    # Unrelated stories score near 0 on all three; paraphrases of one
    # event clear the relevance/supports thresholds.
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jac = inter / len(ta | tb)
    recall = inter / min(len(ta), len(tb))
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return round(_clamp((0.40 * jac + 0.30 * recall + 0.30 * ratio) * 100.0), 1)


# ---------------------------------------------------------------------------
# 3. Relationship classification
# ---------------------------------------------------------------------------

def _number_units(text: str) -> Dict[str, str]:
    """{unit: digits} pairs found in text (e.g. {"crore": "3000"})."""
    out: Dict[str, str] = {}
    for m in _NUM_UNIT_RE.finditer(text or ""):
        unit = (m.group(2) or "").lower()
        digits = re.sub(r"\D", "", m.group(1))
        if unit and digits:
            out[unit] = digits
    return out


def _date_keys(text: str) -> set:
    keys = set()
    for m in _ISO_DATE_RE.finditer(text or ""):
        keys.add(m.group(1))
    for m in _HUMAN_DATE_RE.finditer(text or ""):
        raw = m.group(1).lower().replace(",", " ").split()
        try:
            if raw[0].isdigit():
                day, month, year = int(raw[0]), raw[1], int(raw[-1])
            else:
                month, day, year = raw[0], int(raw[1]), int(raw[-1])
            month_n = _MONTHS[month[:5] if month[:5] in _MONTHS else month[:3]]
            keys.add(datetime(year, month_n, day).date().isoformat())
        except (ValueError, KeyError, IndexError):
            continue
    return keys


def _critical_fact_conflict(claim: Claim, ref_title: str) -> bool:
    """True only when a *critical* fact clearly disagrees: same unit,
    different value (e.g. "3,000 crore" vs "2,000 crore") or different
    dates. Harmless wording differences and different dimensions
    ("526 kg" vs "3,000 crore") are never treated as conflicts."""
    claim_units: Dict[str, str] = {}
    ref_units = _number_units(ref_title)
    for fact in claim.important_facts:
        if fact.get("type") == "number":
            claim_units.update(_number_units(str(fact.get("value", ""))))
    for unit, digits in claim_units.items():
        ref_digits = ref_units.get(unit)
        if ref_digits and ref_digits != digits:
            return True

    claim_dates = set()
    for fact in claim.important_facts:
        if fact.get("type") == "date":
            claim_dates.add(str(fact.get("value", ""))[:10])
    ref_dates = _date_keys(ref_title)
    if claim_dates and ref_dates and claim_dates.isdisjoint(ref_dates):
        return True
    return False


def classify_relationship(
    claim: Claim,
    ref_title: str,
    similarity: float,
    gemini_relationship: Optional[str] = None,
    cfg: Optional[VerificationConfig] = None,
) -> str:
    """Classify a reference source against the target claim (spec:
    classifyRelationship()): SUPPORTS / CONTRADICTS / UNKNOWN.

    Gemini's structured judgment wins when available; the deterministic
    rules below keep the system working offline:
      * unrelated (low similarity)              -> UNKNOWN
      * critical fact conflict / explicit denial -> CONTRADICTS
      * same-event similarity                   -> SUPPORTS
      * similar topic but not clearly the claim -> UNKNOWN
    """
    cfg = cfg or get_config()
    if gemini_relationship in RELATIONSHIPS:
        return gemini_relationship
    if similarity < cfg.relevance_similarity:
        return UNKNOWN
    if _critical_fact_conflict(claim, ref_title):
        return CONTRADICTS
    if similarity >= cfg.denial_similarity and _DENIAL_RE.search(ref_title or ""):
        return CONTRADICTS
    if similarity >= cfg.supports_similarity:
        return SUPPORTS
    return UNKNOWN


# ---------------------------------------------------------------------------
# Gemini batch comparison (ONE call for all sources)
# ---------------------------------------------------------------------------

def _gemini_compare(
    claim: Claim,
    candidates: List[Dict[str, Any]],
    budget: Optional[CallBudget],
    cfg: VerificationConfig,
) -> Dict[int, Dict[str, Any]]:
    """Batched Gemini call: semantic similarity + relationship for every
    candidate at once (minimizes API usage). Returns {} on any failure or
    when no API key is configured."""
    if not candidates or budget is None:
        return {}
    payload = {
        "claim": claim.to_dict(),
        "sources": [
            {"id": i, "title": c.get("title", ""), "outlet": c.get("outlet", "")}
            for i, c in enumerate(candidates)
        ],
    }
    prompt = (
        "You compare a news claim with reference article titles. Return ONLY "
        "a JSON array (no prose). For every source decide:\n"
        '  "semantic_similarity": integer 0-100 — how strongly the two discuss '
        "the SAME underlying claim/event (paraphrases of one event score "
        "high, unrelated stories score low);\n"
        '  "relationship": how the reference relates to the claim — '
        '"SUPPORTS" (corroborates it), "CONTRADICTS" (incompatible key facts '
        'or explicitly refutes it), or "UNKNOWN" (nothing relevant / cannot '
        'tell from the title alone).\n'
        "Similarity is not truth: a source can be highly similar and still "
        "contradict the claim. Never decide whether the claim is true — only "
        "compare.\n"
        'Item schema: {"id": int, "semantic_similarity": int, '
        '"relationship": "SUPPORTS"|"CONTRADICTS"|"UNKNOWN"}\n'
        f"Claim JSON: {json.dumps(payload['claim'], ensure_ascii=False)}\n"
        f"Sources JSON: {json.dumps(payload['sources'], ensure_ascii=False)}"
    )
    cache_key = (
        "cmp:"
        + str(hash((claim.claim, tuple(c.get("title", "") for c in candidates))))
    )
    data = gemini_json(prompt, cache_key=cache_key, budget=budget)
    if not isinstance(data, list):
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(candidates):
            continue
        rel = str(item.get("relationship", "")).strip().upper()
        if rel not in RELATIONSHIPS:
            rel = UNKNOWN
        try:
            sim = _clamp(float(item.get("semantic_similarity")))
        except (TypeError, ValueError):
            sim = None
        out[idx] = {"relationship": rel, "similarity": sim}
    return out


# ---------------------------------------------------------------------------
# 4. Weighted cross-source consensus
# ---------------------------------------------------------------------------

def calculate_consensus(
    assessments: List[SourceAssessment],
    cfg: Optional[VerificationConfig] = None,
    coverage_multiplier: float = 1.0,
) -> float:
    """Weighted cross-source consensus C (spec: calculateConsensus()):

        C = ((A + 0.5*U) / (A + U + D)) * 100

    A = weighted supporting evidence, U = weighted unknown/no-report
    evidence (counts half), D = weighted contradictory evidence (never
    neutral). `coverage_multiplier` < 1 pulls C toward neutral 50 when
    fewer than min_sources were found, so a thin pool never *pretends*
    consensus is strong (spec item 12).
    """
    cfg = cfg or get_config()
    a = sum(
        x.weight * (x.similarity / 100.0)
        for x in assessments
        if x.relationship == SUPPORTS
    )
    d = sum(
        x.weight * (x.similarity / 100.0)
        for x in assessments
        if x.relationship == CONTRADICTS
    )
    u = sum(x.weight for x in assessments if x.relationship == UNKNOWN)
    denom = a + u + d
    if denom <= 0:
        return float(cfg.neutral_score)  # no data: neutral, never invented
    c = ((a + cfg.unknown_factor * u) / denom) * 100.0
    if 0.0 < coverage_multiplier < 1.0:
        c = cfg.neutral_score + (c - cfg.neutral_score) * coverage_multiplier
    return round(_clamp(c), 1)


# ---------------------------------------------------------------------------
# 5. Factual consistency
# ---------------------------------------------------------------------------

def _fact_appears(fact: Dict[str, Any], ref_title: str) -> bool:
    ftype = str(fact.get("type", ""))
    value = str(fact.get("value", "")).strip()
    if not value or not ref_title:
        return False

    if ftype == "number":
        digits = re.sub(r"\D", "", value)
        if not digits:
            return False
        return digits in re.sub(r"\D", "", ref_title)
    if ftype == "date":
        ref_dates = _date_keys(ref_title)
        return str(value)[:10] in ref_dates
    # entity / location / event: stemmed token subset match on the normalized
    # reference title (wording differences are fine, missing facts are not).
    want = {
        w for w in normalize_claim(value).split()
        if w and w not in ("the", "a", "an")
    }
    if not want:
        return False
    have = set(normalize_claim(ref_title).split())
    return want.issubset(have)


def _fact_conflicts_with_any(fact: Dict[str, Any], assessments: List[SourceAssessment]) -> bool:
    """True when a reference source states the same fact *differently*
    (same unit, different number / a different date). Mere absence is not
    a conflict."""
    ftype = str(fact.get("type", ""))
    value = str(fact.get("value", "")).strip()
    if not value:
        return False
    if ftype == "number":
        want = _number_units(value)
        if not want:
            return False
        for a in assessments:
            have = _number_units(a.title)
            for unit, digits in want.items():
                if unit in have and have[unit] != digits:
                    return True
        return False
    if ftype == "date":
        want_date = value[:10]
        for a in assessments:
            dates = _date_keys(a.title)
            if dates and want_date not in dates:
                return True
        return False
    return False


def calculate_fact_consistency(
    claim: Claim,
    assessments: List[SourceAssessment],
    cfg: Optional[VerificationConfig] = None,
) -> float:
    """F (spec: calculateFactConsistency()):
    weighted consistent important facts / total important facts * 100.
    Critical facts (date/number/person/location/event) count double.
    A fact confirmed by a supporting source counts fully; a fact stated
    *differently* by a reference source counts zero (critical-fact mismatch
    outweighs any wording agreement); a fact no source mentions gets
    fact_unmentioned_factor credit -- unverified is not disproved. Wording
    differences never count against F.
    Neutral 50 when the article states no checkable facts or nothing is
    close enough to check (no data -- not a pass, not a fail)."""
    cfg = cfg or get_config()
    facts = [f for f in claim.important_facts if str(f.get("value", "")).strip()]
    if not facts:
        return float(cfg.neutral_score)

    assessable = [a for a in assessments if a.similarity >= cfg.relevance_similarity]
    if not assessable:
        # No source close enough to check against: no data -> neutral.
        return float(cfg.neutral_score)
    # Only supporting sources may *confirm* a fact: an UNKNOWN source may
    # merely be about the same topic (possibly a different event), and a
    # contradicting source by definition disagrees with the claim.
    confirmers = [a for a in assessable if a.relationship == SUPPORTS]
    total = 0.0
    credit = 0.0
    for fact in facts:
        w = cfg.fact_critical_weight if fact.get("critical", True) else 1.0
        total += w
        if any(_fact_appears(fact, a.title) for a in confirmers):
            credit += w                      # confirmed by a supporting source
        elif _fact_conflicts_with_any(fact, assessable):
            credit += 0.0                    # stated differently: real mismatch
        else:
            # Not mentioned by any source: unverified, not disproved --
            # same half-weight principle as unknown evidence in consensus.
            credit += w * cfg.fact_unmentioned_factor
    if total <= 0:
        return float(cfg.neutral_score)
    return round(_clamp((credit / total) * 100.0), 1)


# ---------------------------------------------------------------------------
# 6. Source-quality score
# ---------------------------------------------------------------------------

def calculate_source_quality(assessments: List[SourceAssessment]) -> float:
    """S: mean tier weight of the selected sources, normalized to 0-100."""
    if not assessments:
        return 0.0
    return round(_clamp(sum(a.weight for a in assessments) / len(assessments) * 100.0), 1)


# ---------------------------------------------------------------------------
# 7. Final evidence score
# ---------------------------------------------------------------------------

def calculate_evidence_score(
    M: float, C: float, F: float, S: float, cfg: Optional[VerificationConfig] = None
) -> float:
    """E = 0.30M + 0.30C + 0.20F + 0.20S (weights configurable), clamped
    0-100. This is an EVIDENCE SCORE, not a probability of truth."""
    cfg = cfg or get_config()
    e = (
        cfg.w_semantic * _clamp(M)
        + cfg.w_consensus * _clamp(C)
        + cfg.w_facts * _clamp(F)
        + cfg.w_source * _clamp(S)
    )
    return round(_clamp(e), 1)


# ---------------------------------------------------------------------------
# 8. Contradiction protection
# ---------------------------------------------------------------------------

def apply_contradiction_penalty(
    evidence_score: float,
    assessments: List[SourceAssessment],
    cfg: Optional[VerificationConfig] = None,
) -> tuple:
    """E_final = E - penalty when a *high-weight trusted source* strongly
    contradicts the claim (a pile of weak supporting sources must never
    drown it out); otherwise E_final = E. Clamped to 0-100 (spec item 8)."""
    cfg = cfg or get_config()
    strong = any(
        a.relationship == CONTRADICTS
        and a.weight >= cfg.strong_contradiction_min_weight
        and a.similarity >= cfg.strong_contradiction_min_similarity
        for a in assessments
    )
    if strong:
        return _clamp(evidence_score - cfg.contradiction_penalty), True
    return _clamp(evidence_score), False


# ---------------------------------------------------------------------------
# 9. Breaking-news detection
# ---------------------------------------------------------------------------

def detect_breaking_news(
    published_date: Optional[datetime],
    available_sources: int,
    cfg: Optional[VerificationConfig] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Detect very recent articles with thin coverage (spec:
    detectBreakingNews()). Absence of reports is NOT evidence against a
    story that just happened — the caller marks it BREAKING_NEWS /
    EARLY_REPORT internally. Unknown dates are never treated as breaking."""
    cfg = cfg or get_config()
    now = now or datetime.now(timezone.utc)
    info: Dict[str, Any] = {
        "is_breaking": False,
        "published": None,
        "age_hours": None,
        "reason": "no publication date",
    }
    if published_date is None:
        return info
    pdt = (
        published_date
        if published_date.tzinfo is not None
        else published_date.replace(tzinfo=timezone.utc)
    )
    if pdt > now + timedelta(hours=1):
        info["reason"] = "publication date in the future (bad data)"
        return info
    age_hours = (now - pdt).total_seconds() / 3600.0
    info["published"] = pdt.isoformat()
    info["age_hours"] = round(age_hours, 1)
    if age_hours > cfg.breaking_window_hours:
        info["reason"] = "older than the breaking-news window"
        return info
    if available_sources >= cfg.breaking_min_sources:
        info["reason"] = "recent but enough independent sources exist"
        return info
    info["is_breaking"] = True
    info["reason"] = (
        f"published {age_hours:.1f}h ago with only {available_sources} "
        "reference source(s)"
    )
    return info


# ---------------------------------------------------------------------------
# 12. Orchestrator
# ---------------------------------------------------------------------------

def get_final_verification_result(
    claim: Claim,
    candidates: List[Dict[str, Any]],
    region: str = "",
    published_date: Optional[datetime] = None,
    queries: Optional[List[str]] = None,
    budget: Optional[CallBudget] = None,
    cfg: Optional[VerificationConfig] = None,
) -> VerificationResult:
    """Run the full deterministic scoring pipeline (spec:
    getFinalVerificationResult()).

    `candidates` comes from retrieve_reference_sources() (already ranked,
    relevance-filtered). At most cfg.max_sources (10) are processed.
    """
    cfg = cfg or get_config()
    queries = list(queries or [])

    found = len(candidates)
    selected_sources = candidates[: cfg.max_sources]

    breaking_info = detect_breaking_news(published_date, found, cfg)
    is_breaking = bool(breaking_info.get("is_breaking"))

    # ONE batched Gemini call for similarity + relationship (budgeted).
    gemini_results = _gemini_compare(claim, selected_sources, budget, cfg)
    gemini_used = bool(gemini_results)

    assessments: List[SourceAssessment] = []
    for i, src in enumerate(selected_sources):
        domain = str(src.get("domain", ""))
        g = gemini_results.get(i)
        sim = calculate_semantic_similarity(
            claim.claim, str(src.get("title", "")),
            gemini_similarity=(g or {}).get("similarity"),
        )
        rel = classify_relationship(
            claim,
            str(src.get("title", "")),
            sim,
            gemini_relationship=(g or {}).get("relationship"),
            cfg=cfg,
        )
        assessments.append(
            SourceAssessment(
                outlet=str(src.get("outlet", "")) or domain or "unknown",
                title=str(src.get("title", ""))[:200],
                url=str(src.get("url", "")),
                domain=domain,
                tier=source_tier(domain),
                weight=source_weight(domain, cfg),
                similarity=sim,
                relationship=rel,
                compared_by="gemini" if g else "deterministic",
            )
        )

    selected = len(assessments)
    coverage = min(selected / float(cfg.min_sources), 1.0)

    # M: mean similarity of the sources that actually discuss this claim
    # (SUPPORTS or CONTRADICTS -- semantic similarity is not truth). UNKNOWN
    # sources are "maybe the same topic, not clearly the claim", so they
    # never pad the similarity score; 0 when no source is confident.
    claim_sims = sorted(
        (a.similarity for a in assessments if a.relationship != UNKNOWN),
        reverse=True,
    )
    if claim_sims:
        top = claim_sims[:5]
        M = round(sum(top) / len(top), 1)
    else:
        M = 0.0

    # C: thin pools are pulled toward neutral unless the story is breaking.
    coverage_for_consensus = 1.0 if is_breaking else coverage
    C = calculate_consensus(assessments, cfg, coverage_multiplier=coverage_for_consensus)

    F = calculate_fact_consistency(claim, assessments, cfg)
    S = calculate_source_quality(assessments)
    E = calculate_evidence_score(M, C, F, S, cfg)
    e_final, penalty_applied = apply_contradiction_penalty(E, assessments, cfg)

    if is_breaking:
        state = BREAKING if selected > 0 else EARLY_REPORT
    elif selected == 0:
        state = INSUFFICIENT  # no usable reference source: never "fake"
    elif selected < cfg.min_sources:
        state = INSUFFICIENT
    else:
        state = SUFFICIENT

    result = VerificationResult(
        state=state,
        claim=claim,
        M=M,
        C=C,
        F=F,
        S=S,
        E=E,
        e_final=e_final,
        penalty_applied=penalty_applied,
        breaking=is_breaking,
        breaking_info=breaking_info,
        assessments=assessments,
        examined=found,
        selected=selected,
        queries=queries,
        region=region,
        gemini_compared=gemini_used,
    )

    logger.debug("claim: %s", claim.claim)
    logger.debug("queries: %s", queries)
    logger.debug(
        "sources found=%d selected=%d state=%s",
        found, selected, state,
    )
    logger.debug(
        "source weights: %s",
        {a.outlet: a.weight for a in assessments},
    )
    logger.debug(
        "similarities: %s",
        {a.outlet: a.similarity for a in assessments},
    )
    logger.debug(
        "supports=%d contradicts=%d unknown=%d",
        result.support_count, result.contradict_count, result.unknown_count,
    )
    logger.debug(
        "M=%.1f C=%.1f F=%.1f S=%.1f E=%.1f e_final=%.1f penalty=%s breaking=%s",
        M, C, F, S, E, e_final, penalty_applied, is_breaking,
    )
    return result
