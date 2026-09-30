// Veritas background service worker (MV3).
//
// The service worker is the only extension context allowed to fetch
// http://localhost from within HTTPS pages' content scripts (mixed content
// rules do not apply to the extension's own background context).

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(
    { backend: "http://localhost:8000", region: "in", autoCheck: true },
    (defaults) => chrome.storage.sync.set(defaults)
  );
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Content script -> backend relay (mixed-content-safe path).
  if (msg.type === "VERITAS_FETCH") {
    (async () => {
      try {
        const cfg = await chrome.storage.sync.get({
          backend: "http://localhost:8000",
          region: "in",
        });
        const payload = { ...(msg.payload || {}), region: cfg.region };
        const res = await fetch(cfg.backend.replace(/\/$/, "") + "/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error("backend responded " + res.status);
        sendResponse(await res.json());
      } catch (err) {
        sendResponse({ error: String((err && err.message) || err) });
      }
    })();
    return true; // async sendResponse
  }

  // Popup -> active tab trigger.
  if (msg.type === "VERITAS_TRIGGER") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (tab && tab.id) {
        chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, () => {
          sendResponse({ ok: !chrome.runtime.lastError });
        });
      } else {
        sendResponse({ ok: false });
      }
    });
    return true; // async sendResponse
  }
});
