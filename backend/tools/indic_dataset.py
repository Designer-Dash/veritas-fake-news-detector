"""Reproducible loading and splitting of the Indic news misinformation corpora.

Two publicly obtainable, article-domain-matched corpora are supported:

``ifnd``
    IFND — the Indian Fake News Dataset (Singhal et al.). Indian news items with
    a headline, an outlet, a publication month, a topic and a binary
    ``TRUE``/``Fake`` verdict. Obtained from a public GitHub mirror of the
    released CSV because the original distribution is not script-addressable.
    Text scope: headline-level English (and romanised) Indian news.

``hindi_news``
    Hindi News Real/Fake Articles (Zenodo 10.5281/zenodo.11408513). Full
    article text in Devanagari harvested from Hindi news sites and Hindi
    debunking sites, with a binary ``real``/``fake`` folder label. Text scope:
    full article-level Hindi.

Both are research corpora. Nothing here fabricates, augments or synthesises
examples, and no field is invented when the source does not provide it: the
Hindi corpus genuinely has no outlet or date column, so those stay empty and
are reported as unavailable rather than guessed.

Every loader records file SHA-256 digests so a training run can be reproduced
and audited. Only the standard library is used.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
import csv
import hashlib
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.request import Request, urlopen
import zipfile

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.text_model import UNICODE_TOKENIZER, tokenize  # noqa: E402


# -- Corpus registry ----------------------------------------------------------
CORPORA: Dict[str, Dict[str, Any]] = {
    "ifnd": {
        "name": "IFND (Indian Fake News Dataset)",
        "citation": "Singhal et al., IFND: A Benchmark Dataset for Fake News Detection",
        "url": (
            "https://raw.githubusercontent.com/Rajat-Dhanuka/"
            "Fake-News-Classifier-using-the-IFND-dataset/main/IFND%20dataset/IFND.csv"
        ),
        "distribution": "public GitHub mirror of the released IFND CSV",
        "filename": "IFND.csv",
        "sha256": "3728c7b785ea93c9dadcc61a4c549c0eddd71a5fb6a1a31be08b5235ee4f5bfa",
        "license_notice": (
            "Research use. Distributed without an explicit open licence by the mirror; "
            "confirm terms with the original authors before commercial use."
        ),
        "language": "en",
        "text_scope": "Indian news headline / statement text (English), not full articles",
        "has_outlet": True,
        "has_date": True,
        "label_map": {"true": "supported", "fake": "false"},
    },
    "hindi_news": {
        "name": "Hindi News Real/Fake Articles (Zenodo 11408513)",
        "citation": "Patil, Gandhi, Chauhan, Patil, Pawar - Multilingual Fake News Detection Dataset",
        "url": "https://zenodo.org/api/records/11408513/files/Hindi_F&R_News.zip/content",
        "distribution": "Zenodo record 10.5281/zenodo.11408513 (Hindi_F&R_News.zip)",
        "filename": "Hindi_FR_News.zip",
        "sha256": "5a613af50db4164687b8808ed75a783af88d7c449982ebe8a178affc21d3337e",
        "license_notice": (
            "Published on Zenodo for research on fake news detection; check the record's "
            "terms before commercial use."
        ),
        "language": "hi",
        "text_scope": "full Hindi news article text",
        "has_outlet": False,
        "has_date": False,
        "label_map": {"real": "supported", "fake": "false"},
    },
}

# VERITAS label mapping is intentionally binary here. IFND's own label column is
# TRUE/Fake and the Hindi corpus is real/fake; neither supports a trustworthy
# third class, so no MIXED/MISLEADING class is invented. IFND's *topic* column
# does contain a "MISLEADING" value whose rows are all labelled Fake by the
# dataset authors, which is recorded in the model card as a known flattening.

DEFAULT_DATA_DIR = BACKEND_DIR / ".model-training" / "indic"

# Curated, auditable publisher/fact-checker brand tokens.
#
# IFND's statement text frequently attributes a claim to the publisher that
# reported or debunked it ("AFP fact check", "Alt News", "NDTV reports"). If
# those brand names stay in the feature space the model can memorise *which
# newsroom wrote a headline* instead of how misinformation reads - exactly the
# publisher shortcut that must not decide a verdict. This list, together with
# every outlet value found in the loaded corpora, is redacted from the word
# feature space at both training and serving time.
#
# The trade-off is explicit: a little legitimate lexical signal is discarded in
# exchange for removing a publisher-identity shortcut. Chosen terms are
# distinctive brand strings, not generic news vocabulary.
PUBLISHER_BRAND_TOKENS: frozenset = frozenset(
    {
        "afp",
        "altnews",
        "ani",
        "bbc",
        "boomlive",
        "bpnews",
        "cnn",
        "deccanherald",
        "dnaindia",
        "factcheck",
        "factchecker",
        "factcrescendo",
        "factly",
        "firstpost",
        "hindustantimes",
        "ians",
        "indianexpress",
        "indiatoday",
        "indiatv",
        "industantimes",
        "livemint",
        "mirrornow",
        "ndtv",
        "news18",
        "newschecker",
        "newsmeter",
        "newsmobile",
        "oneindia",
        "opindia",
        "organiser",
        "pib",
        "pti",
        "reuters",
        "republictv",
        "scrollin",
        "swarajya",
        "teekhimirchi",
        "telegraphindia",
        "thehindu",
        "thelogicalindian",
        "theprint",
        "thequint",
        "thestateman",
        "thestatesman",
        "thewire",
        "timesnow",
        "tribuneindia",
        "wion",
        "zeenews",
    }
)


def publisher_terms(examples: Sequence["Example"]) -> List[str]:
    """Return the sorted redaction list: curated brands plus corpus outlets.

    Outlet values are normalised the same way the feature space is (lower case,
    spaces removed) so ``India Today`` and ``indiatoday`` are the same term.
    """
    terms = set(PUBLISHER_BRAND_TOKENS)
    for example in examples:
        outlet = (example.outlet or "").replace(" ", "").lower()
        if outlet:
            terms.add(outlet)
    return sorted(term for term in terms if term)


@dataclass(frozen=True)
class Example:
    """One labelled news item, with only source-provided metadata."""

    text: str
    label: str          # VERITAS label: "false" or "supported"
    source_label: str   # the corpus' own label string
    corpus: str
    language: str
    outlet: str = ""    # empty when the corpus has no outlet column
    date: str = ""      # "YYYY-MM" when the corpus has an unambiguous month
    date_raw: str = ""  # the untouched source value, kept for audit purposes
    group: str = ""     # duplicate / near-duplicate cluster id


# -- Integrity helpers --------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CorpusIntegrityError(RuntimeError):
    """Raised when a downloaded corpus does not match its recorded digest."""


def download_corpus(corpus_id: str, data_dir: Path) -> Path:
    """Download one corpus with an atomic replace; never partially trusts a file."""
    spec = CORPORA[corpus_id]
    destination = data_dir / spec["filename"]
    if destination.exists() and destination.stat().st_size:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(spec["url"], headers={"User-Agent": "veritas-training/2.0"})
    try:
        with urlopen(request, timeout=180) as response, temporary.open("wb") as output:
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
    os.replace(temporary, destination)
    return destination


def ensure_corpus(
    corpus_id: str, data_dir: Path, *, download: bool = False, verify: bool = True
) -> Path:
    """Return the corpus path, optionally downloading and verifying its digest."""
    spec = CORPORA[corpus_id]
    path = data_dir / spec["filename"]
    if not path.exists():
        if not download:
            raise FileNotFoundError(
                f"{path} is missing. Re-run with --download to fetch {spec['name']}."
            )
        path = download_corpus(corpus_id, data_dir)
    if verify and spec.get("sha256"):
        actual = sha256_file(path)
        if actual != spec["sha256"]:
            raise CorpusIntegrityError(
                f"{spec['filename']} sha256 mismatch: expected {spec['sha256']}, got {actual}"
            )
    return path


# -- Text normalisation ------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s\u0900-\u097f]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(text: str) -> str:
    """Case-fold, drop punctuation and collapse whitespace for duplicate checks."""
    folded = _PUNCT_RE.sub(" ", (text or "").lower())
    return _SPACE_RE.sub(" ", folded).strip()


def exact_digest(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:24]


# -- Duplicate / near-duplicate grouping -------------------------------------
def _stable_hash(payload: str) -> int:
    return int.from_bytes(hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest(), "big")


def shingle_hashes(text: str, *, size: int = 4, cap: int = 600) -> List[int]:
    """Hashed word shingles for near-duplicate detection (bounded work per doc).

    Uses the Indic-aware tokenizer so Devanagari words are not shredded at
    their vowel signs before being hashed.
    """
    words = tokenize(text, UNICODE_TOKENIZER)
    if len(words) < size:
        return [_stable_hash(" ".join(words))] if words else []
    hashes: List[int] = []
    for index in range(len(words) - size + 1):
        hashes.append(_stable_hash(" ".join(words[index:index + size])))
        if len(hashes) >= cap:
            break
    return hashes


def bottom_k_signature(hashes: Sequence[int], k: int) -> List[int]:
    """The k smallest distinct shingle hashes (a bottom-k MinHash sketch)."""
    return sorted(set(hashes))[:k]


def assign_groups(
    examples: Sequence[Example],
    *,
    signature_size: int = 20,
    shingle_size: int = 4,
    shingle_cap: int = 400,
    max_bucket: int = 40,
    min_shared_signature: Optional[int] = None,
    opening_tokens: int = 10,
) -> Tuple[List[Example], Dict[str, Any]]:
    """Cluster duplicate and near-duplicate items so no cluster crosses a split.

    Three structural rules are applied, all deterministic:

    1. Exact duplicates share a normalised-text digest.
    2. Items whose first few tokens are identical share an opening digest
       (catches syndicated copies and identical headlines).
    3. Near-duplicates are linked only when two items genuinely agree on a
       large share of their bottom-k MinHash signature. Requiring agreement
       *per pair* - rather than chaining through shared hash buckets - is what
       stops transitively merging unrelated documents into one giant cluster.
    """
    parent = list(range(len(examples)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_digest: Dict[str, List[int]] = defaultdict(list)
    openings: Dict[str, List[int]] = defaultdict(list)
    for index, example in enumerate(examples):
        by_digest[exact_digest(example.text)].append(index)
        words = tokenize(example.text, UNICODE_TOKENIZER)[:opening_tokens]
        if len(words) >= 4:
            openings[_stable_hash(" ".join(words)).to_bytes(8, "big").hex()].append(index)
    exact_duplicate_extra = 0
    for indices in by_digest.values():
        for other in indices[1:]:
            union(indices[0], other)
            exact_duplicate_extra += 1
    opening_links = 0
    for indices in openings.values():
        for other in indices[1:]:
            union(indices[0], other)
            opening_links += 1

    threshold = min_shared_signature
    if threshold is None:
        threshold = max(3, int(signature_size * 0.45))
    buckets: Dict[int, List[int]] = defaultdict(list)
    for index, example in enumerate(examples):
        signature = bottom_k_signature(
            shingle_hashes(example.text, size=shingle_size, cap=shingle_cap), signature_size
        )
        for value in signature:
            buckets[value].append(index)
    pair_agreement: Counter[Tuple[int, int]] = Counter()
    for indices in buckets.values():
        if not 2 <= len(indices) <= max_bucket:
            continue
        for position, left in enumerate(indices):
            for right in indices[position + 1:]:
                pair_agreement[(left, right)] += 1
    near_duplicate_links = 0
    for (left, right), shared in pair_agreement.items():
        if shared >= threshold:
            union(left, right)
            near_duplicate_links += 1

    grouped: List[Example] = []
    cluster_sizes: Counter[int] = Counter()
    for index, example in enumerate(examples):
        root = find(index)
        cluster_sizes[root] += 1
        grouped.append(replace(example, group=f"{example.corpus}:g{root}"))

    stats = {
        "items": len(examples),
        "exact_duplicate_items": exact_duplicate_extra,
        "opening_duplicate_links": opening_links,
        "near_duplicate_links": near_duplicate_links,
        "near_duplicate_signature_agreement": threshold,
        "clusters": len(cluster_sizes),
        "largest_cluster": max(cluster_sizes.values()) if cluster_sizes else 0,
        "items_in_multi_item_clusters": sum(size for size in cluster_sizes.values() if size > 1),
    }
    return grouped, stats


# -- Loaders -----------------------------------------------------------------
def _parse_ifnd_month(raw: str) -> str:
    """Parse IFND's publication month, returning "YYYY-MM" or "".

    IFND's ``Date`` column mixes two formats: an unambiguous ``Mon-YY`` form
    (e.g. ``Oct-20``) and a ``DD-Mon`` form (e.g. ``20-Jul``) that carries no
    year at all. Only the first is parsed; the second is deliberately left
    unparsed rather than guessing a year, because inventing one would make a
    temporal split look better than the data supports. The unparsed share is
    counted and reported instead.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    for fmt in ("%b-%y", "%b-%Y", "%B-%y", "%B-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return ""


def load_ifnd(path: Path) -> List[Example]:
    """Load the IFND CSV: statement text, binary verdict, outlet and month."""
    spec = CORPORA["ifnd"]
    label_map = spec["label_map"]
    examples: List[Example] = []
    csv.field_size_limit(10**9)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Statement", "Label", "Web", "Date"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        for row_number, row in enumerate(reader, start=2):
            text = (row.get("Statement") or "").strip()
            if not text:
                continue
            raw_label = (row.get("Label") or "").strip().lower()
            if raw_label not in label_map:
                raise ValueError(f"{path.name}:{row_number}: unknown label {raw_label!r}")
            examples.append(
                Example(
                    text=text,
                    label=label_map[raw_label],
                    source_label=raw_label,
                    corpus="ifnd",
                    language=spec["language"],
                    # Web/Category/Image are recorded for auditing and split
                    # construction only; they are never turned into features.
                    outlet=(row.get("Web") or "").strip().lower(),
                    date=_parse_ifnd_month(row.get("Date") or ""),
                    date_raw=(row.get("Date") or "").strip(),
                )
            )
    if not examples:
        raise ValueError(f"{path} contains no usable examples")
    return examples


def load_hindi_news(path: Path) -> List[Example]:
    """Load the Hindi archive: folder name is the label, file body is the article."""
    spec = CORPORA["hindi_news"]
    label_map = spec["label_map"]
    examples: List[Example] = []
    with zipfile.ZipFile(path) as bundle:
        members = [
            info
            for info in bundle.infolist()
            if not info.is_dir() and info.filename.endswith(".txt") and "__MACOSX" not in info.filename
        ]
        if not members:
            raise ValueError(f"{path} contains no .txt articles")
        for info in members:
            parts = Path(info.filename).parts
            folder = parts[-2].lower() if len(parts) >= 2 else ""
            if "fake" in folder:
                source_label = "fake"
            elif "real" in folder:
                source_label = "real"
            else:
                raise ValueError(f"{path.name}: cannot infer label from {info.filename!r}")
            body = bundle.read(info).decode("utf-8", "replace").lstrip("\ufeff").strip()
            if not body:
                continue
            examples.append(
                Example(
                    text=body,
                    label=label_map[source_label],
                    source_label=source_label,
                    corpus="hindi_news",
                    language=spec["language"],
                )
            )
    if not examples:
        raise ValueError(f"{path} contains no usable examples")
    return examples


LOADERS = {"ifnd": load_ifnd, "hindi_news": load_hindi_news}


def load_corpus(corpus_id: str, path: Path) -> List[Example]:
    return LOADERS[corpus_id](path)


def load_all(
    data_dir: Path,
    *,
    corpora: Sequence[str] = ("ifnd", "hindi_news"),
    download: bool = False,
    verify: bool = True,
    cluster: bool = True,
) -> Tuple[List[Example], Dict[str, Any]]:
    """Load, concatenate and cluster the requested corpora."""
    collected: List[Example] = []
    files: Dict[str, Any] = {}
    for corpus_id in corpora:
        path = ensure_corpus(corpus_id, data_dir, download=download, verify=verify)
        rows = load_corpus(corpus_id, path)
        files[corpus_id] = {
            "path": path.name,
            "sha256": sha256_file(path),
            "examples": len(rows),
            "url": CORPORA[corpus_id]["url"],
            "name": CORPORA[corpus_id]["name"],
        }
        collected.extend(rows)
    grouping: Dict[str, Any] = {}
    if cluster:
        collected, grouping = assign_groups(collected)
    else:
        collected = [replace(example, group=f"{example.corpus}:s{index}") for index, example in enumerate(collected)]
    return collected, {"files": files, "grouping": grouping}


# -- Deterministic partitioning ----------------------------------------------
def bucket_fraction(key: str, salt: str) -> float:
    """Stable pseudo-uniform value in [0, 1) for a key, with no RNG state."""
    return _stable_hash(f"{salt}:{key}") / float(1 << 64)


def split_by_group(
    examples: Sequence[Example],
    *,
    weights: Optional[Mapping[str, float]] = None,
    salt: str = "veritas-split-v2",
) -> Dict[str, List[Example]]:
    """Assign whole duplicate clusters to one split each.

    Whole-group assignment is what makes duplicate and near-duplicate leakage
    structurally impossible rather than merely checked afterwards.
    """
    active_weights: Mapping[str, float] = weights or {
        "train": 0.70,
        "validation": 0.15,
        "test": 0.15,
    }
    if abs(sum(active_weights.values()) - 1.0) > 1e-9:
        raise ValueError("split weights must sum to 1.0")
    names = list(active_weights)
    weights = active_weights
    splits: Dict[str, List[Example]] = {name: [] for name in names}
    for example in examples:
        value = bucket_fraction(example.group, salt)
        cumulative = 0.0
        chosen = names[-1]
        for name in names:
            cumulative += weights[name]
            if value < cumulative:
                chosen = name
                break
        splits[chosen].append(example)
    return splits


def strip_shared_groups(train: Sequence[Example], evaluation: Sequence[Example]) -> Tuple[List[Example], int]:
    """Remove evaluation rows whose duplicate cluster also appears in training."""
    train_groups = {example.group for example in train}
    kept = [example for example in evaluation if example.group not in train_groups]
    return kept, len(evaluation) - len(kept)


def temporal_partition(
    examples: Sequence[Example], *, test_fraction: float = 0.20
) -> Tuple[List[Example], List[Example], str]:
    """Time split on dated rows, weighted by item count rather than month count.

    Returns the rows strictly older than the cutoff, the rows at or after the
    cutoff, and the cutoff itself. Months with no parsed year are excluded by
    the caller before this runs.
    """
    dated = [example for example in examples if example.date]
    if not dated:
        return [], [], ""
    counts = Counter(example.date for example in dated)
    months = sorted(counts)
    if len(months) < 3:
        return [], [], ""
    total = sum(counts.values())
    target = total * (1.0 - test_fraction)
    running = 0
    cutoff = months[-1]
    for month in months:
        running += counts[month]
        if running >= target:
            cutoff = month
            break
    older = [example for example in dated if example.date < cutoff]
    newer = [example for example in dated if example.date >= cutoff]
    if not older or not newer:
        return [], [], ""
    return older, newer, cutoff


def outlet_partition(
    examples: Sequence[Example],
    *,
    min_outlet_rows: int = 120,
    holdout_share: float = 0.15,
    salt: str = "veritas-outlet-v2",
) -> Tuple[List[Example], List[Example], List[str]]:
    """Hold entire outlets out of training and evaluate on them."""
    counts = Counter(example.outlet for example in examples if example.outlet)
    candidates = sorted(outlet for outlet, size in counts.items() if size >= min_outlet_rows)
    if len(candidates) < 3:
        return [], [], []
    ordered = sorted(candidates, key=lambda outlet: bucket_fraction(outlet, salt))
    target = sum(counts[outlet] for outlet in candidates) * holdout_share
    holdout: List[str] = []
    total = 0
    for outlet in ordered:
        if total >= target and holdout:
            break
        holdout.append(outlet)
        total += counts[outlet]
    held = set(holdout)
    train = [example for example in examples if example.outlet and example.outlet not in held]
    test = [example for example in examples if example.outlet in held]
    return train, test, sorted(holdout)


def describe(examples: Sequence[Example]) -> Dict[str, Any]:
    """Structural statistics used for reporting and leak auditing."""
    labels = Counter(example.label for example in examples)
    languages = Counter(example.language for example in examples)
    corpora = Counter(example.corpus for example in examples)
    outlets = Counter(example.outlet for example in examples if example.outlet)
    dates = [example.date for example in examples if example.date]
    texts = [example.text for example in examples]
    return {
        "items": len(examples),
        "label_counts": dict(sorted(labels.items())),
        "language_counts": dict(sorted(languages.items())),
        "corpus_counts": dict(sorted(corpora.items())),
        "distinct_outlets": len(outlets),
        "distinct_months": len(set(dates)),
        "date_range": [min(dates), max(dates)] if dates else [],
        "items_with_parsed_date": len(dates),
        "items_with_unparsed_date": sum(
            1 for example in examples if example.date_raw and not example.date
        ),
        "distinct_groups": len({example.group for example in examples}),
        "mean_chars": round(sum(len(text) for text in texts) / len(texts), 2) if texts else 0.0,
    }


__all__ = [
    "CORPORA",
    "DEFAULT_DATA_DIR",
    "CorpusIntegrityError",
    "Example",
    "assign_groups",
    "bottom_k_signature",
    "bucket_fraction",
    "describe",
    "download_corpus",
    "ensure_corpus",
    "exact_digest",
    "load_all",
    "load_corpus",
    "load_hindi_news",
    "load_ifnd",
    "normalize_text",
    "outlet_partition",
    "publisher_terms",
    "PUBLISHER_BRAND_TOKENS",
    "sha256_file",
    "shingle_hashes",
    "split_by_group",
    "strip_shared_groups",
    "temporal_partition",
]
