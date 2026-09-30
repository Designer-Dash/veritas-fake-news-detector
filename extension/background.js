// Veritas background service worker (MV3).

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(
    { backend: "http://localhost:8000", region: "in", autoCheck: true },
    (defaults) => chrome.storage.sync.set(defaults)
  );
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
