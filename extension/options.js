const els = {
  region: document.getElementById("region"),
  backend: document.getElementById("backend"),
  apiKey: document.getElementById("apiKey"),
  autoCheck: document.getElementById("autoCheck"),
  save: document.getElementById("save"),
  saved: document.getElementById("saved"),
};

// SECURITY: the API key is a credential — it lives in storage.local (never
// synced to Google accounts, unlike storage.sync) and is only sent to the
// configured backend as a Bearer header.
chrome.storage.local.get({ apiKey: "" }, (local) => {
  els.apiKey.value = local.apiKey;
});
chrome.storage.sync.get(
  { backend: "http://localhost:8000", region: "in", autoCheck: true },
  (cfg) => {
    els.region.value = cfg.region;
    els.backend.value = cfg.backend;
    els.autoCheck.checked = cfg.autoCheck;
  }
);

els.save.addEventListener("click", () => {
  // Validate before persisting: only http(s) URLs are ever allowed as the
  // backend (the background worker re-validates on every use — this is UX).
  let backend = els.backend.value.trim().replace(/\/+$/, "");
  try {
    const parsed = new URL(backend);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error();
  } catch {
    els.saved.textContent = "Invalid backend URL (must start with http:// or https://)";
    return;
  }
  const region = ["in", "us", "uk", "global"].includes(els.region.value) ? els.region.value : "in";
  chrome.storage.sync.set(
    {
      region,
      backend,
      autoCheck: !!els.autoCheck.checked,
    },
    () => {
      const apiKey = els.apiKey.value.trim();
      // Basic shape check before storing: our keys start with vfnd_ and are
      // short; anything else is refused rather than silently persisted.
      if (apiKey && !/^vfnd_[A-Za-z0-9_-]{20,80}$/.test(apiKey)) {
        els.saved.textContent = "API key format looks wrong (expected vfnd_…)";
        return;
      }
      chrome.storage.local.set({ apiKey }, () => {
        els.saved.textContent = "Saved ✓";
        setTimeout(() => (els.saved.textContent = ""), 1500);
      });
    }
  );
});
