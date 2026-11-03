"""Collector health: what each route of each source has done lately, and whether it has earned trust.

One ledger entry per source and route, kept between runs in data/_shared/collector_health.json:

  * every real attempt (success, failure and its code, how many items, how long), newest last, capped;
  * a pause after a failure that deserves a rest (a rate limit rests an hour, a block a day), which survives
    until the next run so a refused platform is not asked again at once;
  * a state that only turns "degraded" after several failures in a row and only returns to "healthy" after
    several successes in a row, so one flicker never changes it;
  * the latest live acceptance result.

A route is "healthy" only when a live acceptance run has passed recently for the route as it is declared now.
Without that it is "unverified", however many ordinary runs have worked, and nothing that scores or ranks may
depend on it. Pausing and the final-error rule follow Agent-Reach's channel health (MIT; see THIRD_PARTY_NOTICES.md).
"""
from datetime import datetime, timedelta, timezone

from . import contract
from .safety import scrub
from .sources import route_hash as _route_hash

_TS = "%Y-%m-%dT%H:%M:%SZ"
NEUTRAL = frozenset({contract.NOT_FOUND, contract.BAD_INPUT})     # about the request, not about the source: never a health event
STATE_VERSION = 1

SHEET_COLUMNS = ["route_key", "source", "route", "status", "verified", "tier", "access_type", "last_success", "last_error",
                 "error_code", "pause_until", "success_rate_7d", "attempts_7d", "avg_items", "latest_acceptance",
                 "acceptance_result", "acceptance_items", "version", "route_hash", "limits"]


def _parse(ts):
    try:
        return datetime.strptime(str(ts), _TS).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def add_seconds(now, seconds):
    return (_parse(now) + timedelta(seconds=seconds)).strftime(_TS)


def age_days(ts, now):
    a, b = _parse(ts), _parse(now)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 86400


def _blank(source, route):
    return {"source": source, "route": route, "history": [], "consecutive_failures": 0, "consecutive_successes": 0,
            "state": "healthy", "last_success": "", "last_error": None, "pause": None, "acceptance": None}


def _clean(data):
    """The saved state as a safe dict: a damaged or foreign file means an empty ledger, never a crash."""
    if not isinstance(data, dict) or not isinstance(data.get("routes"), dict):
        return {"version": STATE_VERSION, "routes": {}}
    routes = {}
    for key, entry in data["routes"].items():
        if not isinstance(entry, dict) or not entry.get("source") or not entry.get("route"):
            continue
        fresh = _blank(entry["source"], entry["route"])
        for k in fresh:
            if k in entry:
                fresh[k] = entry[k]
        if not isinstance(fresh["history"], list):
            fresh["history"] = []
        fresh["history"] = [h for h in fresh["history"] if isinstance(h, dict) and h.get("at")]
        routes[str(key)] = fresh
    return {"version": STATE_VERSION, "routes": routes}


class HealthLedger:
    def __init__(self, data, registry, hash_for=None):
        self.state = _clean(data)
        self.registry = registry
        self._hash_for = hash_for or (lambda source, route: _route_hash(registry, source, route))

    @classmethod
    def load(cls, store, registry, hash_for=None):
        return cls(store.read_health(), registry, hash_for)

    def save(self, store):
        return store.write_health(self.state)

    def route_hash(self, source, route):
        return self._hash_for(source, route)

    # ---- recording ----------------------------------------------------------------------------------------------------------------------

    def _entry(self, source, route):
        key = f"{source}|{route}"
        if key not in self.state["routes"]:
            self.state["routes"][key] = _blank(source, route)
        return self.state["routes"][key]

    def observe(self, source, route, *, ok, code="", items=0, message="", now, elapsed_ms=0):
        """Record one real attempt. A skipped route (resting) is not an attempt and is never recorded here."""
        entry = self._entry(source, route)
        neutral = (not ok) and code in NEUTRAL
        entry["history"].append({"at": now, "ok": None if neutral else bool(ok), "code": "" if ok else code,
                                 "items": int(items or 0), "ms": int(elapsed_ms or 0)})
        del entry["history"][:-self.registry.history_cap]
        if neutral:
            return entry
        if ok:
            entry["consecutive_successes"] += 1
            entry["consecutive_failures"] = 0
            entry["last_success"] = now
            entry["pause"] = None
            if entry["state"] == "degraded" and entry["consecutive_successes"] >= self.registry.recover_after_successes:
                entry["state"] = "healthy"
            return entry
        entry["consecutive_failures"] += 1
        entry["consecutive_successes"] = 0
        entry["last_error"] = {"at": now, "code": code, "message": scrub(message, 300)}
        if entry["state"] == "healthy" and entry["consecutive_failures"] >= self.registry.degrade_after_failures:
            entry["state"] = "degraded"
        seconds = self.registry.pause_for(code)
        if seconds > 0:
            until = add_seconds(now, seconds)
            current = (entry["pause"] or {}).get("until", "")
            if until > current:                       # a longer rest is never shortened by a later, milder failure
                entry["pause"] = {"until": until, "code": code, "message": scrub(message, 300)}
        return entry

    def record_acceptance(self, record):
        """Keep the latest live acceptance result for a route and count it as a real attempt."""
        entry = self._entry(record["source"], record["route"])
        entry["acceptance"] = {k: record.get(k, "") for k in ("at", "passed", "code", "items", "version", "route_hash", "run")}
        self.observe(record["source"], record["route"], ok=bool(record["passed"]), code=record.get("code", ""),
                     items=record.get("items", 0), message=record.get("message", ""), now=record["at"],
                     elapsed_ms=record.get("elapsed_ms", 0))
        return entry

    def clear(self, source, route=None):
        """A person says the problem is dealt with: remove the rest period (and the failure run) for a source or one route."""
        cleared = 0
        for entry in self.state["routes"].values():
            if entry["source"] == source and (route is None or entry["route"] == route):
                if entry["pause"] or entry["consecutive_failures"]:
                    cleared += 1
                entry["pause"] = None
                entry["consecutive_failures"] = 0
        return cleared

    # ---- asking -------------------------------------------------------------------------------------------------------------------------

    def is_paused(self, source, route, now):
        entry = self.state["routes"].get(f"{source}|{route}")
        pause = (entry or {}).get("pause")
        if not pause or not pause.get("until") or pause["until"] <= now:
            return None
        left = (_parse(pause["until"]) - _parse(now)).total_seconds()
        return {"code": pause["code"], "until": pause["until"], "retry_after_seconds": max(1, int(left)),
                "message": f"{source} ({route}) is resting until {pause['until'][:16].replace('T', ' ')} UTC after {pause['code']}: "
                           f"{pause.get('message', '')}".strip().rstrip(":")}

    def window(self, entry, now):
        cutoff = self.registry.window_days
        return [h for h in entry["history"] if (age_days(h["at"], now) or 0) <= cutoff]

    def success_rate(self, source, route, now):
        entry = self.state["routes"].get(f"{source}|{route}")
        if not entry:
            return None
        counted = [h for h in self.window(entry, now) if h["ok"] is not None]
        return None if not counted else sum(1 for h in counted if h["ok"]) / len(counted)

    def verified(self, source, route, now):
        """True when a live acceptance run passed recently, for the route as it is declared now."""
        entry = self.state["routes"].get(f"{source}|{route}")
        acc = (entry or {}).get("acceptance")
        if not acc or not acc.get("passed"):
            return False
        age = age_days(acc.get("at"), now)
        return (age is not None and age <= self.registry.acceptance_max_age_days
                and acc.get("route_hash") == self._hash_for(source, route))

    def status(self, source, route, now):
        """planned, disabled, paused, unverified, degraded or healthy."""
        src = self.registry.source(source)
        rt = self.registry.route(source, route)
        if src.status == "planned":
            return "planned"
        if not rt.enabled:
            return "disabled"
        if self.is_paused(source, route, now):
            return "paused"
        entry = self.state["routes"].get(f"{source}|{route}") or _blank(source, route)
        acc = entry["acceptance"]
        age = age_days(acc.get("at"), now) if acc else None
        fresh = (age is not None and age <= self.registry.acceptance_max_age_days
                 and acc.get("route_hash") == self._hash_for(source, route))
        if not fresh:
            return "unverified"
        if not acc.get("passed") or entry["state"] == "degraded":
            return "degraded"
        return "healthy"

    def scoring_ready(self, source, now):
        """True when at least one enabled route of the source is healthy. Scoring must not require a source that is not."""
        src = self.registry.source(source)
        return any(self.status(source, r.name, now) == "healthy" for r in src.enabled_routes())

    # ---- showing ------------------------------------------------------------------------------------------------------------------------

    def rows(self, now):
        """One row per declared route, for the Collector Health tab and the printed report."""
        out = []
        for src in self.registry.sources.values():
            for rt in src.routes.values():
                entry = self.state["routes"].get(f"{src.id}|{rt.name}") or _blank(src.id, rt.name)
                counted = [h for h in self.window(entry, now) if h["ok"] is not None]
                good = [h for h in counted if h["ok"]]
                rate = self.success_rate(src.id, rt.name, now)
                acc = entry["acceptance"] or {}
                pause = self.is_paused(src.id, rt.name, now)
                out.append({
                    "route_key": f"{src.id}|{rt.name}", "source": src.id, "route": rt.name,
                    "status": self.status(src.id, rt.name, now), "verified": "yes" if self.verified(src.id, rt.name, now) else "no",
                    "tier": rt.tier, "access_type": rt.access_type, "last_success": entry["last_success"],
                    "last_error": (entry["last_error"] or {}).get("message", ""), "error_code": (entry["last_error"] or {}).get("code", ""),
                    "pause_until": pause["until"] if pause else "",
                    "success_rate_7d": "" if rate is None else round(rate * 100),
                    "attempts_7d": len(counted),
                    "avg_items": round(sum(h["items"] for h in good) / len(good), 1) if good else "",
                    "latest_acceptance": acc.get("at", ""),
                    "acceptance_result": "" if not acc else ("pass" if acc.get("passed") else "fail"),
                    "acceptance_items": acc.get("items", "") if acc else "",
                    "version": acc.get("version", "") if acc else "",
                    "route_hash": self._hash_for(src.id, rt.name),
                    "limits": "; ".join(src.limitations[:2]) + (f" | {rt.note}" if rt.note else ""),
                })
        return out


def format_report(rows):
    """Plain words for the log: one line per route."""
    lines = ["Collector health (a route is healthy only after a live acceptance run has passed):"]
    for r in rows:
        extra = []
        if r["pause_until"]:
            extra.append(f"resting until {r['pause_until'][:16].replace('T', ' ')} UTC")
        if r["success_rate_7d"] != "":
            extra.append(f"{r['success_rate_7d']}% of {r['attempts_7d']} attempts in 7 days")
        if r["error_code"]:
            extra.append(f"last error {r['error_code']}")
        if r["acceptance_result"]:
            extra.append(f"acceptance {r['acceptance_result']} {r['latest_acceptance'][:10]}")
        lines.append(f"  {r['source'] + ' / ' + r['route']:<34} {r['status']:<11}" + ("; " + "; ".join(extra) if extra else ""))
    return "\n".join(lines)


def format_matrix(registry, rows):
    """What each source is, how each route is reached, and what live runs have shown: the comparison of routes in one place."""
    by_key = {r["route_key"]: r for r in rows}
    lines = ["Source capability matrix (declared in sources.yaml; 'status' is what live runs have actually shown):"]
    for src in registry.sources.values():
        lines.append(f"{src.id}: {src.label} [{src.status}]")
        lines.append("  gives:  " + ("; ".join(src.capabilities) or "nothing declared"))
        lines.append("  cannot: " + ("; ".join(src.limitations) or "nothing declared"))
        for rt in src.routes.values():
            r = by_key[f"{src.id}|{rt.name}"]
            flags = (" OFF" if not rt.enabled else "") + (" EXPERIMENTAL" if rt.experimental else "")
            seen = f", {r['success_rate_7d']}% of {r['attempts_7d']} attempts in 7 days" if r["success_rate_7d"] != "" else ""
            err = f", last error {r['error_code']}" if r["error_code"] else ""
            lines.append(f"    route {rt.name}: {rt.tier}, {rt.access_type}, login {rt.login}{flags} -> {r['status']}{seen}{err}")
            if rt.note:
                lines.append(f"      note: {rt.note}")
    return "\n".join(lines)
