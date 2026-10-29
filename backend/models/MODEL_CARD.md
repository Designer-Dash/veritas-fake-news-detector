# Veritas Text-Risk Models — Model Card

This document provides audit cards for Veritas's bundled, reproducible text-risk classifiers.
Neither model is a factual verifier; both run as weak, bounded advisory signals in accordance with Veritas's deterministic cross-source evidence math ($E = 0.30M + 0.30C + 0.20F + 0.20S$).

---

## 1. Veritas Indic News MNB v2 (`indic_news_mnb_v2.json`) — Default Model

### Purpose

`indic_news_mnb_v2.json` is a domain-matched, bilingual (English + Hindi) multinomial Naive Bayes text-risk classifier trained on Indian news articles and headlines. It evaluates whether linguistic style and n-gram patterns resemble known fabricated news, while strictly preserving zero-weight shadow-mode guarantees if deployment gates are not cleared.

### Provenance and Datasets

- **IFND (Indian Fake News Dataset)**: 56,714 items of Indian news headline / statement text in English.
  - Distribution: Public research mirror (`IFND.csv`).
  - SHA-256: `3728c7b785ea93c9dadcc61a4c549c0eddd71a5fb6a1a31be08b5235ee4f5bfa`
- **Hindi News Real/Fake Articles (Zenodo 11408513)**: 20,593 full-length Hindi news articles.
  - Distribution: Zenodo record 10.5281/zenodo.11408513 (`Hindi_F&R_News.zip`).
  - SHA-256: `5a613af50db4164687b8808ed75a783af88d7c449982ebe8a178affc21d3337e`
- **Combined Corpus**: 77,307 total news items (56,714 English, 20,593 Hindi).
- **Reproducible Command**: `cd backend && python tools/train_indic_model.py --download`

### Architecture and Preprocessing

- **Format**: `veritas-text-model-v2` (plain validated JSON; zero pickle or executable code).
- **Tokenizer**: `unicode-words-v2` — explicitly preserves Indic script ranges (`\u0900-\u0dff`) and combining vowel marks (matras) so Hindi/Indic words remain intact.
- **Vocabulary**: 120,000 features (word unigrams + adjacent bigrams, document frequency $\ge 2$).
- **Anti-Leakage Safeguards**:
  - **Cluster-based Splits**: 58,102 distinct clusters derived from BLAKE2b digests; whole clusters are assigned to a single split, guaranteeing zero duplicate or near-duplicate leakage across train, validation, and test.
  - **Publisher Redaction**: 57 publisher and fact-checker names (e.g., `thehindu`, `ndtv`, `boomlive`, `altnews`, `reuters`, `ani`, `pti`) are strictly redacted so the model cannot exploit outlet name shortcuts.
  - **Metadata Excluded**: Outlet, publication month, category, and image URL columns are never converted into features.
- **Calibration**: Piecewise linear calibration fitted on validation split to align posterior probabilities with empirical false rates.

### Evaluation and Deployment Status

| Split | Examples | Accuracy | Macro F1 | False-Class Precision | False-Class Recall | False-Class F1 |
|---|---:|---:|---:|---:|---:|---:|
| **Validation** | 12,117 | 94.91% | 94.59% | 97.17% | 89.70% | 93.28% |
| **Test** | 11,496 | 94.05% | 93.64% | 94.75% | 89.47% | 92.03% |
| **Outlet-Holdout** | 3,611 | 95.40% | 94.46% | 98.59% | 86.57% | 92.19% |
| **Temporal-Holdout** | 3,225 | 99.50% | 83.88% | 51.52% | 100.0% | 68.00% |

- **Deployment Decision**: `shadow_only` (contributes 0.00 suspicion weight in production).
- **Gate Evaluation**:
  - Validation Precision $\ge 70\%$: **Passed** (97.17% at threshold 0.80).
  - Validation Flagged Volume $\ge 25$: **Passed** (4,410 flagged).
  - Test Precision $\ge 60\%$: **Passed** (94.75%).
  - Outlet Holdout Precision $\ge 50\%$: **Passed** (98.59%).
  - Temporal Holdout Volume $\ge 50$: **Failed** (only 17 items available, as IFND authors omitted publication years for historical fake news rows).
  - Since temporal support was insufficient, Veritas's strict gate policy keeps this model in **shadow mode**.

---

## 2. Veritas LIAR MNB Baseline (`liar_mnb_v1.json`) — Legacy Baseline

### Purpose

`liar_mnb_v1.json` is an auditable multinomial Naive Bayes baseline trained on English political fact-check statements. It is retained for comparison and can be selected via `VERITAS_TEXT_MODEL_PATH`.

### Provenance and Training

- **Dataset**: LIAR v1.0, short English PolitiFact claims.
- **Source**: https://www.cs.ucsb.edu/~william/data/liar_dataset.zip
- **Reproducible Command**: `cd backend && python tools/train_text_model.py --download`
- **Splits**: Train 10,240; Validation 1,284; Test 1,267.
- **Labels**: `false` (pants-fire/false), `mixed` (barely-true/half-true), `supported` (mostly-true/true).
- **Features**: Lower-cased word unigrams and bigrams, top 20,000 document-frequency vocabulary, additive smoothing $\alpha = 1.0$.
- **Artifact SHA-256**: `dff7b9523f768d4148c6aedcb73ae4878d6ad97e8464e7ce2ce41b777e2848e9`

### Evaluation and Deployment Status

| Split | Accuracy | Macro F1 | False-Class Precision | False-Class Recall | False-Class F1 |
|---|---:|---:|---:|---:|---:|
| **Validation** | 45.17% | 44.91% | 43.40% | 39.05% | 41.11% |
| **Test** | 43.96% | 43.21% | 40.07% | 34.90% | 37.30% |

- **Deployment Decision**: `shadow_only` (validation precision 45.29% falls below the 70% threshold; contributes 0.00 suspicion weight).

---

## 3. Universal Constraints & Safety Principles

1. **Advisory Signal Only**: Neither model can decide a verdict or reduce suspicion.
2. **Authority Hierarchy**: Deterministic cross-source evidence, trusted wire service consensus, and fact-check records strictly override text model probabilities.
3. **Auditability**: All model files are serialized as human-readable, schema-validated JSON with embedded calibration curves and deployment condition reports.
