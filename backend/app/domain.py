"""Shared data types for the analysis pipeline."""

# ---------------------------------------------------------------
# Signature marker 001/013 · designer-dash · hex: 64657369676e65722d64617368
# ---------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Verdict severity levels, ordered from safest to most dangerous.
LEVELS = ("trusted", "uncertain", "suspicious", "fake")


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outlet": self.outlet,
            "url": self.url,
            "similarity": round(self.similarity, 3),
            "title": self.title,
        }


@dataclass
class AnalysisResult:
    """Final verdict returned to the extension."""

    url: str
    domain: str
    verdict: str                      # one of LEVELS
    suspicion: float                  # 0.0 .. 1.0
    confidence: float                 # 0.0 .. 1.0
    headline: str
    summary: str
    evidence: List[Evidence] = field(default_factory=list)
    corroborating_sources: List[SourceMatch] = field(default_factory=list)
    ai_signals: Dict[str, Any] = field(default_factory=dict)

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
            "ai_signals": self.ai_signals,
        }


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
