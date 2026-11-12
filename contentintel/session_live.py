"""Stored login sessions in use (Update 50): prove, one platform at a time, that a platform really accepts its stored session from a runner, and read one logged-in page through the fallback chain.

Each platform is judged on its own and reported on its own line: X, Instagram, TikTok, Reddit, Facebook, LinkedIn, Xiaohongshu and Boss never share a result. A session is only ever used on that platform's own
hosts, through the Scrapling worker (which is handed the one named secret and nothing else), and a platform whose check has not passed recently is never read through its session by the chain.

What ACCEPTED means: the platform's own page, loaded with the stored session from this runner, showed a logged-in state. It does not mean every kind of read works (see each platform's `can`).
"""
from __future__ import annotations

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

    def record(self, platform, state, reason, now):
        entry = self.data.setdefault(platform, {"state": "", "reason": "", "checked_at": "", "proved_on": ""})
        entry.update(state=state, reason=str(reason)[:300], checked_at=now)
        if state == ACCEPTED:
            entry["proved_on"] = now
        elif state in CLEARS_PROOF:
            entry["proved_on"] = ""

    def proved_on(self, platform):
        return (self.data.get(platform) or {}).get("proved_on", "")

    def proved(self, platform, now):
        """True only when a live check passed within PROVED_DAYS and nothing since has voided it."""
        then, here = _parse(self.proved_on(platform)), _parse(_now(now))
        return bool(then and here and timedelta(0) <= here - then <= timedelta(days=PROVED_DAYS))

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

def _worker_page(platform, url, settings, contact, python, environ, runner, *, with_session, timeout=180):
    """One address through the Scrapling worker, with the stored session or without it. Returns the worker's result for the address; raises K.Failure for what could not be done at all."""
    from .observatory import worker_client as wc
    adv = {"stealth": True}
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


def check_platform(platform, settings, contact, python, *, environ=None, runner=subprocess.run, now="", ledger=None, offline=False):
    """One platform's result as a dict: state (ACCEPTED, REJECTED, EXPIRED, BAD CAPTURE, CANNOT CHECK, UNCONFIRMED, NOT SET UP or OFF), why, expiry and the day it was last proved.
    `offline` makes no request (status only). Never raises."""
    environ = os.environ if environ is None else environ
    now = _now(now)
    spec = S.PLATFORMS[platform]
    row = {"platform": platform, "label": spec["label"], "secret": spec["secret"], "can": spec["can"], "lifetime": spec["lifetime"], "state": "", "reason": "", "expires_on": "", "days_left": "", "proved_on": ""}

    def done(state, reason, record=True):
        row.update(state=state, reason=reason)
        if ledger is not None:
            if record and not offline:
                ledger.record(platform, state, reason, now)
            row["proved_on"] = ledger.proved_on(platform)
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
    if err.get("code") == "PLATFORM_BLOCKED":
        return done(BLOCKED, f"{spec['label']} showed a bot-check or challenge page to this runner, so the session itself could not be judged"
                    + (f"; and {why_plain}" if plain else "") + ". This is the platform blocking the runner, not a bad session")
    if not html:
        if status in (401, 403):
            return done(REJECTED, f"the site answered {status}: either it refused the session or it blocked this runner; capture a new session, and if it repeats the platform blocks runners")
        return done(CANNOT_CHECK, f"the page could not be read ({err.get('code') or status or 'no answer'}): {err.get('message') or 'no page came back'}")
    ok, reason = S.check_logged_in(platform, final, html, status or 200)
    if ok:
        return done(ACCEPTED, "the platform's own page showed a logged-in state" + (f" (read over plain HTTP because {why_plain})" if plain else ""))
    if plain:
        return done(UNCONFIRMED, f"{reason}; but the page was read over plain HTTP because {why_plain}, and this site may only show its logged-in state in a browser")
    return done(REJECTED, f"{reason}: the stored session was not accepted; capture a new one")


def check_all(settings, contact, python, *, platforms=None, environ=None, runner=subprocess.run, now="", ledger=None, offline=False):
    """Every requested platform, each on its own and in the same order every time. Saves the ledger when it makes requests."""
    platforms = list(platforms or S.PLATFORMS)
    rows = [check_platform(p, settings, contact, python, environ=environ, runner=runner, now=now, ledger=ledger, offline=offline) for p in platforms]
    if ledger is not None and not offline:
        ledger.save()
    return rows


def format_report(rows, offline=False):
    """One line per platform and a count line; what ACCEPTED does and does not mean."""
    width = max(len(r["label"]) for r in rows) if rows else 10
    head = "Stored login sessions: no request was made, each platform shows what is stored" if offline else "Stored login sessions: each platform is judged on its own, live"
    lines = [head + " (a missing secret is not a failure; an expired, refused or unusable one needs a new capture):"]
    for r in rows:
        left = "" if r["days_left"] == "" else (" (less than a day left)" if r["days_left"] == 0 else f" ({r['days_left']} day(s) left)")
        expiry = f"; expires {r['expires_on']}{left}" if r["expires_on"] else ""
        proof = f"; last proved {r['proved_on'][:10]}" if r.get("proved_on") else ""
        lines.append(f"  {r['label']:<{width}}  {r['state']:<12} {r['reason']}{expiry}{proof}")
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    order = [s for s in ("PRESENT", *ORDER) if counts.get(s)]
    lines.append("  " + ", ".join(f"{counts[s]} {s}" for s in order) if order else "  no platform")
    if not offline:
        lines.append("  ACCEPTED means the platform's own page showed a logged-in state for the stored session from this runner. It does not mean every kind of read works: see each platform's limits in docs/platform/session-capture-guide.md.")
    return "\n".join(lines)


def exit_code(rows):
    """1 when a platform that HAS a stored session needs a new capture (a person has to act); 0 otherwise. A secret that is not set, a platform switched off, or a check that could not be made here never turns a run red."""
    return 1 if any(r["state"] in OWNER_ACTION for r in rows) else 0


# ---- one logged-in page through the fallback chain ------------------------------------------------------------------------------------------------

def _rows_from(res, route):
    text = res.get("text") or res.get("markdown") or ""
    return [{"url": res.get("url"), "final_url": res.get("final_url"), "title": res.get("title") or "", "text": text, "chars": len(text), "route": route}] if text else []


def _failure_from(res):
    err = res.get("error") or {}
    message = err.get("message") or "the page could not be read"
    code = {"NEED_SETUP": K.NEED_SETUP, "NEED_LOGIN": K.NEED_LOGIN, "RATE_LIMITED": K.RATE_LIMITED, "PLATFORM_BLOCKED": K.PLATFORM_BLOCKED, "NOT_FOUND": K.NOT_FOUND, "BAD_INPUT": K.BAD_INPUT}.get(err.get("code"), K.UPSTREAM_BROKEN)
    if WALL.search(message):
        code = K.NEED_LOGIN
    return K.Failure(code, message)


def read_page(platform, url, settings, contact, python, *, environ=None, runner=subprocess.run, now="", ledger=None, log=None):
    """Read one address of the platform: the plain reader first, then the stored session (only on the platform's own hosts, only while the owner has it on and a live check passed in the last
    PROVED_DAYS), then an honest skip. One attempt each, no retry loop. Returns the chain's result; its rows say which route answered."""
    environ = os.environ if environ is None else environ
    now = _now(now)
    if platform not in S.PLATFORMS:
        raise K.Failure(K.BAD_INPUT, f"no stored-session platform called {platform!r}; the platforms are {', '.join(S.PLATFORMS)}")
    if not _host_ok(platform, url):
        raise K.Failure(K.BAD_INPUT, f"{url} is not on {', '.join(S.PLATFORMS[platform]['hosts'])}: a stored session is never sent to another site")
    on, why = switched_on(settings, platform)

    def primary():
        res = _worker_page(platform, url, settings, contact, python, environ, runner, with_session=False)
        if not res.get("ok"):
            raise _failure_from(res)
        return _rows_from(res, "primary")

    def session():
        res = _worker_page(platform, url, settings, contact, python, environ, runner, with_session=True)
        if not res.get("ok"):
            raise _failure_from(res)
        return _rows_from(res, "session")

    proved = ledger.proved(platform, now) if ledger is not None else False
    off = "" if on and proved else (why if not on else f"no live proof yet for {platform}: run the sessions check first (a session is only used after the platform accepted it in the last {PROVED_DAYS} days)")
    return SC.run_chain(f"{platform} page", [SC.Step("primary", primary), SC.Step("session", session, enabled=on and proved, off_reason=off)], log=log)
