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
        "max_wait_seconds": 120,            # a Retry-After longer than this stops the site for the run instead of waiting
        "rate_limit_pause_seconds": 3600,   # how long a site that rate-limited us rests (survives between runs)
        "error_pause_seconds": 90,          # how long a site that keeps erroring rests
        "cache_minutes": 360,               # a page fetched again within this time is answered from memory
        "robots_cache_minutes": 1440,
    },
    "retention": {"daily_days": 7, "weekly_weeks": 8, "monthly_months": 12, "always_keep_latest": 3},
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
    "discovery": {
        "enabled": True, "min_score": 40, "auto_monitor_score": 65, "auto_monitor_max": 6, "max_monitored": 10, "probe_max": 8,
        "use_ai": False, "min_appearances": 1, "lookup_max": 8,
    },
    "youtube": {"daily_unit_cap": 3000, "max_channels": 25, "videos_per_channel": 50, "search_calls_per_run": 2, "min_videos_for_median": 4,
                "outlier_min_multiple": 2.0},
    "corpus": {"max_items_in_sheet": 150, "snippet_max_chars": 600},
    "sheet": {"max_events": 400, "max_products_per_competitor": 150},
    "readers": {"jina": True, "exa": True, "jina_per_minute": 15, "max_chars": 20000},
    "engine": {"pages": "core"},                   # core (the built-in reader) or scrapling (the isolated worker); the worker needs its own environment
    # lists are validated by the functions below, not by _RANGES
    "crawl_policy": {"robots": "obey", "reader_robots": "advisory", "basis": "", "overrides": []},
    "authorised_access": {"hosts": []},
    "advanced": {"proxies": [], "rotate": "none"},
}

_RANGES = {   # (section, key): (minimum, maximum)
    ("readers", "jina_per_minute"): (1, 60), ("readers", "max_chars"): (1000, 200_000),
    ("politeness", "min_delay_seconds"): (1, 120), ("politeness", "max_delay_seconds"): (5, 600), ("politeness", "timeout_seconds"): (3, 120),
    ("politeness", "max_bytes"): (10_000, 50_000_000), ("politeness", "max_redirects"): (0, 8), ("politeness", "max_requests_per_domain"): (1, 2000),
    ("politeness", "retries"): (0, 5), ("politeness", "max_wait_seconds"): (0, 900), ("politeness", "rate_limit_pause_seconds"): (60, 86400),
    ("politeness", "error_pause_seconds"): (10, 3600), ("politeness", "cache_minutes"): (0, 10080), ("politeness", "robots_cache_minutes"): (0, 10080),
    ("retention", "daily_days"): (1, 90), ("retention", "weekly_weeks"): (0, 104), ("retention", "monthly_months"): (0, 60), ("retention", "always_keep_latest"): (2, 20),
    ("changes", "removal_confirm_runs"): (1, 10), ("changes", "price_change_min_pct"): (0, 100), ("changes", "collection_move_min_positions"): (1, 1000),
    ("changes", "collection_top_n"): (1, 250), ("changes", "page_change_min_chars"): (0, 5000), ("changes", "max_events_per_competitor"): (10, 5000),
    ("shopify", "max_products"): (1, 20000), ("shopify", "max_collections"): (1, 500), ("shopify", "max_pages_per_collection"): (1, 40), ("shopify", "per_page"): (10, 250),
    ("sitemaps", "max_urls"): (100, 200000), ("sitemaps", "max_child_sitemaps"): (1, 200), ("sitemaps", "max_decompressed_bytes"): (100_000, 100_000_000),
    ("pages", "max_per_competitor"): (0, 100), ("pages", "max_text_chars"): (500, 200_000), ("pages", "keep_text_chars"): (200, 50_000),
    ("feeds", "max_items"): (10, 5000),
    ("discovery", "min_score"): (0, 100), ("discovery", "auto_monitor_score"): (0, 100), ("discovery", "auto_monitor_max"): (0, 50),
    ("discovery", "max_monitored"): (1, 100), ("discovery", "probe_max"): (0, 50), ("discovery", "lookup_max"): (0, 50), ("discovery", "min_appearances"): (1, 100),
    ("youtube", "daily_unit_cap"): (0, 10000), ("youtube", "max_channels"): (0, 200), ("youtube", "videos_per_channel"): (4, 500),
    ("youtube", "search_calls_per_run"): (0, 20), ("youtube", "min_videos_for_median"): (4, 50), ("youtube", "outlier_min_multiple"): (1, 20),
    ("corpus", "max_items_in_sheet"): (10, 2000), ("corpus", "snippet_max_chars"): (100, 5000),
    ("sheet", "max_events"): (10, 5000), ("sheet", "max_products_per_competitor"): (10, 2000),
}


ROBOTS_MODES = ("obey", "advisory")
ROBOTS_BASES = ("owner", "client_authorised", "written_permission", "public_page_reader", "owner_decision")
ACCESS_BASES = ("owner", "client_authorised", "written_permission")
ACCESS_METHODS = ("stealth", "challenge", "fingerprint", "proxy", "logged_in_browser")
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


def _authorised(given, where):
    _only(given, DEFAULTS["authorised_access"], f"{where} authorised_access")
    seen, out = set(), []
    for n, entry in enumerate(given.get("hosts") or [], start=1):
        host = _record(entry, f"{where} authorised_access.hosts[{n}]", ACCESS_BASES)
        _only(entry, ("host", "basis", "approved_by", "date", "methods", "note"), f"{where} authorised_access.hosts[{n}]")
        methods = entry.get("methods")
        if not isinstance(methods, list) or not methods or any(m not in ACCESS_METHODS for m in methods):
            raise ObservatoryConfigError(f"{where} authorised_access.hosts[{n}]: methods must list at least one of {', '.join(ACCESS_METHODS)}")
        if host in seen:
            raise ObservatoryConfigError(f"{where} authorised_access.hosts: {host} is listed twice")
        seen.add(host)
        out.append({"host": host, "basis": entry["basis"], "approved_by": str(entry["approved_by"]).strip(), "date": str(entry["date"]), "methods": sorted(set(methods)),
                    "note": str(entry.get("note", ""))})
    return {"hosts": out}


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
    for section, given in data.items():
        if section == "version":
            continue
        if not isinstance(given, dict):
            raise ObservatoryConfigError(f"{path} {section} must be name: value settings")
        if section in ("crawl_policy", "authorised_access", "advanced"):
            out[section] = {"crawl_policy": _crawl_policy, "authorised_access": _authorised, "advanced": _advanced}[section](given, str(path))
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
    if out["discovery"]["use_ai"]:
        raise ObservatoryConfigError(f"{path} discovery.use_ai: the AI judge for discovery is not built yet, so the setting is refused rather than ignored; candidates are scored by the rubric")
    return out


# ---- competitors and brand options ---------------------------------------------------------------------------------------------------------------

_HOST = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_COMPETITOR_KEYS = ("name", "domain", "kind", "pages", "blog", "feeds", "youtube", "instagram", "tiktok", "monitor", "advanced", "selectors")
_ADVANCED_KEYS = ("stealth", "solve_cloudflare", "impersonate", "fingerprint", "proxy", "cookies_env", "rotate", "headless", "network_idle", "wait_selector")
_MONITOR_KEYS = ("shopify", "sitemap", "feeds", "pages")
BRAND_KEYS = ("competitors", "discovery", "youtube", "exclude_domains", "ignore")


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
    adv_in = item.get("advanced") or {}
    if not isinstance(adv_in, dict):
        raise ObservatoryConfigError(f"{where} competitor {n} advanced must be name: value settings")
    _only(adv_in, _ADVANCED_KEYS, f"{where} competitor {n} advanced")
    advanced = {}
    for key, value in adv_in.items():
        if key in ("cookies_env",):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", str(value)):
                raise ObservatoryConfigError(f"{where} competitor {n} advanced.cookies_env must be the NAME of a secret, such as OBSERVATORY_COOKIES_SHOP; the cookies themselves never go in a file")
        elif key == "rotate":
            if value not in ("sequential", "random"):
                raise ObservatoryConfigError(f"{where} competitor {n} advanced.rotate must be sequential or random")
        elif key == "wait_selector":
            if not isinstance(value, str):
                raise ObservatoryConfigError(f"{where} competitor {n} advanced.wait_selector must be a CSS selector")
        elif not isinstance(value, bool):
            raise ObservatoryConfigError(f"{where} competitor {n} advanced.{key} must be true or false")
        advanced[key] = value
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
    excluded = []
    for key in ("exclude_domains", "ignore"):
        for value in data.get(key) or []:
            host = host_of(value)
            if not host:
                raise ObservatoryConfigError(f"{where} {key}: {value!r} is not a web site name such as example.com")
            excluded.append(host.removeprefix("www."))
    return {"competitors": competitors, "discovery": disc, "youtube": {"channels": [c.strip() for c in channels], "queries": [q.strip() for q in queries],
            "max_videos_per_channel": yt.get("max_videos_per_channel")}, "exclude_domains": sorted(set(excluded))}


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
