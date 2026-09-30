const els = {
  region: document.getElementById("region"),
  backend: document.getElementById("backend"),
  autoCheck: document.getElementById("autoCheck"),
  save: document.getElementById("save"),
  saved: document.getElementById("saved"),
};

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
      els.saved.textContent = "Saved ✓";
      setTimeout(() => (els.saved.textContent = ""), 1500);
    }
  );
});
