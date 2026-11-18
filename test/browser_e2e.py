"""Live test of firefox-sigstore in Firefox or Zen via `web-ext run`.

Usage: python3 test/browser_e2e.py [path to firefox or zen binary]

Launches the browser on a throwaway profile with the extension loaded and
serves fake auth pages on a random port. Each page pings the server while open
and sends a beacon on pagehide, so the server can tell which tabs the
extension closed.
"""
import http.server
import json
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

EXT_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
BROWSER = sys.argv[1] if len(sys.argv) > 1 else "/Applications/Zen.app/Contents/MacOS/zen"
CWD = tempfile.mkdtemp(prefix="firefox-sigstore-e2e-")
RUN_SECONDS = 40

T0 = time.time()
lock = threading.Lock()
loads, pings, gone = {}, {}, {}

TITLES = {
    "/launch": "Launcher",
    "/sig": "Sigstore Authentication",
    "/cg": "Chainguard Authentication",
    "/ctl": "Control page",
}

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<body><h1>{title}</h1><p>{id}</p><script>
const id = {id_js};
setInterval(() => fetch('/ping?id=' + id).catch(() => {{}}), 400);
addEventListener('pagehide', () => navigator.sendBeacon('/gone?id=' + id));
{extra}
</script>"""


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
        now = round(time.time() - T0, 1)
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
            to = q["to"][0]
            # Navigate after the extension has had time to install.
            extra = f"setTimeout(() => location.href = {json.dumps(to)}, 12000);"
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
    f"{L}/sig?id=sig-at-start",
    f"{L}/launch?id=launch1&to=/sig?id=sig-navigated",
    f"{L}/launch?id=launch2&to=/cg?id=cg-navigated",
    f"{IP}/launch?id=launch3&to=/sig?id=sig-on-127",
]
expect_closed = {"sig-at-start", "sig-navigated", "cg-navigated", "sig-on-127"}
expect_open = {"control"}

cmd = [
    "npx", "--yes", "web-ext@8", "run",
    "--source-dir", EXT_DIR,
    "--firefox", BROWSER,
    "--no-reload", "--no-input", "--verbose",
    "--pref", "zen.welcome-screen.seen=true",
    "--pref", "devtools.console.stdout.chrome=true",
    "--pref", "devtools.console.stdout.content=true",
]
for url in start_urls:
    cmd += ["--start-url", url]

print(f"server on port {port}", flush=True)
proc = subprocess.Popen(cmd, cwd=CWD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
out_lines = []
threading.Thread(target=lambda: out_lines.extend(proc.stdout), daemon=True).start()

time.sleep(RUN_SECONDS)
proc.send_signal(signal.SIGINT)
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
end = round(time.time() - T0, 1)
srv.shutdown()

print("\nextension console / install lines:")
for line in out_lines:
    if any(k in line for k in ("closing tab", "failed to", "Installed", "installed as a temporary", "sigstore-close", "Error", "error:")):
        print("  " + line.rstrip()[:240])

print(f"\nrun ended at t={end}s")
print(f"{'page':16} {'loaded':>7} {'last ping':>10} {'pagehide':>9}  verdict")
ok = True
for pid in sorted(expect_closed | expect_open):
    loaded = loads.get(pid)
    last = max(pings.get(pid, [None]) or [None], default=None) if pings.get(pid) else None
    hide = gone.get(pid)
    if loaded is None:
        verdict, good = "NEVER LOADED", False
    else:
        # Treat a tab as closed if it sent pagehide well before the run ended,
        # or stopped pinging more than 5s before the end.
        closed = (hide is not None and hide < RUN_SECONDS - 2) or (last is None or last < RUN_SECONDS - 5)
        want = pid in expect_closed
        verdict = "closed" if closed else "open"
        good = closed == want
        verdict += "  OK" if good else f"  FAIL (expected {'closed' if want else 'open'})"
    ok &= good
    print(f"{pid:16} {str(loaded):>7} {str(last):>10} {str(hide):>9}  {verdict}")

print("\nRESULT:", "PASS" if ok else "FAIL")
if not ok:
    print("\n--- web-ext output tail ---")
    print("".join(out_lines[-60:]))
sys.exit(0 if ok else 1)
