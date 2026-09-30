// Veritas content script — runs on every page, extracts the article,
// asks the backend for a verdict and renders a banner.

const DEFAULT_BACKEND = "http://localhost:8000";

function getBackendBase() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ backend: DEFAULT_BACKEND, region: "in", autoCheck: true }, (cfg) => {
      resolve(cfg);
    });
  });
}

function extractHeadline() {
  const og = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
  if (og && og.content) return og.content.trim();
  const h1 = document.querySelector("h1");
  return h1 ? h1.textContent.trim().slice(0, 300) : document.title.slice(0, 300);
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

function renderBanner(result) {
  const old = document.getElementById("veritas-banner");
  if (old) old.remove();

  const s = verdictStyles(result.verdict);
  const banner = document.createElement("div");
  banner.id = "veritas-banner";
  banner.style.cssText = `position:fixed;top:12px;right:12px;z-index:2147483647;
    max-width:380px;background:${s.bg};color:#fff;padding:14px 16px;border-radius:12px;
    font-family:system-ui,sans-serif;font-size:13px;line-height:1.45;
    box-shadow:0 8px 30px rgba(0,0,0,.35);cursor:default`;

  const stars = Math.round((result.confidence || 0) * 5);
  const sources = (result.corroborating_sources || [])
    .slice(0, 3)
    .map((m) => `<li><a href="${m.url}" target="_blank" rel="noopener"
        style="color:#ffe082">${escapeHtml(m.outlet)}</a>
        <span style="opacity:.8">(${Math.round(m.similarity * 100)}% match)</span></li>`)
    .join("");

  banner.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <strong style="font-size:14px">🛡️ Veritas: ${s.label}</strong>
      <span id="veritas-close" style="cursor:pointer;opacity:.8;padding:2px 6px">✕</span>
    </div>
    <div style="margin:6px 0;opacity:.95">
      Confidence ${"★".repeat(stars)}${"☆".repeat(5 - stars)}
      · suspicion ${(result.suspicion * 100).toFixed(0)}%
    </div>
    <div style="opacity:.95">${escapeHtml(result.summary || "")}</div>
    ${sources ? `<div style="margin-top:8px"><div style="opacity:.8;margin-bottom:2px">Corroborated by:</div><ul style="margin:0;padding-left:18px">${sources}</ul></div>` : ""}
  `;

  document.documentElement.appendChild(banner);
  banner.querySelector("#veritas-close").addEventListener("click", () => banner.remove());
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function analyzeCurrentPage() {
  const cfg = await getBackendBase();
  if (!cfg.autoCheck) return;
  const payload = {
    url: location.href,
    region: cfg.region,
    headline: extractHeadline(),
    html: document.documentElement.outerHTML.slice(0, 900000),
  };
  try {
    const res = await fetch(cfg.backend.replace(/\/$/, "") + "/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("backend " + res.status);
    renderBanner(await res.json());
  } catch (err) {
    console.warn("[Veritas] analysis unavailable:", err.message);
  }
}

// Only auto-analyze pages that look like articles/news.
if (/news|article|story|\/\d{4}\/\d{2}\b/i.test(location.href) || document.querySelector("article")) {
  analyzeCurrentPage();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "VERITAS_ANALYZE") {
    analyzeCurrentPage().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
});
