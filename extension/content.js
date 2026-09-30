// Veritas content script — "Read-While-Analyzing" UX.
//
// Phase 1 (instant, on page detection): an expanded card opens immediately
//   showing the headline + a short preview with an animated "Analyzing…"
//   state, so the user never waits staring at nothing.
// Phase 2 (3-8 s later, silent): the verdict animates into the same card.
//
// The fetch MUST go through the background service worker: a content script
// inside an HTTPS page is mixed-content-blocked from calling http://localhost
// directly (verified in live browser testing).
//
// SECURITY (unchanged): the host page is untrusted. All page/backend-derived
// strings are rendered via textContent only (no innerHTML anywhere), the
// widget lives in a CLOSED shadow root so page CSS/JS cannot read or restyle
// it, the only page data uploaded is the extracted article text — never the
// full DOM — and any URL rendered as a link must pass an https-only check.

function getBackendBase() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      { backend: "http://localhost:8000", region: "in", autoCheck: true },
      (cfg) => resolve(cfg)
    );
  });
}

function extractHeadline() {
  const og = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
  if (og && og.content) return og.content.trim().slice(0, 512);
  const h1 = document.querySelector("h1");
  return h1 ? h1.textContent.trim().slice(0, 512) : document.title.slice(0, 512);
}

function extractArticleText() {
  const candidates = [
    document.querySelector("article"),
    document.querySelector('[itemprop="articleBody"]'),
    document.querySelector("main"),
    document.body,
  ].filter(Boolean);
  for (const el of candidates) {
    const t = (el.innerText || "").replace(/\s+/g, " ").trim();
    if (t.length > 400) return t.slice(0, 12000);
  }
  return "";
}

// ---------------------------------------------------------------------------
// Verdict vocabulary — precise, never "FAKE". Fixed strings only, never built
// from page or backend data.
// ---------------------------------------------------------------------------

const VERDICTS = {
  supported: { dot: "#2e7d32", label: "Supported" },
  unverified: { dot: "#f9a825", label: "Unverified" },
  suspicious: { dot: "#ef6c00", label: "Misleading" },
  fake: { dot: "#c62828", label: "Contradicted" },
};

// Map backend verdicts to user-facing states. `ai_media` promotes the card to
// the distinct blue AI state.
function resolveState(verdict, aiMedia) {
  if (aiMedia && (verdict === "trusted" || verdict === "uncertain")) {
    return "ai";
  }
  if (verdict in VERDICTS) return verdict;
  return "unverified";
}

function stateMeta(state) {
  if (state === "ai") {
    return { dot: "#1565c0", label: "Possible AI-generated media" };
  }
  return VERDICTS[state];
}

// Suspicion bar color coding.
function suspicionColor(s) {
  if (s < 0.25) return "#22c55e";
  if (s < 0.45) return "#eab308";
  if (s < 0.70) return "#f97316";
  return "#ef4444";
}

// One-line, evidence-anchored explanation per state (fixed copy — no backend
// string is interpolated into these sentences; the dynamic reason is rendered
// separately as its own text node).
const STATE_EXPLANATIONS = {
  supported: "Trusted news outlets corroborate this story.",
  unverified: "No matching coverage was found in trusted outlets yet.",
  suspicious: "This conflicts with how trusted outlets report the story.",
  fake: "This claim conflicts with information from relevant trusted sources.",
  ai: "Media on this page shows signs of AI generation.",
};

const STYLE = {
  host:
    // The host anchors the widget; pointer-events:none guarantees the card
    // can never block page interactions outside its own visible box.
    "all:initial;position:fixed;top:12px;right:12px;z-index:2147483646;" +
    "display:block;pointer-events:none;",
  card:
    "all:initial;display:block;width:300px;max-width:calc(100vw - 24px);" +
    "background:#ffffff;border:1px solid rgba(0,0,0,.14);border-radius:12px;" +
    "box-shadow:0 4px 16px rgba(0,0,0,.16);" +
    "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
    "font-size:13px;color:#1f2933;line-height:1.45;overflow:hidden;" +
    "pointer-events:auto;",
  row: "display:flex;align-items:center;gap:8px;padding:10px 12px 2px;",
  body: "padding:2px 12px 10px;",
  footer:
    "display:flex;align-items:center;justify-content:space-between;" +
    "padding:8px 12px 10px;border-top:1px solid rgba(0,0,0,.08);",
  dismiss: "font-size:14px;color:#5f6b76;cursor:pointer;padding:2px 6px;",
  dot: "width:8px;height:8px;border-radius:50%;flex:none;",
  section:
    "font-size:11px;font-weight:600;letter-spacing:.04em;color:#5f6b76;" +
    "text-transform:uppercase;margin:8px 0 3px;",
  link: "color:#1a237e;font-weight:600;text-decoration:none;cursor:pointer;",
  chip:
    "display:inline-block;background:#f1f5f9;border-radius:999px;" +
    "padding:2px 8px;font-size:11px;margin-right:4px;color:#1f2933;",
  meter:
    "width:100%;height:4px;border-radius:999px;background:#e2e8f0;" +
    "margin:8px 0 2px;overflow:hidden;",
  meterFill: "height:4px;border-radius:999px;",
};

// Shadow-DOM stylesheet: spinner, shimmer placeholders, entrance animations.
const SHADOW_CSS = [
  "@keyframes veritas-spin { to { transform: rotate(360deg); } }",
  "@keyframes veritas-shimmer { 0% { background-position: -200px 0; } 100% { background-position: calc(200px + 100%) 0; } }",
  "@keyframes veritas-in { from { opacity:0; transform:translateY(-6px); } to { opacity:1; transform:none; } }",
  "@keyframes veritas-fade-in { from { opacity:0; } to { opacity:1; } }",
  "@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }",
  ".veritas-spinner { width:14px; height:14px; border:2px solid #e2e8f0; border-top-color:#6366f1; border-radius:50%; animation: veritas-spin 0.8s linear infinite; flex:none; }",
  ".veritas-shimmer { height:11px; border-radius:6px; width:80%; background: linear-gradient(90deg, #f1f5f9 25%, #e2e8f0 50%, #f1f5f9 75%); background-size:400px 100%; animation: veritas-shimmer 1.5s ease-in-out infinite; margin:5px 0; }",
].join("\n");

function el(tag, style, text) {
  const node = document.createElement(tag);
  if (style) node.setAttribute("style", style);
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------------------
// PHASE 1 — instant "Analyzing…" card. Opens expanded; no pill-first step.
// ---------------------------------------------------------------------------

function renderAnalyzing(headline, preview) {
  const old = document.getElementById("veritas-banner-host");
  if (old) old.remove();

  const host = document.createElement("div");
  host.id = "veritas-banner-host";
  host.setAttribute("style", STYLE.host);
  const shadow = host.attachShadow({ mode: "closed" }); // page JS cannot reach in
  host._veritasShadow = shadow; // applyVerdict/applyError access point

  const styleEl = document.createElement("style");
  styleEl.textContent = SHADOW_CSS;
  shadow.appendChild(styleEl);

  const card = el("div", STYLE.card);
  card.style.animation = "veritas-in 180ms ease-out";

  // Header: 🛡️ · Veritas + spinner + dismiss ✕ top-right.
  const head = el("div", STYLE.row);
  head.appendChild(el("span", null, "🛡️"));
  head.appendChild(el("strong", "font-size:13px;", "Veritas"));
  const spinner = el("span", null);
  spinner.className = "veritas-spinner";
  head.appendChild(spinner);
  const spacer = el("span", "flex:1;");
  head.appendChild(spacer);
  const closeBtn = el("span", STYLE.dismiss, "✕");
  closeBtn.setAttribute("role", "button");
  closeBtn.setAttribute("aria-label", "Dismiss news check");
  head.appendChild(closeBtn);
  card.appendChild(head);

  // Headline zone (max 120 chars, textContent).
  const title = el(
    "div",
    "font-size:14px;font-weight:700;padding:6px 12px 0;overflow-wrap:anywhere;",
    headline.slice(0, 120) + (headline.length > 120 ? "…" : "")
  );
  card.appendChild(title);

  // Preview zone (first 280 chars, textContent).
  if (preview) {
    card.appendChild(
      el(
        "div",
        "font-size:12.5px;color:#5f6b76;padding:4px 12px 0;overflow-wrap:anywhere;",
        preview.length >= 280 ? preview + "…" : preview
      )
    );
  }

  // Verdict slot with three shimmer bars.
  const slot = el("div", "padding:6px 12px 4px;");
  slot.id = "veritas-verdict-slot";
  for (let i = 0; i < 3; i++) {
    const bar = el("span");
    bar.className = "veritas-shimmer";
    if (i === 1) bar.style.width = "95%";
    if (i === 2) bar.style.width = "60%";
    slot.appendChild(bar);
  }
  card.appendChild(slot);

  // Footer.
  const foot = el("div", STYLE.footer);
  const footText = el("span", "font-size:11px;color:#9aa5b1;", "Checking trusted sources…");
  footText.id = "veritas-footer-text";
  foot.appendChild(footText);
  card.appendChild(foot);

  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    sessionDismissed = true;
    host.remove();
  });

  shadow.appendChild(card);
  document.documentElement.appendChild(host);
}

// ---------------------------------------------------------------------------
// PHASE 2 — verdict arrives: animate into the already-open card.
// ---------------------------------------------------------------------------

function applyVerdict(result) {
  // Validate the relayed response shape before rendering. Fail closed.
  if (!result || typeof result !== "object") return;
  const verdict = typeof result.verdict === "string" ? result.verdict : "";
  const suspicion = Number(result.suspicion);
  const confidence = Number(result.confidence);
  if (!Number.isFinite(suspicion) || !Number.isFinite(confidence)) return;

  const host = document.getElementById("veritas-banner-host");
  const shadow = host && host._veritasShadow;
  if (!shadow) return;
  const slot = shadow.getElementById("veritas-verdict-slot");
  if (!slot) return;

  const state = resolveState(verdict, result.ai_media === true);
  const meta = stateMeta(state);
  const reason = typeof result.reason === "string" ? result.reason.slice(0, 160) : "";
  const corroborating = Array.isArray(result.corroborating_sources)
    ? result.corroborating_sources.filter((m) => m && typeof m.url === "string" && /^https:\/\//i.test(m.url))
    : [];

  // Spinner -> static colored dot.
  const spinner = shadow.querySelector(".veritas-spinner");
  if (spinner) {
    const dot = el("span", STYLE.dot + `background:${meta.dot};`);
    spinner.replaceWith(dot);
  }

  // Rebuild the verdict slot: accent bar, label, explanation, reason,
  // source chips, suspicion meter.
  slot.textContent = "";
  slot.style.animation = "veritas-fade-in 250ms ease-out";

  const block = el("div", `border-left:3px solid ${meta.dot};padding-left:8px;margin:6px 0;`);
  block.appendChild(el("div", `font-weight:700;color:${meta.dot};font-size:13px;`, meta.label));
  block.appendChild(el("div", null, STATE_EXPLANATIONS[state]));
  if (reason && reason !== STATE_EXPLANATIONS[state]) {
    block.appendChild(el("div", "color:#5f6b76;font-size:12px;font-style:italic;", "\u201C" + reason + "\u201D"));
  }
  slot.appendChild(block);

  if (corroborating.length) {
    slot.appendChild(el("div", STYLE.section, "Corroborating outlets"));
    const chips = el("div");
    for (const m of corroborating) {
      const chip = el("span", STYLE.chip, (m.outlet || "source").slice(0, 40));
      chip.title = m.url; // validated https URL; shown in tooltip, not href interpolation
      chips.appendChild(chip);
    }
    slot.appendChild(chips);
  }

  const meter = el("div", STYLE.meter);
  const fill = el("div", STYLE.meterFill + `width:${Math.round(Math.min(Math.max(suspicion, 0), 1) * 100)}%;background:${suspicionColor(suspicion)};`);
  meter.appendChild(fill);
  slot.appendChild(meter);

  // Footer: sources checked + confidence stars.
  const footText = shadow.getElementById("veritas-footer-text");
  if (footText) {
    const stars = Math.max(0, Math.min(5, Math.round(confidence * 5)));
    const checked = Number.isFinite(Number(result.sources_checked))
      ? Math.max(0, Math.min(99, Number(result.sources_checked)))
      : 0;
    footText.textContent = `Sources checked: ${checked} · ${"★".repeat(stars)}${"☆".repeat(5 - stars)}`;
  }
}

// ---------------------------------------------------------------------------
// PHASE 2 (failure) — degrade honestly, never fake a verdict.
// ---------------------------------------------------------------------------

function applyError(msg) {
  const host = document.getElementById("veritas-banner-host");
  const shadow = host && host._veritasShadow;
  if (!shadow) return;
  const slot = shadow.getElementById("veritas-verdict-slot");
  if (!slot) return;

  const spinner = shadow.querySelector(".veritas-spinner");
  if (spinner) spinner.remove();

  slot.textContent = "";
  slot.style.animation = "veritas-fade-in 250ms ease-out";
  slot.appendChild(el("div", "font-weight:700;color:#92400e;font-size:13px;", "⚠️ Check unavailable"));
  slot.appendChild(el("div", "color:#5f6b76;font-size:12px;margin-top:2px;", "Is the backend running?"));

  const footText = shadow.getElementById("veritas-footer-text");
  if (footText) footText.textContent = "Analysis failed";
}

let sessionDismissed = false;

async function analyzeCurrentPage() {
  const cfg = await getBackendBase();
  if (!cfg.autoCheck) return;
  const headline = extractHeadline();
  const preview = extractArticleText().slice(0, 280);
  renderAnalyzing(headline, preview); // Phase 1: instant
  const payload = {
    url: location.href,
    region: cfg.region,
    headline,
    // DATA MINIMIZATION: article text only — never document.outerHTML,
    // which would upload passwords, tokens and private messages.
    text: extractArticleText(),
  };
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload }, (res) => {
    if (sessionDismissed) return;
    if (!res || res.error) {
      applyError(res && res.error);
      return;
    }
    applyVerdict(res); // Phase 2: when ready
  });
}

// Only auto-analyze pages that look like articles/news.
console.info("[Veritas] content script loaded on", location.hostname);
if (/news|article|story|\/\d{4}\/\d{2}\b/i.test(location.href) || document.querySelector("article")) {
  analyzeCurrentPage();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Only accept messages from our own extension context.
  if (sender.id !== chrome.runtime.id) return;
  if (msg && msg.type === "VERITAS_ANALYZE") {
    sessionDismissed = false; // explicit re-check overrides dismissal
    analyzeCurrentPage().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
});
