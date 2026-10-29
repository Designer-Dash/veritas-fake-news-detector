// Veritas content script — Single-Theme Editorial Card & Auto-Navigation Engine
//
// Features:
// 1. Single source of truth: approved compact newspaper-theme card (340px, paper-white #FAF8F5).
// 2. Auto-updates on article navigation: pushState, replaceState, popstate, hashchange,
//    and debounced DOM MutationObserver for in-page headline/article transitions.
// 3. Concurrency-safe: invalidates prior in-flight requests on rapid article switching.
// 4. URL-based caching: avoids duplicate backend requests for previously analyzed articles.
// 5. Zero duplicate elements: guarantees exactly one #veritas-banner-host in the DOM at all times.
// 6. Two-way popup coordination: shares current verified state with the toolbar popup.
//
// Signature marker 011/013 · designer-dash

(function () {
  "use strict";

  // Prevent multiple executions of the content script in the same frame
  if (window.__veritasContentScriptInitialized) return;
  window.__veritasContentScriptInitialized = true;

  const DEFAULT_REGION = "in";
  const DEFAULT_OUTLETS = "India Today · The Hindu · Hindustan Times · NDTV · Times of India";
  const TIER_LABEL = { 1: "Tier 1", 2: "Tier 2", 3: "Tier 3" };

  // In-memory canonical URL cache: url -> { result, headline, timestamp }
  const urlResultCache = new Map();
  const dismissedUrls = new Set();

  let currentRequestId = 0;
  let currentActiveUrl = "";
  let lastAnalyzedHeadline = "";

  // ---------------------------------------------------------------------------
  // Card Styling (Approved Editorial News Theme)
  // ---------------------------------------------------------------------------
  const STYLE = {
    host:
      "all:initial;position:fixed;top:14px;right:14px;z-index:2147483646;" +
      "display:block;pointer-events:none;",
    card:
      "all:initial;display:block;width:340px;max-width:calc(100vw - 28px);" +
      "background:#FAF8F5;border:1px solid #E8E4DC;border-radius:12px;" +
      "box-shadow:0 8px 24px rgba(0,0,0,0.12);" +
      "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
      "font-size:13px;color:#0F172A;line-height:1.45;overflow:hidden;" +
      "padding:16px 18px;pointer-events:auto;box-sizing:border-box;",
    masthead:
      "display:flex;justify-content:space-between;align-items:flex-start;",
    mastheadTitles:
      "display:flex;flex-direction:column;",
    mastheadName:
      "font-family:'Playfair Display',Georgia,Times,serif;font-size:19px;font-weight:800;" +
      "letter-spacing:0.06em;color:#1E3A5F;line-height:1.1;margin:0;",
    mastheadDesk:
      "font-family:system-ui,sans-serif;font-size:9px;font-weight:700;" +
      "letter-spacing:0.09em;color:#64748B;text-transform:uppercase;margin-top:3px;",
    dismiss:
      "color:#94A3B8;font-size:16px;line-height:1;cursor:pointer;padding:2px 4px;" +
      "background:none;border:none;",
    redDivider:
      "height:3px;background:#8B1E2B;margin:10px 0 14px 0;border-radius:1px;",
    headline:
      "font-family:Georgia,'Times New Roman',serif;font-size:16.5px;font-weight:700;" +
      "line-height:1.35;color:#0F172A;margin:0 0 6px 0;overflow-wrap:anywhere;",
    subExplanation:
      "font-size:13px;color:#475569;line-height:1.45;margin:0 0 12px 0;",
    stateBox:
      "border-left:4px solid #C5832B;padding:2px 0 2px 12px;margin:12px 0 14px 0;",
    stateTitle:
      "font-family:Georgia,serif;font-size:15.5px;font-weight:700;color:#92580E;margin:0;",
    stateSubtitle:
      "font-size:12.5px;color:#4B5563;margin-top:3px;",
    hairline:
      "height:1px;background:#E8E4DC;margin:14px 0 12px 0;border:none;",
    statsRow:
      "display:flex;justify-content:space-between;align-items:center;font-size:12.5px;color:#475569;margin-bottom:6px;",
    evidenceRow:
      "font-size:13px;font-weight:600;color:#854D0E;display:flex;align-items:center;gap:4px;margin-bottom:4px;",
    stars:
      "color:#B45309;font-size:14px;letter-spacing:1px;",
    publishersList:
      "font-size:11.5px;color:#64748B;line-height:1.4;margin:0 0 14px 0;",
    actionBtn:
      "width:100%;padding:9px 14px;background:#FFFFFF;border:1.5px solid #CBD5E1;border-radius:8px;" +
      "font-family:system-ui,-apple-system,sans-serif;font-size:13.5px;font-weight:700;color:#1E3A5F;" +
      "cursor:pointer;transition:all 120ms ease;box-sizing:border-box;display:block;text-align:center;",
    sourcesDrawer:
      "display:none;margin-top:10px;max-height:200px;overflow-y:auto;border:1px solid #E2E8F0;" +
      "border-radius:8px;background:#FFFFFF;",
  };

  const SHADOW_CSS = [
    "@keyframes veritas-shimmer { 0% { background-position: -200px 0; } 100% { background-position: calc(200px + 100%) 0; } }",
    "@keyframes veritas-in { from { opacity:0; transform:translateY(-6px); } to { opacity:1; transform:none; } }",
    "@keyframes veritas-fade-in { from { opacity:0; } to { opacity:1; } }",
    "@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }",
    ".veritas-shimmer { height:12px; border-radius:4px; width:80%; background: linear-gradient(90deg, #F1EFEA 25%, #E6E2DA 50%, #F1EFEA 75%); background-size:400px 100%; animation: veritas-shimmer 1.5s ease-in-out infinite; margin:6px 0; display:block; }",
    ".action-btn:hover { background: #F8FAFC !important; border-color: #94A3B8 !important; }",
    ".source-item { padding: 7px 10px; border-bottom: 1px solid #F1F5F9; font-size: 12px; }",
    ".source-item:last-child { border-bottom: none; }",
    ".source-top { display: flex; align-items: center; gap: 6px; font-size: 11px; }",
    ".source-badge { font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 4px; color: #FFFFFF; }",
    ".source-badge.t1 { background: #15803D; }",
    ".source-badge.t2 { background: #B45309; }",
    ".source-badge.t3 { background: #64748B; }",
    ".source-outlet { font-weight: 600; color: #1E293B; }",
    ".source-title { display: block; margin-top: 2px; color: #334155; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }",
    ".source-title:hover { text-decoration: underline; color: #1E3A5F; }",
  ].join("\n");

  function el(tag, style, text) {
    const node = document.createElement(tag);
    if (style) node.setAttribute("style", style);
    if (text !== undefined) node.textContent = text;
    return node;
  }

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

  function getCanonicalUrl() {
    const link = document.querySelector('link[rel="canonical"]');
    if (link && link.href) {
      try {
        const u = new URL(link.href, location.href);
        return u.origin + u.pathname;
      } catch {}
    }
    return location.origin + location.pathname;
  }

  function isNewsPage() {
    const url = location.href.toLowerCase();
    const path = location.pathname.toLowerCase();

    // Never treat blank or pure root homepages as individual articles unless strong article tags exist
    if ((path === "/" || path === "" || path === "/index.html") && !document.querySelector("article")) {
      return false;
    }

    if (/news|article|story|\/\d{4}\/\d{2}\b|\/[a-z0-9_-]+-[0-9]{5,}\b/i.test(url)) {
      return true;
    }

    if (document.querySelector("article") || document.querySelector('[itemprop="articleBody"]')) {
      return true;
    }

    const ogType = document.querySelector('meta[property="og:type"]');
    if (ogType && /article/i.test(ogType.content)) {
      return true;
    }

    return false;
  }

  function extractHeadline() {
    const og = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
    if (og && og.content) return og.content.trim().slice(0, 512);
    const h1 = document.querySelector("h1");
    if (h1 && h1.textContent.trim()) return h1.textContent.trim().slice(0, 512);
    return document.title ? document.title.slice(0, 512) : "News Article";
  }

  function extractArticleText() {
    const candidates = [
      document.querySelector("article"),
      document.querySelector('[itemprop="articleBody"]'),
      document.querySelector("main"),
      document.body,
    ].filter(Boolean);
    for (const item of candidates) {
      const t = (item.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > 300) return t.slice(0, 12000);
    }
    return "";
  }

  function extractLocalClaim(headline, articleText) {
    const cleanBody = (articleText || "").replace(/\s+/g, " ").trim();
    const firstSentence = cleanBody.split(/(?<=[.!?])\s/)[0] || "";
    if (firstSentence && firstSentence.length > 20 && firstSentence.length < 200) {
      return `${headline}. ${firstSentence}`;
    }
    return headline;
  }

  function removeAllExistingBanners() {
    const hosts = document.querySelectorAll("#veritas-banner-host");
    for (const h of hosts) {
      h.remove();
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

  // ---------------------------------------------------------------------------
  // Card Creation & Verdict Rendering
  // ---------------------------------------------------------------------------
  function getOrCreateCard(headline) {
    removeAllExistingBanners();

    const host = document.createElement("div");
    host.id = "veritas-banner-host";
    host.setAttribute("style", STYLE.host);
    const shadow = host.attachShadow({ mode: "closed" });
    host._veritasShadow = shadow;

    const styleEl = document.createElement("style");
    styleEl.textContent = SHADOW_CSS;
    shadow.appendChild(styleEl);

    const card = el("div", STYLE.card);
    card.style.animation = "veritas-in 180ms ease-out";

    // Masthead
    const masthead = el("div", STYLE.masthead);
    const titles = el("div", STYLE.mastheadTitles);
    titles.appendChild(el("h1", STYLE.mastheadName, "VERITAS"));
    titles.appendChild(el("span", STYLE.mastheadDesk, "NEWS VERIFICATION DESK"));
    masthead.appendChild(titles);

    const closeBtn = el("button", STYLE.dismiss, "✕");
    closeBtn.setAttribute("title", "Close");
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dismissedUrls.add(getCanonicalUrl());
      host.remove();
    });
    masthead.appendChild(closeBtn);
    card.appendChild(masthead);

    // Red Divider
    card.appendChild(el("div", STYLE.redDivider));

    // Headline
    const h2 = el("h2", STYLE.headline, headline || "Checking article…");
    h2.id = "veritas-headline";
    card.appendChild(h2);

    // Sub-explanation
    const sub = el("p", STYLE.subExplanation, "Analyzing independent coverage…");
    sub.id = "veritas-sub";
    card.appendChild(sub);

    // Result state callout box
    const stateBox = el("div", STYLE.stateBox);
    stateBox.id = "veritas-state-box";
    const stateTitle = el("div", STYLE.stateTitle, "Checking sources");
    stateTitle.id = "veritas-state-title";
    const stateSubtitle = el("div", STYLE.stateSubtitle, "Scanning verified outlets.");
    stateSubtitle.id = "veritas-state-subtitle";
    stateBox.appendChild(stateTitle);
    stateBox.appendChild(stateSubtitle);
    card.appendChild(stateBox);

    // Slot for shimmers and verdict contents
    const slot = el("div", "margin-top: 10px;");
    slot.id = "veritas-slot";
    for (let i = 0; i < 3; i++) {
      const shimmer = el("div");
      shimmer.className = "veritas-shimmer";
      if (i === 1) shimmer.style.width = "92%";
      if (i === 2) shimmer.style.width = "65%";
      slot.appendChild(shimmer);
    }
    card.appendChild(slot);

    shadow.appendChild(card);
    document.documentElement.appendChild(host);

    return shadow;
  }

  function applyVerdict(result, currentHeadline) {
    const host = document.getElementById("veritas-banner-host");
    const shadow = host && host._veritasShadow;
    if (!shadow) return;

    const headlineEl = shadow.getElementById("veritas-headline");
    const subEl = shadow.getElementById("veritas-sub");
    const stateBoxEl = shadow.getElementById("veritas-state-box");
    const stateTitleEl = shadow.getElementById("veritas-state-title");
    const stateSubtitleEl = shadow.getElementById("veritas-state-subtitle");
    const slot = shadow.getElementById("veritas-slot");
    if (!slot) return;

    slot.textContent = "";
    slot.style.animation = "veritas-fade-in 220ms ease-out";

    const verdict = typeof result.verdict === "string" ? result.verdict : "uncertain";
    const coverageState = typeof result.coverage_state === "string" ? result.coverage_state : "";
    const confidence = Number(result.confidence) || 0.5;
    const aiMedia = Boolean(result.ai_media);

    const corroborating = Array.isArray(result.corroborating_sources)
      ? result.corroborating_sources.filter((m) => m && typeof m.url === "string" && /^https:\/\//i.test(m.url))
      : [];

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
      subExplanation = (typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
        ? result.summary
        : "This article expresses personal views or humor rather than factual claims.";
    } else if (aiMedia) {
      stateBorderColor = "#2563EB";
      stateTitleColor = "#1D4ED8";
      stateTitle = "Possible AI-generated media";
      stateSubtitle = "Media signals indicate synthetic origin.";
      subExplanation = (typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
        ? result.summary
        : "Media on this page contains cues consistent with AI synthesis.";
    } else if (verdict === "fake") {
      stateBorderColor = "#991B1B";
      stateTitleColor = "#7F1D1D";
      stateTitle = "Contradicted claim";
      stateSubtitle = "Directly contradicted by fact-checks or primary sources.";
      subExplanation = (typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
        ? result.summary
        : "This claim conflicts with reporting from verified news outlets.";
    } else if (verdict === "suspicious") {
      stateBorderColor = "#EA580C";
      stateTitleColor = "#C2410C";
      stateTitle = "Suspicious reporting";
      stateSubtitle = "Conflicts with reporting from verified outlets.";
      subExplanation = (typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
        ? result.summary
        : "Coverage contains uncorroborated elements or misleading framing.";
    } else if (verdict === "trusted") {
      stateBorderColor = "#15803D";
      stateTitleColor = "#166534";
      stateTitle = "Supported by coverage";
      stateSubtitle = "Corroborated by independent news outlets.";
      subExplanation = (typeof result.summary === "string" && result.summary && !result.summary.startsWith("Verdict:"))
        ? result.summary
        : "Multiple verified news outlets corroborate this story.";
    } else {
      stateBorderColor = "#C5832B";
      stateTitleColor = "#92580E";
      stateTitle = "Unverified for now";
      stateSubtitle = "Not a false-news verdict.";
      if (typeof result.summary === "string" && result.summary.trim() && !result.summary.startsWith("Verdict:")) {
        subExplanation = result.summary.trim();
      } else {
        subExplanation = "Independent coverage is still limited.";
      }
    }

    if (headlineEl) {
      headlineEl.textContent = currentHeadline || (result && result.headline) || extractHeadline();
    }

    if (stateBoxEl) stateBoxEl.style.borderLeftColor = stateBorderColor;
    if (stateTitleEl) {
      stateTitleEl.textContent = stateTitle;
      stateTitleEl.style.color = stateTitleColor;
    }
    if (stateSubtitleEl) stateSubtitleEl.textContent = stateSubtitle;
    if (subEl) subEl.textContent = subExplanation;

    // Hairline divider
    slot.appendChild(el("hr", STYLE.hairline));

    // Stats row
    const searchedCount = Number.isFinite(Number(result.sources_checked)) && Number(result.sources_checked) > 0
      ? Number(result.sources_checked)
      : Math.max(5, corroborating.length);
    const matchCount = corroborating.length;

    const statsRow = el("div", STYLE.statsRow);
    statsRow.appendChild(el("span", null, `${searchedCount} publishers searched`));
    statsRow.appendChild(el("span", null, `${matchCount} match${matchCount === 1 ? "" : "es"}`));
    slot.appendChild(statsRow);

    // Evidence signal stars
    const starsCount = Math.max(1, Math.min(5, Math.round(confidence * 5)));
    const evidenceRow = el("div", STYLE.evidenceRow);
    evidenceRow.appendChild(el("span", null, "Evidence signal"));
    const starsSpan = el("span", STYLE.stars, "★".repeat(starsCount) + "☆".repeat(5 - starsCount));
    evidenceRow.appendChild(starsSpan);
    slot.appendChild(evidenceRow);

    // Publishers List
    const pubList = el("p", STYLE.publishersList);
    if (corroborating.length > 0) {
      const uniqueOutlets = [...new Set(corroborating.map((s) => s.outlet || "News Outlet"))];
      pubList.textContent = uniqueOutlets.slice(0, 5).join(" · ");
    } else {
      pubList.textContent = DEFAULT_OUTLETS;
    }
    slot.appendChild(pubList);

    // Action Button ("View checked sources")
    const actionBtn = el("button", STYLE.actionBtn, "View checked sources");
    actionBtn.className = "action-btn";
    slot.appendChild(actionBtn);

    // Expandable Sources Drawer (hidden by default)
    const drawer = el("div", STYLE.sourcesDrawer);
    renderSourcesList(drawer, corroborating);
    slot.appendChild(drawer);

    actionBtn.addEventListener("click", () => {
      const isHidden = drawer.style.display === "none" || !drawer.style.display;
      if (isHidden) {
        drawer.style.display = "block";
        actionBtn.textContent = "Hide checked sources";
      } else {
        drawer.style.display = "none";
        actionBtn.textContent = "View checked sources";
      }
    });
  }

  function applyError(msg) {
    const host = document.getElementById("veritas-banner-host");
    const shadow = host && host._veritasShadow;
    if (!shadow) return;

    const subEl = shadow.getElementById("veritas-sub");
    const stateBoxEl = shadow.getElementById("veritas-state-box");
    const stateTitleEl = shadow.getElementById("veritas-state-title");
    const stateSubtitleEl = shadow.getElementById("veritas-state-subtitle");
    const slot = shadow.getElementById("veritas-slot");
    if (!slot) return;

    if (subEl) subEl.textContent = "Verification service could not complete the request.";
    if (stateBoxEl) stateBoxEl.style.borderLeftColor = "#DC2626";
    if (stateTitleEl) {
      stateTitleEl.textContent = "Backend unreachable";
      stateTitleEl.style.color = "#991B1B";
    }
    if (stateSubtitleEl) {
      stateSubtitleEl.textContent = msg && /auth/i.test(msg)
        ? String(msg).slice(0, 160)
        : "Ensure local backend is running (01_START_SERVER.bat), then re-check.";
    }

    slot.textContent = "";
    const errBox = el("div", "font-size:12px;color:#64748B;margin-top:6px;line-height:1.4;", "Local backend at http://localhost:8000");
    slot.appendChild(errBox);
  }

  // ---------------------------------------------------------------------------
  // Lifecycle & Navigation Orchestration
  // ---------------------------------------------------------------------------
  async function analyzeArticle(forceRecheck) {
    const canonical = getCanonicalUrl();

    // If dismissed for this URL and not forced, keep hidden
    if (dismissedUrls.has(canonical) && !forceRecheck) {
      removeAllExistingBanners();
      return;
    }

    // Gate: only analyze if the page looks like a news article
    if (!isNewsPage() && !forceRecheck) {
      removeAllExistingBanners();
      return;
    }

    const thisRequestId = ++currentRequestId;
    const headline = extractHeadline();
    const articleText = extractArticleText();
    const localClaim = extractLocalClaim(headline, articleText);

    // Cache hit (instant Back/Forward or repeated visit within 15 mins)
    if (!forceRecheck && urlResultCache.has(canonical)) {
      const entry = urlResultCache.get(canonical);
      if (Date.now() - entry.timestamp < 15 * 60 * 1000) {
        getOrCreateCard(entry.headline || headline);
        applyVerdict(entry.result, entry.headline || headline);
        return;
      }
    }

    // Phase 1: Render instant compact newspaper card with checking state
    getOrCreateCard(headline);

    const cfg = await new Promise((resolve) => {
      chrome.storage.sync.get(
        { backend: "http://localhost:8000", region: DEFAULT_REGION, autoCheck: true },
        (c) => resolve(c || {})
      );
    });

    if (!cfg.autoCheck && !forceRecheck) {
      removeAllExistingBanners();
      return;
    }

    const payload = {
      url: location.href,
      region: cfg.region || DEFAULT_REGION,
      headline,
      ...(cfg.privacyMode ? {} : { claim: localClaim }),
    };

    let responded = false;
    const timeoutId = setTimeout(() => {
      if (responded || thisRequestId !== currentRequestId) return;
      responded = true;
      applyError("Backend timeout");
    }, 25000);

    chrome.runtime.sendMessage({ type: "VERITAS_FETCH", payload }, (res) => {
      clearTimeout(timeoutId);
      if (thisRequestId !== currentRequestId) {
        // Obsolete request from previous article — DISCARD!
        return;
      }
      responded = true;

      if (chrome.runtime.lastError || !res || res.error) {
        const err = (res && res.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message);
        applyError(err);
        return;
      }

      // Store in canonical URL cache
      urlResultCache.set(canonical, {
        result: res,
        headline,
        timestamp: Date.now(),
      });

      applyVerdict(res, headline);
    });
  }

  // ---------------------------------------------------------------------------
  // History API & MutationObserver Event Listeners
  // ---------------------------------------------------------------------------
  let navDebounceTimer = null;

  function triggerNavigationCheck(force) {
    if (navDebounceTimer) clearTimeout(navDebounceTimer);
    navDebounceTimer = setTimeout(() => {
      const newUrl = getCanonicalUrl();
      const newHeadline = extractHeadline();

      if (!force && newUrl === currentActiveUrl && newHeadline === lastAnalyzedHeadline) {
        return;
      }

      currentActiveUrl = newUrl;
      lastAnalyzedHeadline = newHeadline;
      analyzeArticle(force);
    }, 180);
  }

  function initNavigationHooks() {
    // Monkey-patch History API for SPA route changes
    const originalPushState = history.pushState;
    const originalReplaceState = history.replaceState;

    history.pushState = function (...args) {
      const res = originalPushState.apply(this, args);
      window.dispatchEvent(new Event("veritas:locationchange"));
      return res;
    };

    history.replaceState = function (...args) {
      const res = originalReplaceState.apply(this, args);
      window.dispatchEvent(new Event("veritas:locationchange"));
      return res;
    };

    window.addEventListener("popstate", () => triggerNavigationCheck(false));
    window.addEventListener("hashchange", () => triggerNavigationCheck(false));
    window.addEventListener("veritas:locationchange", () => triggerNavigationCheck(false));

    // Debounced MutationObserver on document for SPA content transitions
    let domTimer = null;
    const observer = new MutationObserver(() => {
      if (domTimer) clearTimeout(domTimer);
      domTimer = setTimeout(() => {
        const newUrl = getCanonicalUrl();
        const newHeadline = extractHeadline();
        if (newUrl !== currentActiveUrl || (newHeadline && newHeadline !== lastAnalyzedHeadline)) {
          triggerNavigationCheck(false);
        }
      }, 300);
    });

    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  // ---------------------------------------------------------------------------
  // Toolbar Popup Communication
  // ---------------------------------------------------------------------------
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return;

    if (msg && msg.type === "VERITAS_GET_STATE") {
      const canonical = getCanonicalUrl();
      const cached = urlResultCache.get(canonical);
      if (cached && Date.now() - cached.timestamp < 15 * 60 * 1000) {
        sendResponse({ ok: true, cached: true, result: cached.result, headline: cached.headline });
      } else {
        sendResponse({ ok: true, cached: false, headline: extractHeadline() });
      }
      return true;
    }

    if (msg && (msg.type === "VERITAS_ANALYZE" || msg.type === "VERITAS_RECHECK")) {
      const canonical = getCanonicalUrl();
      dismissedUrls.delete(canonical);
      urlResultCache.delete(canonical);
      triggerNavigationCheck(true);
      sendResponse({ ok: true });
      return true;
    }
  });

  // ---------------------------------------------------------------------------
  // Initial Run
  // ---------------------------------------------------------------------------
  initNavigationHooks();
  currentActiveUrl = getCanonicalUrl();
  lastAnalyzedHeadline = extractHeadline();
  analyzeArticle(false);
})();
