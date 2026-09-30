// Popup logic — dark premium redesign. All backend-derived text is rendered
// via textContent only; no innerHTML anywhere in this file.

const badgeEl = document.getElementById("badge");
const meterFill = document.getElementById("meterFill");
const summaryEl = document.getElementById("summary");
const chipsEl = document.getElementById("chips");
const recheckBtn = document.getElementById("recheck");

// Fixed verdict metadata: badge copy + colors (never built from data).
const VERDICT_META = {
  trusted: { bg: "#22c55e", fg: "#052e16", text: "✓ Supported by trusted sources", meter: "#22c55e" },
  uncertain: { bg: "#eab308", fg: "#1c1917", text: "? Unverified — no corroboration found", meter: "#eab308" },
  suspicious: { bg: "#f97316", fg: "#1c1917", text: "⚠ Misleading signals detected", meter: "#f97316" },
  fake: { bg: "#ef4444", fg: "#ffffff", text: "✕ Contradicted by trusted outlets", meter: "#ef4444" },
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showShimmer() {
  summaryEl.textContent = "";
  chipsEl.style.display = "none";
  chipsEl.textContent = "";
  meterFill.style.width = "0%";
  badgeEl.textContent = "";
  badgeEl.style.background = "#1e293b";
  badgeEl.style.color = "#e2e8f0";
  badgeEl.appendChild(el("div", "shimmer"));
  badgeEl.appendChild(el("div", "shimmer"));
  const third = el("div", "shimmer");
  third.style.width = "60%";
  badgeEl.appendChild(third);
}

function render(result) {
  // Shape-validate before rendering; drop anything unexpected (fail closed).
  if (!result || !VERDICT_META[result.verdict]) {
    applyError("unexpected response");
    return;
  }
  const suspicion = Number(result.suspicion);
  const confidence = Number(result.confidence);
  if (!Number.isFinite(suspicion) || !Number.isFinite(confidence)) {
    applyError("unexpected response");
    return;
  }
  const meta = VERDICT_META[result.verdict];

  badgeEl.textContent = meta.text;
  badgeEl.style.background = meta.bg;
  badgeEl.style.color = meta.fg;

  meterFill.style.width = `${Math.round(Math.min(Math.max(suspicion, 0), 1) * 100)}%`;
  meterFill.style.background = meta.meter;

  summaryEl.textContent = typeof result.summary === "string" ? result.summary.slice(0, 400) : "";

  chipsEl.textContent = "";
  const sources = Array.isArray(result.corroborating_sources)
    ? result.corroborating_sources.filter(
        (m) => m && typeof m.outlet === "string" && typeof m.url === "string" && /^https:\/\//i.test(m.url)
      )
    : [];
  if (sources.length) {
    chipsEl.style.display = "flex";
    for (const m of sources.slice(0, 4)) {
      chipsEl.appendChild(el("span", "chip", m.outlet.slice(0, 60)));
    }
  }
}

function applyError(msg) {
  badgeEl.textContent = "⚠ Backend unreachable";
  badgeEl.style.background = "#78350f";
  badgeEl.style.color = "#fde68a";
  meterFill.style.width = "0%";
  summaryEl.textContent =
    msg && /auth/i.test(msg)
      ? String(msg).slice(0, 200)
      : "Start the backend server, then re-check. " + (msg ? `(${String(msg).slice(0, 120)})` : "");
  chipsEl.style.display = "none";
}

async function analyzeActiveTab(triggerBanner) {
  showShimmer();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) {
    applyError("no active page");
    return;
  }
  if (triggerBanner && tab.id) {
    chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, () => void chrome.runtime.lastError);
  }
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload: { url: tab.url } }, (res) => {
    if (chrome.runtime.lastError || !res || res.error) {
      applyError((res && res.error) || chrome.runtime.lastError);
      return;
    }
    render(res);
  });
}

recheckBtn.addEventListener("click", () => analyzeActiveTab(true));
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
analyzeActiveTab(false);
