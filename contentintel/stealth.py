"""Stealth / human-behaviour layer for the sessions that are logged in with an account.

The main session reads (``contentintel/browser.py``) used to run on plain Playwright
headless Chromium with one fixed user agent and one fixed viewport, no proxy and
instant form fills — the shape of an automated read, and the shape an account
warning names. This module gives those reads a human shape instead:

* a stealth browser for session reads — Camoufox first (its fingerprint is drawn
  by a model and its headless mode runs headed on a private X server), patchright
  headed under Xvfb next, plain Playwright last. The engine is picked once per
  process and degrades gracefully; a missing browser is never a failed run.
* one stable identity per account — a pinned user agent, viewport, timezone and
  proxy, written to ``data/_shared/stealth-identities.json`` the first time the
  account is seen and reused on every later run. The identity never rotates per
  run and never changes mid-run. A fresh login from a new device or IP never
  happens from the runner: a stealth session is cookie-only.
* a sticky proxy per account, read from a secret named in the identity
  (``STEALTH_PROXY_URL`` by default, or ``STEALTH_PROXY_<SITE>`` for one account).
  The proxy is optional — with no proxy configured the stealth browser and the
  human pacing still apply. The proxy address and any credentials in it are
  never logged, never written to a file and never appear in an error message.
* a per-read log (pages, timing, IP) and a per-read "human score", so a future
  warning can be traced back to what the account actually did.

The layer is ON by default (``browser.yaml`` ``stealth: enabled: true``) and the
owner can turn it off; with it off every read behaves exactly as it did before
this layer existed. Nothing in this module changes a workflow file, a collector
or a test that already exists.
"""

import json
import os
import random
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    # The whole layer. False restores the old behaviour (plain Playwright,
    # fixed UA/viewport, no proxy, no per-read log) for every site at once.
    "enabled": True,
    # Which engine to try first: "auto" (Camoufox, then patchright, then plain),
    # or one of "camoufox", "patchright", "plain". A missing engine is skipped,
    # never an error.
    "engine": "auto",
    # Camoufox options, passed through when that engine is used. "virtual" runs
    # the browser headed on its own private X server, so there is no headless
    # tell at all. humanize moves the mouse and types the way a person does.
    "humanize": True,
    # Camoufox geoip: derive the timezone, locale and WebRTC settings from the
    # proxy's IP, so the browser's story matches the address it comes from.
    "geoip": True,
    # Where the per-account identities live. The file holds no secret, only the
    # pinned values and the NAME of the secret that holds the proxy address.
    "identities_file": "data/_shared/stealth-identities.json",
    # Where the persistent browser profiles live, one directory per account.
    # A profile holds the cookies and the local storage, so the session stays
    # logged in between runs and never has to log in again from the runner.
    "profiles_root": ".stealth-profiles",          # NOT under data/: Collect commits all of data/, and a browser profile holds the session cookies and can be hundreds of megabytes
    # The secret that holds the proxy address, unless a site overrides it with
    # its own ``STEALTH_PROXY_<SITE>`` name. The address is read from the
    # environment at run time; it is never written to a file.
    "proxy_secret": "STEALTH_PROXY_URL",
    # Write one JSONL line per read (page, timing, IP, human score) to the
    # run's debug directory, and add one summary line to the run report.
    "log_reads": True,
    # Read the planned pages in a shuffled order (a person does not work
    # through a list in exactly the same order every time). Careful mode keeps
    # its own rotation and is never shuffled.
    "shuffle_reads": True,
}

_ENGINES = ("auto", "camoufox", "patchright", "plain")


class StealthError(Exception):
    """A stealth setting is wrong, or a stealth engine cannot be used."""


def resolve(block):
    """Check a ``stealth:`` block and fill in the defaults.

    Raises StealthError for anything unknown or out of range. ``None`` means
    "not given", which is the same as the defaults.
    """
    if block is None:
        return dict(DEFAULTS)
    if not isinstance(block, dict):
        raise StealthError("stealth: must be a mapping of settings")
    cfg = dict(DEFAULTS)
    for key, value in block.items():
        if key not in DEFAULTS:
            raise StealthError(f"stealth: unknown setting {key!r}")
        cfg[key] = value
    for key in ("enabled", "humanize", "geoip", "log_reads", "shuffle_reads"):
        if not isinstance(cfg[key], bool):
            raise StealthError(f"stealth: {key} must be true or false")
    if cfg["engine"] not in _ENGINES:
        raise StealthError(f"stealth: engine must be one of {', '.join(_ENGINES)}")
    for key in ("identities_file", "profiles_root", "proxy_secret"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise StealthError(f"stealth: {key} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg["proxy_secret"]):
        raise StealthError("stealth: proxy_secret must be a valid environment variable name")
    return cfg


# ---------------------------------------------------------------------------
# One stable identity per account
# ---------------------------------------------------------------------------

# Real browser strings, current at the time of writing. The pool is small on
# purpose: an account is pinned to ONE of these and keeps it, so the account
# always looks like the same machine. The pool is only used when a new
# identity is drawn; a drawn identity is never re-drawn.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
]

_VIEWPORTS = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1280, 720), (2560, 1440)]

# Timezone and locale are drawn together, so the account's clock and language
# always agree with each other. geoip overrides these when a proxy is set.
_TIMEZONES = [
    ("America/New_York", "en-US"),
    ("America/Chicago", "en-US"),
    ("America/Los_Angeles", "en-US"),
    ("Europe/London", "en-GB"),
]


def _os_for(user_agent):
    """The operating system the user agent claims, for the browser's story."""
    if "Macintosh" in user_agent:
        return "macos"
    if "Firefox" in user_agent:
        return "windows" if "Windows" in user_agent else "macos"
    return "windows"


def _draw_identity(site, cfg, rng):
    """Draw a fresh identity for an account that has never been seen."""
    user_agent = rng.choice(_USER_AGENTS)
    viewport = rng.choice(_VIEWPORTS)
    timezone_id, locale = rng.choice(_TIMEZONES)
    return {
        "site": site,
        "user_agent": user_agent,
        "viewport": [viewport[0], viewport[1]],
        "timezone_id": timezone_id,
        "locale": locale,
        "os": _os_for(user_agent),
        "profile_dir": os.path.join(cfg["profiles_root"], site),
        # The NAME of the secret that holds this account's proxy address. The
        # address itself is never stored here. The per-account name is tried
        # first; the shared STEALTH_PROXY_URL is the fallback (see
        # resolve_identity), so one address can serve every account when the
        # owner has only one, and each account can have its own when needed.
        "proxy_secret": f"STEALTH_PROXY_{site.upper()}",
        "fingerprint_preset": None,
    }


def _load_identities(path):
    """Read the identities file, or an empty map when there is none yet."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A damaged file must not stop a run; the identities are re-drawn.
        return {}
    return data if isinstance(data, dict) else {}


def _save_identities(path, data):
    """Write the identities file. Returns False when it cannot be written."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def resolve_identity(site, cfg, env=None, identities_path=None, rng=None):
    """Return the pinned identity for an account, drawing and saving one when
    the account has never been seen. The returned dict also carries the
    run-time fields the browser needs: the parsed proxy (or None), the
    ``allow_login`` flag (False — a stealth session is cookie-only), the
    shuffle flag and a random source for the read order.

    The proxy address is read from the environment at this point and parsed
    here, so a bad address fails the run with a message that never shows it.
    """
    env = os.environ if env is None else env
    rng = rng or random.Random()
    path = identities_path or cfg["identities_file"]
    stored = _load_identities(path)
    pinned = stored.get(site)
    if not isinstance(pinned, dict) or "user_agent" not in pinned:
        pinned = _draw_identity(site, cfg, rng)
        stored[site] = pinned
        _save_identities(path, stored)
    identity = dict(pinned)
    identity.setdefault("profile_dir", os.path.join(cfg["profiles_root"], site))
    identity.setdefault("proxy_secret", f"STEALTH_PROXY_{site.upper()}")
    # The account's own proxy secret first, then the shared one, so one
    # address can serve every account and each account can still have its own.
    # A secret that is set but unusable fails the run (parse_proxy raises);
    # an unset secret simply means "no proxy for this account".
    proxy = None
    used = None
    for name in (identity["proxy_secret"], cfg["proxy_secret"]):
        if name and (env.get(name) or "").strip():
            proxy = parse_proxy(name, env)
            used = name
            break
    identity["proxy"] = proxy
    identity["proxy_secret_used"] = used
    # A stealth session never logs in from the runner. The cookies are the
    # session; a missing or expired cookie means "nothing was read", never a
    # fresh login from a new device and IP.
    identity["allow_login"] = False
    identity["shuffle_reads"] = cfg["shuffle_reads"]
    identity["rng"] = random.Random()
    identity["engine"] = None
    return identity


def identity_summary(identity):
    """One line describing an identity, safe to print (no proxy address)."""
    width, height = identity["viewport"]
    proxy = "off" if identity["proxy"] is None else "on"
    return (f"{identity['site']}: {identity['os']} {width}x{height} "
            f"{identity['timezone_id']} proxy {proxy}")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

def parse_proxy(secret_name, env=None):
    """Parse the proxy address held in the named secret into the shape
    Playwright expects, or None when no proxy is configured.

    Accepts ``http://``, ``https://`` and ``socks5://`` addresses, with or
    without credentials (``scheme://user:pass@host:port``). Raises StealthError
    for an address that is set but unusable — silently ignoring a configured
    proxy would send the account's traffic from a datacenter IP while the
    owner believes it is proxied. The message never contains the address.
    """
    env = os.environ if env is None else env
    value = (env.get(secret_name) or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
        raise StealthError(
            f"the proxy secret {secret_name} is set but is not a usable proxy "
            "address (use http://, https:// or socks5:// with a host and port); "
            "the address itself is never shown")
    try:
        port = parsed.port
    except ValueError:
        # An out-of-range port (urlparse raises instead of returning one).
        raise StealthError(
            f"the proxy secret {secret_name} is set but is not a usable proxy "
            "address (use http://, https:// or socks5:// with a host and port); "
            "the address itself is never shown") from None
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}"
             + (f":{port}" if port else "")}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def proxy_host(proxy):
    """The host part of a parsed proxy, for the read log. Never credentials."""
    if not proxy:
        return None
    return urlparse(proxy["server"]).hostname


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

def _importable(module):
    """True when the module can be imported (a missing engine is skipped)."""
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def available_engines(preferred="auto"):
    """The engines to try, best first, from what is installed.

    "auto" is Camoufox, then patchright, then plain Playwright. A single
    engine name pins the order to that engine first and plain last, so a
    pinned engine that is missing still degrades instead of failing.
    """
    order = ["camoufox", "patchright", "plain"]
    if preferred in order:
        order = [preferred] + [name for name in order if name != preferred]
    found = [name for name in order if name == "plain" or _importable(
        "camoufox.sync_api" if name == "camoufox" else "patchright.sync_api")]
    if "plain" not in found:
        found.append("plain")
    return found


def start_patchright():
    """Start patchright's driver (its own Playwright, patched so the browser
    does not advertise that it is automated)."""
    from patchright.sync_api import sync_playwright
    return sync_playwright().start()


def launch_camoufox(pw, identity, cfg):
    """Open a Camoufox context for one account on the shared Playwright
    instance. The context is persistent, so the profile directory holds the
    session between runs. The fingerprint is the account's pinned preset when
    one has been recorded, otherwise a fresh draw for this launch (whether a
    persistent profile keeps the drawn fingerprint across launches is still
    to be proved live — see docs/platform/stealth.md).
    """
    from camoufox.sync_api import NewBrowser
    kwargs = {
        "os": identity["os"],
        "headless": "virtual",
        "humanize": cfg["humanize"],
        "geoip": cfg["geoip"],
        "locale": identity["locale"],
        "persistent_context": True,
        "user_data_dir": identity["profile_dir"],
        "window": tuple(identity["viewport"]),
    }
    if identity.get("proxy") is not None:
        kwargs["proxy"] = identity["proxy"]
    if identity.get("fingerprint_preset"):
        kwargs["fingerprint_preset"] = identity["fingerprint_preset"]
    return NewBrowser(pw, **kwargs)


def launch_patchright(driver, identity, cfg, headless=True, xvfb=None):
    """Open a patchright persistent context for one account. Headed when an
    X server is available (headed is what removes the headless tell); headless
    when there is none, with the pinned user agent still applied.

    ``xvfb`` is the X server this launch runs on, when one was started: its
    outcome is recorded on the returned context as ``xvfb_status`` (``started``,
    ``already_running``, ``not_available`` or ``failed``), so the run report
    can show whether the browser really ran headed. Recording it never fails
    the launch.
    """
    context = driver.chromium.launch_persistent_context(
        user_data_dir=identity["profile_dir"],
        headless=headless,
        viewport={"width": identity["viewport"][0], "height": identity["viewport"][1]},
        user_agent=identity["user_agent"],
        timezone_id=identity["timezone_id"],
        locale=identity["locale"],
        proxy=identity["proxy"],
        args=["--no-sandbox"])
    if xvfb is not None:
        try:
            context.xvfb_status = xvfb.status
        except Exception:
            pass
    return context


def launch_plain(pw, identity, headless=True):
    """Open a plain Playwright context for one account, with the identity's
    user agent, viewport, timezone, locale and proxy applied. This is the
    last resort when neither stealth engine is installed; it is still better
    than the old fixed values, and it is what runs when the layer is off.
    """
    browser = pw.chromium.launch(headless=headless, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": identity["viewport"][0], "height": identity["viewport"][1]},
        user_agent=identity["user_agent"],
        timezone_id=identity["timezone_id"],
        locale=identity["locale"],
        proxy=identity["proxy"])
    return context, browser


class Xvfb:
    """A private X server for headed browsers, started only when one is
    needed and stopped when the run is done. Best effort: when Xvfb is not
    installed, ``start`` returns False and the caller falls back.

    ``status`` records how the start attempt went, so a run report can show
    it instead of leaving the headed/headless question unanswered: ``started``
    (this run started one), ``already_running`` (DISPLAY was already set, so
    no server was started), ``not_available`` (Xvfb is not installed),
    ``failed`` (starting one was tried and did not work), or None before any
    attempt. ``status_line()`` is the plain line for the run log.
    """

    def __init__(self):
        self.process = None
        self.display = None
        self._old_display = None
        self.status = None

    def status_line(self):
        """One plain line about the start attempt, for the run log."""
        return {
            "started": "Xvfb: started",
            "already_running": "Xvfb: already running (DISPLAY was already set) — patchright runs headed",
            "not_available": "Xvfb: not available — patchright ran headless",
            "failed": "Xvfb: failed to start — patchright ran headless",
            None: "Xvfb: not attempted",
        }.get(self.status, "Xvfb: unknown")

    def start(self):
        if os.environ.get("DISPLAY"):
            self.status = "already_running"
            return True
        if shutil.which("Xvfb") is None:
            self.status = "not_available"
            return False
        for number in range(99, 120):
            lock = f"/tmp/.X{number}-lock"
            if os.path.exists(lock):
                continue
            try:
                proc = subprocess.Popen(
                    ["Xvfb", f":{number}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.status = "failed"
                return False
            for _ in range(50):
                if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                    break
                time.sleep(0.1)
            else:
                proc.terminate()
                continue
            if proc.poll() is not None:
                continue
            self.process = proc
            self.display = f":{number}"
            self._old_display = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = self.display
            self.status = "started"
            return True
        self.status = "failed"
        return False

    def stop(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                except OSError:
                    pass
            self.process = None
        if self._old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._old_display
        self._old_display = None
        self.display = None


# ---------------------------------------------------------------------------
# Read order
# ---------------------------------------------------------------------------

def shuffle_reads(urls, rng=None):
    """Return the planned pages in a shuffled order. A person does not work
    through a list in exactly the same order every time."""
    shuffled = list(urls)
    (rng or random.Random()).shuffle(shuffled)
    return shuffled


# ---------------------------------------------------------------------------
# Per-read log and human score
# ---------------------------------------------------------------------------

_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


_EGRESS = {}                       # key (the proxy host, or "direct") -> (when, ip): one lookup per route, not one per browser open (a FOUND address only)
_EGRESS_FOUND_SECONDS = 900.0


def resolve_egress_ip(page, key=None):
    """The IP the account's traffic comes from, read through the browser's
    own context so it goes through the proxy when one is configured.
    Best effort: None when the check fails. The address is only ever written
    to the run's own debug directory.

    With a `key` (the proxy host, or "direct") a FOUND address is kept for 15 minutes: a run opens a browser per site and per task, and the route has not changed in between.
    A failed lookup is never kept: the next open asks again, so one failure cannot blank the address in every read that follows.
    """
    now = time.monotonic()
    if key is not None and key in _EGRESS:
        when, ip = _EGRESS[key]
        if ip and now - when < _EGRESS_FOUND_SECONDS:
            return ip
    try:
        response = page.context.request.get("https://api.ipify.org", timeout=10000)
        text = (response.text() or "").strip()
    except Exception:
        text = None
    ip = text if isinstance(text, str) and _IP_RE.match(text) else None
    if key is not None and ip:
        _EGRESS[key] = (now, ip)
    return ip


def human_score(dwell_seconds, content_chars, mouse_moves, scroll_events, pauses):
    """A 0-100 score for how human one read looked, from what was measured.

    40 points for dwelling long enough to read the page (about 200 words a
    minute, capped at two minutes), 25 for moving the mouse, 20 for scrolling
    and 15 for pausing between actions. A read that opened a page and left
    instantly scores near zero; a read that lingered, moved and scrolled
    scores near a hundred. The score is a trace, not a verdict.
    """
    words = max(content_chars, 0) / 5.0
    expected = min(words / 200.0 * 60.0, 120.0)
    if expected > 0:
        dwell = 40.0 * min(dwell_seconds / expected, 1.0)
    else:
        # A page with nothing to read (a photo grid): a few seconds of looking earns the dwell marks, leaving instantly earns none.
        dwell = 40.0 * min(dwell_seconds / 5.0, 1.0)
    mouse = 25.0 * min(mouse_moves / 8.0, 1.0)
    scroll = 20.0 * min(scroll_events / 5.0, 1.0)
    pause = 15.0 * min(pauses / 3.0, 1.0)
    return round(min(dwell + mouse + scroll + pause, 100.0))


class ReadLog:
    """One entry per page read, for tracing a future warning back to what the
    account actually did. Kept in memory during the run and written as JSONL
    to the run's debug directory when the browser closes.
    """

    def __init__(self, site, engine, identity, egress_ip=None, xvfb_status=None):
        self.site = site
        self.engine = engine
        self.identity = identity
        self.egress_ip = egress_ip
        self.xvfb_status = xvfb_status       # how the X server attempt went (None when no X server was involved)
        self.entries = []

    def record(self, url, started_at, ended_at, content_chars=0, mouse_moves=0,
               scroll_events=0, pauses=0, error=None):
        """Add one read. Times are epoch seconds; dwell is derived from them."""
        dwell = max(ended_at - started_at, 0.0)
        entry = {
            "site": self.site,
            "url": url,
            "engine": self.engine,
            "started_at": started_at,
            "ended_at": ended_at,
            "dwell_seconds": round(dwell, 2),
            "content_chars": content_chars,
            "mouse_moves": mouse_moves,
            "scroll_events": scroll_events,
            "pauses": pauses,
            "human_score": human_score(dwell, content_chars, mouse_moves,
                                       scroll_events, pauses),
            "egress_ip": self.egress_ip,
            "proxy": proxy_host(self.identity.get("proxy")) if self.identity else None,
        }
        if error:
            entry["error"] = str(error)[:200]
        self.entries.append(entry)
        return entry

    def update(self, entry, ended_at=None, mouse_moves=None, scroll_events=None,
               pauses=None, content_chars=None):
        """Refresh an entry with what the read has looked like so far (the
        scrolling happens after the page opens) and recompute its score."""
        if ended_at is not None:
            entry["ended_at"] = ended_at
            entry["dwell_seconds"] = round(max(ended_at - entry["started_at"], 0.0), 2)
        for key, value in (("mouse_moves", mouse_moves), ("scroll_events", scroll_events),
                           ("pauses", pauses), ("content_chars", content_chars)):
            if value is not None:
                entry[key] = value
        entry["human_score"] = human_score(entry["dwell_seconds"], entry["content_chars"],
                                           entry["mouse_moves"], entry["scroll_events"],
                                           entry["pauses"])
        return entry

    def summary(self):
        """One dict for the run report: pages, engine, identity, proxy host,
        egress IP and the mean human score."""
        if not self.entries:
            return None
        scores = [entry["human_score"] for entry in self.entries]
        dwells = [entry["dwell_seconds"] for entry in self.entries]
        return {
            "pages": len(self.entries),
            "engine": self.engine,
            "identity": self.identity["site"] if self.identity else None,
            "proxy": proxy_host(self.identity.get("proxy")) if self.identity else None,
            "egress_ip": self.egress_ip,
            "xvfb": self.xvfb_status,
            "human_score": round(sum(scores) / len(scores)),
            "dwell_seconds": round(sum(dwells) / len(dwells), 1),
        }

    def write_jsonl(self, path):
        """Write the entries as JSONL. Nothing is written when nothing was read (an empty file would never reach the
        repository: GitHub's web upload skips empty files, and a test asserts no empty file exists). Returns False when
        nothing was written."""
        if not self.entries:
            return False
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                for entry in self.entries:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            return False
        return True
