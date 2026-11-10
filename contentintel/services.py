"""The platform's shared services: Scrapling, Agent-Reach, web search, the AI Gateway and the page readers, built once and available to every module, command, workflow and repository.

Anything that needs one of them asks this module with a purpose and gets the same behaviour from the same settings:

  1. the owner's central settings (observatory.yaml `services:`, and the owner's changes made through the Controls tab or `observatory controls`);
  2. optional target-specific overrides (Scrapling: by host or by purpose);
  3. the target's own settings (a competitor's `advanced:` block), which win over both.

A service is declared once in the registry (observatory/config.py `register_service`); validation, defaults, the Controls tab and the `controls` command all follow from that, so a new repository adds its
service and its switches without new plumbing: see docs/platform/adding-a-service.md. There is no list of allowed hosts and no approval step. A master switch, a per-purpose switch and, where a service has them,
per-channel (provider, site) switches turn each service off, and one that is off is refused with a message naming the switch. An owner's switch is never recorded as a route failure. Credentials never live
here: cookies and proxies are named secrets the worker reads from the environment; Agent-Reach logins stay in a browser on a machine you control.
"""
import json
import os
import re
from pathlib import Path

from . import connections as CONN
from . import contract as K
from . import guards as _GUARDS
from .observatory import config as OC

# ---- the controls the owner can see and change (Controls tab, `observatory controls`) -----------------------------------------------------------

_OPTION_CONTROLS = (       # central request defaults for Scrapling: (option, kind, unset value, what it does)
    ("browser", "bool", False, "read pages in Scrapling's browser"), ("session", "bool", False, "keep the cookies a site sets during a run"), ("proxy", "bool", False, "use the proxies named under advanced.proxies"),
    ("impersonate", "bool", False, "TLS and browser identity change"), ("hide_canvas", "bool", False, "canvas fingerprint change (browser)"), ("block_webrtc", "bool", False, "WebRTC block (browser)"),
    ("humanize", "bool", False, "simulate human cursor movement (browser)"), ("solve_challenges", "bool", False, "ask the browser to solve Cloudflare checks"), ("disable_resources", "bool", False, "skip images and fonts (browser)"),
    ("delay", "number", 0, "seconds between requests; never below the site's Crawl-delay or the tool's minimum (0 = standing pause)"), ("rest_hours", "number", 0, "hours a host rests after a refusal (0 = standing rest)"),
    ("locale", "text", "", "browser language, such as en-GB"), ("timezone", "text", "", "browser timezone, such as Europe/London"), ("identity", "text", "", "the User-Agent text sent (blank = the honest default)"),
    ("accept_language", "text", "", "Accept-Language header, such as en-GB"))

_OPTION_CONTROLS = tuple((opt, kind, OC.SCRAPLING_ON_DEFAULTS.get(opt, unset) if kind == "bool" else unset, text) for opt, kind, unset, text in _OPTION_CONTROLS)      # the on/off ones default ON

_ROUTE_TEXT = {"connection_routes": "which route the {p} connection tries first: external_first (the repository itself, then the platform's own version), builtin_first, external_only or builtin_only",
               "connection_versions": "which version of the {p} repository is used: latest (always the newest) or pinned",
               "ai_gateway": "language-model calls for {p}: gateway_first (try the free providers, fall back to Claude) or claude (Claude only)",
               "page_readers": "the reader used for pages in {p}: auto (Scrapling, then Jina, then built-in; never another engine after a refusal), jina, core (built-in) or scrapling"}


def _build():
    """Every control, generated from the service registry, so a registered service appears here by itself."""
    rows = []

    def add(key, kind, default, text, scope, choices=()):
        rows.append({"key": key, "kind": kind, "default": default, "text": text, "scope": scope, "choices": tuple(choices)})
    for name, spec in OC.SERVICE_REGISTRY.items():
        label = spec["label"].split(" (")[0]
        add(f"services.{name}.enabled", "bool", True, f"{label} is available to the platform (master switch)", label)
        for p in OC.spec_purposes(spec):
            add(f"services.{name}.use_for.{p}", "bool", True, f"{p.replace('_', ' ')} may use {label}", label)
        for c in spec["channels"]:
            if name == "guards":
                add(f"services.{name}.channels.{c}", "bool", spec.get("channel_default", True), f"guard {c}: {_GUARDS.GUARDS[c]}", label)
            elif name == "connection_secrets":
                add(f"services.{name}.channels.{c}", "bool", True, f"the secrets the {c} repository declares it needs are handed to it (every other secret is withheld)", label)
            elif name == "connections":
                add(f"services.{name}.channels.{c}", "bool", True, f"the {c} repository is connected (OFF: it is never run)", label)
            else:
                add(f"services.{name}.channels.{c}", "bool", spec.get("channel_default", True), f"the {c} {'channel' if name == 'agent_reach' else 'provider'} may be used", label)
        if spec["routes"]:
            for p in OC.route_purposes(spec):
                add(f"services.{name}.routes.{p}", "choice", OC.route_default_for(spec, p), _ROUTE_TEXT.get(name, "route for {p}").format(p=p.replace("_", " ")), label, spec["routes"]["choices"])
        if spec["options"]:
            for opt, kind, unset, text in _OPTION_CONTROLS:
                add(f"services.{name}.defaults.{opt}", kind, unset, text + " (central default for every target)", f"{label} defaults")
    for k in ("custom_identity", "proxy", "session", "browser", "account_cookies", "evasion"):
        add(f"access_controls.{k}", "bool", True, f"owner switch: {k.replace('_', ' ')}", "Request controls")
    add("engine.pages", "choice", "core", "the older single switch: scrapling makes Scrapling the only reader for competitor and discovery pages while their route is auto (page_readers routes are the full control)", "Engine", ("core", "scrapling"))
    return rows


CONTROLS = _build()
BY_KEY = {c["key"]: c for c in CONTROLS}


def register_service(name, **kwargs):
    """Register a shared service (see config.register_service) and refresh the controls so it appears in the Controls tab and the controls command."""
    OC.register_service(name, **kwargs)
    CONTROLS[:] = _build()
    BY_KEY.clear()
    BY_KEY.update({c["key"]: c for c in CONTROLS})


class ControlError(ValueError):
    pass


def coerce(defn, raw):
    """The typed value for a control from text typed in a sheet or on the command line, or a ControlError that says what is acceptable."""
    kind = defn["kind"]
    text = str(raw).strip()
    if kind == "bool":
        if text.lower() in ("true", "yes", "on", "1", "y"):
            return True
        if text.lower() in ("false", "no", "off", "0", "n"):
            return False
        raise ControlError(f"{defn['key']} must be on or off")
    if kind == "number":
        try:
            value = float(text) if text else 0.0
        except ValueError:
            raise ControlError(f"{defn['key']} must be a number") from None
        if value < 0 or value > (3600 if defn["key"].endswith("delay") else 168):
            raise ControlError(f"{defn['key']} is out of range")
        return int(value) if value == int(value) else value
    if kind == "choice":
        if text not in defn["choices"]:
            raise ControlError(f"{defn['key']} must be one of {', '.join(defn['choices'])}")
        return text
    if re.search(r"[\x00-\x1f]", text) or len(text) > 200:
        raise ControlError(f"{defn['key']} must be short text on one line")
    return text


def overrides_path(data_dir):
    return Path(data_dir) / "_shared" / "controls.json"


def load_overrides(data_dir):
    try:
        data = json.loads(overrides_path(data_dir).read_text(encoding="utf-8"))
        values = data.get("values", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in values.items() if k in BY_KEY}


def save_overrides(data_dir, values, who, now, note=""):
    path = overrides_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(path.read_text(encoding="utf-8")).get("history", [])
    except (OSError, ValueError):
        history = []
    if note:
        history = (history + [{"at": now, "by": who, "change": note}])[-200:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"values": values, "history": history, "updated_at": now}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _slot(settings, key):
    """(container, last name) inside settings for a control key; defaults live in a dict that may not have the option."""
    parts = key.split(".")
    node = settings
    for part in parts[:-1]:
        node = node[part]
    return node, parts[-1]


def apply_controls(settings, values):
    """The settings with the owner's control changes laid over them. A central default set back to its unset value is removed, so it does not mask a competitor's or an override's own setting."""
    for key, value in values.items():
        defn = BY_KEY.get(key)
        if not defn:
            continue
        node, name = _slot(settings, key)
        if ".defaults." in key:
            unset = next(u for o, k, u, _ in _OPTION_CONTROLS if o == name)
            if value == unset:
                node.pop(name, None)
                continue
        node[name] = value
    return settings


def current(settings, key):
    node, name = _slot(settings, key)
    defn = BY_KEY[key]
    return node.get(name, defn["default"]) if ".defaults." in key else node[name]


def effective_rows(settings, data_dir, yaml_settings=None):
    """Every control with its value now, its initial default, and where the value comes from (default, observatory.yaml or the Controls tab)."""
    owner = load_overrides(data_dir)
    rows = []
    for defn in CONTROLS:
        value = current(settings, defn["key"])
        if defn["key"] in owner:
            source = "controls"
        else:
            source = "default" if value == defn["default"] else "observatory.yaml"
        rows.append({**defn, "value": value, "source": source})
    return rows


def display(value):
    return "on" if value is True else "off" if value is False else str(value)


def _refresh_controls():
    CONTROLS[:] = _build()
    BY_KEY.clear()
    BY_KEY.update({c["key"]: c for c in CONTROLS})


def register_purpose(name, *, ai_route=False):
    """Add a kind of work to the platform (see config.register_purpose) and refresh the controls, so every service shows a switch for it."""
    OC.register_purpose(name, ai_route=ai_route)
    _refresh_controls()


def unregister_purpose(name):
    OC.unregister_purpose(name)
    _refresh_controls()


def unregister_service(name):
    """Remove a service added with register_service (for tests and for a repository that is taken out). The built-in services cannot be removed."""
    if name in ("scrapling", "agent_reach", "search", "ai_gateway", "page_readers"):
        raise ValueError(f"{name} is a built-in service")
    OC.SERVICE_REGISTRY.pop(name, None)
    CONTROLS[:] = _build()
    BY_KEY.clear()
    BY_KEY.update({c["key"]: c for c in CONTROLS})


# ---- resolving what a request should use ---------------------------------------------------------------------------------------------------------

def _matches(match, host, purpose):
    if match == "all":
        return True
    if match.startswith("purpose:"):
        return match.split(":", 1)[1] == purpose
    host = (host or "").lower()
    base = match.removeprefix("*.").removeprefix("www.")                 # a pattern written with www. matches the site and its subdomains, as one written without it does
    return host == base or host.endswith("." + base)


def allowed(settings, service, purpose, channel=None):
    """(True, '') or (False, why) for any registered service: its master switch, the switch for this purpose and, when asked, the channel's (provider's, site's) switch. The one check every module uses."""
    sec = settings["services"].get(service)
    if sec is None:
        return False, f"{service} is not a registered service"
    if not sec["enabled"]:
        return False, f"{OC.SERVICE_REGISTRY[service]['label'].split(' (')[0]} is switched off (services.{service}.enabled)"
    if purpose not in sec["use_for"]:
        return False, f"{service} has no switch for {purpose}"
    if not sec["use_for"][purpose]:
        return False, f"{OC.SERVICE_REGISTRY[service]['label'].split(' (')[0]} is switched off for {purpose.replace('_', ' ')} (services.{service}.use_for.{purpose})"
    if channel is not None and not sec.get("channels", {}).get(channel, False):
        return False, f"the {channel} {'channel' if service == 'agent_reach' else 'provider'} is switched off (services.{service}.channels.{channel})"
    return True, ""


def route_for(settings, service, purpose):
    """The owner's chosen route for a purpose (the AI Gateway: claude or gateway_first; the page readers: jina, core, scrapling or auto)."""
    return settings["services"][service]["routes"][purpose]


def scrapling_allowed(settings, purpose):
    return allowed(settings, "scrapling", purpose)


def resolve_scrapling(settings, host, purpose, target_advanced=None):
    """The request options for one target: central defaults, then every matching override in order, then the target's own settings. Raises Failure(BAD_INPUT) when Scrapling is off for this purpose."""
    ok, why = scrapling_allowed(settings, purpose)
    if not ok:
        raise K.Failure(K.BAD_INPUT, why)
    sc = settings["services"]["scrapling"]
    merged = dict(sc["defaults"])
    explicit = {}
    for item in sc["overrides"]:
        if _matches(item["match"], host, purpose):
            explicit.update({k: v for k, v in item.items() if k != "match"})
    explicit.update(target_advanced or {})
    merged.update(explicit)
    # The central defaults are all on. They never cause a refusal themselves: an option that came only from the defaults is skipped when its permission is switched off (access_controls) or, for
    # the proxy, when no proxy is listed. The same option asked for by an override or the target itself still refuses loudly, as it always did.
    from .observatory import worker_client as wc
    controls = settings["access_controls"]
    for key, switch in (("browser", "browser"), ("session", "session"), ("proxy", "proxy"), ("impersonate", "evasion"), ("hide_canvas", "evasion"), ("block_webrtc", "evasion"),
                        ("humanize", "evasion"), ("solve_challenges", "evasion")):
        if merged.get(key) and not explicit.get(key) and not controls.get(switch, True):
            merged.pop(key)
    if merged.get("proxy") and not explicit.get("proxy") and not wc.proxy_env_names(settings):
        merged.pop("proxy")
    return merged


def agent_reach_allowed(settings, channel, purpose):
    ok, why = allowed(settings, "agent_reach", purpose, channel)
    return (ok, why) if ok else (False, why.replace("Agent-Reach is switched off for", "Agent-Reach is switched off for"))


def agent_reach_client(settings, channel, purpose, *, environ, timeout=90, factory=None):
    """The client for an Agent-Reach channel. When the Agent-Reach repository is installed here, connected, and the owner's route allows it, the client runs Agent-Reach itself first and the platform's own
    adapter is the backup (the owner's route for the agent_reach connection decides the order); otherwise it is the platform's own adapter, exactly as before."""
    builtin = _builtin_agent_reach_client(settings, channel, purpose, environ=environ, timeout=timeout, factory=factory)
    if factory:
        return builtin
    from . import agent_reach_external as ARX
    from . import connections as CONN
    d = CONN.decide(settings, "agent_reach")
    exe = CONN.binary_path("agent_reach") if d["on"] and d["route"] != "builtin_only" else ""
    if not exe:
        return builtin
    return ARX.RoutedAgentReach(settings, ARX.ExternalAgentReach(settings, binary=exe, environ=environ, timeout=timeout), builtin)


def _builtin_agent_reach_client(settings, channel, purpose, *, environ, timeout=90, factory=None):
    """A client for an Agent-Reach channel, or Failure(NEED_SETUP) naming the switch that is off. Instagram and Reddit use OpenCLI in a logged-in browser, and their client refuses to run on a hosted runner;
    YouTube uses yt-dlp, which needs no login and so has no such refusal (YouTube's own bot checks of data-centre addresses apply). `factory` replaces the real program in tests."""
    ok, why = agent_reach_allowed(settings, channel, purpose)
    if not ok:
        raise K.Failure(K.NEED_SETUP, why)
    if factory:
        return factory()
    if channel == "youtube":                        # Agent-Reach's YouTube channel is yt-dlp: no login, no OpenCLI, no browser
        from . import ytdlp_youtube
        return ytdlp_youtube.YtDlpYouTube(timeout=timeout, environ=None)
    if channel == "github":                         # public REST API: no login, no browser (GITHUB_TOKEN, if set, only raises the rate limit)
        from . import github_reach
        return github_reach.GitHubPublic(token=(environ or {}).get("GITHUB_TOKEN", ""), timeout=timeout, environ=environ)
    if channel == "web":
        raise K.Failure(K.BAD_INPUT, "Agent-Reach's web channel is the platform's shared page readers: use SV.read_page (or the corpus read command)")
    from . import agent_reach_instagram as AR
    return AR.OpenCLI(environ.get("OPENCLI_BIN", "opencli"), timeout=timeout, site=channel, environ=environ)


# ---- loading settings with the owner's changes, and the Controls tab ----------------------------------------------------------------------------

PLUGIN_GROUP = "contentintel.services"
EXTRA_FILE = "services_extra.yaml"
NOTES = []                       # problems met while loading registrations; they are reported, never raised, so one repository's mistake cannot stop another's work
_PLUGINS_DONE = False


def load_declared(path=None):
    """Register the services and purposes a YAML file declares: the way a repository (or the owner, from the GitHub page) adds one with no code. The file is `services_extra.yaml` next to observatory.yaml,
    or the path in CONTENTINTEL_SERVICES_FILE. Returns the names registered. A bad entry is noted in NOTES and skipped."""
    import yaml
    path = path or os.environ.get("CONTENTINTEL_SERVICES_FILE") or EXTRA_FILE
    if not Path(path).is_file():
        return []
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("it must contain name: value settings")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        NOTES.append(f"{path} not read: {exc}")
        return []
    done = []
    for item in data.get("purposes") or []:
        try:
            name, ai = (item, False) if isinstance(item, str) else (item["name"], bool(item.get("ai_route")))
            register_purpose(name, ai_route=ai)
        except (ValueError, KeyError, TypeError) as exc:
            NOTES.append(f"{path}: purpose {item!r} skipped: {exc}")
    for item in data.get("services") or []:
        try:
            if not isinstance(item, dict) or set(item) - {"name", "label", "channels", "purposes", "routes", "options"}:
                raise ValueError("a service needs name and may have label, channels, purposes, routes, options")
            routes = item.get("routes")
            if routes is not None and (not isinstance(routes, dict) or not {"choices", "default"} <= set(routes)):
                raise ValueError("routes needs choices and default (and optionally purposes: all, ai or a list)")
            if routes is not None:
                routes = {"purposes": routes.get("purposes", "all"), "choices": tuple(routes["choices"]), "default": routes["default"]}
            register_service(item["name"], label=item.get("label", ""), channels=tuple(item.get("channels") or ()), purposes=tuple(item["purposes"]) if item.get("purposes") is not None else None,
                             routes=routes, options=bool(item.get("options")))
            done.append(item["name"])
        except (ValueError, KeyError, TypeError) as exc:
            NOTES.append(f"{path}: service {item.get('name') if isinstance(item, dict) else item!r} skipped: {exc}")
    return done


def load_plugins(force=False):
    """Register the services and purposes that installed packages offer under the entry-point group `contentintel.services`. Each entry point is a function called with an object that has
    register_service and register_purpose, so another repository's package adds its services just by being installed alongside this one. Runs once per process; returns the entry points loaded."""
    global _PLUGINS_DONE
    if _PLUGINS_DONE and not force:
        return []
    _PLUGINS_DONE = True
    from importlib import metadata
    from types import SimpleNamespace
    api = SimpleNamespace(register_service=register_service, register_purpose=register_purpose)
    loaded = []
    try:
        points = list(metadata.entry_points(group=PLUGIN_GROUP))
    except Exception as exc:                        # noqa: BLE001
        NOTES.append(f"plugins not listed: {exc.__class__.__name__}")
        return []
    for point in points:
        try:
            point.load()(api)
            loaded.append(point.name)
        except Exception as exc:                    # noqa: BLE001  (a plugin that fails is reported and skipped)
            NOTES.append(f"plugin {point.name} skipped: {exc.__class__.__name__}: {str(exc)[:120]}")
    return loaded


def load_settings(path=None, data_dir="data"):
    """observatory.yaml with the owner's control changes (made in the Controls tab or with `observatory controls`) laid over it. Services and purposes that other repositories registered (a plugin, or
    services_extra.yaml) are registered first, so they are validated, defaulted and controlled like the built-in ones."""
    load_plugins()
    load_declared()
    settings = OC.load_settings(path) if path else OC.load_settings()
    return apply_controls(settings, load_overrides(data_dir))


CONTROLS_TAB = "Controls"
CONTROLS_COLUMNS = ["key", "value", "default", "source", "scope", "description"]


def _read_state(data_dir):
    try:
        data = json.loads(overrides_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sheet_tab(settings, data_dir, now, sheet_id="sheet"):
    """The Controls tab: every control with its value now. Change a value in the sheet and the next update reads it back (on or off, a number, text or a listed choice). Records what was written,
    so the next read can tell a person's edit from what the tool wrote. That record is kept per sheet (sheet_id, the brand), so a copy of the tab that has not been updated yet in another brand's sheet
    is never mistaken for an edit."""
    rows = {}
    written = {}
    for r in effective_rows(settings, data_dir):
        rows[r["key"]] = {"key": r["key"], "value": display(r["value"]), "default": display(r["default"]), "source": r["source"], "scope": r["scope"], "description": r["text"]}
        written[r["key"]] = display(r["value"])
    path = overrides_path(data_dir)
    data = _read_state(data_dir)
    all_written = data.get("written") if isinstance(data.get("written"), dict) and all(isinstance(v, dict) for v in data.get("written", {}).values()) else {}
    all_written[sheet_id] = written
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"values": data.get("values", {}), "history": data.get("history", []), "written": all_written, "updated_at": now}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return {"title": CONTROLS_TAB, "columns": CONTROLS_COLUMNS, "rows_by_key": rows, "key_col": "key", "starter_manual": []}


def apply_sheet_controls(grid, settings, data_dir, now, save=True, sheet_id="sheet"):
    """(changes, problems) from the values a person edited in the Controls tab. Only a cell that differs from what the tool last wrote counts as an edit, so a stale copy of the tab in another
    brand's sheet cannot undo a change. A value that cannot be understood is reported and ignored; the next update writes the real value back."""
    if not grid:
        return [], []
    header = [str(h).strip().lower() for h in grid[0]]
    if "key" not in header or "value" not in header:
        return [], []
    ki, vi = header.index("key"), header.index("value")
    state = _read_state(data_dir)
    written = (state.get("written") or {}).get(sheet_id, {}) if isinstance(state.get("written"), dict) else {}
    values = load_overrides(data_dir)
    changes, problems = [], []
    for row in grid[1:]:
        if len(row) <= max(ki, vi):
            continue
        key, raw = str(row[ki]).strip(), str(row[vi]).strip()
        defn = BY_KEY.get(key)
        if not defn or raw == "":
            continue
        now_text = display(current(settings, key))
        baseline = written.get(key, now_text)
        if raw.lower() == baseline.lower() or raw.lower() == now_text.lower():
            continue
        try:
            typed = coerce(defn, raw)
        except ControlError as exc:
            problems.append(str(exc))
            continue
        if typed == defn["default"]:
            values.pop(key, None)
        else:
            values[key] = typed
        apply_controls(settings, {key: typed})
        changes.append(f"{key}: {now_text} -> {display(typed)}")
    if changes and save:
        save_overrides(data_dir, values, "sheet", now, "; ".join(changes))
        data = _read_state(data_dir)                            # save_overrides rewrites the file; keep every sheet's record of what was written
        data["written"] = state.get("written", {})
        overrides_path(data_dir).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return changes, problems


def scrapling_page(settings, url, purpose, *, contact, python, snaps, target_advanced=None, pauses=None, now="", environ=None, runner=None):
    """One page read through the isolated Scrapling worker with the options resolved for this host and purpose (central defaults, overrides, the target's own). The same call serves competitor
    pages, social-account discovery and content research. Raises Failure for a switched-off service, a missing worker or whatever the worker reports."""
    from urllib.parse import urlsplit

    from .observatory import worker_client as wc
    host = (urlsplit(url).hostname or "").lower()
    adv = resolve_scrapling(settings, host, purpose, target_advanced)
    comp = {"id": OC.competitor_id(host), "name": host, "domain": host, "advanced": adv, "selectors": {}}
    extra = {}
    if environ is not None:
        extra["environ"] = environ
    if runner is not None:
        extra["runner"] = runner
    return wc.WorkerPages(comp, [url], settings, contact, python, snaps, pauses=pauses, now=now, **extra).get(url)


def _crawl_state_path(state_dir, url):
    import hashlib
    from pathlib import Path
    return Path(state_dir) / "_shared" / "crawl_state" / (hashlib.sha1(url.strip().rstrip("/").lower().encode("utf-8")).hexdigest()[:16] + ".json")


def scrapling_crawl(settings, url, purpose, *, contact, python, max_pages=None, depth=None, target_advanced=None, pauses=None, now="", environ=None, runner=None, sitemap=False, extract=None, state_dir=None, resume=False):
    """A polite crawl of one site through the isolated Scrapling worker, for any purpose and any repository: {"pages": [{url, title, markdown, ...}], "skipped": [...], "stopped": why}. The options are
    resolved exactly as for a single page (central defaults, overrides, the target's own); max_pages and depth default to the owner's crawl settings. Raises Failure for a switched-off service, a
    missing worker, a resting site or whatever the worker reports."""
    from urllib.parse import urlsplit

    from .observatory import worker_client as wc
    ok, why = allowed(settings, "scrapling", purpose)
    if not ok:
        raise K.Failure(K.NEED_SETUP, why)
    host = (urlsplit(url).hostname or "").lower()
    comp = {"id": OC.competitor_id(host), "name": host, "domain": host, "advanced": resolve_scrapling(settings, host, purpose, target_advanced), "selectors": {}}
    extra = {}
    if environ is not None:
        extra["environ"] = environ
    if runner is not None:
        extra["runner"] = runner
    state = None
    path = _crawl_state_path(state_dir, url) if state_dir else None
    if resume and path is not None and path.is_file():                  # a paused crawl of this address carries on where it stopped
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
    got = wc.crawl_site(comp, url, settings, contact, python, max_pages=max_pages or settings["crawl"]["max_pages"], depth=settings["crawl"]["depth"] if depth is None else depth,
                        pauses=pauses, now=now, sitemap=sitemap, resume=state, extract=extract, **extra)
    got["resumed"], got["state_saved"] = bool(state), False
    if path is not None:
        if got.get("state") and got["stopped"] != "no more pages to follow":                # stopped early (page limit, a refusal): keep what is left for a later run
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(got["state"]), encoding="utf-8")
            got["state_saved"] = True
        elif path.is_file():
            path.unlink()                                                    # finished: nothing to resume
    return got


def discover_feeds(settings, url, purpose, *, contact, python, target_advanced=None, pauses=None, now="", environ=None, runner=None):
    """The feeds (RSS, Atom) of a site, for any purpose and repository: [{"url", "how"}]. Links the page advertises, plus feeds at the usual addresses that really answer with one, read through the
    Scrapling worker's guarded fetch under the owner's switches. Raises Failure for a switched-off service, a missing worker, a resting site or whatever the worker reports."""
    from urllib.parse import urlsplit

    from .observatory import feeds as feeds_mod
    from .observatory import worker_client as wc
    ok, why = allowed(settings, "scrapling", purpose)
    if not ok:
        raise K.Failure(K.NEED_SETUP, why)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    comp = {"id": OC.competitor_id(host), "name": host, "domain": host, "advanced": resolve_scrapling(settings, host, purpose, target_advanced), "selectors": {}}
    extra = {}
    if environ is not None:
        extra["environ"] = environ
    if runner is not None:
        extra["runner"] = runner
    return wc.discover_feeds(comp, url, settings, contact, python, guesses=feeds_mod.guess_feed_urls(f"{parts.scheme}://{parts.netloc}"), pauses=pauses, now=now, **extra)


# ---- shared interface: web search ---------------------------------------------------------------------------------------------------------------

def search_providers(settings, purpose, environ, *, build=None):
    """The search providers a purpose may use ({name: provider}), built from the environment's keys and filtered by the owner's switches (the Search service, this purpose, each provider). Google-backed
    providers (Serper, SerpApi), Tavily and Exa are all behind this one call, so every module and repository gets the same providers under the same controls. {} when search is off for the purpose."""
    ok, _ = allowed(settings, "search", purpose)
    if not ok:
        return {}
    if build is None:
        from . import providers as providers_mod
        build = providers_mod.build_providers
    enabled = settings["services"]["search"]["channels"]
    return {name: prov for name, prov in build(environ).items() if enabled.get(name, True)}


# ---- shared interface: the AI Gateway --------------------------------------------------------------------------------------------------------------

def ai_stack(store, ledger, environ, *, providers_config="ai_providers.yaml", lanes_config="ai_lanes.yaml", gateway_dir=None, workdir="ai-work", settings=None):
    """(router, usage, review, side, notes, finish): the real AI layer with whatever providers have a credential here. finish() saves the spending and review records and stops the gateway. Used by the
    competitor judge and by every module the owner routes through the gateway."""
    from types import SimpleNamespace

    from .ai import commands as aic
    ns = SimpleNamespace(providers_config=providers_config, lanes_config=lanes_config, gateway_dir=gateway_dir or environ.get("FREEAPI_GATEWAY_DIR", "gateway-src"), workdir=workdir)
    registry, _ = aic.load_all(ns)
    notes = []
    if settings is not None:                                    # the owner's switch for each external provider (services.ai_gateway.channels.<provider>); Claude, the fallback, has none
        import dataclasses
        for pid, on in settings["services"]["ai_gateway"].get("channels", {}).items():
            provider = registry.providers.get(pid)
            if not on and provider is not None and provider.enabled:
                registry.providers[pid] = dataclasses.replace(provider, enabled=False)
                notes.append(f"SKIPPED: {pid}: switched off by the owner (services.ai_gateway.channels.{pid})")
    clients, side = aic.live_clients(registry, environ, ns.gateway_dir, workdir, notes)
    _, _, usage, review, router = aic.build_stack(ns, store, ledger, mode="live", clients=clients)

    def finish():
        try:
            aic.save_state(usage, review, ledger, store, "live")
        finally:
            if side is not None:
                side.stop()
    return router, usage, review, side, notes, finish


class RoutedClaude:
    """A Claude-compatible client (complete, json_task, json_task_with_images and everything else the wrapped client has) that, for a purpose the owner routed gateway_first, asks the AI Gateway first and
    falls back to the wrapped client when the gateway has no usable answer. Modules keep calling the same three methods and need no change. Pictures always go to the wrapped client: the free providers
    here have no vision. Nothing the gateway does is a failure of the module: no answer means the wrapped client is asked, exactly as it would have been."""

    def __init__(self, inner, purpose, stack_factory, now_fn):
        self.inner, self.purpose, self._factory, self._now = inner, purpose, stack_factory, now_fn
        self._stack, self.calls, self.notes = None, [], []

    def __getattr__(self, name):                    # model, check, answer, prepare_prompt and anything else Claude offers
        return getattr(self.inner, name)

    def _router(self):
        if self._stack is None:
            try:
                self._stack = self._factory() or False
            except Exception as exc:                # noqa: BLE001  (the gateway is an extra; the module must go on without it)
                self.notes.append(f"AI Gateway not available: {exc.__class__.__name__}: {str(exc)[:120]}")
                self._stack = False
            if self._stack:
                import atexit
                atexit.register(self.close)
        return self._stack or None

    def close(self):
        stack, self._stack = self._stack, False
        if stack and len(stack) > 1 and callable(stack[1]):
            stack[1]()

    def _ask(self, lane, system, user):
        stack = self._router()
        if stack is None:
            return None
        res = stack[0].run(lane, data_class="internal", instructions=user, system=system, now=self._now(), item_ids=(self.purpose,))
        if res["status"] in ("ok", "needs_review"):
            return res
        self.notes.append(f"AI Gateway had no answer for {self.purpose} ({res.get('reason', res['status'])}); Claude was used")
        return None

    def complete(self, system, user, max_tokens=None):
        res = self._ask("general_text", system, user)
        if res and isinstance(res.get("text"), str) and res["text"].strip():
            self.calls.append("gateway")
            return res["text"]
        self.calls.append("claude")
        return self.inner.complete(system, user, max_tokens)

    def json_task(self, system, user, max_tokens=None):
        res = self._ask("general_json", system, user)
        if res and isinstance(res.get("output"), (dict, list)) and res["output"]:
            self.calls.append("gateway")
            return res["output"]
        self.calls.append("claude")
        return self.inner.json_task(system, user, max_tokens)

    def json_task_with_images(self, system, user, images, max_tokens=None):
        self.calls.append("claude")
        return self.inner.json_task_with_images(system, user, images, max_tokens)

    def prepare_prompt(self, question, context):
        """The research prompt for Gemini (collectors), through the gateway when routed, else Claude's own."""
        from .claude_client import PREPARE_SYSTEM
        res = self._ask("general_text", PREPARE_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nResearch question:\n{question}")
        if res and isinstance(res.get("text"), str) and res["text"].strip():
            self.calls.append("gateway")
            return res["text"]
        self.calls.append("claude")
        return self.inner.prepare_prompt(question, context)

    def answer(self, original_prompt, context, gemini_message):
        """The reply to Gemini (collectors): '' when its message was not asking anything. Same rule as Claude's own method."""
        from .claude_client import ANSWER_SYSTEM, NO_QUESTION
        res = self._ask("general_text", ANSWER_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nOur research request:\n{original_prompt}\n\nGemini's message:\n{gemini_message}")
        if res and isinstance(res.get("text"), str) and res["text"].strip():
            self.calls.append("gateway")
            return "" if res["text"].strip().upper().startswith(NO_QUESTION) else res["text"]
        self.calls.append("claude")
        return self.inner.answer(original_prompt, context, gemini_message)


def ai_client(settings, purpose, claude, *, stack_factory, now_fn):
    """The client a module should use for a purpose: the wrapped Claude (unchanged behaviour, the default) unless the owner set services.ai_gateway.routes.<purpose> to gateway_first and the AI Gateway is
    switched on for it. None stays None (no Claude key: the module reports that as it always did)."""
    if claude is None:
        return None
    ok, _ = allowed(settings, "ai_gateway", purpose)
    if not ok or route_for(settings, "ai_gateway", purpose) != "gateway_first":
        return claude
    return RoutedClaude(claude, purpose, stack_factory, now_fn)


# ---- shared interface: page readers ----------------------------------------------------------------------------------------------------------------

_PAGE_PURPOSES_OLD_ENGINE = ("competitors", "discovery", "social_discovery")


def effective_engine(settings, purpose, *, scrapling_installed):
    """The reader to use for a purpose: the owner's page_readers route (auto by default: Scrapling first when installed, then Jina, then the built-in reader). The older single switch engine.pages:
    scrapling (and the workflow's Scrapling box that sets it) still works: for competitor and discovery pages it makes Scrapling the only reader, but only while the route is left at auto, so an owner's
    explicit route (core, jina...) is never overridden by it."""
    choice = route_for(settings, "page_readers", purpose)
    if choice == "auto" and settings["engine"]["pages"] == "scrapling" and purpose in _PAGE_PURPOSES_OLD_ENGINE and scrapling_installed:
        return "scrapling"
    return choice


REFUSALS = (K.PLATFORM_BLOCKED, K.RATE_LIMITED, K.NOT_FOUND)         # after one of these no other engine is tried: a refusal is never a reason to escalate


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_page(settings, url, purpose, engines, *, engine=None):
    """One page through the owner's chosen reader for the purpose: jina, core (built-in), scrapling, or auto (Scrapling, then Jina, then built-in). `engines` is {name: callable(url) -> page} supplied by the
    module (it has the contact address, the polite client and the data folder), so every module and repository shares the same choice, switches and rules while keeping its own plumbing. In auto, an engine
    that is unavailable or fails for an ordinary reason passes the page to the next; a refusal (blocked, rate-limited, not found) ends it. The result always has url, title, text, social and engine."""
    ok, why = allowed(settings, "page_readers", purpose)
    if not ok:
        raise K.Failure(K.BAD_INPUT, why)
    choice = engine or route_for(settings, "page_readers", purpose)
    if choice not in OC.PAGE_ENGINES:
        raise K.Failure(K.BAD_INPUT, f"the page reader {choice!r} is not one of {', '.join(OC.PAGE_ENGINES)}")
    order = [choice]
    skipped, last = [], None
    if choice == "auto":                                    # the owner's choices for the Scrapling connection: on or off, and which route first
        conn = CONN.decide(settings, "scrapling")
        order = {"external_first": ["scrapling", "jina", "core"], "builtin_first": ["core", "jina", "scrapling"], "external_only": ["scrapling"], "builtin_only": ["jina", "core"]}[conn["route"]]
        if not conn["on"]:
            skipped.append("scrapling: the Scrapling connection is switched off (services.connections.channels.scrapling)")
            order = [n for n in order if n != "scrapling"]
    for name in order:
        fn = engines.get(name)
        if fn is None:
            skipped.append(f"{name}: not available here")
            continue
        if name == "scrapling":
            ok, why = scrapling_allowed(settings, purpose)
            if not ok:
                skipped.append(f"scrapling: {why}")
                continue
        try:
            page = dict(fn(url))
        except K.Failure as exc:
            if name == "scrapling" and exc.code not in REFUSALS:                         # a site's refusal is not the repository failing; anything else is
                CONN.note(settings, "scrapling", False, _utc_now(), code=exc.code, message=str(exc))
            if choice != "auto" or (exc.code in REFUSALS and _GUARDS.on("rest_after_refusal")):        # the rest guard: a refusal ends the chain; with it off the next reader gets a go
                raise
            skipped.append(f"{name}: {exc.code}")
            last = exc
            continue
        if name == "scrapling":
            CONN.note(settings, "scrapling", True, _utc_now())
        page.setdefault("title", "")
        page.setdefault("text", "")
        page.setdefault("social", {})
        page["engine"] = name
        page["skipped"] = skipped
        return page
    raise K.Failure(getattr(last, "code", K.NEED_SETUP), "no page reader could read this page: " + "; ".join(skipped))
