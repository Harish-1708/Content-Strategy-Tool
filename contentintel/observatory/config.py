"""Settings for the observatory: observatory.yaml (whole tool) and brands/<brand>/observatory.yaml (one brand).

Both files are optional; every value has a safe default, and an unknown key is an error rather than being ignored,
so a typo cannot silently switch a safety setting off.
"""
import copy
import re
from pathlib import Path

import yaml

DEFAULT_PATH = "observatory.yaml"
USER_AGENT_TOKEN = "KelsonResearchBot"

# What the platform's shared services are used for. A module asks for a service with one of these purposes, and the owner can switch each service on or off per purpose.
PURPOSES = ("competitors", "discovery", "social_discovery", "content_research", "youtube_research", "general_research", "idea_bank", "planning", "brief", "deep_check", "autoseeds", "collectors")
from .. import agent_reach_catalog as _AR_CATALOG
AR_CHANNELS = _AR_CATALOG.integrated()          # every Agent-Reach channel the platform runs (the catalogue says how, and why any other is not)
SEARCH_PROVIDERS = ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp", "agent_reach_reddit", "agent_reach_youtube")
AI_PURPOSES = ("general_research", "idea_bank", "planning", "brief", "deep_check", "autoseeds", "collectors")        # the purposes whose language-model calls the AI Gateway can take
PAGE_ENGINES = ("jina", "core", "scrapling", "auto")
PAGE_ENGINE_DEFAULTS = {}                       # every purpose defaults to auto: Scrapling first when its worker is installed, then Jina Reader, then the built-in reader

# The service registry. A service declares its switches once and everything else follows from it: validation, defaults, the Controls tab, the `controls` command. A future repository adds its service
# with register_service() and gets all of that without new plumbing. channels: named switches (providers, sites). routes: a choice per purpose (the engine that reads pages, or whether the AI Gateway is
# tried first). options: central request options and target overrides (Scrapling).
SERVICE_REGISTRY = {}


def register_purpose(name, *, ai_route=False):
    """Add a kind of work (a purpose) to the platform, so every shared service gets a switch for it. A repository registers its own (for example 'outreach'); ai_route=True also lets the AI Gateway take its
    language-model calls. Registering one that exists changes nothing."""
    global PURPOSES, AI_PURPOSES
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        raise ValueError(f"a purpose name is lowercase letters, digits and underscores: {name!r}")
    if name not in PURPOSES:
        PURPOSES = PURPOSES + (name,)
    if ai_route and name not in AI_PURPOSES:
        AI_PURPOSES = AI_PURPOSES + (name,)


BUILT_IN_PURPOSES = PURPOSES


def unregister_purpose(name):
    """Remove a purpose added with register_purpose (for tests, and for a repository that is taken out). The built-in purposes cannot be removed."""
    global PURPOSES, AI_PURPOSES
    if name in BUILT_IN_PURPOSES:
        raise ValueError(f"{name} is a built-in purpose")
    PURPOSES = tuple(p for p in PURPOSES if p != name)
    AI_PURPOSES = tuple(p for p in AI_PURPOSES if p != name)


def spec_purposes(spec):
    """The purposes a service has switches for: every purpose there is (including ones registered later) unless the service named its own."""
    return tuple(PURPOSES) if spec.get("all_purposes") else spec["purposes"]


def route_purposes(spec):
    p = spec["routes"]["purposes"]
    return tuple(PURPOSES) if p == "all" else tuple(AI_PURPOSES) if p == "ai" else tuple(p)


def route_default_for(spec, purpose):
    default = spec["routes"]["default"]
    return default.get(purpose, default.get("*")) if isinstance(default, dict) else default


def register_service(name, *, label="", channels=(), purposes=None, routes=None, options=False, channel_default=True):
    """Add a shared service. purposes: None means every purpose, now and later. routes is {'purposes': 'all' | 'ai' | (...), 'choices': (...), 'default': value or {purpose: value, '*': fallback}} or None.
    Registering the same name again replaces it (so a repository can refine its entry)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        raise ValueError(f"a service name is lowercase letters, digits and underscores: {name!r}")
    SERVICE_REGISTRY[name] = {"label": label or name, "channels": tuple(channels), "purposes": tuple(purposes) if purposes is not None else (), "all_purposes": purposes is None, "routes": routes, "options": bool(options), "channel_default": bool(channel_default)}


register_service("scrapling", label="Scrapling (page reading, browser, request controls)", options=True)
register_service("agent_reach", label="Agent-Reach (social and research channels through OpenCLI)", channels=AR_CHANNELS)
register_service("search", label="Web search (Google-backed providers)", channels=SEARCH_PROVIDERS)
GATEWAY_PROVIDERS = ("cloudflare", "github", "google", "groq", "mistral", "openrouter")          # the external free providers enabled in ai_providers.yaml (a test keeps this equal); Claude is the fallback and has no switch here
register_service("ai_gateway", label="AI Gateway (free-provider routing with Claude as fallback)", channels=GATEWAY_PROVIDERS, routes={"purposes": "ai", "choices": ("claude", "gateway_first"), "default": "gateway_first"})
from .. import connections as _CONN
from .. import guards as _GUARDS
register_service("guards", label="Guards (each protection that could stop a request; OFF never blocks)", channels=tuple(_GUARDS.GUARDS), purposes=(), channel_default=_GUARDS.legacy())
_CONN.load_declared()                                  # connections declared in connections_extra.yaml join the registry before the controls are generated
register_service("connections", label="External connections (the repositories themselves)", channels=tuple(_CONN.CONNECTIONS))
register_service("connection_routes", label="External connections: which route first", purposes=(), routes={"purposes": tuple(_CONN.CONNECTIONS), "choices": _CONN.ROUTES, "default": "external_first"})
register_service("connection_versions", label="External connections: which version", purposes=(), routes={"purposes": tuple(_CONN.CONNECTIONS), "choices": _CONN.VERSIONS, "default": "latest"})
register_service("connection_secrets", label="External connections: secrets handed over", channels=tuple(_CONN.CONNECTIONS), purposes=())
register_service("page_readers", label="Page readers (built-in, Jina Reader, Scrapling)", routes={"purposes": "all", "choices": PAGE_ENGINES, "default": {**PAGE_ENGINE_DEFAULTS, "*": "auto"}})


# Scrapling's per-request options that are central defaults, all ON: the owner turns one off by setting it false (Controls tab, `observatory controls`, or services.scrapling.defaults in observatory.yaml).
SCRAPLING_ON_DEFAULTS = {"browser": True, "session": True, "proxy": True, "impersonate": True, "hide_canvas": True, "block_webrtc": True, "humanize": True, "solve_challenges": True,
                         "disable_resources": True}


def service_defaults():
    """The default settings for every registered service."""
    out = {}
    for name, spec in SERVICE_REGISTRY.items():
        entry = {"enabled": True, "use_for": {p: True for p in spec_purposes(spec)}}
        if spec["channels"]:
            entry["channels"] = {c: spec.get("channel_default", True) for c in spec["channels"]}
        if spec["routes"]:
            entry["routes"] = {p: route_default_for(spec, p) for p in route_purposes(spec)}
        if spec["options"]:
            entry["defaults"], entry["overrides"] = dict(SCRAPLING_ON_DEFAULTS), []
        out[name] = entry
    return out
DEFAULTS = {
    "version": 1,
    "politeness": {
        "contact": "",                      # an email address or web page a site owner can write to (OBSERVATORY_CONTACT overrides)
        "min_delay_seconds": 5,             # the pause between two requests to one site
        "max_delay_seconds": 60,            # the longest pause the throttle will reach after blocks
        "timeout_seconds": 20,
        "max_bytes": 5_000_000,             # a response larger than this is refused
        "max_redirects": 3,
        "max_requests_per_domain": 80,      # a hard ceiling per site per run
        "retries": 2,                       # for a server error only
        "rate_limit_pause_seconds": 3600,   # how long a site that rate-limited us rests (survives between runs)
        "error_pause_seconds": 90,          # how long a site that keeps erroring rests
        "cache_minutes": 360,               # a page fetched again within this time is answered from memory
        "robots_cache_minutes": 1440,
    },
    "retention": {"auto_prune": True, "daily_days": 7, "weekly_weeks": 8, "monthly_months": 12, "always_keep_latest": 3},
    "changes": {
        "removal_confirm_runs": 2,          # a product must be missing this many runs in a row before it counts as removed
        "price_change_min_pct": 0.5,        # smaller price moves are ignored as noise
        "collection_move_min_positions": 5, # a product has to move this far in a collection to be reported
        "collection_top_n": 12,             # entering or leaving this many first places is always reported
        "page_change_min_chars": 40,        # smaller text changes on a page are ignored
        "max_events_per_competitor": 400,   # a run that finds more than this keeps the most important and says so
    },
    "shopify": {"max_products": 5000, "max_collections": 60, "max_pages_per_collection": 8, "per_page": 250, "probe_best_selling": True},
    "sitemaps": {"max_urls": 50000, "max_child_sitemaps": 25, "max_decompressed_bytes": 25_000_000},
    "pages": {"max_per_competitor": 12, "max_text_chars": 20000, "keep_text_chars": 6000},
    "feeds": {"max_items": 500},
    "retry": {"attempts": 3, "backoff_seconds": 2},          # with the rest guard OFF: how many times a refusing site is tried, and the pause (seconds) that grows with each try, before it is marked failed and the run moves on
    "crawl": {"max_pages": 25, "depth": 2},                     # a site crawl through Scrapling (SV.scrapling_crawl, corpus --what crawl): pages at most (1-200) and links deep at most (0-6)
    "discovery": {
        "enabled": True, "min_score": 40, "auto_monitor_score": 65, "auto_monitor_max": 6, "max_monitored": 10, "probe_max": 8,
        "min_appearances": 1, "lookup_max": 8,
        # who a candidate is, from several sources, and an AI second opinion beside the rubric (a person reviews every verdict)
        "enrich": True, "enrich_max": 8, "min_confidence": 0.7, "agent_reach_calls": 12, "judge": True, "judge_max": 16,
    },
    "youtube": {"daily_unit_cap": 3000, "max_channels": 25, "videos_per_channel": 50, "search_calls_per_run": 2, "min_videos_for_median": 4,
                "outlier_min_multiple": 2.0, "trend_window_days": 7, "trend_min_hours": 12, "trend_min_multiple": 2.0},
    "corpus": {"max_items_in_sheet": 150, "snippet_max_chars": 600},
    "sheet": {"max_events": 400, "max_products_per_competitor": 150},
    "readers": {"jina": True, "exa": True, "jina_per_minute": 15, "max_chars": 20000},
    "reddit": {"posts_per_target": 25, "max_targets": 20, "timeout_seconds": 90, "api_approval": {}},     # the official API route runs only with a recorded approval from Reddit
    "services": {},                 # built from the service registry when the settings are loaded (see register_service)
    "instagram": {"max_handles": 15, "posts_per_handle": 12, "timeout_seconds": 90},         # the Agent-Reach route that runs beside the existing collector
    "engine": {"pages": "core"},                   # core (the built-in reader) or scrapling (the isolated worker); the worker needs its own environment
    # lists are validated by the functions below, not by _RANGES
    "crawl_policy": {"robots": "obey", "reader_robots": "advisory", "basis": "", "overrides": []},
    "advanced": {"proxies": [], "rotate": "none"},
    # the owner's switches for each kind of request control; every one is on by default and can be turned off here (evasion is also recorded per competitor)
    "access_controls": {"custom_identity": True, "proxy": True, "session": True, "browser": True, "account_cookies": True, "evasion": True},
}

_RANGES = {   # (section, key): (minimum, maximum)
    ("readers", "jina_per_minute"): (1, 60), ("readers", "max_chars"): (1000, 200_000),
    ("reddit", "posts_per_target"): (1, 100), ("reddit", "max_targets"): (1, 100), ("reddit", "timeout_seconds"): (10, 600),
    ("instagram", "max_handles"): (1, 60), ("instagram", "posts_per_handle"): (1, 50), ("instagram", "timeout_seconds"): (10, 600),
    ("politeness", "min_delay_seconds"): (1, 120), ("politeness", "max_delay_seconds"): (5, 600), ("politeness", "timeout_seconds"): (3, 120),
    ("politeness", "max_bytes"): (10_000, 50_000_000), ("politeness", "max_redirects"): (0, 8), ("politeness", "max_requests_per_domain"): (1, 2000),
    ("politeness", "retries"): (0, 5), ("politeness", "rate_limit_pause_seconds"): (60, 86400),
    ("politeness", "error_pause_seconds"): (10, 3600), ("politeness", "cache_minutes"): (0, 10080), ("politeness", "robots_cache_minutes"): (0, 10080),
    ("retention", "daily_days"): (1, 90), ("retention", "weekly_weeks"): (0, 104), ("retention", "monthly_months"): (0, 60), ("retention", "always_keep_latest"): (2, 20),
    ("changes", "removal_confirm_runs"): (1, 10), ("changes", "price_change_min_pct"): (0, 100), ("changes", "collection_move_min_positions"): (1, 1000),
    ("changes", "collection_top_n"): (1, 250), ("changes", "page_change_min_chars"): (0, 5000), ("changes", "max_events_per_competitor"): (10, 5000),
    ("shopify", "max_products"): (1, 20000), ("shopify", "max_collections"): (1, 500), ("shopify", "max_pages_per_collection"): (1, 40), ("shopify", "per_page"): (10, 250),
    ("sitemaps", "max_urls"): (100, 200000), ("sitemaps", "max_child_sitemaps"): (1, 200), ("sitemaps", "max_decompressed_bytes"): (100_000, 100_000_000),
    ("pages", "max_per_competitor"): (0, 100), ("pages", "max_text_chars"): (500, 200_000), ("pages", "keep_text_chars"): (200, 50_000),
    ("feeds", "max_items"): (10, 5000), ("retry", "attempts"): (1, 10), ("retry", "backoff_seconds"): (0, 60), ("crawl", "max_pages"): (1, 200), ("crawl", "depth"): (0, 6),
    ("discovery", "min_score"): (0, 100), ("discovery", "auto_monitor_score"): (0, 100), ("discovery", "auto_monitor_max"): (0, 50),
    ("discovery", "max_monitored"): (1, 100), ("discovery", "probe_max"): (0, 50), ("discovery", "lookup_max"): (0, 50), ("discovery", "min_appearances"): (1, 100),
    ("discovery", "enrich_max"): (0, 30), ("discovery", "min_confidence"): (0.3, 0.99), ("discovery", "agent_reach_calls"): (0, 60), ("discovery", "judge_max"): (0, 50),
    ("youtube", "daily_unit_cap"): (0, 10000), ("youtube", "max_channels"): (0, 200), ("youtube", "videos_per_channel"): (4, 500),
    ("youtube", "search_calls_per_run"): (0, 20), ("youtube", "min_videos_for_median"): (4, 50), ("youtube", "outlier_min_multiple"): (1, 20),
    ("youtube", "trend_window_days"): (1, 60), ("youtube", "trend_min_hours"): (1, 168), ("youtube", "trend_min_multiple"): (1, 100),
    ("corpus", "max_items_in_sheet"): (10, 2000), ("corpus", "snippet_max_chars"): (100, 5000),
    ("sheet", "max_events"): (10, 5000), ("sheet", "max_products_per_competitor"): (10, 2000),
}


ROBOTS_MODES = ("obey", "advisory")
ROBOTS_BASES = ("owner", "client_authorised", "written_permission", "public_page_reader", "owner_decision")
ENGINES = ("core", "scrapling")


class ObservatoryConfigError(Exception):
    """A settings file is wrong. The message says which file, which key and what is allowed."""


def _only(data, allowed, where):
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ObservatoryConfigError(f"{where}: unknown setting {extra[0]!r}; allowed: {', '.join(sorted(allowed))}")


def _record(entry, where, bases, extra_keys=()):
    """A recorded decision needs a basis, a named approver and a date, so every exception can be traced to a person."""
    if not isinstance(entry, dict):
        raise ObservatoryConfigError(f"{where} must be name: value settings")
    for key in ("host", "basis", "approved_by", "date"):
        if not str(entry.get(key, "")).strip():
            raise ObservatoryConfigError(f"{where} needs {key}: an exception is a recorded decision (host, basis, approved_by, date)")
    if entry["basis"] not in bases:
        raise ObservatoryConfigError(f"{where}: basis must be one of {', '.join(bases)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry["date"])):
        raise ObservatoryConfigError(f"{where}: date must look like 2026-11-03")
    host = host_of(entry["host"])
    if not host:
        raise ObservatoryConfigError(f"{where}: {entry['host']!r} is not a web site name such as example.com")
    return host.removeprefix("www.")


def _crawl_policy(given, where):
    base = copy.deepcopy(DEFAULTS["crawl_policy"])
    _only(given, base, f"{where} crawl_policy")
    for key in ("robots", "reader_robots"):
        if key in given:
            if given[key] not in ROBOTS_MODES:
                raise ObservatoryConfigError(f"{where} crawl_policy.{key} must be one of {', '.join(ROBOTS_MODES)}")
            base[key] = given[key]
    base["basis"] = str(given.get("basis", "")).strip()
    if base["robots"] == "advisory" and not base["basis"]:
        raise ObservatoryConfigError(f"{where} crawl_policy.robots: advisory needs crawl_policy.basis, a sentence saying who decided and why (it is recorded in every report)")
    seen, out = set(), []
    for n, entry in enumerate(given.get("overrides") or [], start=1):
        host = _record(entry, f"{where} crawl_policy.overrides[{n}]", ROBOTS_BASES)
        _only(entry, ("host", "basis", "approved_by", "date", "robots", "note"), f"{where} crawl_policy.overrides[{n}]")
        mode = entry.get("robots", "ignore")
        if mode not in ("obey", "ignore", "advisory"):
            raise ObservatoryConfigError(f"{where} crawl_policy.overrides[{n}]: robots must be obey, advisory or ignore")
        if host in seen:
            raise ObservatoryConfigError(f"{where} crawl_policy.overrides: {host} is listed twice")
        seen.add(host)
        out.append({"host": host, "robots": mode, "basis": entry["basis"], "approved_by": str(entry["approved_by"]).strip(), "date": str(entry["date"]), "note": str(entry.get("note", ""))})
    base["overrides"] = out
    return base


def _bools(given, allowed, where):
    if not isinstance(given, dict):
        raise ObservatoryConfigError(f"{where} must be name: true/false settings")
    _only(given, allowed, where)
    for key, value in given.items():
        if not isinstance(value, bool):
            raise ObservatoryConfigError(f"{where}.{key} must be true or false")
    return dict(given)


def _match_ok(text):
    t = str(text).strip().lower()
    if t == "all":
        return True
    if t.startswith("purpose:"):
        return t.split(":", 1)[1] in PURPOSES
    return bool(re.fullmatch(r"(?:\*\.)?[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", t))


def _services(given, where):
    """The shared services' settings, validated from the service registry (so a newly registered service is validated too), with a competitor's own request-option rules for the Scrapling options."""
    out = service_defaults()
    _only(given, tuple(SERVICE_REGISTRY), f"{where} services")
    for name, section in given.items():
        spec, here = SERVICE_REGISTRY[name], f"{where} services.{name}"
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ObservatoryConfigError(f"{here} must be name: value settings")
        allowed = ["enabled", "use_for"] + (["channels"] if spec["channels"] else []) + (["routes"] if spec["routes"] else []) + (["defaults", "overrides"] if spec["options"] else [])
        _only(section, tuple(allowed), here)
        if "enabled" in section:
            if not isinstance(section["enabled"], bool):
                raise ObservatoryConfigError(f"{here}.enabled must be true or false")
            out[name]["enabled"] = section["enabled"]
        out[name]["use_for"].update(_bools(section.get("use_for") or {}, spec_purposes(spec), f"{here}.use_for"))
        if spec["channels"]:
            out[name]["channels"].update(_bools(section.get("channels") or {}, spec["channels"], f"{here}.channels"))
        if spec["routes"]:
            routes = section.get("routes") or {}
            if not isinstance(routes, dict):
                raise ObservatoryConfigError(f"{here}.routes must be purpose: choice settings")
            _only(routes, route_purposes(spec), f"{here}.routes")
            for purpose, choice in routes.items():
                if choice not in spec["routes"]["choices"]:
                    raise ObservatoryConfigError(f"{here}.routes.{purpose} must be one of {', '.join(spec['routes']['choices'])}")
                out[name]["routes"][purpose] = choice
        if spec["options"]:
            out[name]["defaults"] = {**SCRAPLING_ON_DEFAULTS, **_advanced_options(section.get("defaults") or {}, f"{here}.defaults")}
            overrides = section.get("overrides") or []
            if not isinstance(overrides, list):
                raise ObservatoryConfigError(f"{here}.overrides must be a list")
            for n, item in enumerate(overrides, start=1):
                label = f"{here}.overrides[{n}]"
                if not isinstance(item, dict) or not _match_ok(item.get("match", "")):
                    raise ObservatoryConfigError(f"{label} needs match: a site such as example.com or *.example.com, purpose:discovery (or any other purpose), or all")
                rest = {k: v for k, v in item.items() if k != "match"}
                out[name]["overrides"].append({"match": str(item["match"]).strip().lower(), **_advanced_options(rest, label)})
    return out


def _advanced(given, where):
    _only(given, DEFAULTS["advanced"], f"{where} advanced")
    proxies = given.get("proxies") or []
    if not isinstance(proxies, list) or any(not re.fullmatch(r"[a-z0-9_]{1,20}", str(p)) for p in proxies):
        raise ObservatoryConfigError(f"{where} advanced.proxies must list short names (letters, digits, underscore); the proxy address itself is read from the secret OBSERVATORY_PROXY_<NAME>, never from this file")
    rotate = given.get("rotate", "none")
    if rotate not in ("none", "sequential", "random"):
        raise ObservatoryConfigError(f"{where} advanced.rotate must be none, sequential or random")
    return {"proxies": list(proxies), "rotate": rotate}


def _check(section, key, value, where):
    default = DEFAULTS[section][key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ObservatoryConfigError(f"{where} {section}.{key} must be true or false, got {value!r}")
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ObservatoryConfigError(f"{where} {section}.{key} must be text, got {value!r}")
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservatoryConfigError(f"{where} {section}.{key} must be a number, got {value!r}")
    low, high = _RANGES[(section, key)]
    if not low <= value <= high:
        raise ObservatoryConfigError(f"{where} {section}.{key} must be between {low} and {high}, got {value!r}")
    return value


def load_settings(path=DEFAULT_PATH):
    """The settings with the file's values over the defaults. A missing file means the defaults."""
    data = {}
    p = Path(path)
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ObservatoryConfigError(f"{path} is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ObservatoryConfigError(f"{path} must contain name: value settings")
    _only(data, DEFAULTS, str(path))
    if data.get("version", 1) != 1:
        raise ObservatoryConfigError(f"{path}: version must be 1")
    out = copy.deepcopy(DEFAULTS)
    out["services"] = service_defaults()
    for section, given in data.items():
        if section == "version":
            continue
        if not isinstance(given, dict):
            raise ObservatoryConfigError(f"{path} {section} must be name: value settings")
        if section == "reddit":
            _only(given, DEFAULTS["reddit"], f"{path} reddit")
            for key, value in given.items():
                if key == "api_approval":
                    approval = value or {}
                    if approval:
                        if not isinstance(approval, dict):
                            raise ObservatoryConfigError(f"{path} reddit.api_approval must be name: value settings")
                        _only(approval, ("basis", "approved_by", "date", "reference"), f"{path} reddit.api_approval")
                        for need in ("basis", "approved_by", "date"):
                            if not str(approval.get(need, "")).strip():
                                raise ObservatoryConfigError(f"{path} reddit.api_approval needs {need}: the official API route runs only on a recorded approval from Reddit")
                        if approval["basis"] not in ("non_commercial_approved", "commercial_contract"):
                            raise ObservatoryConfigError(f"{path} reddit.api_approval.basis must be non_commercial_approved or commercial_contract")
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(approval["date"])):
                            raise ObservatoryConfigError(f"{path} reddit.api_approval.date must look like 2026-11-03")
                        out[section]["api_approval"] = {k: str(approval.get(k, "")).strip() for k in ("basis", "approved_by", "date", "reference")}
                else:
                    _check(section, key, value, path)
                    out[section][key] = value
            continue
        if section == "access_controls":
            _only(given, DEFAULTS[section], f"{path} access_controls")
            for key, value in given.items():
                if not isinstance(value, bool):
                    raise ObservatoryConfigError(f"{path} access_controls.{key} must be true or false")
                out[section][key] = value
            continue
        if section in ("crawl_policy", "advanced", "services"):
            out[section] = {"crawl_policy": _crawl_policy, "advanced": _advanced, "services": _services}[section](given, str(path))
            continue
        _only(given, DEFAULTS[section], f"{path} {section}")
        if section == "engine":
            for key, value in given.items():
                if value not in ENGINES:
                    raise ObservatoryConfigError(f"{path} engine.{key} must be one of {', '.join(ENGINES)}")
                out[section][key] = value
            continue
        for key, value in given.items():
            out[section][key] = _check(section, key, value, str(path))
    pol = out["politeness"]
    if pol["max_delay_seconds"] < pol["min_delay_seconds"]:
        raise ObservatoryConfigError(f"{path} politeness.max_delay_seconds cannot be below min_delay_seconds")
    if out["discovery"]["auto_monitor_score"] < out["discovery"]["min_score"]:
        raise ObservatoryConfigError(f"{path} discovery.auto_monitor_score cannot be below min_score")
    if out["retention"]["monthly_months"] and out["retention"]["weekly_weeks"] == 0 and out["retention"]["daily_days"] < 1:
        raise ObservatoryConfigError(f"{path} retention needs at least one daily day")
    return out


# ---- competitors and brand options ---------------------------------------------------------------------------------------------------------------

_HOST = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_COMPETITOR_KEYS = ("name", "domain", "kind", "pages", "blog", "feeds", "youtube", "instagram", "tiktok", "monitor", "advanced", "selectors")
_ADVANCED_KEYS = ("browser", "stealth", "proxy", "rotate", "cookies_env", "session", "identity", "headers", "accept_language", "delay", "headless", "network_idle", "wait_selector",
                  "locale", "timezone", "disable_resources", "rest_hours",
                  "impersonate", "hide_canvas", "block_webrtc", "humanize", "solve_challenges",           # how a request presents itself to a site's bot detection; the owner's choice, on any competitor
                  "fingerprint", "solve_cloudflare")                                                      # older names for hide_canvas + block_webrtc and for solve_challenges, still accepted
# headers a person may not set: credentials (they belong to accounts we own and go through cookies_env on a listed host) and anything that falsifies where a request comes from
_FORBIDDEN_HEADERS = {"cookie", "authorization", "proxy-authorization", "x-forwarded-for", "forwarded", "via", "client-ip", "x-real-ip", "true-client-ip", "x-originating-ip", "host",
                      "user-agent", "from", "content-length", "transfer-encoding", "connection"}
_MONITOR_KEYS = ("shopify", "sitemap", "feeds", "pages")
BRAND_KEYS = ("competitors", "discovery", "youtube", "reddit", "exclude_domains", "ignore")


def host_of(value):
    """A bare host name from a domain or address, or '' when it is not one."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    if "@" in text or text.count(":") > 1:
        return ""
    text = text.split(":")[0].rstrip(".")
    return text if _HOST.match(text) else ""


def competitor_id(domain):
    """A stable folder and key name for a competitor's site, for example graza-co."""
    return re.sub(r"[^a-z0-9]+", "-", host_of(domain).removeprefix("www.")).strip("-")


def _urls(value, where):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ObservatoryConfigError(f"{where} must be a list of web addresses")
    return [v.strip() for v in value]


def _advanced_options(adv_in, label):
    """Validate request options (identity, headers, proxy, session, browser, cookies, fingerprint, challenge handling, pace, rest) and return them in their standard form. Used for a competitor,
    for the central defaults and for target-specific overrides alike, so every place that can set them follows the same rules."""
    if not isinstance(adv_in, dict):
        raise ObservatoryConfigError(f"{label} must be name: value settings")
    _only(adv_in, _ADVANCED_KEYS, f"{label}")
    advanced = {}
    for key, value in adv_in.items():
        here = f"{label}.{key}"
        if key == "cookies_env":
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", str(value)):
                raise ObservatoryConfigError(f"{here} must be the NAME of a secret, such as OBSERVATORY_COOKIES_SHOP; the cookies themselves never go in a file")
        elif key == "rotate":
            if value not in ("sequential", "random"):
                raise ObservatoryConfigError(f"{here} must be sequential or random")
        elif key == "wait_selector":
            if not isinstance(value, str):
                raise ObservatoryConfigError(f"{here} must be a CSS selector")
        elif key in ("identity", "accept_language"):
            if not isinstance(value, str) or not value.strip() or len(value) > 200 or re.search(r"[\x00-\x1f]", value):
                raise ObservatoryConfigError(f"{here} must be a short piece of text on one line")
        elif key == "locale":
            if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2})?", value):
                raise ObservatoryConfigError(f"{here} must look like en-GB or de-DE")
        elif key == "timezone":
            if not isinstance(value, str) or not re.fullmatch(r"UTC|[A-Za-z]+(/[A-Za-z_\-]+){1,2}", value):
                raise ObservatoryConfigError(f"{here} must be a timezone name such as Europe/London")
        elif key == "headers":
            if not isinstance(value, dict) or len(value) > 12:
                raise ObservatoryConfigError(f"{here} must be name: value pairs (at most 12)")
            for name, text in value.items():
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{0,40}", str(name)) or str(name).lower() in _FORBIDDEN_HEADERS:
                    raise ObservatoryConfigError(f"{here}: {name!r} cannot be set here (account cookies go through cookies_env, and identity headers are set with identity)")
                if not isinstance(text, str) or len(text) > 200 or re.search(r"[\x00-\x1f]", text):
                    raise ObservatoryConfigError(f"{here}.{name} must be short text on one line")
        elif key == "delay":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3600:
                raise ObservatoryConfigError(f"{here} must be a number of seconds between 0 and 3600 (it can only slow requests down, never go below the site's own wishes)")
        elif key == "rest_hours":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 168:
                raise ObservatoryConfigError(f"{here} must be a number of hours between 1 and 168: how long a host rests after a challenge, refusal or rate limit (never shorter than the standing rest)")
        elif not isinstance(value, bool):
            raise ObservatoryConfigError(f"{here} must be true or false")
        advanced[key] = value
    if advanced.pop("fingerprint", False):                           # the older names mean these
        advanced["hide_canvas"] = advanced["block_webrtc"] = True
    if advanced.pop("solve_cloudflare", False):
        advanced["solve_challenges"] = True
    return advanced


def _competitor(item, n, where):
    if isinstance(item, str):
        item = {"name": item, "domain": item} if host_of(item) else {"name": item}
    if not isinstance(item, dict):
        raise ObservatoryConfigError(f"{where} competitor {n} must be a name or name: value settings")
    _only(item, _COMPETITOR_KEYS, f"{where} competitor {n}")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ObservatoryConfigError(f"{where} competitor {n} needs a name")
    host = host_of(item.get("domain"))
    if item.get("domain") and not host:
        raise ObservatoryConfigError(f"{where} competitor {n}: {item['domain']!r} is not a web site name such as example.com")
    kind = item.get("kind", "store")
    if kind not in ("store", "brand", "media"):
        raise ObservatoryConfigError(f"{where} competitor {n}: kind must be store, brand or media")
    monitor = {"shopify": "auto", "sitemap": True, "feeds": True, "pages": True}
    given = item.get("monitor") or {}
    if not isinstance(given, dict):
        raise ObservatoryConfigError(f"{where} competitor {n} monitor must be name: value settings")
    _only(given, _MONITOR_KEYS, f"{where} competitor {n} monitor")
    for key, value in given.items():
        if key == "shopify" and value not in ("auto", True, False):
            raise ObservatoryConfigError(f"{where} competitor {n} monitor.shopify must be auto, true or false")
        if key != "shopify" and not isinstance(value, bool):
            raise ObservatoryConfigError(f"{where} competitor {n} monitor.{key} must be true or false")
        monitor[key] = value
    advanced = _advanced_options(item.get("advanced") or {}, f"{where} competitor {n} advanced")
    if advanced and not host:
        raise ObservatoryConfigError(f"{where} competitor {n}: advanced methods need the site's domain")
    selectors_in = item.get("selectors") or {}
    if not isinstance(selectors_in, dict) or any(k not in ("announcement", "headline") or not isinstance(v, str) or not v.strip() for k, v in selectors_in.items()):
        raise ObservatoryConfigError(f"{where} competitor {n} selectors may name announcement and headline, each a CSS selector")
    return {"id": competitor_id(host) or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name.strip(), "domain": host, "kind": kind, "advanced": advanced, "selectors": dict(selectors_in),
            "pages": _urls(item.get("pages"), f"{where} competitor {n} pages"), "blog": (_urls(item.get("blog"), f"{where} competitor {n} blog") or [""])[0],
            "feeds": _urls(item.get("feeds"), f"{where} competitor {n} feeds"), "youtube": str(item.get("youtube") or "").strip(),
            "instagram": str(item.get("instagram") or "").strip().lstrip("@"), "tiktok": str(item.get("tiktok") or "").strip().lstrip("@"),
            "monitor": monitor, "origin": "observatory.yaml"}


def load_brand_file(slug, root="brands"):
    """brands/<brand>/observatory.yaml, checked. A missing file is an empty one."""
    path = Path(root) / slug / "observatory.yaml"
    data = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ObservatoryConfigError(f"{path} is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ObservatoryConfigError(f"{path} must contain name: value settings")
    where = f"{slug}/observatory.yaml"
    _only(data, BRAND_KEYS, where)
    competitors, seen = [], set()
    for n, item in enumerate(data.get("competitors") or [], start=1):
        comp = _competitor(item, n, where)
        if comp["id"] in seen:
            raise ObservatoryConfigError(f"{where}: competitor {comp['name']!r} is listed twice")
        seen.add(comp["id"])
        competitors.append(comp)
    disc = data.get("discovery") or {}
    if not isinstance(disc, dict):
        raise ObservatoryConfigError(f"{where} discovery must be name: value settings")
    _only(disc, DEFAULTS["discovery"], f"{where} discovery")
    for key, value in disc.items():
        _check("discovery", key, value, where)
    yt = data.get("youtube") or {}
    if not isinstance(yt, dict):
        raise ObservatoryConfigError(f"{where} youtube must be name: value settings")
    _only(yt, ("channels", "queries", "max_videos_per_channel"), f"{where} youtube")
    channels = yt.get("channels") or []
    queries = yt.get("queries") or []
    for label, items in (("channels", channels), ("queries", queries)):
        if not isinstance(items, list) or any(not isinstance(v, str) or not v.strip() for v in items):
            raise ObservatoryConfigError(f"{where} youtube.{label} must be a list of text values")
    rd = data.get("reddit") or {}
    if not isinstance(rd, dict):
        raise ObservatoryConfigError(f"{where} reddit must be name: value settings")
    _only(rd, ("subreddits", "queries"), f"{where} reddit")
    for label in ("subreddits", "queries"):
        items = rd.get(label) or []
        if not isinstance(items, list) or any(not isinstance(v, str) or not v.strip() for v in items):
            raise ObservatoryConfigError(f"{where} reddit.{label} must be a list of text values")
    excluded = []
    for key in ("exclude_domains", "ignore"):
        for value in data.get(key) or []:
            host = host_of(value)
            if not host:
                raise ObservatoryConfigError(f"{where} {key}: {value!r} is not a web site name such as example.com")
            excluded.append(host.removeprefix("www."))
    return {"competitors": competitors, "discovery": disc, "youtube": {"channels": [c.strip() for c in channels], "queries": [q.strip() for q in queries],
            "max_videos_per_channel": yt.get("max_videos_per_channel")},
            "reddit": {"subreddits": [x.strip().removeprefix("r/") for x in (rd.get("subreddits") or [])], "queries": [x.strip() for x in (rd.get("queries") or [])]}, "exclude_domains": sorted(set(excluded))}


def research_seeds(brand, root="brands"):
    """The competitors already named in the brand's research.yaml that have a web site: the observatory watches them too."""
    from .. import research
    try:
        loaded = research.load_brand_research(brand, root=root)
    except Exception:           # a research.yaml problem is reported by the research commands, not here
        return []
    out = []
    for item in loaded.get("competitors", []):
        host = host_of(item.get("domain"))
        if host:
            out.append({"id": competitor_id(host), "name": item["name"], "domain": host, "kind": "store", "pages": [], "blog": "", "feeds": [], "advanced": {}, "selectors": {}, "youtube": "",
                        "instagram": "", "tiktok": "", "monitor": {"shopify": "auto", "sitemap": True, "feeds": True, "pages": True}, "origin": "research.yaml"})
    return out
