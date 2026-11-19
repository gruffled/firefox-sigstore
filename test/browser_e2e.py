"""Live test of firefox-sigstore in Firefox or Zen via `web-ext run`.

Usage: python3 test/browser_e2e.py [path to firefox or zen binary]

Launches the browser on a throwaway profile with the extension loaded and
serves fake auth pages on a random port. Each page pings the server while open
and sends a beacon on pagehide, so the server can tell which tabs the
extension closed. Set MOZ_HEADLESS=1 to run without a window, as CI does.
"""
import atexit
import http.server
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
BROWSER = sys.argv[1] if len(sys.argv) > 1 else "/Applications/Zen.app/Contents/MacOS/zen"
CWD = tempfile.mkdtemp(prefix="firefox-sigstore-e2e-")
# The first run may have to download web-ext before the browser starts.
INSTALL_TIMEOUT = 180
# After the install, how long to wait for every page to load and every auth
# tab to close.
SETTLE_TIMEOUT = 60
# Then how long to keep the browser open, so the pages that should stay open
# show that they do.
HOLD_SECONDS = 5
# Open pages ping at least once a second, even in background tabs. A page that
# has been silent this long is treated as closed.
QUIET_SECONDS = 3

T0 = time.time()
lock = threading.Lock()
installed = threading.Event()
loads, pings, gone = {}, {}, {}

TITLES = {
    "/launch": "Launcher",
    "/auth/callback": "Sigstore Authentication",
    "/callback": "Chainguard Authentication",
    "/other": "Sigstore Authentication",
    "/ctl": "Control page",
}

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<body><h1>{title}</h1><p>{id}</p><script>
const id = {id_js};
setInterval(() => fetch('/ping?id=' + id).catch(() => {{}}), 400);
addEventListener('pagehide', () => navigator.sendBeacon('/gone?id=' + id));
{extra}
</script>"""

# Launch pages wait until the extension is installed and then navigate, so
# the extension sees the auth page arrive through tabs.onUpdated.
NAVIGATE_WHEN_READY = """let going = false;
const ready = setInterval(() => fetch('/ready').then((r) => {{
  if (!r.ok || going) return;
  going = true;
  clearInterval(ready);
  setTimeout(() => {{ location.href = {to_js}; }}, 2000);
}}).catch(() => {{}}), 500);"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        pid = q.get("id", ["?"])[0]
        now = time.time() - T0
        if u.path == "/ready":
            return self._reply(204 if installed.is_set() else 503)
        with lock:
            if u.path == "/ping":
                pings.setdefault(pid, []).append(now)
                return self._reply(204)
            if u.path == "/gone":
                gone[pid] = now
                return self._reply(204)
        title = TITLES.get(u.path)
        if not title:
            return self._reply(404)
        with lock:
            loads.setdefault(pid, now)
        extra = ""
        if u.path == "/launch":
            extra = NAVIGATE_WHEN_READY.format(to_js=json.dumps(q["to"][0]))
        body = PAGE.format(title=title, id=pid, id_js=json.dumps(pid), extra=extra)
        self._reply(200, body.encode(), "text/html; charset=utf-8")

    do_GET = _handle
    do_POST = _handle


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A tab closing mid-request is the expected outcome, not an error.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


srv = Server(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

L, IP = f"http://localhost:{port}", f"http://127.0.0.1:{port}"
start_urls = [
    f"{L}/ctl?id=control",
    f"{L}/auth/callback?id=sig-at-start",
    f"{L}/other?id=sig-other-path",
    f"{L}/launch?id=launch1&to=/auth/callback?id=sig-navigated",
    f"{L}/launch?id=launch2&to=/callback?id=cg-navigated",
    f"{IP}/launch?id=launch3&to=/auth/callback?id=sig-on-127",
]
expect_closed = {"sig-at-start", "sig-navigated", "cg-navigated", "sig-on-127"}
expect_open = {"control", "sig-other-path"}

cmd = [
    str(ROOT / "scripts" / "web-ext.sh"), "run",
    "--source-dir", str(ROOT),
    "--firefox", BROWSER,
    "--no-reload", "--no-input", "--verbose",
    "--pref", "zen.welcome-screen.seen=true",
    "--pref", "devtools.console.stdout.chrome=true",
    "--pref", "devtools.console.stdout.content=true",
]
for url in start_urls:
    cmd += ["--start-url", url]

print(f"server on port {port}", flush=True)
# web-ext and the browser it starts share a new process group, so the whole
# group can be stopped even if web-ext exits without closing the browser.
proc = subprocess.Popen(
    cmd, cwd=CWD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
)


def _signal_group(sig):
    try:
        os.killpg(proc.pid, sig)
        return True
    except ProcessLookupError:
        return False


def stop_browser():
    if proc.poll() is None:
        _signal_group(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    if _signal_group(signal.SIGTERM):
        for _ in range(10):
            time.sleep(0.5)
            if not _signal_group(0):
                return
        _signal_group(signal.SIGKILL)


atexit.register(stop_browser)
out_lines = []


def read_output():
    for line in proc.stdout:
        out_lines.append(line)
        if "as a temporary add-on" in line:
            installed.set()


threading.Thread(target=read_output, daemon=True).start()


def elapsed():
    return time.time() - T0


def fmt(t):
    return "None" if t is None else f"{t:.1f}"


def closed_by(pid, t):
    """Whether the page had closed by time t: it sent pagehide, or went quiet.

    Call with the lock held.
    """
    hide = gone.get(pid)
    if hide is not None and hide <= t:
        return True
    seen = [p for p in pings.get(pid, []) if p <= t]
    ref = max(seen) if seen else loads.get(pid)
    return ref is not None and t - ref > QUIET_SECONDS


def settled():
    t = elapsed()
    with lock:
        return all(pid in loads for pid in expect_closed | expect_open) and all(
            closed_by(pid, t) for pid in expect_closed
        )


def wait_until(done, timeout):
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        if done():
            return True
        time.sleep(0.25)
    return False


if wait_until(installed.is_set, INSTALL_TIMEOUT):
    print(f"extension installed at t={fmt(elapsed())}s", flush=True)
    if wait_until(settled, SETTLE_TIMEOUT):
        print(f"all pages loaded and auth tabs closed at t={fmt(elapsed())}s", flush=True)
        time.sleep(HOLD_SECONDS)
    else:
        print(f"gave up waiting for pages at t={fmt(elapsed())}s", flush=True)
stop = elapsed()

stop_browser()
srv.shutdown()

if not installed.is_set():
    print(f"\nRESULT: FAIL (extension not installed by t={fmt(stop)}s)")
    print("\n--- web-ext output tail ---")
    print("".join(out_lines[-60:]))
    sys.exit(1)

print("\nextension console / install lines:")
for line in out_lines:
    if any(k in line for k in ("closing auth tab", "failed to", "as a temporary add-on")):
        print("  " + line.rstrip()[:240])

print(f"\nstopped the browser at t={fmt(stop)}s")
print(f"{'page':16} {'loaded':>7} {'last ping':>10} {'pagehide':>9}  verdict")
ok = True
for pid in sorted(expect_closed | expect_open):
    loaded = loads.get(pid)
    last = max(pings[pid]) if pings.get(pid) else None
    hide = gone.get(pid)
    if loaded is None:
        verdict, good = "NEVER LOADED", False
    else:
        # Every page gets pagehide when the browser quits, so judge each one
        # as it was at `stop`, just before the browser was told to quit.
        closed = closed_by(pid, stop)
        want = pid in expect_closed
        verdict = "closed" if closed else "open"
        good = closed == want
        verdict += "  OK" if good else f"  FAIL (expected {'closed' if want else 'open'})"
    ok &= good
    print(f"{pid:16} {fmt(loaded):>7} {fmt(last):>10} {fmt(hide):>9}  {verdict}")

print("\nRESULT:", "PASS" if ok else "FAIL")
if not ok:
    print("\n--- web-ext output tail ---")
    print("".join(out_lines[-60:]))
sys.exit(0 if ok else 1)
