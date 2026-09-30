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
| **Cross-source corroboration** | Story matched against Google News coverage **restricted to trusted regional outlets** (India: TOI, India Today, The Hindu, NDTV, HT, Reuters, AP…), shingle-overlap similarity | Explainable multi-source verification (`s41599-025-05389-4`) |
| **Domain reputation** | Known trusted outlets get a bonus; lookalike TLDs (`.com.co`, `.news`, `.xyz`), hyphen-heavy and blog-mashup domains get penalties | Classic fake-news feature work |

Every signal contributes a signed weight; the sum is a **suspicion score** mapped
to four verdicts: `trusted → uncertain → suspicious → fake`. All evidence is
returned so the UI can *explain* the verdict (explainability was the core theme
of your base paper).

## Repository layout

```
backend/
  app/
    main.py          FastAPI app: POST /analyze, GET /health
    pipeline.py      Combines all signals into one explainable verdict
    ai_signals.py    C2PA / IPTC DST / SynthID / generator-marker scanning
    text_analysis.py Stylometry + clickbait/hedging heuristics (no deps)
    cross_check.py   Google News RSS search + trusted-outlet matching
    sources.py       Trusted-outlet registry by region (India-first)
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

20 deterministic, offline tests cover text extraction/scoring, cross-check
similarity and self-outlet exclusion, pipeline verdicts (with mocked network),
and the API contract. Live behavior against real news pages is verified
separately (see the live-test reports in the project history).

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
  permissions accordingly) and add **authentication** (per-user API keys/OAuth)
  — the local endpoint is unauthenticated by design for development.
- Set `VERITAS_EXTENSION_ID` to your published extension's ID to lock CORS.
- The in-memory rate limiter is per-process; use Redis for multi-instance
  deployments.

## Notes & limits (v0.1)

- The AI-watermark checks are metadata/byte-marker based: they catch *declared*
  provenance and tool leakage, not statistical SynthID verification (needs Google's
  detector, not public).
- Cross-checking fetches live pages; a failing network or paywall lowers
  confidence rather than raising suspicion.
- Heuristics are intentionally transparent; the ML roadmap upgrades accuracy
  without changing the extension contract (`POST /analyze`).
