"""Runtime support for Veritas's reproducible text-risk classifiers.

Two artifact families are supported, both plain validated JSON (never pickle and
never executable code):

``veritas-text-model-v1``
    The original LIAR claim-text baseline. Word unigrams + adjacent bigrams,
    a fixed three-label scheme, validated against the historical defaults so an
    old artifact keeps loading byte-for-byte identical behaviour.

``veritas-text-model-v2``
    The domain-matched Indic news model. The artifact declares its own label
    set, feature specification, language coverage and optional calibration map,
    so training and serving can never silently disagree about features.

Either way the classifier is a *weak advisory* signal: it does not verify facts,
does not replace cross-source evidence, and returns unavailable rather than
guessing when the input is outside its vocabulary.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


logger = logging.getLogger("veritas.text_model")

# -- Format identifiers -------------------------------------------------------
MODEL_FORMAT = "veritas-text-model-v1"
MODEL_FORMAT_V2 = "veritas-text-model-v2"
SUPPORTED_FORMATS: Tuple[str, ...] = (MODEL_FORMAT, MODEL_FORMAT_V2)
MODEL_VERSION = "liar-mnb-v1"
LABELS: Tuple[str, ...] = ("false", "mixed", "supported")

# -- Tokenizers ---------------------------------------------------------------
# ``legacy-word2`` is the original tokenizer. It is kept byte-for-byte because
# the v1 LIAR artifact was trained with it and its published metrics must keep
# describing the artifact that ships.
LEGACY_TOKENIZER = "legacy-word2"
# ``unicode-words-v2`` fixes a real defect: Python's ``\w`` does not include
# combining marks (matras), so the legacy pattern shredded every Indic word at
# each vowel sign - "\u0935\u093e\u092f\u0930\u0932" became "\u092f\u0930\u0932". The Indic ranges are added explicitly so Devanagari and other
# Indic words stay whole. Latin behaviour is equivalent to the legacy pattern.
UNICODE_TOKENIZER = "unicode-words-v2"
TOKENIZERS: Tuple[str, ...] = (LEGACY_TOKENIZER, UNICODE_TOKENIZER)

_MARK_AND_SCRIPT_RANGES = (
    "\u0300-\u036f"  # combining diacritical marks
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u0750-\u077f"  # Arabic supplement
    "\u0900-\u097f"  # Devanagari
    "\u0980-\u09ff"  # Bengali
    "\u0a00-\u0a7f"  # Gurmukhi
    "\u0a80-\u0aff"  # Gujarati
    "\u0b00-\u0b7f"  # Oriya
    "\u0b80-\u0bff"  # Tamil
    "\u0c00-\u0c7f"  # Telugu
    "\u0c80-\u0cff"  # Kannada
    "\u0d00-\u0d7f"  # Malayalam
    "\u0d80-\u0dff"  # Sinhala
)

TOKEN_RE = re.compile(r"(?u)\b[\w'-]{2,}\b")
WORD_RUN_RE = re.compile(
    "(?u)[\\w%s][\\w%s'-]*" % (_MARK_AND_SCRIPT_RANGES, _MARK_AND_SCRIPT_RANGES)
)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
LATIN_RE = re.compile(r"[A-Za-z]")
WHITESPACE_RE = re.compile(r"\s+")

# The domain-matched Indian-news model is the default. The original LIAR
# claim-level baseline is kept alongside it for comparison and can be selected
# with VERITAS_TEXT_MODEL_PATH.
MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
DEFAULT_MODEL_PATH = MODELS_DIR / "indic_news_mnb_v2.json"
LEGACY_MODEL_PATH = MODELS_DIR / "liar_mnb_v1.json"
DEFAULT_FEATURE_SPEC: Dict[str, Any] = {
    "word_ngrams": [1, 2],
    "char_ngrams": [],
    "char_limit": 0,
    "text_limit": 0,
    "redacted_terms": [],
    "tokenizer": LEGACY_TOKENIZER,
}
MAX_FEATURES = 400_000
MAX_LABELS = 12


def tokenize(text: str, tokenizer: str = LEGACY_TOKENIZER) -> List[str]:
    """Return the deterministic tokenization shared by training and serving.

    The tokenizer is named in the artifact's feature specification, so a model
    is always served with the exact tokenizer it was trained with.
    """
    sample = text or ""
    if tokenizer == UNICODE_TOKENIZER:
        return [
            match.group(0).lower()
            for match in WORD_RUN_RE.finditer(sample)
            if len(match.group(0)) >= 2
        ]
    return [token.lower() for token in TOKEN_RE.findall(sample)]


def make_features(tokens: Iterable[str]) -> Counter[str]:
    """Generate unigram and adjacent-bigram count features (v1 representation)."""
    words = list(tokens)
    features: Counter[str] = Counter("w:" + word for word in words)
    features.update("b:" + left + " " + right for left, right in zip(words, words[1:]))
    return features


def detect_language(text: str) -> str:
    """Classify text as Devanagari (``hi``), Latin (``en``) or ``unknown``.

    This is a script heuristic only, used to report which language variant an
    input exercises. It is deliberately not a language-identification model and
    is never used as a classification feature.
    """
    sample = text or ""
    devanagari = len(DEVANAGARI_RE.findall(sample))
    latin = len(LATIN_RE.findall(sample))
    if devanagari == 0 and latin == 0:
        return "unknown"
    return "hi" if devanagari >= latin else "en"


def _char_snippet(text: str, limit: int) -> str:
    collapsed = WHITESPACE_RE.sub(" ", (text or "").lower())
    if limit and limit > 0:
        return collapsed[:limit]
    return collapsed


def word_feature_term(feature: str) -> Optional[str]:
    """Return the comparable term of a word feature, or None for char features.

    ``w:ndtv`` -> ``ndtv``; ``b:fact checker`` -> ``factchecker`` so a two-word
    brand and its joined form match the same redaction entry.
    """
    if feature.startswith("w:"):
        return feature[2:]
    if feature.startswith("b:"):
        return feature[2:].replace(" ", "")
    return None


def extract_features(text: str, spec: Optional[Mapping[str, Any]] = None) -> Counter[str]:
    """Feature extraction driven by the artifact's own feature specification.

    Training and inference both call this function so the two can never drift.
    ``redacted_terms`` removes publisher/fact-checker identity from the word
    feature space; because the list travels inside the artifact, serving applies
    exactly the same redaction the trainer applied.
    """
    active = spec or DEFAULT_FEATURE_SPEC
    features: Counter[str] = Counter()
    text_limit = int(active.get("text_limit") or 0)
    working = text[:text_limit] if text_limit > 0 else text
    word_ngrams = {int(n) for n in active.get("word_ngrams", []) or []}
    if word_ngrams:
        words = tokenize(working, active.get("tokenizer") or LEGACY_TOKENIZER)
        if 1 in word_ngrams:
            features.update("w:" + word for word in words)
        if 2 in word_ngrams:
            features.update("b:" + left + " " + right for left, right in zip(words, words[1:]))
    char_ngrams = sorted({int(n) for n in active.get("char_ngrams", []) or [] if int(n) >= 2})
    if char_ngrams:
        snippet = _char_snippet(working, int(active.get("char_limit") or 0))
        length = len(snippet)
        for size in char_ngrams:
            if length < size:
                continue
            prefix = "c%d:" % size
            features.update(prefix + snippet[index:index + size] for index in range(length - size + 1))
    redacted = active.get("redacted_terms") or ()
    if redacted:
        blocked = frozenset(redacted)
        features = Counter(
            {
                feature: count
                for feature, count in features.items()
                if word_feature_term(feature) not in blocked
            }
        )
    return features


def default_model_path() -> Path:
    configured = os.environ.get("VERITAS_TEXT_MODEL_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_MODEL_PATH


def _unavailable(reason: str, *, token_count: int = 0) -> Dict[str, Any]:
    return {
        "available": False,
        "applied": False,
        "reason": reason,
        "token_count": token_count,
    }


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_calibration(raw: Any) -> Dict[str, Any]:
    """Validate an optional monotone piecewise-linear calibration map."""
    if raw is None or (isinstance(raw, Mapping) and not raw):
        return {}
    if not isinstance(raw, Mapping) or raw.get("method") != "piecewise_linear":
        raise ValueError("unsupported calibration")
    xs = [float(value) for value in raw["x"]]
    ys = [float(value) for value in raw["y"]]
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError("invalid calibration shape")
    if any(not math.isfinite(value) for value in xs + ys):
        raise ValueError("non-finite calibration")
    if any(left >= right for left, right in zip(xs, xs[1:])):
        raise ValueError("calibration knots are not increasing")
    if any(left > right + 1e-12 for left, right in zip(ys, ys[1:])):
        raise ValueError("calibration map is not monotone")
    return {"method": "piecewise_linear", "x": xs, "y": ys}


def _validate_feature_spec(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return dict(DEFAULT_FEATURE_SPEC)
    if not isinstance(raw, Mapping):
        raise ValueError("invalid feature spec")
    word_ngrams = sorted({int(n) for n in raw.get("word_ngrams", []) or []})
    char_ngrams = sorted({int(n) for n in raw.get("char_ngrams", []) or []})
    if any(n not in (1, 2, 3) for n in word_ngrams):
        raise ValueError("unsupported word n-gram")
    if any(n < 2 or n > 8 for n in char_ngrams):
        raise ValueError("unsupported character n-gram")
    limit = int(raw.get("char_limit") or 0)
    if limit < 0 or limit > 200_000:
        raise ValueError("invalid char limit")
    text_limit = int(raw.get("text_limit") or 0)
    if text_limit < 0 or text_limit > 400_000:
        raise ValueError("invalid text limit")
    if not word_ngrams and not char_ngrams:
        raise ValueError("empty feature spec")
    redacted_raw = raw.get("redacted_terms") or []
    if not isinstance(redacted_raw, (list, tuple)) or len(redacted_raw) > 4000:
        raise ValueError("invalid redaction list")
    redacted: List[str] = []
    for term in redacted_raw:
        if not isinstance(term, str) or not term or len(term) > 80:
            raise ValueError("invalid redaction term")
        if term != term.strip().lower() or " " in term:
            raise ValueError("redaction terms must be lower-case with no spaces")
        redacted.append(term)
    tokenizer = str(raw.get("tokenizer") or LEGACY_TOKENIZER)
    if tokenizer not in TOKENIZERS:
        raise ValueError("unsupported tokenizer")
    return {
        "word_ngrams": word_ngrams,
        "char_ngrams": char_ngrams,
        "char_limit": limit,
        "text_limit": text_limit,
        "redacted_terms": sorted(set(redacted)),
        "tokenizer": tokenizer,
    }


@lru_cache(maxsize=4)
def _load_model(path_string: str, mtime_ns: int) -> Optional[Dict[str, Any]]:
    """Load and validate a JSON model, keyed by path + mtime for hot reload."""
    del mtime_ns  # Included in the cache key; not part of the file format.
    path = Path(path_string)
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Text model unavailable at %s: %s", path, type(exc).__name__)
        return None

    try:
        if not isinstance(raw, Mapping):
            raise ValueError("artifact is not an object")
        model_format = raw.get("format")
        if model_format not in SUPPORTED_FORMATS:
            raise ValueError("unsupported format")
        labels = [str(label) for label in raw["labels"]]
        if not labels or len(labels) > MAX_LABELS or len(set(labels)) != len(labels):
            raise ValueError("invalid label set")
        if model_format == MODEL_FORMAT and tuple(labels) != LABELS:
            raise ValueError("unexpected label set")
        vocabulary = raw["vocabulary"]
        log_priors = raw["log_priors"]
        log_likelihoods = raw["log_likelihoods"]
        unknown_log_likelihoods = raw["unknown_log_likelihoods"]
        if not isinstance(vocabulary, list) or not vocabulary or len(vocabulary) > MAX_FEATURES:
            raise ValueError("invalid vocabulary")
        if any(not isinstance(feature, str) or len(feature) > 300 for feature in vocabulary):
            raise ValueError("invalid feature")
        if len(set(vocabulary)) != len(vocabulary):
            raise ValueError("duplicate features")
        for label in labels:
            if not _finite(log_priors[label]):
                raise ValueError("invalid prior")
            values = log_likelihoods[label]
            if not isinstance(values, list) or len(values) != len(vocabulary):
                raise ValueError("invalid likelihood vector")
            if not all(_finite(value) for value in values):
                raise ValueError("invalid likelihood")
            if not _finite(unknown_log_likelihoods[label]):
                raise ValueError("invalid unknown likelihood")
        feature_spec = _validate_feature_spec(raw.get("feature_spec"))
        calibration = _validate_calibration(raw.get("calibration"))
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Rejected invalid text model at %s: %s", path, exc)
        return None

    prepared = dict(raw)
    prepared["labels"] = labels
    prepared["feature_spec"] = feature_spec
    prepared["calibration"] = calibration
    prepared["_feature_index"] = {feature: index for index, feature in enumerate(vocabulary)}
    return prepared


def _get_model(path: Path) -> Optional[Dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load_model(str(path.resolve()), stat.st_mtime_ns)


def _softmax(log_scores: Mapping[str, float], labels: Sequence[str]) -> Dict[str, float]:
    top = max(log_scores.values())
    values = {label: math.exp(log_scores[label] - top) for label in labels}
    total = sum(values.values())
    return {label: values[label] / total for label in labels}


def apply_calibration(value: float, calibration: Mapping[str, Any]) -> float:
    """Map a raw posterior onto an empirical reliability curve, if provided."""
    xs = calibration.get("x")
    ys = calibration.get("y")
    if not xs or not ys:
        return value
    if value <= xs[0]:
        return ys[0]
    if value >= xs[-1]:
        return ys[-1]
    for index in range(1, len(xs)):
        if value <= xs[index]:
            span = xs[index] - xs[index - 1]
            share = 0.0 if span <= 0 else (value - xs[index - 1]) / span
            return ys[index - 1] + share * (ys[index] - ys[index - 1])
    return ys[-1]


def classify_news_text(
    text: str,
    *,
    enabled: bool = True,
    path: Optional[Path] = None,
    min_tokens: int = 30,
    min_coverage: float = 0.10,
) -> Dict[str, Any]:
    """Classify text with the saved model without turning it into a verdict.

    A text-only class score cannot establish factual truth. Low token or feature
    coverage therefore produces an explicit unavailable state instead of an
    uncalibrated prior, and the caller remains responsible for keeping any
    resulting weight strictly advisory.
    """
    model_path = path or default_model_path()
    loaded = _get_model(model_path)
    tokenizer = (loaded or {}).get("feature_spec", {}).get("tokenizer") or LEGACY_TOKENIZER
    tokens = tokenize(text, tokenizer)
    language = detect_language(text)
    if not enabled:
        return {**_unavailable("disabled", token_count=len(tokens)), "language": language}
    if len(tokens) < min_tokens:
        return {**_unavailable("too_little_text", token_count=len(tokens)), "language": language}

    model = loaded
    if model is None:
        return {**_unavailable("model_unavailable", token_count=len(tokens)), "language": language}

    labels: List[str] = model["labels"]
    feature_spec = model["feature_spec"]
    feature_index = model["_feature_index"]
    features = extract_features(text, feature_spec)
    known_unigrams = sum(1 for token in tokens if "w:" + token in feature_index)
    token_coverage = known_unigrams / len(tokens)
    if known_unigrams < 3 or token_coverage < min_coverage:
        return {
            **_unavailable("out_of_distribution", token_count=len(tokens)),
            "language": language,
            "matched_tokens": known_unigrams,
            "token_coverage": round(token_coverage, 4),
            "model_version": model.get("model_version", MODEL_VERSION),
        }

    log_scores: Dict[str, float] = {}
    for label in labels:
        score = float(model["log_priors"][label])
        likelihoods = model["log_likelihoods"][label]
        for feature, count in features.items():
            index = feature_index.get(feature)
            if index is not None:
                score += count * float(likelihoods[index])
        log_scores[label] = score

    probabilities = _softmax(log_scores, labels)
    predicted = max(labels, key=probabilities.get)
    false_probability = float(probabilities.get("false", 0.0))
    calibration = model.get("calibration") or {}
    risk_score = apply_calibration(false_probability, calibration) if calibration else false_probability

    dataset = model.get("dataset", {})
    deployment = model.get("deployment", {})
    evaluation = model.get("evaluation", {})
    return {
        "available": True,
        "applied": False,
        "reason": "ok",
        "model_version": model.get("model_version", MODEL_VERSION),
        "algorithm": model.get("algorithm", "multinomial_naive_bayes"),
        "dataset": {
            "name": dataset.get("name", "LIAR"),
            "text_scope": dataset.get("text_scope", "fact-check claims"),
        },
        "language": language,
        "languages_evaluated": list(model.get("languages", []) or []),
        "feature_spec": feature_spec,
        "token_count": len(tokens),
        "matched_tokens": known_unigrams,
        "token_coverage": round(token_coverage, 4),
        "prediction": predicted,
        "probabilities": {label: round(probabilities[label], 4) for label in labels},
        # Raw (uncalibrated) posterior for the false class.
        "false_probability": round(false_probability, 4),
        # The score callers should threshold on: calibrated when the artifact
        # ships a reliability curve, otherwise the raw posterior.
        "risk_score": round(risk_score, 4),
        "calibrated": bool(calibration),
        # The trainer sets this only after every gate condition passes. Missing
        # deployment metadata is intentionally treated as ineligible so a new or
        # partial artifact can never silently influence scoring.
        "scoring_eligible": bool(deployment.get("eligible_for_scoring", False)),
        "deployment_gate": {
            "risk_threshold": deployment.get("risk_threshold"),
            "validation_precision": deployment.get("validation_precision"),
            "minimum_validation_precision": deployment.get("minimum_validation_precision"),
            "failed_conditions": list(deployment.get("failed_conditions", []) or []),
            "decision": deployment.get("decision"),
        },
        "evaluation": {
            "validation": evaluation.get("validation", {}),
            "test": evaluation.get("test", {}),
        },
    }
