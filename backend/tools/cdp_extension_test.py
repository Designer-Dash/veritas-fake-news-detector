"""CDP harness: run the real unpacked extension against a live article.

Launches headless Chromium (Edge build) with --load-extension, opens the
The Hindu article, waits for the content script -> service worker -> backend
round trip, and reports console output + banner state.

Usage: python backend/tools/cdp_extension_test.py [url]
"""

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

import websocket

# Resolve the extension dir relative to this file: backend/tools -> project root.
EXT = str((pathlib.Path(__file__).resolve().parents[2] / "extension"))
# Branded Chrome/Edge Stable 137+ ignore --load-extension; Chrome for Testing keeps it.
EDGE = r"C:\Users\Lenovo\AppData\Local\Temp\chrome-for-testing\extracted\chrome-win64\chrome.exe"
PROFILE = r"C:\Users\Lenovo\AppData\Local\Temp\veritas-cdp-profile"
DEBUG_PORT = 9223
DEFAULT_URL = (
    "https://www.thehindu.com/news/national/"
    "india-bloc-meeting-today-who-are-attending-rahul-mamata-kharge-full-list-of-leaders/"
    "article71526950.ece"
)


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def kill_stale_headless() -> None:
    """Kill only processes listening on our debug port (never the user's browser)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        ).stdout
        pids = {
            line.split()[-1]
            for line in out.splitlines()
            if f":{DEBUG_PORT}" in line and "LISTENING" in line.upper()
        }
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], capture_output=True)
    except Exception:
        pass


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    kill_stale_headless()
    time.sleep(1)

    print(f"extension dir: {EXT} (exists={pathlib.Path(EXT).exists()})")
    proc = subprocess.Popen(
        [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            "--enable-unsafe-extension-debugging",  # branded Chromium 137+: required for --load-extension
            f"--user-data-dir={PROFILE}",
            f"--disable-extensions-except={EXT}",
            f"--load-extension={EXT}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for the DevTools endpoint.
        ws_url = None
        for _ in range(40):
            try:
                targets = http_json(f"http://127.0.0.1:{DEBUG_PORT}/json/list")
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            print("FAIL: DevTools endpoint never came up")
            return 2

        ws = websocket.create_connection(ws_url, timeout=10)
        msg_id = 0

        # Show every target so we can see whether the extension loaded
        # (a chrome-extension:// service_worker target must exist).
        try:
            targets = http_json(f"http://127.0.0.1:{DEBUG_PORT}/json/list")
            print("targets:", [f"{t.get('type')}:{t.get('url','')[:60]}" for t in targets])
        except Exception:
            pass

        def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            return msg_id

        logs = []

        def drain(timeout=1.0, want_id=None):
            ws.settimeout(timeout)
            try:
                while True:
                    data = json.loads(ws.recv())
                    if data.get("method") == "Runtime.consoleAPICalled":
                        args = [
                            a.get("value", a.get("description", ""))
                            for a in data["params"].get("args", [])
                        ]
                        logs.append(
                            (data["params"].get("type", "log"), " | ".join(str(a) for a in args))
                        )
                    if want_id and data.get("id") == want_id:
                        return data
            except websocket.WebSocketTimeoutException:
                return None

        send("Runtime.enable")
        drain(0.5)

        def evaluate(expr, timeout=25):
            rid = send(
                "Runtime.evaluate",
                {"expression": expr, "awaitPromise": True, "returnByValue": True},
            )
            resp = drain(timeout, want_id=rid)
            if not resp:
                return None
            return resp.get("result", {}).get("result", {}).get("value")

        evaluate(f"location.href = {json.dumps(url)}; 'navigating'")
        time.sleep(6)
        drain(0.5)

        banner = None
        for i in range(20):
            time.sleep(2)
            banner = evaluate(
                "(() => { const b = document.getElementById('veritas-banner');"
                " return b ? { text: b.innerText.slice(0, 400) } : null; })()"
            )
            if banner:
                break

        print("=== result ===")
        print(
            "banner:",
            json.dumps(banner, indent=2, ensure_ascii=False) if banner else "NOT FOUND",
        )

        # Screenshot proof of the rendered banner.
        if banner:
            send("Page.enable")
            drain(0.3)
            rid = send("Page.captureScreenshot", {"format": "png"})
            resp = drain(10, want_id=rid)
            if resp and resp.get("result", {}).get("data"):
                import base64

                out = pathlib.Path(__file__).resolve().parents[2] / "banner-live.png"
                out.write_bytes(base64.b64decode(resp["result"]["data"]))
                print(f"screenshot saved: {out}")

        print(f"=== console logs captured ({len(logs)}) ===")
        for kind, text in logs[-12:]:
            print(f"[{kind}] {text[:160]}")
        return 0 if banner else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid), "/T"], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
