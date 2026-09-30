// Popup logic: show verdict for the active tab via the background relay
// (one fetch path for the whole extension), allow manual re-check.

const statusEl = document.getElementById("status");

function labelFor(v) {
  return {
    fake: "❌ Likely FAKE / AI-generated",
    suspicious: "⚠️ Suspicious",
    uncertain: "❓ Uncertain",
    trusted: "✅ No fake-news signals found",
  }[v] || v;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function render(result) {
  const stars = Math.round((result.confidence || 0) * 5);
  statusEl.innerHTML = `
    <div class="verdict ${result.verdict}">${labelFor(result.verdict)}</div>
    <div>Suspicion ${(result.suspicion * 100).toFixed(0)}% ·
         confidence ${"★".repeat(stars)}${"☆".repeat(5 - stars)}</div>
    <div style="margin-top:6px">${escapeHtml(result.summary || "")}</div>
    ${(result.corroborating_sources || []).length
      ? `<div style="margin-top:6px">Corroborated by: ${result.corroborating_sources
          .slice(0, 3)
          .map((m) => escapeHtml(m.outlet))
          .join(", ")}</div>`
      : ""}
  `;
}

async function analyzeActiveTab(triggerBanner) {
  statusEl.textContent = "Checking this page…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) {
    statusEl.textContent = "No active page to check.";
    return;
  }
  // Trigger the on-page banner too (only when the content script is present).
  if (triggerBanner && tab.id) {
    chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, () => void chrome.runtime.lastError);
  }
  // Verdict for the popup itself goes through the background worker.
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
