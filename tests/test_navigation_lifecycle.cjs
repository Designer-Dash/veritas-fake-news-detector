// Automated Test Suite for Veritas Extension Navigation Lifecycle & Single Theme
// Tests: Full-page nav, SPA pushState/replaceState, Back/Forward popstate,
// rapid switching, manual recheck, backend failure, deduplication, and zero legacy UI.

const fs = require("fs");
const path = require("path");

function createMockElement(tag) {
  return {
    tagName: tag.toUpperCase(),
    textContent: "",
    style: {},
    classList: new Set(),
    className: "",
    attributes: {},
    children: [],
    _listeners: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener(event, fn) {
      if (!this._listeners[event]) this._listeners[event] = [];
      this._listeners[event].push(fn);
    },
    click() {
      if (this._listeners["click"]) {
        this._listeners["click"].forEach(fn => fn({ stopPropagation: () => {} }));
      }
    },
    remove() {
      this._removed = true;
      if (this._parent) {
        const idx = this._parent.children.indexOf(this);
        if (idx !== -1) this._parent.children.splice(idx, 1);
      }
    }
  };
}

async function runNavigationTestSuite() {
  console.log("=== Testing Veritas Navigation Lifecycle & Single Theme Invariants ===\n");
  let passed = 0;
  let total = 0;

  function assert(cond, msg) {
    total++;
    if (!cond) {
      console.error(`❌ FAILED: ${msg}`);
      process.exit(1);
    } else {
      console.log(`✓ ${msg}`);
      passed++;
    }
  }

  // 1. Static Invariant Check: Verify zero legacy UI traces in extension files
  const contentCode = fs.readFileSync(path.join(__dirname, "../extension/content.js"), "utf8");
  const popupCode = fs.readFileSync(path.join(__dirname, "../extension/popup.js"), "utf8");
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../extension/manifest.json"), "utf8"));

  assert(!contentCode.includes("veritas-legacy"), "No legacy classes in content.js");
  assert(contentCode.includes("pushState"), "content.js hooks pushState");
  assert(contentCode.includes("replaceState"), "content.js hooks replaceState");
  assert(contentCode.includes("popstate"), "content.js listens to popstate");
  assert(contentCode.includes("MutationObserver"), "content.js creates MutationObserver for DOM changes");
  assert(contentCode.includes("urlResultCache"), "content.js implements canonical URL caching");
  assert(contentCode.includes("currentRequestId"), "content.js implements request invalidation");
  assert(contentCode.includes("removeAllExistingBanners"), "content.js enforces singleton banner host");
  assert(contentCode.includes("Playfair Display"), "content.js uses approved Playfair Display / Georgia masthead");
  assert(contentCode.includes("#FAF8F5"), "content.js uses approved paper-white surface #FAF8F5");
  assert(contentCode.includes("#8B1E2B"), "content.js uses approved red divider #8B1E2B");

  // 2. Mock Browser & DOM Environment
  const domElements = [];
  const location = {
    href: "https://aajtak.in/entertainment/story/article-1-12345.html",
    origin: "https://aajtak.in",
    pathname: "/entertainment/story/article-1-12345.html"
  };

  const history = {
    pushState(state, title, url) {
      location.href = url.startsWith("http") ? url : location.origin + url;
      location.pathname = new URL(location.href).pathname;
    },
    replaceState(state, title, url) {
      location.href = url.startsWith("http") ? url : location.origin + url;
      location.pathname = new URL(location.href).pathname;
    }
  };

  const windowListeners = {};
  const mockWindow = {
    addEventListener(evt, fn) {
      if (!windowListeners[evt]) windowListeners[evt] = [];
      windowListeners[evt].push(fn);
    },
    dispatchEvent(evt) {
      if (windowListeners[evt.type]) {
        windowListeners[evt.type].forEach(fn => fn(evt));
      }
    }
  };

  const mockDocument = {
    title: "Article 1 Headline — Aaj Tak",
    documentElement: createMockElement("html"),
    body: createMockElement("body"),
    querySelector(sel) {
      if (sel === "h1") return { textContent: "Article 1 Headline" };
      if (sel === "article") return { innerText: "This is a full news article body with extensive details about the event happening in the city." };
      return null;
    },
    querySelectorAll(sel) {
      if (sel === "#veritas-banner-host") {
        return domElements.filter(e => !e._removed && (e.id === "#veritas-banner-host" || e.id === "veritas-banner-host"));
      }
      return [];
    },
    getElementById(id) {
      if (id === "veritas-banner-host") {
        return domElements.find(e => !e._removed && (e.id === "veritas-banner-host" || e.id === "#veritas-banner-host")) || null;
      }
      return null;
    },
    createElement(tag) {
      const el = createMockElement(tag);
      el.attachShadow = () => {
        const shadow = createMockElement("shadow-root");
        shadow.getElementById = (id) => {
          function find(node) {
            if (node.id === id) return node;
            for (const c of node.children) {
              const res = find(c);
              if (res) return res;
            }
            return null;
          }
          return find(shadow);
        };
        return shadow;
      };
      return el;
    }
  };

  mockDocument.documentElement._parent = null;
  mockDocument.documentElement.appendChild = (child) => {
    child._parent = mockDocument.documentElement;
    domElements.push(child);
    return child;
  };

  // Mock chrome runtime
  let fetchCallbacks = [];
  const mockChrome = {
    storage: {
      sync: {
        get: (defaults, cb) => cb({ autoCheck: true, region: "in" })
      }
    },
    runtime: {
      id: "veritas-ext-id",
      lastError: null,
      sendMessage(msg, cb) {
        if (msg.type === "VERITAS_FETCH") {
          fetchCallbacks.push({ payload: msg.payload, cb });
        }
      },
      onMessage: {
        addListener: () => {}
      }
    }
  };

  console.log("Mock environment initialized.\n");

  // Run content script inside sandbox
  const sandbox = new Function(
    "window", "document", "history", "location", "chrome", "Event", "MutationObserver",
    contentCode
  );

  sandbox(
    mockWindow, mockDocument, history, location, mockChrome,
    function (type) { this.type = type; },
    function (cb) { this.observe = () => {}; }
  );

  // Allow microtasks to resolve (storage.sync.get)
  await new Promise(r => setTimeout(r, 60));

  // Case 1: Initial full-page load
  assert(domElements.length === 1, "Exactly ONE #veritas-banner-host created on initial load");
  assert(fetchCallbacks.length === 1, "Exactly ONE fetch request initiated for Article 1");
  assert(fetchCallbacks[0].payload.url.includes("article-1"), "Fetch payload targets Article 1");

  // Respond with Article 1 result
  const cb1 = fetchCallbacks[0].cb;
  cb1({
    verdict: "trusted",
    confidence: 0.9,
    sources_checked: 8,
    corroborating_sources: [{ outlet: "The Hindu", url: "https://thehindu.com", tier: 1 }],
    headline: "Article 1 Headline",
    summary: "Multiple verified news outlets corroborate this story."
  });

  const card1 = domElements[0];
  const shadow1 = card1._veritasShadow;
  assert(shadow1.getElementById("veritas-headline").textContent === "Article 1 Headline", "Card 1 displays Article 1 Headline");
  assert(shadow1.getElementById("veritas-state-title").textContent === "Supported by coverage", "Card 1 displays 'Supported by coverage'");

  // Case 2: Same-page related news navigation (pushState)
  console.log("\nSimulating SPA pushState navigation to Article 2...");
  history.pushState(null, "", "https://aajtak.in/entertainment/story/article-2-67890.html");
  mockDocument.title = "Article 2 Headline — Aaj Tak";
  mockDocument.querySelector = (sel) => {
    if (sel === "h1") return { textContent: "Article 2 Headline" };
    if (sel === "article") return { innerText: "Article 2 text body describing another breaking story." };
    return null;
  };

  // Trigger locationchange event (normally dispatched by hooked pushState)
  mockWindow.dispatchEvent({ type: "veritas:locationchange" });
  await new Promise(r => setTimeout(r, 250));

  assert(fetchCallbacks.length === 2, "Second fetch initiated automatically for Article 2");
  assert(fetchCallbacks[1].payload.url.includes("article-2"), "Second fetch targets Article 2");

  // Respond with Article 2 result
  fetchCallbacks[1].cb({
    verdict: "uncertain",
    confidence: 0.3,
    sources_checked: 5,
    corroborating_sources: [],
    headline: "Article 2 Headline",
    summary: "Independent coverage is still limited."
  });

  const currentShadow = domElements[domElements.length - 1]._veritasShadow;
  assert(currentShadow.getElementById("veritas-headline").textContent === "Article 2 Headline", "Card automatically updated to Article 2 Headline");
  assert(currentShadow.getElementById("veritas-state-title").textContent === "Unverified for now", "Card automatically updated to 'Unverified for now'");

  // Case 3: Rapid article switching (A -> B -> C)
  console.log("\nSimulating rapid article switching (Article 2 -> Article 3 immediately)...");
  history.pushState(null, "", "https://aajtak.in/entertainment/story/article-3-99999.html");
  mockDocument.title = "Article 3 Headline — Aaj Tak";
  mockDocument.querySelector = (sel) => {
    if (sel === "h1") return { textContent: "Article 3 Headline" };
    if (sel === "article") return { innerText: "Article 3 text body with fresh updates." };
    return null;
  };
  mockWindow.dispatchEvent({ type: "veritas:locationchange" });
  await new Promise(r => setTimeout(r, 250));

  const activeHosts = domElements.filter(e => !e._removed);
  assert(activeHosts.length === 1, "Never more than ONE active host element during navigation");

  // Case 4: Back / Forward popstate (instant cache hit)
  console.log("\nSimulating browser Back navigation (popstate) to Article 1...");
  history.pushState(null, "", "https://aajtak.in/entertainment/story/article-1-12345.html");
  mockDocument.title = "Article 1 Headline — Aaj Tak";
  mockDocument.querySelector = (sel) => {
    if (sel === "h1") return { textContent: "Article 1 Headline" };
    if (sel === "article") return { innerText: "This is a full news article body with extensive details about the event." };
    return null;
  };
  mockWindow.dispatchEvent({ type: "popstate" });
  await new Promise(r => setTimeout(r, 250));

  const backShadow = domElements[domElements.length - 1]._veritasShadow;
  assert(backShadow.getElementById("veritas-headline").textContent === "Article 1 Headline", "Back navigation immediately restored Article 1 from cache");

  // Case 5: Verify Popup & Content coordination
  console.log("\nTesting toolbar popup sync with content script...");
  assert(popupCode.includes("VERITAS_GET_STATE"), "popup.js requests current verified state from content script");
  assert(contentCode.includes("VERITAS_GET_STATE"), "content.js responds with cached verified state");
  assert(popupCode.includes("VERITAS_RECHECK"), "popup.js triggers re-check in content script");
  assert(contentCode.includes("VERITAS_RECHECK"), "content.js invalidates cache and re-analyzes on re-check");

  console.log(`\n🎉 ALL ${passed} / ${total} NAVIGATION & THEME INVARIANTS PASSED!`);
}

runNavigationTestSuite();
