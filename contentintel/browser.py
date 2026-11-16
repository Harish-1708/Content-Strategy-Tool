"""A real (headless) browser, driven the way a person would use it.

The collectors never touch Playwright directly. They use the small interface below, so the
collection logic can be tested without a browser, and only this file knows about Playwright.
Nothing here is ever printed or saved that contains a cookie value or a password.

The stealth layer (Update 68, contentintel/stealth.py) gives a logged-in session a
stable identity — one pinned user agent, viewport, timezone and proxy per account —
and a stealth browser (Camoufox, then patchright headed under Xvfb, then plain
Playwright). It is on by default and the owner can turn it off; with it off every
read behaves exactly as it did before. The plain path below is unchanged: one
Chromium per site on the shared Playwright, no automation-hiding flag, --no-sandbox
only.
"""
import collections
import json
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from .human import Human, HumanConfigError, resolve as resolve_human

SITES = ("meta_ads", "instagram", "tiktok", "gemini")
_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


class BrowserError(Exception):
    """Something went wrong driving a site; the message says what a person can do."""


class LoginWall(BrowserError):
    """The dummy account is not logged in (expired cookies, or the site refused the login)."""


class Challenge(BrowserError):
    """The site asked for a code, puzzle or 'confirm it's you'; a person must clear it."""


class AccountWarning(Challenge):
    """The site warned the account (suspected automation, an action blocked, a lock). Using it again straight away
    is what turns a warning into a ban, so the run stops using it and the account is rested."""

    def __init__(self, site, reason):
        self.site, self.reason = site, reason
        super().__init__(f"{site}: {site} showed an account warning ({reason}). The tool stopped using the account "
                         "straight away. Open the account by hand and follow what the warning says")


# ---------------------------------------------------------------------------------------
# settings (browser.yaml)
# ---------------------------------------------------------------------------------------

_TYPES = {
    "needs_login": "bool", "secrets": "secrets", "home": "str", "hashtag_url": "str",
    "profile_url": "str", "search_url": "str", "app_url": "str", "settle_seconds": "num",
    "json_url_hints": "strs", "consent_any": "strs", "logged_in_any": "strs", "login_wall_any": "strs",
    "challenge_any": "strs", "challenge_dismiss": "strs", "error_any": "strs", "hashtag_via_search_on_error": "bool", "hashtag_via_search_first": "bool", "search_box": "strs",
    "warning_any": "strs", "warning_url_any": "strs", "warmup": "warmup",
    "pacing": "pace", "max_scrolls_per_page": "whole", "max_pages_per_run": "whole", "max_pages_per_day": "whole", "min_hours_between_runs": "num",
    "deep_research_active_any": "strs", "signed_out_any": "strs", "link_contains": "strs", "prompt_box": "strs", "send": "strs",
    "start_research": "strs", "still_working_any": "strs", "done_any": "strs", "response": "strs",
    "deep_research_clicks": "strs_list", "login_steps": "steps", "wait": "wait", "password_fallback": "fallback",
    "scroll_pause_seconds": "num",
}
_REQUIRED = {
    "meta_ads": ["needs_login"],
    "instagram": ["needs_login", "secrets", "home", "hashtag_url", "link_contains", "logged_in_any", "login_wall_any"],
    "tiktok": ["needs_login", "secrets", "home", "hashtag_url", "profile_url", "search_url", "link_contains",
               "logged_in_any", "login_wall_any"],
    "gemini": ["needs_login", "secrets", "app_url", "logged_in_any", "login_wall_any", "prompt_box",
               "deep_research_clicks", "send", "start_research", "done_any", "response", "wait"],
}
_WAIT_KEYS = ("max_minutes", "plan_wait_seconds", "poll_seconds", "settle_seconds", "min_chars",
              "question_settle_seconds")
_PACE_KEYS = {"min_delay_seconds", "max_delay_seconds", "scroll_pause_seconds"}
_STEP_ACTIONS = ("goto", "fill", "click", "click_optional", "wait")
# "never" is the stealth sessions' value: a cookie-only session never logs in
# from the runner, so a missing or expired cookie means "nothing was read",
# never a fresh login from a new device and IP.
PASSWORD_FALLBACKS = ("no_cookies_only", "when_cookies_fail", "never")


def _num(value, where, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise BrowserError(f"{where} must be a number of at least {minimum}, got {value!r}")
    return value


def _strs(value, where):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
        raise BrowserError(f"{where} must be a non-empty list of text values")
    return list(value)


def _check_value(kind, value, where):
    if kind == "bool":
        if not isinstance(value, bool):
            raise BrowserError(f"{where} must be true or false")
    elif kind == "str":
        if not isinstance(value, str) or not value.startswith("https://"):
            raise BrowserError(f"{where} must be a https:// address, got {value!r}")
    elif kind == "num":
        _num(value, where)
    elif kind == "whole":
        _num(value, where, minimum=1)
        if value != int(value):
            raise BrowserError(f"{where} must be a whole number, got {value!r}")
    elif kind == "pace":
        if not isinstance(value, dict) or not value or set(value) - _PACE_KEYS:
            raise BrowserError(f"{where} must be some of: {', '.join(sorted(_PACE_KEYS))}")
        for k, v in value.items():
            _num(v, f"{where}.{k}")
    elif kind == "fallback":
        if value not in PASSWORD_FALLBACKS:
            raise BrowserError(f"{where} must be one of: {', '.join(PASSWORD_FALLBACKS)}; got {value!r}")
    elif kind == "strs":
        _strs(value, where)
    elif kind == "strs_list":
        if not isinstance(value, list) or not value:
            raise BrowserError(f"{where} must be a list of lists of selectors")
        for n, item in enumerate(value, start=1):
            _strs(item, f"{where} item {n}")
    elif kind == "warmup":
        if not isinstance(value, dict) or set(value) - {"enabled", "urls", "token", "wait_seconds", "poll_seconds"}:
            raise BrowserError(f"{where} must have only: enabled, urls, token, wait_seconds, poll_seconds")
        if "enabled" in value and not isinstance(value["enabled"], bool):
            raise BrowserError(f"{where}.enabled must be true or false")
        if "urls" in value:
            _strs(value["urls"], f"{where}.urls")
            for url in value["urls"]:
                if not url.startswith("https://"):
                    raise BrowserError(f"{where}.urls must be https:// addresses, got {url!r}")
        if "token" in value and (not isinstance(value["token"], str) or not value["token"].strip()):
            raise BrowserError(f"{where}.token must be a non-empty cookie name")
        for key in ("wait_seconds", "poll_seconds"):
            if key in value:
                _num(value[key], f"{where}.{key}")
    elif kind == "secrets":
        if not isinstance(value, dict) or "cookies" not in value or set(value) - {"cookies", "username", "password"}:
            raise BrowserError(f"{where} must name a cookies secret, and optionally username and password")
        if any(not isinstance(v, str) or not v.isidentifier() for v in value.values()):
            raise BrowserError(f"{where} secret names must be plain names such as INSTAGRAM_COOKIES")
    elif kind == "wait":
        if not isinstance(value, dict) or set(value) != set(_WAIT_KEYS):
            raise BrowserError(f"{where} must have exactly: {', '.join(_WAIT_KEYS)}")
        for k, v in value.items():
            _num(v, f"{where}.{k}", minimum=1)
    elif kind == "steps":
        if not isinstance(value, list) or not value:
            raise BrowserError(f"{where} must be a list of steps")
        for n, step in enumerate(value, start=1):
            if not isinstance(step, dict):
                raise BrowserError(f"{where} step {n} must be name: value pairs")
            actions = [k for k in step if k in _STEP_ACTIONS]
            extra = set(step) - set(_STEP_ACTIONS) - {"with"}
            if len(actions) != 1 or extra:
                raise BrowserError(f"{where} step {n} needs exactly one of {', '.join(_STEP_ACTIONS)}")
            action = actions[0]
            if action == "goto" and not isinstance(step["goto"], str):
                raise BrowserError(f"{where} step {n}: goto needs an address")
            if action == "wait":
                _num(step["wait"], f"{where} step {n} wait")
            if action in ("fill", "click", "click_optional"):
                _strs(step[action], f"{where} step {n} {action}")
            if action == "fill" and step.get("with") not in ("username", "password"):
                raise BrowserError(f"{where} step {n}: fill needs with: username or with: password")


def load_browser_settings(path="browser.yaml"):
    path = Path(path)
    if not path.is_file():
        raise BrowserError(f"{path} is missing; it holds the page addresses and button names the browser uses")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    extra = set(data) - {"pacing", "limits", "sites", "human", "stealth"}
    if extra:
        raise BrowserError(f"browser.yaml: unknown {sorted(extra)}")
    pacing, limits = data.get("pacing") or {}, data.get("limits") or {}
    if set(pacing) != {"min_delay_seconds", "max_delay_seconds", "scroll_pause_seconds"}:
        raise BrowserError("browser.yaml pacing needs min_delay_seconds, max_delay_seconds, scroll_pause_seconds")
    for k, v in pacing.items():
        _num(v, f"pacing.{k}")
    if pacing["min_delay_seconds"] > pacing["max_delay_seconds"]:
        raise BrowserError("pacing.min_delay_seconds cannot be more than max_delay_seconds")
    if set(limits) != {"max_scrolls_per_page"}:
        raise BrowserError("browser.yaml limits needs max_scrolls_per_page")
    _num(limits["max_scrolls_per_page"], "limits.max_scrolls_per_page", minimum=1)
    try:
        resolve_human(data.get("human"))
    except HumanConfigError as exc:
        raise BrowserError(str(exc)) from exc
    from . import stealth as stealth_mod
    try:
        stealth = stealth_mod.resolve(data.get("stealth"))
    except stealth_mod.StealthError as exc:
        raise BrowserError(str(exc)) from exc
    sites = data.get("sites") or {}
    if set(sites) != set(SITES):
        raise BrowserError(f"browser.yaml sites must be exactly: {', '.join(SITES)}")
    for name, cfg in sites.items():
        where = f"browser.yaml sites.{name}"
        if not isinstance(cfg, dict):
            raise BrowserError(f"{where} must be name: value pairs")
        unknown = sorted(set(cfg) - set(_TYPES))
        if unknown:
            raise BrowserError(f"{where}: unknown {unknown}")
        missing = [k for k in _REQUIRED[name] if k not in cfg]
        if missing:
            raise BrowserError(f"{where} is missing {missing}")
        for key, value in cfg.items():
            _check_value(_TYPES[key], value, f"{where}.{key}")
        if "pacing" in cfg:
            merged = {**pacing, **cfg["pacing"]}
            if merged["min_delay_seconds"] > merged["max_delay_seconds"]:
                raise BrowserError(f"{where}.pacing: min_delay_seconds cannot be more than max_delay_seconds")
    # The resolved stealth block is attached to every site's settings, so
    # open_browser (which only sees one site) can use it without any caller
    # changing. The block itself is also returned at the top level.
    for cfg in sites.values():
        cfg["stealth"] = stealth
    return {"pacing": pacing, "limits": limits, "sites": sites, "human": resolve_human(data.get("human")), "stealth": stealth}


def site_pacing(cfg, site):
    """The pauses for one site. From weakest to strongest: the general ones; the site's own `scroll_pause_seconds` (how long a
    slow-drawing page is given after each scroll); the site's `pacing` block, which is what Careful Mode writes and so always wins."""
    own = cfg["sites"][site]
    pace = dict(cfg["pacing"])
    if "scroll_pause_seconds" in own:
        pace["scroll_pause_seconds"] = own["scroll_pause_seconds"]
    return {**pace, **own.get("pacing", {})}


def site_scrolls(cfg, site):
    """How far down a results page to scroll for one site: its own setting, else the general one."""
    return cfg["sites"][site].get("max_scrolls_per_page") or cfg["limits"]["max_scrolls_per_page"]


# ---------------------------------------------------------------------------------------
# sessions: cookies exported from a logged-in dummy account
# ---------------------------------------------------------------------------------------

def parse_cookies(text):
    """Browser-export JSON (or a Playwright storage state) -> the cookie list Playwright wants."""
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BrowserError("the cookies secret is not valid JSON (export it as JSON from a cookie extension)") from exc
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise BrowserError("the cookies secret should be a JSON list of cookies")
    out = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c or not c.get("domain"):
            continue
        cookie = {"name": str(c["name"]), "value": str(c["value"]), "domain": c["domain"],
                  "path": c.get("path") or "/", "secure": bool(c.get("secure", True)),
                  "httpOnly": bool(c.get("httpOnly", False)),
                  "sameSite": _SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax")}
        expires = c.get("expirationDate", c.get("expires"))
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            cookie["expires"] = float(expires)
        out.append(cookie)
    if not out:
        raise BrowserError("no usable cookies were found in the cookies secret")
    return out


GOOGLE_SIGNIN_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID")


def describe_cookies(text, site="", now=None):
    """What a cookies secret holds, as names and counts only (never a value), so a failed login can be understood:
    how many cookies loaded, for which sites, how many are already expired and, for Google, which of the cookies
    that prove a sign-in are absent."""
    try:
        cookies = parse_cookies(text)
    except BrowserError as exc:
        return f"the cookies secret could not be read ({exc})"
    if not cookies:
        return "no cookies secret is set"
    now = time.time() if now is None else now
    domains = collections.Counter(c["domain"] for c in cookies)
    parts = [f"{len(cookies)} cookies loaded ({', '.join(f'{d} x{n}' for d, n in domains.most_common(4))})"]
    expired = sum(1 for c in cookies if c.get("expires") and c["expires"] < now)
    if expired:
        parts.append(f"{expired} already expired")
    if site == "gemini":
        missing = [n for n in GOOGLE_SIGNIN_COOKIES if n not in {c["name"] for c in cookies}]
        if missing:
            parts.append("Google sign-in cookies missing: " + ", ".join(missing)
                         + " (export again and include hidden/HttpOnly cookies)")
    return "; ".join(parts)


def describe_form_facts(facts):
    """A short plain sentence from form_facts(): never a value, only what kinds of boxes the page has."""
    if not facts:
        return "the page's boxes could not be listed"
    boxes = []
    for e in facts.get("inputs", [])[:10]:
        bits = [e.get("tag", "input") + (f"[type={e['type']}]" if e.get("type") else "")]
        for key, label in (("name", "name"), ("id", "id"), ("placeholder", "placeholder"), ("autocomplete", "autocomplete"), ("aria", "aria-label")):
            if e.get(key):
                bits.append(f"{label}='{str(e[key])[:40]}'")
        bits.append("visible" if e.get("visible") else "hidden")
        boxes.append(" ".join(bits))
    return (f"the page was {facts.get('url') or '(unknown address)'} titled '{facts.get('title', '')}' and had "
            + (f"{len(boxes)} box(es): " + "; ".join(boxes) if boxes else "no boxes to type into (an interstitial, a block page or a page that had not finished loading)"))


def parse_json_text(text):
    """Every JSON value in a response body (some sites send one per line, or a 'for (;;);' prefix)."""
    text = (text or "").strip()
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[9:]
        if line[:1] in ("{", "["):
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
    return found


# ---------------------------------------------------------------------------------------
# the browser itself
# ---------------------------------------------------------------------------------------

_SCRIPT_JSON = re.compile(r"<script\b[^>]*\btype=[\"']application/json[\"'][^>]*>(.*?)</script>", re.S | re.I)


def parse_embedded_json(html):
    """The JSON blocks a page carries inside itself (<script type="application/json">).

    Meta, Instagram and TikTok put their first batch of ads and posts there instead of fetching it
    separately, so it never shows up among the data requests.
    """
    found = []
    for match in _SCRIPT_JSON.finditer(html or ""):
        body = match.group(1).strip()
        if not body or len(body) > 8_000_000:
            continue
        try:
            found.append(json.loads(body))
        except ValueError:
            continue
    return found


_LINKS_JS = ("els => els.map(e => ({href: e.href, text: (e.innerText || e.getAttribute('aria-label') || "
             "(e.querySelector('img') && e.querySelector('img').alt) || '').slice(0, 300)}))")


def _short(exc):
    return " ".join(str(exc).split())[:160]


class PlaywrightBrowser:
    """The only code that talks to Playwright. Written from its documentation, not yet run live."""

    def __init__(self, page, debug_dir, pacing, hints, human=None):
        self.page = page
        self.debug_dir = Path(debug_dir)
        self.pacing = pacing
        self.human = human                                    # a Human (see human.py) for a logged-in session, else None: the plain pauses and jumps below
        self.hints = hints
        self._json, self._urls = [], []
        # Set by open_browser when the stealth layer is on: the account's
        # pinned identity, and the log of what its reads looked like.
        self.identity = None
        self.read_log = None
        self._current_read = None
        self._last_url = None
        page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            url = response.url
            if self.hints and not any(h in url for h in self.hints):
                return
            body = response.text()
            if len(body) > 5_000_000:
                return
        except Exception:                                     # images, redirects, closed pages
            return
        found = parse_json_text(body)
        if found:
            self._json.extend(found)
            self._urls.append(url)

    @property
    def url(self):
        return self.page.url

    def open(self, url):
        self._json, self._urls = [], []
        if self.human is not None:
            self.human.before_page(self.pacing)
        else:
            time.sleep(random.uniform(self.pacing["min_delay_seconds"], self.pacing["max_delay_seconds"]))
        # A person arrives from somewhere: the page they were just on, when
        # there is one. A session that opens every page from nowhere is the
        # shape of a machine.
        goto = {"wait_until": "domcontentloaded", "timeout": 60000}
        if self._last_url and self._last_url != url:
            goto["referer"] = self._last_url
        try:
            self.page.goto(url, **goto)
        except Exception as exc:
            raise BrowserError(f"could not open {url}: {_short(exc)}") from exc
        self._last_url = url
        chars = 0
        if self.human is not None:
            try:
                chars = len(self.page.content() or "")
            except Exception:
                chars = 0
            # The dwell after the page opens is proportional to how much text
            # the page holds, so a long page is read longer than a short one.
            self.human.after_load(self.page, content_chars=chars)
        if self.read_log is not None:
            self._current_read = self.read_log.record(
                url, time.time(), time.time(), content_chars=chars,
                mouse_moves=self.human.moves if self.human is not None else 0,
                scroll_events=self.human.scrolls if self.human is not None else 0,
                pauses=self.human.pauses if self.human is not None else 0)

    def _note_read(self):
        """Refresh the current read-log entry with what the page's read has
        looked like so far (the scrolling happens after the page opens)."""
        if self.read_log is None or self._current_read is None:
            return
        human = self.human
        self.read_log.update(self._current_read, ended_at=time.time(),
                             mouse_moves=human.moves if human is not None else 0,
                             scroll_events=human.scrolls if human is not None else 0,
                             pauses=human.pauses if human is not None else 0)

    def finish_read_log(self):
        """Write the per-read log to the run's debug directory, if there is one. Never fails a run."""
        if self.read_log is None:
            return
        self._note_read()
        try:
            self.read_log.write_jsonl(str(self.debug_dir / "read-log.jsonl"))
        except Exception:
            pass

    def wait(self, seconds):
        self.page.wait_for_timeout(int(seconds * 1000))

    def scroll(self, times=1, pause=3):
        if self.human is not None:
            self.human.scroll(self.page, times, pause)
            self._note_read()
            return
        for _ in range(times):
            self.page.mouse.wheel(0, random.randint(1800, 2600))
            self.page.wait_for_timeout(int(pause * 1000 * random.uniform(0.8, 1.3)))
        self._note_read()

    def json_responses(self):
        return list(self._json)

    def embedded_json(self):
        return parse_embedded_json(self.html())

    def response_urls(self):
        return list(self._urls)

    def links(self, contains):
        try:
            found = self.page.eval_on_selector_all("a[href]", _LINKS_JS)
        except Exception:
            return []
        return [f for f in found if any(c in f.get("href", "") for c in contains)]

    def exists(self, selectors):
        for sel in selectors:
            try:
                if self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def click(self, selectors):
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=8000)
                    return True
            except Exception:
                continue
        return False

    def type_text(self, selectors, text, submit=False):
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    box = loc.first
                    box.click(timeout=8000)
                    # A logged-in session types one keystroke at a time, with
                    # the occasional typo that is noticed and corrected; long
                    # text is pasted, the way a person pastes a long prompt.
                    if self.human is not None:
                        self.human.type_like_person(box, text)
                    else:
                        box.fill(text)
                    if submit:
                        box.press("Enter")
                    return True
            except Exception:
                continue
        return False

    def form_facts(self):
        """What the page offers to type into (kind, type, name, placeholder; never a value), with its address (no query) and title.
        Evidence for fixing a login step that cannot find its box."""
        try:
            inputs = self.page.eval_on_selector_all(
                "input, textarea", "els => els.slice(0, 15).map(e => ({tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', "
                "id: e.id || '', placeholder: e.placeholder || '', autocomplete: e.autocomplete || '', aria: e.getAttribute('aria-label') || '', "
                "visible: !!(e.offsetWidth || e.offsetHeight)}))")
        except Exception:
            inputs = []
        try:
            title = self.page.title()
        except Exception:
            title = ""
        return {"url": str(self.url).split("?")[0][:120], "title": str(title)[:80], "inputs": inputs}

    def text(self, selectors):
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    return loc.last.inner_text(timeout=8000)
            except Exception:
                continue
        return ""

    def response_links(self, selectors):
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    return loc.last.locator("a[href^='http']").evaluate_all("els => els.map(e => e.href)")
            except Exception:
                continue
        return []

    def fetch_bytes(self, url, referer, max_bytes):
        """(status, body) of an address fetched with this browser's cookies, as the page itself would."""
        user_agent = self.identity["user_agent"] if self.identity is not None else _UA
        resp = self.page.context.request.get(url, headers={"Referer": referer, "User-Agent": user_agent}, timeout=90000)
        body = resp.body() if resp.status < 400 else b""
        return resp.status, (body if len(body) <= max_bytes else b"")

    def html(self):
        try:
            return self.page.content()
        except Exception:
            return ""

    def screenshot(self, name):
        """Save a picture and the page's HTML, for working out why something was not found."""
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.debug_dir / f"{name}.png"), full_page=True)
            (self.debug_dir / f"{name}.html").write_text(self.html(), encoding="utf-8")
            return str(self.debug_dir / f"{name}.png")
        except Exception:
            return ""


def _start_playwright():
    """Start Playwright's synchronous driver. This must happen once per process, not once per site: see _SharedPlaywright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserError("Playwright is not installed (pip install playwright, then "
                           "playwright install --with-deps chromium)") from exc
    return sync_playwright().start()


class _SharedPlaywright:
    """One Playwright for every browser a run opens.

    Playwright's synchronous API runs its own event loop inside the process, and only one such instance may be running at a time: starting a
    second one while the first is still running fails with "It looks like you are using Playwright Sync API inside the asyncio loop".
    A collection run keeps one browser open per site (Meta, TikTok, Instagram, Gemini) for the whole run, so each site gets its own Chromium
    from the SAME Playwright. It is started by the first browser that opens and stopped when the last one has closed."""

    def __init__(self):
        self.pw, self.users = None, 0

    def acquire(self, start):
        if self.users == 0:
            self.pw = start()                     # if this fails nothing is counted, so nothing is left to stop
        self.users += 1
        return self.pw

    def release(self):
        self.users -= 1
        if self.users == 0:
            pw, self.pw = self.pw, None
            pw.stop()


_SHARED = _SharedPlaywright()


def _close_browser(browser):
    """Close a browser, never failing the run for it."""
    if browser is None:
        return
    try:
        browser.close()
    except Exception:
        pass


def _close_context(context):
    """Close a persistent context (which closes its browser with it), never failing the run for it."""
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


# The private X server patchright's headed browser runs on, started when the
# first patchright browser opens and stopped when the last one closes.
_XVFB = {"instance": None}


def _start_patchright():
    """Start patchright's driver: its own Playwright, patched so the browser does not advertise that it is automated. The X server is started
    first when one is needed, so the browser can run headed (headed is what removes the headless tell); without an X server it runs headless."""
    from . import stealth as stealth_mod
    xvfb = stealth_mod.Xvfb()
    xvfb.start()
    _XVFB["instance"] = xvfb
    # The start attempt is logged plainly, so a run report can say whether
    # the patchright browser really ran headed: a stale "Waiting" view is
    # not the only thing that hides it — before this, nothing recorded
    # whether Xvfb had started at all.
    try:
        print(xvfb.status_line())
    except Exception:
        pass
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        xvfb.stop()
        _XVFB["instance"] = None
        raise BrowserError("patchright is not installed (pip install patchright, then patchright install chromium)") from exc
    return sync_playwright().start()


class _PatchrightPlaywright(_SharedPlaywright):
    """patchright's driver, held the same way as the plain one, with the X server stopped when the last browser closes."""

    def release(self):
        super().release()
        if self.users == 0:
            xvfb = _XVFB["instance"]
            _XVFB["instance"] = None
            if xvfb is not None:
                xvfb.stop()


_PATCHRIGHT = _PatchrightPlaywright()


def _try_stealth_engines(site, identity, stealth_cfg):
    """Try the stealth engines in order for one account and return (engine, context, closer), or (None, None, None) when none of them could
    be used — the caller then falls back to plain Playwright. A failed attempt stops whatever it started, so a later attempt can start a
    different driver. Only one synchronous Playwright may run at a time, so an engine whose driver cannot start while another is running
    simply fails its attempt and the next one is tried."""
    from . import stealth as stealth_mod
    candidates = stealth_mod.available_engines(stealth_cfg.get("engine", "auto"))
    last_error = None
    for engine in candidates:
        if engine == "plain":
            continue
        driver = None
        try:
            if engine == "camoufox":
                driver = _SHARED.acquire(_start_playwright)
                context = stealth_mod.launch_camoufox(driver, identity, stealth_cfg)
                return engine, context, lambda context=context: (_close_context(context), _SHARED.release())
            driver = _PATCHRIGHT.acquire(_start_patchright)
            context = stealth_mod.launch_patchright(driver, identity, stealth_cfg,
                                                    headless=not bool(os.environ.get("DISPLAY")),
                                                    xvfb=_XVFB["instance"])
            return engine, context, lambda context=context: (_close_context(context), _PATCHRIGHT.release())
        except Exception as exc:
            last_error = exc
            if driver is not None:
                if engine == "camoufox":
                    _SHARED.release()
                else:
                    _PATCHRIGHT.release()
            continue
    if last_error is not None:
        print(f"stealth: no stealth engine could be started for {site} ({type(last_error).__name__}); using plain Playwright")
    return None, None, None


def person_for(cookies, human_block, viewport=None):
    """The Human a browser session gets: one for a session logged in with an account (cookies), none for a public one, and none when the owner set `human: enabled: false`. The viewport is the account's pinned one when the stealth layer gives it, the old fixed size otherwise."""
    return Human(human_block, viewport=viewport) if cookies and resolve_human(human_block)["enabled"] else None


@contextmanager
def open_browser(site, cfg, pacing, cookies_text, debug_dir, headless=True, human=None, identities_path=None):
    """Start a browser (logged in with the exported cookies, if there are any) for one site. `human` is the `human:` block of browser.yaml: a LOGGED-IN session then browses like a person
    (human.py); a logged-out one (the public Meta Ads library) keeps its plain pauses, which have worked.

    With the stealth layer on (`stealth: enabled: true` in browser.yaml, the default), the session also gets the account's pinned identity — one
    user agent, viewport, timezone and proxy per account — and the best stealth engine available (Camoufox, then patchright headed under Xvfb,
    then plain Playwright). The engines degrade gracefully: a missing engine is skipped, and a failed launch falls to the next one. With the
    layer off, or when no stealth engine can be started, the plain path below runs exactly as it did before: one Chromium per site on the shared
    Playwright, no automation-hiding flag, --no-sandbox only. `identities_path` is for tests; runs use the identities file from browser.yaml.
    """
    cookies = parse_cookies(cookies_text)
    stealth_cfg = cfg.get("stealth")
    identity = None
    if stealth_cfg and stealth_cfg.get("enabled"):
        from . import stealth as stealth_mod
        identity = stealth_mod.resolve_identity(site, stealth_cfg, identities_path=identities_path)
    person = person_for(cookies, human, viewport=identity["viewport"] if identity is not None else None)
    browser, context, closer, engine = None, None, None, "plain"
    browser_obj = None
    try:
        if identity is not None:
            engine, context, closer = _try_stealth_engines(site, identity, stealth_cfg)
        if context is None:
            engine = "plain"
            try:
                pw = _SHARED.acquire(_start_playwright)
            except BrowserError:
                raise
            except Exception as exc:                      # the real reason is kept in the message, never replaced
                raise BrowserError(f"could not start Playwright for {site}: {_short(exc)}") from exc
            try:
                browser = pw.chromium.launch(headless=headless,
                                             args=["--no-sandbox"])
                if identity is not None:
                    # The plain engine still reads with the account's pinned
                    # identity (its own user agent, viewport, timezone and
                    # proxy), which is better than the old fixed values.
                    context = browser.new_context(viewport={"width": identity["viewport"][0], "height": identity["viewport"][1]},
                                                  user_agent=identity["user_agent"], timezone_id=identity["timezone_id"],
                                                  locale=identity["locale"], proxy=identity["proxy"])
                else:
                    context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US",
                                                  user_agent=_UA, timezone_id="America/New_York")
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                page.set_default_timeout(30000)
            except Exception as exc:
                _close_browser(browser)
                _SHARED.release()
                raise BrowserError(f"could not start the browser for {site}: {_short(exc)}") from exc
            closer = lambda browser=browser: (_close_browser(browser), _SHARED.release())
        else:
            # A stealth engine opened the context; the page still needs the
            # cookies and the timeout, the same as the plain path.
            try:
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                page.set_default_timeout(30000)
            except Exception as exc:
                closer()
                closer = None
                raise BrowserError(f"could not start the browser for {site}: {_short(exc)}") from exc
        if identity is not None:
            identity["engine"] = engine
        browser_obj = PlaywrightBrowser(page, Path(debug_dir) / site, pacing, cfg.get("json_url_hints", []), human=person)
        browser_obj.identity = identity
        # How the X server attempt went, when one was started for this run:
        # the read log and the run report can show it, instead of leaving
        # the headed/headless question unanswered.
        xvfb_instance = _XVFB["instance"]
        browser_obj.xvfb_status = getattr(xvfb_instance, "status", None) if xvfb_instance is not None else None
        if identity is not None and stealth_cfg.get("log_reads"):
            browser_obj.read_log = stealth_mod.ReadLog(site, engine, identity,
                                                       egress_ip=stealth_mod.resolve_egress_ip(page),
                                                       xvfb_status=browser_obj.xvfb_status)
        # Warm-up: visit the site's public video/profile pages and poll for
        # its clearance cookie before any listing page is opened (see
        # contentintel/warmup.py). It never fails the run, and with no
        # warmup: block in the site config it does nothing at all.
        try:
            from . import warmup as warmup_mod
            report = warmup_mod.warm_up(browser_obj, cfg)
        except Exception:
            report = None
        if report is not None and report.get("visited"):
            print(f"warm-up {site}: visited {len(report['visited'])} page(s), "
                  f"clearance token found: {'yes' if report['token_found'] else 'no'}")
        yield browser_obj
    finally:
        if browser_obj is not None:
            browser_obj.finish_read_log()
        if closer is not None:
            closer()
