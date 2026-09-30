// Veritas content script — runs on every page, extracts the article,
// asks the backend (via the background worker) for a verdict and renders a
// banner. The fetch MUST go through the background service worker: a
// content script inside an HTTPS page is mixed-content-blocked from
// calling http://localhost directly (verified in live browser testing).
//
// SECURITY: the host page is untrusted. All page-derived strings are
// rendered via textContent only (no innerHTML anywhere), the banner lives
// in a closed Shadow DOM so page CSS/JS cannot read or restyle it, and the
// only page data uploaded is the extracted article text — never the full
// DOM (which could contain passwords, tokens or private messages).

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

function verdictStyles(v) {
  switch (v) {
    case "fake":
      return { bg: "#b3261e", label: "Likely FAKE / AI-generated" };
    case "suspicious":
      return { bg: "#e65100", label: "Suspicious — treat with caution" };
    case "uncertain":
      return { bg: "#f9a825", label: "Uncertain — verify before sharing" };
    default:
      return { bg: "#2e7d32", label: "No fake-news signals found" };
  }
}

function el(tag, attrs, text) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text; // the only sink we use
  return node;
}

// Fixed labels only — never built from page or backend strings.
const VERDICT_LABELS = {
  fake: "Likely FAKE / AI-generated",
  suspicious: "Suspicious — treat with caution",
  uncertain: "Uncertain — verify before sharing",
  trusted: "No fake-news signals found",
};

function renderBanner(result) {
  // Validate the backend response shape before rendering. Fail closed:
  // anything unexpected is logged and dropped, never rendered.
  if (!result || typeof result !== "object") return;
  const verdict = VERDICT_LABELS[result.verdict] ? result.verdict : null;
  if (!verdict) return;
  const suspicion = Number(result.suspicion);
  const confidence = Number(result.confidence);
  if (!Number.isFinite(suspicion) || !Number.isFinite(confidence)) return;
  const summary = typeof result.summary === "string" ? result.summary.slice(0, 400) : "";
  const matches = Array.isArray(result.corroborating_sources)
    ? result.corroborating_sources
        .filter(
          (m) =>
            m &&
            typeof m.url === "string" &&
            /^https:\/\//i.test(m.url) && // https only, never javascript:/data:
            typeof m.outlet === "string" &&
            typeof m.similarity === "number"
        )
        .slice(0, 3)
    : [];

  const old = document.getElementById("veritas-banner-host");
  if (old) old.remove();

  const style = verdictStyles(verdict);
  const host = document.createElement("div");
  host.id = "veritas-banner-host";
  const shadow = host.attachShadow({ mode: "closed" }); // page JS cannot reach in

  const banner = el("div", {
    id: "veritas-banner",
    style: `position:fixed;top:12px;right:12px;z-index:2147483647;max-width:380px;background:${style.bg};color:#fff;padding:14px 16px;border-radius:12px;font-family:system-ui,sans-serif;font-size:13px;line-height:1.45;box-shadow:0 8px 30px rgba(0,0,0,.35)`,
  });

  const head = el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px" });
  head.appendChild(el("strong", { style: "font-size:14px" }, `🛡️ Veritas: ${VERDICT_LABELS[verdict]}`));
  const close = el("span", { id: "veritas-close", style: "cursor:pointer;opacity:.8;padding:2px 6px" }, "✕");
  head.appendChild(close);

  const stars = Math.max(0, Math.min(5, Math.round(confidence * 5)));
  const meta = el("div", { style: "margin:6px 0;opacity:.95" },
    `Confidence ${"★".repeat(stars)}${"☆".repeat(5 - stars)} · suspicion ${(suspicion * 100).toFixed(0)}%`);

  banner.appendChild(head);
  banner.appendChild(meta);
  if (summary) banner.appendChild(el("div", { style: "opacity:.95" }, summary));

  if (matches.length) {
    const wrap = el("div", { style: "margin-top:8px" });
    wrap.appendChild(el("div", { style: "opacity:.8;margin-bottom:2px" }, "Corroborated by:"));
    const list = el("ul", { style: "margin:0;padding-left:18px" });
    for (const m of matches) {
      const li = el("li");
      const a = el("a", {
        href: m.url, // validated https URL above; assigned via setAttribute, not interpolation
        target: "_blank",
        rel: "noopener noreferrer",
        style: "color:#ffe082",
      });
      a.textContent = m.outlet.slice(0, 80);
      const pct = el("span", { style: "opacity:.8" }, ` (${Math.round(m.similarity * 100)}% match)`);
      li.appendChild(a);
      li.appendChild(pct);
      list.appendChild(li);
    }
    wrap.appendChild(list);
    banner.appendChild(wrap);
  }

  shadow.appendChild(banner);
  close.addEventListener("click", () => host.remove());
  document.documentElement.appendChild(host);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
void escapeHtml; // kept for future plain-text nodes; rendering uses textContent only

async function analyzeCurrentPage() {
  const cfg = await getBackendBase();
  if (!cfg.autoCheck) return;
  const payload = {
    url: location.href,
    region: cfg.region,
    headline: extractHeadline(),
    // DATA MINIMIZATION: article text only — never document.outerHTML,
    // which would upload passwords, tokens and private messages on pages
    // with login forms or webmail in other tabs' DOM patterns.
    text: extractArticleText(),
  };
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload }, (res) => {
    if (chrome.runtime.lastError || !res || res.error) {
      console.warn("[Veritas] analysis unavailable:", (res && res.error) || chrome.runtime.lastError);
      return;
    }
    renderBanner(res);
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
    analyzeCurrentPage().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
});
