"""Guards: every protection that could stop or slow a request is an owner switch, and none of them acts unless the owner turns it ON.

Nothing here decides anything for the owner. A guard is OFF by default (it never blocks), the owner turns it ON or OFF in the Controls tab, with `observatory controls`, or in observatory.yaml
(services.guards.channels.<guard>), and code asks `guards.on("name")` at the moment it would have blocked. Setting CONTENTINTEL_LEGACY_GUARDS=1 starts with every guard ON, as Updates 1 to 40 behaved.

With the rest guard OFF a site that refuses is tried again a few times (the `retry` settings), and if it still does not work the result is marked failed and the run moves on to the next site.
"""
from __future__ import annotations

import os

GUARDS = {
    "robots_txt": "obey each site's robots.txt (OFF: robots.txt is not consulted before a request)",
    "private_address_block": "refuse addresses that point inside a private network, and local or internal host names (OFF: any address is fetched)",
    "rest_after_refusal": "rest a site or channel for hours or days after a refusal, rate limit or account warning (OFF: try again a few times, mark it failed, and move on)",
    "hosted_runner_block": "refuse to run browser-login channels (Instagram, Reddit, X and similar through OpenCLI) on a GitHub-hosted runner (OFF: try anyway)",
    "reddit_approval_required": "require a recorded Reddit approval before the official Reddit API route runs (OFF: it runs whenever the credentials exist)",
}


def legacy():
    """True when the environment asks for every guard to start ON."""
    return os.environ.get("CONTENTINTEL_LEGACY_GUARDS", "") == "1"


_state = None                                   # {guard: bool} once configured


def configure(source):
    """Set the guards from settings (a settings dict with services.guards), from a plain {guard: bool} dict (the worker process gets one in its job), or None to forget and reload lazily."""
    global _state
    if source is None:
        _state = None
        return
    if "services" in source:
        sec = source["services"].get("guards") or {}
        master = sec.get("enabled", True)
        channels = sec.get("channels") or {}
        _state = {g: bool(master and channels.get(g, legacy())) for g in GUARDS}
    else:
        _state = {g: bool(source.get(g, legacy())) for g in GUARDS}


def snapshot():
    """The current state of every guard, for a job file sent to another process."""
    return {g: on(g) for g in GUARDS}


def on(name):
    """True when the owner has this guard ON (it may act). Unknown names are never ON."""
    global _state
    if name not in GUARDS:
        return False
    if _state is None:
        try:                                    # first use in this process: read the owner's settings once
            from . import services
            configure(services.load_settings(None, os.environ.get("CONTENTINTEL_DATA_DIR", "data")))
        except Exception:                       # noqa: BLE001 - a broken settings file must never stop a run: fall back to the defaults
            _state = {g: legacy() for g in GUARDS}
    return _state[name]
