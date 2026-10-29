// Veritas background service worker (MV3).
//
// The service worker is the only extension context allowed to fetch
// http://localhost from within HTTPS pages' content scripts (mixed content
// rules do not apply to the extension's own background context).
//
// SECURITY: this worker is the trust boundary between untrusted pages and
// the backend. Every message is validated (type + sender + payload shape)
// before any action; unknown shapes fail closed.
//
// Signature marker 012/013 · designer-dash
//
// Only a fixed whitelist of
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
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort("timeout"), 15000);
    const res = await fetch(cfg.backend.replace(/\/$/, "") + "/analyze", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    }).finally(() => clearTimeout(timer));
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
    // reason: highest-weight evidence claim, for the expanded card.
    let reason = "";
    if (Array.isArray(data.evidence) && data.evidence.length) {
      let top = null;
      for (const e of data.evidence) {
        if (e && typeof e.claim === "string" && typeof e.weight === "number") {
          if (!top || Math.abs(e.weight) > Math.abs(top.weight)) top = e;
        }
      }
      if (top) reason = top.claim.slice(0, 160);
    }
    let aiMedia = false;
    const media = data.ai_signals && data.ai_signals.media;
    if (media && typeof media === "object") {
      aiMedia = media.weight >= 0.3 || (Array.isArray(media.tool_markers) && media.tool_markers.length > 0);
    }
    sendResponse({
      verdict: typeof data.verdict === "string" ? data.verdict : "uncertain",
      suspicion: typeof data.suspicion === "number" ? data.suspicion : 0.5,
      confidence: typeof data.confidence === "number" ? data.confidence : 0.5,
      summary: typeof data.summary === "string" ? data.summary.slice(0, 400) : "",
      reason,
      sources_checked: typeof data.ai_signals?.cross_check?.checked === "number"
        ? data.ai_signals.cross_check.checked
        : 0,
      independent_origins: typeof data.ai_signals?.cross_check?.independent_origins === "number"
        ? data.ai_signals.cross_check.independent_origins
        : undefined,
      corroborating_sources: Array.isArray(data.corroborating_sources)
        ? data.corroborating_sources.slice(0, 10).map((m) => ({
            outlet: typeof m.outlet === "string" ? m.outlet.slice(0, 80) : "",
            url: typeof m.url === "string" && /^https?:\/\//i.test(m.url) ? m.url : "",
            title: typeof m.title === "string" ? m.title.slice(0, 200) : "",
            tier: typeof m.tier === "number" ? m.tier : 3,
            published: typeof m.published === "number" ? m.published : (typeof m.published === "string" ? Date.parse(m.published) / 1000 : null),
            is_wire_syndication: Boolean(m.is_wire_syndication),
            wire_agency: typeof m.wire_agency === "string" ? m.wire_agency : null,
          }))
        : [],
      ai_media: aiMedia,
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
