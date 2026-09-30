// Veritas content script — runs on every page, extracts the article,
// asks the backend (via the background worker) for a verdict and renders a
// compact, non-intrusive verification badge. The fetch MUST go through the
// background service worker: a content script inside an HTTPS page is
// mixed-content-blocked from calling http://localhost directly (verified in
// live browser testing).
//
// SECURITY: the host page is untrusted. All page/backend-derived strings are
// rendered via textContent only (no innerHTML anywhere), the badge lives in
// a closed Shadow DOM so page CSS/JS cannot read or restyle it, the only
// page data uploaded is the extracted article text — never the full DOM —
// and any URL rendered as a link must pass an https-only check.
//
// UX CONTRACT (see README): collapsed pill by default; one click expands a
// small evidence card; outside click / Esc / × closes; dismissal persists
// for the current page session only; the article stays the primary focus.

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

// Map backend verdicts to the five user-facing states. `ai_media` promotes
// the badge to the distinct blue AI state.
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

// One-line, evidence-anchored explanation per state (fixed copy — no
// backend string is interpolated into these sentences; the dynamic reason
// is rendered separately as its own text node).
const STATE_EXPLANATIONS = {
  supported: "Trusted news outlets corroborate this story.",
  unverified: "No matching coverage was found in trusted outlets yet.",
  suspicious: "This conflicts with how trusted outlets report the story.",
  fake: "This claim conflicts with information from relevant trusted sources.",
  ai: "Media on this page shows signs of AI generation.",
};

const STYLE = {
  host:
    // The host anchors the widget; pointer-events:none guarantees the badge
    // can never block page interactions outside its own visible box.
    "all:initial;position:fixed;top:12px;right:12px;z-index:2147483646;" +
    "display:block;pointer-events:none;",
  pill:
    "all:initial;display:inline-flex;align-items:center;gap:6px;padding:5px 11px 5px 9px;" +
    "background:rgba(255,255,255,.97);border:1px solid rgba(0,0,0,.14);" +
    "border-radius:999px;box-shadow:0 1px 4px rgba(0,0,0,.18);" +
    "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
    "font-size:12.5px;font-weight:600;color:#1f2933;cursor:pointer;" +
    "user-select:none;-webkit-user-select:none;line-height:1.2;max-width:72vw;" +
    "white-space:nowrap;overflow:hidden;pointer-events:auto;",
  card:
    "all:initial;display:block;width:288px;max-width:calc(100vw - 24px);" +
    "background:#ffffff;border:1px solid rgba(0,0,0,.14);border-radius:12px;" +
    "box-shadow:0 4px 16px rgba(0,0,0,.16);" +
    "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
    "font-size:13px;color:#1f2933;line-height:1.45;overflow:hidden;pointer-events:auto;",
  row: "display:flex;align-items:center;gap:8px;padding:10px 12px 2px;",
  body: "padding:2px 12px 10px;",
  footer:
    "display:flex;align-items:center;justify-content:space-between;" +
    "padding:8px 12px 10px;border-top:1px solid rgba(0,0,0,.08);",
  btn:
    "all:initial;font-family:inherit;font-size:12px;font-weight:600;" +
    "color:#1a237e;cursor:pointer;padding:2px 4px;border-radius:6px;",
  dismiss: "font-size:14px;color:#5f6b76;cursor:pointer;padding:2px 6px;",
  dot: "width:8px;height:8px;border-radius:50%;flex:none;",
  section:
    "font-size:11px;font-weight:600;letter-spacing:.04em;color:#5f6b76;" +
    "text-transform:uppercase;margin:8px 0 3px;",
  link: "color:#1a237e;font-weight:600;text-decoration:none;cursor:pointer;",
};

function el(tag, style, text) {
  const node = document.createElement(tag);
  if (style) node.setAttribute("style", style);
  if (text !== undefined) node.textContent = text;
  return node;
}

function pillLabel(state) {
  return "News Check: " + stateMeta(state).label;
}

// ---------------------------------------------------------------------------
// Rendering. Closed shadow root: the page can neither read nor restyle it.
// ---------------------------------------------------------------------------

let sessionDismissed = false;

function renderBadge(result) {
  // Validate the relayed response shape before rendering. Fail closed:
  // anything unexpected is dropped, never rendered.
  if (!result || typeof result !== "object") return;
  const verdict = typeof result.verdict === "string" ? result.verdict : "";
  const suspicion = Number(result.suspicion);
  const confidence = Number(result.confidence);
  if (!Number.isFinite(suspicion) || !Number.isFinite(confidence)) return;

  const state = resolveState(verdict, result.ai_media === true);
  const meta = stateMeta(state);
  const reason = typeof result.reason === "string" ? result.reason.slice(0, 160) : "";
  const sourcesChecked = Number.isFinite(Number(result.sources_checked))
    ? Math.max(0, Math.min(99, Number(result.sources_checked)))
    : 0;
  const corroborating = Array.isArray(result.corroborating)
    ? result.corroborating.filter((m) => m && typeof m.url === "string" && /^https:\/\//i.test(m.url))
    : [];

  const host = document.createElement("div");
  host.id = "veritas-banner-host";
  host.setAttribute("style", STYLE.host);
  const shadow = host.attachShadow({ mode: "closed" }); // page JS cannot reach in

  // --- expanded card (built first; hidden until expanded) -----------------
  const card = el("div", STYLE.card);
  card.style.display = "none";

  const head = el("div", STYLE.row);
  head.appendChild(el("span", null, "🛡️"));
  head.appendChild(el("strong", null, "News Check"));
  card.appendChild(head);

  const stateRow = el("div", STYLE.row);
  stateRow.appendChild(el("span", STYLE.dot + `background:${meta.dot};`));
  stateRow.appendChild(el("strong", `font-size:14px;color:${meta.dot};`, meta.label));
  card.appendChild(stateRow);

  const body = el("div", STYLE.body);
  body.appendChild(el("div", `margin-top:6px;`, STATE_EXPLANATIONS[state]));
  if (reason && reason !== STATE_EXPLANATIONS[state]) {
    const q = el("div", `margin-top:6px;color:#5f6b76;font-size:12.5px;`);
    q.textContent = "\u201C" + reason + "\u201D"; // textContent, quoted as evidence
    body.appendChild(q);
  }

  if (corroborating.length) {
    body.appendChild(el("div", STYLE.section, "Corroborating outlets"));
    const list = el("div");
    for (const m of corroborating) {
      const a = el("a", STYLE.link);
      a.setAttribute("href", m.url); // https-only validated above
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
      a.textContent = (m.outlet || "source") .slice(0, 60);
      list.appendChild(a);
      list.appendChild(el("span", null, "  "));
    }
    body.appendChild(list);
  }
  card.appendChild(body);

  const foot = el("div", STYLE.footer);
  foot.appendChild(el("span", `font-size:11.5px;color:#5f6b76;`, `Sources checked: ${sourcesChecked}`));
  const closeBtn = el("span", STYLE.dismiss, "×");
  closeBtn.setAttribute("role", "button");
  closeBtn.setAttribute("aria-label", "Dismiss news check");
  foot.appendChild(closeBtn);
  card.appendChild(foot);

  // --- collapsed pill ------------------------------------------------------
  const pill = el("div", STYLE.pill);
  pill.setAttribute("role", "button");
  pill.setAttribute("aria-label", pillLabel(state) + " — show details");
  pill.appendChild(el("span", null, "🛡️"));
  pill.appendChild(el("span", STYLE.dot + `background:${meta.dot};`));
  pill.appendChild(
    el("span", null, "News Check: ")
  );
  pill.appendChild(el("span", `font-weight:700;`, meta.label));

  // Subtle entrance: 160ms fade + 6px slide, respects reduced-motion.
  const styleEl = document.createElement("style");
  styleEl.textContent =
    "@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}" +
    "@keyframes veritas-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}" +
    "@keyframes veritas-card-in{from{opacity:0;transform:translateY(-4px) scale(.98)}to{opacity:1;transform:none}}";
  shadow.appendChild(styleEl);

  let expanded = false;
  function setExpanded(v) {
    expanded = v;
    card.style.display = v ? "block" : "none";
    if (v) {
      card.style.animation = "veritas-card-in 160ms ease-out";
      pill.style.display = "none";
    } else {
      pill.style.display = "inline-flex";
    }
  }

  function dismiss() {
    sessionDismissed = true;
    host.remove();
  }

  pill.addEventListener("click", (e) => {
    e.stopPropagation();
    setExpanded(true);
  });
  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    dismiss();
  });

  shadow.appendChild(card);
  shadow.appendChild(pill);

  // Clicking outside closes the expanded card (back to pill, not dismiss).
  document.addEventListener(
    "click",
    (e) => {
      if (!expanded) return;
      if (e.composedPath && e.composedPath().includes(host)) return;
      setExpanded(false);
    },
    true
  );
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Escape" && expanded) setExpanded(false);
    },
    true
  );

  document.documentElement.appendChild(host);
  pill.style.animation = "veritas-in 160ms ease-out";
}

async function analyzeCurrentPage() {
  const cfg = await getBackendBase();
  if (!cfg.autoCheck) return;
  const payload = {
    url: location.href,
    region: cfg.region,
    headline: extractHeadline(),
    // DATA MINIMIZATION: article text only — never document.outerHTML,
    // which would upload passwords, tokens and private messages.
    text: extractArticleText(),
  };
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload }, (res) => {
    if (chrome.runtime.lastError || !res || res.error) {
      console.warn("[Veritas] analysis unavailable:", (res && res.error) || chrome.runtime.lastError);
      return;
    }
    if (sessionDismissed) return; // stay dismissed for this page session
    renderBadge(res);
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
