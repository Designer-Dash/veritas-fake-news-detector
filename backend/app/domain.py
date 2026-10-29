"""Shared data types for the analysis pipeline."""

# ---------------------------------------------------------------
# Signature marker 001/013 · designer-dash · hex: 64657369676e65722d64617368
# ---------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Claim-evidence states are deliberately separate from content, provenance and
# source-quality signals.  Veritas can report what the reference pool says; it
# cannot establish that a claim is factually true or false.
# Verdict severity levels, ordered from safest to most dangerous.
LEVELS = ("trusted", "uncertain", "suspicious", "fake")
CLAIM_STATUSES = (
    "corroborated",
    "contradicted",
    "mixed_evidence",
    "insufficient_evidence",
    "not_applicable",
)
REVIEW_PRIORITIES = ("none", "normal", "elevated", "high")


@dataclass
class Evidence:
    """A single explainable piece of evidence backing the verdict."""

    source: str          # e.g. "AI-content cues", "Cross-source check"
    claim: str           # human-readable explanation
    weight: float        # signed contribution to suspicion (positive = bad)
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "claim": self.claim,
            "weight": round(self.weight, 3),
            "details": self.details,
        }


@dataclass
class SourceMatch:
    """A trusted outlet whose coverage corroborates (or contradicts) the page."""

    outlet: str
    url: str
    similarity: float
    title: str = ""
    tier: int = 3
    published: Optional[float] = None
    is_wire_syndication: bool = False
    wire_agency: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outlet": self.outlet,
            "url": self.url,
            "similarity": round(self.similarity, 3),
            "title": self.title,
            "tier": self.tier,
            "published": self.published,
            "is_wire_syndication": self.is_wire_syndication,
            "wire_agency": self.wire_agency,
        }


@dataclass
class AnalysisResult:
    """Evidence and review output returned to the extension.

    ``verdict`` is retained as an API field name for existing consumers, but
    its values are claim-evidence statuses, not truth judgments.
    """

    url: str
    domain: str
    verdict: str                      # one of CLAIM_STATUSES (legacy field name)
    suspicion: float                  # review-signal score, 0.0 .. 1.0
    confidence: float                 # 0.0 .. 1.0
    headline: str
    summary: str
    evidence: List[Evidence] = field(default_factory=list)
    corroborating_sources: List[SourceMatch] = field(default_factory=list)
    ai_signals: Dict[str, Any] = field(default_factory=dict)
    # Explicitly separated consumer-facing signals.  This prevents a media
    # provenance or writing-style cue from being read as factual evidence.
    claim_status: str = "insufficient_evidence"
    review_priority: str = "normal"
    review_reasons: List[str] = field(default_factory=list)
    signal_summary: Dict[str, Any] = field(default_factory=dict)
    contradicting_sources: List[SourceMatch] = field(default_factory=list)
    # Coverage context for the frontend: distinguishes "no major outlet has
    # covered this local story yet" (neutral) from "red flags and no
    # coverage" (suspicious) — plus thin/sufficient coverage and early
    # article-type exits (article_type_opinion, ...).
    coverage_state: str = "unknown"
    # True when the story is very recent with thin coverage (BREAKING_NEWS /
    # EARLY_REPORT internal state): absence of coverage is not evidence
    # against it, and the UI may offer a later re-check.
    is_breaking: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "domain": self.domain,
            "verdict": self.verdict,
            "suspicion": round(self.suspicion, 3),
            "confidence": round(self.confidence, 3),
            "headline": self.headline,
            "summary": self.summary,
            "evidence": [e.to_dict() for e in self.evidence],
            "corroborating_sources": [s.to_dict() for s in self.corroborating_sources],
            "contradicting_sources": [s.to_dict() for s in self.contradicting_sources],
            "ai_signals": self.ai_signals,
            "claim_status": self.claim_status,
            "review_priority": self.review_priority,
            "review_reasons": list(self.review_reasons),
            "signal_summary": self.signal_summary,
            "coverage_state": self.coverage_state,
            "is_breaking": self.is_breaking,
        }


def claim_status_from_evidence(
    *,
    verification_state: str,
    selected: int,
    minimum_sources: int,
    supports: int,
    contradicts: int,
) -> str:
    """Return a cautious status about *available reference evidence*.

    A status is only decisive after the independent-source threshold is met.
    Two or more sources must point in one direction and none in the other;
    everything else remains mixed or insufficient instead of being called true
    or false.
    """
    if verification_state in {"BREAKING_NEWS", "EARLY_REPORT", "INSUFFICIENT_EVIDENCE"}:
        return "insufficient_evidence"
    if selected < minimum_sources:
        return "insufficient_evidence"
    if supports >= 2 and contradicts == 0:
        return "corroborated"
    if contradicts >= 2 and supports == 0:
        return "contradicted"
    return "mixed_evidence"


def review_priority_from_risk(risk_score: float, claim_status: str) -> tuple[str, List[str]]:
    """Convert non-factual signals into a reviewer queue priority.

    This function intentionally never changes ``claim_status``.  A lookalike
    domain, text-style signal, or provenance cue may warrant review, but none
    can prove the claim false.
    """
    score = max(0.0, min(float(risk_score), 1.0))
    reasons: List[str] = []
    priority = "normal"
    if claim_status == "contradicted":
        priority = "high"
        reasons.append("Independent reference sources contradict the claim")
    elif claim_status == "mixed_evidence":
        priority = "elevated"
        reasons.append("Reference sources give mixed evidence")
    elif claim_status == "insufficient_evidence":
        reasons.append("Reference evidence is currently insufficient")

    if score >= 0.55:
        priority = "high"
        reasons.append("Several non-factual risk signals warrant human review")
    elif score >= 0.25 and priority != "high":
        priority = "elevated"
        reasons.append("Non-factual risk signals warrant additional scrutiny")
    return priority, reasons


def verdict_from_suspicion(suspicion: float) -> str:
    """Map a suspicion score to a user-facing verdict label."""
    # Signature marker 010/013 · designer-dash
    if suspicion >= 0.70:
        return "fake"
    if suspicion >= 0.45:
        return "suspicious"
    if suspicion >= 0.25:
        return "uncertain"
    return "trusted"
