"""Warm-up visits before a logged-in session reads a site.

TikTok's WAF mints a clearance cookie (``_waftokenid``) that is scoped per
page kind: a token minted on a video page clears only video pages, and one
minted on a profile page clears only profile pages, while the homepage mints
no token at all. A run that deep-links straight to a listing page therefore
arrives with no clearance for that kind of page. Warm-up visits one public
video page and one public profile page first, waits a few seconds on each
(the challenge finishes a few seconds after the page loads), and polls the
cookie jar for the clearance cookie — before any listing page is opened.

It never solves anything and never logs in: it only opens public pages. It
never fails a run: a page that is gone, a token that never appears, or any
other error is logged plainly and the run carries on. It is on by default
when a site configures warm-up URLs, and the owner turns it off per site
with ``warmup.enabled: false``. With no warm-up block, or no URLs in it,
this is a no-op.

The token name is configurable per site because not every site mints the
same one: TikTok mints ``_waftokenid``; Instagram has no WAF clearance
cookie, so its block polls for ``csrftoken`` instead, which only confirms
the session cookies are present after a real page load.
"""
import time

DEFAULT_TOKEN = "_waftokenid"
DEFAULT_WAIT_SECONDS = 5
DEFAULT_POLL_SECONDS = 10


def _log(log, message):
    """One plain line, to the caller's log when one was given, else stdout."""
    if callable(log):
        try:
            log(message)
        except Exception:
            pass
    else:
        print(message)


def _number(value, default):
    """A usable number of seconds, or the default when the value is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return value


def resolve_warmup(site_cfg):
    """The normalised warm-up block for one site, or None when warm-up does
    not apply: no ``warmup:`` block, ``enabled: false``, or no URLs.

    Unknown or unusable values fall back to the defaults rather than
    failing the run; a block with no usable https:// URL is the same as
    no block at all.
    """
    if not isinstance(site_cfg, dict):
        return None
    block = site_cfg.get("warmup")
    if not isinstance(block, dict):
        return None
    if block.get("enabled", True) is not True:
        return None
    urls = [u for u in (block.get("urls") or [])
            if isinstance(u, str) and u.startswith("https://")]
    if not urls:
        return None
    token = block.get("token") or DEFAULT_TOKEN
    if not isinstance(token, str) or not token.strip():
        token = DEFAULT_TOKEN
    return {
        "enabled": True,
        "urls": urls,
        "token": token,
        "wait_seconds": _number(block.get("wait_seconds"), DEFAULT_WAIT_SECONDS),
        "poll_seconds": _number(block.get("poll_seconds"), DEFAULT_POLL_SECONDS),
    }


def _cookie_names(browser):
    """The names of the cookies in the browser's jar, or an empty set when
    they cannot be read (a fake browser in a test, a closed context)."""
    try:
        context = browser.page.context
    except Exception:
        context = None
    if context is not None:
        try:
            cookies = context.cookies()
        except Exception:
            cookies = None
        if cookies:
            return {c.get("name") for c in cookies
                    if isinstance(c, dict) and c.get("name")}
    getter = getattr(browser, "cookies", None)
    if callable(getter):
        try:
            return {c.get("name") for c in getter()
                    if isinstance(c, dict) and c.get("name")}
        except Exception:
            return set()
    return set()


def _wait(browser, seconds):
    """Wait through the browser when it offers one, else sleep. Never raises."""
    if seconds <= 0:
        return
    try:
        browser.wait(seconds)
        return
    except Exception:
        pass
    try:
        time.sleep(seconds)
    except Exception:
        pass


def _goto(browser, url):
    """Open one page with a plain page load (not the browser's read path, so
    warm-up never lands in the per-read log and never takes the human dwell).
    A missing or refused page raises, and the caller treats that as a page
    to skip, never as a failed run."""
    page = browser.page
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        browser._last_url = url        # the next real read then arrives from here, the way a person would
    except Exception:
        pass


def _poll_for_token(browser, token, poll_seconds):
    """True when the token is in the cookie jar now or appears within
    ``poll_seconds``; the challenge finishes a few seconds after the page
    loads, so the jar is read immediately and then once a second."""
    if token in _cookie_names(browser):
        return True
    steps = int(poll_seconds)
    for _ in range(steps):
        _wait(browser, 1)
        if token in _cookie_names(browser):
            return True
    return False


def warm_up(browser, site_cfg, log=None):
    """Visit the site's warm-up pages and poll for its clearance cookie.

    Returns ``{"visited": [...], "token_found": bool, "seconds": float}``.
    Every page kind is visited even when the token was already found on an
    earlier one: the token is scoped per page kind, so one kind's token
    does not clear another kind's pages. Nothing here ever raises: a page
    that fails, a token that never appears, or a browser that cannot be
    read is logged plainly and the run carries on without it.
    """
    started = time.time()
    report = {"visited": [], "token_found": False, "seconds": 0.0}
    try:
        cfg = resolve_warmup(site_cfg)
        if cfg is None:
            return report
        found = False
        for url in cfg["urls"]:
            try:
                _goto(browser, url)
            except Exception as exc:
                _log(log, f"warm-up: could not open {url} "
                          f"({type(exc).__name__}); continuing without it")
                continue
            report["visited"].append(url)
            _wait(browser, cfg["wait_seconds"])
            try:
                if _poll_for_token(browser, cfg["token"], cfg["poll_seconds"]):
                    found = True
            except Exception as exc:
                _log(log, f"warm-up: could not read the cookies after {url} "
                          f"({type(exc).__name__}); continuing")
        report["token_found"] = found
        if not found:
            _log(log, f"warm-up: the {cfg['token']} token did not appear "
                      f"after {len(report['visited'])} page(s); continuing anyway")
    except Exception as exc:                       # warm-up never fails a run
        _log(log, f"warm-up: skipped after an error ({type(exc).__name__}); continuing")
    finally:
        report["seconds"] = round(time.time() - started, 2)
    return report
