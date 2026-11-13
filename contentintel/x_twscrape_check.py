"""The last-resort check of an X session (Update 52): when x.com's web page shows the runner a bot-check, ask X's own profile interface through twscrape instead.

It runs as its own program inside its OWN isolated environment (twscrape brings its own libraries; none of them is in the core's) and prints exactly one line of JSON: {"state": ..., "detail": ...}.
  accepted     the account's own profile was read with the stored cookies: X accepts the session
  rejected     X answered that the cookies were not accepted, or that the account is locked or needs verification (the detail says which)
  unreachable  nothing could be judged: no profile came back (a bot-check, a block, a network problem, a timeout), or the session lacks what is needed. The caller keeps its BLOCKED result.

What it does, and does not do:
  - it uses only the cookies already in the stored session (auth_token and ct0, plus twid to know which profile to read): no password, no email, no new secret, no key, no card;
  - it reads ONE profile (the account's own, by its numeric id from the twid cookie); it never searches, never follows, never posts, never retries;
  - twscrape's usage reporting to an outside service (PostHog) is switched off before the library is imported, and this script never writes a cookie value anywhere;
  - the cookies are handed in through one environment variable (X_SESSION_COOKIES, an object of name and value) and its database is a temporary file that is deleted afterwards.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile

os.environ["TWS_TELEMETRY"] = "0"                      # twscrape reports usage to an outside service unless told not to; told, before it is imported
os.environ["DO_NOT_TRACK"] = "1"

REQUIRED = ("auth_token", "ct0")
USER_NAME = "session-check"


def user_id_from(jar):
    """The numeric id of the account from its twid cookie (a value such as u%3D1234567890 or u=1234567890), or ''."""
    raw = str((jar or {}).get("twid", ""))
    found = re.search(r"(?:u|%3D|=)\D{0,3}(\d{5,})", raw) or re.search(r"(\d{5,})", raw)
    return found.group(1) if found else ""


def explain(error_msg, active):
    """(state, why) from what twscrape recorded about the account, or None when it recorded nothing that says what X thought."""
    msg = str(error_msg or "")
    low = msg.lower()
    if "(32)" in msg or "could not authenticate" in low:
        return "rejected", "X did not accept the two cookies (error 32, could not authenticate you): the session has expired or was ended"
    if "(326)" in msg or "locked" in low or "denied by access control" in low:
        return "rejected", "X reports that the account is locked or needs a verification (error 326, denied by access control)"
    if "(88)" in msg:
        return "rejected", "X answered with a ban-like rate limit for this account (error 88)"
    if active is False and not msg:
        return "rejected", "X answered 403 and ended the session: it has expired or the account was banned"
    return None


def _scrub(text, secrets, bound=300):
    text = " ".join(str(text).split())
    for value in secrets:
        if len(str(value)) >= 6:
            text = text.replace(str(value), "***")
    return text[:bound]


def _capture_logs(logs):
    """twscrape says why a request failed in its own log (the reason an account is put aside for a while): keep its warnings in `logs` instead of letting them go to the console unread."""
    try:
        from loguru import logger
        logger.remove()
        logger.add(lambda message: logs.append(str(message).strip()), level="WARNING", format="{message}")
    except Exception:
        pass


async def check(jar, api_factory=None, timeout=60, logs=None):
    """The result for one session. `api_factory(db_path)` makes the twscrape API (a stand-in in tests). `logs` collects twscrape's own warnings, which explain a failure that gave no profile."""
    logs = [] if logs is None else logs
    secrets = [str(v) for v in (jar or {}).values()]
    missing = [n for n in REQUIRED if not (jar or {}).get(n)]
    if missing:
        return {"state": "unreachable", "detail": f"the session has no {' or '.join(missing)} cookie, so there is nothing to ask X with"}
    uid = user_id_from(jar)
    if not uid:
        return {"state": "unreachable", "detail": "the session has no twid cookie, so the profile to read is not known"}
    cookie_header = "; ".join(f"{n}={jar[n]}" for n in (*REQUIRED, "twid") if jar.get(n))
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "session-check.db")
        if api_factory is None:
            from twscrape import API                                                     # imported only now: after the telemetry switch above
            _capture_logs(logs)
            api_factory = lambda path: API(path, raise_when_no_account=True, wait_timeout=20)
        api = api_factory(db)
        user, error = None, None
        try:
            add = getattr(api.pool, "add_account_cookies", None)
            if add is not None:
                await add(USER_NAME, cookie_header)
            else:                                                                        # an older twscrape: the cookies go in through add_account
                await api.pool.add_account(USER_NAME, "_", "_", "_", cookies=cookie_header)
            user = await asyncio.wait_for(api.user_by_id(int(uid)), timeout)
        except asyncio.TimeoutError:
            error = "the profile read timed out"
        except Exception as exc:                                                         # a lost network, a missing account, a changed interface: nothing could be judged
            error = f"{exc.__class__.__name__}: {_scrub(exc, secrets)}"
        if user is not None:
            return {"state": "accepted", "detail": f"the profile of account id {uid} was read through X's own interface with the stored cookies"}
        account = None
        try:
            accounts = await api.pool.get_all()
            account = accounts[0] if accounts else None
        except Exception:
            pass
        if account is not None:
            said = explain(getattr(account, "error_msg", None), getattr(account, "active", None))
            if said:
                return {"state": said[0], "detail": _scrub(said[1], secrets)}
        from_logs = explain("\n".join(logs), None) if logs else None                   # (32), (326) or (88) in twscrape's own messages say what X thought
        if from_logs:
            return {"state": from_logs[0], "detail": _scrub(from_logs[1], secrets)}
        heard = _scrub(" | ".join(logs[-3:]), secrets, bound=300)
        base = _scrub(error or "X returned no profile (a bot-check, a block or a network problem), so nothing could be judged", secrets)
        return {"state": "unreachable", "detail": _scrub(base + (f"; twscrape logged: {heard}" if heard else ""), secrets, bound=600)}


def main(environ=None, out=sys.stdout, api_factory=None):
    environ = os.environ if environ is None else environ
    try:
        jar = json.loads(environ.get("X_SESSION_COOKIES", "") or "{}")
        if not isinstance(jar, dict):
            raise ValueError("not an object")
    except ValueError:
        out.write(json.dumps({"state": "unreachable", "detail": "the cookies were not handed over as an object of name and value"}) + "\n")
        return 0
    result = asyncio.run(check(jar, api_factory=api_factory, logs=[]))
    out.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
