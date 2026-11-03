"""The collector contract: one result shape and one set of error codes for every source and route.

Every action on every source (an ad library, a social site, a search service, a catalogue feed, a video download,
a model) reports in the same envelope:

    {schema_version, ok, source, action, route, items, meta: {elapsed_ms, attempts}, error}

and every failure carries one of eight codes. Five of them are *final*: another route cannot fix them, so the
source stops for this run instead of trying the next route.

The envelope, the codes and the final-error rule are modelled on Agent-Reach
(https://github.com/Panniantong/Agent-Reach, MIT licence, copyright 2025 Agent Eyes); see THIRD_PARTY_NOTICES.md.
"""
import json
import re
import socket
import subprocess
import time

from .safety import scrub

SCHEMA_VERSION = 1

NEED_SETUP = "NEED_SETUP"              # a key, tool or setting is missing or wrong
NEED_LOGIN = "NEED_LOGIN"              # the site wants a login the saved session does not satisfy
UPSTREAM_BROKEN = "UPSTREAM_BROKEN"    # the source answered, but not with something usable
TIMEOUT = "TIMEOUT"
RATE_LIMITED = "RATE_LIMITED"          # a limit, or out of credits
PLATFORM_BLOCKED = "PLATFORM_BLOCKED"  # a challenge, a warning or a refusal: stop, never work around it
NOT_FOUND = "NOT_FOUND"
BAD_INPUT = "BAD_INPUT"

CODES = (NEED_SETUP, NEED_LOGIN, UPSTREAM_BROKEN, TIMEOUT, RATE_LIMITED, PLATFORM_BLOCKED, NOT_FOUND, BAD_INPUT)
FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN})

HINTS = {
    NEED_SETUP: "A key, tool or setting is missing or wrong. Check the repository secret or the setting named in the message.",
    NEED_LOGIN: "The saved login was not accepted. Export fresh cookies for the dummy account; never type a password into the tool.",
    UPSTREAM_BROKEN: "The source answered with something unusable. Try again later; if it keeps happening the page or feed changed.",
    TIMEOUT: "The source did not answer in time. It will be tried again on a later run.",
    RATE_LIMITED: "A limit was reached. The source rests before it is used again; nothing is retried in a loop.",
    PLATFORM_BLOCKED: "The platform refused or challenged this server. Automation on this source stops; a person has to look at the account.",
    NOT_FOUND: "What was asked for does not exist (any more). Check the address.",
    BAD_INPUT: "The request itself was wrong. Check the arguments or the settings.",
}

MAX_MESSAGE = 500
_ATTEMPT_KEYS = ("route", "result")


class Failure(Exception):
    """Raised by an adapter that already knows which code a failure deserves."""

    def __init__(self, code, message="", hint="", retry_after=None):
        super().__init__(message)
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code, self.message, self.hint, self.retry_after = code, message, hint, retry_after


def make_error(code, message="", hint="", retry_after=None):
    if code not in CODES:
        raise ValueError(f"unknown error code {code!r}")
    return {"code": code, "message": " ".join(scrub(message, MAX_MESSAGE).split()), "hint": hint or HINTS[code],
            "retry_after_seconds": retry_after}


# ---- classification --------------------------------------------------------------------------------------------------------------------------

_WORDS = (
    (RATE_LIMITED, ("rate limit", "rate-limit", "ratelimit", "too many requests", "http error 429", "429 client error", "out of credit", "not enough credits",
                    "quota", "usage limit", "out of searches", "run out of")),
    (PLATFORM_BLOCKED, ("challenge", "captcha", "verification", "account warning", "suspicious", "unusual activity", "temporarily blocked",
                        "refused the download", "http error 403", "403 client error", "forbidden", "not a bot", "action blocked", "restricted your")),
    (NEED_LOGIN, ("not logged in", "login wall", "log in", "login required", "sign in", "cookies were not accepted", "http error 401")),
    (NOT_FOUND, ("not found", "http error 404", "404 client error", "no longer available", "has been removed", "was deleted", "does not exist", "is private",
                 "video unavailable", "video not available", "this video is unavailable")),
    (TIMEOUT, ("timed out", "timeout", "did not answer in time")),
    (NEED_SETUP, ("no key", "not installed", "set anthropic_api_key", "is not set", "missing key", "no login or key", "not configured", "api key was rejected",
                  "key was rejected", "invalid api key", "unauthorized")),
    (BAD_INPUT, ("bad input", "invalid argument", "must be a", "unknown source", "unknown provider")),
)


_HTTP_STATUS = re.compile(r"\bhttp(?: error)?[ :/]*(?:1\.[01] )?(\d{3})\b")


def classify_message(text):
    """The error code a plain-words failure message points to, or '' when it does not say."""
    low = str(text or "").lower()
    status = _HTTP_STATUS.search(low)
    if status:
        number = int(status.group(1))
        if number in (401, 403, 404, 429):
            return _http_code(number)
        if 500 <= number <= 599:
            return UPSTREAM_BROKEN
    for code, words in _WORDS:
        if any(w in low for w in words):
            return code
    return ""


def _http_code(status):
    return {401: NEED_LOGIN, 403: PLATFORM_BLOCKED, 404: NOT_FOUND, 429: RATE_LIMITED}.get(status, UPSTREAM_BROKEN)


def classify_exception(exc):
    """An error dict for any exception, including this tool's own LoginWall, Challenge, AccountWarning, AuthError and QuotaError."""
    if isinstance(exc, Failure):
        return make_error(exc.code, exc.message, exc.hint, exc.retry_after)
    names = [c.__name__ for c in type(exc).__mro__]
    message = str(exc)
    if "AccountWarning" in names or "Challenge" in names:
        return make_error(PLATFORM_BLOCKED, message)
    if "LoginWall" in names:
        return make_error(NEED_LOGIN, message)
    if "AuthError" in names:
        return make_error(NEED_SETUP, message or "the key was rejected")
    if "QuotaError" in names:
        return make_error(RATE_LIMITED, message or "out of credits")
    if isinstance(exc, (subprocess.TimeoutExpired, socket.timeout, TimeoutError)) or "Timeout" in names or "ReadTimeout" in names:
        return make_error(TIMEOUT, message or "no answer in time")
    if "HTTPError" in names:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return make_error(_http_code(status), message)
    if "ConnectionError" in names or isinstance(exc, ConnectionError):
        return make_error(UPSTREAM_BROKEN, "network error: " + message)
    if isinstance(exc, FileNotFoundError):
        return make_error(NEED_SETUP, f"missing tool or file: {getattr(exc, 'filename', None) or message}")
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return make_error(UPSTREAM_BROKEN, "the answer was not readable (malformed data)")
    if "CollectError" in names or "ResearchError" in names:
        return make_error(classify_message(message) or BAD_INPUT, message)
    guessed = classify_message(message)
    if "BrowserError" in names:
        return make_error(guessed if guessed in (TIMEOUT, NOT_FOUND) else UPSTREAM_BROKEN, message)
    return make_error(guessed or UPSTREAM_BROKEN, message or type(exc).__name__)


def code_for(exc):
    return classify_exception(exc)["code"]


# ---- the envelope ----------------------------------------------------------------------------------------------------------------------------

def envelope(source, action, route=None, *, items=None, error=None, elapsed_ms=0, attempts=None):
    """The one result shape. A success has a route and items and no error; a failure has an error, no items and no route."""
    if error is not None and error.get("code") not in CODES:
        raise ValueError("error must carry one of the contract codes")
    ok = error is None
    return {
        "schema_version": SCHEMA_VERSION, "ok": ok, "source": source, "action": action,
        "route": route if ok else None,
        "items": list(items or []) if ok else [],
        "meta": {"elapsed_ms": int(elapsed_ms), "attempts": list(attempts or [])},
        "error": error,
    }


def validate_envelope(env):
    """Problems with an envelope; an empty list means it keeps the contract."""
    problems = []
    if not isinstance(env, dict):
        return ["the envelope is not an object"]
    for key in ("schema_version", "ok", "source", "action", "route", "items", "meta", "error"):
        if key not in env:
            problems.append(f"missing '{key}'")
    if problems:
        return problems
    if env["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version {env['schema_version']} is not {SCHEMA_VERSION}")
    if not isinstance(env["ok"], bool):
        problems.append("ok must be true or false")
    if not env["source"] or not env["action"]:
        problems.append("source and action must be named")
    if not isinstance(env["items"], list):
        problems.append("items must be a list")
    meta = env["meta"]
    if not isinstance(meta, dict) or "elapsed_ms" not in meta or not isinstance(meta.get("attempts"), list):
        problems.append("meta needs elapsed_ms and a list of attempts")
    else:
        for att in meta["attempts"]:
            if not isinstance(att, dict) or any(k not in att for k in _ATTEMPT_KEYS):
                problems.append("every attempt needs a route and a result")
                break
    if env["ok"]:
        if env["error"] is not None:
            problems.append("a successful result must not carry an error")
        if not env["route"]:
            problems.append("a successful result must name its route")
    else:
        err = env["error"]
        if not isinstance(err, dict) or err.get("code") not in CODES:
            problems.append("a failed result needs an error with one of the contract codes")
        if env["items"]:
            problems.append("a failed result must not carry items")
    return problems


# ---- trying routes in order ------------------------------------------------------------------------------------------------------------------

def _pick_error(errors):
    for err in errors:
        if err["code"] in FINAL_CODES:
            return err
    for err in errors:
        if err["code"] != NEED_SETUP:
            return err
    return errors[0]


def run_action(source, action, routes, *, health=None, now="", clock=time.monotonic, empty_ok=False):
    """Try each (route name, function) in order and return one envelope.

    A route the health ledger says is resting is skipped and recorded as such. A final error (login wall, block, rate
    limit, bad input, not found) stops the run: no other route is tried. Anything else moves to the next route. A route that
    returns nothing counts as broken unless empty_ok, so an empty answer is never passed off as a successful one.
    """
    started = clock()
    attempts, errors = [], []

    def elapsed():
        return int((clock() - started) * 1000)

    for route, fn in routes:
        pause = health.is_paused(source, route, now) if health is not None else None
        if pause:
            attempts.append({"route": route, "result": pause["code"], "skipped": True, "message": pause["message"],
                             "retry_after_seconds": pause.get("retry_after_seconds")})
            errors.append(make_error(pause["code"], pause["message"], retry_after=pause.get("retry_after_seconds")))
            continue
        t0 = clock()
        try:
            items = list(fn() or [])
            if not items and not empty_ok:
                raise Failure(UPSTREAM_BROKEN, "the route answered but returned nothing usable")
        except Exception as exc:        # any failure of any adapter is classified, never allowed to end the run
            err = classify_exception(exc)
            attempts.append({"route": route, "result": err["code"], "message": err["message"]})
            errors.append(err)
            if health is not None:
                health.observe(source, route, ok=False, code=err["code"], message=err["message"], now=now,
                               elapsed_ms=int((clock() - t0) * 1000))
            if err["code"] in FINAL_CODES:
                break
            continue
        attempts.append({"route": route, "result": "ok", "items": len(items)})
        if health is not None:
            health.observe(source, route, ok=True, items=len(items), now=now, elapsed_ms=int((clock() - t0) * 1000))
        return envelope(source, action, route, items=items, elapsed_ms=elapsed(), attempts=attempts)
    if not errors:
        errors = [make_error(BAD_INPUT, "no route was given")]
    return envelope(source, action, error=_pick_error(errors), elapsed_ms=elapsed(), attempts=attempts)


ENVELOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "envelope.v1.schema.json",
    "title": "Collector result envelope, version 1",
    "type": "object",
    "required": ["schema_version", "ok", "source", "action", "route", "items", "meta", "error"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "ok": {"type": "boolean"},
        "source": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "route": {"type": ["string", "null"]},
        "items": {"type": "array"},
        "meta": {"type": "object", "required": ["elapsed_ms", "attempts"],
                 "properties": {"elapsed_ms": {"type": "integer", "minimum": 0},
                                "attempts": {"type": "array", "items": {"type": "object", "required": list(_ATTEMPT_KEYS)}}}},
        "error": {"oneOf": [{"type": "null"},
                            {"type": "object", "required": ["code", "message", "hint"],
                             "properties": {"code": {"type": "string", "enum": list(CODES)}, "message": {"type": "string", "maxLength": MAX_MESSAGE},
                                            "hint": {"type": "string"}, "retry_after_seconds": {"type": ["integer", "null"]}}}]},
    },
    "allOf": [
        {"if": {"properties": {"ok": {"const": True}}}, "then": {"properties": {"error": {"type": "null"}, "route": {"type": "string", "minLength": 1}}}},
        {"if": {"properties": {"ok": {"const": False}}}, "then": {"properties": {"error": {"type": "object"}, "items": {"maxItems": 0}, "route": {"type": "null"}}}},
    ],
}
