"""Train Veritas's dependency-free LIAR claim-text baseline.

This tool deliberately trains on the official train split only and reports the
unchanged validation/test splits.  It never uses speaker, party, venue, or
history metadata, which would leak information unavailable in a news article.

Example (from ``backend/``)::

    python tools/train_text_model.py --download

The downloaded corpus is ignored by Git.  The JSON model records source URLs,
file SHA-256 values, parameters, label mapping, and held-out metrics so another
developer can reproduce the same artifact without third-party ML packages.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.request import Request, urlopen
import zipfile


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.text_model import LABELS, MODEL_FORMAT, MODEL_VERSION, make_features, tokenize  # noqa: E402


DEFAULT_DATASET_URL = "https://www.cs.ucsb.edu/~william/data/liar_dataset.zip"
DATASET_NAME = "LIAR v1.0"
# Preserve the six public labels rather than flattening the ambiguous cases.
LABEL_MAP = {
    "pants-fire": "false",
    "false": "false",
    "barely-true": "mixed",
    "half-true": "mixed",
    "mostly-true": "supported",
    "true": "supported",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(destination: Path, url: str) -> None:
    """Download one input file with a timeout and atomic replacement."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "Veritas-training/1.0"})
    try:
        with urlopen(request, timeout=45) as response, temporary.open("wb") as output:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError(f"HTTP {response.status}")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(destination)


def download_liar_splits(destination: Path, dataset_url: str, *, refresh: bool) -> str:
    """Fetch and safely unpack the official LIAR archive.

    Only the three expected top-level TSV members are written. ``extractall``
    is intentionally avoided so an unexpected archive can never write outside
    the dedicated training cache.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "liar_dataset.zip"
    if refresh or not archive.exists():
        print(f"Downloading LIAR archive from {dataset_url}")
        download_file(archive, dataset_url)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = {Path(info.filename).name: info for info in bundle.infolist() if not info.is_dir()}
            for split in ("train", "valid", "test"):
                filename = f"{split}.tsv"
                info = members.get(filename)
                if info is None or info.file_size > 10 * 1024 * 1024:
                    raise ValueError(f"LIAR archive is missing a safe {filename} member")
                output = destination / filename
                temporary = output.with_suffix(".tsv.part")
                with bundle.open(info) as source, temporary.open("wb") as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                temporary.replace(output)
    except (OSError, zipfile.BadZipFile, ValueError):
        raise
    return sha256(archive)


def load_examples(path: Path) -> List[Tuple[str, str]]:
    """Load only LIAR's statement and rating columns; discard metadata."""
    examples: List[Tuple[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row_number, row in enumerate(reader, start=1):
            if len(row) < 3:
                raise ValueError(f"{path.name}:{row_number}: expected at least 3 TSV columns")
            source_label = row[1].strip().lower()
            text = row[2].strip()
            if source_label not in LABEL_MAP:
                raise ValueError(f"{path.name}:{row_number}: unknown label {source_label!r}")
            if text:
                examples.append((LABEL_MAP[source_label], text))
    if not examples:
        raise ValueError(f"{path} contains no usable examples")
    return examples


def _vocabulary(examples: Iterable[Tuple[str, str]], max_features: int) -> List[str]:
    document_frequency: Counter[str] = Counter()
    for _label, text in examples:
        document_frequency.update(make_features(tokenize(text)).keys())
    ranked = sorted(document_frequency.items(), key=lambda item: (-item[1], item[0]))
    return [feature for feature, _count in ranked[:max_features]]


def train_model(
    examples: Sequence[Tuple[str, str]], *, max_features: int, alpha: float
) -> Dict[str, Any]:
    if alpha <= 0:
        raise ValueError("alpha must be greater than zero")
    vocabulary = _vocabulary(examples, max_features)
    vocabulary_set = set(vocabulary)
    class_documents: Counter[str] = Counter()
    class_feature_counts: Dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    class_feature_totals: Counter[str] = Counter()

    for label, text in examples:
        class_documents[label] += 1
        features = make_features(tokenize(text))
        filtered = Counter({feature: count for feature, count in features.items() if feature in vocabulary_set})
        class_feature_counts[label].update(filtered)
        class_feature_totals[label] += sum(filtered.values())

    if any(class_documents[label] == 0 for label in LABELS):
        raise ValueError("all three labels must be represented in training data")

    total_documents = len(examples)
    vocabulary_size = len(vocabulary)
    log_priors = {
        label: math.log(class_documents[label] / total_documents)
        for label in LABELS
    }
    log_likelihoods: Dict[str, List[float]] = {}
    unknown_log_likelihoods: Dict[str, float] = {}
    for label in LABELS:
        denominator = class_feature_totals[label] + alpha * vocabulary_size
        unknown_log_likelihoods[label] = math.log(alpha / denominator)
        counts = class_feature_counts[label]
        log_likelihoods[label] = [
            math.log((counts[feature] + alpha) / denominator) for feature in vocabulary
        ]

    return {
        "format": MODEL_FORMAT,
        "model_version": MODEL_VERSION,
        "algorithm": "multinomial_naive_bayes",
        "labels": list(LABELS),
        "vocabulary": vocabulary,
        "log_priors": log_priors,
        "log_likelihoods": log_likelihoods,
        "unknown_log_likelihoods": unknown_log_likelihoods,
        "training": {
            "examples": total_documents,
            "class_examples": dict(class_documents),
            "max_features": max_features,
            "selected_features": vocabulary_size,
            "alpha": alpha,
            "text_columns": ["statement"],
            "excluded_columns": ["speaker", "party", "context", "credit_history", "subjects"],
            "label_mapping": LABEL_MAP,
        },
    }


def predict(model: Mapping[str, Any], text: str) -> Tuple[str, Dict[str, float]]:
    feature_index = {feature: index for index, feature in enumerate(model["vocabulary"])}
    features = make_features(tokenize(text))
    scores: Dict[str, float] = {}
    for label in LABELS:
        score = float(model["log_priors"][label])
        likelihoods = model["log_likelihoods"][label]
        for feature, count in features.items():
            index = feature_index.get(feature)
            if index is not None:
                score += count * float(likelihoods[index])
        scores[label] = score
    maximum = max(scores.values())
    exp_scores = {label: math.exp(score - maximum) for label, score in scores.items()}
    total = sum(exp_scores.values())
    probabilities = {label: exp_scores[label] / total for label in LABELS}
    return max(LABELS, key=probabilities.get), probabilities


def _safe_divide(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _prf(true_positive: int, false_positive: int, false_negative: int) -> Dict[str, float]:
    precision = _safe_divide(true_positive, true_positive + false_positive)
    recall = _safe_divide(true_positive, true_positive + false_negative)
    f1 = _safe_divide(2 * precision * recall, precision + recall)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def evaluate(model: Mapping[str, Any], examples: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    predictions = [predict(model, text) for _label, text in examples]
    labels = [label for label, _text in examples]
    per_label: Dict[str, Dict[str, float]] = {}
    for label in LABELS:
        true_positive = sum(1 for actual, (predicted, _p) in zip(labels, predictions) if actual == label and predicted == label)
        false_positive = sum(1 for actual, (predicted, _p) in zip(labels, predictions) if actual != label and predicted == label)
        false_negative = sum(1 for actual, (predicted, _p) in zip(labels, predictions) if actual == label and predicted != label)
        per_label[label] = {
            **_prf(true_positive, false_positive, false_negative),
            "support": sum(1 for actual in labels if actual == label),
        }
    accuracy = _safe_divide(sum(1 for actual, (predicted, _p) in zip(labels, predictions) if actual == predicted), len(labels))
    return {
        "examples": len(examples),
        "accuracy": round(accuracy, 4),
        "macro_precision": round(sum(metrics["precision"] for metrics in per_label.values()) / len(LABELS), 4),
        "macro_recall": round(sum(metrics["recall"] for metrics in per_label.values()) / len(LABELS), 4),
        "macro_f1": round(sum(metrics["f1"] for metrics in per_label.values()) / len(LABELS), 4),
        "per_label": per_label,
    }


def threshold_metrics(
    model: Mapping[str, Any], examples: Sequence[Tuple[str, str]], threshold: float
) -> Dict[str, Any]:
    true_positive = false_positive = false_negative = 0
    for actual, text in examples:
        _prediction, probabilities = predict(model, text)
        flagged = probabilities["false"] >= threshold
        is_false = actual == "false"
        if flagged and is_false:
            true_positive += 1
        elif flagged:
            false_positive += 1
        elif is_false:
            false_negative += 1
    return {
        "threshold": threshold,
        "flagged": true_positive + false_positive,
        "false_support": sum(1 for label, _text in examples if label == "false"),
        **_prf(true_positive, false_positive, false_negative),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BACKEND_DIR / ".model-training" / "liar")
    parser.add_argument("--output", type=Path, default=BACKEND_DIR / "models" / "liar_mnb_v1.json")
    parser.add_argument("--download", action="store_true", help="fetch the official LIAR archive when TSV inputs are missing")
    parser.add_argument("--refresh", action="store_true", help="redownload and repack TSV inputs from the official archive")
    parser.add_argument("--dataset-url", default=DEFAULT_DATASET_URL)
    parser.add_argument("--max-features", type=int, default=20_000)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--risk-threshold", type=float, default=0.80, help="report false-class operating point at this threshold")
    parser.add_argument("--minimum-validation-precision", type=float, default=0.70)
    parser.add_argument("--minimum-validation-flags", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_features < 100:
        raise SystemExit("--max-features must be at least 100")
    if not 0.0 < args.risk_threshold < 1.0:
        raise SystemExit("--risk-threshold must be between 0 and 1")
    if not 0.0 < args.minimum_validation_precision <= 1.0:
        raise SystemExit("--minimum-validation-precision must be in (0, 1]")
    if args.minimum_validation_flags < 1:
        raise SystemExit("--minimum-validation-flags must be at least 1")

    paths = {split: args.data_dir / f"{split}.tsv" for split in ("train", "valid", "test")}
    missing = [path for path in paths.values() if not path.exists()]
    archive_sha256 = ""
    if missing or (args.download and args.refresh):
        if not args.download:
            raise SystemExit(f"Missing {missing[0]}. Re-run with --download or provide --data-dir.")
        archive_sha256 = download_liar_splits(args.data_dir, args.dataset_url, refresh=args.refresh)
    elif (args.data_dir / "liar_dataset.zip").exists():
        archive_sha256 = sha256(args.data_dir / "liar_dataset.zip")

    training_examples = load_examples(paths["train"])
    validation_examples = load_examples(paths["valid"])
    test_examples = load_examples(paths["test"])
    model = train_model(training_examples, max_features=args.max_features, alpha=args.alpha)
    model["dataset"] = {
        "name": DATASET_NAME,
        "original_url": args.dataset_url,
        "archive_sha256": archive_sha256 or None,
        "text_scope": "short PolitiFact fact-check claims in English, not full news articles",
        "license_notice": "The LIAR mirror states that the data are for research purposes only; review licensing before commercial use.",
        "files": {
            split: {"name": path.name, "sha256": sha256(path), "examples": len(load_examples(path))}
            for split, path in paths.items()
        },
    }
    validation_metrics = evaluate(model, validation_examples)
    test_metrics = evaluate(model, test_examples)
    validation_operating_point = threshold_metrics(model, validation_examples, args.risk_threshold)
    test_operating_point = threshold_metrics(model, test_examples, args.risk_threshold)
    model["evaluation"] = {
        "validation": validation_metrics,
        "test": test_metrics,
        "false_risk_operating_point": {
            "validation": validation_operating_point,
            "test": test_operating_point,
        },
    }
    model["deployment"] = {
        "risk_threshold": args.risk_threshold,
        "validation_precision": validation_operating_point["precision"],
        "validation_flagged": validation_operating_point["flagged"],
        "minimum_validation_precision": args.minimum_validation_precision,
        "minimum_validation_flags": args.minimum_validation_flags,
        "eligible_for_scoring": (
            validation_operating_point["precision"] >= args.minimum_validation_precision
            and validation_operating_point["flagged"] >= args.minimum_validation_flags
        ),
        "reason": "validation gate prevents a weak text-only model from changing a verdict",
    }
    # Deliberately omit a build timestamp: with identical input hashes and
    # parameters this makes the serialized artifact bit-for-bit reproducible.
    model["reproducible_build"] = True

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    summary = {
        "output": str(args.output),
        "output_sha256": sha256(args.output),
        "training_examples": len(training_examples),
        "validation": model["evaluation"]["validation"],
        "test": model["evaluation"]["test"],
        "false_risk_operating_point": model["evaluation"]["false_risk_operating_point"],
        "deployment": model["deployment"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
