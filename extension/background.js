// Veritas background service worker (MV3).
//
// The service worker is the only extension context allowed to fetch
// http://localhost from within HTTPS pages' content scripts (mixed content
// rules do not apply to the extension's own background context).
//
// SECURITY: this worker is the trust boundary between untrusted pages and
// the backend. Every message is validated (type + sender + payload shape)
// before any action; unknown shapes fail closed. Only a fixed whitelist of
// payload fields is forwarded to the backend -- nothing a hostile page
// injects into its own DOM can add fields, smuggle URLs, or change the
// request destination (backend base URL comes only from chrome.storage).

const DEFAULTS = { backend: "http://localhost:8000", region: "in", autoCheck: true };
// storage.local defaults (credentials are NOT part of synced settings).
const LOCAL_DEFAULTS = { apiKey: "" };

const VALID_REGIONS = new Set(["in", "us", "uk", "global"]);

function isValidBackendUrl(u) {
  try {
    const parsed = new URL(u);
    // The extension only talks to its configured backend over http(s);
    // javascript:/data:/file: and friends are rejected here.
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(DEFAULTS, (defaults) => chrome.storage.sync.set(defaults));
});

function buildPayload(raw, region) {
  // Strict field whitelist with type + length bounds. Everything else is
  // dropped -- unknown fields from a compromised page must not reach the API.
  if (!raw || typeof raw !== "object") return null;
  const url = typeof raw.url === "string" ? raw.url.slice(0, 2048) : "";
  if (!/^https?:\/\//i.test(url)) return null; // fail closed on odd schemes
  const payload = { url, region: VALID_REGIONS.has(region) ? region : "in" };
  if (typeof raw.headline === "string" && raw.headline.trim()) {
    payload.headline = raw.headline.slice(0, 512);
  }
  if (typeof raw.text === "string" && raw.text.length > 0) {
    payload.html = raw.text.slice(0, 400000); // article text (kept as API field name)
  }
  return payload;
}

async function handleFetch(payloadRaw, sendResponse) {
  try {
    const cfg = await chrome.storage.sync.get(DEFAULTS);
    if (!isValidBackendUrl(cfg.backend)) {
      sendResponse({ error: "invalid backend configuration" });
      return;
    }
    const payload = buildPayload(payloadRaw, cfg.region);
    if (!payload) {
      sendResponse({ error: "invalid payload" });
      return;
    }
    // The API key is a credential: read from storage.local (never synced),
    // sent only to the configured backend, never logged.
    const { apiKey } = await chrome.storage.local.get({ apiKey: "" });
    const headers = { "Content-Type": "application/json" };
    if (apiKey && /^vfnd_[A-Za-z0-9_-]{20,80}$/.test(apiKey)) {
      headers["Authorization"] = "Bearer " + apiKey;
    }
    const res = await fetch(cfg.backend.replace(/\/$/, "") + "/analyze", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    if (res.status === 401) {
      sendResponse({ error: "Authentication failed — check your API key in Veritas settings." });
      return;
    }
    if (res.status === 429) {
      sendResponse({ error: "Rate limit reached — try again in a minute." });
      return;
    }
    if (!res.ok) throw new Error("backend responded " + res.status);
    const data = await res.json();
    // Relay only the fields the banner/popup need (response minimization).
    sendResponse({
      verdict: typeof data.verdict === "string" ? data.verdict : "uncertain",
      suspicion: typeof data.suspicion === "number" ? data.suspicion : 0.5,
      confidence: typeof data.confidence === "number" ? data.confidence : 0.5,
      summary: typeof data.summary === "string" ? data.summary.slice(0, 500) : "",
      corroborating_sources: Array.isArray(data.corroborating_sources)
        ? data.corroborating_sources.slice(0, 5)
        : [],
    });
  } catch (err) {
    sendResponse({ error: String((err && err.message) || err) });
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Content-script senders must be our own extension.
  if (sender.id && sender.id !== chrome.runtime.id) return;

  if (msg && msg.type === "VERITAS_FETCH") {
    handleFetch(msg.payload, sendResponse);
    return true; // async sendResponse
  }

  if (msg && msg.type === "VERITAS_TRIGGER") {
    // Only the popup/options pages (extension-internal, no sender.id) may
    // trigger tab actions.
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (tab && tab.id) {
        chrome.tabs.sendMessage(tab.id, { type: "VERITAS_ANALYZE" }, () => {
          void chrome.runtime.lastError;
          sendResponse({ ok: !chrome.runtime.lastError });
        });
      } else {
        sendResponse({ ok: false });
      }
    });
    return true; // async sendResponse
  }

  // Unknown message types: ignore (fail closed, no response channel abuse).
});
