# 🛡️ Veritas — Fake & AI-Generated News Detector

A Chrome-extension + backend system that tells users **in place** whether the news
they are reading looks fake or AI-generated — and shows **why**.

Install the extension once, browse normally; when a page looks like news it is
analyzed automatically and a banner appears with a verdict, the evidence behind
it, and links to trusted coverage of the same story.

---

## ⚡ 1-Click Quick Start (Collaborators & Friends)

Getting started is 1-click simple on Windows:

```bash
# 1. Clone the repo
git clone https://github.com/Designer-Dash/veritas-fake-news-detector.git
cd veritas-fake-news-detector

# 2. Run 1-Click Setup (creates Python venv & installs dependencies)
SETUP.bat
```

| Launcher | Description |
|---|---|
| **`00_QUICK_START.bat`** | Interactive menu to launch backend, extension, or tests from one screen. |
| **`SETUP.bat`** | Creates isolated virtual environment and installs all dependencies. |
| **`01_START_SERVER.bat`** | Runs backend server on `http://localhost:8000` with dev mode. |
| **`02_LOAD_EXTENSION.bat`** | 1-click helper: copies extension path & opens Chrome extensions manager. |
| **`03_TEST_SYSTEM.bat`** | Runs full offline test suite (`pytest`) — 255+ tests, 100% passing. |

> 📖 **Want to edit and contribute?** Read **[CONTRIBUTING.md](CONTRIBUTING.md)** for developer instructions, branching, and pushing code back to GitHub!

---

## How it detects (v0.1 signals)

| Signal | What it checks | Inspired by |
|---|---|---|
| **Media provenance** | C2PA / Content Credentials manifests, IPTC *Digital Source Type* codes, generator EXIF/XMP markers (Midjourney, Stable Diffusion, Firefly…), SynthID-style byte markers | IPTC/C2PA standards; watermark-survey literature |
| **AI-writing cues** | Sentence-length burstiness, vocabulary richness, stock LLM phrases, emoji/hashtag/clickbait density | Stylometric fake-news detection (e.g. `s41598-021-03100-6`) |
| **Claim-text ML baseline** | Auditable multinomial Naive Bayes over word/bigram features, trained only on the text of LIAR fact-check claims; emits a structured diagnostic signal and is validation-gated before it can affect suspicion | Wang (ACL 2017) LIAR benchmark |
| **Cross-source corroboration** | The underlying **claim** is extracted (Gemini when a key is configured, deterministic fallback otherwise), searched against the reference-outlet pool with 2–3 optimized queries (fetched in parallel), and the strongest **5–10 sources** are compared by semantic similarity — not exact headline matching — then scored as weighted SUPPORTS / CONTRADICTS / UNKNOWN consensus | Explainable multi-source verification (`s41599-025-05389-4`) |
| **Domain reputation** | Known trusted outlets get a bonus; lookalike TLDs (`.com.co`, `.news`, `.xyz`), hyphen-heavy and blog-mashup domains get penalties | Classic fake-news feature work |

Every signal contributes a signed weight; the sum is a **suspicion score** mapped
to four verdicts: `trusted → uncertain → suspicious → fake`. All evidence is
returned so the UI can *explain* the verdict (explainability was the core theme
of your base paper).

### Evidence scoring (claim-based verification)

The cross-source layer computes a deterministic **Evidence Score** `E` (0–100,
not a probability of truth) from four normalized components:

```
E = 0.30·M + 0.30·C + 0.20·F + 0.20·S

M = semantic similarity of the claim to the strongest relevant sources
C = weighted cross-source consensus  ((A + 0.5·U) / (A + U + D)) × 100
F = factual consistency (dates/numbers/entities/locations across sources)
S = source-quality score (configurable trust tiers: 1.00 / 0.85 / 0.65 / 0.40)
```

A strong contradiction from a high-weight trusted source applies a flat
configurable penalty (−15) so weak sources cannot bury it. Very recent
stories with thin coverage are marked internally `BREAKING_NEWS` /
`EARLY_REPORT` — but only if the reference pool is fresh too (median
source age ≤ `VERITAS_BREAKING_MAX_MEDIAN_AGE_HOURS`, default 2h; a stale
pool is recycled coverage, not a just-happened event). Thin pools (< 5
sources) become `INSUFFICIENT_EVIDENCE`, and neither is treated as evidence
against the article. Gemini is used only for structured analysis (claim
extraction, batched pairwise comparison) — the score itself is always
computed by the backend. All thresholds and weights live in
`backend/app/config.py` and are env-tunable (`VERITAS_*`); setting
`VERITAS_DEBUG=1` emits one JSON `debug_payload` per analysis at DEBUG level
(developer diagnostics only — never part of the API response).

### Tuning knobs (`VERITAS_*` env vars)

| Variable | Default | Effect |
|---|---|---|
| `VERITAS_W_SEMANTIC` / `VERITAS_W_CONSENSUS` / `VERITAS_W_FACTS` / `VERITAS_W_SOURCE` | 0.30 / 0.30 / 0.20 / 0.20 | Evidence Score weights (E = w·M + w·C + w·F + w·S) |
| `VERITAS_TIER1_WEIGHT` / `VERITAS_TIER2_WEIGHT` / `VERITAS_TIER3_WEIGHT` / `VERITAS_UNKNOWN_WEIGHT` | 1.00 / 0.85 / 0.65 / 0.40 | Source-quality weight per trust tier |
| `VERITAS_MIN_SOURCES` / `VERITAS_MAX_SOURCES` | 5 / 10 | Verification window; fewer than min → `INSUFFICIENT_EVIDENCE` |
| `VERITAS_OUTLET_CHECKS` | 5 (bounded 5-10) | Separately scoped Google News `site:` checks attempted before a cross-source result; unsuccessful checks do not fabricate coverage |
| `VERITAS_SUPPORTS_SIMILARITY` / `VERITAS_RELEVANCE_SIMILARITY` | 60 / 40 | Relationship thresholds on the 0–100 similarity blend |
| `VERITAS_RETRIEVAL_FLOOR` | 0.15 | RSS results below this relevance are dropped |
| `VERITAS_CONTRADICTION_PENALTY` | 15 | Flat E deduction when a strong trusted source contradicts |
| `VERITAS_STRONG_CONTRA_WEIGHT` / `VERITAS_STRONG_CONTRA_SIM` | 0.85 / 40 | What counts as a "strong" contradiction |
| `VERITAS_CROSS_SLOPE` | 1.0 | suspicion += (0.5 − E/100) × slope |
| `VERITAS_NO_COVERAGE_PENALTY` / `VERITAS_BREAKING_PENALTY` | 0.15 / 0.05 | Cross-source weight when zero sources are selected |
| `VERITAS_FACT_UNMENTIONED_FACTOR` | 0.5 | Credit for facts no source confirms or denies |
| `VERITAS_BREAKING_WINDOW_HOURS` | 48 | Article recency window (URL-only dates are date-granular — see `config.py`) |
| `VERITAS_BREAKING_MIN_SOURCES` | 3 | Below this count a recent story may be breaking |
| `VERITAS_BREAKING_MAX_MEDIAN_AGE_HOURS` | 2.0 | Source-freshness gate: older median pool ⇒ never breaking |
| `VERITAS_GEMINI_ENABLED` / `VERITAS_GEMINI_MAX_CALLS` | auto / 2 | Structured analysis budget (never the verdict) |
| `VERITAS_TEXT_MODEL_ENABLED` | on | Load the local claim-text model and return its diagnostic signal |
| `VERITAS_TEXT_MODEL_MIN_TOKENS` / `VERITAS_TEXT_MODEL_MIN_COVERAGE` | 30 / 0.10 | Refuse short or out-of-distribution text instead of using model priors |
| `VERITAS_TEXT_MODEL_FALSE_THRESHOLD` / `VERITAS_TEXT_MODEL_MAX_WEIGHT` | 0.80 / 0.18 | Conservative activation threshold and hard suspicion cap for a validated model |
| `VERITAS_TEXT_MODEL_ALLOW_UNQUALIFIED` | off | Experimental override for a model that failed its validation precision gate; never enable in production |
| `VERITAS_FACT_CHECK_API_KEY` | empty | Google Fact Check Tools API key; feature dormant when unset |
| `VERITAS_FACT_CHECK_ENABLED` / `VERITAS_FACT_CHECK_MAX_RESULTS` / `VERITAS_FACT_CHECK_TIMEOUT` | on / 5 / 6.0 | Fact-check review retrieval |
| `VERITAS_FACT_CHECK_WEIGHT` / `VERITAS_FACT_CHECK_REVIEWER_WEIGHT` | 0.30 / 1.0 | F′ = (1−w)·F + w·FC blend; per-review weight |
| `VERITAS_FACT_CHECK_CACHE_TTL` | 300 | Per-claim review cache (same default as verification cache) |
| `VERITAS_CONFIDENCE_BASE` / `VERITAS_CONF_TEXT_BONUS` / `VERITAS_CONF_VERIFICATION_BONUS` / `VERITAS_CONF_DOMAIN_BONUS` / `VERITAS_CONFIDENCE_CAP` | 0.55 / 0.15 / 0.15 / 0.05 / 0.90 | Confidence increments; cap equals the reachable maximum |
| `VERITAS_TLD_WHITELIST` | empty | Comma-separated registrable domains exempt from the lookalike-TLD penalty |
| `VERITAS_DEBUG` | off | One JSON `debug_payload` per analysis at DEBUG level |
| `VERITAS_RATE_MAX` / `VERITAS_RATE_WINDOW_SEC` / `VERITAS_MAX_CONCURRENT` | see `config`/security section | Abuse limits |
| `VERITAS_EXTENSION_ID` / `VERITAS_AUTH_DISABLED` | empty / off | CORS lockdown; dev auth opt-out |

Every knob is read per request (`app/config.py:get_config()`), so changes take
effect without a server restart.

### Claim-text model: reproducible baseline, not a truth engine

`backend/models/liar_mnb_v1.json` is a standard-library multinomial Naive
Bayes artifact. It uses only statement text: no speaker, party, venue, subject,
or past fact-check-history columns are allowed into training because those fields
would leak information not present in a submitted article. The three preserved
labels are `false` (`pants-fire`/`false`), `mixed` (`barely-true`/`half-true`),
and `supported` (`mostly-true`/`true`).

Rebuild it from scratch (the downloaded TSV files are ignored by Git):

```bash
cd backend
python tools/train_text_model.py --download
```

The tool downloads the public LIAR train/validation/test splits, writes input
SHA-256 values, parameters, label mapping, held-out metrics, and deployment-gate
status into the JSON artifact. It has no `scikit-learn`, PyTorch, or transformer
dependency. The model loader accepts only validated JSON, never pickle.

The current baseline is **shadow-only**. On the untouched LIAR split it scored:

| Split | Examples | Accuracy | Macro F1 | `false` F1 |
|---|---:|---:|---:|---:|
| Validation | 1,284 | 45.17% | 44.91% | 41.11% |
| Test | 1,267 | 43.96% | 43.21% | 37.30% |

At the conservative `false ≥ 0.80` operating point, validation precision was
45.29% (test: 39.86%), which fails the required 70% validation-precision gate.
Therefore it appears in `ai_signals["text_classifier"]` for inspection but has
**zero effect on suspicion or the verdict**. This is intentional: a weak text
classifier must not outrank deterministic cross-source evidence. It can only
become an advisory, capped signal after a domain-matched model clears the gate;
`VERITAS_TEXT_MODEL_ALLOW_UNQUALIFIED=1` exists solely for offline experiments.

LIAR contains short English PolitiFact claims, not full articles and not Hindi
or multilingual Indian news. The runtime refuses text with low known-vocabulary
coverage rather than producing a false-looking probability. The LIAR mirror
states that it is for research use; review dataset licensing before any
commercial deployment.

## Repository layout

```
backend/
  app/
    main.py          FastAPI app: POST /analyze, GET /health
    pipeline.py      Combines all signals into one explainable verdict
                     (SourceRetriever = injectable retrieval boundary)
    ai_signals.py    C2PA / IPTC DST / SynthID / generator-marker scanning
    text_analysis.py Stylometry + clickbait/hedging heuristics (no deps)
    claim_analysis.py  Claim extraction/normalization + search queries (Gemini or fallback)
    cross_check.py   Google News RSS retrieval of the strongest 5-10 reference sources
    verification.py  Deterministic evidence scoring: M/C/F/S/E, consensus, penalties
    config.py        All scoring thresholds/weights (env-tunable VERITAS_*)
    gemini_client.py Budgeted/cached Gemini JSON client (analysis only, never verdicts)
    cache.py         TTL caches: claims, RSS results, verifications
    sources.py       Trusted-outlet registry + configurable source-weight tiers
    textnorm.py      Shared stopword/suffix/date primitives (breaks layer cycle)
    factcheck.py     Google Fact Check Tools API client (F-component blend)
    domain.py        Dataclasses: Evidence, SourceMatch, AnalysisResult
  requirements.txt

extension/
  manifest.json      MV3 manifest
  content.js         Extracts article, requests analysis, renders verdict banner
  background.js      Service worker: backend fetch relay (mixed-content-safe)
  popup.html/js      Toolbar popup with verdict
  options.html/js    Region + backend settings
```

## Quick start

### 1. Run the backend

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt

# Dev mode: auth OFF so the extension works out of the box
# (PowerShell)  $env:VERITAS_AUTH_DISABLED="1"; uvicorn app.main:app --reload --port 8000
# (cmd)         set VERITAS_AUTH_DISABLED=1 && uvicorn app.main:app --reload --port 8000
# (bash/zsh)    VERITAS_AUTH_DISABLED=1 uvicorn app.main:app --reload --port 8000
```

Health check: `curl http://localhost:8000/health`

### 2. Load the extension

1. Chrome / Edge / Brave → `chrome://extensions` (or `edge://extensions`, `brave://extensions`)
2. Enable **Developer mode**
3. **Load unpacked** → select the `extension/` folder
4. Open any news article — a banner appears within a few seconds.

### 3. Try it

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.thehindu.com/news/national/some-story/", "region": "in"}'
```

### 4. Run the tests

```bash
cd backend
python -m pytest -q
```

114 deterministic, offline tests cover text extraction/scoring, cross-check
similarity and self-outlet exclusion, claim extraction/normalization,
consensus/evidence-score math, contradiction penalties, breaking-news
detection (including the source-freshness gate, end to end), source
weighting, retrieval caps, the injectable SourceRetriever boundary, the
VERITAS_DEBUG payload, evidence-weight/suspicion consistency, pipeline
verdicts (with mocked network), and the API contract. Gemini is force-disabled in tests; its HTTP
call is covered with a mocked transport. Live behavior against real news
pages is verified separately (see the live-test reports in the project
history).

### Extreme-scenario soak test

```bash
cd backend
python tools/soak.py --duration 3600   # one hour; Ctrl+C for partial summary
```

A stdlib-only chaos harness that hammers `analyze()` continuously across the
full scenario bank (6 URLs × 3 article styles × 3 link layouts) with randomized
retrieval failures — network `None`, empty bodies, garbage bytes, slow
endpoints, and RSS pools aged from 5 minutes to 30 days (stale pools behind
today-dated URLs, fresh pools for breaking news) — while asserting nine
structural invariants on every result: verdict vocabulary, bounded
suspicion/confidence, zero-score honesty, exactly-one cross-source evidence
whose weight equals the applied `cross_weight` (the ERR-1 invariant), JSON
debug payloads, evidence-score range, no own-outlet corroboration, no
breaking state without a publication date, and full response
serializability. Exit code 1 on any violation; every 7th iteration keeps
caches warm to exercise the cache-hit freshness path.

> **Architecture note:** the content script never calls the backend directly.
> On an HTTPS page that fetch would be blocked as mixed content (verified in
> a real browser); requests are relayed through the MV3 service worker
> (`background.js`), which is exempt from the page's mixed-content rules.

## Roadmap (next milestones)

1. **Domain-matched trained classifier** — the reproducible LIAR baseline now
   exists but failed its safety gate. Collect article-level, source-diverse,
   time-split English/Hindi Indian-news labels before considering a fine-tuned
   transformer (e.g. `roberta-base` or MuRIL). Report calibration, temporal
   holdout performance, and outlet-held-out performance; keep it subordinate to
   the deterministic evidence layer.
2. **Real AI-image detection** — swap the byte-marker scan for a CNN/ViT detector
   (e.g. UniversalFakeDetect or SynthID-verify via API) and a reverse-image search
   (Google Lens API) to catch re-used/out-of-context photos.
3. **Semantic claim matching at scale** — claim extraction and wording-tolerant
   matching are live (deterministic blend in `app/verification.py`); the next step
   is embedding-based nearest-neighbour search plus fact-check API lookups
   (Google Fact Check Tools, Alt News, BOOM, PIB Fact Check for India).
4. **Credibility database** — persist per-domain historical verdicts (NewsGuard-style)
   so frequent offenders are flagged instantly.
5. **Cross-browser store release** — the MV3 build runs on Chrome, Edge, Brave and
   other Chromium browsers; package for the Chrome Web Store and add Firefox via
   WebExtensions APIs.
6. **Report-back loop** — let users flag missed fakes; store labels for retraining.

## Security & privacy

**Permissions (least privilege).** `storage` (settings only), `activeTab` (read the
active tab URL on explicit popup use — avoids the broad `tabs` permission), and
host permissions for `http://localhost/*` + `http://127.0.0.1/*` so the service
worker can reach the dev backend. No `tabs`, `cookies`, `history`, `webRequest`,
`downloads`, `bookmarks`, no `<all_urls>`, no remote code, no eval.

**Untrusted content handling.** Webpage text is data, never instructions. The
banner renders exclusively via `textContent` in a **closed Shadow DOM** (page
JS/CSS cannot read or restyle it); zero `innerHTML` in the extension. URLs from
the backend are rendered only after an https-only allowlist check.

**Data minimization.** The extension uploads only the extracted article text
(capped at 400 KB server-side) — never the full DOM, so passwords/tokens on the
page are never transmitted. Backend responses are relayed to the UI through a
fixed field whitelist.

**SSRF defense (backend).** Every server-side fetch — page, RSS, image — goes
through `app/security.py`: https-only scheme allowlist, port allowlist, no
userinfo, DNS-resolved IPs must be globally public (blocks localhost, RFC1918,
link-local, cloud metadata), redirects re-validated per hop, hard size caps and
timeouts. Invalid input **fails closed** (generic 400s, no internal details).

**Abuse protection (backend).** Per-client sliding-window rate limit +
concurrency cap (env-tunable: `VERITAS_RATE_MAX`, `VERITAS_RATE_WINDOW_SEC`,
`VERITAS_MAX_CONCURRENT`), request bounds via pydantic, CORS restricted to the
extension origin via `VERITAS_EXTENSION_ID` (empty = same-origin only).

**Secrets.** None in the repo or the extension; the architecture keeps all
analysis server-side so future AI-provider keys never ship to clients.

### Production hardening checklist

- Serve the backend over **HTTPS** with a real domain (update extension host
  permissions accordingly).
- Set `VERITAS_EXTENSION_ID` to your published extension's ID to lock CORS.
- The in-memory rate limiter is per-process; use Redis for multi-instance
  deployments.

### Authentication (API keys)

Auth is **on by default**; local development opts out with
`VERITAS_AUTH_DISABLED=1` (the default dev server command in this README does
this so the extension works out of the box).

```bash
# create a key (plaintext shown once; only SHA-256 hash is stored)
cd backend
python tools/manage_keys.py create --name my-laptop --rate-max 60
python tools/manage_keys.py list
python tools/manage_keys.py revoke --key-id k_xxxxxxxx
```

- Keys look like `vfnd_<43 chars>`; the registry lives in
  `data/api_keys.json` (gitignored, mode 0600) and/or the `VERITAS_API_KEYS`
  env var as `id:<sha256-hex>:rate_max` entries — **never** plaintext.
- Verification is constant-time; the file registry hot-reloads, so
  `revoke` takes effect immediately without a server restart.
- Rate limits are keyed by **user** (`user:<key_id>`) with per-key
  `--rate-max` overrides; unauthenticated dev mode falls back to client IP.
- The extension stores the key in `chrome.storage.local` (never synced to
  Google accounts), sends it as `Authorization: Bearer …` to the configured
  backend only, and surfaces 401/429 as user-readable banner/popup errors.

### HTTPS deployment

```bash
# behind a TLS-terminating reverse proxy (Caddy example)
VERITAS_EXTENSION_ID=your_extension_id VERITAS_RATE_MAX=30 \
  uvicorn app.main:app --host 0.0.0.0 --port 8443 --proxy-headers
```

Then point the extension's Backend URL at `https://api.yourdomain.org` and
paste an API key in its settings. Rate-limit identities, CORS and the
security layer need no changes.

## Notes & limits (v0.1)

- The AI-watermark checks are metadata/byte-marker based: they catch *declared*
  provenance and tool leakage, not statistical SynthID verification (needs Google's
  detector, not public).
- Cross-checking fetches Google News RSS headlines (no paywalls); a failing
  network or empty result lowers confidence rather than raising suspicion, and
  is never treated as evidence against the article.
- The bundled LIAR model is a reproducible diagnostic baseline, not a deployable
  fake-news detector: it is English claim-level data, its validation gate is
  currently failed, and it has no authority over evidence scoring or verdicts.
