"""Public-profile snapshots: what a competitor's public social profile shows, read like any visitor would read it (no login), kept as dated snapshots and compared with last time.

A competitor's public profile gives real numbers and words without any stored session: a TikTok profile shows its followers, following, likes and bio; a LinkedIn company page its About text and
employee count; an X profile its followers, posts and bio. The monitor reads each profile once per run, through the same isolated browser the stored-session checks use (page waits included), judges
that something real came back (a puzzle page or a script shell is not a profile), pulls out the numbers and words, and keeps a snapshot only when it changed. A change in followers, likes or
employees that is large enough, or a new bio, becomes an event. Nothing here logs in: it is the public page, and a platform that refuses the runner is reported, never worked around.
"""
import os
import re
import subprocess
from urllib.parse import quote

from .. import contract as K
from .. import session_live as SL
from .. import sessions as S
from . import changes as CH

PLATFORMS = ("tiktok", "linkedin", "instagram", "facebook", "x")
LABELS = {"tiktok": "TikTok", "linkedin": "LinkedIn", "instagram": "Instagram", "facebook": "Facebook", "x": "X"}
PROFILE_URL = {"tiktok": "https://www.tiktok.com/@{h}", "linkedin": "https://www.linkedin.com/company/{h}", "instagram": "https://www.instagram.com/{h}/",
               "facebook": "https://www.facebook.com/{h}/", "x": "https://x.com/{h}"}
MULTIPLIER = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
NUMBER = r"(\d[\d.,]*)\s?([KMBkmb])?\+?"
METRIC_WORDS = {"followers": r"followers?", "following": r"following", "likes": r"likes", "posts": r"posts", "employees": r"employees"}
GONE = ("couldn't find this account", "couldn’t find this account", "this account doesn't exist", "this account doesn’t exist", "this page isn't available", "this page isn’t available",
        "this content isn't available", "this content isn’t available", "page not found", "sorry, this page isn't available", "profile isn't available", "profile isn’t available")
WATCHED = (("followers", "followers"), ("likes", "likes"), ("employees", "employees"))
BIO_CHARS, TEXT_KEPT = 300, 1500
BROWSER_PLATFORMS = ("tiktok", "linkedin")                                              # proved readable as a public page; the others are asked of the search layer (snippets), which a platform cannot refuse
HANDLE_IN_URL = {"instagram": r"instagram\.com/([^/?#]+)", "tiktok": r"tiktok\.com/@([^/?#]+)", "x": r"(?:x|twitter)\.com/([^/?#]+)", "facebook": r"facebook\.com/([^/?#]+)",
                 "linkedin": r"linkedin\.com/company/([^/?#]+)"}


def parse_count(text, suffix=""):
    """'73.3' + 'K' is 73300, '1,234' is 1234, '405' is 405; None when it is not a count."""
    raw = str(text or "").strip().replace(",", "")
    if not raw or raw.count(".") > 1:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(round(value * MULTIPLIER.get(str(suffix or "").lower(), 1)))


def label_metric(text, word):                           # (the name `label_metric` is kept short on purpose)
    """The count written AFTER its label ('Followers: 976', 'Followers 122.3K'), or None. Only a fallback: a page that writes '122.3KFollowers' is read by metric() first."""
    m = re.search(r"(?<![\w])" + METRIC_WORDS[word] + r"\b[\s:\-–]*" + NUMBER, text, re.I)
    return parse_count(m.group(1), m.group(2)) if m else None


def metric(text, word):
    """The count written next to a label ('405Following', '976 Followers', '2 employees'), or None. The first one on the page wins."""
    m = re.search(r"(?<![\w.,])" + NUMBER + r"\s*" + METRIC_WORDS[word] + r"\b", text, re.I)
    return parse_count(m.group(1), m.group(2)) if m else None


def _cut(text, limit=BIO_CHARS):
    return re.sub(r"^#+\s*", "", " ".join(str(text or "").split()))[:limit].strip()


def parse_profile(platform, text, title=""):
    """What a profile page says: {'name', 'bio', 'metrics': {followers, following, likes, posts, employees (only those found)}, 'joined', 'about'}. Written for the public pages as they are
    returned by the reader today; a page that does not fit gives fewer fields, never an error."""
    text = " ".join(str(text or "").split())
    out = {"name": "", "bio": "", "joined": "", "about": "", "metrics": {}}
    for word in METRIC_WORDS:
        found = metric(text, word)
        if found is not None:
            out["metrics"][word] = found
    after = {word: found for word in METRIC_WORDS if (found := label_metric(text, word)) is not None}      # the label-first layout ('Followers 122.3K')
    if len(after) > len(out["metrics"]):                    # a page is read in the layout that explains more of it; a tie keeps the layout that has worked live ('122.3KFollowers'). "Following 69 Followers 122.3K" would otherwise read '69 Followers'
        out["metrics"] = after
    if platform == "tiktok":
        m = re.match(r"#\s+(.+?)\s+##\s+(\S+)", text)
        out["name"] = _cut(m.group(1), 100) if m else ""
        bio = re.search(r"\d[\d.,]*\s?[KMBkmb]?\s*Likes\s+(.*)$", text, re.I)
        out["bio"] = _cut(re.sub(r"(\s*Log in)+\s*$", "", bio.group(1)), BIO_CHARS) if bio else ""
    elif platform == "linkedin":
        m = re.search(r"Sign in\s+#\s+(.+?)\s+##\s+(.+?)\s+####\s+(.+?)\s+(?:Follow\b|-\s*View all|$)", text)
        out["name"] = _cut(m.group(1), 100) if m else ""
        out["bio"] = _cut(m.group(3)) if m else ""
        about = re.search(r"##\s*About us\s+(.*?)(?:\s+##\s|$)", text)
        out["about"] = _cut(about.group(1), 500) if about else ""
        if not out["name"]:
            m = re.search(r"#\s+(.+?)\s+##", text)
            out["name"] = _cut(m.group(1), 100) if m else ""
    elif platform == "x":
        m = re.search(r"#\s+(.+?)\s+@(\w+)\s+(.*?)(?:\s+Joined\b|\s+\d[\d.,]*\s?[KMBkmb]?\s*Following\b|$)", text)
        out["name"] = _cut(m.group(1), 100) if m else ""
        out["bio"] = _cut(m.group(3)) if m else ""
        joined = re.search(r"Joined\s+([A-Z][a-z]+\s+\d{4})", text)
        out["joined"] = joined.group(1) if joined else ""
    else:
        name = re.split(r"\s+\(@|\s+[|•\-–]\s+", str(title or ""), maxsplit=1)[0]
        out["name"] = _cut(name, 100)
        out["bio"] = _cut(text)
    return out


def profile_url(platform, handle):
    return PROFILE_URL[platform].format(h=quote(str(handle).lstrip("@"), safe="._-%"))


def handles_for(comp, discovered, settings):
    """[(platform, handle)] to read for one competitor: what the competitor's file says wins over what its own pages linked to, only the platforms the owner switched on, at most the owner's
    limit per competitor."""
    cfg = settings["social"]
    chosen = []
    for platform in PLATFORMS:
        if not cfg.get(platform):
            continue
        handle = str(comp.get(platform) or discovered.get(platform) or "").strip().lstrip("@")
        if handle:
            chosen.append((platform, handle))
    return chosen[:max(0, int(cfg["max_per_competitor"]))]


RETRY_QUALITIES = ("too little", "script shell", "empty")        # a challenge page is never asked for again: only a page that did not finish drawing
NEEDS_COUNTS = ("tiktok", "instagram", "facebook", "x")          # for these the follower, like or post counts ARE the snapshot: a page without them is not recorded as one (LinkedIn's About text alone is worth keeping)


def make_reader(settings, contact, python, environ=None, runner=subprocess.run):
    """The function the monitor reads a profile with: (platform, url) -> {'text', 'title', 'landed', 'quality'}, or a K.Failure saying why not (no browser here, the platform refused the runner,
    a puzzle page, a script shell). One attempt, no login, the page waited for where the platform draws it late."""
    environ = os.environ if environ is None else environ

    def read(platform, url):
        res = SL._worker_page(platform, url, settings, contact, python, environ, runner, with_session=False, reading=True)
        if not res.get("ok"):
            raise SL._failure_from(res)
        text = " ".join(SL.clean_text(res.get("text") or "").split())
        quality = SL.judge_content(text)[0]
        undrawn = quality == "real text" and platform in NEEDS_COUNTS and not parse_profile(platform, text, str(res.get("title") or ""))["metrics"]
        if quality in RETRY_QUALITIES or undrawn:         # a half-drawn page, or a page that drew its text but not yet its counts (monitoring #35: TikTok read as real text, no counts); a bot-check is never asked for again (monitoring #33: 60 characters for a TikTok profile that a read a few minutes later returned in full): one more try, waiting twice as long
            usual = int((S.PLATFORMS[platform].get("read_adv") or S.PLATFORMS[platform].get("adv") or {}).get("wait") or 3000)
            again = SL._worker_page(platform, url, settings, contact, python, environ, runner, with_session=False, reading=True, wait_ms=max(2 * usual, 6000))
            if again.get("ok"):
                retext = " ".join(SL.clean_text(again.get("text") or "").split())
                requality = SL.judge_content(retext)[0]
                if requality == "real text" or len(retext) > len(text):
                    res, text, quality = again, retext, requality
        if quality != "real text":
            code = K.PLATFORM_BLOCKED if quality == "challenge page" else K.UPSTREAM_BROKEN
            raise K.Failure(code, f"the profile page came back with no readable text ({len(text)} characters: {SL.WHY_NOT_CONTENT[quality]})")
        return {"text": text, "title": str(res.get("title") or ""), "landed": S._landed(res.get("final_url") or url), "quality": quality}
    return read


def snapshot_profile(platform, handle, reader, now=""):
    """One profile, read and parsed, ready to keep: {platform, handle, url, landed, name, bio, joined, about, metrics, quality, text, complete}. `complete` is False when no number was found on
    the page (it was read, but the figures are not there to compare). An account that does not exist is a K.Failure (NOT_FOUND), not a snapshot."""
    url = profile_url(platform, handle)
    got = reader(platform, url)
    low = got["text"].lower()
    if any(phrase in low for phrase in GONE) and len(got["text"]) < 800:
        raise K.Failure(K.NOT_FOUND, f"{LABELS[platform]} says there is no such account or page as {handle!r}")
    parsed = parse_profile(platform, got["text"], got.get("title", ""))
    if not parsed["metrics"] and platform in NEEDS_COUNTS:
        raise K.Failure(K.NOT_FOUND, f"the {LABELS[platform]} page for {handle!r} was read but shows no follower, like or post count, so there is nothing to record (nothing is estimated); the page starts: {got['text'][:160]!r}")
    return {"platform": platform, "handle": handle.lstrip("@"), "url": url, "landed": got.get("landed", ""), "name": parsed["name"], "bio": parsed["bio"], "joined": parsed["joined"],
            "about": parsed["about"], "metrics": parsed["metrics"], "quality": got.get("quality", ""), "text": got["text"][:TEXT_KEPT], "complete": bool(parsed["metrics"])}


def _handle_of(platform, url):
    m = re.search(HANDLE_IN_URL[platform], str(url or ""), re.I)
    return m.group(1).lower() if m else ""


def make_discovery(environ=None, providers=None, limit=12):
    """The function that asks the search layer about a profile when a browser cannot read it: (platform, handle, now) -> a snapshot made from the one search result whose address is exactly that
    account (a near match such as @brand_ for @brand is a different account and is never used), or a K.Failure. At most `limit` searches per run; no login."""
    environ = os.environ if environ is None else environ
    used = {"n": 0}

    def ask(platform, handle, now=""):
        if used["n"] >= int(limit):
            raise K.Failure(K.RATE_LIMITED, f"the limit of {limit} profile searches for this run was reached")
        used["n"] += 1
        url = profile_url(platform, handle)
        rows = SL.discovery_rows(url, environ, providers)
        want = handle.lstrip("@").lower()
        same = [r for r in rows if _handle_of(platform, r.get("url")) == want]         # every result that is exactly this account: its profile, its reels page, its posts
        hit = next((r for r in same if parse_profile(platform, " ".join(f"{r.get('title') or ''} {r.get('text') or ''}".split()), r.get("title", ""))["metrics"]), same[0] if same else None)
        if hit is None:
            seen = ", ".join(sorted({_handle_of(platform, r.get("url")) or "?" for r in rows})[:4])
            raise K.Failure(K.NOT_FOUND, f"the search found no result for exactly {LABELS[platform]} {handle!r}" + (f" (it found {seen}: other accounts are never used)" if rows else ""))
        text = " ".join(f"{hit.get('title') or ''} {hit.get('text') or ''}".split())
        parsed = parse_profile(platform, text, hit.get("title", ""))
        if not parsed["metrics"]:
            raise K.Failure(K.NOT_FOUND, f"the search result for exactly {LABELS[platform]} {handle!r} was found but its text shows no follower, like or post count, so there is nothing to record (nothing is estimated)")
        bio = re.search(r"@" + re.escape(want) + r"\)?\s*[:\-]\s*[\"“]?(.*?)[\"”]?\s*$", text, re.I)
        return {"platform": platform, "handle": want, "url": url, "landed": S._landed(hit.get("url") or ""), "name": parsed["name"], "bio": _cut(bio.group(1)) if bio else parsed["bio"], "joined": "",
                "about": "", "metrics": parsed["metrics"], "quality": "search snippet", "via": "search", "text": text[:TEXT_KEPT], "complete": True}
    return ask


class ProfileReader:
    """What the monitor reads a profile with. TikTok and LinkedIn are read as public pages in the browser; if that fails (a platform that refuses the runner, a puzzle page) and the search layer is
    available, the search snippet is used instead. Instagram, Facebook and X, which refuse or hide their pages from a runner, go to the search layer directly."""

    def __init__(self, browser=None, discovery=None):
        self.browser, self.discovery = browser, discovery

    def snapshot(self, platform, handle, now=""):
        failure = None
        if platform in BROWSER_PLATFORMS and self.browser is not None:
            try:
                return snapshot_profile(platform, handle, self.browser, now)
            except K.Failure as exc:
                if self.discovery is None or exc.code == K.NOT_FOUND:
                    raise
                failure = exc
        if self.discovery is None:
            raise failure or K.Failure(K.NEED_SETUP, f"{LABELS[platform]} profiles need the browser (the Scrapling environment) or a search service key, and neither is available here")
        try:
            return self.discovery(platform, handle, now)
        except K.Failure as exc:
            if failure is not None:
                raise K.Failure(failure.code, f"{failure}; and the search fallback: {exc}") from None
            raise


def build_reader(settings, contact, environ, browser_allowed=True):
    """(reader, note) for a monitoring run: the browser when the Scrapling environment is installed and the owner allows it, the search layer when a search key is set (up to the owner's limit per
    run), both, or (None, why) when there is nothing to read with or the owner switched every platform off."""
    cfg = settings["social"]
    if cfg["max_per_competitor"] <= 0 or not any(cfg[p] for p in PLATFORMS):
        return None, ""
    python = environ.get("SCRAPLING_PYTHON", "")
    browser = make_reader(settings, contact, python, environ) if python and browser_allowed else None
    search = make_discovery(environ, limit=cfg["max_discovery_per_run"]) if cfg["max_discovery_per_run"] > 0 and SL.discovery_providers(environ) else None
    if browser is None and search is None:
        return None, "note: public social profiles need the browser (the Scrapling environment) or a search service key, and neither is available here, so they were not read"
    return ProfileReader(browser, search), "social profiles: read through " + ("the browser" if browser else "no browser") + (" and the search layer" if search else " (no search service key: the platforms that block a browser are skipped)")


def _pct(old, new):
    return (new - old) / old * 100.0 if old else 0.0


def diff_profile(site, name, old, new, settings, now):
    """The events between two readings of one profile: a followers, likes or employees count that moved by at least the owner's percentage (and by more than rounding), and a changed bio."""
    events = []
    platform = new.get("platform", "")
    cfg = settings["social"]
    om, nm = old.get("metrics") or {}, new.get("metrics") or {}
    for key, label in WATCHED:
        a, b = om.get(key), nm.get(key)
        if not isinstance(a, int) or not isinstance(b, int) or a <= 0 or a == b:
            continue
        pct = _pct(a, b)
        floor = 1 if key == "employees" else max(5, int(a * 0.005))                      # a count shown as '73.3K' is only good to about a half of one percent; and a few people are not news
        if abs(pct) < float(cfg["min_change_pct"]) or abs(b - a) < floor:
            continue
        events.append(CH.event(site, "social", "social_metric_changed", f"{name} {LABELS.get(platform, platform)} {label}: {a:,} to {b:,} ({pct:+.1f}%)", at=now,
                               severity="notable" if abs(pct) >= 25 else "info", subject={"kind": "profile", "key": f"{platform}-{key}"}, before=a, after=b,
                               details={"platform": platform, "metric": key, "change": b - a, "pct": round(pct, 1)}, tier="public_polite", access="page_text"))
    before, after = " ".join(str(old.get("bio") or "").lower().split()), " ".join(str(new.get("bio") or "").lower().split())
    if before and after and before != after:
        events.append(CH.event(site, "social", "social_bio_changed", f"{name} changed its {LABELS.get(platform, platform)} bio", at=now, severity="info", subject={"kind": "profile", "key": f"{platform}-bio"},
                               before=old.get("bio"), after=new.get("bio"), details={"platform": platform}, tier="public_polite", access="page_text"))
    return events


SOCIAL_COLUMNS = ["profile_key", "competitor", "domain", "platform", "handle", "followers", "followers_change", "followers_change_pct", "following", "likes", "posts", "employees", "name", "bio",
                  "joined", "quality", "url", "first_read", "last_read"]


def sheet_tab(sites, snaps):
    """The Social Profiles tab: one row per competitor and platform, the latest reading with the change in followers since the reading before it. None when no profile has been read."""
    rows = {}
    for site in sites:
        for kind in snaps.kinds(site["id"]):
            if not kind.startswith("profile-"):
                continue
            entry, snap = snaps.latest(site["id"], kind)
            if not snap or (snap.get("platform") in NEEDS_COUNTS and not snap.get("metrics")):      # a profile without a single count is not a row (older runs stored some; they are no longer shown)
                continue
            history = snaps.history(site["id"], kind)
            prev = snaps.load(site["id"], kind, history[-2]) if len(history) > 1 else {}
            m, pm = snap.get("metrics") or {}, (prev or {}).get("metrics") or {}
            change = m["followers"] - pm["followers"] if isinstance(m.get("followers"), int) and isinstance(pm.get("followers"), int) else None
            key = f"{site['id']}|{snap.get('platform', kind[8:])}"
            rows[key] = {"profile_key": key, "competitor": site.get("name", ""), "domain": site.get("domain", ""), "platform": LABELS.get(snap.get("platform"), snap.get("platform", "")),
                         "handle": snap.get("handle", ""), "followers": m.get("followers"), "followers_change": change,
                         "followers_change_pct": round(_pct(pm["followers"], m["followers"]), 1) if change is not None and pm.get("followers") else None,
                         "following": m.get("following"), "likes": m.get("likes"), "posts": m.get("posts"), "employees": m.get("employees"), "name": snap.get("name", ""),
                         "bio": snap.get("bio") or snap.get("about", ""), "joined": snap.get("joined", ""), "quality": snap.get("quality", ""), "url": snap.get("url", ""),
                         "first_read": (history[0].get("captured_at") or "")[:10], "last_read": entry.get("confirmed_at", "")}
    return {"title": "Social Profiles", "columns": SOCIAL_COLUMNS, "rows_by_key": rows, "key_col": "profile_key", "starter_manual": ["Notes"]} if rows else None
