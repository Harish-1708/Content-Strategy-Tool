"""Video trial harness: one clip per site, go/no-go each, on dummy accounts.

The free video generation that exists lives in consumer web apps, not APIs, so
the only way to test it is a logged-in browser. This module drives that test,
one clip per site, through the same cookie-session method as the logged-in
sessions (contentintel/browser.py + contentintel/stealth.py): the dummy
account's cookies are the session, the account keeps one pinned identity, the
browser paces like a person, and every action is written to a per-run log with
a human score.

What it is NOT: a comparison across providers (each site's trial stands on
its own), a production lane, or a way to publish anything. Output is a draft
only — watermarked, personal-use, publishing OFF.

What it never does: create an account, log in with a password on a run, spend
money, generate more than ONE clip per site per run, or touch the developer's
files. It plugs into his code the way an external connection does: it imports
``contentintel.browser`` (open_browser, parse_cookies, describe_cookies) and
``contentintel.stealth`` (resolve, resolve_identity, ReadLog, human_score) and
``contentintel.human`` (resolve), reads the pacing/human/stealth blocks of
browser.yaml, and calls them — it never reimplements session handling, browser
launch or human pacing, and it never edits browser.yaml (its ``sites`` must
stay exactly the four the developer lists; this module builds its own minimal
per-site config instead of calling load_browser_settings).

Selectors and markers below are FIRST GUESSES from the per-site research
(hidden_files/video-trial-sites-research.md, read from each vendor's own
official pages on 2026-11-17), the way browser.yaml's own header says its
addresses and button names are first guesses: the first live run is expected
to correct them, and the run says plainly what it could not find. Facts the
research could only get from a third-party guide are marked "reported" and
must be confirmed in the logged-in session.

Run it: ``python -m contentintel.video_trial --site all --dry-run`` first
(plan only, no browser), then without --dry-run for the real trial. One clip
per site, hard cap, enforced in code (ClipBudget).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import browser as browser_mod
from . import human as human_mod
from . import stealth as stealth_mod

# One clip per site per run. Not a default to tune: the trial is one clip, and
# a second generation from the same account in the same run is the shape of
# hammering a free tier, not testing it.
MAX_CLIPS_PER_SITE = 1
# A generation takes minutes, not seconds. Poll for a finished clip this long
# before calling it a no-go; a site that has not finished by then has failed
# the trial, however good its model is.
DEFAULT_MAX_WAIT_SECONDS = 600
DEFAULT_POLL_SECONDS = 30
# A clip download is a few megabytes at most on these free tiers; anything
# larger than this is not a clip.
MAX_CLIP_BYTES = 200_000_000
# The prompt used when none is given: brand-neutral, so the trial tests the
# mechanism, not a brand.
DEFAULT_PROMPT = ("A red bicycle leaning against a wooden fence in a sunny "
                  "garden, gentle breeze, steady camera")
# The log and verdicts never hold a cookie value, a password or a secret
# value — only secret NAMES and counts, the way describe_cookies does it.


class TrialError(Exception):
    """Something is wrong with the trial itself (config, budget, input); the
    message says what a person can do. A site that fails its trial is a
    verdict, not this."""


# ---------------------------------------------------------------------------
# The drivers: one entry per site. "reported" facts come from a third-party
# guide, not the vendor's own page, and are confirmed only in the logged-in
# session. Selectors are first guesses, to be corrected on the first live run.
# ---------------------------------------------------------------------------

DRIVERS = {
    "pixverse": {
        "label": "PixVerse",
        "model": "V6",
        "app_url": "https://app.pixverse.ai",
        "cookies_secret": "PIXVERSE_COOKIES",
        "free": {"credits": "60 initial + 30 daily, renew 00:00 UTC, expire same day",
                 "resolution": "540p cap", "watermarked": True, "commercial": False},
        "flow_notes": [
            "prompt box is the main entry; defaults V6, 540p, 16:9, 5s",
            "duration 1-15s (default 5s); 2 concurrent generations",
            "generations land in the Library; download from there",
        ],
        "prompt_selectors": ["textarea", "textarea[placeholder*=\"prompt\" i]"],
        "model_steps": [],            # V6 is the app default; nothing to select
        "generate_selectors": ["button:has-text(\"Generate\")", "text=Generate"],
        "done_markers": ["video[src]", "a[href$=\".mp4\"]", "text=Download"],
        "download_link_contains": [".mp4"],
        "logged_in_markers": ["library", "my creations", "generate"],
        "login_wall_markers": ["log in", "sign in", "create an account"],
        "challenge_markers": ["captcha", "verify to continue", "confirm it's you"],
        "warning_markers": ["account has been suspended", "temporarily locked", "automated behavior"],
        "credits_markers": ["not enough credits", "insufficient credits", "out of credits"],
        "max_wait_seconds": 600,
    },
    "google_flow": {
        "label": "Google Flow",
        "model": "Veo 3.1 Lite",
        "app_url": "https://flow.google.com",
        "cookies_secret": "GOOGLE_FLOW_COOKIES",
        "free": {"credits": "50/day, no rollover, refresh triggered by first generation",
                 "resolution": "720p", "watermarked": True, "commercial": False},
        "flow_notes": [
            "Google account required, 18+, Chromium browser",
            "prompt box defaults to an IMAGE model: click the model name and choose Video + Veo 3.1 Lite",
            "cost is shown before generation; generations appear as tiles",
            "non-subscribers are blocked from video generation ~2-5 PM UTC",
            "Veo 3.1 Fast/Quality retire 2026-12-05; Lite survives",
        ],
        "prompt_selectors": ["textarea", "div[contenteditable=\"true\"]", "div[role=\"textbox\"]"],
        "model_steps": [
            ["button:has-text(\"Veo\")", "text=Veo 3.1"],
            ["text=Veo 3.1 Lite", "button:has-text(\"Lite\")"],
        ],
        "generate_selectors": ["button:has-text(\"Generate\")", "text=Generate"],
        "done_markers": ["video[src]", "a[href$=\".mp4\"]", "text=Download"],
        "download_link_contains": [".mp4"],
        "logged_in_markers": ["new project", "my projects", "create"],
        "login_wall_markers": ["sign in", "create your google account"],
        "challenge_markers": ["unusual activity", "captcha", "verify it's you"],
        "warning_markers": ["account has been suspended", "temporarily locked", "automated behavior"],
        "credits_markers": ["not enough credits", "out of credits", "credit limit"],
        "max_wait_seconds": 600,
        # Non-subscribers are blocked from video generation in these UTC
        # hours (Google's own help page). The runner skips the site with a
        # plain reason instead of burning a generation on the block.
        "peak_hours_utc": (14, 15, 16),
    },
    "dreamina": {
        "label": "Dreamina",
        "model": "Seedance 2.0",
        "models": ["Seedance 2.0", "Seedance Mini"],
        # Seedance 2.5 costs 37 credits/sec at 720p: a 5s clip is 185 credits,
        # more than the reported 120/day free allowance. Free credits cannot
        # buy one 2.5 clip, so 2.5 is forbidden here, not a fallback.
        "forbidden_models": ["Seedance 2.5"],
        "app_url": "https://dreamina.capcut.com",
        "cookies_secret": "DREAMINA_COOKIES",
        "free": {"credits": "reported 120/day, no rollover (amount NOT stated on the official page)",
                 "resolution": "720p", "watermarked": True, "commercial": False},
        "flow_notes": [
            "open the Video tab, pick the model, set aspect 9:16/16:9 and length 5-10s",
            "exact price is shown on the Generate button before you commit",
            "Seedance 2.0 is 17 credits/sec, Mini 5/sec; 2.5 is 37/sec and is NOT tried",
            "preview the result, then download it",
        ],
        "prompt_selectors": ["textarea", "div[contenteditable=\"true\"]"],
        "model_steps": [
            ["text=Seedance 2.0", "button:has-text(\"Seedance 2.0\")"],
        ],
        "generate_selectors": ["button:has-text(\"Generate\")", "text=Generate"],
        "done_markers": ["video[src]", "a[href$=\".mp4\"]", "text=Download"],
        "download_link_contains": [".mp4"],
        "logged_in_markers": ["video", "my creations", "generate"],
        "login_wall_markers": ["log in", "sign in"],
        "challenge_markers": ["captcha", "verify to continue", "confirm it's you"],
        "warning_markers": ["account has been suspended", "temporarily locked", "automated behavior"],
        "credits_markers": ["not enough credits", "insufficient credits", "out of credits"],
        "max_wait_seconds": 600,
    },
    "kling": {
        "label": "Kling",
        "model": "Kling 4.0",
        "app_url": "https://kling.ai",
        "cookies_secret": "KLING_COOKIES",
        "free": {"credits": "monthly promotional credits, amount unpublished (reported 66/month + 3 trials)",
                 "resolution": "720p only (1080p is premium-only)", "watermarked": True, "commercial": False},
        "flow_notes": [
            "creation workspace; Omni Reference is the default entry point",
            "prompt up to 8000 tokens; Generation Mode 720p; duration 3-30s (default 10s)",
            "cost is shown on the Generate button; credits are deducted at once, refunded on failure",
            "1 video per generation on Basic (reported)",
        ],
        "prompt_selectors": ["textarea", "textarea[placeholder*=\"prompt\" i]"],
        "model_steps": [],            # Omni Reference is the default entry; nothing to select
        "generate_selectors": ["button:has-text(\"Generate\")", "text=Generate"],
        "done_markers": ["video[src]", "a[href$=\".mp4\"]", "text=Download"],
        "download_link_contains": [".mp4"],
        "logged_in_markers": ["create", "my works", "credits"],
        "login_wall_markers": ["log in", "sign in", "sign up"],
        "challenge_markers": ["captcha", "verify to continue", "confirm it's you"],
        "warning_markers": ["account has been suspended", "temporarily locked", "automated behavior"],
        "credits_markers": ["not enough credits", "insufficient credits", "out of credits"],
        "max_wait_seconds": 600,
    },
    "luma": {
        # STUB ONLY. Luma states no free plan and no trial-credit amount
        # anywhere on its pages — the amount is only visible after sign-in —
        # and its trial is capped at 14 days, evaluation-only, with the
        # strongest anti-automation terms of the eight. The driver is not
        # built until Harish signs in and reports the trial-credit amount.
        # The runner fails this site with "needs sign-in check" and never
        # opens a browser or reads a cookie for it.
        "label": "Luma Dream Machine",
        "model": "Ray3.2",
        "stub": True,
        "app_url": "https://app.lumalabs.ai",
        "cookies_secret": "LUMA_COOKIES",
        "free": {"credits": "unknown until sign-in (no free plan on the pricing page)",
                 "resolution": "unknown", "watermarked": True, "commercial": False},
        "flow_notes": ["STUB: sign in by hand and report the trial-credit amount first"],
        "prompt_selectors": [],
        "model_steps": [],
        "generate_selectors": [],
        "done_markers": [],
        "download_link_contains": [".mp4"],
        "logged_in_markers": [],
        "login_wall_markers": ["log in", "sign in"],
        "challenge_markers": ["captcha", "verify to continue"],
        "warning_markers": ["account has been suspended", "temporarily locked", "automated behavior"],
        "credits_markers": ["not enough credits", "insufficient credits"],
        "max_wait_seconds": 600,
    },
}

SITE_KEYS = tuple(k for k in DRIVERS if not DRIVERS[k].get("stub"))
ALL_KEYS = tuple(DRIVERS)


# ---------------------------------------------------------------------------
# Settings: the pacing/human/stealth blocks of browser.yaml, read directly.
# load_browser_settings is NOT used: it requires browser.yaml's sites to be
# exactly the four the developer lists, and this module adds no site there.
# ---------------------------------------------------------------------------

def load_trial_settings(path="browser.yaml"):
    """{"pacing", "human", "stealth"} from browser.yaml, resolved the same
    way the developer's code resolves them. Raises TrialError with a plain
    reason when the file is missing or a block is wrong."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise TrialError(f"{path} is missing; it holds the pacing, human and stealth blocks this trial runs on") from None
    except (OSError, ValueError) as exc:
        raise TrialError(f"{path} could not be read ({exc})") from None
    pacing = data.get("pacing") or {}
    for key in ("min_delay_seconds", "max_delay_seconds", "scroll_pause_seconds"):
        value = pacing.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise TrialError(f"browser.yaml pacing.{key} must be a number of at least 0")
    if pacing["min_delay_seconds"] > pacing["max_delay_seconds"]:
        raise TrialError("browser.yaml pacing.min_delay_seconds cannot be more than max_delay_seconds")
    try:
        human = human_mod.resolve(data.get("human"))
    except human_mod.HumanConfigError as exc:
        raise TrialError(str(exc)) from exc
    try:
        stealth = stealth_mod.resolve(data.get("stealth"))
    except stealth_mod.StealthError as exc:
        raise TrialError(str(exc)) from exc
    return {"pacing": pacing, "human": human, "stealth": stealth}


def site_browser_cfg(stealth_cfg):
    """The per-site config open_browser needs. Only the stealth block and the
    JSON hints: no "warmup" key, so warm-up is a no-op for these sites, and no
    browser.yaml site entry, so nothing the developer owns is touched."""
    return {"stealth": stealth_cfg, "json_url_hints": []}


def check_cookies(key, env=None):
    """(state, line) for one site's cookies secret, from the secret's name
    and counts only — never a value. "ok" means cookies are present and
    parse; "not-set-up" means the secret is missing, unreadable or holds no
    usable cookie (capture again by hand)."""
    driver = DRIVERS[key]
    env = os.environ if env is None else env
    raw = (env.get(driver["cookies_secret"]) or "").strip()
    if not raw:
        return "not-set-up", (f"no cookies secret is set ({driver['cookies_secret']}): "
                              "capture the dummy account's cookies by hand and paste them into that secret")
    try:
        browser_mod.parse_cookies(raw)
    except browser_mod.BrowserError as exc:
        return "not-set-up", (f"the cookies secret ({driver['cookies_secret']}) could not be read "
                              f"({exc}): capture again by hand")
    return "ok", browser_mod.describe_cookies(raw, site=key)


# ---------------------------------------------------------------------------
# The per-run log and the clip budget
# ---------------------------------------------------------------------------

class TrialLog:
    """One entry per trial action, written as JSONL. Same shape as the
    stealth layer's per-read log (site, timing, human score), so the two logs
    read the same way. Nothing secret is ever recorded: no cookie value, no
    password, no secret value — only secret names and counts."""

    def __init__(self):
        self.entries = []

    def record(self, site, action, started_at, ended_at, content_chars=0,
               mouse_moves=0, scroll_events=0, pauses=0, error=None):
        dwell = max(ended_at - started_at, 0.0)
        entry = {
            "site": site,
            "action": action,
            "started_at": started_at,
            "ended_at": ended_at,
            "dwell_seconds": round(dwell, 2),
            "content_chars": content_chars,
            "mouse_moves": mouse_moves,
            "scroll_events": scroll_events,
            "pauses": pauses,
            "human_score": stealth_mod.human_score(dwell, content_chars, mouse_moves,
                                                   scroll_events, pauses),
        }
        if error:
            entry["error"] = str(error)[:200]
        self.entries.append(entry)
        return entry

    def mean_human_score(self, site=None):
        scores = [e["human_score"] for e in self.entries
                  if site is None or e["site"] == site]
        return round(sum(scores) / len(scores)) if scores else None

    def write_jsonl(self, path):
        """Write the entries as JSONL. Nothing is written when nothing was
        recorded (an empty file would never reach the repository: GitHub's web
        upload skips empty files). Returns False when nothing was written."""
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


class ClipBudget:
    """The hard cap, enforced in code: one clip per site per run. A second
    generation from the same account in the same run is refused before the
    button is pressed, so a retry or a bug can never double-spend free
    credits."""

    def __init__(self, limit=MAX_CLIPS_PER_SITE):
        self.limit = limit
        self.spent = {}

    def allow(self, site):
        return self.spent.get(site, 0) < self.limit

    def record(self, site):
        """Spend one generation for a site. Raises TrialError when the site's
        cap is already spent — call this BEFORE pressing Generate."""
        if not self.allow(site):
            raise TrialError(f"{site}: the clip budget is spent ({self.limit} clip per site per run); "
                             "a second generation is refused")
        self.spent[site] = self.spent.get(site, 0) + 1


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

VERDICTS = ("go", "no-go", "blocked", "skipped", "not-set-up")


def verdict(site, state, reason, clip=None, human_score=None, seconds=0.0):
    """One site's trial result. "go" means one clip was made and downloaded;
    "no-go" means the trial ran and the site failed it; "blocked" means a
    challenge or an account warning stopped it; "skipped" means it was not
    attempted (dry run, peak hours, stub); "not-set-up" means no usable
    cookies. A no-go is a successful trial finding, not a run failure."""
    if state not in VERDICTS:
        raise TrialError(f"unknown verdict {state!r}")
    return {"site": site, "model": DRIVERS[site]["model"], "verdict": state,
            "reason": reason, "clip": clip, "human_score": human_score,
            "seconds": round(seconds, 1)}


def verdict_line(entry):
    """One plain line for the run log, the way the tool's other runs print."""
    state = entry["verdict"].upper()
    line = f"video-trial: {entry['site']}: {state} — {entry['reason']}"
    if entry.get("clip"):
        line += f" (clip: {entry['clip']})"
    return line


# ---------------------------------------------------------------------------
# Page checks: is the session logged in, and did the site challenge or warn it?
# The login-wall markers win over the logged-in markers (a signed-out page
# can still show a prompt box in its code — the Gemini lesson in
# browser.yaml): any doubt counts as not logged in.
# ---------------------------------------------------------------------------

def _page_text(browser):
    """The page's visible text when the browser can give it, else its HTML.
    Lower-cased, for marker matching."""
    try:
        text = browser.text(["body"]) or ""
    except Exception:
        text = ""
    if text:
        return text.lower()
    try:
        return (browser.html() or "").lower()
    except Exception:
        return ""


def _page_html(browser):
    try:
        return (browser.html() or "").lower()
    except Exception:
        return ""


def logged_in_state(browser, driver):
    """("in", "") when the page shows the logged-in site, ("wall", reason)
    when it shows a login page, ("unknown", reason) when neither is shown.
    Any doubt counts as not logged in."""
    page = _page_text(browser)
    html = _page_html(browser)
    for marker in driver["login_wall_markers"]:
        if marker.lower() in page or marker.lower() in html:
            return "wall", f"the site shows its login page ({marker})"
    for marker in driver["logged_in_markers"]:
        if marker.lower() in page or marker.lower() in html:
            return "in", ""
    return "unknown", "no sign of being logged in was found on the page"


def challenge_or_warning(browser, driver):
    """("challenge", reason) or ("warning", reason) when the page shows one,
    else None. A challenge or a warning stops the site's trial at once — using
    the account again straight away is what turns a warning into a ban."""
    html = _page_html(browser)
    page = _page_text(browser)
    for marker in driver["challenge_markers"]:
        if marker.lower() in html or marker.lower() in page:
            return "challenge", f"the site asked for a check ({marker}); a person must clear it"
    for marker in driver["warning_markers"]:
        if marker.lower() in html or marker.lower() in page:
            return "warning", f"the site warned the account ({marker}); the account rests"
    return None


def credits_exhausted(browser, driver):
    """True when the page says the free credits are gone."""
    html = _page_html(browser)
    page = _page_text(browser)
    return any(m.lower() in html or m.lower() in page for m in driver["credits_markers"])


def _human_counts(browser):
    """What the page's read has looked like so far, for the trial log. A
    real browser carries a Human; a test fake does not."""
    human = getattr(browser, "human", None)
    if human is None:
        return 0, 0, 0
    return getattr(human, "moves", 0), getattr(human, "scrolls", 0), getattr(human, "pauses", 0)


def _short(exc):
    return " ".join(str(exc).split())[:160]


# ---------------------------------------------------------------------------
# One site's trial
# ---------------------------------------------------------------------------

class TrialContext:
    """Everything a site's trial needs, so tests can replace the browser and
    the clock without touching a real site."""

    def __init__(self, settings, env, out_dir, prompt, log, budget,
                 browser_factory=None, now=None,
                 max_wait_seconds=None, poll_seconds=DEFAULT_POLL_SECONDS):
        self.settings = settings
        self.env = env
        self.out_dir = Path(out_dir)
        self.prompt = prompt
        self.log = log
        self.budget = budget
        self.browser_factory = browser_factory or real_browser_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_wait_seconds = max_wait_seconds
        self.poll_seconds = poll_seconds


def real_browser_factory(ctx, key, cookies_text):
    """The real browser for one site, through the developer's open_browser:
    the stealth identity, the human pacing and the per-read log come from his
    code, not from here. The returned value is a context manager."""
    cfg = site_browser_cfg(ctx.settings["stealth"])
    return browser_mod.open_browser(key, cfg, ctx.settings["pacing"], cookies_text,
                                    str(ctx.out_dir / key), human=ctx.settings["human"])


def _record(ctx, site, action, started_at, browser=None, error=None):
    moves, scrolls, pauses = _human_counts(browser) if browser is not None else (0, 0, 0)
    try:
        chars = len(browser.html() or "") if browser is not None else 0
    except Exception:
        chars = 0
    return ctx.log.record(site, action, started_at, time.time(), content_chars=chars,
                          mouse_moves=moves, scroll_events=scrolls, pauses=pauses,
                          error=error)


def _pick_model(ctx, browser, driver):
    """The first selectable model from the driver's list. A forbidden model
    is never picked, even when it is the only one on the page: free credits
    cannot buy it, so picking it would spend the trial on a paid model."""
    for step in driver["model_steps"]:
        if browser.click(step):
            return True
    if not driver["model_steps"]:
        return True
    return False


def _wait_for_done(ctx, browser, driver, site):
    """Poll for a finished clip. Returns ("done", "") or ("timeout", reason).
    A challenge or a warning found while waiting stops the poll at once."""
    limit = driver["max_wait_seconds"] if ctx.max_wait_seconds is None else ctx.max_wait_seconds
    waited = 0.0
    while waited < limit:
        found = challenge_or_warning(browser, driver)
        if found is not None:
            return found[0], found[1]
        if browser.exists(driver["done_markers"]):
            return "done", ""
        browser.wait(ctx.poll_seconds)
        waited += ctx.poll_seconds
    return "timeout", f"generation did not finish within {int(limit // 60)} minutes"


def _download_clip(ctx, browser, driver, site):
    """Download the finished clip with the session's own cookies. Returns the
    saved path (relative to the run directory) or None when no clip could be
    found or fetched."""
    links = browser.links(driver["download_link_contains"])
    for link in links:
        url = link.get("href", "")
        if not url.startswith("https://"):
            continue
        status, body = browser.fetch_bytes(url, driver["app_url"], MAX_CLIP_BYTES)
        if status < 400 and body:
            site_dir = ctx.out_dir / site
            site_dir.mkdir(parents=True, exist_ok=True)
            path = site_dir / "clip.mp4"
            path.write_bytes(body)
            return str(path.relative_to(ctx.out_dir))
    return None


def run_site(key, ctx):
    """One site's trial. Returns a verdict dict. Never raises for a site
    that fails its trial — a failed trial is a verdict; only a broken run
    raises TrialError."""
    driver = DRIVERS[key]
    started = time.time()
    log = ctx.log

    def finish(state, reason, clip=None):
        seconds = time.time() - started
        return verdict(key, state, reason, clip=clip,
                       human_score=log.mean_human_score(key), seconds=seconds)

    # The stub: no browser is opened and no cookie is read for Luma. Its
    # trial-credit amount is unknown until Harish signs in and reports it.
    if driver.get("stub"):
        log.record(key, "stub", started, time.time(),
                   error="needs sign-in check: no free plan stated, trial-credit amount only visible after sign-in")
        return finish("skipped", "needs sign-in check: sign in by hand and report the trial-credit amount first")

    # Peak hours (Google Flow): non-subscribers are blocked from video
    # generation in these UTC hours. Skip with a plain reason instead of
    # burning a generation on the block.
    peak = driver.get("peak_hours_utc")
    if peak is not None and ctx.now().hour in peak:
        log.record(key, "peak-hours", started, time.time(),
                   error="peak hours for non-subscribers (~2-5 PM UTC)")
        return finish("skipped", "peak hours for non-subscribers (~2-5 PM UTC); run outside that window")

    # Cookies first: a missing or unreadable secret is "not set up", never a
    # browser that opens and fails.
    state, line = check_cookies(key, ctx.env)
    if state != "ok":
        log.record(key, "cookies", started, time.time(), error=line)
        return finish("not-set-up", line)
    cookies_text = (ctx.env.get(driver["cookies_secret"]) or "").strip()

    try:
        with ctx.browser_factory(ctx, key, cookies_text) as browser:
            opened_at = time.time()
            browser.open(driver["app_url"])
            _record(ctx, key, "open-app", opened_at, browser)
            found = challenge_or_warning(browser, driver)
            if found is not None:
                kind, reason = found
                browser.screenshot("challenge-or-warning")
                return finish("blocked", reason)
            login, reason = logged_in_state(browser, driver)
            if login != "in":
                if login == "wall":
                    detail = "cookies missing or expired — capture a new session by hand"
                else:
                    detail = reason
                browser.screenshot("login-wall")
                return finish("not-set-up", detail)
            if not _pick_model(ctx, browser, driver):
                return finish("no-go", "the free model could not be selected "
                              f"({', '.join(driver.get('models', [driver['model']]))}); "
                              "the page may have changed — the selectors are first guesses")
            typed_at = time.time()
            if not browser.type_text(driver["prompt_selectors"], ctx.prompt):
                return finish("no-go", "the prompt box could not be found; "
                              "the selectors are first guesses — correct them from the debug capture")
            _record(ctx, key, "type-prompt", typed_at, browser)
            # The budget is spent BEFORE Generate is pressed, so a retry or
            # a bug can never make a second clip.
            ctx.budget.record(key)
            gen_at = time.time()
            if not browser.click(driver["generate_selectors"]):
                return finish("no-go", "the Generate button could not be found; "
                              "the selectors are first guesses — correct them from the debug capture")
            _record(ctx, key, "generate", gen_at, browser)
            done, reason = _wait_for_done(ctx, browser, driver, key)
            if done == "challenge":
                browser.screenshot("challenge-or-warning")
                return finish("blocked", reason)
            if done == "warning":
                browser.screenshot("challenge-or-warning")
                return finish("blocked", reason)
            if done == "timeout":
                if credits_exhausted(browser, driver):
                    return finish("no-go", "the free credits are exhausted")
                browser.screenshot("generation-timeout")
                return finish("no-go", reason)
            clip = _download_clip(ctx, browser, driver, key)
            if clip is None:
                browser.screenshot("no-download")
                return finish("no-go", "generation finished but no clip could be downloaded")
            return finish("go", f"one {driver['model']} clip made and downloaded", clip=clip)
    except browser_mod.LoginWall as exc:
        return finish("not-set-up", f"the site shows its login page ({_short(exc)}); "
                      "cookies missing or expired — capture a new session by hand")
    except browser_mod.AccountWarning as exc:
        return finish("blocked", f"{_short(exc)}; the account rests")
    except browser_mod.Challenge as exc:
        return finish("blocked", f"{_short(exc)}; a person must clear it")
    except browser_mod.BrowserError as exc:
        return finish("no-go", _short(exc))
    except TrialError as exc:
        # The budget refusing a second clip is a no-go for this site, not a
        # broken run: the trial's question is answered (the cap held).
        return finish("no-go", str(exc))


# ---------------------------------------------------------------------------
# The run: plan (dry run) or trial
# ---------------------------------------------------------------------------

def plan_lines(key):
    """The dry-run plan for one site: model, free limits, flow, and what is
    still unknown. No browser is opened."""
    driver = DRIVERS[key]
    free = driver["free"]
    lines = [f"  {key} ({driver['label']}, {driver['model']}):"]
    lines.append(f"    free: {free['credits']}; {free['resolution']}; "
                 f"{'watermarked' if free['watermarked'] else 'no watermark'}; "
                 f"{'commercial' if free['commercial'] else 'non-commercial'}")
    for note in driver["flow_notes"]:
        lines.append(f"    - {note}")
    if driver.get("stub"):
        lines.append("    STUB: no driver built until the sign-in check reports the trial credits")
    return lines


def run_trial(keys, prompt, settings, env, out_dir, verdicts_path=None,
              dry_run=False, browser_factory=None,
              now=None, max_wait_seconds=None, poll_seconds=DEFAULT_POLL_SECONDS):
    """Run the trial for the given sites. Returns (verdicts, exit_code).
    Exit 0 when every site got a verdict — a no-go is a successful trial
    finding, not a run failure. Exit 1 only when the run itself broke (the
    verdicts could not be written). A missing cookies secret is "not-set-up",
    never a failure, the way the sessions workflow treats it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = TrialLog()
    budget = ClipBudget()
    ctx = TrialContext(settings, env, out_dir, prompt, log, budget,
                       browser_factory=browser_factory,
                       now=now, max_wait_seconds=max_wait_seconds,
                       poll_seconds=poll_seconds)
    verdicts = []
    if dry_run:
        print("video-trial: dry run — the plan only, no browser is opened")
        for key in keys:
            for line in plan_lines(key):
                print(line)
            state, line = check_cookies(key, env)
            print(f"  cookies ({DRIVERS[key]['cookies_secret']}): {line}")
            verdicts.append(verdict(key, "skipped", "dry run — no browser opened"))
    else:
        for key in keys:
            entry = run_site(key, ctx)
            verdicts.append(entry)
            print(verdict_line(entry))
    log_path = str(out_dir / "trial-log.jsonl")
    log.write_jsonl(log_path)
    mean = log.mean_human_score()
    # Only when something was actually browsed: a cookies check records an
    # entry with no dwell, and a "mean human score 0" for that would be noise.
    if mean is not None and any(e["dwell_seconds"] > 0 for e in log.entries):
        print(f"video-trial: mean human score {mean} over {len(log.entries)} logged action(s)")
    result = {"run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "prompt": prompt, "dry_run": dry_run,
              "verdicts": verdicts}
    target = Path(verdicts_path) if verdicts_path else out_dir / "video_trial.json"
    try:
        if target.parent != Path("."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"video-trial: could not write the verdicts to {target} ({exc})")
        return verdicts, 1
    print(f"video-trial: verdicts written to {target}")
    return verdicts, 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m contentintel.video_trial",
        description="One clip per site, go/no-go each, on dummy accounts through the existing "
                    "cookie-session method. Drafts only, publishing OFF, free tiers only.")
    parser.add_argument("--site", choices=["all"] + list(ALL_KEYS), default="all",
                        help="one site, or all of them (each is judged on its own either way)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="the prompt for the one clip (default: a brand-neutral scene)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and the cookie checks only; never open a browser")
    parser.add_argument("--out", default="debug/video-trial",
                        help="where the clips, the trial log and the verdicts go")
    parser.add_argument("--verdicts", default=None,
                        help="where to write video_trial.json (default: <out>/video_trial.json)")
    parser.add_argument("--browser-config", default="browser.yaml",
                        help="the browser.yaml that holds the pacing, human and stealth blocks")
    parser.add_argument("--max-wait-seconds", type=int, default=None,
                        help="how long to poll for a finished clip (default: each driver's own limit)")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS,
                        help="how often to check whether the clip is finished")
    args = parser.parse_args(argv)
    if not args.prompt.strip():
        print("video-trial: the prompt is empty; give one with --prompt")
        return 1
    if args.max_wait_seconds is not None and args.max_wait_seconds < 1:
        print("video-trial: --max-wait-seconds must be at least 1")
        return 1
    if args.poll_seconds < 1:
        print("video-trial: --poll-seconds must be at least 1")
        return 1
    try:
        settings = load_trial_settings(args.browser_config)
    except TrialError as exc:
        print(f"video-trial: {exc}")
        return 1
    keys = list(ALL_KEYS) if args.site == "all" else [args.site]
    _, code = run_trial(keys, args.prompt, settings, os.environ, args.out,
                        verdicts_path=args.verdicts, dry_run=args.dry_run,
                        max_wait_seconds=args.max_wait_seconds,
                        poll_seconds=args.poll_seconds)
    return code


if __name__ == "__main__":
    sys.exit(main())
