// Options page logic — connection status ping, validated save with
// "✓ Saved" animation. The API key is a credential: storage.local only
// (never synced), sent only to the configured backend.

const els = {
  region: document.getElementById("region"),
  backend: document.getElementById("backend"),
  apiKey: document.getElementById("apiKey"),
  autoCheck: document.getElementById("autoCheck"),
  save: document.getElementById("save"),
  conn: document.getElementById("conn"),
  connText: document.getElementById("connText"),
};

const connDot = els.conn.querySelector(".dot");

function setConn(state) {
  if (state === "ok") {
    connDot.style.background = "#22c55e";
    els.connText.textContent = "Connected";
    els.connText.style.color = "#15803d";
  } else if (state === "fail") {
    connDot.style.background = "#ef4444";
    els.connText.textContent = "Offline";
    els.connText.style.color = "#b91c1c";
  } else {
    connDot.style.background = "#94a3b8";
    els.connText.textContent = "Checking…";
    els.connText.style.color = "#64748b";
  }
}

async function pingBackend(url) {
  setConn("pending");
  try {
    const res = await fetch(url.replace(/\/$/, "") + "/health", { method: "GET" });
    setConn(res.ok ? "ok" : "fail");
  } catch {
    setConn("fail");
  }
}

// SECURITY: the API key lives in storage.local (never synced to Google
// accounts, unlike storage.sync) and is only sent to the configured backend.
chrome.storage.local.get({ apiKey: "" }, (local) => {
  els.apiKey.value = local.apiKey;
});
chrome.storage.sync.get(
  { backend: "http://localhost:8000", region: "in", autoCheck: true },
  (cfg) => {
    els.region.value = cfg.region;
    els.backend.value = cfg.backend;
    els.autoCheck.checked = cfg.autoCheck;
    pingBackend(cfg.backend);
  }
);

els.backend.addEventListener("change", () => {
  const v = els.backend.value.trim();
  if (/^https?:\/\//i.test(v)) pingBackend(v);
});

els.save.addEventListener("click", () => {
  // Validate before persisting: only http(s) URLs are ever allowed as the
  // backend (the background worker re-validates on every use — this is UX).
  let backend = els.backend.value.trim().replace(/\/+$/, "");
  try {
    const parsed = new URL(backend);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error();
  } catch {
    els.save.textContent = "✕ Invalid URL (must start with http:// or https://)";
    setTimeout(() => (els.save.textContent = "Save settings"), 1800);
    return;
  }
  const region = ["in", "us", "uk", "global"].includes(els.region.value) ? els.region.value : "in";
  const apiKey = els.apiKey.value.trim();
  // Basic shape check before storing: our keys start with vfnd_ and are
  // short; anything else is refused rather than silently persisted.
  if (apiKey && !/^vfnd_[A-Za-z0-9_-]{20,80}$/.test(apiKey)) {
    els.save.textContent = "✕ API key format looks wrong (expected vfnd_…)";
    setTimeout(() => (els.save.textContent = "Save settings"), 1800);
    return;
  }
  chrome.storage.sync.set({ region, backend, autoCheck: !!els.autoCheck.checked }, () => {
    chrome.storage.local.set({ apiKey }, () => {
      els.save.textContent = "✓ Saved";
      setTimeout(() => (els.save.textContent = "Save settings"), 1500);
    });
  });
});
