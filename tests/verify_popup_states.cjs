// Automated DOM Unit Test for Veritas Extension Editorial Redesign
// Tests loading, error, and all verdict states with strict assertion checks.

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
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener(event, fn) {
      if (!this._listeners) this._listeners = {};
      this._listeners[event] = fn;
    },
    click() {
      if (this._listeners && this._listeners["click"]) {
        this._listeners["click"]();
      }
    }
  };
}

function runTests() {
  console.log("=== Testing Veritas Extension Editorial UI States ===\n");
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

  // 1. Verify popup.html file existence & key CSS rules
  const htmlPath = path.join(__dirname, "../extension/popup.html");
  const htmlContent = fs.readFileSync(htmlPath, "utf8");
  assert(htmlContent.includes("340px"), "popup.html specifies 340px width");
  assert(htmlContent.includes("#FAF8F5"), "popup.html uses paper-white surface #FAF8F5");
  assert(htmlContent.includes("#1E3A5F"), "popup.html masthead uses muted navy #1E3A5F");
  assert(htmlContent.includes("#8B1E2B"), "popup.html contains 3px crimson/red divider #8B1E2B");
  assert(htmlContent.includes("Georgia"), "popup.html headline font includes Georgia serif");
  assert(htmlContent.includes("NEWS VERIFICATION DESK"), "popup.html contains masthead desk label");
  assert(htmlContent.includes("View checked sources"), "popup.html contains 'View checked sources' action");

  // 2. Mock environment for popup.js logic
  const elements = {};
  const mockIds = [
    "headline", "subExplanation", "stateBox", "stateTitle", "stateSubtitle",
    "publishersSearched", "matchesFound", "evidenceStars", "publishersList",
    "toggleSourcesBtn", "sourcesDrawer", "contentLoading", "contentReady",
    "closeBtn", "recheckBtn", "settingsBtn"
  ];

  for (const id of mockIds) {
    elements[id] = createMockElement("div");
    elements[id].id = id;
  }

  global.document = {
    getElementById(id) {
      return elements[id] || null;
    },
    createElement(tag) {
      return createMockElement(tag);
    }
  };

  global.window = {
    close: () => {}
  };

  global.chrome = {
    tabs: {
      query: (opts, cb) => cb([{ id: 1, url: "https://example.com/news/123", title: "INDIA Bloc leaders gather in Delhi over EC row" }])
    },
    runtime: {
      lastError: null,
      sendMessage: () => {},
      openOptionsPage: () => {}
    }
  };

  // Read and execute popup.js in sandbox
  const popupJsCode = fs.readFileSync(path.join(__dirname, "../extension/popup.js"), "utf8");
  
  // Wrap in a function that exposes testable render & applyError functions
  const sandbox = new Function("document", "window", "chrome", `
    ${popupJsCode}
    return { render, applyError, setLoading, elements: {
      headlineEl, subExplanationEl, stateBoxEl, stateTitleEl, stateSubtitleEl,
      publishersSearchedEl, matchesFoundEl, evidenceStarsEl, publishersListEl,
      toggleSourcesBtn, sourcesDrawerEl, contentLoadingEl, contentReadyEl
    } };
  `);

  const app = sandbox(global.document, global.window, global.chrome);

  // Test State 1: Loading State
  app.setLoading(true);
  assert(elements.contentLoading.style.display === "block", "Loading state shows contentLoading");
  assert(elements.contentReady.style.display === "none", "Loading state hides contentReady");

  // Test State 2: Neutral "Unverified for now"
  app.render({
    verdict: "uncertain",
    confidence: 1.0,
    sources_checked: 5,
    corroborating_sources: [],
    headline: "INDIA Bloc leaders gather in Delhi over EC row",
    summary: "Independent coverage is still limited."
  }, "INDIA Bloc leaders gather in Delhi over EC row");

  assert(elements.contentReady.style.display === "block", "Ready state shows contentReady");
  assert(elements.headline.textContent === "INDIA Bloc leaders gather in Delhi over EC row", "Headline matches article");
  assert(elements.stateTitle.textContent === "Unverified for now", "State title is 'Unverified for now'");
  assert(elements.stateSubtitle.textContent === "Not a false-news verdict.", "State subtitle is 'Not a false-news verdict.'");
  assert(elements.stateBox.style.borderLeftColor === "#C5832B", "State left border is gold/amber #C5832B");
  assert(elements.subExplanation.textContent === "Independent coverage is still limited.", "Sub-explanation renders summary");
  assert(elements.publishersSearched.textContent === "5 publishers searched", "Publishers searched count is 5");
  assert(elements.matchesFound.textContent === "0 matches", "Matches count is 0 matches");
  assert(elements.evidenceStars.textContent === "★★★★★", "Evidence stars display ★★★★★");
  assert(elements.toggleSourcesBtn.textContent === "View checked sources", "Button text is 'View checked sources'");
  assert(elements.sourcesDrawer.style.display === "none", "Sources drawer is hidden by default");

  // Test Drawer Toggle on click
  elements.toggleSourcesBtn.click();
  assert(elements.sourcesDrawer.style.display === "block", "Clicking button reveals sources drawer");
  assert(elements.toggleSourcesBtn.textContent === "Hide checked sources", "Button toggles to 'Hide checked sources'");
  elements.toggleSourcesBtn.click();
  assert(elements.sourcesDrawer.style.display === "none", "Clicking again hides sources drawer");
  assert(elements.toggleSourcesBtn.textContent === "View checked sources", "Button toggles back to 'View checked sources'");

  // Test State 3: Supported by coverage
  app.render({
    verdict: "trusted",
    confidence: 0.95,
    sources_checked: 7,
    corroborating_sources: [
      { outlet: "The Hindu", url: "https://thehindu.com/story", tier: 1 },
      { outlet: "NDTV", url: "https://ndtv.com/story", tier: 2 }
    ],
    headline: "ISRO launches earth observation satellite",
    summary: "Multiple verified news outlets corroborate this story."
  });
  assert(elements.stateTitle.textContent === "Supported by coverage", "Supported state title is 'Supported by coverage'");
  assert(elements.stateSubtitle.textContent === "Corroborated by independent news outlets.", "Supported subtitle matches");
  assert(elements.stateBox.style.borderLeftColor === "#15803D", "Supported border is forest green #15803D");
  assert(elements.matchesFound.textContent === "2 matches", "Matches count shows 2 matches");

  // Test State 4: Suspicious reporting
  app.render({
    verdict: "suspicious",
    confidence: 0.4,
    sources_checked: 6,
    corroborating_sources: [
      { outlet: "Local Blog", url: "https://example.com/blog", tier: 3 }
    ],
    summary: "Coverage contains uncorroborated elements."
  });
  assert(elements.stateTitle.textContent === "Suspicious reporting", "Suspicious title is 'Suspicious reporting'");
  assert(elements.stateSubtitle.textContent === "Conflicts with reporting from verified outlets.", "Suspicious subtitle matches");
  assert(elements.stateBox.style.borderLeftColor === "#EA580C", "Suspicious border is amber/orange #EA580C");

  // Test State 5: Contradicted claim (Fake)
  app.render({
    verdict: "fake",
    confidence: 0.2,
    sources_checked: 8,
    corroborating_sources: [],
    summary: "Directly contradicted by fact-checks."
  });
  assert(elements.stateTitle.textContent === "Contradicted claim", "Fake state title is 'Contradicted claim'");
  assert(elements.stateSubtitle.textContent === "Directly contradicted by fact-checks or primary sources.", "Fake subtitle matches");
  assert(elements.stateBox.style.borderLeftColor === "#991B1B", "Fake border is crimson red #991B1B");

  // Test State 6: Opinion Piece / Satire
  app.render({
    verdict: "uncertain",
    coverage_state: "article_type_opinion",
    confidence: 0.5,
    summary: "This article expresses personal views rather than factual claims."
  });
  assert(elements.stateTitle.textContent === "Opinion piece", "Opinion piece title matches");
  assert(elements.stateSubtitle.textContent === "Not a factual news report — verification does not apply.", "Opinion piece subtitle matches");
  assert(elements.stateBox.style.borderLeftColor === "#64748B", "Opinion border is slate #64748B");

  // Test State 7: Error State
  app.applyError("Connection refused to port 8000", "Sample Article Headline");
  assert(elements.stateTitle.textContent === "Backend unreachable", "Error title is 'Backend unreachable'");
  assert(elements.evidenceStars.textContent === "☆☆☆☆☆", "Error evidence stars are empty ☆☆☆☆☆");
  assert(elements.stateBox.style.borderLeftColor === "#DC2626", "Error border is red #DC2626");
  assert(elements.toggleSourcesBtn.style.display === "none", "Sources toggle is hidden on error");

  // 3. Verify content.js compliance
  const contentJsCode = fs.readFileSync(path.join(__dirname, "../extension/content.js"), "utf8");
  assert(contentJsCode.includes("width:340px"), "content.js uses 340px card width");
  assert(contentJsCode.includes("#FAF8F5"), "content.js uses paper-white #FAF8F5");
  assert(contentJsCode.includes("#1E3A5F"), "content.js masthead uses muted navy #1E3A5F");
  assert(contentJsCode.includes("#8B1E2B"), "content.js contains red divider #8B1E2B");
  assert(contentJsCode.includes("Georgia"), "content.js headline uses Georgia serif");
  assert(contentJsCode.includes("Unverified for now"), "content.js handles 'Unverified for now'");
  assert(contentJsCode.includes("Not a false-news verdict."), "content.js specifies 'Not a false-news verdict.'");
  assert(contentJsCode.includes("View checked sources"), "content.js has 'View checked sources' action");
  assert(contentJsCode.includes("011/013 · designer-dash"), "content.js preserves signature marker");

  console.log(`\n🎉 ALL ${passed} / ${total} UI REDESIGN TESTS PASSED SUCCESSFULLY!`);
}

runTests();
