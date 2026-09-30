// Popup logic: show verdict for the active tab via the background relay
// (one fetch path for the whole extension), allow manual re-check.
//
// SECURITY: backend responses are untrusted data, not markup — rendered via
// textContent only; no innerHTML anywhere in this file.

const statusEl = document.getElementById("status");

const VERDICT_LABELS = {
  fake: "🔴 Contradicted",
  suspicious: "🟠 Misleading",
  uncertain: "🟡 Unverified",
  trusted: "🟢 Supported",
};

function el(tag, style, text) {
  const node = document.createElement(tag);
  if (style) node.setAttribute("style", style);
  if (text !== undefined) node.textContent = text;
  return node;
}

function render(result) {
  // Shape-validate before rendering; drop anything unexpected.
  if (!result || !VERDICT_LABELS[result.verdict]) return;
  const suspicion = Number(result.suspicion);
  const confidence = Number(result.confidence);
  if (!Number.isFinite(suspicion) || !Number.isFinite(confidence)) return;

  statusEl.textContent = "";
  const stars = Math.max(0, Math.min(5, Math.round(confidence * 5)));

  statusEl.appendChild(
    el("div", "font-weight:700", VERDICT_LABELS[result.verdict])
  );
  statusEl.appendChild(
    el(
      "div",
      "",
      `Suspicion ${(suspicion * 100).toFixed(0)}% · confidence ${"★".repeat(stars)}${"☆".repeat(5 - stars)}`
    )
  );
  if (typeof result.summary === "string" && result.summary) {
    statusEl.appendChild(el("div", "margin-top:6px", result.summary.slice(0, 400)));
  }
  const sources = Array.isArray(result.corroborating_sources)
    ? result.corroborating_sources
        .filter((m) => m && typeof m.outlet === "string" && typeof m.url === "string" && /^https:\/\//i.test(m.url))
        .slice(0, 3)
    : [];
  if (sources.length) {
    statusEl.appendChild(
      el(
        "div",
        "margin-top:6px",
        "Corroborated by: " + sources.map((m) => m.outlet.slice(0, 80)).join(", ")
      )
    );
  }
}

async function analyzeActiveTab(triggerBanner) {
  statusEl.textContent = "Checking this page…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) {
    statusEl.textContent = "No active page to check.";
    return;
  }
  if (triggerBanner && tab.id) {
    chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, () => void chrome.runtime.lastError);
  }
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload: { url: tab.url } }, (res) => {
    if (chrome.runtime.lastError || !res || res.error) {
      statusEl.textContent =
        "Backend unreachable — is it running? " + ((res && res.error) || "");
      return;
    }
    render(res);
  });
}

document.getElementById("recheck").addEventListener("click", () => analyzeActiveTab(true));
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
analyzeActiveTab(false);
