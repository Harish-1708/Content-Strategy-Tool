"""Matching a host against the authorised-access list: pure standard-library code shared by the core gate and the isolated worker.

The worker runs in its own environment and is told which hosts are authorised, so it re-checks every request itself and cannot be steered
toward a host that was not listed, even by a damaged or tampered job file that bypasses the core's gate.
"""
import re

METHODS = ("stealth", "challenge", "fingerprint", "proxy", "logged_in_browser")


def host_only(value):
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", str(value or "").strip().lower())
    return text.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".").removeprefix("www.")


def entry_for(entries, host):
    """The listed entry for a host or one of its parent domains (never a look-alike such as shop.example.evil.test), else None."""
    by_host = {host_only(e["host"]): e for e in entries}
    host = host_only(host)
    while host:
        if host in by_host:
            return by_host[host]
        host = host.partition(".")[2] if "." in host else ""
    return None


def allows(entries, host, method):
    """(True, the recorded basis) or (False, why not)."""
    if method not in METHODS:
        return False, f"{method!r} is not a known advanced method"
    entry = entry_for(entries, host)
    if entry is None:
        return False, f"{host} is not listed under authorised_access, so {method} is not available for it"
    if method not in entry["methods"]:
        return False, f"{host} is authorised for {', '.join(entry['methods'])}, not for {method}"
    return True, f"{entry['basis']}: approved by {entry['approved_by']} on {entry['date']}"


def require(entries, host, *methods):
    """The basis text, or ValueError naming the first method that is not allowed."""
    basis = ""
    for method in methods:
        ok, why = allows(entries, host, method)
        if not ok:
            raise ValueError(why)
        basis = why
    return basis
