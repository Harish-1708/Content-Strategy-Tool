"""A real (headless) browser, driven the way a person would use it.

The collectors never touch Playwright directly. They use the small interface below, so the
collection logic can be tested without a browser, and only this file knows about Playwright.
Nothing here is ever printed or saved that contains a cookie value or a password.
"""
import collections
import json
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

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
    "challenge_any": "strs", "challenge_dismiss": "strs", "error_any": "strs", "hashtag_via_search_on_error": "bool", "search_box": "strs",
    "warning_any": "strs", "warning_url_any": "strs",
    "pacing": "pace", "max_scrolls_per_page": "whole", "max_pages_per_run": "whole", "max_pages_per_day": "whole",
    "deep_research_active_any": "strs", "signed_out_any": "strs", "link_contains": "strs", "prompt_box": "strs", "send": "strs",
    "start_research": "strs", "still_working_any": "strs", "done_any": "strs", "response": "strs",
    "deep_research_clicks": "strs_list", "login_steps": "steps", "wait": "wait",
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
    elif kind == "strs":
        _strs(value, where)
    elif kind == "strs_list":
        if not isinstance(value, list) or not value:
            raise BrowserError(f"{where} must be a list of lists of selectors")
        for n, item in enumerate(value, start=1):
            _strs(item, f"{where} item {n}")
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
    extra = set(data) - {"pacing", "limits", "sites"}
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
    return {"pacing": pacing, "limits": limits, "sites": sites}


def site_pacing(cfg, site):
    """The pauses for one site: its own `pacing` over the general ones."""
    return {**cfg["pacing"], **cfg["sites"][site].get("pacing", {})}


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

    def __init__(self, page, debug_dir, pacing, hints):
        self.page = page
        self.debug_dir = Path(debug_dir)
        self.pacing = pacing
        self.hints = hints
        self._json, self._urls = [], []
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
        time.sleep(random.uniform(self.pacing["min_delay_seconds"], self.pacing["max_delay_seconds"]))
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            raise BrowserError(f"could not open {url}: {_short(exc)}") from exc

    def wait(self, seconds):
        self.page.wait_for_timeout(int(seconds * 1000))

    def scroll(self, times=1, pause=3):
        for _ in range(times):
            self.page.mouse.wheel(0, random.randint(1800, 2600))
            self.page.wait_for_timeout(int(pause * 1000 * random.uniform(0.8, 1.3)))

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
                    box.fill(text)
                    if submit:
                        box.press("Enter")
                    return True
            except Exception:
                continue
        return False

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


@contextmanager
def open_browser(site, cfg, pacing, cookies_text, debug_dir, headless=True):
    """Start Chromium (logged in with the exported cookies, if there are any) for one site."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserError("Playwright is not installed (pip install playwright, then "
                           "playwright install --with-deps chromium)") from exc
    cookies = parse_cookies(cookies_text)
    pw, browser = sync_playwright().start(), None
    try:
        try:
            browser = pw.chromium.launch(headless=headless,
                                         args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US",
                                          user_agent=_UA, timezone_id="America/New_York")
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(30000)
        except Exception as exc:
            raise BrowserError(f"could not start the browser for {site}: {_short(exc)}") from exc
        yield PlaywrightBrowser(page, Path(debug_dir) / site, pacing, cfg.get("json_url_hints", []))
    finally:
        try:
            if browser:
                browser.close()
        finally:
            pw.stop()
