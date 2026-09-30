// Popup logic: show verdict for the active tab, allow manual re-check.

const statusEl = document.getElementById("status");

function render(result) {
  statusEl.innerHTML = `
    <div class="verdict ${result.verdict}">${labelFor(result.verdict)}</div>
    <div>Suspicion ${(result.suspicion * 100).toFixed(0)}% ·
         confidence ${(result.confidence * 100).toFixed(0)}%</div>
    <div style="margin-top:6px">${escapeHtml(result.summary || "")}</div>
  `;
}

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

async function analyzeActiveTab() {
  statusEl.textContent = "Checking this page…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    statusEl.textContent = "No active page to check.";
    return;
  }
  chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, (resp) => {
    if (chrome.runtime.lastError) {
      statusEl.textContent = "Cannot analyze this page (restricted or not loaded).";
      return;
    }
    // The content script renders the banner; pull the latest result for the popup.
    setTimeout(async () => {
      const cfg = await chrome.storage.sync.get({ backend: "http://localhost:8000", region: "in" });
      try {
        const res = await fetch(cfg.backend.replace(/\/$/, "") + "/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: tab.url, region: cfg.region, include_cross_check: true }),
        });
        render(await res.json());
      } catch (e) {
        statusEl.textContent = "Backend unreachable — is it running on " + cfg.backend + "?";
      }
    }, 400);
  });
}

document.getElementById("recheck").addEventListener("click", analyzeActiveTab);
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
analyzeActiveTab();
