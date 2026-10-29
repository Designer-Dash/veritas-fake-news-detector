"""Tests for the domain-matched Indic text-risk model and its tooling.

Every test uses synthetic fixtures written to ``tmp_path``; no test downloads a
corpus or touches the network, and no test depends on the shipped artifact
being present except the explicitly guarded integration checks.
"""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import random
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.text_model import (  # noqa: E402
    LEGACY_TOKENIZER,
    MODEL_FORMAT,
    MODEL_FORMAT_V2,
    UNICODE_TOKENIZER,
    classify_news_text,
    detect_language,
    extract_features,
    tokenize,
)
from tools.indic_dataset import (  # noqa: E402
    Example,
    _parse_ifnd_month,
    assign_groups,
    load_hindi_news,
    load_ifnd,
    normalize_text,
    outlet_partition,
    publisher_terms,
    split_by_group,
    strip_shared_groups,
    temporal_partition,
)
from tools.train_indic_model import (  # noqa: E402
    FEATURE_CONFIGS,
    LABELS,
    build_model,
    classification_metrics,
    evaluate_gate,
    parse_args,
    threshold_table,
    train,
)

TRUE_WORDS = ["government", "minister", "report", "district", "official", "budget"]
FAKE_WORDS = ["viral", "shocking", "miracle", "exposed", "banned", "secret"]
HINDI_TRUE = ["सरकार", "मंत्री", "योजना", "अधिकारी", "रिपोर्ट"]
HINDI_FAKE = ["वायरल", "चौंकाने", "गुप्त", "इलाज", "फर्जी"]

MONTHS = ["Jan-19", "Jun-19", "Feb-20", "Jul-20", "Nov-20", "Mar-21"]


# -- Fixtures -----------------------------------------------------------------
def _write_ifnd(path: Path, rows: int, *, outlets: int = 6, seed: int = 11) -> None:
    rng = random.Random(seed)
    true_outlets = [f"trueoutlet{index}" for index in range(outlets // 2)]
    fake_outlets = [f"fakeoutlet{index}" for index in range(outlets - outlets // 2)]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "Statement", "Image", "Web", "Category", "Date", "Label"])
        for index in range(rows):
            if index % 2 == 0:
                text = " ".join(rng.choice(TRUE_WORDS) for _ in range(14))
                label, outlet = "TRUE", rng.choice(true_outlets)
            else:
                text = " ".join(rng.choice(FAKE_WORDS) for _ in range(14))
                label, outlet = "Fake", rng.choice(fake_outlets)
            writer.writerow([index, text, "http://cdn/x.jpg", outlet, "POLITICS", rng.choice(MONTHS), label])


def _write_hindi(path: Path, rows: int, *, seed: int = 13) -> None:
    rng = random.Random(seed)
    with zipfile.ZipFile(path, "w") as bundle:
        for index in range(rows):
            real = index % 2 == 0
            words = HINDI_TRUE if real else HINDI_FAKE
            body = " ".join(rng.choice(words) for _ in range(120))
            folder = "Hindi_real_news" if real else "Hindi_fake_news"
            bundle.writestr(f"{folder}/article ({index}).txt", body)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    _write_ifnd(tmp_path / "IFND.csv", 900)
    _write_hindi(tmp_path / "Hindi_FR_News.zip", 600)
    return tmp_path


def _train_args(fixture: Path):
    return parse_args(
        [
            "--data-dir",
            str(fixture),
            "--output",
            str(fixture / "artifact.json"),
            "--no-verify",
            "--max-features",
            "3000",
            "--alpha-grid",
            "1.0",
            "--minimum-validation-flags",
            "5",
            "--minimum-holdout-false-support",
            "5",
        ]
    )


# -- Dataset loading and label mapping ---------------------------------------
def test_ifnd_loader_maps_labels_and_keeps_source_metadata(fixture_dir):
    examples = load_ifnd(fixture_dir / "IFND.csv")
    assert len(examples) == 900
    assert {example.label for example in examples} == {"false", "supported"}
    assert {example.source_label for example in examples} == {"true", "fake"}
    assert all(example.language == "en" for example in examples)
    assert all(example.outlet for example in examples)
    assert all(example.date for example in examples)


def test_ifnd_loader_rejects_unknown_labels(tmp_path):
    path = tmp_path / "IFND.csv"
    path.write_text(
        "id,Statement,Image,Web,Category,Date,Label\n1,some headline,,x,POLITICS,Oct-20,MAYBE\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown label"):
        load_ifnd(path)


def test_hindi_loader_uses_folder_as_label(fixture_dir):
    examples = load_hindi_news(fixture_dir / "Hindi_FR_News.zip")
    assert len(examples) == 600
    assert {example.label for example in examples} == {"false", "supported"}
    assert all(example.language == "hi" for example in examples)
    # The corpus genuinely has no outlet or date columns; nothing is invented.
    assert all(example.outlet == "" for example in examples)
    assert all(example.date == "" for example in examples)


def test_dev_anagari_detection_and_month_parsing():
    assert detect_language("सरकार ने योजना शुरू की") == "hi"
    assert detect_language("The government announced a scheme") == "en"
    assert detect_language("") == "unknown"
    assert _parse_ifnd_month("Oct-20") == "2020-10"
    assert _parse_ifnd_month("Jan-21") == "2021-01"
    # Day-month strings carry no year and are deliberately not guessed.
    assert _parse_ifnd_month("20-Jul") == ""
    assert _parse_ifnd_month("") == ""


# -- Duplicate and near-duplicate protection ---------------------------------
def test_exact_and_near_duplicates_share_a_group():
    base = "the government announced a new scheme for farmers in the district today"
    near = base + " according to officials who confirmed the details"
    examples = [
        Example(text=base, label="supported", source_label="true", corpus="ifnd", language="en"),
        Example(text=base, label="supported", source_label="true", corpus="ifnd", language="en"),
        Example(text=near, label="supported", source_label="true", corpus="ifnd", language="en"),
        Example(
            text="viral video shows a miracle cure banned by doctors everywhere",
            label="false",
            source_label="fake",
            corpus="ifnd",
            language="en",
        ),
    ]
    grouped, stats = assign_groups(examples)
    assert stats["exact_duplicate_items"] >= 1
    assert len({example.group for example in grouped[:3]}) == 1
    assert grouped[3].group not in {example.group for example in grouped[:3]}


def test_groups_never_cross_splits():
    examples = [
        Example(
            text=f"headline number {index} about a district report",
            label="supported" if index % 2 else "false",
            source_label="x",
            corpus="ifnd",
            language="en",
        )
        for index in range(200)
    ]
    # Force every item into one cluster to prove the splitter cannot break it up.
    examples = [replace(example, group="shared") for example in examples]
    splits = split_by_group(examples)
    occupied = [name for name, rows in splits.items() if rows]
    assert len(occupied) == 1
    assert len(splits[occupied[0]]) == 200


def test_split_separation_has_no_group_overlap(fixture_dir):
    examples = list(load_ifnd(fixture_dir / "IFND.csv"))
    grouped, _stats = assign_groups(examples)
    splits = split_by_group(grouped)
    train_groups = {example.group for example in splits["train"]}
    for name in ("validation", "test"):
        assert not (train_groups & {example.group for example in splits[name]})
    assert sum(len(rows) for rows in splits.values()) == len(grouped)


# -- Temporal and outlet partitions ------------------------------------------
def test_temporal_partition_is_strictly_older_first():
    examples = [
        Example(text=f"item {index}", label="false", source_label="f", corpus="ifnd", language="en",
                date=date, group=f"g{index}")
        for index, date in enumerate(["2019-01"] * 30 + ["2020-01"] * 30 + ["2021-01"] * 40)
    ]
    older, newer, cutoff = temporal_partition(examples, test_fraction=0.40)
    assert older and newer
    assert all(example.date < cutoff for example in older)
    assert all(example.date >= cutoff for example in newer)


def test_outlet_partition_holds_out_whole_outlets():
    examples = [
        Example(text=f"item {index}", label="false", source_label="f", corpus="ifnd", language="en",
                outlet=f"outlet{index % 5}", group=f"g{index}")
        for index in range(500)
    ]
    train, test, holdout = outlet_partition(examples, min_outlet_rows=50, holdout_share=0.20)
    assert holdout
    assert train and test
    assert not ({example.outlet for example in train} & set(holdout))
    assert {example.outlet for example in test} <= set(holdout)


def test_strip_shared_groups_removes_leaking_rows():
    train = [Example(text="a", label="false", source_label="f", corpus="c", language="en", group="g1")]
    held = [
        Example(text="a", label="false", source_label="f", corpus="c", language="en", group="g1"),
        Example(text="b", label="false", source_label="f", corpus="c", language="en", group="g2"),
    ]
    kept, dropped = strip_shared_groups(train, held)
    assert len(kept) == 1 and dropped == 1


# -- Feature representation ---------------------------------------------------
def test_feature_spec_controls_extraction():
    text = "the government announced a scheme for farmers"
    plain = extract_features(text, FEATURE_CONFIGS["word12"])
    with_chars = extract_features(text, FEATURE_CONFIGS["word12_char45"])
    assert any(feature.startswith("w:") for feature in plain)
    assert any(feature.startswith("b:") for feature in plain)
    assert not any(feature.startswith("c") for feature in plain)
    assert any(feature.startswith("c4:") for feature in with_chars)
    assert any(feature.startswith("c5:") for feature in with_chars)
    assert set(plain).issubset(set(with_chars))


def test_text_limit_bounds_feature_extraction():
    long_text = "government " * 500
    bounded = extract_features(long_text, {"word_ngrams": [1], "text_limit": 50})
    unbounded = extract_features(long_text, {"word_ngrams": [1]})
    assert sum(bounded.values()) < sum(unbounded.values())


def test_normalisation_is_stable():
    assert normalize_text("Hello,  WORLD!!") == normalize_text("hello world")
    assert normalize_text("सरकार, ने!!!") == normalize_text("सरकार ने")


def test_publisher_redaction_removes_brand_tokens_from_features():
    spec = {
        "word_ngrams": [1, 2],
        "char_ngrams": [],
        "char_limit": 0,
        "text_limit": 0,
        "redacted_terms": ["ndtv", "altnews", "indiatoday", "factchecker"],
    }
    kept = extract_features("the district report confirmed the scheme", spec)
    dropped = extract_features("ndtv reports india today and alt news fact checker", spec)
    assert "w:district" in kept
    assert "w:ndtv" not in dropped
    assert "w:altnews" not in dropped
    assert "w:indiatoday" not in dropped
    # The two-word brand form is redacted through its joined term too.
    assert "b:india today" not in dropped
    assert "b:fact checker" not in dropped
    # Ordinary words survive untouched.
    assert "w:reports" in dropped


def test_unicode_tokenizer_preserves_indic_words():
    words = ["वायरल", "वीडियो", "गुप्त", "इलाज", "बीमारी"]
    tokens = tokenize(" ".join(words), UNICODE_TOKENIZER)
    assert tokens == words
    # Latin behaviour stays equivalent to the legacy pattern.
    assert tokenize("The government's report, 2020-ish!", UNICODE_TOKENIZER) == [
        "the",
        "government's",
        "report",
        "2020-ish",
    ]


def test_legacy_tokenizer_behaviour_is_unchanged():
    # The v1 artifact was trained with this tokenizer, so it must not move.
    assert tokenize("the government's report") == ["the", "government's", "report"]
    # Documented legacy defect: combining marks split Indic words.
    assert tokenize("वायरल", LEGACY_TOKENIZER) == ["यरल"]
    assert tokenize("वायरल", UNICODE_TOKENIZER) == ["वायरल"]


def test_feature_spec_rejects_unknown_tokenizer(tmp_path):
    payload = _valid_v2_payload()
    payload["feature_spec"] = {**payload["feature_spec"], "tokenizer": "imaginary"}
    path = tmp_path / "bad-tokenizer.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    signal = classify_news_text("hoax " * 40, path=path, min_tokens=10, min_coverage=0.05)
    assert signal["available"] is False
    assert signal["reason"] == "model_unavailable"


def test_publisher_terms_covers_curated_brands_and_outlet_column():
    examples = [
        Example(text="x", label="false", source_label="f", corpus="ifnd", language="en", outlet="India Today"),
        Example(text="y", label="false", source_label="f", corpus="ifnd", language="en", outlet=""),
    ]
    terms = publisher_terms(examples)
    assert "indiatoday" in terms
    assert "ndtv" in terms
    assert all(term == term.lower().replace(" ", "") for term in terms)


# -- Training, serialization, loading, prediction ----------------------------
def test_training_is_deterministic(fixture_dir):
    first = train(_train_args(fixture_dir))
    first_artifact = Path(first["output"]).read_text(encoding="utf-8")
    second = train(_train_args(fixture_dir))
    assert first_artifact == Path(second["output"]).read_text(encoding="utf-8")
    assert first["output_sha256"] == second["output_sha256"]


def test_trained_artifact_has_required_metadata(fixture_dir):
    summary = train(_train_args(fixture_dir))
    artifact = json.loads(Path(summary["output"]).read_text(encoding="utf-8"))
    assert artifact["format"] == MODEL_FORMAT_V2
    assert artifact["feature_spec"]["tokenizer"] == UNICODE_TOKENIZER
    assert artifact["labels"] == list(LABELS)
    assert artifact["vocabulary"] and artifact["vocabulary_size"] == len(artifact["vocabulary"])
    assert artifact["feature_spec"]["word_ngrams"]
    for key in (
        "model_version",
        "dataset",
        "training",
        "splits",
        "evaluation",
        "leakage_checks",
        "deployment",
        "reproducibility",
        "label_scheme",
    ):
        assert key in artifact
    assert artifact["dataset"]["provenance"]["ifnd"]["sha256"]
    assert set(artifact["evaluation"]) == {
        "validation",
        "test",
        "temporal_holdout",
        "outlet_holdout",
    }
    # The artifact must be JSON only: nothing that could execute on load.
    assert "pickle" not in json.dumps(artifact)[:2000].lower()


def test_artifact_trains_and_serves_through_the_runtime(fixture_dir):
    summary = train(_train_args(fixture_dir))
    path = Path(summary["output"])
    english = classify_news_text(" ".join(FAKE_WORDS * 10), path=path, min_tokens=10, min_coverage=0.05)
    assert english["available"] is True
    assert english["language"] == "en"
    assert english["prediction"] == "false"
    assert set(english["probabilities"]) == set(LABELS)
    assert english["risk_score"] > 0.5
    assert english["scoring_eligible"] in (True, False)

    hindi = classify_news_text(" ".join(HINDI_FAKE * 20), path=path, min_tokens=10, min_coverage=0.05)
    assert hindi["available"] is True
    assert hindi["language"] == "hi"
    assert hindi["prediction"] == "false"


def test_classifier_reports_insufficient_vocabulary(fixture_dir):
    summary = train(_train_args(fixture_dir))
    path = Path(summary["output"])
    # Well-formed Devanagari tokens that the fixture vocabulary never saw.
    signal = classify_news_text("कखगघ ङचछज झटठड णतथद धनफब" * 10, path=path, min_tokens=10, min_coverage=0.10)
    assert signal["available"] is False
    assert signal["reason"] == "out_of_distribution"
    assert signal["token_coverage"] < 0.10


def test_classifier_reports_too_little_text(fixture_dir):
    summary = train(_train_args(fixture_dir))
    signal = classify_news_text("short", path=Path(summary["output"]), min_tokens=30)
    assert signal["available"] is False
    assert signal["reason"] == "too_little_text"


# -- Metrics and thresholds ---------------------------------------------------
def test_threshold_table_reports_precision_recall_and_volume():
    truth = ["false", "false", "supported", "supported"]
    scores = [0.95, 0.85, 0.82, 0.10]
    table = threshold_table(truth, scores)
    assert set(table) == {"0.50", "0.60", "0.70", "0.80", "0.90"}
    at_80 = table["0.80"]
    assert at_80["flagged"] == 3
    assert at_80["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert at_80["recall"] == 1.0
    assert at_80["false_support"] == 2
    assert table["0.90"]["flagged"] == 1
    assert table["0.90"]["precision"] == 1.0


def test_classification_metrics_include_confusion_matrix():
    metrics = classification_metrics(["false", "supported"], ["false", "false"])
    assert metrics["accuracy"] == 0.5
    assert metrics["confusion_matrix"] == {"false": {"false": 1, "supported": 0}, "supported": {"false": 1, "supported": 0}}
    assert metrics["per_label"]["false"]["precision"] == 0.5


def test_model_training_requires_every_label():
    with pytest.raises(ValueError, match="missing labels"):
        build_model(
            ["w:a"],
            {"false": 3},
            {"false": Counter({"w:a": 3})},
            {"false": 3},
            alpha=1.0,
        )


# -- Deployment gate ----------------------------------------------------------
def _gate_payload(precision: float, flagged: int, false_support: int, examples: int = 1000):
    point = {
        "flagged": flagged,
        "flag_rate": 0.1,
        "false_support": false_support,
        "precision": precision,
        "recall": 0.9,
        "f1": 0.5,
    }
    return {
        "metrics": {"examples": examples} if examples else {"examples": 0},
        "thresholds": {"0.80": point},
    }


def _leakage(clean: bool = True):
    return {
        "group_overlap": {
            "validation": 0 if clean else 3,
            "test": 0,
            "temporal_holdout": 0,
            "outlet_holdout": 0,
            "temporal_rows_dropped_for_group_overlap": 7,
            "outlet_rows_dropped_for_group_overlap": 2,
        },
        "metadata_feature_prefixes_absent": {"banned_prefixes": ["web:"], "features_found": 0, "examples": []},
        "outlet_shortcut": {"outlet_tokens_checked": 5, "features_matching_outlet_names": 0, "examples": []},
        "publisher_redaction": {
            "enabled": True,
            "terms": 5,
            "features_still_present": 0,
            "examples": [],
        },
    }


def test_gate_passes_only_when_every_condition_holds():
    gate = evaluate_gate(
        validation=_gate_payload(0.80, 60, 120),
        test=_gate_payload(0.75, 50, 100),
        temporal=_gate_payload(0.60, 40, 80),
        outlet=_gate_payload(0.55, 40, 90),
        leakage=_leakage(),
        risk_threshold=0.80,
        minimum_validation_precision=0.70,
        minimum_validation_flags=25,
        minimum_test_precision=0.60,
        minimum_holdout_precision=0.50,
        minimum_holdout_false_support=50,
    )
    assert gate["eligible_for_scoring"] is True
    assert gate["decision"] == "approved_for_risk_signal"
    assert gate["failed_conditions"] == []


def test_gate_fails_on_weak_validation_precision():
    gate = evaluate_gate(
        validation=_gate_payload(0.45, 60, 120),
        test=_gate_payload(0.40, 50, 100),
        temporal=_gate_payload(0.60, 40, 80),
        outlet=_gate_payload(0.55, 40, 90),
        leakage=_leakage(),
        risk_threshold=0.80,
        minimum_validation_precision=0.70,
        minimum_validation_flags=25,
        minimum_test_precision=0.60,
        minimum_holdout_precision=0.50,
        minimum_holdout_false_support=50,
    )
    assert gate["eligible_for_scoring"] is False
    assert gate["decision"] == "shadow_only"
    assert "validation_false_precision" in gate["failed_conditions"]


def test_gate_fails_on_small_holdout_and_on_leakage():
    gate = evaluate_gate(
        validation=_gate_payload(0.90, 60, 120),
        test=_gate_payload(0.90, 50, 100),
        temporal=_gate_payload(0.90, 3, 4),
        outlet=_gate_payload(0.90, 2, 3),
        leakage=_leakage(clean=False),
        risk_threshold=0.80,
        minimum_validation_precision=0.70,
        minimum_validation_flags=25,
        minimum_test_precision=0.60,
        minimum_holdout_precision=0.50,
        minimum_holdout_false_support=50,
    )
    assert gate["eligible_for_scoring"] is False
    assert "temporal_false_support_sufficient" in gate["failed_conditions"]
    assert "outlet_holdout_false_precision_acceptable" in gate["failed_conditions"]
    assert "no_duplicate_or_near_duplicate_leakage" in gate["failed_conditions"]


def test_volume_condition_fails_below_the_flag_floor():
    gate = evaluate_gate(
        validation=_gate_payload(0.95, 4, 120),
        test=_gate_payload(0.90, 50, 100),
        temporal=_gate_payload(0.60, 40, 80),
        outlet=_gate_payload(0.55, 40, 90),
        leakage=_leakage(),
        risk_threshold=0.80,
        minimum_validation_precision=0.70,
        minimum_validation_flags=25,
        minimum_test_precision=0.60,
        minimum_holdout_precision=0.50,
        minimum_holdout_false_support=50,
    )
    assert "validation_flagged_volume" in gate["failed_conditions"]


# -- Corrupted / invalid artifact rejection ----------------------------------
def _valid_v2_payload() -> dict:
    return {
        "format": MODEL_FORMAT_V2,
        "model_version": "test",
        "algorithm": "multinomial_naive_bayes",
        "labels": ["false", "supported"],
        "vocabulary": ["w:hoax", "w:verified"],
        "log_priors": {"false": -0.7, "supported": -0.7},
        "log_likelihoods": {"false": [-0.1, -9.0], "supported": [-9.0, -0.1]},
        "unknown_log_likelihoods": {"false": -9.0, "supported": -9.0},
        "feature_spec": {"word_ngrams": [1, 2], "char_ngrams": [], "char_limit": 0, "text_limit": 0},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(format="veritas-text-model-v9"),
        lambda payload: payload.update(labels=[]),
        lambda payload: payload.update(vocabulary=[]),
        lambda payload: payload.update(vocabulary=["w:a", "w:a"]),
        lambda payload: payload.update(vocabulary=["w:a"]),
        lambda payload: payload["log_likelihoods"].update(false=[-0.1]),
        lambda payload: payload["log_likelihoods"].update(false=[float("nan"), -9.0]),
        lambda payload: payload["log_likelihoods"].update(supported=[float("inf"), -9.0]),
        lambda payload: payload["log_priors"].update(false="high"),
        lambda payload: payload.update(feature_spec={"word_ngrams": [7]}),
        lambda payload: payload.update(
            calibration={"method": "piecewise_linear", "x": [0.0, 0.5, 0.4, 1.0], "y": [0.0, 0.5, 0.6, 1.0]}
        ),
        lambda payload: payload.update(
            calibration={"method": "piecewise_linear", "x": [0.0, 0.5, 1.0], "y": [0.0, 0.9, 0.2]}
        ),
        lambda payload: payload.update(calibration={"method": "magic"}),
    ],
)
def test_runtime_rejects_invalid_artifacts(tmp_path, mutate):
    payload = _valid_v2_payload()
    mutate(payload)
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    signal = classify_news_text("hoax " * 40, path=path, min_tokens=10, min_coverage=0.05)
    assert signal["available"] is False
    assert signal["reason"] == "model_unavailable"


def test_runtime_rejects_non_json_and_missing_files(tmp_path):
    garbage = tmp_path / "garbage.json"
    garbage.write_text("not json at all", encoding="utf-8")
    assert classify_news_text("hoax " * 40, path=garbage, min_tokens=5)["reason"] == "model_unavailable"
    assert (
        classify_news_text("hoax " * 40, path=tmp_path / "nope.json", min_tokens=5)["reason"]
        == "model_unavailable"
    )


def test_runtime_still_accepts_the_legacy_v1_artifact(tmp_path):
    payload = {
        "format": MODEL_FORMAT,
        "model_version": "liar-mnb-v1",
        "algorithm": "multinomial_naive_bayes",
        "labels": ["false", "mixed", "supported"],
        "vocabulary": ["w:hoax"],
        "log_priors": {label: math.log(1 / 3) for label in ("false", "mixed", "supported")},
        "log_likelihoods": {"false": [-0.001], "mixed": [-5.0], "supported": [-9.0]},
        "unknown_log_likelihoods": {label: -9.0 for label in ("false", "mixed", "supported")},
    }
    path = tmp_path / "liar.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    signal = classify_news_text("hoax " * 40, path=path, min_tokens=10, min_coverage=0.5)
    assert signal["available"] is True
    assert signal["prediction"] == "false"
    assert set(signal["probabilities"]) == {"false", "mixed", "supported"}


# -- Shipped artifact integration --------------------------------------------
def test_shipped_artifact_loads_and_is_advisory_only():
    artifact_path = Path(__file__).resolve().parents[1] / "models" / "indic_news_mnb_v2.json"
    if not artifact_path.exists():
        pytest.skip("Indic artifact has not been trained in this checkout")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["format"] == MODEL_FORMAT_V2
    assert set(artifact["labels"]) == {"false", "supported"}
    assert artifact["deployment"]["decision"] in ("approved_for_risk_signal", "shadow_only")
    assert artifact["leakage_checks"]["group_overlap"]["validation"] == 0
    assert artifact["feature_spec"]["tokenizer"] == UNICODE_TOKENIZER
    assert artifact["leakage_checks"]["outlet_shortcut"]["features_matching_outlet_names"] == 0
    # Build Hindi input from the artifact's own vocabulary so coverage is real.
    devanagari = [
        feature.split(":", 1)[1]
        for feature in artifact["vocabulary"]
        if feature.startswith("w:") and re.search(r"[\u0900-\u097f]", feature)
    ]
    assert devanagari, "artifact contains no Devanagari word features"
    hindi_text = " ".join(devanagari[:60])
    signal = classify_news_text(
        hindi_text, path=artifact_path, min_tokens=10, min_coverage=0.02
    )
    assert signal["available"] is True
    assert signal["language"] == "hi"
    # Advisory only: eligibility is decided by the artifact's own gate result.
    assert signal["scoring_eligible"] == artifact["deployment"]["eligible_for_scoring"]
    assert 0.0 <= signal["risk_score"] <= 1.0


def test_legacy_liar_artifact_is_still_present_and_loadable():
    legacy = Path(__file__).resolve().parents[1] / "models" / "liar_mnb_v1.json"
    if not legacy.exists():
        pytest.skip("legacy artifact absent")
    artifact = json.loads(legacy.read_text(encoding="utf-8"))
    assert artifact["format"] == MODEL_FORMAT
    assert artifact["deployment"]["eligible_for_scoring"] is False
    signal = classify_news_text("hoax " * 40, path=legacy, min_tokens=10, min_coverage=0.05)
    assert signal["reason"] in ("model_unavailable", "out_of_distribution")
