"""The fallback chain for a channel that needs a login: primary route, then the stored session, then the search layer's discovery, then an honest skip.

One attempt per step, in order, and no retry loop anywhere: a step that raises or returns nothing is written down and the next one is tried. The run is never turned red for a missing or expired
session; the result says which route answered and, when none did, the true reason for each. A route that answered with no results is a real, empty answer (not a failure) when nothing before it
failed in a way that could have hidden results.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import contract as K


@dataclass
class Step:
    name: str                       # "primary", "session", "discovery" (any short word)
    run: object                     # a function with no arguments returning a list of rows; raises K.Failure for the true reason
    enabled: bool = True            # the owner's switch for this route
    off_reason: str = ""            # why it is off (shown, never hidden)


@dataclass
class ChainResult:
    channel: str
    route: str = ""                 # the route that returned rows ("" when none did)
    rows: list = field(default_factory=list)
    trail: list = field(default_factory=list)       # [(route, outcome, detail)] outcome: ok, empty, off, or a K code
    skipped: bool = False           # no route could run: the channel is skipped, with the reasons below
    reason: str = ""

    def summary(self):
        """One line for the log: which route ran, and what each earlier one said."""
        head = f"{self.channel}: " + (f"route used: {self.route} ({len(self.rows)} item(s))" if self.route else ("skipped" if self.skipped else "answered with no results"))
        steps = "; ".join(f"{name}: {outcome}" + (f" ({detail})" if detail else "") for name, outcome, detail in self.trail)
        return head + (f" | {steps}" if steps else "")


def run_chain(channel, steps, log=None):
    """Try each step once, in order. Returns a ChainResult; never raises for a step's failure."""
    result = ChainResult(channel)
    empties, failures = 0, 0
    for step in steps:
        if not step.enabled:
            result.trail.append((step.name, "off", step.off_reason or "switched off by the owner"))
            continue
        try:
            rows = list(step.run() or [])
        except K.Failure as exc:
            failures += 1
            result.trail.append((step.name, exc.code, str(exc)))
            continue
        except Exception as exc:                                    # noqa: BLE001 - only the kind of error is kept: an unexpected message could hold a secret
            failures += 1
            result.trail.append((step.name, K.UPSTREAM_BROKEN, f"unexpected {type(exc).__name__}"))
            continue
        if rows:
            result.route, result.rows = step.name, rows
            result.trail.append((step.name, "ok", f"{len(rows)} item(s)"))
            break
        empties += 1
        result.trail.append((step.name, "empty", "answered with no results"))
    else:
        if empties:
            result.reason = "a route answered normally and found nothing that matches" + (f" (earlier routes could not run: {failures})" if failures else "")
        else:
            result.skipped = True
            result.reason = "; ".join(f"{name}: {outcome} {detail}".strip() for name, outcome, detail in result.trail) or "no route is configured"
    if log:
        log(result.summary())
    return result
