"""The password-login BACKUP for a stored session (the owner's decision of 2026-11-19: yes, as a backup, never direct).

ORDER, always: the stored COOKIE session first. Cookies are the already-logged-in state: no password is typed and nothing looks like a takeover. The password login is considered ONLY when the cookie session cannot be
proved: the last THREE checks in a row gave no sign either way (UNCONFIRMED, as TikTok does) or could not be made at all (CANNOT CHECK, as Instagram's redirects do), or the session was REJECTED or EXPIRED, and the platform has never
been ACCEPTED since. Then, and only then, the tool logs in ONCE with the username and password already saved as secrets, captures the fresh cookies, and proves the session with them in the same run.

HARD LIMITS (a login from GitHub's datacenter addresses looks like a takeover, and this account has already been warned once): at most one attempt per platform in 7 days and two in a month; a security check or a code the runner
cannot answer ends the attempt RIGHT THERE (no solving, no second try) and rests the backup for 2 days; an account warning rests it for 30 days; nothing is ever tried while an account is resting; it is never the default and never runs on a schedule.

HOW IT SITS WITH THE STEALTH LAYER'S COOKIE-ONLY RULE: the rule stands unchanged everywhere it applies. It does NOT rest on a `password_fallback: never` setting: browser.yaml says `when_cookies_fail` for Instagram and TikTok. It rests on the
IDENTITY GATE: with the stealth layer on, stealth.py sets the identity's `allow_login` to False and collectors.py refuses a login while it is False, so a missing or expired cookie there still means "nothing was read". If the stealth layer were ever
turned off, that gate would not apply and the browser.yaml fallback would be live for those two sites: the guarantee is the identity, not a `never` setting. This backup needs ONE narrow exception, for the sessions-PROOF path only: it does not go through the collectors' login routine and it does not touch any identity's setting; it is its own command
(`sessions --what password-backup --key <platform>`), owner-triggered, and the fresh cookies it captures live only in that run: they are never printed or saved. (To keep the platform's cookie session alive afterwards, export fresh cookies
into the platform's secret as before: a runner cannot write a secret.) Nothing in this file prints, stores or logs a password or a cookie value."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROOF_FAILS = ("UNCONFIRMED", "CANNOT CHECK", "REJECTED", "EXPIRED")
STREAK_NEEDED = 3
WEEK_DAYS, MONTH_MAX = 7, 2
REST_CHALLENGE_DAYS, REST_WARNING_DAYS = 2, 30            # a security check or code: 2 days (was 14); an account warning: 30


def _day(now):
    return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)


class AttemptLedger:
    """data/_shared/session_password.json: when each platform's password backup was tried and how it ended. Never holds a password or a cookie."""
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except ValueError:
            data = {}
        self.data = data if isinstance(data, dict) else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")

    def attempts(self, platform):
        return list((self.data.get(platform) or {}).get("attempts", []))

    def rest_until(self, platform):
        return (self.data.get(platform) or {}).get("rest_until", "")

    def record(self, platform, now, outcome, rest_days=0):
        entry = self.data.setdefault(platform, {"attempts": []})
        entry["attempts"] = (entry["attempts"] + [{"at": now, "outcome": outcome}])[-12:]
        if rest_days:
            entry["rest_until"] = (_day(now) + timedelta(days=rest_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def eligible(entry, attempts, rest_until, now, *, has_credentials):
    """(ok, reason): may the password backup be tried for this platform now. `entry` is the session ledger's record of the platform."""
    entry = entry or {}
    if not has_credentials:
        return False, "no username and password are saved for this platform (secrets): there is nothing to log in with"
    if entry.get("state") == "ACCEPTED" and entry.get("proved_on"):
        return False, "the cookie session was accepted: the cookie session is always first and is working"
    streak = max(entry.get("unconfirmed_streak", 0), entry.get("cannot_check_streak", 0))
    failed = entry.get("state") in ("REJECTED", "EXPIRED")
    if not failed and streak < STREAK_NEEDED:
        return False, f"the cookie session is not yet shown to be unprovable: {streak} of {STREAK_NEEDED} checks in a row with no sign (or none could be made); keep checking first"
    if rest_until and _day(now) < _day(rest_until):
        return False, f"the backup is resting until {rest_until[:10]} after its last attempt ended in a security check or a warning"
    tries = [a for a in attempts if a.get("at")]
    if any(_day(now) - _day(a["at"]) < timedelta(days=WEEK_DAYS) for a in tries):
        return False, f"it was already tried in the last {WEEK_DAYS} days: at most one attempt a week"
    if sum(1 for a in tries if a["at"][:7] == now[:7]) >= MONTH_MAX:
        return False, f"it was already tried {MONTH_MAX} times this month"
    return True, ""


def attempt(platform, entry, creds, ledger, now, *, login_fn, prove_fn):
    """One backup attempt. login_fn(platform, username, password) -> fresh cookies text, or raises (a message with 'challenge' or 'warning' in its type name ends the attempt and rests the backup); prove_fn(platform,
    cookies_text) -> the live check result dict (state, why). Returns {status, detail, state}: status is 'skipped' (not eligible, nothing tried), 'proved', 'not_proved' or 'stopped'. The result never contains a secret."""
    ok, why = eligible(entry, ledger.attempts(platform), ledger.rest_until(platform), now, has_credentials=bool(creds.get("username") and creds.get("password")))
    if not ok:
        return {"status": "skipped", "detail": why, "state": ""}
    try:
        cookies = login_fn(platform, creds["username"], creds["password"])
    except Exception as exc:                                                                    # noqa: BLE001 - the kind of error is kept, never its message (it could hold a secret)
        kind = type(exc).__name__
        rest = REST_WARNING_DAYS if "Warning" in kind else REST_CHALLENGE_DAYS if "Challenge" in kind or "Login" in kind else 0
        ledger.record(platform, now, f"stopped: {kind}", rest)
        ledger.save()
        return {"status": "stopped", "detail": f"the login ended at {kind}: " + ("a security check or code the runner cannot answer; the backup rests " + str(rest) + " days" if rest else "no cookies were captured"), "state": ""}
    if not cookies:
        ledger.record(platform, now, "no cookies captured")
        ledger.save()
        return {"status": "stopped", "detail": "the login finished but no cookies were captured", "state": ""}
    result = prove_fn(platform, cookies)
    state = str((result or {}).get("state", ""))
    ledger.record(platform, now, "proved" if state == "ACCEPTED" else f"not proved: {state}")
    ledger.save()
    return {"status": "proved" if state == "ACCEPTED" else "not_proved", "detail": str((result or {}).get("why", ""))[:300], "state": state}


def real_login(browser_config="browser.yaml", debug_dir="debug"):
    """login_fn for a real run: opens the platform's browser WITHOUT cookies, types the saved username and password with the platform's own `login_steps` from browser.yaml, stops at a security check, and returns the fresh
    cookies as JSON text. It does not use the collectors' login routine, so every `password_fallback: never` and `allow_login: false` setting keeps its meaning there. Raises the browser layer's own Challenge, AccountWarning
    or LoginWall errors, whose type names `attempt` turns into rests. UNVERIFIED LIVE: the sandbox has no browser."""
    def login(platform, username, password):
        from . import browser as B
        from . import collectors as C
        cfg = B.load_browser_settings(browser_config)
        site_cfg = cfg["sites"].get(platform) or {}
        if not site_cfg.get("login_steps"):
            raise B.BrowserError(f"no login_steps for {platform} in {browser_config}")
        br = B.open_browser(platform, cfg, B.site_pacing(cfg, platform), "", debug_dir, headless=True)
        try:
            C.run_steps(br, site_cfg["login_steps"], {"username": username, "password": password})
            C.check_warning(br, platform, site_cfg)
            state = C.session_state(br, site_cfg)
            if state == "challenge":
                raise B.Challenge(f"{platform}: the login triggered a security check")
            if state == "login_wall":
                raise B.LoginWall(f"{platform}: the login did not open a session")
            return br.export_cookies()
        finally:
            closer = getattr(br, "close", None)
            if callable(closer):
                closer()
    return login


def real_prove(settings, contact, python, ledger, now, base_environ):
    """prove_fn for a real run: the ordinary live check of the platform, run with the fresh cookies in place of the stored secret (the run's own copy of the environment: nothing is written)."""
    def prove(platform, cookies_text):
        import base64
        from . import session_live as SL
        from . import sessions as S
        env = dict(base_environ)
        env[S.PLATFORMS[platform]["secret"]] = base64.b64encode(cookies_text.encode("utf-8")).decode("ascii")
        return SL.check_platform(platform, settings, contact, python, environ=env, now=now, ledger=ledger)
    return prove
