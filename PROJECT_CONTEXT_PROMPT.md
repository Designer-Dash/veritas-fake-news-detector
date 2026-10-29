# Project Context Prompt — Veritas (Fake & AI-Generated News Detector)

You are continuing work on **Veritas**, a Chrome extension (MV3) + FastAPI backend that tells readers *in place* whether a news article looks fake or AI-generated, and shows why.

## What the product does

- The extension's `content.js` extracts article **text** (no HTML meta tags) and sends it as the `html` field (capped 400 KB) plus a separate `headline` to `POST /analyze`.
- The backend returns a verdict (`trusted → uncertain → suspicious → fake`), a suspicion score, confidence, corroborating sources, and an evidence list.
- Requests are relayed through the MV3 service worker (`background.js`) to avoid mixed-content blocking on HTTPS pages.

## Quick Run & Test Commands

- **Master Menu**: Run [`00_QUICK_START.bat`](file:///c:/Users/Lenovo/OneDrive/New%20folder/fake%20news%20detection/00_QUICK_START.bat) for 1-click launcher.
- **Start Backend**: `python run.py` (or [`01_START_SERVER.bat`](file:///c:/Users/Lenovo/OneDrive/New%20folder/fake%20news%20detection/01_START_SERVER.bat)) — launches FastAPI on `http://127.0.0.1:8000`.
- **Load Chrome Extension**: [`02_LOAD_EXTENSION.bat`](file:///c:/Users/Lenovo/OneDrive/New%20folder/fake%20news%20detection/02_LOAD_EXTENSION.bat) — copies extension path and opens Chrome extension manager.
- **Run Unit Tests**: [`03_TEST_SYSTEM.bat`](file:///c:/Users/Lenovo/OneDrive/New%20folder/fake%20news%20detection/03_TEST_SYSTEM.bat) (or `cd backend && python -m pytest -q`).

## Architecture (backend/app)

- `main.py` — FastAPI: `POST /analyze`, `GET /health`; API-key auth on by default (`VERITAS_AUTH_DISABLED=1` for dev), per-user sliding-window rate limits.
- `pipeline.py` — orchestrates all signals into one explainable verdict; evidence branching is **contradiction-safe** (a strong contradiction is never masked by a trusted-link hint); slim verification copy in `evidence.details`, full breakdown in `ai_signals["verification"]`; per-request `CallBudget`; debug logs.
- `claim_analysis.py` — `extract_claim` (Gemini when key configured, deterministic fallback), `normalize_claim` (light stemmer), `generate_search_queries`, `extract_published_date(content, url)`.
- `cross_check.py` — `retrieve_reference_sources`: Google News RSS retrieval of strongest **5–10** reference sources; candidate deduplication, self-outlet exclusion, parallel fetches.
- `verification.py` — deterministic scoring engine: `calculate_semantic_similarity`, `calculate_consensus`, `calculate_fact_consistency`, `calculate_source_quality`, `calculate_evidence_score`, `apply_contradiction_penalty`, `detect_breaking_news`.
- `sources.py` — TIER1/2/3 outlet registry & weights.
- `gemini_client.py` — structured JSON budget client (analysis only, never verdicts).
- `config.py` — env-tunable `VERITAS_*` knobs.
- `cache.py` — TTL caches for claims, RSS results, verifications.
- `textnorm.py` — shared stdlib primitives.

## Scoring spec (binding)

- **Evidence Score**: $E = 0.30 \cdot M + 0.30 \cdot C + 0.20 \cdot F + 0.20 \cdot S$, clamped 0–100.
- **Contradiction protection**: strong trusted contradiction $\rightarrow$ flat $E - 15$ penalty.
- **Insufficient evidence**: $< 5$ sources $\rightarrow$ `INSUFFICIENT_EVIDENCE`.
- **Breaking news**: very recent + thin coverage $\rightarrow$ `BREAKING_NEWS` / `EARLY_REPORT`.

## Constraints

- Zero UI/extension changes unless explicitly requested.
- No extra dependencies (stdlib + FastAPI stack only).
- Signature markers `0XX/013 · designer-dash` preserved.
