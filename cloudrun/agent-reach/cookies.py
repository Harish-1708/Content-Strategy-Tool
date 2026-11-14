"""Put each channel's session cookies, kept in Google Secret Manager in our own account, into the browser profile Agent-Reach uses (~/.agent-reach/browser/profile).

Cloud Run mounts a secret as a file, one directory per secret, so each channel's cookies are at <secrets dir>/<channel>/cookies (a browser export: a JSON list of cookies, or a Playwright storage state). Nothing is read from an environment variable,
nothing is written anywhere but the profile, and no value is ever printed or returned: the state this module returns holds only 'seeded', 'missing' or 'failed: <reason>' per channel. A channel without a secret is simply not logged in.

Not live-tested: that the browser Agent-Reach runs reads cookies written this way is the first thing the live check proves (docs/deferred-live-checks.md)."""
import json
import os

SAME_SITE = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax", "strict": "Strict", "none": "None"}
MAX_BYTES = 200_000


def parse(text):
    """The cookie list Playwright wants, from a browser export or a storage state. Raises ValueError with a plain reason."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("the secret is not JSON") from None
    items = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError("the secret holds no cookies")
    out = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("name") or raw.get("value") is None or not (raw.get("domain") or raw.get("url")):
            continue
        cookie = {"name": str(raw["name"]), "value": str(raw["value"]), "path": str(raw.get("path") or "/")}
        if raw.get("url"):
            cookie["url"] = str(raw["url"])
        else:
            cookie["domain"] = str(raw["domain"])
        expires = raw.get("expires", raw.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)
        cookie["httpOnly"], cookie["secure"] = bool(raw.get("httpOnly")), bool(raw.get("secure"))
        site = SAME_SITE.get(str(raw.get("sameSite") or "").lower())
        if site:
            cookie["sameSite"] = site
        out.append(cookie)
    if not out:
        raise ValueError("no cookie in the secret has a name, a value and a domain")
    return out


def seed_all(secrets_dir, profile, channels, playwright_factory=None):
    """{channel: 'seeded' | 'missing' | 'failed: reason'}. One browser start for all the cookies that exist."""
    state, loaded = {}, {}
    for channel in channels:
        path = os.path.join(secrets_dir, channel, "cookies")
        if not os.path.isfile(path):
            state[channel] = "missing"
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                raise ValueError("the secret is too large")
            with open(path, encoding="utf-8") as handle:
                loaded[channel] = parse(handle.read())
        except (OSError, ValueError) as exc:
            state[channel] = f"failed: {exc}"
    if not loaded:
        return state
    try:
        factory = playwright_factory or _playwright
        os.makedirs(profile, exist_ok=True)
        with factory() as pw:
            context = pw.chromium.launch_persistent_context(profile, headless=True, args=["--no-sandbox"])
            try:
                for channel, cookie_list in loaded.items():
                    context.add_cookies(cookie_list)
                    state[channel] = "seeded"
            finally:
                context.close()
    except Exception as exc:                                       # the kind of error only: its text could hold a path or a value
        for channel in loaded:
            if state.get(channel) != "seeded":
                state[channel] = f"failed: the browser could not be started ({type(exc).__name__})"
    return state


def _playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()
