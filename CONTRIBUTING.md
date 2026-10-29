# 🤝 Contributing to Veritas (Fake & AI-Generated News Detector)

Welcome! This guide explains how to clone, set up, run, edit, test, and push contributions to the **Veritas** project.

---

## 🚀 Quick Start for Collaborators (Windows 1-Click)

### 1. Clone the Repository
Open PowerShell or Command Prompt and clone the repository:
```bash
git clone https://github.com/Designer-Dash/veritas-fake-news-detector.git
cd veritas-fake-news-detector
```

### 2. Run 1-Click Environment Setup
Double-click `SETUP.bat` (or run in terminal):
```cmd
SETUP.bat
```
This automatically:
- Checks your Python 3.10+ installation
- Creates an isolated virtual environment in `backend\.venv`
- Installs and upgrades all runtime and testing dependencies (`fastapi`, `uvicorn`, `pydantic`, `pytest`, `httpx`)

---

## 🖥️ Running the Project

You can control everything from **`00_QUICK_START.bat`** or use the individual launchers:

| Script / Launcher | What It Does |
|---|---|
| **`00_QUICK_START.bat`** | Master control menu with all launch, extension, and test options. |
| **`01_START_SERVER.bat`** | Launches FastAPI backend on `http://127.0.0.1:8000` (auto-activates venv). |
| **`02_LOAD_EXTENSION.bat`** | Copies extension path to your clipboard and opens `chrome://extensions`. |
| **`03_TEST_SYSTEM.bat`** | Runs the full offline pytest verification suite (all green). |

---

## 🧩 Loading the Chrome Extension

1. Double-click `02_LOAD_EXTENSION.bat` (or open Google Chrome and navigate to `chrome://extensions`).
2. Toggle on **Developer mode** in the top-right corner.
3. Click **Load unpacked** in the top-left corner.
4. Select the `extension` folder inside this repository:
   ```
   <repo-folder>\extension
   ```
5. Pin the **Veritas** icon to your Chrome toolbar.
6. Open any news article online (e.g. Reuters, BBC, The Hindu, AP News). The Veritas editorial newspaper badge will appear and analyze the article in real time!

---

## 📁 Repository Structure

```
veritas-fake-news-detector/
├── 00_QUICK_START.bat         # Master launcher menu
├── 01_START_SERVER.bat        # FastAPI server launcher
├── 02_LOAD_EXTENSION.bat      # 1-click Chrome extension loader
├── 03_TEST_SYSTEM.bat         # Test runner (pytest)
├── SETUP.bat                  # 1-click venv & pip installer
├── run.py                     # Python server entrypoint with auto-dependency fallback
├── LICENSE                    # MIT open-source license
├── README.md                  # Comprehensive project documentation
├── CONTRIBUTING.md            # This collaborator guide
│
├── backend/                   # FastAPI Backend
│   ├── app/
│   │   ├── main.py            # API routes (/analyze, /health)
│   │   ├── pipeline.py        # Core 4-signal verification pipeline
│   │   ├── verification.py    # Evidence scoring formula (E = 0.30M + 0.30C + 0.20F + 0.20S)
│   │   ├── cross_check.py     # Live Google News RSS multi-source retrieval
│   │   ├── claim_analysis.py  # Claim extraction & date parsing
│   │   ├── ai_signals.py      # Media provenance & synthetic detection
│   │   ├── text_analysis.py   # Stylometric cues & clickbait heuristics
│   │   ├── config.py          # Thresholds and environment flags
│   │   ├── sources.py         # Trust tier classifications (Tier 1/2/3)
│   │   └── security.py        # SSRF protection & safe fetching
│   ├── requirements.txt       # Production dependencies (fastapi, uvicorn, pydantic)
│   ├── requirements-dev.txt   # Dev dependencies (pytest, httpx)
│   └── tests/                 # Deterministic offline test suite (255+ tests)
│
└── extension/                 # Chrome Browser Extension (Manifest V3)
    ├── manifest.json          # MV3 configuration
    ├── popup.html             # Compact newspaper-theme popup card
    ├── popup.js               # Popup rendering & star rating logic
    ├── content.js             # In-page Shadow DOM banner & SPA navigation observer
    ├── background.js          # Service worker fetch relay
    └── icons/                 # Veritas shield logo assets
```

---

## 🛠️ Editing & Workflow Guidelines

### 1. Create a Feature Branch
Before making changes, create a clean branch:
```bash
git checkout -b feature/your-feature-name
```

### 2. Editing Backend (`backend/app/`)
- Backend runs with auto-reload (`reload=True`), so code edits immediately reload the dev server.
- Keep dependencies lightweight: stick to standard library + FastAPI.
- Preserve the evidence math invariants:
  $$\text{Evidence Score } E = 0.30M + 0.30C + 0.20F + 0.20S \quad (0 \le E \le 100)$$

### 3. Editing Extension (`extension/`)
- The extension uses a paper-white editorial newspaper aesthetic (`#FAF8F5`, serif typography, muted navy masthead).
- When modifying `extension/content.js` or `extension/popup.js`, refresh the extension at `chrome://extensions` (click the reload circle icon on Veritas) and refresh your test news tab.

### 4. Running Tests
Always verify tests pass before pushing:
```bash
03_TEST_SYSTEM.bat
# or directly:
pytest
```
All 255+ backend unit and integration tests must pass cleanly.

### 5. Committing & Pushing
```bash
git add .
git commit -m "feat: describe your change clearly"
git push origin feature/your-feature-name
```
Then open a Pull Request on GitHub to merge into `main`!

---

## 🔑 Requesting Direct Push / Collaborator Access

To push directly to this GitHub repository:
1. Ask the repository owner (**`Designer-Dash`**) to add your GitHub username as a Collaborator.
2. The owner visits:
   `https://github.com/Designer-Dash/veritas-fake-news-detector/settings/access`
3. Click **"Add people"**, type your GitHub username, and send the invite.
4. Accept the invitation email or visit the repo link directly to start pushing!
