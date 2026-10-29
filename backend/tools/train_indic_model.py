"""Train Veritas's domain-matched Indian-news text-risk classifier.

The model is a standard-library multinomial Naive Bayes over sparse word and
character n-grams. It replaces the LIAR claim-level baseline as the *advisory*
text signal: it is trained on Indian news text (English headlines and full Hindi
articles), keeps the source corpora's binary label scheme instead of inventing a
third class, and is evaluated with grouped, temporal and outlet-held-out splits
rather than a single random split.

Example (from ``backend/``)::

    python tools/train_indic_model.py --download

Design guarantees:

* No random numbers anywhere. Every partition and ordering is derived from
  stable BLAKE2b/sha256 digests, so "reproducible" does not depend on a seed.
* No pickle and no executable payloads: the artifact is plain validated JSON.
* Duplicate and near-duplicate items are clustered and whole clusters are
  assigned to a single split, so leakage is structurally impossible rather than
  merely measured.
* Outlet, date, topic and image columns are never converted into features; only
  headline/statement text is learned. Outlet identity is therefore only able to
  influence results through shared language, which the outlet-held-out split
  exists to expose.
* Nothing is fabricated. If an evaluation is not supportable by the data (for
  example a temporal split with too few FALSE examples), it is reported as such
  and the deployment gate treats it as unmet.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.text_model import (  # noqa: E402
    MODEL_FORMAT_V2,
    UNICODE_TOKENIZER,
    apply_calibration,
    extract_features,
)
from tools.indic_dataset import (  # noqa: E402
    CORPORA,
    DEFAULT_DATA_DIR,
    Example,
    describe,
    load_all,
    outlet_partition,
    publisher_terms,
    sha256_file,
    split_by_group,
    strip_shared_groups,
    temporal_partition,
)

MODEL_VERSION = "indic-news-mnb-v2"
LABELS: Tuple[str, ...] = ("false", "supported")
DEFAULT_OUTPUT = BACKEND_DIR / "models" / "indic_news_mnb_v2.json"
DEFAULT_RISK_THRESHOLD = 0.80
THRESHOLD_GRID: Tuple[float, ...] = (0.50, 0.60, 0.70, 0.80, 0.90)

# Label-scheme note carried into the artifact so nobody has to guess later.
LABEL_SCHEME = {
    "labels": list(LABELS),
    "mapping": {
        "ifnd": {"true": "supported", "fake": "false"},
        "hindi_news": {"real": "supported", "fake": "false"},
    },
    "mixed_class": (
        "not represented: neither corpus supports a trustworthy MIXED/MISLEADING class. "
        "IFND's own label column is binary TRUE/Fake, and its MISLEADING *topic* rows are "
        "already labelled Fake by the dataset authors."
    ),
    "ai_generated": (
        "not modelled: AI-text detection is a separate signal. These corpora contain no "
        "AI-generation labels, so this model makes no claim about AI-written news."
    ),
}

FEATURE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "word12": {
        "word_ngrams": [1, 2],
        "char_ngrams": [],
        "char_limit": 0,
        "text_limit": 2000,
        "tokenizer": UNICODE_TOKENIZER,
    },
    "word12_char45": {
        "word_ngrams": [1, 2],
        "char_ngrams": [4, 5],
        "char_limit": 600,
        "text_limit": 2000,
        "tokenizer": UNICODE_TOKENIZER,
    },
    "word1_char45": {
        "word_ngrams": [1],
        "char_ngrams": [4, 5],
        "char_limit": 600,
        "text_limit": 2000,
        "tokenizer": UNICODE_TOKENIZER,
    },
}

# Publisher tokens that must never act as hidden shortcuts. Populated from the
# corpora's own outlet column so the audit list cannot drift from the data.
OUTLET_TOKENS: set = set()
# Redaction list applied to the word feature space (curated brands + outlets).
REDACTION_TERMS: List[str] = []


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


# -- Training primitives ------------------------------------------------------
def build_vocabulary(
    examples: Sequence[Example],
    spec: Mapping[str, Any],
    *,
    max_features: int,
    min_df: int = 2,
) -> List[str]:
    """Rank features by document frequency, with a stable tie-break."""
    document_frequency: Counter = Counter()
    for example in examples:
        document_frequency.update(extract_features(example.text, spec).keys())
    ranked = sorted(
        document_frequency.items(),
        key=lambda item: (-item[1], item[0]),
    )
    vocabulary = [feature for feature, count in ranked if count >= min_df]
    if not vocabulary:
        vocabulary = [feature for feature, _count in ranked]
    return vocabulary[:max_features]


def accumulate_counts(
    examples: Sequence[Example],
    spec: Mapping[str, Any],
    vocabulary: Sequence[str],
) -> Tuple[Counter, Dict[str, Counter], Counter]:
    """Aggregate class x feature counts over the training split."""
    known = set(vocabulary)
    class_documents: Counter = Counter()
    class_feature_counts: Dict[str, Counter] = {label: Counter() for label in LABELS}
    class_totals: Counter = Counter()
    for example in examples:
        if example.label not in class_feature_counts:
            raise ValueError(f"unexpected label {example.label!r}")
        class_documents[example.label] += 1
        features = extract_features(example.text, spec)
        filtered = Counter({key: value for key, value in features.items() if key in known})
        class_feature_counts[example.label].update(filtered)
        class_totals[example.label] += sum(filtered.values())
    return class_documents, class_feature_counts, class_totals


def build_model(
    vocabulary: Sequence[str],
    class_documents: Mapping[str, int],
    class_feature_counts: Mapping[str, Counter],
    class_totals: Mapping[str, int],
    *,
    alpha: float,
) -> Dict[str, Any]:
    """Turn aggregated counts into log priors and log likelihoods."""
    if alpha <= 0:
        raise ValueError("alpha must be greater than zero")
    missing = [label for label in LABELS if not class_documents.get(label)]
    if missing:
        raise ValueError(f"training data is missing labels: {missing}")
    total_documents = sum(class_documents[label] for label in LABELS)
    vocabulary_size = len(vocabulary)
    log_priors = {label: math.log(class_documents[label] / total_documents) for label in LABELS}
    log_likelihoods: Dict[str, List[float]] = {}
    unknown: Dict[str, float] = {}
    for label in LABELS:
        denominator = class_totals[label] + alpha * vocabulary_size
        unknown[label] = round(math.log(alpha / denominator), 6)
        counts = class_feature_counts[label]
        log_likelihoods[label] = [
            round(math.log((counts[feature] + alpha) / denominator), 6) for feature in vocabulary
        ]
    return {
        "log_priors": {label: round(value, 8) for label, value in log_priors.items()},
        "log_likelihoods": log_likelihoods,
        "unknown_log_likelihoods": unknown,
    }


def score_examples(
    examples: Sequence[Example],
    spec: Mapping[str, Any],
    feature_index: Mapping[str, int],
    model: Mapping[str, Any],
) -> List[float]:
    """Return the raw posterior of the ``false`` class for each example."""
    scores: List[float] = []
    priors = model["log_priors"]
    likelihoods = model["log_likelihoods"]
    for example in examples:
        features = extract_features(example.text, spec)
        log_scores: Dict[str, float] = {}
        for label in LABELS:
            total = float(priors[label])
            vector = likelihoods[label]
            for feature, count in features.items():
                index = feature_index.get(feature)
                if index is not None:
                    total += count * float(vector[index])
            log_scores[label] = total
        top = max(log_scores.values())
        exponentials = {label: math.exp(value - top) for label, value in log_scores.items()}
        denominator = sum(exponentials.values())
        scores.append(exponentials["false"] / denominator)
    return scores


# -- Metrics ------------------------------------------------------------------
def confusion(
    truth: Sequence[str], predicted: Sequence[str], label: str
) -> Tuple[int, int, int]:
    true_positive = sum(1 for actual, guess in zip(truth, predicted) if actual == label and guess == label)
    false_positive = sum(1 for actual, guess in zip(truth, predicted) if actual != label and guess == label)
    false_negative = sum(1 for actual, guess in zip(truth, predicted) if actual == label and guess != label)
    return true_positive, false_positive, false_negative


def prf(true_positive: int, false_positive: int, false_negative: int) -> Dict[str, float]:
    precision = _safe_divide(true_positive, true_positive + false_positive)
    recall = _safe_divide(true_positive, true_positive + false_negative)
    f1 = _safe_divide(2 * precision * recall, precision + recall)
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def classification_metrics(
    truth: Sequence[str], predicted: Sequence[str], *, labels: Sequence[str] = LABELS
) -> Dict[str, Any]:
    per_label: Dict[str, Any] = {}
    for label in labels:
        counts = confusion(truth, predicted, label)
        per_label[label] = {
            **prf(*counts),
            "support": sum(1 for actual in truth if actual == label),
        }
    matrix = {actual: {guess: 0 for guess in labels} for actual in labels}
    for actual, guess in zip(truth, predicted):
        matrix[actual][guess] += 1
    accuracy = _safe_divide(sum(1 for a, b in zip(truth, predicted) if a == b), len(truth))
    return {
        "examples": len(truth),
        "accuracy": round(accuracy, 4),
        "macro_precision": round(sum(m["precision"] for m in per_label.values()) / len(labels), 4),
        "macro_recall": round(sum(m["recall"] for m in per_label.values()) / len(labels), 4),
        "macro_f1": round(sum(m["f1"] for m in per_label.values()) / len(labels), 4),
        "per_label": per_label,
        "confusion_matrix": matrix,
    }


def threshold_table(
    truth: Sequence[str], scores: Sequence[float], *, thresholds: Sequence[float] = THRESHOLD_GRID
) -> Dict[str, Dict[str, Any]]:
    """Flag rate, precision and recall at each candidate FALSE threshold."""
    table: Dict[str, Dict[str, Any]] = {}
    for threshold in thresholds:
        flagged = [index for index, score in enumerate(scores) if score >= threshold]
        true_positive = sum(1 for index in flagged if truth[index] == "false")
        false_positive = len(flagged) - true_positive
        false_negatives = sum(1 for actual in truth if actual == "false") - true_positive
        entry = {
            "flagged": len(flagged),
            "flag_rate": round(_safe_divide(len(flagged), len(truth)), 4),
            "false_support": sum(1 for actual in truth if actual == "false"),
            **prf(true_positive, false_positive, false_negatives),
        }
        table[f"{threshold:.2f}"] = entry
    return table


def fit_calibration(
    truth: Sequence[str], scores: Sequence[float], *, bins: int = 10, min_bin: int = 40
) -> Dict[str, Any]:
    """Fit a monotone piecewise-linear reliability map for the FALSE score.

    Knots come from equal-width bin centres and their observed FALSE rate, then
    adjacent violators are pooled so the map can never be non-monotone.
    """
    buckets: List[List[int]] = [[] for _ in range(bins)]
    for index, score in enumerate(scores):
        position = min(bins - 1, max(0, int(score * bins)))
        buckets[position].append(index)
    centres: List[float] = []
    observed: List[float] = []
    for position, indices in enumerate(buckets):
        if len(indices) < min_bin:
            continue
        rate = sum(1 for index in indices if truth[index] == "false") / len(indices)
        centres.append(round((position + 0.5) / bins, 6))
        observed.append(rate)
    if len(centres) < 3:
        return {}
    # Pool adjacent violators to enforce monotonicity.
    pooled_x = [centres[0]]
    pooled_y = [observed[0]]
    weights = [1.0]
    for x_value, y_value in zip(centres[1:], observed[1:]):
        pooled_x.append(x_value)
        pooled_y.append(y_value)
        weights.append(1.0)
        while len(pooled_y) > 1 and pooled_y[-2] > pooled_y[-1]:
            left_weight, right_weight = weights[-2], weights[-1]
            total = left_weight + right_weight
            merged = (pooled_y[-2] * left_weight + pooled_y[-1] * right_weight) / total
            pooled_y[-2] = merged
            weights[-2] = total
            pooled_x[-2] = (pooled_x[-2] + pooled_x[-1]) / 2.0
            pooled_x.pop()
            pooled_y.pop()
            weights.pop()
    knots_x = [0.0] + [round(value, 6) for value in pooled_x] + [1.0]
    knots_y = [0.0] + [round(min(1.0, max(0.0, value)), 6) for value in pooled_y] + [1.0]
    dedup_x: List[float] = []
    dedup_y: List[float] = []
    for x_value, y_value in zip(knots_x, knots_y):
        if dedup_x and x_value <= dedup_x[-1]:
            dedup_y[-1] = max(dedup_y[-1], y_value)
            continue
        dedup_x.append(x_value)
        dedup_y.append(y_value)
    if len(dedup_x) < 3:
        return {}
    return {"method": "piecewise_linear", "x": dedup_x, "y": dedup_y}


def expected_calibration_error(
    truth: Sequence[str], scores: Sequence[float], *, bins: int = 10
) -> float:
    total = len(scores)
    if not total:
        return 0.0
    error = 0.0
    for position in range(bins):
        indices = [
            index
            for index, score in enumerate(scores)
            if min(bins - 1, max(0, int(score * bins))) == position
        ]
        if not indices:
            continue
        observed = sum(1 for index in indices if truth[index] == "false") / len(indices)
        confidence = sum(scores[index] for index in indices) / len(indices)
        error += (len(indices) / total) * abs(observed - confidence)
    return round(error, 4)


def brier_score(truth: Sequence[str], scores: Sequence[float]) -> float:
    if not scores:
        return 0.0
    return round(
        sum(((1.0 if actual == "false" else 0.0) - score) ** 2 for actual, score in zip(truth, scores))
        / len(scores),
        4,
    )


def top_features(model: Mapping[str, Any], vocabulary: Sequence[str], *, count: int = 30) -> List[Dict[str, Any]]:
    """Most FALSE-indicative features by log-ratio, for auditing."""
    false_vector = model["log_likelihoods"]["false"]
    supported_vector = model["log_likelihoods"]["supported"]
    ranked = sorted(
        range(len(vocabulary)),
        key=lambda index: (supported_vector[index] - false_vector[index], vocabulary[index]),
        reverse=True,
    )
    return [
        {
            "feature": vocabulary[index],
            "log_ratio_false_over_supported": round(
                false_vector[index] - supported_vector[index], 4
            ),
        }
        for index in ranked[:count]
    ]


def outlet_shortcut_audit(vocabulary: Sequence[str]) -> Dict[str, Any]:
    """Check whether publisher names leaked into the learned word feature set."""
    flagged = []
    for feature in vocabulary:
        if not (feature.startswith("w:") or feature.startswith("b:")):
            continue
        bare = feature.split(":", 1)[-1].replace(" ", "")
        if bare and bare in OUTLET_TOKENS:
            flagged.append(feature)
    return {
        "outlet_tokens_checked": len(OUTLET_TOKENS),
        "features_matching_outlet_names": len(flagged),
        "examples": sorted(flagged)[:20],
    }


def publisher_redaction_audit(vocabulary: Sequence[str]) -> Dict[str, Any]:
    """Confirm the redaction list stayed out of the learned word features."""
    blocked = set(REDACTION_TERMS)
    remaining = [
        feature
        for feature in vocabulary
        if (feature.startswith("w:") or feature.startswith("b:"))
        and feature.split(":", 1)[-1].replace(" ", "") in blocked
    ]
    return {
        "enabled": bool(REDACTION_TERMS),
        "terms": len(REDACTION_TERMS),
        "features_still_present": len(remaining),
        "examples": sorted(remaining)[:20],
    }


def outlet_label_correlation(examples: Sequence[Example]) -> Dict[str, Any]:
    """Quantify how strongly outlet identity predicts the label in this data."""
    totals: Counter = Counter()
    false_counts: Counter = Counter()
    for example in examples:
        if not example.outlet:
            continue
        totals[example.outlet] += 1
        if example.label == "false":
            false_counts[example.outlet] += 1
    table = {
        outlet: {
            "items": totals[outlet],
            "false_rate": round(false_counts[outlet] / totals[outlet], 4),
        }
        for outlet in totals
    }
    dominant = max(table.items(), key=lambda item: item[1]["false_rate"], default=(None, None))
    support_share = _safe_divide(
        max(false_counts.values()) if false_counts else 0,
        sum(false_counts.values()),
    )
    return {
        "outlets": len(table),
        "highest_false_rate": {"outlet": dominant[0], **(dominant[1] or {})},
        "largest_single_outlet_share_of_false": round(support_share, 4),
        "table": dict(sorted(table.items())),
    }


# -- Orchestration ------------------------------------------------------------
def _evaluate(
    name: str,
    examples: Sequence[Example],
    spec: Mapping[str, Any],
    feature_index: Mapping[str, int],
    model: Mapping[str, Any],
) -> Dict[str, Any]:
    truth = [example.label for example in examples]
    scores = score_examples(examples, spec, feature_index, model)
    predictions = ["false" if score >= DEFAULT_RISK_THRESHOLD else "supported" for score in scores]
    return {
        "name": name,
        "metrics": classification_metrics(truth, predictions),
        "thresholds": threshold_table(truth, scores),
        "calibration": {
            "expected_calibration_error": expected_calibration_error(truth, scores),
            "brier_score": brier_score(truth, scores),
        },
        "_truth": truth,
        "_scores": scores,
    }


def _per_language(examples: Sequence[Example], spec, feature_index, model) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for language in sorted({example.language for example in examples}):
        subset = [example for example in examples if example.language == language]
        if len(subset) < 40:
            continue
        truth = [example.label for example in subset]
        scores = score_examples(subset, spec, feature_index, model)
        predictions = ["false" if score >= DEFAULT_RISK_THRESHOLD else "supported" for score in scores]
        report[language] = {
            "metrics": classification_metrics(truth, predictions),
            "thresholds": threshold_table(truth, scores),
        }
    return report


def train(args: argparse.Namespace) -> Dict[str, Any]:
    examples, load_meta = load_all(
        args.data_dir,
        download=args.download,
        verify=not args.no_verify,
    )
    if not examples:
        raise SystemExit("no training examples were loaded")

    global OUTLET_TOKENS, REDACTION_TERMS
    OUTLET_TOKENS = {
        example.outlet.replace(" ", "").lower() for example in examples if example.outlet
    }
    REDACTION_TERMS = [] if args.no_publisher_redaction else publisher_terms(examples)
    feature_configs = _specs(bool(REDACTION_TERMS))

    splits = split_by_group(examples)
    train_rows, validation_rows, test_rows = (
        splits["train"],
        splits["validation"],
        splits["test"],
    )

    # -- Extra holdouts (IFND only: the Hindi corpus has no outlet or date) ---
    ifnd_rows = [example for example in examples if example.corpus == "ifnd"]
    temporal_train, temporal_test, temporal_cutoff = temporal_partition(ifnd_rows)
    temporal_test, temporal_dropped = strip_shared_groups(train_rows, temporal_test)
    ifnd_outlet_train, ifnd_outlet_test, outlet_holdout = outlet_partition(ifnd_rows)
    outlet_test, outlet_dropped = strip_shared_groups(train_rows, ifnd_outlet_test)

    corpus_stats = {
        corpus_id: describe([example for example in examples if example.corpus == corpus_id])
        for corpus_id in CORPORA
    }

    # -- Feature / smoothing selection on the validation split ---------------
    selection_alpha = args.alpha
    candidates: List[Dict[str, Any]] = []
    chosen_spec: Optional[Mapping[str, Any]] = None
    chosen_counts: Optional[Tuple[Any, Any, Any, List[str]]] = None
    for config_name, spec in feature_configs.items():
        vocabulary = build_vocabulary(
            train_rows, spec, max_features=args.max_features, min_df=args.min_df
        )
        class_documents, class_counts, class_totals = accumulate_counts(train_rows, spec, vocabulary)
        model = build_model(vocabulary, class_documents, class_counts, class_totals, alpha=selection_alpha)
        feature_index = {feature: index for index, feature in enumerate(vocabulary)}
        validation = _evaluate("validation", validation_rows, spec, feature_index, model)
        candidates.append(
            {
                "config": config_name,
                "spec": spec,
                "features": len(vocabulary),
                "alpha": selection_alpha,
                "validation": validation["metrics"],
                "validation_false_at_risk_threshold": validation["thresholds"][
                    f"{DEFAULT_RISK_THRESHOLD:.2f}"
                ],
            }
        )
        if chosen_spec is None or _better(candidates[-1], candidates[-2]):
            chosen_spec = spec
            chosen_counts = (class_documents, class_counts, class_totals, vocabulary)

    assert chosen_spec is not None and chosen_counts is not None
    chosen_config = next(item["config"] for item in candidates if item["spec"] == chosen_spec)

    # -- Smoothing sweep on the winning feature configuration -----------------
    sweep: List[Dict[str, Any]] = []
    best_alpha = selection_alpha
    best_entry: Optional[Dict[str, Any]] = None
    for alpha in args.alpha_grid:
        model = build_model(
            chosen_counts[3], chosen_counts[0], chosen_counts[1], chosen_counts[2], alpha=alpha
        )
        feature_index = {feature: index for index, feature in enumerate(chosen_counts[3])}
        validation = _evaluate("validation", validation_rows, chosen_spec, feature_index, model)
        entry = {
            "alpha": alpha,
            "features": len(chosen_counts[3]),
            "validation": validation["metrics"],
            "validation_false_at_risk_threshold": validation["thresholds"][
                f"{DEFAULT_RISK_THRESHOLD:.2f}"
            ],
        }
        sweep.append(entry)
        if best_entry is None or _better(entry, best_entry):
            best_alpha, best_entry = alpha, entry

    vocabulary = chosen_counts[3]
    final_model = build_model(
        vocabulary, chosen_counts[0], chosen_counts[1], chosen_counts[2], alpha=best_alpha
    )
    feature_index = {feature: index for index, feature in enumerate(vocabulary)}

    evaluations = {
        "validation": _evaluate("validation", validation_rows, chosen_spec, feature_index, final_model),
        "test": _evaluate("test", test_rows, chosen_spec, feature_index, final_model),
        "temporal_holdout": _evaluate(
            "temporal_holdout", temporal_test, chosen_spec, feature_index, final_model
        ),
        "outlet_holdout": _evaluate(
            "outlet_holdout", outlet_test, chosen_spec, feature_index, final_model
        ),
    }
    per_language = _per_language(test_rows, chosen_spec, feature_index, final_model)

    # -- Calibration fitted on validation, checked on test -------------------
    calibration = fit_calibration(evaluations["validation"]["_truth"], evaluations["validation"]["_scores"])
    calibration_report: Dict[str, Any] = {"fitted_on": "validation", "map": bool(calibration)}
    if calibration:
        for split_name in ("validation", "test"):
            raw = evaluations[split_name]["_scores"]
            calibrated = [apply_calibration(score, calibration) for score in raw]
            truth = evaluations[split_name]["_truth"]
            calibration_report[split_name] = {
                "ece_before": expected_calibration_error(truth, raw),
                "ece_after": expected_calibration_error(truth, calibrated),
                "brier_before": brier_score(truth, raw),
                "brier_after": brier_score(truth, calibrated),
                "mean_raw_score": round(sum(raw) / len(raw), 4),
                "mean_calibrated_score": round(sum(calibrated) / len(calibrated), 4),
                "observed_false_rate": round(
                    sum(1 for value in truth if value == "false") / len(truth), 4
                ),
            }
    else:
        calibration_report["note"] = "too few populated validation bins to fit a reliability curve"

    # -- Leakage audit -------------------------------------------------------
    leakage = {
        "group_overlap": {
            "validation": len(
                {example.group for example in train_rows} & {example.group for example in validation_rows}
            ),
            "test": len(
                {example.group for example in train_rows} & {example.group for example in test_rows}
            ),
            "temporal_holdout": len(
                {example.group for example in train_rows} & {example.group for example in temporal_test}
            ),
            "outlet_holdout": len(
                {example.group for example in train_rows} & {example.group for example in outlet_test}
            ),
            "temporal_rows_dropped_for_group_overlap": temporal_dropped,
            "outlet_rows_dropped_for_group_overlap": outlet_dropped,
        },
        "grouping": load_meta["grouping"],
        "metadata_fields_excluded_from_features": [
            "Web (outlet)",
            "Date (publication month)",
            "Category (topic)",
            "Image (media URL)",
            "id",
        ],
        "metadata_feature_prefixes_absent": _metadata_prefix_audit(vocabulary),
        "outlet_shortcut": outlet_shortcut_audit(vocabulary),
        "publisher_redaction": publisher_redaction_audit(vocabulary),
        "outlet_label_correlation": outlet_label_correlation(train_rows),
        "temporal_cutoff": temporal_cutoff,
        "outlet_holdout": outlet_holdout,
    }

    # -- Deployment gate -----------------------------------------------------
    gate = evaluate_gate(
        validation=evaluations["validation"],
        test=evaluations["test"],
        temporal=evaluations["temporal_holdout"],
        outlet=evaluations["outlet_holdout"],
        leakage=leakage,
        risk_threshold=DEFAULT_RISK_THRESHOLD,
        minimum_validation_precision=args.minimum_validation_precision,
        minimum_validation_flags=args.minimum_validation_flags,
        minimum_test_precision=args.minimum_test_precision,
        minimum_holdout_precision=args.minimum_holdout_precision,
        minimum_holdout_false_support=args.minimum_holdout_false_support,
    )

    dataset_meta = {
        "name": " + ".join(CORPORA[corpus_id]["name"] for corpus_id in CORPORA),
        "version": "2024-06 (Hindi Zenodo record) / IFND release as mirrored",
        "text_scope": "; ".join(
            f"{corpus_id}: {CORPORA[corpus_id]['text_scope']}" for corpus_id in CORPORA
        ),
        "languages": sorted({CORPORA[corpus_id]["language"] for corpus_id in CORPORA}),
        "provenance": {
            corpus_id: {
                "name": CORPORA[corpus_id]["name"],
                "url": CORPORA[corpus_id]["url"],
                "distribution": CORPORA[corpus_id]["distribution"],
                "citation": CORPORA[corpus_id]["citation"],
                "sha256": CORPORA[corpus_id]["sha256"],
                "license_notice": CORPORA[corpus_id]["license_notice"],
                "downloaded_sha256": load_meta["files"].get(corpus_id, {}).get("sha256"),
                "text_scope": CORPORA[corpus_id]["text_scope"],
                "outlet_available": CORPORA[corpus_id]["has_outlet"],
                "date_available": CORPORA[corpus_id]["has_date"],
            }
            for corpus_id in CORPORA
        },
    }

    artifact = {
        "format": MODEL_FORMAT_V2,
        "model_version": MODEL_VERSION,
        "algorithm": "multinomial_naive_bayes",
        "labels": list(LABELS),
        "label_scheme": LABEL_SCHEME,
        "languages": sorted({example.language for example in examples}),
        "feature_spec": dict(chosen_spec),
        "vocabulary": vocabulary,
        "log_priors": final_model["log_priors"],
        "log_likelihoods": final_model["log_likelihoods"],
        "unknown_log_likelihoods": final_model["unknown_log_likelihoods"],
        "calibration": calibration,
        "hyperparameters": {
            "alpha": best_alpha,
            "max_features": args.max_features,
            "min_document_frequency": args.min_df,
            "feature_config": chosen_config,
            "tie_break": "document frequency desc, then feature string asc",
        },
        "vocabulary_size": len(vocabulary),
        "training": {
            "examples": len(train_rows),
            "class_examples": dict(
                sorted(Counter(example.label for example in train_rows).items())
            ),
            "corpus_breakdown": dict(
                sorted(Counter(example.corpus for example in train_rows).items())
            ),
            "language_breakdown": dict(
                sorted(Counter(example.language for example in train_rows).items())
            ),
        },
        "splits": {
            "train": len(train_rows),
            "validation": len(validation_rows),
            "test": len(test_rows),
            "temporal_holdout": len(temporal_test),
            "temporal_train_window": len(temporal_train),
            "outlet_holdout": len(outlet_test),
            "outlet_train_pool": len(ifnd_outlet_train),
        },
        "dataset": dataset_meta,
        "dataset_split_stats": {
            "all": describe(examples),
            **corpus_stats,
        },
        "evaluation": {
            split_name: {
                "metrics": payload["metrics"],
                "thresholds": payload["thresholds"],
                "calibration": payload["calibration"],
            }
            for split_name, payload in evaluations.items()
        },
        "per_language": per_language,
        "calibration_report": calibration_report,
        "leakage_checks": leakage,
        "feature_ablation": candidates,
        "smoothing_sweep": sweep,
        "audit": {
            "top_false_indicative_features": top_features(final_model, vocabulary),
        },
        "deployment": gate,
        "reproducible_build": True,
        "reproducibility": {
            "command": "cd backend && python tools/train_indic_model.py --download",
            "randomness": "none - every partition and ordering uses stable cryptographic hashes",
            "training_date": args.training_date,
            "training_date_is_the_only_non_deterministic_field": True,
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    return {
        "output": str(output),
        "output_sha256": sha256_file(output),
        "output_bytes": output.stat().st_size,
        "model_version": MODEL_VERSION,
        "feature_config": chosen_config,
        "alpha": best_alpha,
        "vocabulary_size": len(vocabulary),
        "splits": artifact["splits"],
        "dataset_stats": artifact["dataset_split_stats"],
        "feature_ablation": [
            {
                "config": item["config"],
                "features": item["features"],
                "validation_accuracy": item["validation"]["accuracy"],
                "validation_macro_f1": item["validation"]["macro_f1"],
                "validation_false": item["validation_false_at_risk_threshold"],
            }
            for item in candidates
        ],
        "smoothing_sweep": [
            {
                "alpha": item["alpha"],
                "validation_accuracy": item["validation"]["accuracy"],
                "validation_macro_f1": item["validation"]["macro_f1"],
                "validation_false": item["validation_false_at_risk_threshold"],
            }
            for item in sweep
        ],
        "evaluation": {
            "validation": artifact["evaluation"]["validation"]["metrics"],
            "test": artifact["evaluation"]["test"]["metrics"],
            "temporal_holdout": artifact["evaluation"]["temporal_holdout"]["metrics"],
            "outlet_holdout": artifact["evaluation"]["outlet_holdout"]["metrics"],
        },
        "thresholds": {
            split_name: artifact["evaluation"][split_name]["thresholds"]
            for split_name in ("validation", "test", "temporal_holdout", "outlet_holdout")
        },
        "per_language": {
            language: payload["metrics"] for language, payload in per_language.items()
        },
        "calibration_report": calibration_report,
        "leakage_summary": {
            "group_overlap": leakage["group_overlap"],
            "metadata_feature_prefixes_absent": leakage["metadata_feature_prefixes_absent"],
            "outlet_shortcut": leakage["outlet_shortcut"],
            "outlet_label_correlation": {
                "outlets": leakage["outlet_label_correlation"]["outlets"],
                "highest_false_rate": leakage["outlet_label_correlation"]["highest_false_rate"],
                "largest_single_outlet_share_of_false": leakage["outlet_label_correlation"][
                    "largest_single_outlet_share_of_false"
                ],
            },
        },
        "deployment": gate,
    }


def _better(candidate: Mapping[str, Any], incumbent: Mapping[str, Any]) -> bool:
    """Prefer the higher FALSE operating point at sufficient volume, then F1."""
    def key(entry: Mapping[str, Any]) -> Tuple[int, float, float, float]:
        false = entry["validation_false_at_risk_threshold"]
        volume_ok = 1 if false["flagged"] >= 25 else 0
        return (volume_ok, false["precision"], false["recall"], entry["validation"]["macro_f1"])

    return key(candidate) > key(incumbent)


def _specs(redact: bool) -> Dict[str, Dict[str, Any]]:
    """Copy the feature configs and attach the redaction list (or not)."""
    return {
        name: {**config, "redacted_terms": list(REDACTION_TERMS) if redact else []}
        for name, config in FEATURE_CONFIGS.items()
    }


def _metadata_prefix_audit(vocabulary: Sequence[str]) -> Dict[str, Any]:
    banned = ("web:", "date:", "cat:", "img:", "url:", "domain:", "outlet:")
    found = [feature for feature in vocabulary if feature.lower().startswith(banned)]
    return {"banned_prefixes": list(banned), "features_found": len(found), "examples": found[:10]}


def evaluate_gate(
    *,
    validation: Mapping[str, Any],
    test: Mapping[str, Any],
    temporal: Mapping[str, Any],
    outlet: Mapping[str, Any],
    leakage: Mapping[str, Any],
    risk_threshold: float,
    minimum_validation_precision: float,
    minimum_validation_flags: int,
    minimum_test_precision: float,
    minimum_holdout_precision: float,
    minimum_holdout_false_support: int,
) -> Dict[str, Any]:
    """Apply the documented deployment gate. Never relax it to pass."""
    key = f"{risk_threshold:.2f}"
    validation_point = validation["thresholds"][key]
    test_point = test["thresholds"][key]
    temporal_point = temporal["thresholds"][key]
    outlet_point = outlet["thresholds"][key]
    conditions = [
        {
            "name": "validation_false_precision",
            "passed": validation_point["precision"] >= minimum_validation_precision,
            "detail": {"value": validation_point["precision"], "required": minimum_validation_precision},
        },
        {
            "name": "validation_flagged_volume",
            "passed": validation_point["flagged"] >= minimum_validation_flags,
            "detail": {"value": validation_point["flagged"], "required": minimum_validation_flags},
        },
        {
            "name": "test_false_precision_not_catastrophic",
            "passed": (not test["metrics"]["examples"]) or test_point["precision"] >= minimum_test_precision,
            "detail": {
                "value": test_point["precision"],
                "required": minimum_test_precision,
                "test_support": test["metrics"]["examples"],
            },
        },
        {
            "name": "temporal_false_support_sufficient",
            "passed": temporal_point["false_support"] >= minimum_holdout_false_support,
            "detail": {
                "value": temporal_point["false_support"],
                "required": minimum_holdout_false_support,
                "note": "IFND's date column has no year for most FALSE items, so this split is inherently small",
            },
        },
        {
            "name": "temporal_false_precision_acceptable",
            "passed": (
                temporal_point["false_support"] >= minimum_holdout_false_support
                and temporal_point["precision"] >= minimum_holdout_precision
            ),
            "detail": {
                "value": temporal_point["precision"],
                "required": minimum_holdout_precision,
                "false_support": temporal_point["false_support"],
            },
        },
        {
            "name": "outlet_holdout_false_support_sufficient",
            "passed": outlet_point["false_support"] >= minimum_holdout_false_support,
            "detail": {"value": outlet_point["false_support"], "required": minimum_holdout_false_support},
        },
        {
            "name": "outlet_holdout_false_precision_acceptable",
            "passed": (
                outlet_point["false_support"] >= minimum_holdout_false_support
                and outlet_point["precision"] >= minimum_holdout_precision
            ),
            "detail": {
                "value": outlet_point["precision"],
                "required": minimum_holdout_precision,
                "false_support": outlet_point["false_support"],
            },
        },
        {
            "name": "no_duplicate_or_near_duplicate_leakage",
            "passed": all(
                leakage["group_overlap"][split_name] == 0
                for split_name in (
                    "validation",
                    "test",
                    "temporal_holdout",
                    "outlet_holdout",
                )
            ),
            "detail": leakage["group_overlap"],
        },
        {
            "name": "no_metadata_feature_leakage",
            "passed": leakage["metadata_feature_prefixes_absent"]["features_found"] == 0,
            "detail": leakage["metadata_feature_prefixes_absent"],
        },
        {
            "name": "no_publisher_name_features",
            "passed": leakage["outlet_shortcut"]["features_matching_outlet_names"] == 0
            and leakage["publisher_redaction"]["features_still_present"] == 0,
            "detail": {
                "outlet_shortcut": leakage["outlet_shortcut"],
                "publisher_redaction": leakage["publisher_redaction"],
            },
        },
    ]
    failed = [condition["name"] for condition in conditions if not condition["passed"]]
    approved = not failed
    return {
        "risk_threshold": risk_threshold,
        "validation_precision": validation_point["precision"],
        "validation_flagged": validation_point["flagged"],
        "minimum_validation_precision": minimum_validation_precision,
        "minimum_validation_flags": minimum_validation_flags,
        "eligible_for_scoring": approved,
        "decision": "approved_for_risk_signal" if approved else "shadow_only",
        "conditions": conditions,
        "failed_conditions": failed,
        "reason": (
            "every documented gate condition passed"
            if approved
            else "failed: " + ", ".join(failed)
        ),
        "gate_policy": (
            "Gate is fixed before training and is never relaxed to make a model deployable. "
            "A model that fails any condition stays discovery-only and contributes zero weight."
        ),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--download", action="store_true", help="fetch any missing corpus")
    parser.add_argument("--no-verify", action="store_true", help="skip corpus sha256 verification")
    parser.add_argument(
        "--no-publisher-redaction",
        action="store_true",
        help=(
            "disable publisher/brand redaction (the documented pre-fix comparison run; "
            "it re-exposes the publisher shortcut and fails the leakage condition)"
        ),
    )
    parser.add_argument("--max-features", type=int, default=120_000)
    parser.add_argument("--min-df", type=int, default=2)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument(
        "--alpha-grid",
        type=float,
        nargs="+",
        default=[0.05, 0.2, 0.5, 1.0],
        help="smoothing values swept on the winning feature configuration",
    )
    parser.add_argument("--training-date", default="", help="ISO date recorded in the artifact")
    parser.add_argument("--minimum-validation-precision", type=float, default=0.70)
    parser.add_argument("--minimum-validation-flags", type=int, default=25)
    parser.add_argument("--minimum-test-precision", type=float, default=0.60)
    parser.add_argument("--minimum-holdout-precision", type=float, default=0.50)
    parser.add_argument("--minimum-holdout-false-support", type=int, default=50)
    parser.add_argument("--summary-out", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.max_features < 100:
        raise SystemExit("--max-features must be at least 100")
    if args.min_df < 1:
        raise SystemExit("--min-df must be at least 1")
    if not 0.0 < args.minimum_validation_precision <= 1.0:
        raise SystemExit("--minimum-validation-precision must be in (0, 1]")
    if not args.training_date:
        from datetime import date

        args.training_date = date.today().isoformat()
    summary = train(args)
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.summary_out:
        Path(args.summary_out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
