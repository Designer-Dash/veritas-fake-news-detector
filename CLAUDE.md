# CLAUDE.md — Veritas Project Guidelines for Claude

This document provides system guidelines, commands, and architectural context for Anthropic's **Claude** (Claude Code CLI, Claude.ai Projects, and Claude agents) working on the **Veritas (Fake & AI-Generated News Detector)** codebase.

---

## 📌 Project Overview

**Veritas** is an end-to-end fake news and AI-content detection system consisting of:
1. **FastAPI Backend (`backend/app`)**: Lightweight Python verification engine running on `http://127.0.0.1:8000`.
2. **Chrome Extension MV3 (`extension/`)**: In-place editorial newspaper banner and popup that evaluates articles live on news sites.

---

## ⚡ Primary Developer Commands

### Environment Setup
```bash
# Windows 1-click:
SETUP.bat

# Or manual terminal:
python -m venv backend/.venv
# Windows:
backend\.venv\Scripts\activate.bat
# Linux/macOS:
source backend/.venv/bin/activate
pip install -r backend/requirements-dev.txt
```

### Running the Server
```bash
# 1-click launcher:
01_START_SERVER.bat

# Or terminal:
python run.py
# Backend runs at http://127.0.0.1:8000 (Health check: http://127.0.0.1:8000/health)
# Swagger Docs: http://127.0.0.1:8000/docs
# Default dev mode: VERITAS_AUTH_DISABLED=1
```

### Running Tests
```bash
# 1-click launcher:
03_TEST_SYSTEM.bat

# Backend pytest suite (255+ deterministic offline tests):
pytest
# or:
python -m pytest

# Extension UI & Navigation invariant tests:
node tests/verify_popup_states.cjs
node tests/test_navigation_lifecycle.cjs
```

---

## 🏗️ Architecture & Core Components

```
backend/app/
├── main.py            # FastAPI app: POST /analyze, GET /health, Auth & Rate Limiting
├── pipeline.py        # Core decision engine combining 4 detection signals
├── verification.py    # Evidence score math (E = 0.30M + 0.30C + 0.20F + 0.20S)
├── cross_check.py     # Live Google News RSS multi-source retrieval (5–10 sources)
├── claim_analysis.py  # Claim extraction (Gemini / deterministic fallback) + published date parsing
├── ai_signals.py      # Media provenance (C2PA, IPTC DST, SynthID byte markers)
├── text_analysis.py   # Stylometric AI cues, burstiness, stock LLM phrases, clickbait density
├── config.py          # Environment-tunable thresholds and weights (VERITAS_*)
├── sources.py         # Outlet trust tier mappings (Tier 1/2/3)
└── security.py        # SSRF protection, private IP filtering, safe fetching

extension/
├── manifest.json      # Chrome Manifest V3
├── popup.html         # Compact 340px editorial newspaper card
├── popup.js           # Popup state rendering & star rating logic
├── content.js         # Shadow DOM in-page banner & SPA auto-navigation observer
├── background.js      # Service worker fetch relay (prevents mixed-content issues)
└── icons/             # Veritas shield icons
```

---

## ⚖️ Evidence Scoring Specification (Binding Invariants)

- **Formula**: $E = 0.30 \cdot M + 0.30 \cdot C + 0.20 \cdot F + 0.20 \cdot S$ (bounded 0–100).
- **M (Semantic Similarity)**: Similarity of article claim to corroborating sources.
- **C (Cross-Source Consensus)**: Weighted agreement across the reference pool: $((A + 0.5 \cdot U) / (A + U + D)) \times 100$.
- **F (Factual Consistency)**: Date, number, entity, and location consistency.
- **S (Source Quality)**: Weighted by outlet trust tiers (Tier 1: 1.00, Tier 2: 0.85, Tier 3: 0.65, Unknown: 0.40).
- **Contradiction Protection**: A strong trusted contradiction applies a flat $-15$ penalty to $E$.
- **Insufficient Evidence**: Fewer than 5 reference sources $\rightarrow$ `INSUFFICIENT_EVIDENCE`.

---

## 🎨 Extension Design System (Newspaper Editorial Theme)

- **Width**: Compact 340px card pinned top-right.
- **Palette**: Paper-white background (`#FAF8F5`), muted navy masthead (`#1E3A5F`), restrained red divider line (`#8B1E2B`, 3px).
- **Typography**: Playfair Display / Georgia serif for masthead and headlines; clean sans-serif for body/metrics.
- **Single Host Rule**: Only one `#veritas-banner-host` element must ever exist in the page DOM at any time.

---

## 🔒 Constraints & Rules for Claude

1. **Lightweight Backend**: Backend uses Python standard library + FastAPI only. Do not introduce heavy dependencies (such as torch, transformers, or scikit-learn).
2. **Preserve Mathematical Invariants**: Do not change the Evidence Score weights or contradiction penalty without running the full test suite.
3. **Preserve Signature Markers**: Maintain `0XX/013 · designer-dash` markers in source files.
4. **Dev Mode Default**: `VERITAS_AUTH_DISABLED=1` is set by default in runner scripts for frictionless extension development.
5. **Always Run Tests**: Before concluding any coding task, execute `pytest` to guarantee all 255+ tests pass with zero regressions.
