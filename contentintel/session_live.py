"""Stored login sessions in use (Update 50): prove, one platform at a time, that a platform really accepts its stored session from a runner, and read one logged-in page through the fallback chain.

Each platform is judged on its own and reported on its own line: X, Instagram, TikTok, Reddit, Facebook, LinkedIn, Xiaohongshu and Boss never share a result. A session is only ever used on that platform's own
hosts, through the Scrapling worker (which is handed the one named secret and nothing else), and a platform whose check has not passed recently is never read through its session by the chain.

What ACCEPTED means: the platform's own page, loaded with the stored session from this runner, showed a logged-in state. It does not mean every kind of read works (see each platform's `can`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contract as K
from . import session_chain as SC
from . import sessions as S

PROVED_DAYS = 14                      # a passed check counts as live proof for this long
ACCEPTED, REJECTED, EXPIRED = "ACCEPTED", "REJECTED", "EXPIRED"
NOT_SET_UP, BAD_CAPTURE, OFF = "NOT SET UP", "BAD CAPTURE", "OFF"
CANNOT_CHECK, UNCONFIRMED, BLOCKED = "CANNOT CHECK", "UNCONFIRMED", "BLOCKED"
ORDER = (ACCEPTED, UNCONFIRMED, REJECTED, EXPIRED, BAD_CAPTURE, BLOCKED, CANNOT_CHECK, NOT_SET_UP, OFF)
OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE)           # the states a person has to fix (a new capture): they turn a check run red, a missing secret does not
CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP)       # these states void an earlier proof; "cannot check", "blocked", "unconfirmed" and "off" learn nothing, so they keep it
WALL = re.compile(r"login page|log in|sign in", re.I)


def _now(now=""):
    return now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---- what was found, kept between runs (no secret value is ever written) -------------------------------------------------------------------------------

REASON_LIMIT = 1500
AMBIGUOUS = ("no sign of being logged in", "the page came back empty")
UNCONFIRMED_LIMIT = 3                                                                    # checks in a row with no sign either way before the session counts as refused
EVIDENCE_KEEP = ("fetched_with", "status", "landed", "title", "html_chars", "html_truncated", "text_chars", "visible_text_available", "logged_in_found", "logged_in_missing", "logged_in_text_found",
                 "logged_in_text_missing", "logged_out_in_text", "logged_out_only_in_code", "password_field", "login_word_in_address", "scripts", "body_ids", "noscript", "cookies_sent", "cookies_back",
                 "worker_error")


def compact_evidence(ev):
    """The evidence worth keeping with a verdict: how the page was read, what was found and missing, the counts. Never the page's words (text_starts is left out: the repository may be public)."""
    if not isinstance(ev, dict):
        return {}
    kept = {k: ev[k] for k in EVIDENCE_KEEP if k in ev}
    if "title" in kept:
        kept["title"] = str(kept["title"])[:200]
    return kept


class SessionLedger:
    """data/_shared/sessions_health.json: per platform the last result, why, when, and the day a live check last passed (empty when a later check voided it)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get("CONTENTINTEL_DATA_DIR", "data")) / "_shared" / "sessions_health.json"
        self.data = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {k: v for k, v in loaded.items() if k in S.PLATFORMS and isinstance(v, dict)} if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    def record(self, platform, state, reason, now, via="browser", evidence=None, expires_on="", days_left="", twscrape=None):
        entry = self.data.setdefault(platform, {"state": "", "reason": "", "checked_at": "", "proved_on": ""})
        entry.update(state=state, reason=" ".join(str(reason).split())[:REASON_LIMIT], checked_at=now, expires_on=expires_on or "", days_left=days_left, evidence=compact_evidence(evidence))
        entry["unconfirmed_streak"] = (entry.get("unconfirmed_streak", 0) + 1) if state == UNCONFIRMED else 0
        if isinstance(twscrape, dict):
            entry["twscrape"] = {"state": str(twscrape.get("state", "")), "detail": " ".join(str(twscrape.get("detail", "")).split())[:600]}
        else:
            entry.pop("twscrape", None)
        if state == ACCEPTED:
            entry["proved_on"], entry["proved_via"] = now, via
        elif state in CLEARS_PROOF:
            entry["proved_on"], entry["proved_via"] = "", ""

    def proved_on(self, platform):
        return (self.data.get(platform) or {}).get("proved_on", "")

    def proved_via(self, platform):
        return (self.data.get(platform) or {}).get("proved_via", "browser") if self.proved_on(platform) else ""

    def proved(self, platform, now, via="browser"):
        """True only when a live check passed, the way asked about, within PROVED_DAYS and nothing since has voided it. The fallback chain reads through the BROWSER, so a session that was only
        proved through twscrape (a different route) is not proof for it."""
        then, here = _parse(self.proved_on(platform)), _parse(_now(now))
        return bool(then and here and timedelta(0) <= here - then <= timedelta(days=PROVED_DAYS) and self.proved_via(platform) == via)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def switched_on(settings, platform):
    """(True, '') or (False, why): the owner's master switch and the platform's own switch (both ON unless the owner turned them off)."""
    sec = (settings.get("services") or {}).get("stored_sessions") or {}
    if not sec.get("enabled", True):
        return False, "stored login sessions are switched off (services.stored_sessions.enabled)"
    if not (sec.get("channels") or {}).get(platform, True):
        return False, f"the {platform} session is switched off (services.stored_sessions.channels.{platform})"
    return True, ""


def _host_ok(platform, url):
    host = re.sub(r"^https?://", "", str(url).lower()).split("/")[0].split("?")[0].split("@")[-1].split(":")[0]
    return any(host == h or host.endswith("." + h) for h in S.PLATFORMS[platform]["hosts"])


# ---- one platform ---------------------------------------------------------------------------------------------------------------------------------

def _worker_page(platform, url, settings, contact, python, environ, runner, *, with_session, timeout=180, reading=False):
    """One address through the Scrapling worker, with the stored session or without it. Returns the worker's result for the address; raises K.Failure for what could not be done at all."""
    from .observatory import worker_client as wc
    adv = {"stealth": True, **(S.PLATFORMS[platform].get("adv") or {}), **((S.PLATFORMS[platform].get("read_adv") or {}) if reading else {})}      # a platform that draws its page after it has loaded is waited for
    env = dict(environ)
    if with_session:
        session = S.load(platform, environ)
        name, text = session.worker_env()
        adv["cookies_env"] = name
        env[name] = text                                                                 # the worker is handed this one named secret and nothing else of ours
    job = wc.build_job([{"url": url, "advanced": adv}], settings, contact)
    job["return_html"] = True
    job["retry"] = {"attempts": 1, "backoff_seconds": 0}                                  # one attempt: a challenge page is never asked for again with the same session
    got = wc.run_worker(job, python, timeout=timeout, environ=env, runner=runner)
    results = got.get("results") or []
    if not results:
        raise K.Failure(K.UPSTREAM_BROKEN, "the Scrapling worker returned nothing for the page")
    return results[0]


def _evidence(platform, res, session, plain):
    """What the page showed, from the worker's answer, with no secret: see sessions.page_evidence. Adds how it was fetched and the cookie NAMES sent and sent back."""
    err = res.get("error") or {}
    ev = S.page_evidence(platform, res.get("final_url") or "", res.get("html") or "", res.get("status") or 0, res.get("text") or "")
    ev.update(html_truncated=bool(res.get("html_truncated")), fetched_with=res.get("fetched_with") or ("plain HTTP" if plain else "browser"), cookies_sent=sorted(session.cookie_jar()), cookies_back=list(res.get("cookie_names_back") or []),
              worker_error=(f"{err.get('code')}: {' '.join(str(err.get('message')).split('Call log:')[0].split())}"[:300] if err else ""))
    return ev


CHALLENGE_WORDS = ("just a moment", "attention required", "cf-chl", "challenge-platform", "verify you are human", "enable javascript and cookies to continue", "captcha")


def looks_like_challenge(status, html, title=""):
    """A bot-check page (Cloudflare's 'Just a moment...' and the like) answered with a refusal status: the platform is blocking the runner, whatever the worker called it."""
    if int(status or 0) not in (401, 403, 429, 503):
        return False
    blob = (str(title) + " " + str(html or "")[:20000]).lower()
    return any(word in blob for word in CHALLENGE_WORDS)


TWSCRAPE_SCRIPT = Path(__file__).resolve().parent / "x_twscrape_check.py"


def twscrape_layer(session, environ, runner=subprocess.run, timeout=150):
    """The last resort for X when x.com shows the runner a bot-check: contentintel/x_twscrape_check.py in its own environment (TWSCRAPE_PYTHON). Returns {"state": accepted, rejected or unreachable,
    "detail": why}. Nothing is installed or run when the environment is not there: that is 'unreachable' with the reason. The program is handed only this one session's cookies."""
    python = (environ.get("TWSCRAPE_PYTHON") or "").strip()
    if not python or not Path(python).exists():
        return {"state": "unreachable", "detail": "the twscrape environment is not installed here (TWSCRAPE_PYTHON), so X's profile interface was not asked"}
    env = {"X_SESSION_COOKIES": json.dumps(session.cookie_jar(), separators=(",", ":")), "TWS_TELEMETRY": "0", "DO_NOT_TRACK": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PATH": environ.get("PATH", "/usr/bin:/bin"), "HOME": environ.get("HOME", "/tmp")}
    try:
        done = runner([python, str(TWSCRAPE_SCRIPT)], env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "unreachable", "detail": f"the twscrape check could not be run ({exc.__class__.__name__})"}
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.strip().startswith("{")]
    try:
        got = json.loads(lines[-1])
        state, detail = str(got["state"]), str(got.get("detail", ""))[:600]
    except (IndexError, ValueError, KeyError):
        return {"state": "unreachable", "detail": f"the twscrape check gave no answer (exit {done.returncode})"}
    return {"state": state if state in ("accepted", "rejected", "unreachable") else "unreachable", "detail": detail}


def check_platform(platform, settings, contact, python, *, environ=None, runner=subprocess.run, now="", ledger=None, offline=False):
    """One platform's result as a dict: state (ACCEPTED, REJECTED, EXPIRED, BAD CAPTURE, CANNOT CHECK, UNCONFIRMED, NOT SET UP or OFF), why, expiry and the day it was last proved.
    `offline` makes no request (status only). Never raises."""
    environ = os.environ if environ is None else environ
    now = _now(now)
    spec = S.PLATFORMS[platform]
    row = {"platform": platform, "label": spec["label"], "secret": spec["secret"], "can": spec["can"], "lifetime": spec["lifetime"], "state": "", "reason": "", "expires_on": "", "days_left": "", "proved_on": ""}

    def done(state, reason, record=True, via="browser"):
        row.update(state=state, reason=reason, via=via)
        if ledger is not None:
            if record and not offline:
                ledger.record(platform, state, reason, now, via=via, evidence=row.get("evidence"), expires_on=row.get("expires_on", ""), days_left=row.get("days_left", ""), twscrape=row.get("twscrape"))
            row["proved_on"], row["proved_via"] = ledger.proved_on(platform), ledger.proved_via(platform)
        return row

    on, why = switched_on(settings, platform)
    if not on:
        return done(OFF, why, record=False)
    try:
        session = S.load(platform, environ, now)
    except K.Failure as exc:
        text = str(exc)
        if "is not set" in text:
            return done(NOT_SET_UP, f"add the secret {spec['secret']} (capture one by hand: docs/platform/session-capture-guide.md)")
        return done(EXPIRED if exc.code == K.NEED_LOGIN else BAD_CAPTURE, text)
    row.update(expires_on=session.expires_on(), days_left="" if session.days_left() is None else session.days_left())
    if offline:
        proof = f"proved live on {ledger.proved_on(platform)[:10]}" if ledger is not None and ledger.proved(platform, now) else "not yet proved against the live site"
        return done("PRESENT", f"stored session present and not expired; {proof}", record=False)
    try:
        res = _worker_page(platform, spec["check_url"], settings, contact, python, environ, runner, with_session=True)
    except K.Failure as exc:
        return done(CANNOT_CHECK, f"the check could not be made here: {exc}")
    html, final, status = res.get("html") or "", res.get("final_url") or spec["check_url"], int(res.get("status") or 0)
    plain = bool(res.get("browser_note"))
    why_plain = f"the browser could not be used here ({res.get('browser_error') or 'no reason was given'})"
    err = res.get("error") or {}
    row["evidence"] = _evidence(platform, res, session, plain)
    refused = "err_http_response_code_failure" in str(err.get("message", "")).lower()                  # the browser's own words for a platform that answers an automated visit with an error status
    if err and err.get("code") != "PLATFORM_BLOCKED" and not refused and not WALL.search(str(err.get("message", ""))) and int(status or 0) not in (401, 403) and not looks_like_challenge(status, html, row["evidence"]["title"]):
        return done(CANNOT_CHECK, f"the Scrapling worker could not read the page cleanly ({humanise_error(err.get('code'), err.get('message', ''), platform)}), so nothing was judged; this is not the platform's answer")
    if err.get("code") == "PLATFORM_BLOCKED" or refused or looks_like_challenge(status, html, row["evidence"]["title"]):
        blocked = ((f"{spec['label']}: {humanise_error(err.get('code'), err.get('message', ''), platform)}, so the session itself could not be judged" if refused else
                    f"{spec['label']} showed a bot-check or challenge page to this runner, so the session itself could not be judged")
                   + (f"; and {why_plain}" if plain else "") + ". This is the platform blocking the runner, not a bad session")
        if platform == "x":                                                              # the approved last resort: ask X's own profile interface, with the two cookies already stored
            layered = twscrape_layer(session, environ, runner)
            row["twscrape"] = layered
            via = "checked through twscrape because x.com's web page showed the runner a challenge"
            if layered["state"] == "accepted":
                return done(ACCEPTED, f"{layered['detail']} ({via}; so the browser read of x.com is not proved)", via="twscrape")
            if layered["state"] == "rejected":
                return done(REJECTED, f"{layered['detail']} ({via})")
            blocked += f". The last-resort twscrape check could not judge it either: {layered['detail']}"
            if "403" in layered["detail"]:
                blocked += ". X has answered 403 to this runner's address on both routes (its web page and its profile interface) and has also accepted it (the check of 14 Nov 2026 reached x.com/home logged in), so it is intermittent: when it blocks, its session cannot be proved from GitHub's servers and only a different network address would change that"
        return done(BLOCKED, blocked)
    if not html:
        if status in (401, 403):
            return done(REJECTED, f"the site answered {status}: either it refused the session or it blocked this runner; capture a new session, and if it repeats the platform blocks runners")
        return done(CANNOT_CHECK, f"the page could not be read ({err.get('code') or status or 'no answer'}): {err.get('message') or 'no page came back'}")
    ok, reason = S.check_logged_in(platform, final, html, status or 200, text=res.get("text"))
    if ok:
        return done(ACCEPTED, "the platform's own page showed a logged-in state" + (f" (read over plain HTTP because {why_plain})" if plain else ""))
    if plain:
        return done(UNCONFIRMED, f"{reason}; but the page was read over plain HTTP because {why_plain}, and this site may only show its logged-in state in a browser")
    if reason.startswith(AMBIGUOUS):                                                      # nothing says logged in, and nothing says logged out either: an error page, a half-drawn page, an empty one
        streak = ((ledger.data.get(platform) or {}).get("unconfirmed_streak", 0) + 1) if ledger is not None else 1
        if streak < UNCONFIRMED_LIMIT:
            return done(UNCONFIRMED, f"the page gave no sign of being logged in or logged out (it may be an error or a half-drawn page), so it is not counted as a refusal and the proof is kept; "
                                     f"it is looked at again on the next check, and after {UNCONFIRMED_LIMIT} such checks in a row it is treated as refused (this is check {streak})")
        return done(REJECTED, f"no sign of being logged in was found on {streak} checks in a row (any doubt counts as not logged in): the stored session was not accepted; capture a new one")
    return done(REJECTED, f"{reason}: the stored session was not accepted; capture a new one")


def check_all(settings, contact, python, *, platforms=None, environ=None, runner=subprocess.run, now="", ledger=None, offline=False):
    """Every requested platform, each on its own and in the same order every time. Saves the ledger when it makes requests."""
    platforms = list(platforms or S.PLATFORMS)
    rows = [check_platform(p, settings, contact, python, environ=environ, runner=runner, now=now, ledger=ledger, offline=offline) for p in platforms]
    if ledger is not None and not offline:
        ledger.save()
    return rows


def evidence_lines(r):
    """What the page showed, in a few indented lines (none when no page was loaded). An ACCEPTED platform gets one line; every other one gets the full evidence, because that is the one to diagnose."""
    ev = r.get("evidence")
    if not ev:
        return []
    shown = f"read with the {ev['fetched_with']}; HTTP {ev['status'] or 'no answer'}; landed on {ev['landed'] or 'no address'}; title '{ev['title']}'" if ev["title"] else \
            f"read with the {ev['fetched_with']}; HTTP {ev['status'] or 'no answer'}; landed on {ev['landed'] or 'no address'}; no title"
    found = ev["logged_in_found"]
    markers = f"logged-in markers found: {', '.join(found)}" if found else f"logged-in markers found: none of {len(ev['logged_in_missing'])} ({', '.join(ev['logged_in_missing'])})"
    if ev["logged_in_text_found"] or ev["logged_in_text_missing"]:
        in_text = ev["logged_in_text_found"]
        markers += "; " + (f"logged-in words in the visible text: {', '.join(in_text)}" if in_text else f"logged-in words in the visible text: none of {len(ev['logged_in_text_missing'])} ({', '.join(ev['logged_in_text_missing'])})")
    if r["state"] == ACCEPTED:
        return [f"      page: {shown}; {markers}"]
    if ev["visible_text_available"]:
        out_words = f"logged-out wording in the visible text: {', '.join(ev['logged_out_in_text']) or 'none'}" + (f"; only in the page's code (not counted): {', '.join(ev['logged_out_only_in_code'])}" if ev["logged_out_only_in_code"] else "")
    else:
        out_words = "no visible text was available, so the logged-out wording was not judged" + (f"; in the page's code (not counted): {', '.join(ev['logged_out_only_in_code'])}" if ev["logged_out_only_in_code"] else "")
    lines = [f"      page: {shown}",
             f"      cookies sent (names): {', '.join(ev['cookies_sent']) or 'none'}; the platform sent back (names): {', '.join(ev['cookies_back']) or 'none'}",
             f"      {markers}; {out_words}; password field on the page: {'yes' if ev['password_field'] else 'no'}"
             + (f"; login or challenge word in the address: {ev['login_word_in_address']}" if ev["login_word_in_address"] else "")]
    if ev["text_starts"]:
        lines.append(f"      visible text starts: '{ev['text_starts']}'")
    else:
        lines.append(f"      page code: {ev['html_chars']} characters{' (cut at the limit: the rest was not searched)' if ev.get('html_truncated') else ''}, {ev['scripts']} script tags" + (f", element ids: {', '.join(ev['body_ids'])}" if ev["body_ids"] else ", no element ids")
                     + (f"; the no-script note says: '{ev['noscript']}'" if ev["noscript"] else "; no no-script note"))
    if ev["worker_error"] and ev["worker_error"][:120] not in " ".join(str(r.get("reason") or "").split()):          # said once: not again when the verdict already carries it
        lines.append(f"      the worker said: {ev['worker_error']}")
    tw = r.get("twscrape")
    if tw:
        lines.append(f"      last-resort twscrape check: {tw['state']}: {tw['detail']}")
    return lines


def format_report(rows, offline=False, discovery=""):
    """One line per platform and a count line; what ACCEPTED does and does not mean. Under each platform that was really checked, what its page showed."""
    width = max(len(r["label"]) for r in rows) if rows else 10
    head = "Stored login sessions: no request was made, each platform shows what is stored" if offline else "Stored login sessions: each platform is judged on its own, live"
    lines = [head + " (a missing secret is not a failure; an expired, refused or unusable one needs a new capture):"]
    for r in rows:
        left = "" if r["days_left"] == "" else (" (less than a day left)" if r["days_left"] == 0 else f" ({r['days_left']} day(s) left)")
        expiry = f"; expires {r['expires_on']}{left}" if r["expires_on"] else ""
        proof = f"; last proved {r['proved_on'][:10]}" + (f" (through {r['proved_via']}, not the browser)" if r.get("proved_via") == "twscrape" else "") if r.get("proved_on") else ""
        lines.append(f"  {r['label']:<{width}}  {r['state']:<12} {r['reason']}{expiry}{proof}")
        if not offline:
            lines.extend(evidence_lines(r))
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    order = [s for s in ("PRESENT", *ORDER) if counts.get(s)]
    lines.append("  " + ", ".join(f"{counts[s]} {s}" for s in order) if order else "  no platform")
    if discovery:
        lines.append(f"  If a session fails, a read falls back to discovery (the search layer, no login): {discovery}")
    if not offline:
        lines.append("  ACCEPTED means the platform's own page showed a logged-in state for the stored session from this runner. It does not mean every kind of read works: see each platform's limits in docs/platform/session-capture-guide.md.")
    return "\n".join(lines)


def exit_code(rows):
    """1 when a platform that HAS a stored session needs a new capture (a person has to act); 0 otherwise. A secret that is not set, a platform switched off, or a check that could not be made here never turns a run red."""
    return 1 if any(r["state"] in OWNER_ACTION for r in rows) else 0


# ---- one logged-in page through the fallback chain ------------------------------------------------------------------------------------------------

DATA_URI = re.compile(r"data:[\w/+.\-]+;base64,[A-Za-z0-9+/=]{20,}")
LONG_TOKEN = re.compile(r"\S{200,}")
MIN_READABLE_CHARS, MIN_READABLE_WORDS = 80, 12


def clean_text(text):
    """A page's text for the log and the corpus: image data and long unbroken tokens (the junk a script shell leaves) are replaced by a short note, so they are never printed or counted as content."""
    text = DATA_URI.sub("[image]", str(text or ""))
    return LONG_TOKEN.sub("[data]", text)


def readable(text):
    """True when there is real text to read: enough characters and enough words made of letters. A script shell that is only a title, an icon name and an image is not."""
    words = [w for w in str(text).split() if any(c.isalpha() for c in w) and len(w) <= 30]
    return len(str(text).strip()) >= MIN_READABLE_CHARS and len(words) >= MIN_READABLE_WORDS


def humanise_error(code, message, platform=""):
    """A worker error in plain words, keeping the original at the end. The browser's own wording (net::ERR_...) means little to a reader. Only LinkedIn is named for status 999: it is what LinkedIn does,
    and saying it about another platform would be a guess."""
    raw = " ".join(str(message or "").split("Call log:")[0].split())
    low = raw.lower()
    if "err_http_response_code_failure" in low:
        plain = "the platform answered the browser with an error status instead of a page, which is its own refusal of an automated visit" + (" (LinkedIn does this with status 999)" if platform == "linkedin" else " (the browser does not say which status)")
    elif "timeout" in low and ("goto" in low or "exceeded" in low):
        plain = "the page did not finish loading in time"
    elif "net::err_" in low:
        plain = "a network error stopped the page from loading"
    else:
        return f"{code}: {raw}"[:300]
    return f"{plain} ({code}: {raw[:150]})"


CHALLENGE_TEXT = ("drag the puzzle piece", "verify you are human", "performing security verification", "just a moment", "captcha", "access denied", "are you a robot", "unusual traffic")
WHY_NOT_CONTENT = {"script shell": "a script shell, an icon or an image", "challenge page": "a bot-check or puzzle page, not content", "too little": "too little to be a page", "empty": "no text came back"}


def judge_content(text):
    """What a read really returned: (quality, detail). 'real text' only when there is enough of it; otherwise 'script shell' (a title, an icon name, an image), 'challenge page' (a bot-check or puzzle),
    'too little' or 'empty'. The size of what came back is not the test: twenty thousand characters of image data is a script shell."""
    clean = " ".join(clean_text(text).split())
    if not clean:
        return "empty", WHY_NOT_CONTENT["empty"]
    low = clean.lower()
    if len(clean) < 600 and any(word in low for word in CHALLENGE_TEXT):
        return "challenge page", WHY_NOT_CONTENT["challenge page"]
    if readable(clean):
        words = sum(1 for w in clean.split() if any(c.isalpha() for c in w) and len(w) <= 30)
        return "real text", f"{len(clean)} characters, {words} words"
    return ("script shell" if ("[image]" in clean or "[data]" in clean) else "too little"), WHY_NOT_CONTENT["script shell" if ("[image]" in clean or "[data]" in clean) else "too little"]


def _rows_from(res, route):
    text = clean_text(res.get("text") or res.get("markdown") or "")
    quality, _ = judge_content(text)
    if text.strip() and quality != "real text":
        raise K.Failure(K.UPSTREAM_BROKEN, f"the page came back with no readable text ({len(text.strip())} characters: {WHY_NOT_CONTENT[quality]})")
    return [{"url": res.get("url"), "final_url": res.get("final_url"), "title": res.get("title") or "", "text": text, "chars": len(text), "route": route, "quality": quality}] if text.strip() else []


def _failure_from(res, platform=""):
    err = res.get("error") or {}
    message = err.get("message") or "the page could not be read"
    code = {"NEED_SETUP": K.NEED_SETUP, "NEED_LOGIN": K.NEED_LOGIN, "RATE_LIMITED": K.RATE_LIMITED, "PLATFORM_BLOCKED": K.PLATFORM_BLOCKED, "NOT_FOUND": K.NOT_FOUND, "BAD_INPUT": K.BAD_INPUT}.get(err.get("code"), K.UPSTREAM_BROKEN)
    if WALL.search(message):
        return K.Failure(K.NEED_LOGIN, message)
    return K.Failure(code, humanise_error(err.get("code") or "no answer", message, platform))


def _read_once(platform, url, settings, contact, python, environ, runner, with_session):
    """One read through the worker, summarised: did it work, how much text, the title, where it landed, the first words, or why it failed. Never raises."""
    try:
        res = _worker_page(platform, url, settings, contact, python, environ, runner, with_session=with_session, reading=True)
    except K.Failure as exc:
        return {"ok": False, "chars": 0, "words": set(), "title": "", "landed": "", "starts": "", "error": str(exc)}
    text = " ".join(clean_text(res.get("text") or "").split())
    err = res.get("error") or {}
    worked = bool(res.get("ok"))
    quality = judge_content(text)[0] if worked else ""
    if worked and quality != "real text":
        worked, error = False, f"UPSTREAM_BROKEN: the page came back with no readable text ({len(text)} characters: {WHY_NOT_CONTENT[quality]})"
    else:
        error = "" if worked else humanise_error(err.get("code") or "no answer", err.get("message") or "no page came back", platform)
    return {"ok": worked, "chars": len(text), "words": set(text.lower().split()), "title": str(res.get("title") or ""), "landed": S._landed(res.get("final_url") or ""), "starts": text[:300], "error": error, "quality": quality}


def compare_page(platform, url, settings, contact, python, *, environ=None, runner=subprocess.run, now="", ledger=None):
    """The same address read twice, once with no login and once through the stored session (only when the owner has it on and a live check passed), so the question 'what does the session add?' has an
    answer in characters and in words the public read did not show. One attempt each. The address must be on the platform's own hosts. Returns {"public", "session", "verdict"}."""
    environ = os.environ if environ is None else environ
    now = _now(now)
    if platform not in S.PLATFORMS:
        raise K.Failure(K.BAD_INPUT, f"no stored-session platform called {platform!r}; the platforms are {', '.join(S.PLATFORMS)}")
    if not _host_ok(platform, url):
        raise K.Failure(K.BAD_INPUT, f"{url} is not on {', '.join(S.PLATFORMS[platform]['hosts'])}: a stored session is never sent to another site")
    public = _read_once(platform, url, settings, contact, python, environ, runner, False)
    on, why = switched_on(settings, platform)
    if not on:
        session = {"used": False, "reason": why}
    elif ledger is None or not ledger.proved(platform, now):
        session = {"used": False, "reason": f"no live proof yet for {platform}: run the sessions check first (a session is only used after the platform accepted it in the last {PROVED_DAYS} days)"}
    else:
        session = {"used": True, **_read_once(platform, url, settings, contact, python, environ, runner, True)}
    if not session["used"]:
        verdict = "only the public read was made: " + session["reason"] if public["ok"] else "the public read failed and the session was not used: " + session["reason"]
    elif public["ok"] and session["ok"]:
        new_words = len(session["words"] - public["words"])
        if session["chars"] > public["chars"] * 1.2 and new_words >= 20:
            verdict = f"the session adds content: {session['chars'] - public['chars']} more characters and {new_words} words the public read did not show"
        else:
            verdict = f"the session adds little or nothing here: {session['chars']} characters against {public['chars']} in public, {new_words} words not in the public read"
    elif session["ok"]:
        verdict = "only the session read works: the public read failed (" + public["error"] + ")"
    elif public["ok"]:
        verdict = "the public read works and the session read failed (" + session["error"] + "): the session adds nothing here"
    else:
        verdict = "neither read works (public: " + public["error"] + "; session: " + session["error"] + ")"
    return {"public": public, "session": session, "verdict": verdict}


MAX_READS = 200


class ReadLog:
    """data/_shared/sessions_reads.json: the last MAX_READS reads and compares, newest last, so they outlive the run logs: what was asked (the address without any query), which route answered, the
    verdict, how much real text came back and what kind of content it was. The first words are kept for a read with no login (and for discovery); never for a read through a stored session."""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get("CONTENTINTEL_DATA_DIR", "data")) / "_shared" / "sessions_reads.json"
        self.items = []
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = [i for i in loaded if isinstance(i, dict)][-MAX_READS:] if isinstance(loaded, list) else []
            except (OSError, ValueError):
                self.items = []

    def add(self, entry, now):
        self.items = (self.items + [{"at": now, **entry}])[-MAX_READS:]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_entry(platform, url, result):
    """The stored record of one read: the route that answered (or none), why nothing did, the quality of what came back and the trail of routes tried."""
    row = result.rows[0] if result.rows else {}
    by_session = result.route == "session"
    quality = row.get("quality") or ("snippets about the address, not the page" if result.route == "discovery" else "")
    if row.get("left_out"):
        quality += f"; {len(row['left_out'])} result(s) about a different account ({', '.join(row['left_out'])}) left out"
    return {"kind": "read", "platform": platform, "url": S._landed(url), "route": result.route or "none", "skipped": bool(result.skipped), "reason": " ".join(str(result.reason).split())[:300],
            "quality": quality, "chars": int(row.get("chars") or 0), "title": str(row.get("title") or "")[:200], "landed": S._landed(row.get("final_url") or ""),
            "starts": "" if by_session else " ".join(str(row.get("text") or "").split())[:200], "trail": [[n, o, " ".join(str(d).split())[:200]] for n, o, d in result.trail]}


def compare_entry(platform, url, got):
    """The stored record of one compare: the verdict, and for each of the two reads whether it worked, how much real text, what kind, where it landed, why not."""
    def part(p, words):
        out = {"ok": bool(p.get("ok")), "chars": int(p.get("chars") or 0), "quality": str(p.get("quality") or ""), "title": str(p.get("title") or "")[:200], "landed": str(p.get("landed") or ""),
               "error": " ".join(str(p.get("error") or "").split())[:300]}
        if words:
            out["starts"] = " ".join(str(p.get("starts") or "").split())[:200]
        return out
    session = got["session"]
    return {"kind": "compare", "platform": platform, "url": S._landed(url), "verdict": " ".join(str(got["verdict"]).split())[:600], "public": part(got["public"], True),
            "session": ({"used": True, **part(session, False)} if session.get("used") else {"used": False, "reason": " ".join(str(session.get("reason", "")).split())[:300]})}


SESSIONS_TAB, SESSION_READS_TAB = "Sessions", "Session Reads"
SESSIONS_COLUMNS = ["platform", "label", "state", "reason", "checked_at", "proved_on", "proved_via", "expires_on", "days_left", "landed", "http_status", "fetched_with", "html_chars", "text_chars",
                    "logged_in_markers_found", "logged_in_markers_missing", "logged_out_wording", "password_field", "worker_error", "twscrape"]
READS_COLUMNS = ["read_id", "at", "kind", "platform", "url", "route", "quality", "chars", "title", "landed", "verdict", "public_chars", "public_quality", "session_used", "session_chars",
                 "session_quality", "starts", "reason"]


def sessions_rows(ledger):
    """One row per platform for the Sessions tab, from the tool's own record (sessions_health.json): the verdict, the whole reason, the dates and the evidence, readable at a glance. A platform that has never
    been checked says so. Never a cookie value; the page's words are not in the record."""
    rows = {}
    for platform, spec in S.PLATFORMS.items():
        got = ledger.data.get(platform) or {}
        ev = got.get("evidence") or {}
        tw = got.get("twscrape") or {}
        rows[platform] = {
            "platform": platform, "label": spec["label"], "state": got.get("state") or "NOT CHECKED", "reason": got.get("reason") or "no check has been run for this platform yet",
            "checked_at": got.get("checked_at", ""), "proved_on": got.get("proved_on", ""), "proved_via": got.get("proved_via", "") if got.get("proved_on") else "",
            "expires_on": got.get("expires_on", ""), "days_left": got.get("days_left", ""), "landed": ev.get("landed", ""), "http_status": ev.get("status", ""), "fetched_with": ev.get("fetched_with", ""),
            "html_chars": ev.get("html_chars", ""), "text_chars": ev.get("text_chars", ""),
            "logged_in_markers_found": ", ".join(list(ev.get("logged_in_found") or []) + list(ev.get("logged_in_text_found") or [])),
            "logged_in_markers_missing": ", ".join(list(ev.get("logged_in_missing") or []) + list(ev.get("logged_in_text_missing") or [])),
            "logged_out_wording": ", ".join(ev.get("logged_out_in_text") or []), "password_field": ("yes" if ev.get("password_field") else "no") if ev else "",
            "worker_error": ev.get("worker_error", ""), "twscrape": (f"{tw.get('state', '')}: {tw.get('detail', '')}" if tw else "")}
    return rows


def read_id(entry):
    """A stable key for one stored read or compare: when, what and where. The same entry always gets the same key, so a sheet row is updated, not repeated."""
    stamp = hashlib.sha1(f"{entry.get('url', '')}".encode("utf-8")).hexdigest()[:6]
    return f"{entry.get('at', '')}|{entry.get('platform', '')}|{entry.get('kind', '')}|{stamp}"


def reads_rows(reads):
    """One row per stored read or compare for the Session Reads tab, newest last, from sessions_reads.json."""
    rows = {}
    for entry in reads.items:
        key = read_id(entry)
        pub, ses = entry.get("public") or {}, entry.get("session") or {}
        trail = " | ".join(f"{n}: {o}" + (f" ({d})" if d else "") for n, o, d in (entry.get("trail") or []))
        rows[key] = {
            "read_id": key, "at": entry.get("at", ""), "kind": entry.get("kind", ""), "platform": entry.get("platform", ""), "url": entry.get("url", ""), "route": entry.get("route", ""),
            "quality": entry.get("quality", ""), "chars": entry.get("chars", "") if entry.get("kind") == "read" else "", "title": entry.get("title", "") or pub.get("title", ""),
            "landed": entry.get("landed", "") or pub.get("landed", ""), "verdict": entry.get("verdict", ""), "public_chars": pub.get("chars", ""), "public_quality": pub.get("quality", ""),
            "session_used": ("yes" if ses.get("used") else "no") if entry.get("kind") == "compare" else "", "session_chars": ses.get("chars", "") if ses.get("used") else "",
            "session_quality": ses.get("quality", "") if ses.get("used") else "", "starts": entry.get("starts", "") or pub.get("starts", ""),
            "reason": entry.get("reason", "") or trail or ses.get("reason", "") or ses.get("error", "")}
    return rows


def sheet_tabs(ledger, reads):
    """The two Sheets tabs that mirror the tool's own record: Sessions (one row per platform) and Session Reads (every stored read and compare). A tab is offered only when its record has something in it,
    so a sheet gains no empty tab before the first check. The JSON files stay the record; the sheet is the readable copy."""
    tabs = []
    if ledger.data:
        tabs.append({"title": SESSIONS_TAB, "columns": SESSIONS_COLUMNS, "rows_by_key": sessions_rows(ledger), "key_col": "platform", "starter_manual": []})
    if reads.items:
        tabs.append({"title": SESSION_READS_TAB, "columns": READS_COLUMNS, "rows_by_key": reads_rows(reads), "key_col": "read_id", "starter_manual": []})
    return tabs


SEARCH_ORDER = ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp")                 # the discovery layer's providers, in the order they are tried (the first one that has a key is asked): the ones that return snippets come first, because Exa's search here asks for titles and links only (page text is billed separately)


def discovery_providers(environ):
    """[(name, provider)] for the search providers that have a key in `environ`, in SEARCH_ORDER."""
    from . import providers as P
    built = P.build_providers(environ)
    return [(name, built[name]) for name in SEARCH_ORDER if name in built]


def discovery_status(environ):
    """One sentence for the log: whether the discovery fallback can run here, and through which provider."""
    try:
        names = [n for n, _ in discovery_providers(environ)]
    except Exception:
        names = []
    return (f"available here, through {names[0]}" + (f" (then {', '.join(names[1:])})" if len(names) > 1 else "")) if names else \
        "not available in this run: no search key (EXA_API_KEY, SERPER_API_KEY or TAVILY_API_KEY) is passed to it"


def _query_for(url):
    """The address as words a search engine understands: the site's name as it is, then the path's words."""
    text = re.sub(r"^https?://(www\.)?", "", str(url).split("?")[0].split("#")[0].lower())
    host, _, path = text.partition("/")
    return " ".join([host, *re.sub(r"[/_\-.]+", " ", path).split()])[:120]


NOT_ACCOUNTS = frozenset({"p", "reel", "reels", "tv", "explore", "stories", "hashtag", "tags", "tag", "video", "watch", "status", "i", "in", "company", "school", "posts", "groups", "pages", "r", "user", "search", "channel", "c", "feed", "web", "share", "photo", "photos", "events", "marketplace", "login", "accounts", "directory", "locations", "popular", "settings", "intent", "hashtags", "topics", "discover", "music", "place", "t", "u", "@"})


def _account_of(url):
    """(host, account) of an address whose first path word names an account (instagram.com/name, tiktok.com/@name, x.com/name), else (host, ''). Words like /reel/ or /explore/ are not accounts."""
    text = re.sub(r"^https?://(www\.)?", "", str(url or "").split("?")[0].split("#")[0].lower())
    host, _, path = text.partition("/")
    first = path.split("/")[0].lstrip("@").strip()
    return host, ("" if not first or first in NOT_ACCOUNTS else first)


def _other_account(target, found):
    """The other account's name (as @name) when a search result is a profile of a DIFFERENT account on the same site as the address that was asked for (swoveralls_ when swoveralls was asked for), else ''.
    A result on another site, or about a post or a page, is not judged here. Accounts are compared exactly: a near match is a different account."""
    t_host, t_account = _account_of(target)
    f_host, f_account = _account_of(found)
    return f"@{f_account}" if t_account and f_account and f_host == t_host and f_account != t_account else ""


def discovery_rows(url, environ, providers=None):
    """The discovery fallback: ONE search about the address through the first search provider that has a key. These are titles and snippets from the search layer, not the page itself, and no login is used."""
    from . import providers as P
    found = providers if providers is not None else discovery_providers(environ)
    if not found:
        raise K.Failure(K.NEED_SETUP, "no search key is passed to this run (EXA_API_KEY, SERPER_API_KEY or TAVILY_API_KEY), so the discovery fallback has nothing to ask")
    name, provider = found[0]
    try:
        results = provider.search(_query_for(url), kind="web", num=5)
    except P.AuthError as exc:
        raise K.Failure(K.NEED_SETUP, str(exc)) from None
    except P.QuotaError as exc:
        raise K.Failure(K.RATE_LIMITED, str(exc)) from None
    except P.ProviderError as exc:
        raise K.Failure(K.UPSTREAM_BROKEN, str(exc)) from None
    rows = [{"url": r.url, "final_url": r.url, "title": r.title, "text": r.snippet or "", "chars": len(r.snippet or ""), "route": "discovery", "provider": name} for r in results if r.snippet or r.title]
    kept, left_out = [], []
    for row in rows:
        other = _other_account(url, row["url"])
        (left_out.append(other) if other else kept.append(row))
    if not kept and left_out:
        raise K.Failure(K.NOT_FOUND, f"the search found only a different account ({', '.join(sorted(set(left_out)))}) for {url}: an account with a similar name is never used in its place")
    if kept and left_out:
        kept[0]["left_out"] = sorted(set(left_out))                   # said in the stored record and the log: these results were about a different account
    return kept


def read_page(platform, url, settings, contact, python, *, environ=None, runner=subprocess.run, now="", ledger=None, log=None, providers=None):
    """Read one address of the platform: the plain reader first, then the stored session (only on the platform's own hosts, only while the owner has it on and a live check passed in the last
    PROVED_DAYS), then discovery (the search layer: no login, snippets about the address rather than the page), then an honest skip. One attempt each, no retry loop. Returns the chain's result; its
    rows say which route answered."""
    environ = os.environ if environ is None else environ
    now = _now(now)
    if platform not in S.PLATFORMS:
        raise K.Failure(K.BAD_INPUT, f"no stored-session platform called {platform!r}; the platforms are {', '.join(S.PLATFORMS)}")
    if not _host_ok(platform, url):
        raise K.Failure(K.BAD_INPUT, f"{url} is not on {', '.join(S.PLATFORMS[platform]['hosts'])}: a stored session is never sent to another site")
    on, why = switched_on(settings, platform)

    def primary():
        res = _worker_page(platform, url, settings, contact, python, environ, runner, with_session=False, reading=True)
        if not res.get("ok"):
            raise _failure_from(res, platform)
        return _rows_from(res, "primary")

    def session():
        res = _worker_page(platform, url, settings, contact, python, environ, runner, with_session=True, reading=True)
        if not res.get("ok"):
            raise _failure_from(res, platform)
        return _rows_from(res, "session")

    proved = ledger.proved(platform, now) if ledger is not None else False
    off = "" if on and proved else (why if not on else f"no live proof yet for {platform}: run the sessions check first (a session is only used after the platform accepted it in the last {PROVED_DAYS} days)")
    def discovery():
        return discovery_rows(url, environ, providers)

    return SC.run_chain(f"{platform} page", [SC.Step("primary", primary), SC.Step("session", session, enabled=on and proved, off_reason=off), SC.Step("discovery", discovery)], log=log)
