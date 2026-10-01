# 🛡️ Veritas — Fake & AI-Generated News Detector

A Chrome-extension + backend system that tells users **in place** whether the news
they are reading looks fake or AI-generated — and shows **why**.

Install the extension once, browse normally; when a page looks like news it is
analyzed automatically and a banner appears with a verdict, the evidence behind
it, and links to trusted coverage of the same story.

## How it detects (v0.1 signals)

| Signal | What it checks | Inspired by |
|---|---|---|
| **Media provenance** | C2PA / Content Credentials manifests, IPTC *Digital Source Type* codes, generator EXIF/XMP markers (Midjourney, Stable Diffusion, Firefly…), SynthID-style byte markers | IPTC/C2PA standards; watermark-survey literature |
| **AI-writing cues** | Sentence-length burstiness, vocabulary richness, stock LLM phrases, emoji/hashtag/clickbait density | Stylometric fake-news detection (e.g. `s41598-021-03100-6`) |
| **Cross-source corroboration** | The underlying **claim** is extracted (Gemini when a key is configured, deterministic fallback otherwise), searched against the reference-outlet pool with 2–3 optimized queries, and the strongest **5–10 sources** are compared by semantic similarity — not exact headline matching — then scored as weighted SUPPORTS / CONTRADICTS / UNKNOWN consensus | Explainable multi-source verification (`s41599-025-05389-4`) |
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
uvicorn app.main:app --reload --port 8000
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

111 deterministic, offline tests cover text extraction/scoring, cross-check
similarity and self-outlet exclusion, claim extraction/normalization,
consensus/evidence-score math, contradiction penalties, breaking-news
detection (including the source-freshness gate), source weighting,
retrieval caps, the injectable SourceRetriever boundary, the
VERITAS_DEBUG payload, pipeline verdicts (with mocked network), and the
API contract. Gemini is force-disabled in tests; its HTTP
call is covered with a mocked transport. Live behavior against real news
pages is verified separately (see the live-test reports in the project
history).

> **Architecture note:** the content script never calls the backend directly.
> On an HTTPS page that fetch would be blocked as mixed content (verified in
> a real browser); requests are relayed through the MV3 service worker
> (`background.js`), which is exempt from the page's mixed-content rules.

## Roadmap (next milestones)

1. **Trained ML classifier** — replace the stylometry heuristics with a fine-tuned
   transformer (e.g. `roberta-base` or a multilingual `indic-bert`/MuRIL for Hindi +
   English Indian news) trained on LIAR, FakeNewsNet and the Kaggle fake-news sets.
   Keep the heuristic scores as explanation features.
2. **Real AI-image detection** — swap the byte-marker scan for a CNN/ViT detector
   (e.g. UniversalFakeDetect or SynthID-verify via API) and a reverse-image search
   (Google Lens API) to catch re-used/out-of-context photos.
3. **Claim matching instead of headline matching** — extract claims, embed with
   sentence-transformers, and do semantic nearest-neighbor search against fact-check
   APIs (Google Fact Check Tools, Alt News, BOOM, PIB Fact Check for India).
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
- Cross-checking fetches live pages; a failing network or paywall lowers
  confidence rather than raising suspicion.
- Heuristics are intentionally transparent; the ML roadmap upgrades accuracy
  without changing the extension contract (`POST /analyze`).
