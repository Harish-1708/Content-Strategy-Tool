"""Standard-library helpers shared by the core and the isolated worker: host names, and which request options are the ones that change how a request presents itself to a site's bot
detection (TLS impersonation, canvas and WebRTC changes, cursor simulation, challenge solving).

There is no list of hosts and no approval step: every one of these is the owner's setting, on any competitor. The owner's switch `access_controls.evasion` turns them off for everyone, and the
worker honours it for every request. What no setting can do is retry past a refusal: a challenge, 401/403 or 429 rests the host (worker and pause ledger), whatever options are set.
"""
import re

FLAGS = ("impersonate", "fingerprint", "solve_cloudflare", "humanize")      # the worker's names for these options


def host_only(value):
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", str(value or "").strip().lower())
    return text.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".").removeprefix("www.")


def flags_requested(adv):
    """The bot-detection options a request asks for, by name (empty for identity, headers, proxy, session, cookies, browser, pace)."""
    return [f for f in FLAGS if adv.get(f)]
