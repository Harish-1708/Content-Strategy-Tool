"""The Agent-Reach Instagram route, run beside the existing logged-in Instagram collector.

Agent-Reach (https://github.com/Panniantong/Agent-Reach, MIT) reads Instagram by driving the OpenCLI tool (https://github.com/jackwener/opencli) in a browser that a person has
logged in to. This module re-implements that call (`opencli instagram <profile|user|search> ... -f json`), without copying Agent-Reach's code, and then does what the existing
collector's source declaration promised before the route could be relied on: it merges what both routes found, never lets the newer route overwrite the established one, and
reports the two side by side on unique usable items, completeness and failure rate.

What it needs, plainly: the `opencli` program on the machine and a browser that someone has logged in to Instagram with. A hosted GitHub runner has neither, so there the route
reports NEED_SETUP and nothing else happens. It is for a machine you control (a self-hosted runner, or run once by hand) and is not live-verified.

OpenCLI's output fields for Instagram were not verified against a running OpenCLI: they are read through the generic field names Agent-Reach itself maps (url, author, text, date,
counts). Counts that are missing stay empty and show up as lower completeness in the comparison; nothing is invented.
"""
import json
import re
import shutil
import subprocess

from . import contract as K
from .collectors import _first_number, _iso, _row
from .safety import clean_text, scrub

CODE = re.compile(r"/(?:p|reel|tv)/([\w-]+)")
LOGIN_WORDS = ("login", "log in", "logged in", "sign in", "http 401")
BLOCK_WORDS = ("captcha", "security check", "account_risk", "suspicious", "challenge_required", "checkpoint", "verify your identity", "browser environment")
SETUP_WORDS = ("browser bridge", "extension", "daemon is not", "no browser", "not connected", "econnrefused")


def hosted_runner(environ=None):
    """True on a GitHub-hosted runner. The route never runs there: the logged-in browser's cookies must stay on a machine you control."""
    import os

    from . import guards
    return guards.on("hosted_runner_block") and (environ if environ is not None else os.environ).get("RUNNER_ENVIRONMENT", "") == "github-hosted"


SITE_NAMES = {"instagram": "Instagram", "reddit": "Reddit", "twitter": "X", "facebook": "Facebook", "linkedin": "LinkedIn", "xiaohongshu": "Xiaohongshu", "boss": "Boss"}


def site_name(site):
    """The site's name as a person writes it (OpenCLI's site words are lower case)."""
    return SITE_NAMES.get(site, str(site).capitalize())


class OpenCLI:
    def __init__(self, binary="opencli", *, run=subprocess.run, which=shutil.which, timeout=90, environ=None, site="instagram"):
        self.binary, self.run, self.which, self.timeout, self.environ, self.site = binary, run, which, timeout, environ, site

    def available(self):
        return bool(self.which(self.binary))

    def call(self, args):
        """The parsed JSON of one `opencli instagram ...` command, or a Failure with the real code. One try, no retry: a refusal is never answered by trying harder."""
        if hosted_runner(self.environ):
            raise K.Failure(K.NEED_SETUP, f"this route does not run on a GitHub-hosted runner: it needs a browser logged in to {self.site.capitalize()}, and that login must stay on a machine you control "
                            f"(a self-hosted runner, or run it by hand there). No {self.site.capitalize()} cookie or password is ever passed to a hosted runner")
        if not self.available():
            raise K.Failure(K.NEED_SETUP, f"{self.binary} is not installed on this machine. This route needs the OpenCLI program and a browser logged in to {site_name(self.site)}, which a hosted runner does not have")
        argv = [self.binary, self.site, *map(str, args), "-f", "json"]
        try:
            done = self.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise K.Failure(K.TIMEOUT, f"OpenCLI did not answer within {self.timeout} seconds") from None
        except OSError as exc:
            raise K.Failure(K.NEED_SETUP, f"OpenCLI could not be started ({exc.__class__.__name__})") from None
        text = (done.stdout or "") + ("\n" + done.stderr if done.stderr else "")
        data = None
        try:
            data = json.loads(done.stdout or "")
        except ValueError:
            pass
        if done.returncode != 0 or data is None or (isinstance(data, dict) and data.get("ok") is False):
            raise classify_error(text)
        return data


def classify_error(text):
    """A Failure from OpenCLI's error output (a YAML block that follows `ok: false`, possibly after Node warnings). The wording is kept short and scrubbed."""
    import yaml
    start = re.search(r"(?m)^ok: false\s*$", text)
    doc = None
    try:
        doc = yaml.safe_load(text[start.start():] if start else text)
    except yaml.YAMLError:
        doc = None
    err = (doc or {}).get("error") if isinstance(doc, dict) else None
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code", "")).upper()
    message = " ".join(str(err.get("message") or text.strip() or "OpenCLI failed").split())[:240]
    low = (message + " " + code).lower()
    safe = scrub(message, 240)
    if any(w in low for w in BLOCK_WORDS) or code in ("ACCOUNT_RISK", "CHALLENGE_REQUIRED"):
        return K.Failure(K.PLATFORM_BLOCKED, f"Instagram showed a security check: {safe}. Nothing is worked around; the account needs a person")
    if code in ("RATE_LIMITED", "RATE_LIMIT") or "http 429" in low or "too many requests" in low:
        return K.Failure(K.RATE_LIMITED, f"Instagram asked for fewer requests: {safe}")
    if any(w in low for w in SETUP_WORDS):
        return K.Failure(K.NEED_SETUP, f"OpenCLI's browser is not ready: {safe}")
    if not re.search(r"\bhttp 404\b", low) and ("AUTH" in code or "LOGIN" in code or any(w in low for w in LOGIN_WORDS)):
        return K.Failure(K.NEED_LOGIN, f"the browser is not logged in to Instagram: {safe}. A person has to log in; the tool never does")
    if re.search(r"\bhttp 404\b|not found", low):
        return K.Failure(K.NOT_FOUND, f"Instagram has no such account or post: {safe}")
    return K.Failure(K.UPSTREAM_BROKEN, f"OpenCLI failed: {safe}")


def rows_from(data):
    """The list of result rows from OpenCLI's answer (a list, or a dict wrapping one under items or data)."""
    if isinstance(data, dict):
        listed = next((data[k] for k in ("items", "data") if isinstance(data.get(k), list)), None)
        data = listed if listed is not None else [data]
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _pick(raw, *keys):
    for k in keys:
        v = raw.get(k)
        if isinstance(v, dict):
            v = v.get("username") or v.get("name") or v.get("nickname")
        if v not in (None, ""):
            return v
    return None


def _count(*values):
    """The first usable count. Plain numbers, '9,000' and abbreviations such as '1.2K', '3M' are understood; anything else is left empty rather than guessed."""
    for v in values:
        if isinstance(v, str):
            m = re.fullmatch(r"\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmMbB]?)\s*", v)
            if m:
                n = float(m.group(1).replace(",", "")) * {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2).lower()]
                return int(round(n))
    return _first_number(*values)


def normalize_post(raw, handle_hint=""):
    """One OpenCLI post row as the same record the existing collector writes (item id instagram:<code>), or None when it has no post link or code."""
    url = str(_pick(raw, "url", "link", "permalink") or "")
    code = _pick(raw, "shortcode", "code") or (CODE.search(url).group(1) if CODE.search(url) else None)
    if not code:
        return None
    kind = "reel" if "/reel/" in url or raw.get("is_video") or raw.get("media_type") in (2, "video", "VIDEO") else "p"
    handle = str(_pick(raw, "username", "author", "user", "owner") or handle_hint or "").lstrip("@")
    return _row("instagram", code, handle=handle, text=clean_text(str(_pick(raw, "caption", "text", "content", "desc", "description", "title") or "")),
                url=url.split("?")[0] if url.startswith("https://www.instagram.com/") else f"https://www.instagram.com/{kind}/{code}/",
                posted_at=_iso(_pick(raw, "taken_at", "timestamp", "published_at", "created_at", "date", "time")),
                likes=_count(raw.get("likes"), raw.get("like_count"), raw.get("likesCount"), raw.get("likes_count")),
                comments=_count(raw.get("comments"), raw.get("comment_count"), raw.get("commentsCount"), raw.get("comments_count")),
                views=_count(raw.get("views"), raw.get("play_count"), raw.get("view_count"), raw.get("plays")),
                media_url=str(_pick(raw, "display_url", "thumbnail", "image", "cover", "media_url") or ""),
                found_via="agent_reach")


def collect_handles(cli, handles, limit):
    """([rows], [problems]). A missing account is noted and skipped; a login, block, rate limit or setup problem stops the whole route (the same handle list is never retried)."""
    rows, problems = [], []
    for handle in handles:
        try:
            for raw in rows_from(cli.call(["user", handle.lstrip("@"), "--limit", limit]))[:limit]:
                row = normalize_post(raw, handle)
                if row:
                    rows.append(row)
        except K.Failure as exc:
            problems.append({"handle": handle, "code": exc.code, "message": str(exc)})
            if exc.code != K.NOT_FOUND:
                break
    return rows, problems


# ---- merging with what the established route already holds --------------------------------------------------------------------------------

FIELDS = ("handle", "text", "url", "posted_at", "likes", "comments", "views", "media_url")
DISAGREE = 0.10          # counts further apart than this are noted, not averaged


def merge_rows(established, new):
    """One record for a post both routes found. The established route's values stand; only gaps are filled from the new route; a real disagreement in a count is written down."""
    merged = dict(established)
    filled, conflicts = [], []
    for f in FIELDS:
        a, b = established.get(f), new.get(f)
        if a in (None, "") and b not in (None, ""):
            merged[f] = b
            filled.append(f)
        elif f in ("likes", "comments", "views") and isinstance(a, int) and isinstance(b, int) and max(a, b) and abs(a - b) / max(a, b) > DISAGREE:
            conflicts.append(f"{f} {a} vs {b}")
    notes = []
    if filled:
        notes.append("filled from agent_reach: " + ", ".join(filled))
    if conflicts:
        notes.append("routes disagree: " + ", ".join(conflicts) + " (kept the established route's)")
    if notes:
        merged["details"] = (str(merged.get("details") or "") + (" | " if merged.get("details") else "") + "; ".join(notes))[:400]
    merged["found_via"] = "browser+agent_reach" if "agent_reach" not in str(established.get("found_via", "")) and established.get("found_via") != "agent_reach" else established.get("found_via")
    return merged, filled, conflicts


def merge_with_existing(batches, new_rows, source="instagram", newcomer="agent_reach"):
    """(rows to save, stats). New posts are saved as they are; posts the established route already holds are saved merged, so reading the history back gives one record per post."""
    held = {}
    for rec in batches:
        if rec.get("source") != source or rec.get("via") == newcomer:
            continue
        for item in rec.get("items", []):
            held[item["item_id"]] = item
    out, stats = [], {"new": 0, "overlap": 0, "filled": 0, "conflicts": 0}
    for row in new_rows:
        old = held.get(row["item_id"])
        if old is None:
            stats["new"] += 1
            out.append(row)
            continue
        merged, filled, conflicts = merge_rows(old, row)
        stats["overlap"] += 1
        stats["filled"] += len(filled)
        stats["conflicts"] += len(conflicts)
        out.append(merged)
    return out, stats


# ---- the comparison the route is judged by -----------------------------------------------------------------------------------------------

def _rate(value):
    """A success rate as a number, or None when there is none yet (the health rows use an empty string for a route that has not run)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _has(row, field):
    """Whether a record has a field. 'text' also counts a title: for a Reddit post the title is the content, for an Instagram post the caption is."""
    if field == "text":
        return bool(row.get("text") or row.get("title"))
    return row.get(field) not in (None, "")


def _usable(row):
    return bool(row.get("url")) and (_has(row, "text") or row.get("likes") is not None)


def route_of(batch, newcomer="agent_reach"):
    via = str(batch.get("via") or "")
    return newcomer if via == newcomer else ("apify" if via.startswith("apify") else "browser")


def compare(batches, health_row=None, source="instagram", newcomer="agent_reach"):
    """{route: {...}, 'overlap': n, 'verdict': text}. Judged on unique usable items, completeness and failure rate, as the source declaration promised; advisory only, it never enables anything."""
    by_route = {"browser": {}, "agent_reach": {}}
    for rec in batches:
        if rec.get("source") != source:
            continue
        route = route_of(rec, newcomer)
        route = "agent_reach" if route == newcomer else route
        if route == "apify":
            route = "browser"                                   # both established routes count as "the existing collector" here
        for item in rec.get("items", []):
            by_route[route][item["item_id"]] = item

    def stats(items):
        n = len(items)
        pct = lambda f: round(100 * sum(1 for r in items.values() if _has(r, f)) / n) if n else 0   # noqa: E731
        return {"posts": n, "usable": sum(1 for r in items.values() if _usable(r)), "with_text": pct("text"), "with_likes": pct("likes"), "with_comments": pct("comments"),
                "with_posted_at": pct("posted_at"), "with_media": pct("media_url")}
    a, b = by_route["browser"], by_route["agent_reach"]
    both = set(a) & set(b)
    unique_b = [k for k in b if k not in a and _usable(b[k])]
    out = {"browser": stats(a), "agent_reach": stats(b), "overlap": len(both), "unique_to_agent_reach": len(unique_b), "unique_to_browser": len([k for k in a if k not in b])}
    rate = _rate((health_row or {}).get("success_rate_7d"))
    out["agent_reach_success_rate_7d"] = rate
    complete = lambda s: (s["with_text"] + s["with_likes"] + s["with_posted_at"]) / 3   # noqa: E731
    if not b:
        out["verdict"] = "The Agent-Reach route has collected nothing yet, so there is nothing to compare."
    else:
        share = len(unique_b) / len(b)
        reasons = []
        if share < 0.10:
            reasons.append(f"only {round(100 * share)}% of its posts are new to the existing collector (it adds little)")
        if a and complete(out["agent_reach"]) < complete(out["browser"]) - 10:
            reasons.append("its records are noticeably less complete than the established route's")
        if rate is not None and rate < 70:
            reasons.append(f"it succeeded only {round(rate)}% of the time over the last 7 days")
        out["verdict"] = ("Keep it on: it adds posts the existing collector missed, with comparable completeness and a healthy success rate." if not reasons
                          else "Not yet worth relying on: " + "; ".join(reasons) + ". This is advice only; nothing was switched off.")
    return out


def report_text(c, brand_name, now, *, established="Existing collector (browser / Apify)", newcomer="Agent-Reach route", title="Instagram routes compared"):
    lines = [f"{title} for {brand_name} ({now[:10]})", ""]
    for route, label in (("browser", established), ("agent_reach", newcomer)):
        s = c[route]
        lines.append(f"{label}: {s['posts']} posts, {s['usable']} usable; with text {s['with_text']}%, likes {s['with_likes']}%, comments {s['with_comments']}%, date {s['with_posted_at']}%, picture {s['with_media']}%")
    lines += ["", f"Found by both: {c['overlap']}. Only by Agent-Reach (usable): {c['unique_to_agent_reach']}. Only by the other route: {c['unique_to_browser']}.",
              f"Agent-Reach success rate over 7 days: {'not enough runs yet' if c['agent_reach_success_rate_7d'] is None else str(round(c['agent_reach_success_rate_7d'])) + '%'}", "", c["verdict"]]
    return "\n".join(lines) + "\n"


def check_row(cli, handle):
    """One small real read for the acceptance check: {ok, items, detail} or {ok: False, code, detail}. Never raises."""
    try:
        rows, problems = collect_handles(cli, [handle], 2)
    except K.Failure as exc:
        return {"ok": False, "code": exc.code, "detail": str(exc)}
    if rows:
        return {"ok": True, "items": len(rows), "detail": f"read {len(rows)} post(s) of @{handle} through OpenCLI"}
    first = problems[0] if problems else {"code": K.UPSTREAM_BROKEN, "message": "OpenCLI answered but no post could be read"}
    return {"ok": False, "code": first["code"], "detail": first["message"]}


def routes_report(batches, health_rows, source, brand_name, now):
    """A plain report on every route that has collected for a platform: posts, how complete the records are, what each route found that the others did not, and its success rate over 7 days.
    It works for any platform (Instagram, TikTok, Reddit). Advice only: nothing is switched off or on."""
    by_route = {}
    for rec in batches:
        if rec.get("source") != source:
            continue
        route = str(rec.get("via") or "unknown").split("+")[0] or "unknown"
        by_route.setdefault(route, {}).update({i["item_id"]: i for i in rec.get("items", [])})
    health = {r["route"]: r for r in health_rows}
    lines = [f"{source} collection routes for {brand_name} ({now[:10]})", ""]
    if not by_route:
        return "\n".join(lines + ["Nothing has been collected for this platform yet."]) + "\n"
    ids = {route: set(items) for route, items in by_route.items()}
    scored = []
    for route in sorted(by_route):
        items = by_route[route]
        n = len(items)
        pct = lambda f: round(100 * sum(1 for r in items.values() if _has(r, f)) / n) if n else 0   # noqa: E731
        others = set().union(*[v for k, v in ids.items() if k != route]) if len(ids) > 1 else set()
        rate = _rate((health.get(route) or {}).get("success_rate_7d"))
        attempts = (health.get(route) or {}).get("attempts_7d")
        complete = (pct("text") + pct("likes") + pct("posted_at")) / 3
        lines.append(f"{route}: {n} posts, {sum(1 for r in items.values() if _usable(r))} usable; text {pct('text')}%, likes {pct('likes')}%, date {pct('posted_at')}%; "
                     f"found only by this route: {len(ids[route] - others)}; success rate 7 days: " + ("not enough runs yet" if rate is None else f"{round(rate)}% over {attempts} run(s)"))
        scored.append((route, n, rate, complete))
    lines.append("")
    low = [r for r, _, rate, _ in scored if rate is not None and rate < 70]
    steady = [r for r, n, rate, c in scored if rate is not None and rate >= 70 and n]
    if low:
        lines.append("Needs attention: " + ", ".join(low) + " succeeded less than 70% of the time over the last 7 days.")
    if steady:
        best = max((s for s in scored if s[0] in steady), key=lambda s: (s[2], s[3]))
        lines.append(f"Most reliable by success rate: {best[0]}.")
    if not low and not steady:
        lines.append("There is not enough run history yet to say which route is more reliable.")
    lines.append("This is advice only; no route was switched on or off.")
    return "\n".join(lines) + "\n"
