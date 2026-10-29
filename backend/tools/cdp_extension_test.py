"""CDP harness: run the real unpacked extension against a live article.

Launches headless Chromium (Edge build) with --load-extension, opens the
The Hindu article, waits for the content script -> service worker -> backend
round trip, and reports console output + banner state.

Usage:
    python backend/tools/cdp_extension_test.py                # default article
    python backend/tools/cdp_extension_test.py <article-url>  # custom article
    python backend/tools/cdp_extension_test.py --help         # does NOT run anything

Requires the optional dev-only dependency ``websocket-client`` (not part of
requirements.txt -- the runtime backend never needs it) and a Chrome-for-
Testing/Edge binary; set VERITAS_CHROME_PATH to override the browser path.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

try:
    import websocket
except ImportError:  # pragma: no cover - environment problem, not a code path
    websocket = None

# Resolve the extension dir relative to this file: backend/tools -> project root.
EXT = str((pathlib.Path(__file__).resolve().parents[2] / "extension"))
# Branded Chrome/Edge Stable 137+ ignore --load-extension; Chrome for Testing keeps it.
EDGE = os.environ.get(
    "VERITAS_CHROME_PATH",
    r"C:\Users\Lenovo\AppData\Local\Temp\chrome-for-testing\extracted\chrome-win64\chrome.exe",
)
PROFILE = os.environ.get(
    "VERITAS_CDP_PROFILE",
    r"C:\Users\Lenovo\AppData\Local\Temp\veritas-cdp-profile",
)
DEBUG_PORT = int(os.environ.get("VERITAS_CDP_PORT", "9223"))
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
    ap = argparse.ArgumentParser(description="CDP harness: real extension vs live article")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="article URL to analyze")
    args = ap.parse_args()
    if websocket is None:
        print(
            "cdp_extension_test: missing optional dev dependency 'websocket-client'.\n"
            "Install it with: pip install websocket-client",
            file=sys.stderr,
        )
        return 2
    url = args.url
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

        # Optional: delay /analyze (Fetch interception) so the Phase-1
        # shimmer state is photographable despite a fast local backend.
        delay = float(os.environ.get("VERITAS_DELAY_ANALYZE", "0"))
        if delay > 0:
            send("Fetch.enable", {"patterns": [{"urlPattern": "*localhost:8000/analyze*"}]})
            drain(0.3)

            def hold_requests():
                """Hold intercepted /analyze requests for `delay` seconds."""
                ws.settimeout(2.0)
                held = []
                deadline = time.time() + delay
                while time.time() < deadline:
                    try:
                        data = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    if data.get("method") == "Fetch.requestPaused":
                        held.append(data["params"]["requestId"])
                for rid_ in held:
                    ws.send(json.dumps({"id": 900000 + int(rid_[-3:], 16) % 100000, "method": "Fetch.continueRequest", "params": {"requestId": rid_}}))
                ws.settimeout(10)
                return len(held)

            n_held = hold_requests()
            print(f"interception: held {n_held} /analyze request(s) for {delay}s")

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
        time.sleep(2.5)  # catch the Phase-1 analyzing state before the verdict lands
        drain(0.5)

        # Immediate capture: Phase 1 (shimmer) if we're quick enough.
        early = evaluate(
            "(() => { const h = document.getElementById('veritas-banner-host');"
            " return h ? 'present' : null; })()"
        )
        if early:
            send("Page.enable")
            drain(0.3)
            rid = send("Page.captureScreenshot", {"format": "png"})
            resp = drain(10, want_id=rid)
            if resp and resp.get("result", {}).get("data"):
                import base64

                out = pathlib.Path(__file__).resolve().parents[2] / "banner-analyzing.png"
                out.write_bytes(base64.b64decode(resp["result"]["data"]))
                print(f"analyzing-state screenshot: {out}")
        time.sleep(4)

        banner = None
        analyzing_seen = None
        for i in range(30):
            time.sleep(1)
            # The card lives in a CLOSED shadow root (page JS must not reach
            # it), so light-DOM detection is the host element + its geometry;
            # textual proof comes from the screenshot below.
            banner = evaluate(
                "(() => { const h = document.getElementById('veritas-banner-host');"
                " if (!h) return null; const r = h.getBoundingClientRect();"
                " return { rendered: r.width > 0 && r.height > 0, w: Math.round(r.width), h: Math.round(r.height),"
                " x: Math.round(r.x), y: Math.round(r.y) }; })()"
            )
            if banner and analyzing_seen is None:
                analyzing_seen = True
                rid = send("Page.captureScreenshot", {"format": "png"})
                resp = drain(10, want_id=rid)
                if resp and resp.get("result", {}).get("data"):
                    import base64

                    out = pathlib.Path(__file__).resolve().parents[2] / "banner-phase2.png"
                    out.write_bytes(base64.b64decode(resp["result"]["data"]))
                    print(f"phase2 screenshot: {out}")
            if banner:
                # Verdict has applied once the footer no longer says Checking…
                footer = evaluate(
                    "'ok'"  # geometry probe only; footer proof is the second screenshot
                )
                break

        print("=== result ===")
        print(
            "banner:",
            json.dumps(banner, indent=2, ensure_ascii=False) if banner else "NOT FOUND",
        )

        # Interaction contract: real mouse clicks via CDP Input domain.
        # 1) click pill -> expands (card ~288px wide replaces the pill)
        # 2) click far outside -> collapses back to pill
        # 3) re-expand, click the dismiss button -> host element removed
        if banner:
            def host_rect():
                return evaluate(
                    "(() => { const h = document.getElementById('veritas-banner-host');"
                    " if (!h) return null; const r = h.getBoundingClientRect();"
                    " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()"
                )

            def click_at(x, y):
                for t in ("mousePressed", "mouseReleased"):
                    send("Input.dispatchMouseEvent", {
                        "type": t, "x": int(x), "y": int(y),
                        "button": "left", "clickCount": 1,
                    })
                    drain(0.3)

            r1 = host_rect()
            if r1:
                click_at(r1["x"] + r1["w"] / 2, r1["y"] + r1["h"] / 2)
                r2 = host_rect()
                # Card is a fixed ~290px; pills vary (150-250px), so 1.3x is
                # the reliable expand signal.
                expanded = bool(r2 and r2["w"] > r1["w"] * 1.3)
                print(f"expand-on-click: {expanded} (pill {r1['w']:.0f}px -> {r2['w']:.0f}px)")

                # screenshot of the expanded card
                rid = send("Page.captureScreenshot", {"format": "png"})
                resp = drain(10, want_id=rid)
                if resp and resp.get("result", {}).get("data"):
                    import base64
                    out = pathlib.Path(__file__).resolve().parents[2] / "banner-expanded.png"
                    out.write_bytes(base64.b64decode(resp["result"]["data"]))
                    print(f"expanded screenshot: {out}")

                # outside click -> collapse
                click_at(200, 400)
                r3 = host_rect()
                collapsed = bool(r3 and r3["w"] <= r1["w"] * 1.3)
                print(f"outside-click-collapses: {collapsed} ({r3['w']:.0f}px)")

                # re-expand, then dismiss via the × (bottom-right of card)
                click_at(r3["x"] + r3["w"] / 2, r3["y"] + r3["h"] / 2)
                r4 = host_rect()
                if r4 and r4["w"] > r1["w"] * 1.3:
                    click_at(r4["x"] + r4["w"] - 16, r4["y"] + r4["h"] - 14)
                    gone = evaluate("document.getElementById('veritas-banner-host') === null")
                    print(f"dismiss-removes: {gone}")

        # Screenshot proof of the settled (verdict-applied) card.
        if banner:
            time.sleep(4)  # allow backend round trip to complete
            send("Page.enable")
            drain(0.3)
            rid = send("Page.captureScreenshot", {"format": "png"})
            resp = drain(10, want_id=rid)
            if resp and resp.get("result", {}).get("data"):
                import base64

                out = pathlib.Path(__file__).resolve().parents[2] / "banner-live.png"
                out.write_bytes(base64.b64decode(resp["result"]["data"]))
                print(f"verdict screenshot saved: {out}")

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
