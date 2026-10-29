"""Veritas Fake News Detector — Server Runner Script

Runs the FastAPI backend server on http://localhost:8000.
By default, sets VERITAS_AUTH_DISABLED=1 for development mode
so the Chrome Extension works out of the box.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add project root and backend folder to Python path
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Ensure console output uses UTF-8 to prevent Windows cp1252 encoding errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Default environment variables for dev mode
os.environ.setdefault("VERITAS_AUTH_DISABLED", "1")


def main():
    print("=" * 60)
    print(" 🛡️  Veritas — Fake & AI-Generated News Detector Server")
    print("=" * 60)
    print(" Starting backend API server...")
    print(f" Root Directory:    {ROOT_DIR}")
    print(f" Backend Directory: {BACKEND_DIR}")
    print(f" Auth Mode:         {'DISABLED (Dev Mode)' if os.environ.get('VERITAS_AUTH_DISABLED') == '1' else 'ENABLED'}")
    print(" Server URL:        http://127.0.0.1:8000")
    print(" Health Check:      http://127.0.0.1:8000/health")
    print(" Press Ctrl+C to stop the server.")
    print("=" * 60)
    print()

    try:
        import uvicorn
    except ImportError:
        print("\n❌ Error: 'uvicorn' module is not installed.")
        print("Please install requirements using:")
        print("    pip install -r backend/requirements.txt")
        sys.exit(1)

    try:
        uvicorn.run(
            "backend.app.main:app",
            host="127.0.0.1",
            port=8000,
            reload=True,
            reload_dirs=[str(BACKEND_DIR)],
        )
    except KeyboardInterrupt:
        print("\n🛑 Veritas server stopped by user.")
    except Exception as e:
        print(f"\n❌ Error starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
