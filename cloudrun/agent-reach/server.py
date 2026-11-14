"""A small HTTP wrapper around Agent-Reach's command line, for Google Cloud Run (the platform calls it over HTTPS from Actions; contentintel/agent_reach_remote.py is the other end).

  GET  /healthz   open: what is installed and which channels have cookies (names only, never a value)
  POST /run       needs the X-Agent-Reach-Token header: {"channel", "action", "values", "timeout"} -> the JSON envelope Agent-Reach prints ({"ok", "items", "error"})

Only the seven OpenCLI channels and only the actions the catalogue declares are run, with only the inputs it declares; an input that starts with '-' is refused so it can never become an option. One call runs at a time, with the
gaps and breaks of a person (human.py), because the account behind it must not look like a script, and Cloud Run is on Google data-centre addresses. The token and the cookies are never passed to the command, never logged and never returned.
Standard library only; the catalogue and human.py are copied beside this file when the image is built."""
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent_reach_catalog as CAT
from human import Human

OPENCLI_CHANNELS = tuple(sorted(name for name, spec in CAT.CHANNELS.items() if spec["integration"] == "opencli"))
OPTIONAL = ("limit", "location", "city", "lang")             # taken as --name value; every other input is given in order (the same rule as the platform's own runner)
MAX_BODY, MAX_VALUE, MAX_OUTPUT, MAX_ITEMS = 8192, 300, 2_000_000, 200
DEFAULT_TIMEOUT, LONGEST_TIMEOUT = 90, 240
FAILED_LIMIT, FAILED_WINDOW = 10, 300                       # this many wrong tokens in this many seconds and every call waits the window out
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
PACE = {"break_every_pages": [8, 14], "break_seconds": [30, 90]}
GAP = (6.0, 3.0, 25.0)                                      # the usual gap between two runs in seconds (median, shortest, longest)


def build_command(binary, channel, action, values, timeout):
    """`agent-reach <channel> <action> <inputs> --timeout N`, exactly as the platform's own runner builds it (a test holds the two equal for every declared action)."""
    declared = CAT.DECLARED[channel][action]
    cmd = [binary, channel, action]
    cmd += [str(values[name]) for name in declared if name not in OPTIONAL and values.get(name)]
    for name in declared:
        if name in OPTIONAL and values.get(name):
            cmd += [f"--{name}", str(values[name])]
    return cmd + ["--timeout", str(timeout)]


def check_request(body):
    """(channel, action, values, timeout) from a request body, or raises ValueError with a plain reason."""
    if not isinstance(body, dict):
        raise ValueError("the request must be a JSON object")
    channel, action = body.get("channel"), body.get("action")
    if channel not in OPENCLI_CHANNELS:
        raise ValueError(f"channel must be one of: {', '.join(OPENCLI_CHANNELS)}")
    declared = CAT.DECLARED.get(channel, {}).get(action)
    if declared is None:
        raise ValueError(f"{channel} has no {action!r} action")
    raw = body.get("values") or {}
    if not isinstance(raw, dict) or set(raw) - set(declared):
        raise ValueError("values may hold only the inputs this action declares: " + ", ".join(declared))
    values = {}
    for name, value in raw.items():
        text = str(value)
        if isinstance(value, (dict, list)) or len(text) > MAX_VALUE or any(c in text for c in "\x00\r\n"):
            raise ValueError(f"the value of {name} is not usable (too long, or it holds a line break)")
        if text.startswith("-") and name not in OPTIONAL:
            raise ValueError(f"the value of {name} may not start with '-'")
        if name in OPTIONAL and name == "limit" and not text.isdigit():
            raise ValueError("limit must be a whole number")
        if text:
            values[name] = text
    timeout = body.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= LONGEST_TIMEOUT:
        raise ValueError(f"timeout must be a number from 5 to {LONGEST_TIMEOUT} seconds")
    return channel, action, values, int(timeout)


def redact(text, secrets):
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[hidden]")
    return text


class Service:
    """The service without a network socket, so it is tested without one."""

    def __init__(self, token, *, binary="agent-reach", runner=subprocess.run, human=None, sleep=time.sleep, clock=time.monotonic, status=None, environ=None):
        if len(str(token or "")) < 24:
            raise ValueError("AGENT_REACH_TOKEN must be set, at least 24 characters")
        self.token, self.binary, self.runner, self.sleep, self.clock = token, binary, runner, sleep, clock
        self.human = human if human is not None else Human(PACE, sleep=sleep)           # the gaps and the breaks sleep through the same function
        self.status = status or (lambda: {})
        self.environ = dict(os.environ if environ is None else environ)
        self.lock = threading.Lock()
        self.failed, self.blocked_until, self.ran = [], 0.0, 0

    def _auth(self, headers):
        got = ""
        for key, value in headers.items():
            if key.lower() == "x-agent-reach-token":
                got = str(value or "")
        now = self.clock()
        self.failed = [t for t in self.failed if now - t < FAILED_WINDOW]
        if now < self.blocked_until:
            return 429
        if hmac.compare_digest(got.encode(), self.token.encode()):
            return 0
        self.failed.append(now)
        if len(self.failed) >= FAILED_LIMIT:
            self.blocked_until = now + FAILED_WINDOW
        return 401

    def handle(self, method, path, headers, body):
        """(status, dict). `body` is the raw request body (bytes)."""
        path = path.split("?")[0]
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True, "channels": list(OPENCLI_CHANNELS), **self.status()}
        if method != "POST" or path != "/run":
            return 404, {"ok": False, "error": {"code": "BAD_INPUT", "message": "unknown address"}}
        denied = self._auth(headers)
        if denied:
            return denied, {"ok": False, "error": {"code": "RATE_LIMITED" if denied == 429 else "NEED_SETUP", "message": "too many wrong tokens, wait" if denied == 429 else "the token is missing or wrong"}}
        if len(body or b"") > MAX_BODY:
            return 413, {"ok": False, "error": {"code": "BAD_INPUT", "message": "the request is too large"}}
        try:
            channel, action, values, timeout = check_request(json.loads((body or b"{}").decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            return 400, {"ok": False, "error": {"code": "BAD_INPUT", "message": str(exc)[:200]}}
        with self.lock:                                           # one call at a time: one person, one account
            if self.ran:
                self.sleep(self.human.reading_time(GAP[0], GAP[1], GAP[2]))
            self.human.count_page()                                   # and a longer break after every few runs
            self.ran += 1
            return 200, self._run(channel, action, values, timeout)

    def _run(self, channel, action, values, timeout):
        cmd = build_command(self.binary, channel, action, values, timeout)
        env = {k: self.environ[k] for k in ENV_KEEP if k in self.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            done = self.runner(cmd, env=env, capture_output=True, text=True, timeout=timeout + 30)
        except FileNotFoundError:
            return {"ok": False, "items": [], "error": {"code": "NEED_SETUP", "message": "Agent-Reach is not installed in this container"}}
        except subprocess.TimeoutExpired:
            return {"ok": False, "items": [], "error": {"code": "TIMEOUT", "message": f"Agent-Reach did not answer within {timeout + 30} seconds"}}
        out = (done.stdout or "")[:MAX_OUTPUT]
        lines = [line for line in out.splitlines() if line.strip().startswith("{")]
        try:
            envelope = json.loads(lines[-1])
        except (IndexError, ValueError):
            tail = redact(" ".join((done.stderr or done.stdout or "").split())[-200:], [self.token])
            return {"ok": False, "items": [], "error": {"code": "UPSTREAM_BROKEN", "message": f"Agent-Reach gave no readable result ({tail or 'no output'})"}}
        if not isinstance(envelope, dict):
            return {"ok": False, "items": [], "error": {"code": "UPSTREAM_BROKEN", "message": "Agent-Reach's result was not an object"}}
        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            return {"ok": False, "items": [], "error": {"code": str(err.get("code") or "UPSTREAM_BROKEN"), "message": redact(str(err.get("message") or "Agent-Reach reported an error")[:300], [self.token])}}
        return {"ok": True, "items": list(envelope.get("items") or [])[:MAX_ITEMS], "error": None}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-reach-wrapper"

        def _reply(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(*service.handle("GET", self.path, dict(self.headers), b""))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY + 1)) if length else b""
            self._reply(*service.handle("POST", self.path, dict(self.headers), body))

        def log_message(self, fmt, *args):                          # one short line, never a header or a body
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))
    return Handler


def main():
    import cookies
    state = cookies.seed_all(os.environ.get("AGENT_REACH_SECRETS_DIR", "/secrets"), os.environ.get("AGENT_REACH_PROFILE", os.path.expanduser("~/.agent-reach/browser/profile")), OPENCLI_CHANNELS)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap.json"), encoding="utf-8") as handle:
            bootstrap = json.load(handle)
    except (OSError, ValueError):
        bootstrap = {}
    service = Service(os.environ.get("AGENT_REACH_TOKEN", ""), status=lambda: {"cookies": state, "bootstrap": bootstrap})
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(service)).serve_forever()


if __name__ == "__main__":
    main()
