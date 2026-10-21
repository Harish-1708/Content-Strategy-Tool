"""Research collection.

For each brand: plan queries (from its research.yaml plus a few cheap automatic ones), reuse
anything searched recently, and send each remaining query to the first provider that is allowed
to spend credits on it. One shared ledger counts credits across all brands, so a free tier is
never overspent. Results are saved exactly as the providers returned them (title, link,
snippet, date); nothing is interpreted here.
"""
import copy
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from .collectors import CollectError, parse_collect_config
from .providers import PROVIDER_NAMES, RECENCY, AuthError, ProviderError, QuotaError

KINDS = ("web", "news")
DEPTHS = ("basic", "advanced")
_TS = "%Y-%m-%dT%H:%M:%SZ"
_TRACKING_PREFIXES = ("utm_", "mc_", "ref_", "gad_")
_TRACKING_EXACT = {"gclid", "fbclid", "igshid", "srsltid", "msclkid", "yclid", "_ga", "_gl",
                   "mkt_tok", "si", "is_from_webapp", "sender_device", "web_id"}


class ResearchError(Exception):
    """A problem a person can fix in a research file."""


# ---------------------------------------------------------------------------------------
# settings (research.yaml at the top of the repository)
# ---------------------------------------------------------------------------------------

DEFAULTS = {
    "providers": {
        # monthly_cap: most credits we are willing to spend this calendar month
        # lifetime_cap: for one-time pools (Serper.dev's free credits); blank for monthly plans
        # keep_free: credits to leave untouched when the provider reports its own balance
        "serper_dev": {"monthly_cap": 250, "lifetime_cap": 2400, "keep_free": 0},
        "tavily": {"monthly_cap": 600, "lifetime_cap": None, "keep_free": 50},
        "serpapi": {"monthly_cap": 100, "lifetime_cap": None, "keep_free": 20},
    },
    "routing": {
        "web": ["serper_dev", "tavily", "serpapi"],
        "news": ["tavily", "serper_dev", "serpapi"],
    },
    "defaults": {"results_per_query": 10, "cache_days": 7, "max_queries_per_run": 30,
                 "country": "us", "recency": None, "compare_queries": 5},
}


@dataclass(frozen=True)
class Caps:
    monthly_cap: float
    lifetime_cap: object
    keep_free: float


@dataclass(frozen=True)
class Settings:
    caps: dict
    routing: dict
    results_per_query: int
    cache_days: int
    max_queries_per_run: int
    country: object
    recency: object
    compare_queries: int = 5


def _number(value, where, *, minimum=0, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ResearchError(f"{where} must be a number of at least {minimum}, got {value!r}")
    return value


def _whole(value, where, minimum=0):
    """A whole number (7 or 7.0, never 7.5), so nothing is silently rounded."""
    number = _number(value, where, minimum=minimum)
    if number != int(number):
        raise ResearchError(f"{where} must be a whole number, got {value!r}")
    return int(number)


def _only_keys(data, allowed, where):
    if not isinstance(data, dict):
        raise ResearchError(f"{where} must be a set of name: value pairs")
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ResearchError(f"{where}: unknown {', '.join(map(repr, extra))} "
                            f"(allowed: {', '.join(sorted(allowed))})")


def _country(value, where):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 2 or not value.isalpha():
        raise ResearchError(f"{where} must be a two-letter country code such as us, got {value!r}")
    return value.lower()


def _recency(value, where):
    if value is not None and value not in RECENCY:
        raise ResearchError(f"{where} must be one of {list(RECENCY)} or blank, got {value!r}")
    return value


def load_settings(path="research.yaml"):
    data = {}
    path = Path(path)
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _only_keys(data, ("providers", "routing", "defaults", "collectors"), "research.yaml")

    caps = {}
    given = data.get("providers") or {}
    _only_keys(given, PROVIDER_NAMES, "research.yaml providers")
    for name in PROVIDER_NAMES:
        merged = {**DEFAULTS["providers"][name], **(given.get(name) or {})}
        _only_keys(merged, ("monthly_cap", "lifetime_cap", "keep_free"), f"providers.{name}")
        caps[name] = Caps(
            monthly_cap=_number(merged["monthly_cap"], f"providers.{name}.monthly_cap"),
            lifetime_cap=_number(merged["lifetime_cap"], f"providers.{name}.lifetime_cap",
                                 allow_none=True),
            keep_free=_number(merged["keep_free"], f"providers.{name}.keep_free"))

    routing = {kind: list(order) for kind, order in DEFAULTS["routing"].items()}
    given = data.get("routing") or {}
    _only_keys(given, KINDS, "research.yaml routing")
    for kind, order in given.items():
        if (not isinstance(order, list) or not order or len(set(order)) != len(order)
                or any(name not in PROVIDER_NAMES for name in order)):
            raise ResearchError(f"routing.{kind} must list providers from {list(PROVIDER_NAMES)} "
                                f"once each, got {order!r}")
        routing[kind] = list(order)

    base = {**DEFAULTS["defaults"], **(data.get("defaults") or {})}
    _only_keys(base, DEFAULTS["defaults"], "research.yaml defaults")
    per_query = _number(base["results_per_query"], "defaults.results_per_query", minimum=1)
    if per_query > 10 or per_query != int(per_query):
        raise ResearchError("defaults.results_per_query must be a whole number from 1 to 10")
    return Settings(
        caps=caps, routing=routing, results_per_query=int(per_query),
        cache_days=_whole(base["cache_days"], "defaults.cache_days"),
        max_queries_per_run=_whole(base["max_queries_per_run"], "defaults.max_queries_per_run", minimum=1),
        country=_country(base["country"], "defaults.country"),
        recency=_recency(base["recency"], "defaults.recency"),
        compare_queries=_whole(base["compare_queries"], "defaults.compare_queries", minimum=1))


# ---------------------------------------------------------------------------------------
# a brand's research file (brands/<brand>/research.yaml) and the queries it produces
# ---------------------------------------------------------------------------------------

DEFAULT_TEMPLATES = {
    "brand": ['"{brand}" reviews', '"{brand}" competitors', '"{brand}" TikTok'],
    "competitor": ["{name} reviews", {"q": "{name}", "kind": "news", "recency": "month"}],
    "topic": ["{topic}", {"q": "{topic} trends", "kind": "news", "recency": "month"}],
}


@dataclass(frozen=True)
class Query:
    brand: str
    text: str
    kind: str = "web"
    recency: object = None
    tag: str = "auto"
    country: object = None
    depth: str = "basic"


def _norm(text):
    return " ".join(str(text).lower().split())


def _query_spec(item, where):
    """A string or {q, kind, recency, depth} -> (text, kind, recency, depth)."""
    if isinstance(item, str):
        item = {"q": item}
    _only_keys(item, ("q", "kind", "recency", "depth"), where)
    if not isinstance(item.get("q"), str) or not item["q"].strip():
        raise ResearchError(f"{where} needs a q: the search text")
    kind = item.get("kind", "web")
    depth = item.get("depth", "basic")
    if kind not in KINDS:
        raise ResearchError(f"{where}: kind must be one of {list(KINDS)}, got {kind!r}")
    if depth not in DEPTHS:
        raise ResearchError(f"{where}: depth must be one of {list(DEPTHS)}, got {depth!r}")
    return item["q"], kind, _recency(item.get("recency"), f"{where} recency"), depth


def load_brand_research(brand, root="brands"):
    path = Path(root) / brand.slug / "research.yaml"
    data = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    where = f"{brand.slug}/research.yaml"
    _only_keys(data, ("competitors", "topics", "queries", "templates", "auto", "country",
                      "ads", "social", "deep_research"), where)
    try:
        collect = parse_collect_config(data, where)
    except CollectError as exc:
        raise ResearchError(str(exc)) from exc

    competitors = []
    for n, item in enumerate(data.get("competitors") or [], start=1):
        if isinstance(item, str):
            item = {"name": item}
        _only_keys(item, ("name", "domain"), f"{where} competitor {n}")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ResearchError(f"{where} competitor {n} needs a name")
        competitors.append({"name": item["name"].strip(), "domain": item.get("domain") or ""})

    topics = data.get("topics") or []
    if not isinstance(topics, list) or any(not isinstance(t, str) or not t.strip() for t in topics):
        raise ResearchError(f"{where} topics must be a list of phrases")

    explicit = data.get("queries") or []
    if not isinstance(explicit, list):
        raise ResearchError(f"{where} queries must be a list")
    for n, item in enumerate(explicit, start=1):
        _query_spec(item, f"{where} query {n}")

    templates = {k: list(v) for k, v in DEFAULT_TEMPLATES.items()}
    given = data.get("templates") or {}
    _only_keys(given, DEFAULT_TEMPLATES, f"{where} templates")
    for key, items in given.items():
        if not isinstance(items, list):
            raise ResearchError(f"{where} templates.{key} must be a list")
        for n, item in enumerate(items, start=1):
            _query_spec(item, f"{where} templates.{key} item {n}")
        templates[key] = list(items)

    auto = {"brand": True, "competitors": True, "topics": True}
    given = data.get("auto") or {}
    _only_keys(given, auto, f"{where} auto")
    for key, value in given.items():
        if not isinstance(value, bool):
            raise ResearchError(f"{where} auto.{key} must be true or false")
        auto[key] = value
    return {"competitors": competitors, "topics": [t.strip() for t in topics],
            "queries": explicit, "templates": templates, "auto": auto, "collect": collect,
            "country": _country(data.get("country"), f"{where} country")}


def plan_queries(brand, config, settings):
    """Every query to run for a brand, in a stable order, without duplicates."""
    out, seen = [], set()
    country = config.get("country") or settings.country

    def add(text, kind, recency, tag, depth):
        text = " ".join(text.split())
        key = (_norm(text), kind, recency)
        if text and key not in seen:
            seen.add(key)
            out.append(Query(brand=brand.slug, text=text, kind=kind,
                             recency=recency or settings.recency, tag=tag,
                             country=country, depth=depth))

    def expand(items, tag, **names):
        for item in items:
            text, kind, recency, depth = _query_spec(item, f"{brand.slug} template")
            try:
                add(text.format(**names), kind, recency, tag, depth)
            except (KeyError, IndexError, ValueError) as exc:
                raise ResearchError(f"{brand.slug}: template {text!r} uses an unknown or broken "
                                    f"placeholder ({exc})") from exc

    for item in config["queries"]:
        text, kind, recency, depth = _query_spec(item, f"{brand.slug} query")
        add(text, kind, recency, "config", depth)
    if config["auto"]["brand"]:
        expand(config["templates"]["brand"], "brand", brand=brand.name)
    if config["auto"]["competitors"]:
        for comp in config["competitors"]:
            expand(config["templates"]["competitor"], f"competitor:{comp['name']}",
                   brand=brand.name, name=comp["name"], domain=comp["domain"])
    if config["auto"]["topics"]:
        for topic in config["topics"]:
            expand(config["templates"]["topic"], f"topic:{topic}", brand=brand.name, topic=topic)
    return out


# ---------------------------------------------------------------------------------------
# credits
# ---------------------------------------------------------------------------------------

class Ledger:
    """Credits spent, per provider, this month and over the key's lifetime."""

    def __init__(self, usage, month):
        self.usage = usage
        self.month = month
        self.usage.setdefault("months", {})
        self.usage.setdefault("lifetime", {})

    def month_used(self, name):
        return self.usage["months"].get(self.month, {}).get(name, 0)

    def life_used(self, name):
        return self.usage["lifetime"].get(name, 0)

    def charge(self, name, credits):
        month = self.usage["months"].setdefault(self.month, {})
        month[name] = round(month.get(name, 0) + credits, 4)
        self.usage["lifetime"][name] = round(self.usage["lifetime"].get(name, 0) + credits, 4)


def _allowance(name, caps, ledger, reported):
    cap = caps[name]
    room = cap.monthly_cap - ledger.month_used(name)
    if cap.lifetime_cap is not None:
        room = min(room, cap.lifetime_cap - ledger.life_used(name))
    if reported.get(name) is not None:
        room = min(room, reported[name] - cap.keep_free)
    return room


# ---------------------------------------------------------------------------------------
# remembering what was searched
# ---------------------------------------------------------------------------------------

def cache_key(q):
    raw = "|".join([q.kind, _norm(q.text), q.country or "", q.recency or "", q.depth])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse(ts):
    return datetime.strptime(ts, _TS)


def _find_cached(records, key, now, days, provider=None):
    if days <= 0:
        return None
    newest = None
    for rec in records:
        if rec.get("key") == key and rec.get("provider") and provider in (None, rec["provider"]):
            newest = rec
    if newest and _parse(now) - _parse(newest["at"]) < timedelta(days=days):
        return newest
    return None


# ---------------------------------------------------------------------------------------
# a run
# ---------------------------------------------------------------------------------------

@dataclass
class BrandReport:
    slug: str
    name: str
    planned: int = 0
    cached: list = field(default_factory=list)
    searched: list = field(default_factory=list)       # (Query, provider, credits, result count)
    deferred: list = field(default_factory=list)
    skipped: list = field(default_factory=list)        # (Query, reason)
    failed: list = field(default_factory=list)         # (Query, reason)


@dataclass
class ResearchResult:
    run_id: str
    now: str
    dry_run: bool
    enabled: list
    brands: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    notes_with_problems: list = field(default_factory=list)   # providers whose key was rejected
    reported: dict = field(default_factory=dict)
    ledger: object = None
    settings: object = None
    compare: list = field(default_factory=list)      # one entry per search run on several services

    @property
    def problems(self):
        return bool(self.notes_with_problems or any(b.failed for b in self.brands.values()))


def _urls(results):
    return [canonical_url(r["url"]) for r in results if r.get("url")]


def run_research(brands, store, *, providers, settings, now, configs=None, dry_run=False,
                 max_queries=None, root="brands", compare=False, compare_limit=None):
    """Plan, route and run searches for `brands`. `providers` maps name -> Provider."""
    result = ResearchResult(run_id=secrets.token_hex(4), now=now, dry_run=dry_run,
                            enabled=sorted(providers), settings=settings)
    usage = store.load_usage()
    ledger = Ledger(copy.deepcopy(usage) if dry_run else usage, now[:7])
    result.ledger = ledger
    limit = max_queries or settings.max_queries_per_run
    compare_cap = (compare_limit or settings.compare_queries) if compare and len(providers) > 1 else 0
    compared = 0
    disabled, exhausted = {}, set()

    reported = {}
    for name, provider in providers.items():
        try:
            reported[name] = provider.remaining()
        except AuthError as exc:
            disabled[name] = str(exc)
        except ProviderError as exc:
            result.notes.append(f"could not read {name}'s own balance ({exc}); using the local count")
            reported[name] = None
    for name, why in disabled.items():
        result.notes.append(f"{name} switched off for this run: {why}")
        result.notes_with_problems.append(name)

    attempted = 0
    for brand in brands:
        report = BrandReport(slug=brand.slug, name=brand.name)
        result.brands[brand.slug] = report
        config = (configs or {}).get(brand.slug) or load_brand_research(brand, root)
        plan = plan_queries(brand, config, settings)
        report.planned = len(plan)
        past = store.read_searches(brand.slug)
        spent = {}

        for q in plan:
            key = cache_key(q)
            if compared < compare_cap and attempted < limit:
                outcome = _compare_one(q, key, providers, disabled, exhausted, settings, ledger,
                                       reported, past, now, store, brand, result, dry_run, spent)
                if outcome == "reused":
                    report.cached.append(q)
                    continue
                if outcome:
                    compared += 1
                    attempted += 1
                    report.searched.append((q, "+".join(outcome["used"]) or "(all reused)",
                                            outcome["credits"], None))
                    continue
            if _find_cached(past, key, now, settings.cache_days):
                report.cached.append(q)
                continue
            if attempted >= limit:
                report.deferred.append(q)
                continue
            attempted += 1
            reasons, chosen = [], None
            for name in settings.routing[q.kind]:
                if name not in providers or name in disabled or name in exhausted:
                    continue
                provider = providers[name]
                cost = provider.cost(q.kind, q.depth)
                if _allowance(name, settings.caps, ledger, reported) < cost:
                    reasons.append(("budget", name, "no credits left under the cap"))
                    continue
                if dry_run:
                    results = None
                else:
                    try:
                        results = provider.search(q.text, kind=q.kind, num=settings.results_per_query,
                                                  country=q.country, recency=q.recency,
                                                  depth=q.depth)
                    except AuthError as exc:
                        disabled[name] = str(exc)
                        result.notes.append(f"{name} switched off for this run: {exc}")
                        result.notes_with_problems.append(name)
                        continue
                    except QuotaError as exc:
                        exhausted.add(name)
                        result.notes.append(f"{name} is out of credits: {exc}")
                        continue
                    except ProviderError as exc:
                        reasons.append(("error", name, str(exc)))
                        continue
                ledger.charge(name, cost)
                if reported.get(name) is not None:
                    reported[name] -= cost
                chosen = (name, cost, results)
                break
            if not chosen:
                errors = [f"{n}: {m}" for kind, n, m in reasons if kind == "error"]
                if errors:
                    report.failed.append((q, "; ".join(errors)))
                elif reasons:
                    report.skipped.append((q, "out of credits under your caps"))
                else:
                    report.skipped.append((q, "no provider available for this kind of search"))
                continue
            name, cost, results = chosen
            spent[name] = spent.get(name, 0) + cost
            report.searched.append((q, name, cost, None if results is None else len(results)))
            if not dry_run:
                store.append_searches(brand.slug, [{
                    "at": now, "run": result.run_id, "key": key, "query": q.text, "kind": q.kind,
                    "recency": q.recency, "country": q.country, "depth": q.depth, "tag": q.tag,
                    "provider": name, "credits": cost,
                    "results": [r.to_dict() for r in results]}])
                store.save_usage(ledger.usage)
        if not dry_run:
            store.append_research_run(brand.slug, {
                "at": now, "run": result.run_id, "planned": report.planned,
                "cached": len(report.cached), "searched": len(report.searched),
                "deferred": len(report.deferred), "skipped": len(report.skipped),
                "failed": len(report.failed), "credits": spent})
    result.reported = {k: v for k, v in reported.items() if v is not None}   # after this run
    return result


def _compare_one(q, key, providers, disabled, exhausted, settings, ledger, reported, past, now,
                 store, brand, result, dry_run, spent):
    """Run one search on every usable provider so their answers can be compared.

    Returns "reused" when every provider already has a fresh answer and nothing needs paying
    for, a dict describing what happened, or None when no provider could be used at all.
    """
    answers, used, credits, failures = {}, [], 0, {}
    for name in sorted(providers):
        if name in disabled or name in exhausted:
            continue
        hit = _find_cached(past, key, now, settings.cache_days, provider=name)
        if hit:
            answers[name] = _urls(hit["results"])
            continue
        provider = providers[name]
        cost = provider.cost(q.kind, q.depth)
        if _allowance(name, settings.caps, ledger, reported) < cost:
            failures[name] = "no credits left under the cap"
            continue
        if dry_run:
            results = None
        else:
            try:
                results = provider.search(q.text, kind=q.kind, num=settings.results_per_query,
                                          country=q.country, recency=q.recency, depth=q.depth)
            except AuthError as exc:
                disabled[name] = str(exc)
                result.notes.append(f"{name} switched off for this run: {exc}")
                result.notes_with_problems.append(name)
                continue
            except QuotaError as exc:
                exhausted.add(name)
                result.notes.append(f"{name} is out of credits: {exc}")
                continue
            except ProviderError as exc:
                failures[name] = str(exc)
                continue
        ledger.charge(name, cost)
        if reported.get(name) is not None:
            reported[name] -= cost
        spent[name] = spent.get(name, 0) + cost
        used.append(name)
        credits += cost
        answers[name] = None if results is None else _urls([r.to_dict() for r in results])
        if not dry_run:
            store.append_searches(brand.slug, [{
                "at": now, "run": result.run_id, "key": key, "query": q.text, "kind": q.kind,
                "recency": q.recency, "country": q.country, "depth": q.depth, "tag": q.tag,
                "provider": name, "credits": cost, "mode": "compare",
                "results": [r.to_dict() for r in results]}])
            store.save_usage(ledger.usage)
    if not used and answers and not failures:
        result.compare.append({"brand": brand.slug, "query": q.text, "kind": q.kind,
                               "answers": answers, "failures": failures, "reused": True})
        return "reused"
    if not answers and not failures:
        return None
    result.compare.append({"brand": brand.slug, "query": q.text, "kind": q.kind,
                           "answers": answers, "failures": failures, "reused": False})
    return {"used": used, "credits": credits}


def compare_summary(entries):
    """Overlap between providers across every compared search (needs real answers)."""
    names = sorted({n for e in entries for n, urls in e["answers"].items() if urls is not None})
    totals = {n: {"searches": 0, "results": 0, "only_here": 0} for n in names}
    pairs = {}
    for e in entries:
        live = {n: set(u) for n, u in e["answers"].items() if u is not None}
        for n, urls in live.items():
            totals[n]["searches"] += 1
            totals[n]["results"] += len(urls)
            others = set().union(*[v for k, v in live.items() if k != n]) if len(live) > 1 else set()
            totals[n]["only_here"] += len(urls - others)
        for a in sorted(live):
            for b in sorted(live):
                if a < b:
                    slot = pairs.setdefault((a, b), {"shared": 0, "union": 0})
                    slot["shared"] += len(live[a] & live[b])
                    slot["union"] += len(live[a] | live[b])
    return totals, pairs


def format_compare_report(result):
    entries = result.compare
    if not entries:
        return ""
    lines = ["", "Comparison of services (each search was run on every ticked service):"]
    for e in entries:
        lines.append(f'  [{e["kind"]}] "{e["query"]}"' + ("  (answers reused from the last run)" if e["reused"] else ""))
        live = {n: u for n, u in e["answers"].items() if u is not None}
        for name in sorted(e["answers"]):
            urls = e["answers"][name]
            others = set().union(*[set(v) for k, v in live.items() if k != name]) if len(live) > 1 else set()
            if urls is None:
                lines.append(f"      {name}: would be searched")
            else:
                lines.append(f"      {name}: {len(urls)} results, {len(set(urls) - others)} only this service found")
        for name, why in sorted(e["failures"].items()):
            lines.append(f"      {name}: not used ({why})")
        if len(live) > 1:
            shared = set.intersection(*[set(v) for v in live.values()])
            lines.append(f"      found by every service: {len(shared)}")
    totals, pairs = compare_summary(entries)
    if totals:
        lines.append("  Overall:")
        for name, t in sorted(totals.items()):
            lines.append(f"    {name}: {t['results']} results over {t['searches']} searches, {t['only_here']} found by no other service")
        for (a, b), p in sorted(pairs.items()):
            pct = round(100 * p["shared"] / p["union"]) if p["union"] else 0
            lines.append(f"    {a} vs {b}: {p['shared']} pages in common ({pct}% of everything either found)")
    return "\n".join(lines)


CHECK_QUERY = "sweatpants overalls"
CHECK_NEWS_QUERY = "overalls"      # broad, so an empty answer means something is wrong
CHECK_NEWS_RECENCY = "year"


def check_providers(providers, store, settings, now):
    """Prove each key and each kind of search works: one web and one news search per provider.

    Costs the provider's normal price for each (1 credit, usually). The credits are counted in
    the shared ledger; the results are shown but not saved as findings.
    """
    ledger = Ledger(store.load_usage(), now[:7])
    rows = []
    for name in sorted(providers):
        provider = providers[name]
        dead = None
        for kind in KINDS:
            row = {"provider": name, "kind": kind, "ok": False, "credits": 0, "count": 0,
                   "sample": "", "problem": "", "note": ""}
            rows.append(row)
            if dead:
                row["problem"] = dead
                continue
            cost = provider.cost(kind)
            if _allowance(name, settings.caps, ledger, {}) < cost:
                row["problem"] = "skipped: no credits left under your caps"
                continue
            try:
                results = provider.search(
                    CHECK_NEWS_QUERY if kind == "news" else CHECK_QUERY, kind=kind, num=3,
                    country=settings.country,
                    recency=CHECK_NEWS_RECENCY if kind == "news" else None)
            except AuthError as exc:
                dead = row["problem"] = str(exc)
                continue
            except QuotaError as exc:
                dead = row["problem"] = str(exc)
                continue
            except ProviderError as exc:
                row["problem"] = str(exc)
                continue
            ledger.charge(name, cost)
            store.save_usage(ledger.usage)
            row.update(ok=True, credits=cost, count=len(results), note=provider.last_note,
                       sample=results[0].title if results else "")
    return rows


def format_check_report(rows):
    lines = ["Provider check: one web search and one news search per service"]
    for r in rows:
        if r["ok"]:
            note = f"OK, {r['count']} results ({r['credits']:g} credit{'s' if r['credits'] != 1 else ''})"
            if r["count"] == 0:
                note = f"answered but returned no results ({r['credits']:g} credit)"
                if r["note"]:
                    note += f" [{r['note']}]"
            lines.append(f"  {r['provider']:<11} {r['kind']:<5} {note}"
                         + (f": {r['sample'][:60]}" if r["sample"] else ""))
        else:
            lines.append(f"  {r['provider']:<11} {r['kind']:<5} FAILED: {r['problem']}")
    return "\n".join(lines)


def format_research_report(result):
    s = result.settings
    lines = [f"Research run {result.now}" + ("  [dry run: nothing was searched or saved]" if result.dry_run else ""),
             "Providers in use: " + (", ".join(result.enabled) or "none (no API keys found)")]
    for slug, rep in result.brands.items():
        lines.append("")
        lines.append(f"=== {slug} ===")
        lines.append(f"{rep.planned} planned: {len(rep.cached)} reused from the last "
                     f"{s.cache_days} days, {len(rep.searched)} "
                     f"{'would be searched' if result.dry_run else 'searched'}, "
                     f"{len(rep.deferred)} left for the next run, {len(rep.skipped)} skipped, "
                     f"{len(rep.failed)} failed")
        for q, name, cost, count in rep.searched:
            tail = "" if count is None else f", {count} results"
            lines.append(f"  [{q.kind}] \"{q.text}\" -> {name} ({cost:g} credit{'s' if cost != 1 else ''}{tail})")
        for q, reason in rep.skipped:
            lines.append(f"  skipped [{q.kind}] \"{q.text}\": {reason}")
        for q, reason in rep.failed:
            lines.append(f"  FAILED [{q.kind}] \"{q.text}\": {reason}")
        if rep.deferred:
            lines.append(f"  left for next run: {len(rep.deferred)} (raise max_queries_per_run to do more at once)")
    lines.append("")
    lines.append(f"Credits used this month ({result.ledger.month}), against your caps:")
    for name in sorted(s.caps):
        cap = s.caps[name]
        life = "" if cap.lifetime_cap is None else f"; lifetime {result.ledger.life_used(name):g}/{cap.lifetime_cap:g}"
        lines.append(f"  {name}: {result.ledger.month_used(name):g}/{cap.monthly_cap:g}{life}")
    if result.reported:
        lines.append("Provider-reported credits left: " + ", ".join(
            f"{k} {v:g}" for k, v in sorted(result.reported.items())))
    lines.extend(f"note: {n}" for n in result.notes)
    comparison = format_compare_report(result)
    return "\n".join(lines) + (("\n" + comparison) if comparison else "")


# ---------------------------------------------------------------------------------------
# sources: what the sheet shows (one row per distinct page)
# ---------------------------------------------------------------------------------------

def _is_tracking(key):
    key = key.lower()
    return key in _TRACKING_EXACT or key.startswith(_TRACKING_PREFIXES)


def clean_url(url):
    """The page's address without tracking codes or a #fragment (what the sheet shows)."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _is_tracking(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def canonical_url(url):
    parts = urlsplit(clean_url(url))
    query = sorted(parse_qsl(parts.query, keep_blank_values=True))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def source_id(url):
    return "s_" + hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:12]


def research_sources(records):
    """One row per distinct page found across all searches, oldest search first."""
    rows = {}
    for rec in records:
        if not rec.get("provider"):
            continue
        counted = set()
        for item in rec.get("results", []):
            if not item.get("url"):
                continue
            sid = source_id(item["url"])
            row = rows.setdefault(sid, {
                "source_id": sid, "title": "", "url": clean_url(item["url"]), "domain": "", "published": "",
                "first_seen": rec["at"], "last_seen": rec["at"], "times_seen": 0,
                "providers": set(), "purposes": set(), "queries": [], "best_rank": item["rank"],
                "snippet": ""})
            row["last_seen"] = max(row["last_seen"], rec["at"])
            row["first_seen"] = min(row["first_seen"], rec["at"])
            row["title"] = item.get("title") or row["title"]
            row["snippet"] = (item.get("snippet") or row["snippet"])[:300]
            row["published"] = item.get("date") or row["published"]
            row["best_rank"] = min(row["best_rank"], item["rank"])
            row["providers"].add(rec["provider"])
            row["purposes"].add(rec.get("tag") or "")
            if rec["query"] not in row["queries"]:
                row["queries"].append(rec["query"])
            if sid not in counted:
                row["times_seen"] += 1
                counted.add(sid)
    out = {}
    for sid, row in rows.items():
        host = urlsplit(row["url"]).netloc.lower()
        out[sid] = {**row, "domain": host[4:] if host.startswith("www.") else host,
                    "providers": "; ".join(sorted(row["providers"])),
                    "purposes": "; ".join(sorted(p for p in row["purposes"] if p)),
                    "queries": " | ".join(row["queries"][:5])}
    return out
