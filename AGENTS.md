# 🛡️ AGENTS.md — Veritas Project Memory Blueprint

This memory blueprint describes the **Veritas (Fake & AI-Generated News Detector)** codebase for AI agents, pair programmers, and future development sessions.

---

## 📌 Project Summary

**Veritas** is an end-to-end fake news and AI-content detection system featuring:
1. **FastAPI Backend (`backend/app`)**: Core detection engine running on `http://localhost:8000`.
2. **Chrome Extension (MV3) (`extension/`)**: In-place banner analysis on news websites.

---

## ⚡ Quick Launch Commands (Top-Level 1-Click Launchers)

| Purpose | Command / Script | Description |
|---|---|---|
| **Master Control Menu** | `00_QUICK_START.bat` | Interactive terminal menu to launch server, extension, or tests from one screen. |
| **Start Server Only** | `01_START_SERVER.bat` (or `python run.py`) | Runs FastAPI backend on `http://localhost:8000` with dev mode (`VERITAS_AUTH_DISABLED=1`). |
| **Load Chrome Extension** | `02_LOAD_EXTENSION.bat` | 1-click extension loader: copies path to clipboard and launches Chrome extensions manager. |
| **Run Test Suite** | `03_TEST_SYSTEM.bat` (or `pytest`) | Runs all offline deterministic backend tests with verbose summary. |
| **Run Chaos Soak Harness** | `cd backend && python tools/soak.py --duration 3600` | Runs 1-hour extreme scenario soak test asserting 9 structural invariants. |

---

## 🏗️ Architecture & Core Components

```
backend/app/
├── main.py          # FastAPI app: POST /analyze, GET /health, Auth & Rate Limiting
├── pipeline.py      # Core decision engine combining all 4 detection signals
├── verification.py  # Evidence score math (E = 0.30M + 0.30C + 0.20F + 0.20S)
├── cross_check.py   # Parallel RSS retrieval (Google News) for 5–10 reference sources
├── claim_analysis.py# Claim extraction (Gemini / deterministic fallback) + published date parsing
├── ai_signals.py    # Media provenance (C2PA, IPTC DST, SynthID, generator EXIF)
├── text_analysis.py # Stylometric AI cues, burstiness, stock LLM phrases, clickbait density
├── config.py        # All thresholds/weights (env-tunable VERITAS_*)
├── sources.py       # Tier 1/2/3 trusted outlet registry & weight mappings
└── security.py      # SSRF protection, URL validation, public IP filter

extension/
├── manifest.json    # Manifest V3
├── content.js       # Extracts article text & renders Shadow DOM banner
└── background.js    # Service worker fetch relay (prevents mixed-content blocking)
```

---

## ⚖️ Evidence Scoring Specification (Binding)

- **Evidence Score Formula**: $E = 0.30 \cdot M + 0.30 \cdot C + 0.20 \cdot F + 0.20 \cdot S$ (bounded 0–100).
- **M (Semantic Similarity)**: Similarity to corroborating sources (SUPPORTS / CONTRADICTS).
- **C (Cross-Source Consensus)**: Weighted agreement across reference pool.
- **F (Factual Consistency)**: Date/number/entity consistency across outlets.
- **S (Source Quality)**: Weighted by outlet trust tiers (Tier 1: 1.00, Tier 2: 0.85, Tier 3: 0.65, Unknown: 0.40).
- **Contradiction Protection**: A strong trusted contradiction applies a flat $-15$ penalty to $E$.
- **Insufficient Evidence**: $< 5$ reference sources $\rightarrow$ `INSUFFICIENT_EVIDENCE`.

---

## 🔒 Constraints & Rules for Agents

1. **Zero Extension Changes**: Do not touch `extension/*` unless explicitly requested.
2. **No Extra Heavy Dependencies**: Keep backend stdlib + FastAPI only.
3. **Preserve Signature Markers**: Maintain `0XX/013 · designer-dash` markers in source files.
4. **Dev Mode Default**: `VERITAS_AUTH_DISABLED=1` is set by default in runner scripts for seamless extension development.
