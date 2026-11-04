"""Recognising what a web answer is saying, in plain standard-library Python so the isolated worker can use it too.

A challenge page, a login wall and a Retry-After are *signals*: the tool reads them to stop, slow down or report. It never answers them by
disguising itself on a site it has no authorisation for.
"""
import re
from datetime import datetime, timezone

_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare", "access denied", "security check", "verify you are human",
                     "are you a robot", "pardon our interruption", "robot or human", "one more step", "checking your browser")
_CHALLENGE_MARKERS = ("/cdn-cgi/challenge-platform/", "cf-chl-", "enable javascript and cookies to continue", "captcha-delivery", "px-captcha",
                      "ddos protection by", "performing security verification", "requiring captcha")
_LOGIN_TITLE = re.compile(r"^\s*(log\s*in|sign\s*in|login|sign up|create an account)\b", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_BLOCK_STATUSES = (403, 429, 503)


def parse_retry_after(value, now_ts=None):
    """Seconds a Retry-After header asks for (a number or an HTTP date), or None when absent or unreadable."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime
    try:
        when = parsedate_to_datetime(value)
        base = datetime.fromtimestamp(now_ts, timezone.utc) if now_ts is not None else datetime.now(timezone.utc)
        return max((when - base).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def looks_like_challenge(status, headers, body):
    """True for a challenge or bot-check page. Recognised so the run can stop politely; never solved."""
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    sample = body[:60_000].decode("utf-8", errors="ignore").lower()
    title = _TITLE.search(sample)
    title = " ".join(title.group(1).split()) if title else ""
    if any(title.startswith(t) for t in _CHALLENGE_TITLES):
        return True
    if status in _BLOCK_STATUSES and any(marker in sample for marker in _CHALLENGE_MARKERS):
        return True
    return status == 200 and len(body) < 20_000 and any(marker in sample for marker in ("/cdn-cgi/challenge-platform/", "performing security verification"))


def looks_like_login_wall(body, title=""):
    """True when a page is only a login form (so it says nothing about the thing that was asked for)."""
    text = " ".join(re.sub(r"<[^>]+>", " ", body[:30_000].decode("utf-8", errors="ignore")).split())
    return bool(_LOGIN_TITLE.match(title or "")) and len(text) < 600


