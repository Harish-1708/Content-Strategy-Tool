"""The declared registry of sources: what each one is, how it is reached and what it can and cannot give.

Loaded from sources.yaml. Nothing here proves a source works; that is the job of the acceptance run
(contentintel/acceptance.py) and the health ledger (contentintel/health.py).
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import contract

TIERS = ("official_api", "public_polite", "session_based", "third_party_service", "licensed_vendor", "human_assisted")
ACCESS_TYPES = ("site_data", "page_links", "page_text", "search_snippet", "report_text", "media_file", "model_output", "feed")
KINDS = ("collector", "search", "catalogue", "video", "ai")
STATUSES = ("established", "experimental", "planned")
DEFAULT_PATH = "sources.yaml"

_DEFAULTS = {
    "stale_after_days": 14, "acceptance_max_age_days": 14, "window_days": 7, "degrade_after_failures": 3,
    "recover_after_successes": 2, "history_cap": 200,
    "pause_seconds": {contract.RATE_LIMITED: 3600, contract.TIMEOUT: 60, contract.UPSTREAM_BROKEN: 120, contract.NEED_SETUP: 60,
                      contract.NEED_LOGIN: 43200, contract.PLATFORM_BLOCKED: 86400},
}


class SourcesError(Exception):
    pass


@dataclass(frozen=True)
class Route:
    name: str
    tier: str
    access_type: str
    login: str = "none"
    enabled: bool = True
    experimental: bool = False
    note: str = ""


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    label: str
    platform: str
    status: str
    capabilities: tuple
    limitations: tuple
    stale_after_days: int
    routes: dict = field(default_factory=dict)

    def enabled_routes(self):
        return [r for r in self.routes.values() if r.enabled]


@dataclass(frozen=True)
class Registry:
    sources: dict
    stale_after_days: int
    acceptance_max_age_days: int
    window_days: int
    degrade_after_failures: int
    recover_after_successes: int
    history_cap: int
    pause_seconds: dict

    def source(self, source_id):
        try:
            return self.sources[source_id]
        except KeyError:
            raise SourcesError(f"{source_id!r} is not declared in sources.yaml") from None

    def route(self, source_id, route_name):
        src = self.source(source_id)
        try:
            return src.routes[route_name]
        except KeyError:
            raise SourcesError(f"{source_id} has no route {route_name!r} in sources.yaml") from None

    def pause_for(self, code):
        return int(self.pause_seconds.get(code, 0))


def _only(data, allowed, where):
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise SourcesError(f"{where}: unknown setting(s) {', '.join(extra)} (allowed: {', '.join(allowed)})")


def _whole(value, where, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SourcesError(f"{where} must be a whole number of at least {minimum}")
    return value


def _text_list(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SourcesError(f"{where} must be a list of non-empty text lines")
    return tuple(v.strip() for v in value)


def _route(name, data, where):
    if not isinstance(data, dict):
        raise SourcesError(f"{where} must list tier and access_type")
    _only(data, ("tier", "access_type", "login", "enabled", "experimental", "note"), where)
    if data.get("tier") not in TIERS:
        raise SourcesError(f"{where}.tier must be one of {', '.join(TIERS)}")
    if data.get("access_type") not in ACCESS_TYPES:
        raise SourcesError(f"{where}.access_type must be one of {', '.join(ACCESS_TYPES)}")
    for flag in ("enabled", "experimental"):
        if flag in data and not isinstance(data[flag], bool):
            raise SourcesError(f"{where}.{flag} must be true or false")
    return Route(name=name, tier=data["tier"], access_type=data["access_type"], login=str(data.get("login", "none")),
                 enabled=data.get("enabled", True), experimental=data.get("experimental", False),
                 note=" ".join(str(data.get("note", "")).split()))


def load_sources(path=DEFAULT_PATH):
    p = Path(path)
    if not p.is_file():
        raise SourcesError(f"{path} is missing. It declares each source, how it is reached and what it can and cannot give.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SourcesError(f"{path} could not be read: {exc}") from None
    if not isinstance(raw, dict):
        raise SourcesError(f"{path} must be a list of settings")
    _only(raw, ("version", "defaults", "sources"), path)
    if raw.get("version") != 1:
        raise SourcesError(f"{path}: version must be 1")
    given = raw.get("defaults") or {}
    if not isinstance(given, dict):
        raise SourcesError(f"{path}: defaults must be a list of settings")
    _only(given, tuple(_DEFAULTS), f"{path} defaults")
    merged = {**_DEFAULTS, **{k: v for k, v in given.items() if k != "pause_seconds"}}
    for key in ("stale_after_days", "acceptance_max_age_days", "window_days", "degrade_after_failures",
                "recover_after_successes", "history_cap"):
        _whole(merged[key], f"defaults.{key}", 1)
    pauses = dict(_DEFAULTS["pause_seconds"])
    given_pause = given.get("pause_seconds") or {}
    if not isinstance(given_pause, dict):
        raise SourcesError(f"{path}: defaults.pause_seconds must be a list of code: seconds pairs")
    for code, seconds in given_pause.items():
        if code not in contract.CODES:
            raise SourcesError(f"defaults.pause_seconds: {code!r} is not an error code ({', '.join(contract.CODES)})")
        pauses[code] = _whole(seconds, f"defaults.pause_seconds.{code}", 0)
    declared = raw.get("sources")
    if not isinstance(declared, dict) or not declared:
        raise SourcesError(f"{path}: sources must name at least one source")
    out = {}
    for sid, data in declared.items():
        where = f"sources.{sid}"
        if not isinstance(data, dict):
            raise SourcesError(f"{where} must be a list of settings")
        _only(data, ("kind", "label", "platform", "status", "capabilities", "limitations", "stale_after_days", "routes"), where)
        if data.get("kind") not in KINDS:
            raise SourcesError(f"{where}.kind must be one of {', '.join(KINDS)}")
        if data.get("status") not in STATUSES:
            raise SourcesError(f"{where}.status must be one of {', '.join(STATUSES)}")
        routes_raw = data.get("routes")
        if not isinstance(routes_raw, dict) or not routes_raw:
            raise SourcesError(f"{where}.routes must name at least one route")
        routes = {name: _route(name, r, f"{where}.routes.{name}") for name, r in routes_raw.items()}
        out[sid] = Source(
            id=sid, kind=data["kind"], label=str(data.get("label") or sid), platform=str(data.get("platform") or ""),
            status=data["status"], capabilities=_text_list(data.get("capabilities", []), f"{where}.capabilities"),
            limitations=_text_list(data.get("limitations", []), f"{where}.limitations"),
            stale_after_days=_whole(data.get("stale_after_days", merged["stale_after_days"]), f"{where}.stale_after_days", 1),
            routes=routes)
    return Registry(sources=out, stale_after_days=merged["stale_after_days"], acceptance_max_age_days=merged["acceptance_max_age_days"],
                    window_days=merged["window_days"], degrade_after_failures=merged["degrade_after_failures"],
                    recover_after_successes=merged["recover_after_successes"], history_cap=merged["history_cap"], pause_seconds=pauses)


def route_hash(registry, source_id, route_name, extra=None):
    """A short fingerprint of how a route is declared (and of the settings it depends on, passed as extra).

    When a route's declaration or its settings change, the fingerprint changes, and the route has to pass
    acceptance again before it is trusted.
    """
    route = registry.route(source_id, route_name)
    payload = {"source": source_id, "route": route_name, "tier": route.tier, "access_type": route.access_type,
               "login": route.login, "enabled": route.enabled, "experimental": route.experimental, "extra": extra}
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]
