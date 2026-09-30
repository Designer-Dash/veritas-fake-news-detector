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
  chrome.storage.sync.set(
    {
      region: els.region.value,
      backend: els.backend.value.replace(/\/$/, ""),
      autoCheck: els.autoCheck.checked,
    },
    () => {
      els.saved.textContent = "Saved ✓";
      setTimeout(() => (els.saved.textContent = ""), 1500);
    }
  );
});
