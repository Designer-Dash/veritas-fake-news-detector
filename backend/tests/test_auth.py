"""Authentication & authorization tests: fail-closed 401s, hashed storage,
per-user rate limits, revocation, malformed-registry resilience."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import AuthContext, KeyRegistry, auth_enabled, generate_key, hash_key  # noqa: E402


@pytest.fixture()
def key_db(tmp_path):
    return tmp_path / "api_keys.json"


def _write_db(path, keys):
    path.write_text(json.dumps({"keys": keys}), encoding="utf-8")


def test_generate_key_shape_and_hash_roundtrip():
    key, key_id, sha = generate_key()
    assert key.startswith("vfnd_")
    assert len(key) > 40
    assert sha == hash_key(key)
    assert key_id.startswith("k_")


def test_verify_valid_key_and_reject_bad(monkeypatch, key_db):
    key, key_id, sha = generate_key()
    _write_db(key_db, [{"key_id": key_id, "sha256": sha, "revoked": False}])
    monkeypatch.setenv("VERITAS_API_KEYS", "")  # env off

    reg = KeyRegistry(file_path=str(key_db))
    ctx = reg.verify(key)
    assert ctx is not None and ctx.key_id == key_id
    assert reg.verify("vfnd_" + "A" * 43) is None
    assert reg.verify("") is None
    assert reg.verify("not-a-key") is None
    assert reg.verify(key + "x") is None  # tampered suffix


def test_plaintext_never_written_to_disk(key_db):
    key, key_id, sha = generate_key()
    _write_db(key_db, [{"key_id": key_id, "sha256": sha, "revoked": False}])
    contents = key_db.read_text(encoding="utf-8")
    assert key not in contents          # only the hash may be stored
    assert sha in contents
    assert key_id in contents


def test_revocation_takes_effect_via_hot_reload(monkeypatch, key_db):
    key, key_id, sha = generate_key()
    _write_db(key_db, [{"key_id": key_id, "sha256": sha, "revoked": False}])
    reg = KeyRegistry(file_path=str(key_db))
    assert reg.verify(key) is not None

    import os, time

    _write_db(key_db, [{"key_id": key_id, "sha256": sha, "revoked": True}])
    os.utime(key_db, None)  # ensure mtime changes
    assert reg.verify(key) is None  # revoked without restart


def test_malformed_registry_fails_closed(monkeypatch, key_db):
    key_db.write_text("{not json at all", encoding="utf-8")
    reg = KeyRegistry(file_path=str(key_db))
    assert reg.verify("vfnd_" + "B" * 43) is None


def test_env_keys_parse_and_merge(monkeypatch):
    key, key_id, sha = generate_key()
    monkeypatch.setenv("VERITAS_API_KEYS", f"{key_id}:{sha}:120")
    reg = KeyRegistry(file_path=tempfile.gettempdir() + "/definitely-missing-registry.json")
    ctx = reg.verify(key)
    assert ctx is not None
    assert ctx.source == "env"
    assert ctx.rate_max == 120


def test_env_malformed_entries_skipped(monkeypatch):
    monkeypatch.setenv("VERITAS_API_KEYS", "garbage,k2:" + "a" * 64 + ":notanint,ok1:" + "b" * 64 + ":30")
    reg = KeyRegistry(file_path=tempfile.gettempdir() + "/definitely-missing-registry.json")
    assert reg._by_hash.get("b" * 64) is not None
    assert reg._by_hash.get("a" * 64) is None  # bad rate dropped
    assert len(reg._by_hash) == 1


def test_auth_disabled_flag():
    os.environ.pop("VERITAS_AUTH_DISABLED", None)
    assert auth_enabled() is True
    os.environ["VERITAS_AUTH_DISABLED"] = "1"
    assert auth_enabled() is False
    os.environ.pop("VERITAS_AUTH_DISABLED", None)


# --------------------------------------------------------------------------
# API-level auth tests
# --------------------------------------------------------------------------

@pytest.fixture()
def auth_client(monkeypatch, key_db):
    """TestClient with auth enabled and one known key registered."""
    key, key_id, sha = generate_key()
    _write_db(key_db, [{"key_id": key_id, "sha256": sha, "rate_max": 5, "revoked": False}])
    monkeypatch.setenv("VERITAS_API_KEYS", "")
    monkeypatch.setenv("VERITAS_AUTH_DISABLED", "")  # ensure auth ON
    monkeypatch.setattr("app.main.get_registry", lambda: KeyRegistry(file_path=str(key_db)))

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c, key, key_id


def _mock_crosscheck_off(monkeypatch):
    import app.pipeline as pl

    monkeypatch.setattr(pl, "safe_fetch", lambda url, **k: None)


def test_analyze_requires_auth(auth_client, monkeypatch):
    client, _, _ = auth_client
    _mock_crosscheck_off(monkeypatch)
    resp = client.post("/analyze", json={"url": "https://example.com/a", "include_cross_check": False})
    assert resp.status_code == 401


def test_analyze_rejects_bad_key(auth_client, monkeypatch):
    client, _, _ = auth_client
    _mock_crosscheck_off(monkeypatch)
    resp = client.post(
        "/analyze",
        json={"url": "https://example.com/a", "include_cross_check": False},
        headers={"Authorization": "Bearer vfnd_" + "Z" * 43},
    )
    assert resp.status_code == 401
    assert "vfnd_" not in resp.text  # key never echoed


def test_analyze_accepts_valid_key(auth_client, monkeypatch):
    client, key, _ = auth_client
    _mock_crosscheck_off(monkeypatch)
    resp = client.post(
        "/analyze",
        json={"url": "https://example.com/a", "include_cross_check": False},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] in ("trusted", "uncertain", "suspicious", "fake")


def test_per_key_rate_limit_enforced(auth_client, monkeypatch):
    client, key, _ = auth_client
    _mock_crosscheck_off(monkeypatch)
    headers = {"Authorization": f"Bearer {key}"}
    codes = []
    for _ in range(8):  # key registered with rate_max=5
        r = client.post(
            "/analyze",
            json={"url": "https://example.com/a", "include_cross_check": False},
            headers=headers,
        )
        codes.append(r.status_code)
    assert codes.count(429) >= 1
    assert codes[:5] == [200, 200, 200, 200, 200]


def test_health_is_public(auth_client):
    client, _, _ = auth_client
    assert client.get("/health").status_code == 200
