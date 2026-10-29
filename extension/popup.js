// Veritas Extension Popup — Editorial News Desk Style
// Strict security: all backend-derived strings rendered via textContent only.
// No innerHTML, no eval, no unsafe DOM manipulations.
// Signature marker 012/013 · designer-dash

const headlineEl = document.getElementById("headline");
const subExplanationEl = document.getElementById("subExplanation");
const stateBoxEl = document.getElementById("stateBox");
const stateTitleEl = document.getElementById("stateTitle");
const stateSubtitleEl = document.getElementById("stateSubtitle");
const publishersSearchedEl = document.getElementById("publishersSearched");
const matchesFoundEl = document.getElementById("matchesFound");
const evidenceStarsEl = document.getElementById("evidenceStars");
const publishersListEl = document.getElementById("publishersList");
const toggleSourcesBtn = document.getElementById("toggleSourcesBtn");
const sourcesDrawerEl = document.getElementById("sourcesDrawer");
const contentLoadingEl = document.getElementById("contentLoading");
const contentReadyEl = document.getElementById("contentReady");
const closeBtn = document.getElementById("closeBtn");
const recheckBtn = document.getElementById("recheckBtn");
const settingsBtn = document.getElementById("settingsBtn");

const DEFAULT_OUTLETS = "India Today · The Hindu · Hindustan Times · NDTV · Times of India";

const TIER_LABEL = { 1: "Tier 1", 2: "Tier 2", 3: "Tier 3" };

function safeHttpUrl(u) {
  try {
    const x = new URL(u);
    return /^https?:$/.test(x.protocol) ? x.href : null;
  } catch {
    return null;
  }
}

function timeAgo(sec) {
  if (!sec) return "";
  const d = Math.max(0, Date.now() / 1000 - sec);
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

function setLoading(isLoading) {
  if (isLoading) {
    if (contentLoadingEl) contentLoadingEl.style.display = "block";
    if (contentReadyEl) contentReadyEl.style.display = "none";
  } else {
    if (contentLoadingEl) contentLoadingEl.style.display = "none";
    if (contentReadyEl) contentReadyEl.style.display = "block";
  }
}

function renderSourcesList(container, sources) {
  container.textContent = "";
  if (!sources || !sources.length) {
    const empty = document.createElement("div");
    empty.style.padding = "10px";
    empty.style.color = "#64748B";
    empty.style.fontSize = "12px";
    empty.style.textAlign = "center";
    empty.textContent = "No corroborating sources found in the searched outlets.";
    container.appendChild(empty);
    return;
  }

  for (const s of sources) {
    const item = document.createElement("div");
    item.className = "source-item";

    const top = document.createElement("div");
    top.className = "source-top";

    const tierNum = s.tier || 3;
    const badge = document.createElement("span");
    badge.className = `source-badge t${tierNum}`;
    badge.textContent = TIER_LABEL[tierNum] || "Tier 3";
    top.appendChild(badge);

    const outlet = document.createElement("span");
    outlet.className = "source-outlet";
    outlet.textContent = s.outlet || "News Outlet";
    top.appendChild(outlet);

    if (s.is_wire_syndication && s.wire_agency) {
      const wire = document.createElement("span");
      wire.style.fontSize = "10px";
      wire.style.color = "#475569";
      wire.textContent = `via ${s.wire_agency}`;
      top.appendChild(wire);
    }

    const tStr = timeAgo(s.published);
    if (tStr) {
      const timeSpan = document.createElement("span");
      timeSpan.style.marginLeft = "auto";
      timeSpan.style.color = "#94A3B8";
      timeSpan.textContent = tStr;
      top.appendChild(timeSpan);
    }

    item.appendChild(top);

    const href = safeHttpUrl(s.url);
    const titleLink = href ? document.createElement("a") : document.createElement("span");
    titleLink.className = "source-title";
    titleLink.textContent = s.title || s.outlet || "Reference Article";
    if (href) {
      titleLink.href = href;
      titleLink.target = "_blank";
      titleLink.rel = "noopener noreferrer";
    }
    item.appendChild(titleLink);

    container.appendChild(item);
  }
}

function render(result, activeTabTitle) {
  setLoading(false);

  // Headline
  const displayHeadline = (result && typeof result.headline === "string" && result.headline.trim())
    ? result.headline.trim()
    : (activeTabTitle || "News Article");
  headlineEl.textContent = displayHeadline;

  const verdict = result && typeof result.verdict === "string" ? result.verdict : "uncertain";
  const coverageState = result && typeof result.coverage_state === "string" ? result.coverage_state : "";
  const confidence = Number(result && result.confidence) || 0.5;
  const suspicion = Number(result && result.suspicion) || 0.0;
  const aiMedia = Boolean(result && result.ai_media);

  const corroborating = Array.isArray(result && result.corroborating_sources)
    ? result.corroborating_sources.filter((m) => m && typeof m.url === "string" && /^https:\/\//i.test(m.url))
    : [];

  // Determine state metadata
  let stateTitle = "Unverified for now";
  let stateSubtitle = "Not a false-news verdict.";
  let stateBorderColor = "#C5832B";
  let stateTitleColor = "#92580E";
  let subExplanation = "Independent coverage is still limited.";

  if (coverageState.startsWith("article_type_")) {
    stateBorderColor = "#64748B";
    stateTitleColor = "#334155";
    stateTitle = coverageState === "article_type_satire" ? "Satire content" : "Opinion piece";
    stateSubtitle = "Not a factual news report — verification does not apply.";
    subExplanation = (result && typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
      ? result.summary
      : "This article expresses personal views or humor rather than factual claims.";
  } else if (aiMedia) {
    stateBorderColor = "#2563EB";
    stateTitleColor = "#1D4ED8";
    stateTitle = "Possible AI-generated media";
    stateSubtitle = "Media signals indicate synthetic origin.";
    subExplanation = (result && typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
      ? result.summary
      : "Media on this page contains cues consistent with AI synthesis.";
  } else if (verdict === "fake") {
    stateBorderColor = "#991B1B";
    stateTitleColor = "#7F1D1D";
    stateTitle = "Contradicted claim";
    stateSubtitle = "Directly contradicted by fact-checks or primary sources.";
    subExplanation = (result && typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
      ? result.summary
      : "This claim conflicts with reporting from verified news outlets.";
  } else if (verdict === "suspicious") {
    stateBorderColor = "#EA580C";
    stateTitleColor = "#C2410C";
    stateTitle = "Suspicious reporting";
    stateSubtitle = "Conflicts with reporting from verified outlets.";
    subExplanation = (result && typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
      ? result.summary
      : "Coverage contains uncorroborated elements or misleading framing.";
  } else if (verdict === "trusted") {
    stateBorderColor = "#15803D";
    stateTitleColor = "#166534";
    stateTitle = "Supported by coverage";
    stateSubtitle = "Corroborated by independent news outlets.";
    subExplanation = (result && typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
      ? result.summary
      : "Multiple verified news outlets corroborate this story.";
  } else {
    // Neutral "Unverified for now" state
    stateBorderColor = "#C5832B";
    stateTitleColor = "#92580E";
    stateTitle = "Unverified for now";
    stateSubtitle = "Not a false-news verdict.";
    if (result && typeof result.summary === "string" && result.summary.trim() && !result.summary.startsWith("Verdict:")) {
      subExplanation = result.summary.trim();
    } else {
      subExplanation = "Independent coverage is still limited.";
    }
  }

  // Update State Callout
  stateBoxEl.style.borderLeftColor = stateBorderColor;
  stateTitleEl.textContent = stateTitle;
  stateTitleEl.style.color = stateTitleColor;
  stateSubtitleEl.textContent = stateSubtitle;
  subExplanationEl.textContent = subExplanation;

  // Publishers searched & matches
  const searchedCount = Number.isFinite(Number(result && result.sources_checked)) && Number(result.sources_checked) > 0
    ? Number(result.sources_checked)
    : Math.max(5, corroborating.length);
  publishersSearchedEl.textContent = `${searchedCount} publishers searched`;

  const matchCount = corroborating.length;
  matchesFoundEl.textContent = `${matchCount} match${matchCount === 1 ? "" : "es"}`;

  // Evidence Signal Stars
  const starsCount = Math.max(1, Math.min(5, Math.round(confidence * 5)));
  evidenceStarsEl.textContent = "★".repeat(starsCount) + "☆".repeat(5 - starsCount);

  // Publishers List
  if (corroborating.length > 0) {
    const uniqueOutlets = [...new Set(corroborating.map((s) => s.outlet || "News Outlet"))];
    publishersListEl.textContent = uniqueOutlets.slice(0, 5).join(" · ");
  } else {
    publishersListEl.textContent = DEFAULT_OUTLETS;
  }

  // Populate sources drawer (kept hidden by default!)
  renderSourcesList(sourcesDrawerEl, corroborating);
  sourcesDrawerEl.style.display = "none";
  toggleSourcesBtn.textContent = "View checked sources";
  toggleSourcesBtn.style.display = "block";
}

function applyError(msg, activeTabTitle) {
  setLoading(false);
  headlineEl.textContent = activeTabTitle || "News Article";
  subExplanationEl.textContent = "Verification service could not complete the request.";

  stateBoxEl.style.borderLeftColor = "#DC2626";
  stateTitleEl.textContent = "Backend unreachable";
  stateTitleEl.style.color = "#991B1B";
  stateSubtitleEl.textContent = msg && /auth/i.test(msg)
    ? String(msg).slice(0, 160)
    : "Ensure local backend is running (01_START_SERVER.bat), then re-check.";

  publishersSearchedEl.textContent = "0 publishers searched";
  matchesFoundEl.textContent = "0 matches";
  evidenceStarsEl.textContent = "☆☆☆☆☆";
  publishersListEl.textContent = "Local backend at http://localhost:8000";

  sourcesDrawerEl.textContent = "";
  sourcesDrawerEl.style.display = "none";
  toggleSourcesBtn.style.display = "none";
}

// Drawer Toggle Action
if (toggleSourcesBtn) {
  toggleSourcesBtn.addEventListener("click", () => {
    const isHidden = sourcesDrawerEl.style.display === "none" || !sourcesDrawerEl.style.display;
    if (isHidden) {
      sourcesDrawerEl.style.display = "block";
      toggleSourcesBtn.textContent = "Hide checked sources";
    } else {
      sourcesDrawerEl.style.display = "none";
      toggleSourcesBtn.textContent = "View checked sources";
    }
  });
}

// Close Button
if (closeBtn) {
  closeBtn.addEventListener("click", () => window.close());
}

// Settings Link
if (settingsBtn) {
  settingsBtn.addEventListener("click", () => chrome.runtime.openOptionsPage());
}

// Recheck Action
if (recheckBtn) {
  recheckBtn.addEventListener("click", () => analyzeActiveTab(true));
}

async function analyzeActiveTab(triggerBanner) {
  setLoading(true);
  let activeTab = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    activeTab = tab;
  } catch {
    // tab query error handled below
  }

  const activeTitle = activeTab && activeTab.title ? activeTab.title.trim() : "";
  if (activeTitle) {
    headlineEl.textContent = activeTitle;
  }

  if (!activeTab || !activeTab.url) {
    applyError("No active web page detected.", activeTitle);
    return;
  }

  // If recheck is explicitly clicked, notify content script to invalidate cache
  if (triggerBanner && activeTab.id) {
    chrome.tabs.sendMessage(activeTab.id, { type: "VERITAS_RECHECK" }, () => void chrome.runtime.lastError);
  }

  // 1. Try to fetch current verified state from content.js (avoids duplicate fetch)
  if (!triggerBanner && activeTab.id) {
    chrome.tabs.sendMessage(activeTab.id, { type: "VERITAS_GET_STATE" }, (stateRes) => {
      if (!chrome.runtime.lastError && stateRes && stateRes.cached && stateRes.result) {
        render(stateRes.result, stateRes.headline || activeTitle);
        return;
      }
      // 2. Fallback to background fetch if content script has not cached this URL
      fetchViaBackground(activeTab, activeTitle);
    });
    return;
  }

  fetchViaBackground(activeTab, activeTitle);
}

function fetchViaBackground(activeTab, activeTitle) {
  chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload: { url: activeTab.url } }, (res) => {
    if (chrome.runtime.lastError || !res || res.error) {
      const err = (res && res.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message);
      applyError(err, activeTitle);
      return;
    }
    render(res, activeTitle);
  });
}

// Initial trigger
analyzeActiveTab(false);
