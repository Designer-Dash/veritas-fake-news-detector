"""Manage Veritas API keys: create / list / revoke / verify.

Writes only SHA-256 hashes to ``data/api_keys.json`` (mode 0600).
Plaintext keys are shown once at creation and never stored.

Usage (from backend/):
    python tools/manage_keys.py create --name ci-bot --rate-max 120
    python tools/manage_keys.py list
    python tools/manage_keys.py revoke --key-id k_abc12345
    python tools/manage_keys.py verify vfnd_xxx   # dev helper only
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import generate_key, hash_key  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "api_keys.json"


def _load() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"keys": []}


def _save(db: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps(db, indent=2), encoding="utf-8")
    try:
        os.chmod(DB_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # best-effort on platforms without POSIX perms


def main() -> int:
    parser = argparse.ArgumentParser(description="Veritas API key manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a new key")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--rate-max", type=int, default=None,
                          help="per-key requests-per-minute override")

    sub.add_parser("list", help="list key ids (no secrets)")

    p_revoke = sub.add_parser("revoke", help="revoke a key by id")
    p_revoke.add_argument("--key-id", required=True)

    p_verify = sub.add_parser("verify", help="verify a plaintext key (dev helper)")
    p_verify.add_argument("key")

    args = parser.parse_args()
    db = _load()

    if args.cmd == "create":
        key, key_id, sha = generate_key()
        db["keys"].append(
            {
                "key_id": key_id,
                "name": args.name,
                "sha256": sha,
                "rate_max": args.rate_max,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "revoked": False,
            }
        )
        _save(db)
        print(f"key_id : {key_id}")
        print(f"name   : {args.name}")
        print(f"rate   : {args.rate_max or 'default'}")
        print("KEY (shown once, store it now):")
        print(key)
        return 0

    if args.cmd == "list":
        for k in db["keys"]:
            status = "REVOKED" if k.get("revoked") else "active"
            print(f"{k['key_id']}  {status:8s} {k.get('name', ''):20s} rate={k.get('rate_max') or '-'}")
        return 0

    if args.cmd == "revoke":
        found = False
        for k in db["keys"]:
            if k["key_id"] == args.key_id:
                k["revoked"] = True
                found = True
        if found:
            _save(db)
            print(f"revoked: {args.key_id}")
            return 0
        print("key id not found")
        return 1

    if args.cmd == "verify":
        sha = hash_key(args.key)
        for k in db["keys"]:
            if k["sha256"] == sha and not k.get("revoked"):
                print(f"VALID key_id={k['key_id']}")
                return 0
        print("INVALID or revoked")
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
