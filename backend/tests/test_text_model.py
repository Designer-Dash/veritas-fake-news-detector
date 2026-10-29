"""Tests for the advisory, dependency-free claim-text model integration."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import analyze  # noqa: E402
from app.text_model import classify_news_text  # noqa: E402
from tools.train_text_model import train_model  # noqa: E402


def _write_tiny_model(path: Path) -> None:
    """Write a deliberately tiny valid model with decisive known examples."""
    payload = {
        "format": "veritas-text-model-v1",
        "model_version": "test-v1",
        "algorithm": "multinomial_naive_bayes",
        "labels": ["false", "mixed", "supported"],
        "vocabulary": ["w:hoax", "w:verified"],
        "log_priors": {label: math.log(1 / 3) for label in ("false", "mixed", "supported")},
        "log_likelihoods": {
            "false": [-0.001, -12.0],
            "mixed": [-8.0, -8.0],
            "supported": [-12.0, -0.001],
        },
        "unknown_log_likelihoods": {label: -12.0 for label in ("false", "mixed", "supported")},
        "dataset": {"name": "fixture", "text_scope": "test claims"},
        "deployment": {
            "eligible_for_scoring": True,
            "risk_threshold": 0.8,
            "validation_precision": 1.0,
            "minimum_validation_precision": 0.7,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_classifier_is_honest_when_model_is_missing(tmp_path):
    signal = classify_news_text("plain reporting words " * 20, path=tmp_path / "missing.json")
    assert signal["available"] is False
    assert signal["reason"] == "model_unavailable"


def test_classifier_returns_probabilities_for_a_valid_model(tmp_path):
    path = tmp_path / "model.json"
    _write_tiny_model(path)
    signal = classify_news_text("hoax " * 35, path=path, min_tokens=30, min_coverage=0.10)
    assert signal["available"] is True
    assert signal["prediction"] == "false"
    assert signal["false_probability"] > 0.99
    assert signal["token_coverage"] == 1.0
    assert signal["scoring_eligible"] is True
    assert set(signal["probabilities"]) == {"false", "mixed", "supported"}


def test_classifier_rejects_out_of_distribution_text(tmp_path):
    path = tmp_path / "model.json"
    _write_tiny_model(path)
    signal = classify_news_text("समाचार परीक्षण शब्द " * 20, path=path, min_tokens=30, min_coverage=0.10)
    assert signal["available"] is False
    assert signal["reason"] == "out_of_distribution"


def test_training_recipe_is_deterministic_for_identical_examples():
    examples = [
        ("false", "hoax claim repeated"),
        ("mixed", "claim contains some accurate details"),
        ("supported", "official report confirms statement"),
    ]
    first = train_model(examples, max_features=100, alpha=1.0)
    second = train_model(examples, max_features=100, alpha=1.0)
    assert first == second


def test_pipeline_applies_capped_model_risk_without_changing_verdict_authority(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    _write_tiny_model(path)
    monkeypatch.setenv("VERITAS_TEXT_MODEL_ENABLED", "1")
    monkeypatch.setenv("VERITAS_TEXT_MODEL_PATH", str(path))
    monkeypatch.setenv("VERITAS_TEXT_MODEL_FALSE_THRESHOLD", "0.80")
    result = analyze(
        "https://city-voice.in/example.html",
        html="<html><head><title>Claim under review</title></head><body><article><p>" + ("hoax " * 40) + "</p></article></body></html>",
        include_cross_check=False,
    )
    signal = result.ai_signals["text_classifier"]
    model_evidence = [item for item in result.evidence if item.source == "ML text-risk signal"]
    assert signal["available"] is True
    assert signal["applied"] is True
    assert 0 < signal["weight"] <= 0.18
    assert len(model_evidence) == 1
    # No model output is ever written into the cross-source evidence score.
    assert result.ai_signals["cross_check"]["state"] == "disabled"
