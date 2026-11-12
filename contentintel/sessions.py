"""Stored login sessions: the secondary route for the channels that otherwise need OpenCLI and a logged-in desktop browser.

What this module is, in one line: a safe way to hold, check and hand on a login session that the owner captured once, by hand, in a real browser.

How it fits (the pattern R_C_E uses for Refunnel, applied to the social channels):
  1. CAPTURE, once, on the owner's own machine: `python -m contentintel.sessions capture --platform instagram` opens a real browser, the owner logs in himself, and the session
     (Playwright `storage_state`: cookies and local storage) is saved to a file that is never committed.
  2. STORE: `... encode` turns that file into one base64 text file; the owner pastes it into the GitHub Secret named below (`INSTAGRAM_SESSION_B64`, and so on).
  3. USE, on a GitHub-hosted runner: `load()` decodes the secret, checks it (required cookies present, none expired) BEFORE any request, and hands the cookies on to the one code path that
     needs them: the Scrapling worker (`cookies_env`, the route that already exists), a cookie file for yt-dlp or instaloader, or a header. Never to an external repository or a subprocess it
     does not need.
  4. SAY WHAT HAPPENED: `status()` reports healthy, degraded (expired, or about to be), down (unusable) or unknown (never set up), with the true reason and the date it expires.

What it never does: log in with a password on a run (a password is only ever used by hand, once, to capture), retry a login in a loop, write a session back to GitHub Secrets, print or log a
session value, or treat a doubtful session as good. Anything it is not sure about is "invalid". Whether a stored session survives a login check from a new datacentre address cannot be known
from here: every platform below is marked `verified: False` until a real run proves it.

Whether a session can search is a different question from whether it is valid. For Instagram, TikTok, Xiaohongshu, Facebook and LinkedIn the keyword search is served by signed internal
requests that check the browser, the address and the session together, and a stored session does not pass that check from a datacentre address. A session buys single-item reads there, and real
search on X; the honest route for the rest is a broker or the search layer. See docs/platform/session-capture-guide.md.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import contract as K

MAX_ENCODED_CHARS = 45000            # GitHub Secrets hold at most 48 KB: leave room
MAX_DECODED_BYTES = 200_000
MAX_COOKIES = 400
WARN_DAYS = 7                        # a session this close to expiring is healthy but says so

# Words that mean "this is a login, a checkpoint or a challenge, not the logged-in site" when they appear in the address the page ended up at. Compared in lower case, so the capital
# /Login that sent R_C_E's runs to a login screen for 40 minutes is caught the same as /login.
LOGIN_WORDS = ("login", "log-in", "signin", "sign-in", "sign_in", "checkpoint", "challenge", "authwall", "captcha", "verify", "suspended", "/i/flow/", "emailsignup", "reauth", "unusual")

# `verified` stays False until a real run has shown the platform accepts the stored session from a runner. `logged_in` markers are best guesses: with none of them present the session is
# treated as invalid, which is the safe direction.
PLATFORMS = {
    "instagram": {"label": "Instagram", "secret": "INSTAGRAM_SESSION_B64", "hosts": ("instagram.com",), "required": ("sessionid", "csrftoken"), "any_of": (),
                  "login_url": "https://www.instagram.com/accounts/login/", "check_url": "https://www.instagram.com/",
                  "logged_in": ('"viewerid"', '"is_logged_in":true', "/accounts/activity", "direct/inbox"), "logged_out": ("log in to instagram", "create new account"),
                  "lifetime": "about 90 days", "can": "single posts and profiles at low volume; not keyword search", "verified": False},
    "x": {"label": "X (Twitter)", "secret": "X_SESSION_B64", "hosts": ("x.com", "twitter.com"), "required": ("auth_token", "ct0"), "any_of": (),
          "login_url": "https://x.com/i/flow/login", "check_url": "https://x.com/home",
          "logged_in": ('"isloggedin":true', "data-testid=\"sidebarcolumn\"", "/compose/post", "aria-label=\"home timeline\""), "logged_out": ("sign in to x", "don't miss what's happening"),
          "lifetime": "months; the spare account must be phone-verified for search", "can": "search, timelines, users (the best session case)", "verified": False},
    "tiktok": {"label": "TikTok", "secret": "TIKTOK_SESSION_B64", "hosts": ("tiktok.com",), "required": ("sessionid",), "any_of": (),
               "login_url": "https://www.tiktok.com/login", "check_url": "https://www.tiktok.com/",
               "logged_in": ('"islogin":true', "data-e2e=\"profile-icon\"", "upload"), "logged_out": ("log in to tiktok",),
               "lifetime": "about 30 days for the token", "can": "single known videos through yt-dlp; not search", "verified": False},
    "reddit": {"label": "Reddit", "secret": "REDDIT_SESSION_B64", "hosts": ("reddit.com",), "required": (), "any_of": ("reddit_session", "token_v2"),
               "login_url": "https://www.reddit.com/login", "check_url": "https://www.reddit.com/settings/",
               "logged_in": ("logout", "settings/account", '"loggedin":true'), "logged_out": ("log in to reddit",),
               "lifetime": "weeks to months; Reddit's own API closes in March 2027", "can": "reads while Reddit still allows them", "verified": False},
    "facebook": {"label": "Facebook", "secret": "FACEBOOK_SESSION_B64", "hosts": ("facebook.com",), "required": ("c_user", "xs"), "any_of": (),
                 "login_url": "https://www.facebook.com/login", "check_url": "https://www.facebook.com/",
                 "logged_in": ('"is_logged_in":true', "/me/", "logout"), "logged_out": ("log in to facebook", "create new account"),
                 "lifetime": "about 90 days", "can": "named public Pages only; not keyword search", "verified": False},
    "linkedin": {"label": "LinkedIn", "secret": "LINKEDIN_SESSION_B64", "hosts": ("linkedin.com",), "required": ("li_at",), "any_of": (),
                 "login_url": "https://www.linkedin.com/login", "check_url": "https://www.linkedin.com/feed/",
                 "logged_in": ("global-nav", "/feed/", "voyager"), "logged_out": ("sign in to linkedin", "join now"),
                 "lifetime": "about a year", "can": "public post reads where possible (high detection risk); not search", "verified": False},
    "xiaohongshu": {"label": "Xiaohongshu", "secret": "XIAOHONGSHU_SESSION_B64", "hosts": ("xiaohongshu.com",), "required": ("web_session",), "any_of": (),
                    "login_url": "https://www.xiaohongshu.com/", "check_url": "https://www.xiaohongshu.com/explore",
                    "logged_in": ("user-info", "userinfo"), "logged_out": ("login-container",),
                    "lifetime": "weeks", "can": "single notes (request signing changes often); not search", "verified": False},
    "boss": {"label": "Boss (Zhipin)", "secret": "BOSS_SESSION_B64", "hosts": ("zhipin.com",), "required": (), "any_of": ("wt2", "zp_token", "__zp_stoken__"),
             "login_url": "https://www.zhipin.com/", "check_url": "https://www.zhipin.com/",
             "logged_in": ("user-nav",), "logged_out": ("login",),
             "lifetime": "unknown", "can": "to be decided by a live check", "verified": False},
}


class SessionProblem(Exception):
    """Raised inside this module for a session that cannot be used; load() turns it into the platform's own K.Failure. The text never contains a session value."""


def _spec(platform):
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise K.Failure(K.BAD_INPUT, f"unknown platform {platform!r}; the stored-session platforms are {', '.join(PLATFORMS)}") from None


def _utc(now=""):
    if not now:
        return datetime.now(timezone.utc)
    value = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---- decoding, the one place a session value is parsed --------------------------------------------------------------------------------------------

_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def _as_state(obj):
    """A storage_state dict from what was pasted: a capture's own file ({"cookies": [...]}) or the JSON a browser cookie-export extension makes (a list of cookies, with `expirationDate`)."""
    cookies = obj if isinstance(obj, list) else (obj.get("cookies") if isinstance(obj, dict) else None)
    if not isinstance(cookies, list):
        return obj
    out = []
    for c in cookies:
        if not isinstance(c, dict):
            out.append(c)
            continue
        c = dict(c)
        if "expires" not in c:
            c["expires"] = -1 if c.get("session") else c.get("expirationDate", -1)
        c.pop("expirationDate", None)
        c.pop("session", None)
        c.pop("hostOnly", None)
        c.pop("storeId", None)
        same = _SAMESITE.get(str(c.get("sameSite", "")).lower())
        if same:
            c["sameSite"] = same
        else:
            c.pop("sameSite", None)
        out.append(c)
    return {"cookies": out, "origins": (obj.get("origins") if isinstance(obj, dict) else None) or []}


def decode_session(encoded):
    """The storage_state dict held in a secret: base64 of a capture's file, base64 of a cookie export, or the cookie-export JSON pasted as it is. Raises SessionProblem with a reason that never
    contains the value."""
    text = "".join(str(encoded or "").split()) if not str(encoded or "").lstrip()[:1] in ("[", "{") else str(encoded).strip()
    if not text:
        raise SessionProblem("it is empty")
    if len(text) > MAX_ENCODED_CHARS * 2:
        raise SessionProblem("it is far larger than a GitHub secret can hold, so it was not captured correctly")
    if text[:1] in ("[", "{"):
        raw = text.encode("utf-8")
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise SessionProblem("it is not valid base64 (paste the whole text of the .b64.txt file, or the cookie export as it is, with nothing added or missing)") from None
    if len(raw) > MAX_DECODED_BYTES:
        raise SessionProblem("it is too large to be a session")
    try:
        state = _as_state(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        raise SessionProblem("it decoded, but not to a session or a cookie export (it should be JSON)") from None
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list) or not cookies:
        raise SessionProblem("it holds no cookies")
    if len(cookies) > MAX_COOKIES:
        raise SessionProblem("it holds an unreasonable number of cookies")
    for c in cookies:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("value"), str) or not isinstance(c.get("domain"), str):
            raise SessionProblem("a cookie in it is not in the expected shape")
    return state


def encode_session(state):
    """The base64 text of a storage_state, trimmed to fit a GitHub secret: local storage is dropped, origin by origin, largest first, then altogether, until it fits (cookies are what is used).
    Returns (text, notes)."""
    notes = []
    state = json.loads(json.dumps(state))
    text = base64.b64encode(json.dumps(state, separators=(",", ":")).encode("utf-8")).decode("ascii")
    origins = sorted(state.get("origins") or [], key=lambda o: len(json.dumps(o)), reverse=True)
    while len(text) > MAX_ENCODED_CHARS and origins:
        gone = origins.pop(0)
        notes.append(f"left out the saved page data of {gone.get('origin', 'one site')} to fit the 48 KB limit of a GitHub secret")
        state["origins"] = origins
        text = base64.b64encode(json.dumps(state, separators=(",", ":")).encode("utf-8")).decode("ascii")
    if len(text) > MAX_ENCODED_CHARS:
        raise SessionProblem(f"even the cookies alone are {len(text)} characters, more than a GitHub secret can hold ({MAX_ENCODED_CHARS}); log out of other sites in that browser profile and capture again")
    return text, notes


# ---- a loaded session -----------------------------------------------------------------------------------------------------------------------------

class Session:
    """A checked session. Its values are reachable only through the methods that hand them to a particular use; printing or logging it shows no value."""

    def __init__(self, platform, state, now=""):
        self.platform, self._state, self._now = platform, state, _utc(now)
        self.spec = _spec(platform)

    def __repr__(self):
        return f"<Session {self.platform}: {len(self._cookies())} cookies, expires {self.expires_on() or 'unknown'}>"

    __str__ = __repr__

    def _cookies(self):
        hosts = self.spec["hosts"]
        return [c for c in self._state["cookies"] if any(str(c["domain"]).lstrip(".").lower().endswith(h) for h in hosts)]

    def names(self):
        return {c["name"] for c in self._cookies()}

    def missing(self):
        have = self.names()
        out = [n for n in self.spec["required"] if n not in have]
        if self.spec["any_of"] and not (set(self.spec["any_of"]) & have):
            out.append(" or ".join(self.spec["any_of"]))
        return out

    def _expiry_of(self, names):
        out = []
        for c in self._cookies():
            if c["name"] in names and isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
                out.append(datetime.fromtimestamp(c["expires"], tz=timezone.utc))
        return out

    def _key_names(self):
        return set(self.spec["required"]) | set(self.spec["any_of"])

    def expires_on(self):
        """The earliest date a cookie this route depends on stops working (YYYY-MM-DD), or '' when the cookies carry no expiry (they last as long as the browser session would)."""
        dates = self._expiry_of(self._key_names())
        return min(dates).strftime("%Y-%m-%d") if dates else ""

    def days_left(self):
        dates = self._expiry_of(self._key_names())
        return None if not dates else (min(dates) - self._now).days

    def expired(self):
        left = self.days_left()
        return left is not None and left < 0

    # ---- the uses -------------------------------------------------------------------------------------------------------------------------------

    def playwright_cookies(self):
        """The cookies in the shape Playwright and Scrapling's browser take."""
        return [dict(c) for c in self._cookies()]

    def cookie_header(self):
        """name=value pairs for a Cookie header (for a single request)."""
        return "; ".join(f"{c['name']}={c['value']}" for c in self._cookies())

    def netscape_text(self):
        """The cookies as a Netscape cookies.txt, which yt-dlp and instaloader read."""
        lines = ["# Netscape HTTP Cookie File"]
        for c in self._cookies():
            expiry = int(c["expires"]) if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0 else 0
            lines.append("\t".join([c["domain"], "TRUE" if str(c["domain"]).startswith(".") else "FALSE", c.get("path") or "/", "TRUE" if c.get("secure") else "FALSE", str(expiry), c["name"], c["value"]]))
        return "\n".join(lines) + "\n"

    def cookie_jar(self):
        """{name: value} for this platform's own cookies: the shape the Scrapling worker reads (it scopes them to each page's address itself)."""
        return {c["name"]: c["value"] for c in self._cookies()}

    def worker_env(self):
        """(environment variable name, JSON text) for the Scrapling worker's `cookies_env`: the worker is handed only this one named secret, as an object of name and value."""
        return f"SESSION_COOKIES_{self.platform.upper()}", json.dumps(self.cookie_jar(), separators=(",", ":"))

    def write_private(self, directory, kind="netscape"):
        """Write the cookies to a file only the owner of the process can read (the runner's temporary folder, never the repository) and return its path."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (f"{self.platform}_cookies.txt" if kind == "netscape" else f"{self.platform}_cookies.json")
        body = self.netscape_text() if kind == "netscape" else json.dumps(self.playwright_cookies(), separators=(",", ":"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path


def load(platform, environ=None, now=""):
    """The platform's stored session, checked without making any request. Raises K.Failure with the true reason: NEED_SETUP (not set up, or not captured correctly), NEED_LOGIN (expired)."""
    spec = _spec(platform)
    env = os.environ if environ is None else environ
    raw = (env.get(spec["secret"]) or "").strip()
    if not raw:
        raise K.Failure(K.NEED_SETUP, f"no stored {spec['label']} session: {spec['secret']} is not set (capture one by hand, see docs/platform/session-capture-guide.md)")
    try:
        state = decode_session(raw)
    except SessionProblem as exc:
        raise K.Failure(K.NEED_SETUP, f"the stored {spec['label']} session ({spec['secret']}) cannot be used: {exc}") from None
    session = Session(platform, state, now)
    if not session._cookies():
        raise K.Failure(K.NEED_SETUP, f"the stored {spec['label']} session holds no cookie for {', '.join(spec['hosts'])}; it was captured from the wrong site or the wrong browser profile")
    missing = session.missing()
    if missing:
        raise K.Failure(K.NEED_SETUP, f"the stored {spec['label']} session is missing the cookie {', '.join(missing)}: the capture was not logged in; capture again after logging in")
    if session.expired():
        raise K.Failure(K.NEED_LOGIN, f"the stored {spec['label']} session expired on {session.expires_on()}: capture a new one (about every {spec['lifetime']}) and update {spec['secret']}")
    return session


def status(platform, environ=None, now=""):
    """{'state', 'reason', 'expires_on', 'days_left', 'verified'} for the Connections Health tab, with no request made. States: healthy, degraded (expired, or expiring within a week), down (cannot be
    used), unknown (never set up). `verified` is whether a real run has proved the platform accepts it."""
    spec = _spec(platform)
    base = {"platform": platform, "label": spec["label"], "secret": spec["secret"], "expires_on": "", "days_left": "", "verified": "yes" if spec["verified"] else "no"}
    try:
        session = load(platform, environ, now)
    except K.Failure as exc:
        text = str(exc)
        if exc.code == K.NEED_LOGIN:
            return {**base, "state": "degraded", "reason": text}
        if "is not set" in text:
            return {**base, "state": "unknown", "reason": "not set up"}
        return {**base, "state": "down", "reason": text}
    left = session.days_left()
    base.update(expires_on=session.expires_on(), days_left="" if left is None else left)
    if left is not None and left <= WARN_DAYS:
        return {**base, "state": "degraded", "reason": f"expires in {left} day(s), on {session.expires_on()}: capture a new one soon"}
    return {**base, "state": "healthy", "reason": "stored session present and not expired" + ("" if spec["verified"] else " (not yet proved against the live site)")}


# ---- is it still logged in? -----------------------------------------------------------------------------------------------------------------------

def _title(html):
    found = re.search(r"<title[^>]*>(.*?)</title>", str(html or ""), re.I | re.S)
    return " ".join(found.group(1).split())[:120] if found else ""


def _landed(url):
    """An address with no query, fragment or login-in-the-address part, so nothing that could be a token is ever printed."""
    text = str(url or "")
    text = re.sub(r"^(https?://)[^/@]*@", r"\1", text)
    return text.split("?")[0].split("#")[0][:200]


def page_evidence(platform, final_url, html, http_status=0, text=""):
    """What a page showed, for the log, with no secret in it: where it landed (no query), the HTTP status, the title, which logged-in markers were and were not in the page, which logged-out wording was
    in the visible text and which only in the page's code, whether a password field is on it, and the first words of its visible text."""
    spec = _spec(platform)
    low, vis = str(html or "").lower(), " ".join(str(text or "").split()).lower()
    landed = _landed(final_url)
    return {"landed": landed, "status": int(http_status or 0), "title": _title(html), "html_chars": len(str(html or "")), "text_chars": len(vis),
            "logged_in_found": [m for m in spec["logged_in"] if m in low], "logged_in_missing": [m for m in spec["logged_in"] if m not in low],
            "logged_out_in_text": [m for m in spec["logged_out"] if m in (vis if text else low)], "logged_out_only_in_code": [m for m in spec["logged_out"] if text and m in low and m not in vis],
            "password_field": 'type="password"' in low or "type='password'" in low, "login_word_in_address": next((w for w in LOGIN_WORDS if w in landed.lower()), ""),
            "text_starts": " ".join(str(text or "").split())[:240]}


def check_logged_in(platform, final_url, html, http_status=200, text=None):
    """(True, why) only when the page that loaded with the session really is the logged-in site. Any doubt means (False, why): a redirect to anything that looks like a login or a challenge
    (case-insensitive: /Login counts), another site, an empty page, the logged-out wording, or no logged-in marker at all. The logged-out wording is looked for in the page's VISIBLE text when that
    is given (the page's code can hold that wording in its translations even for a logged-in page); the logged-in markers are looked for in the page's code."""
    spec = _spec(platform)
    url = str(final_url or "")
    low_url = url.lower()
    if http_status in (401, 403):
        return False, f"the site answered {http_status}"
    host = re.sub(r"^https?://", "", low_url).split("/")[0].split("?")[0].split(":")[0]
    if not any(host == h or host.endswith("." + h) for h in spec["hosts"]):
        return False, "the page ended up on a different site than the platform"
    for word in LOGIN_WORDS:
        if word in low_url:
            return False, f"the page ended up at a login, checkpoint or challenge address ({word})"
    body = str(html or "")
    if len(body) < 200:
        return False, "the page came back empty or nearly empty"
    low = body.lower()
    visible = " ".join(str(text).split()).lower() if text is not None else low
    for marker in spec["logged_out"]:
        if marker in visible:
            return False, "the page shows the logged-out state"
    if not any(marker in low for marker in spec["logged_in"]):
        return False, "no sign of being logged in was found on the page (any doubt counts as not logged in)"
    return True, "logged in"


def refresh_session(platform):
    """An automatic re-login is not built for any platform: each platform's login flow (a captcha, a slider, a device check, a code by SMS or app) has to be proved live first, and a failed
    re-login must never be retried. So the answer is always the true reason and 'capture again'."""
    spec = _spec(platform)
    raise K.Failure(K.NEED_LOGIN, f"{spec['label']} cannot be logged in again automatically (that has not been proved to work from a runner): capture a new session by hand and update {spec['secret']}")


# ---- capture (by hand, on the owner's own machine) ------------------------------------------------------------------------------------------------

def capture_session_interactive(platform, out_path, *, launch=None, input_fn=input, print_fn=print):
    """Open a real, visible browser at the platform's login page; the owner logs in himself and presses Enter; the session is saved to out_path (owner-only file). Never run in a workflow."""
    spec = _spec(platform)
    if launch is None:
        launch = _playwright_launch
    print_fn(f"A browser window will open at {spec['login_url']}.\nLog in yourself, using the account you want this tool to use (a dedicated account, not your personal one).")
    with launch(spec["login_url"]) as (context, close):
        input_fn(f"When you are logged in and can see the {spec['label']} home page, come back here and press Enter... ")
        state = context.storage_state()
        close()
    problem = None
    try:
        text, notes = encode_session(state)
        decode_session(text)
    except SessionProblem as exc:
        problem = str(exc)
    if problem:
        raise K.Failure(K.NEED_SETUP, problem)
    out = Path(out_path)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state))
    session = Session(platform, state)
    missing = session.missing()
    for note in notes:
        print_fn("note: " + note)
    if missing:
        print_fn(f"WARNING: the session is missing the cookie {', '.join(missing)}, which means you were not logged in. Do not use this file; run the capture again.")
    print_fn(f"Saved the session to {out}. It expires {session.expires_on() or 'when the browser session would'} (about every {spec['lifetime']}).\nNext: encode it and paste it into the GitHub secret {spec['secret']}. Never share or commit this file.")
    return out


def _playwright_launch(url):
    from contextlib import contextmanager

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise K.Failure(K.NEED_SETUP, "Playwright is not installed on this machine: run `pip install playwright` and `playwright install chromium` once, then capture again") from None

    @contextmanager
    def opened():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            context.new_page().goto(url)
            yield context, browser.close
    return opened()


def encode_file(in_path, out_path):
    """Read a captured session file and write its base64 text, ready to paste into the secret, to out_path (a file, so nothing secret is printed). Returns the notes."""
    state = json.loads(Path(in_path).read_text(encoding="utf-8"))
    text, notes = encode_session(state)
    decode_session(text)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return notes


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m contentintel.sessions", description="Capture, encode and check stored login sessions (see docs/platform/session-capture-guide.md).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="on YOUR machine: log in by hand in a real browser and save the session")
    cap.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    cap.add_argument("--out", help="where to save the session (default: <platform>_session.json)")
    enc = sub.add_parser("encode", help="turn a captured session file into the base64 text for the GitHub secret (written to a file, not printed)")
    enc.add_argument("--in", dest="source", required=True)
    enc.add_argument("--out", help="default: <input>.b64.txt")
    sub.add_parser("status", help="check every stored session in the environment, offline (no request is made)")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "capture":
            capture_session_interactive(args.platform, args.out or f"{args.platform}_session.json")
        elif args.cmd == "encode":
            out = args.out or f"{args.source}.b64.txt"
            for note in encode_file(args.source, out):
                print("note: " + note)
            print(f"Wrote {out}. Open it, copy ALL of its text, and paste it as the value of the GitHub secret. Then delete both files from this machine.")
        else:
            for platform in PLATFORMS:
                s = status(platform)
                print(f"  {s['label']:<14} {s['state']:<9} {s['reason']}" + (f" (expires {s['expires_on']})" if s["expires_on"] else ""))
    except (K.Failure, SessionProblem, OSError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
