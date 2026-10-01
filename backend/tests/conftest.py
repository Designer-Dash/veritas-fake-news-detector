"""Shared test isolation.

Every test runs offline and hermetically:
  * Gemini is force-disabled (VERITAS_GEMINI_ENABLED=0) so results never
    depend on an API key being present on the machine, and no test can
    reach the network through the Gemini client.
  * All TTL caches (claim / RSS / verification / gemini) are cleared before
    and after each test so no test observes another test's cached state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_verification_state(monkeypatch):
    monkeypatch.setenv("VERITAS_GEMINI_ENABLED", "0")
    from app.cache import clear_caches

    clear_caches()
    yield
    clear_caches()
