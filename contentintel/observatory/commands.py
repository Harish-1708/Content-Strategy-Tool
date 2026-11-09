"""The `observatory` command: plan, monitor, report, history, prune, status.

Loaded only when the command is used (see cli.py), so a missing or damaged observatory folder can never stop the rest of the tool.
"""
import json
import os
from pathlib import Path
import re

from ..brands import BrandError, list_brand_slugs, load_brand
from ..contract import Failure
from . import config as OC
from . import corpus as corpus_mod
from .. import services as SV
from . import discovery, monitor, report
from . import enrich as enrich_mod
from . import judge as judge_mod
from . import polite
from . import readers as readers_mod
from .pauses import PauseLedger
from .snapshots import SnapshotError, SnapshotStore

from .. import agent_reach_catalog as CAT

ACTIONS = ("plan", "discover", "decide", "judge", "monitor", "corpus", "instagram", "reddit", "agentreach", "reliability", "controls", "report", "history", "prune", "status")
JUDGE_ROUTER = None               # tests replace this: () -> (router, usage, review, side); None means the real AI layer
HOMEPAGE_SOCIAL = None            # tests replace this: (domain) -> {instagram, tiktok, youtube}; None means read the site
REDDIT_HTTP = None                # tests replace this: the HTTP function the Reddit API route uses; None means the real one
OPENCLI_RUN = None                # tests replace these; None means the real OpenCLI program
YOUTUBE_GET = None                # tests replace these four; None means the real services
READER_HTTP = None
PROVIDERS_FACTORY = None          # tests replace these two; None means the real search services and the real polite client
CLIENT_FACTORY = None


def add_arguments(p):
    p.add_argument("action", choices=ACTIONS)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--brand", default="")
    group.add_argument("--all", action="store_true", help="every brand")
    p.add_argument("--settings", default=OC.DEFAULT_PATH, help="observatory.yaml")
    p.add_argument("--only", default="", help="monitor only these parts: pages, feeds, sitemap, shopify (comma-separated)")
    p.add_argument("--competitor", default="", help="only these competitor ids (comma-separated)")
    p.add_argument("--workers", type=int, default=3, help="how many sites are read at the same time (each site is still read one request at a time)")
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--site", default="", help="history: the competitor id")
    p.add_argument("--kind", default="catalogue", help="history: catalogue, sitemap, or page-/feed- followed by the address hash")
    p.add_argument("--product", default="", help="history: a product id or handle")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="monitor: show what would be read and make no request")
    p.add_argument("--decision", default="", choices=("", "monitor", "stop", "reset"), help="decide: monitor or stop a discovered competitor, or reset it to the rubric")
    p.add_argument("--what", default="youtube", choices=("youtube", "outliers", "search", "stats", "read", "trending", "collect", "compare", "check", "run", "list", "approve", "reject", "show", "set", "reset", "subtitle", "deepread", "channelpages", "crawl", "feeds", "profile", "posts", "tweets", "thread", "jobs", "subreddit"),
                   help="corpus: youtube, outliers, trending, search, stats or read; instagram: collect, compare or check")
    p.add_argument("--source", default="", help="corpus search: only these sources (youtube_public, meta_ads, instagram, competitor_pages, web_search...), comma-separated")
    p.add_argument("--since", default="", help="corpus search: only items captured on or after this date (YYYY-MM-DD)")
    p.add_argument("--handle", default="", help="instagram: extra accounts to read, comma-separated")
    p.add_argument("--query", default="", help="corpus search: the words to look for; reddit: extra search words, comma-separated")
    p.add_argument("--url", default="", help="corpus read: a public page to read through the reader")
    p.add_argument("--channel", default="", help="corpus youtube: extra channels (handles like @name or ids), comma-separated")
    p.add_argument("--find-channels", action="store_true", help="corpus youtube: also search for channels using the brand's youtube.queries (each search costs 100 of the free 10,000 daily units)")
    p.add_argument("--route", default="all", choices=("all", "api", "agent_reach"), help="reddit: which route to use (default: every route that can run)")
    p.add_argument("--subreddit", default="", help="reddit: extra communities, comma-separated")
    p.add_argument("--platform", default="instagram", help="reliability: instagram, tiktok or reddit")
    p.add_argument("--engine", default="", choices=("", "jina", "core", "scrapling", "auto"), help="corpus read, deepread, channelpages: the page reader (blank: the owner's choice in the Controls)")
    p.add_argument("--top", type=int, default=5, help="corpus deepread: how many search-result pages to read")
    p.add_argument("--ar-channel", default="youtube", choices=CAT.integrated(), help="agentreach: which Agent-Reach channel (the catalogue lists each channel's actions and what it needs)")
    p.add_argument("--max-pages", type=int, default=0, help="corpus crawl: how many pages at most (blank: the crawl.max_pages setting)")
    p.add_argument("--depth", type=int, default=-1, help="corpus crawl: how many links deep at most (blank: the crawl.depth setting)")
    p.add_argument("--sitemap", action="store_true", help="corpus crawl: also start from the site's sitemap (robots.txt's Sitemap lines and /sitemap.xml)")
    p.add_argument("--resume", action="store_true", help="corpus crawl: carry on a crawl of this address that stopped early (page limit or a refusal), without reading anything twice")
    p.add_argument("--extract", action="append", default=[], help="corpus crawl: pull a value out of every page, as name=kind:value where kind is css, xpath, text, regex or similar (Scrapling's parser tools), repeatable")
    p.add_argument("--param", action="append", default=[], help="agentreach: an extra input as name=value (for example location=Austin for LinkedIn jobs, city=Shanghai for Boss), repeatable")
    p.add_argument("--key", default="", help="controls set/reset: the control's key, as `observatory controls` lists it")
    p.add_argument("--value", default="", help="controls set: the new value (on, off, a number, text or a listed choice)")
    p.add_argument("--no-enrich", action="store_true", help="discover: do not look for competitors' websites and accounts in several sources")
    p.add_argument("--no-judge", action="store_true", help="discover: do not ask the AI for a second opinion on each candidate")
    p.add_argument("--review-id", default="", help="judge approve/reject: the review item (the candidate key works too)")
    p.add_argument("--providers-config", default="ai_providers.yaml")
    p.add_argument("--lanes-config", default="ai_lanes.yaml")
    p.add_argument("--gateway-dir", default="gateway-src", help="the built, pinned gateway (judge only)")
    p.add_argument("--workdir", default="ai-work", help="scratch folder for the gateway (judge only)")
    p.add_argument("--no-lookup", action="store_true", help="discover: do not search for the websites of candidates that have none")
    p.add_argument("--no-probe", action="store_true", help="discover: do not check whether candidate sites are Shopify stores")
    p.add_argument("--research-config", default="research.yaml")


def _brands(args):
    slugs = list_brand_slugs(args.brands_dir) if args.all or not args.brand else [args.brand]
    return [load_brand(s, args.brands_dir) for s in slugs]


def _plan(args, store, settings, brands):
    discovered = {b.slug: report.discovered_competitors(store, b.slug) for b in brands}
    sites = monitor.plan_sites(brands, settings, root=args.brands_dir, data_dir=args.data_dir, discovered=discovered)
    snaps = SnapshotStore(args.data_dir)
    for site_id, slot in sites.items():                          # accounts found on the competitor's own pages fill in what the files and discovery left empty
        for key, value in snaps.read_social(site_id).items():
            if not slot["comp"].get(key):
                slot["comp"][key] = value
    for slot in sites.values():                                  # the central settings and any matching override, then the competitor's own settings on top
        try:
            slot["comp"]["advanced"] = SV.resolve_scrapling(settings, slot["comp"]["domain"], "competitors", slot["comp"].get("advanced"))
        except Failure:
            pass                                                 # Scrapling is switched off for competitor pages: the built-in reader is used, and the run says so
    return sites


def run(args, store, ledger, now):
    try:
        settings = SV.load_settings(args.settings, args.data_dir)
    except OC.ObservatoryConfigError as exc:
        print(f"Error: {exc}")
        return 1
    if os.environ.get("OBSERVATORY_ENGINE_PAGES") in OC.ENGINES:          # the workflow's "scrapling" box
        settings["engine"]["pages"] = os.environ["OBSERVATORY_ENGINE_PAGES"]
    snaps = SnapshotStore(args.data_dir)
    try:
        brands = _brands(args) if args.action != "history" else []
        if args.action == "plan":
            return _show_plan(args, store, settings, brands)
        if args.action == "discover":
            return _discover(args, store, ledger, settings, brands, now)
        if args.action == "corpus":
            return _corpus(args, store, ledger, settings, brands, snaps, now)
        if args.action == "decide":
            return _decide(args, store, brands, now)
        if args.action == "judge":
            return _judge_command(args, store, ledger, settings, brands, now)
        if args.action == "instagram":
            return _instagram(args, store, ledger, settings, brands, now)
        if args.action == "reddit":
            return _reddit(args, store, ledger, settings, brands, now)
        if args.action == "reliability":
            return _reliability(args, store, ledger, brands, now)
        if args.action == "agentreach":
            return _agentreach(args, store, ledger, settings, brands, now)
        if args.action == "controls":
            return _controls(args, store, settings, brands, now)
        if args.action == "monitor":
            return _monitor(args, store, ledger, settings, brands, snaps, now)
        if args.action == "report":
            return _report(args, store, settings, brands, snaps, now)
        if args.action == "history":
            return _history(args, snaps)
        if args.action == "prune":
            return _prune(settings, snaps, now)
        return _status(args, store, settings, brands, snaps, now)
    except (BrandError, SnapshotError, Failure) as exc:
        print(f"Error: {exc}")
        return 1


def _show_plan(args, store, settings, brands):
    sites = _plan(args, store, settings, brands)
    pol = settings["politeness"]
    contact = bool((os.environ.get("OBSERVATORY_CONTACT") or pol["contact"]).strip())
    print(f"Observatory plan: {len(sites)} site(s) for {len(brands)} brand(s). No request is made.")
    print(f"  identity: KelsonResearchBot/1.0 with a contact address ({'set' if contact else 'MISSING: set the OBSERVATORY_CONTACT secret'})")
    print(f"  pace: at least {pol['min_delay_seconds']}s between requests to one site, doubling after a block up to {pol['max_delay_seconds']}s; robots.txt always obeyed;"
          f" at most {pol['max_requests_per_domain']} requests per site per run")
    for sid, slot in sorted(sites.items()):
        comp = slot["comp"]
        parts = [name for name in monitor.PARTS if comp["monitor"].get(name, True) is not False]
        print(f"  {sid:28} {comp['domain']:28} brands: {', '.join(slot['brands'])}; parts: {', '.join(parts)}; pages: {len(comp['pages']) + 1}; origin: {comp['origin']}")
    if not sites:
        print("  Nothing is watched yet. Add competitors to brands/<brand>/observatory.yaml (or to research.yaml with a domain), or run: observatory discover.")
    return 0


def _monitor(args, store, ledger, settings, brands, snaps, now):
    sites = _plan(args, store, settings, brands)
    ids = {s.strip() for s in args.competitor.split(",") if s.strip()}
    only = [p.strip() for p in args.only.split(",") if p.strip()] or None
    bad = [p for p in (only or []) if p not in monitor.PARTS]
    if bad:
        print(f"Error: unknown part {bad[0]!r}; choose from {', '.join(monitor.PARTS)}")
        return 1
    contact = (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()
    if args.dry_run or not sites:
        return _show_plan(args, store, settings, brands)
    if not contact:
        print("Error: no contact address. Set the OBSERVATORY_CONTACT secret (an email address or web page a site owner can write to); the tool identifies itself honestly and will not read without it.")
        return 1
    pauses = PauseLedger(args.data_dir)

    def make():
        return CLIENT_FACTORY() if CLIENT_FACTORY else polite.PoliteClient(settings, contact=contact, ledger=pauses, now_fn=lambda: now)
    page_reader = None
    wish = SV.effective_engine(settings, "competitors", scrapling_installed=True)                 # the owner's page-reader route for competitors (or the older engine.pages switch)
    if wish == "auto":
        wish = "scrapling" if os.environ.get("SCRAPLING_PYTHON") else "core"
    if wish == "jina":
        print("  note: pages are read by the built-in reader: monitoring keeps structured pages, which Jina Reader (text only) does not give")
        wish = "core"
    if wish == "scrapling" and not SV.scrapling_allowed(settings, "competitors")[0]:
        print(f"  note: pages are read by the built-in reader because {SV.scrapling_allowed(settings, 'competitors')[1]}")
    elif wish == "scrapling":
        from . import worker_client as wc
        page_reader = lambda comp, urls: wc.WorkerPages(comp, urls, settings, contact, os.environ.get("SCRAPLING_PYTHON", ""), snaps, pauses=pauses, now=now)   # noqa: E731
    results = monitor.run_sites(sites, make_client=make, snaps=snaps, settings=settings, hl=ledger, now=now, only=only, workers=max(1, args.workers), ids=ids or None,
                                page_reader=page_reader)
    pauses.save(now)
    for b in brands:
        mine = [slot["comp"] for sid, slot in sorted(sites.items()) if b.slug in slot["brands"]]
        report.write_brand_sites(store, b.slug, [{"id": c["id"], "name": c["name"], "domain": c["domain"], "origin": c["origin"], "kind": c["kind"],
                                                       "request_controls": report.controls_summary(c.get("advanced") or {})} for c in mine], now)
        text = report.build_report(b.name, [c for c in mine], snaps, now, days=args.since_days)
        store.write_export(b.slug, "observatory-report.md", text)
    failed = 0
    for r in results:
        parts = [(name, p) for name, plist in r["parts"].items() for p in plist]
        bad_parts = [(n, p) for n, p in parts if not p["ok"]]
        events = [e for e in r["events"] if e["type"] != "baseline_recorded"]
        print(f"{r['domain']}: {len(parts) - len(bad_parts)} part(s) read, {len(bad_parts)} failed, {len(events)} change(s)" + (f", stopped: {r['stopped']}" if r["stopped"] else ""))
        for name, p in bad_parts:
            print(f"  ::warning::{r['domain']} {name}: {p['code']}: {p['message']}")
        for e in [e for e in events if e["severity"] == "major"][:5]:
            print(f"  [major] {e['summary']}")
        failed += 1 if (r["stopped"] or (bad_parts and len(bad_parts) == len(parts))) else 0
    print(f"Observatory run: {len(results)} site(s), {sum(len(r['events']) for r in results)} event(s), {failed} site(s) with problems.")
    if settings["retention"]["auto_prune"]:                              # old readings are thinned after every run, so the stored history cannot grow without limit
        _prune(settings, snaps, now, quiet=True)
    return 1 if results and failed == len(results) else 0


def _lookup_domains(args, store, ledger, settings, brand, names, now):
    """{key: (domain, title)} for the names a search finds a matching website for. Uses the research search services within their caps."""
    from .. import providers as providers_mod
    from .. import research
    providers = {k: v for k, v in SV.search_providers(settings, "discovery", os.environ, build=lambda env: PROVIDERS_FACTORY() if PROVIDERS_FACTORY else providers_mod.build_providers(env)).items()
                 if not k.startswith("agent_reach")}                  # Reddit and YouTube searches do not find a company's website
    if not settings["readers"]["exa"]:                                    # readers.exa: false keeps Exa out of the website lookups
        providers = {k: v for k, v in providers.items() if not k.startswith("exa")}
    if not providers:
        return {}, "no search service key is set, so websites were not looked up"
    cfg = {"competitors": [], "topics": [], "queries": [{"q": f"{name} official website", "kind": "web"} for _, name in names], "auto": {"brand": False, "competitors": False, "topics": False},
           "templates": dict(research.DEFAULT_TEMPLATES), "collect": None, "country": None}
    rsettings = research.load_settings(args.research_config)
    research.run_research([brand], store, providers=providers, settings=rsettings, now=now, configs={brand.slug: cfg}, health=ledger, max_queries=len(names))
    wanted = {f"{name} official website": (key, name) for key, name in names}
    found = {}
    for rec in store.read_searches(brand.slug):
        hit = wanted.get(rec.get("query", ""))
        if hit and hit[0] not in found:
            pick = discovery.pick_domain(hit[1], rec.get("results", []), discovery.own_domains(brand))
            if pick:
                found[hit[0]] = pick
    return found, ""


def _discover(args, store, ledger, settings, brands, now):
    if not settings["discovery"]["enabled"]:
        print("Competitor discovery is switched off (discovery.enabled in observatory.yaml). Nothing was looked up or changed.")
        return 0
    for brand in brands:
        book = report.read_discovery(store, brand.slug)
        entities = discovery.extract_entities(store, brand, now)
        floor = settings["discovery"]["min_appearances"]                  # an advertiser seen fewer times than this is not yet a candidate
        entities = {k: e for k, e in entities.items() if e["signals"]["ads"] + e["signals"]["posts"] + e["signals"]["kept_items"] >= floor}
        keywords = discovery.brand_keywords(store, brand)
        scores = {k: discovery.score_entity(e, keywords) for k, e in entities.items()}
        print(f"=== {brand.name}: {len(entities)} possible competitor(s) found in what has been collected ===")
        notes = []
        names = [] if args.no_lookup else [(k, n) for k, n in discovery.needs_lookup(book, entities, settings, 10 ** 6)
                                           if scores[k][0] >= settings["discovery"]["min_score"]][:settings["discovery"]["lookup_max"]]
        if names:
            found, why = _lookup_domains(args, store, ledger, settings, brand, names, now)
            if why:
                notes.append(why)
            for key, name in names:
                if key in found:
                    entities[key]["domain"] = found[key][0]
                    notes.append(f"{name}: website found, {found[key][0]}")
                else:
                    known = {c["key"]: c for c in book["candidates"]}.get(key)
                    if known is not None:
                        known["lookup_failed_at"] = now
                    entities[key]["lookup_failed"] = True
        for key, name in names:
            if key in found_domains(entities, key):
                entities[key]["domain_confidence"] = 0.6
                entities[key]["domain_evidence"] = [{"source": "search", "weight": 0.6, "detail": f"a web search for \"{name} official website\" found {entities[key]['domain']}"}]
        if settings["discovery"]["enrich"] and not args.no_enrich:
            notes += _enrich_entities(args, store, ledger, settings, brand, entities, scores, book, now)
        probes = {}
        contact = (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()
        due = [c for c in sorted(entities.values(), key=lambda e: -scores[e["key"]][0]) if c["domain"] and scores[c["key"]][0] >= settings["discovery"]["min_score"]]
        due = [e for e in due if ({c["key"]: c for c in book["candidates"]}.get(e["key"]) or {}).get("shopify") is None][:settings["discovery"]["probe_max"]]
        if due and not args.no_probe:
            if not contact:
                notes.append("sites were not checked: set OBSERVATORY_CONTACT (the tool identifies itself honestly and will not read without it)")
            else:
                from . import shopify as S
                from .pauses import PauseLedger
                pauses = PauseLedger(args.data_dir)
                for e in due:
                    try:
                        client = CLIENT_FACTORY() if CLIENT_FACTORY else polite.PoliteClient(settings, contact=contact, ledger=pauses, now_fn=lambda: now)
                        probes[e["key"]] = S.is_store(client, S.base_url(e["domain"]))
                    except Failure as exc:
                        notes.append(f"{e['domain']} could not be checked ({exc.code})")
                pauses.save(now)
        for key, (is_store, why_store) in probes.items():
            if is_store and key in entities and entities[key].get("domain_evidence") is not None:      # a verified store raises confidence in the website
                ev = entities[key]["domain_evidence"] + [{"source": "store_check", "weight": 0.25, "detail": why_store}]
                entities[key]["domain_evidence"], entities[key]["domain_confidence"] = ev, enrich_mod.combine([e["weight"] for e in ev])
        judged = {}
        if settings["discovery"]["judge"] and not args.no_judge:
            judged, jnotes = _judge_entities(args, store, ledger, settings, brand, entities, scores, book, now)
            notes += jnotes
        elsewhere = len({c["id"] for c in report.read_brand_sites(store, brand.slug) if c.get("origin") != "discovery"})
        notes += discovery.merge(book, entities, scores, settings, now, probes=probes, judged=judged, monitored_elsewhere=elsewhere)
        for c in book["candidates"]:
            if c["key"] in entities and entities[c["key"]].get("lookup_failed") and not c["domain"]:
                c["lookup_failed_at"] = now
        report.write_discovery(store, brand.slug, book, now)
        shown = [c for c in book["candidates"] if c["status"] != "weak"]
        for c in shown[:15]:
            print(f"  {c['score']:3} {c['status']:10} {c['name'][:30]:30} {c['domain'] or '(needs a website)':28} " + "; ".join(c["reasons"][:2]))
        for n in notes:
            print(f"  note: {n}")
        print(f"  {len(shown)} candidate(s) kept, {sum(1 for c in book['candidates'] if c['status'] == 'monitoring')} being monitored, "
              f"{sum(1 for c in book['candidates'] if c['status'] == 'weak')} too weak to list.")
    return 0


def found_domains(entities, key):
    return [key] if entities[key].get("domain") and not entities[key].get("lookup_failed") else []


def _run_searches(args, store, ledger, settings, brand, queries, now):
    """{query: [{url, title}]} for web searches run through the research services within their caps, plus a note when nothing could search."""
    from .. import providers as providers_mod
    from .. import research
    providers = {k: v for k, v in SV.search_providers(settings, "social_discovery", os.environ, build=lambda env: PROVIDERS_FACTORY() if PROVIDERS_FACTORY else providers_mod.build_providers(env)).items()
                 if not k.startswith("agent_reach")}                  # nor an Instagram account
    if not settings["readers"]["exa"]:
        providers = {k: v for k, v in providers.items() if not k.startswith("exa")}
    if not providers:
        return {}, "no search service key is set, so web searches were not run"
    cfg = {"competitors": [], "topics": [], "queries": [{"q": q, "kind": "web"} for q in queries], "auto": {"brand": False, "competitors": False, "topics": False},
           "templates": dict(research.DEFAULT_TEMPLATES), "collect": None, "country": None}
    research.run_research([brand], store, providers=providers, settings=research.load_settings(args.research_config), now=now, configs={brand.slug: cfg}, health=ledger, max_queries=len(queries))
    wanted = set(queries)
    got = {}
    for rec in store.read_searches(brand.slug):
        if rec.get("query") in wanted and rec["query"] not in got:
            got[rec["query"]] = [{"url": r.get("url", ""), "title": r.get("title", "")} for r in rec.get("results", [])]
    return got, ""


def _page_engines(args, ledger, settings, now, contact, purpose, pauses):
    """{name: callable(url) -> page} for the three readers (built-in, Jina Reader, Scrapling). Each records its own route health when a ledger is given, and each raises the Failure it met, so the shared
    reader chain (services.read_page) can apply the owner's choice and its one fixed rule: nothing is tried after a refusal."""
    from .. import contract as K

    def run(route, action, fn):
        if ledger is None:
            return fn()
        env = K.run_action("competitor_pages", action, [(route, lambda: [fn()])], health=ledger, now=now)
        if not env["ok"]:
            raise K.Failure(env["error"]["code"], env["error"]["message"])
        return env["items"][0]

    def client():
        return CLIENT_FACTORY() if CLIENT_FACTORY else polite.PoliteClient(settings, contact=contact, ledger=pauses, now_fn=lambda: now)

    def core(url):
        snap = run("static_fetch", "read_page", lambda: monitor._snapshot_page(client(), url, settings))
        return {"url": url, "title": snap.get("title", ""), "text": snap.get("text", ""), "social": snap.get("social") or {}, "robots_note": "", "access_type": "page_text", "completeness_verified": False, "third_party": False}

    def jina(url):
        if not settings["readers"]["jina"]:                                   # the owner's switch is not a route failure: reported, never recorded in the route's health
            raise K.Failure(K.NEED_SETUP, "the Jina reader is switched off (readers.jina in observatory.yaml)")
        rd = readers_mod.JinaReader(settings, client(), http=READER_HTTP or polite.default_http)
        return run("reader", "read_via_reader", lambda: rd.read(url))

    def scrapling(url):
        python = os.environ.get("SCRAPLING_PYTHON")
        if not python:
            raise K.Failure(K.NEED_SETUP, "the Scrapling worker is not installed here (SCRAPLING_PYTHON is not set; the workflow's scrapling box installs it)")

        def read():
            page = SV.scrapling_page(settings, url, purpose, contact=contact, python=python, snaps=SnapshotStore(args.data_dir), pauses=pauses, now=now)
            return {"url": page["url"], "title": page.get("title", ""), "text": page.get("text", ""), "social": page.get("social") or {}, "published": "", "robots_note": page.get("robots_note", ""),
                    "access_type": "page_text", "completeness_verified": False, "third_party": False}
        return run("static_fetch", "read_page", read)
    return {"core": core, "jina": jina, "scrapling": scrapling}


def _homepage_social(args, store, settings, domain, now, contact):
    """The accounts a competitor's own home page links to, read through the shared page readers (the owner's route for social discovery: built-in by default, Scrapling when chosen). {} when nothing could be read."""
    from .. import contract as K
    if HOMEPAGE_SOCIAL:
        return HOMEPAGE_SOCIAL(domain)
    from . import shopify as S
    from .pauses import PauseLedger
    pauses = PauseLedger(args.data_dir)
    engines = _page_engines(args, None, settings, now, contact, "social_discovery", pauses)
    engine = SV.effective_engine(settings, "social_discovery", scrapling_installed=bool(os.environ.get("SCRAPLING_PYTHON")))
    try:
        page = SV.read_page(settings, S.base_url(domain) + "/", "social_discovery", {k: v for k, v in engines.items() if k != "jina"}, engine=engine if engine in ("core", "scrapling", "auto") else "core")
        return page.get("social") or {}
    except K.Failure:
        return {}
    finally:
        pauses.save(now)


def _enrich_entities(args, store, ledger, settings, brand, entities, scores, book, now):
    """Find each strong candidate's Instagram account (and website, when it has none) from several sources and record how sure we are. Returns notes."""
    from .. import contract as K
    cfg = settings["discovery"]
    notes = []
    targets = [e for e in sorted(entities.values(), key=lambda e: -scores[e["key"]][0]) if scores[e["key"]][0] >= cfg["min_score"]][:cfg["enrich_max"]]
    known = {c["key"]: c for c in book["candidates"]}
    targets = [e for e in targets if not e["instagram"] or (known.get(e["key"]) or {}).get("instagram_confidence") is None]
    if not targets:
        return notes
    contact = (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()
    if args.no_lookup and args.no_probe:                                  # both flags promise no outside request, so there is nothing to ask anyone
        return notes
    queries = [f"{e['name']} instagram" for e in targets] if not args.no_lookup else []
    searched, why = ({}, "") if not queries else _run_searches(args, store, ledger, settings, brand, queries, now)
    if why:
        notes.append(why)
    calls, ar_stopped, cli = 0, "", None
    try:
        cli = SV.agent_reach_client(settings, "instagram", "social_discovery", environ=os.environ, timeout=settings["instagram"]["timeout_seconds"], factory=OPENCLI_RUN)
    except K.Failure as exc:
        ar_stopped = f"Agent-Reach account search is not used: {exc}"
        notes.append(ar_stopped)
    for e in targets:
        homepage = None
        if e["domain"] and contact and not args.no_probe:
            homepage = (_homepage_social(args, store, settings, e["domain"], now, contact) or {}).get("instagram")
        agent_rows, profile_sites = None, {}
        if not ar_stopped and not args.no_lookup and calls < cfg["agent_reach_calls"]:
            try:
                agent_rows = enrich_mod.agent_rows_for(cli, e["name"])
                calls += 1
                for row in agent_rows[:2]:
                    if calls < cfg["agent_reach_calls"] and enrich_mod.name_matches_handle(e["name"], row["username"], row["name"]):
                        profile_sites[row["username"]] = row.get("site") or enrich_mod.profile_site(cli, row["username"])
                        calls += 1
            except K.Failure as exc:
                ar_stopped = f"Agent-Reach account search stopped ({exc.code}): {exc}"
                notes.append(ar_stopped)
                agent_rows = None
        found = enrich_mod.evidence_for_handle(e["name"], e["domain"], collected=e["instagram"] or None, homepage=homepage, search_results=searched.get(f"{e['name']} instagram"),
                                               agent_rows=agent_rows, profile_site=profile_sites)
        handle, conf, evidence, possible = enrich_mod.pick(found, cfg["min_confidence"])
        if handle and e["instagram"] and handle != e["instagram"].lstrip("@").lower():
            handle, possible = "", {"handle": handle, "confidence": conf, "evidence": evidence}         # disagrees with the account we already hold: say so, change nothing
        if enrich_mod.record(e, "instagram", handle, conf, evidence, possible, now):
            notes.append(f"{e['name']}: Instagram @{handle} (confidence {conf:.2f}, from {', '.join(sorted({x['source'] for x in evidence}))})")
        elif possible:
            notes.append(f"{e['name']}: possible Instagram @{possible['handle']} but only {possible['confidence']:.2f} sure; not recorded")
        for handle2, site in profile_sites.items():                                                          # an account's bio link can reveal a website we did not have
            if handle2 == handle and site and not e["domain"]:
                e["domain"] = OC.host_of(site)
                e["domain_confidence"] = 0.9
                e["domain_evidence"] = [{"source": "bio_link", "weight": 0.9, "detail": f"the Instagram profile @{handle2} links to {e['domain']}"}]
                notes.append(f"{e['name']}: website {e['domain']} from the Instagram profile's link")
    return notes


def _judge_stack(args, store, ledger, now):
    """(router, usage, review, side, notes): the real AI layer, or the test hook."""
    if JUDGE_ROUTER:
        router, usage, review, side = JUDGE_ROUTER()
        return router, usage, review, side, []
    try:
        hub = SV.load_settings(args.settings, args.data_dir)
    except OC.ObservatoryConfigError:
        hub = None
    router, usage, review, side, notes, _finish = SV.ai_stack(store, ledger, os.environ, providers_config=args.providers_config, lanes_config=args.lanes_config, gateway_dir=args.gateway_dir, workdir=args.workdir, settings=hub)
    return router, usage, review, side, notes


def _save_ai_state(usage, review, ledger, store):
    from ..ai import commands as aic
    aic.save_state(usage, review, ledger, store, "live")


def _judge_entities(args, store, ledger, settings, brand, entities, scores, book, now):
    """({key: verdict}, notes): the AI's opinion on strong candidates that have none yet. Never raises: no answer means no verdict."""
    cfg = settings["discovery"]
    ok, why = SV.allowed(settings, "ai_gateway", "discovery")
    if not ok:
        return {}, [f"AI judge not used: {why}"]
    known = {c["key"]: c for c in book["candidates"]}
    pool = [e for e in sorted(entities.values(), key=lambda e: -scores[e["key"]][0]) if scores[e["key"]][0] >= cfg["min_score"]
            and not (known.get(e["key"]) or {}).get("ai") and not (known.get(e["key"]) or {}).get("ai_rejected")][:cfg["judge_max"]]
    if not pool:
        return {}, []
    candidates = [{"key": e["key"], "name": e["name"], "domain": e["domain"], "score": scores[e["key"]][0], "sources": sorted(e["sources"]), "signals": {k: round(v, 1) for k, v in e["signals"].items()},
                   "reasons": scores[e["key"]][1]} for e in pool]
    side = None
    try:
        router, usage, review, side, notes = _judge_stack(args, store, ledger, now)
        verdicts, jnotes = judge_mod.judge_candidates(router, brand, candidates, entities, now)
        if not JUDGE_ROUTER:
            _save_ai_state(usage, review, ledger, store)
        elif hasattr(usage, "save"):
            usage.save()
            review.save()
        return verdicts, [n.replace("APPLICATION: ", "AI layer: ") for n in notes] + jnotes + ([f"AI judge: {len(verdicts)} verdict(s), each awaiting your review"] if verdicts else [])
    except Exception as exc:                                                    # noqa: BLE001  (the judge is an extra; discovery must go on without it)
        return {}, [f"AI judge skipped: {exc.__class__.__name__}: {str(exc)[:160]}"]
    finally:
        if side is not None:
            side.stop()


def _judge_command(args, store, ledger, settings, brands, now):
    """judge list: pending verdicts. judge approve|reject --competitor <key>: a person's decision on the AI's verdict. judge run: judge the current candidates now."""
    what = args.what if args.what in ("list", "approve", "reject", "run") else "list"
    for brand in brands:
        book = report.read_discovery(store, brand.slug)
        if what == "run":
            entities = discovery.extract_entities(store, brand, now)
            keywords = discovery.brand_keywords(store, brand)
            scores = {k: discovery.score_entity(e, keywords) for k, e in entities.items()}
            judged, notes = _judge_entities(args, store, ledger, settings, brand, entities, scores, book, now)
            discovery.merge(book, entities, scores, settings, now, judged=judged)           # creates any candidate that is new and attaches the verdicts the way discover does
            report.write_discovery(store, brand.slug, book, now)
            print(f"{brand.name}: {len(judged)} verdict(s) recorded, each awaiting your review")
            for note in notes:
                print(f"  note: {note}")
            continue
        from ..ai import review as review_mod
        queue = review_mod.ReviewQueue(Path(args.data_dir) / "_shared" / "ai_review.json")
        if what == "list":
            rows = [c for c in book["candidates"] if (c.get("ai") or {}).get("review") == "pending" or (c.get("ai_proposed") or {}).get("review") == "pending"]
            print(f"=== {brand.name}: {len(rows)} AI verdict(s) awaiting your review ===")
            for c in rows:
                v = c.get("ai") or c.get("ai_proposed")
                print(f"  {c['key']}: {v['suitability']} (confidence {v['confidence']:.2f}, relevant={v['relevant']}) via {v['provider']}/{v['model']}  [review {v['review_id']}]\n      {v['reason']}\n      evidence: " + "; ".join(v["evidence"][:3]))
            continue
        target = args.review_id or args.competitor
        match = next((c for c in book["candidates"] if c["key"] == target or any((c.get(s) or {}).get("review_id") == target for s in ("ai", "ai_proposed"))), None)
        if not match:
            print(f"{brand.name}: nothing awaiting review matches {target!r}")
            return 1
        v = match.get("ai") if (match.get("ai") or {}).get("review") == "pending" else match.get("ai_proposed")
        if not v or v.get("review") != "pending":
            print(f"{brand.name}: {match['key']} has no verdict awaiting review")
            return 1
        try:
            queue.decide(v["review_id"], "approved" if what == "approve" else "rejected", "owner", now)
        except (KeyError, ValueError) as exc:
            print(f"{brand.name}: {exc}")
            return 1
        queue.save()
        judge_mod.sync_reviews(book, queue)
        report.write_discovery(store, brand.slug, book, now)
        print(f"{brand.name}: AI verdict for {match['key']} {what}d" if what == "approve" else f"{brand.name}: AI verdict for {match['key']} rejected, so it is ignored from now on")
    return 0


def _brand_competitors(args, store, settings, brand):
    """The competitors a brand watches right now (its file, what discovery chose, accounts found on their own pages), planned the way `monitor` plans them, so nothing needs a monitor run first."""
    planned = _plan(args, store, settings, [brand])
    return [slot["comp"] for slot in planned.values() if brand.slug in slot["brands"]]


def _instagram(args, store, ledger, settings, brands, now):
    """The Agent-Reach Instagram route beside the existing collector: collect, compare, or check that it can run here. Needs OpenCLI and a browser a person has logged in; never runs on a hosted runner."""
    from .. import agent_reach_instagram as AR
    from .. import contract as K
    from .. import evidence as evidence_mod
    from .. import sources as sources_mod
    what = args.what if args.what in ("collect", "compare", "check") else "compare"
    cfg = settings["instagram"]
    registry = sources_mod.load_sources(args.sources_config)
    try:
        cli = SV.agent_reach_client(settings, "instagram", "competitors", environ=os.environ, timeout=cfg["timeout_seconds"], factory=OPENCLI_RUN)
    except K.Failure as exc:
        print(f"Instagram via Agent-Reach: {exc.code}: {exc}")
        return 1
    worst = 0
    if what == "check":
        handle = (args.handle.split(",")[0].strip() or os.environ.get("INSTAGRAM_CHECK_HANDLE", "instagram")).lstrip("@")
        row = AR.check_row(cli, handle)
        print(("ok: " if row["ok"] else f"FAILED ({row.get('code')}): ") + row["detail"])
        return 0 if row["ok"] else 1
    for brand in brands:
        batches = store.read_collected(brand.slug)
        if what == "compare":
            health_row = next((r for r in ledger.rows(now) if r["source"] == "instagram" and r["route"] == "agent_reach"), None)
            result = AR.compare(batches, health_row)
            text = AR.report_text(result, brand.name, now)
            store.write_export(brand.slug, "instagram-routes.md", text)
            print(text)
            continue
        handles = list(dict.fromkeys([h.strip().lstrip("@") for h in args.handle.split(",") if h.strip()] + [c["instagram"].lstrip("@") for c in _brand_competitors(args, store, settings, brand) if c.get("instagram")]
                                     + [c["instagram"].lstrip("@") for c in report.read_discovery(store, brand.slug)["candidates"] if c.get("instagram")]))[:cfg["max_handles"]]
        if not handles:
            print(f"{brand.name}: no Instagram accounts to read. Add instagram: handles to competitors in brands/{brand.slug}/observatory.yaml, or use --handle.")
            continue
        holder = {}

        def read_all():
            rows, problems = AR.collect_handles(cli, handles, cfg["posts_per_handle"])
            holder["problems"] = problems
            if not rows and problems:
                first = problems[0]
                raise K.Failure(first["code"], first["message"])
            return rows
        env = K.run_action("instagram", "profile_posts", [("agent_reach", read_all)], health=ledger, now=now)
        if not env["ok"]:
            print(f"{brand.name}: Instagram via Agent-Reach: {env['error']['code']}: {env['error']['message']}")
            worst = 1
            continue
        rows, stats = AR.merge_with_existing(batches, env["items"])
        run_id = "obs-" + now
        record = {"at": now, "run": run_id, "source": "instagram", "key": "agent_reach:" + ",".join(handles), "seeds": handles, "via": "agent_reach", "from_site_data": 0, "from_links": 0, "items": rows,
                  "provenance": evidence_mod.provenance(registry, "instagram", "agent_reach", now, run_id, ledger.status("instagram", "agent_reach", now))}
        store.append_collected(brand.slug, record)
        print(f"=== {brand.name}: Instagram via Agent-Reach: {len(rows)} post(s) from {len(handles)} account(s): {stats['new']} new, {stats['overlap']} already held by the existing collector "
              f"({stats['filled']} gaps filled, {stats['conflicts']} count disagreement(s) noted) ===")
        for p in holder.get("problems", []):
            print(f"  ::warning::@{p['handle']}: {p['code']}: {p['message']}")
    return worst


def _reddit(args, store, ledger, settings, brands, now):
    """Reddit through the official API and Agent-Reach together: collect, compare the two, or check each can run. The API route refuses to run without a recorded approval from Reddit."""
    from .. import agent_reach_instagram as AR
    from .. import contract as K
    from .. import evidence as evidence_mod
    from .. import reddit_routes as RR
    from .. import sources as sources_mod
    what = args.what if args.what in ("collect", "compare", "check") else "collect"
    cfg = settings["reddit"]
    contact = (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()
    registry = sources_mod.load_sources(args.sources_config)
    routes = [r for r in ("api", "agent_reach") if args.route in ("all", r)]

    def make(route):
        if route == "api":
            return RR.RedditAPI(os.environ.get("REDDIT_CLIENT_ID", "").strip(), os.environ.get("REDDIT_CLIENT_SECRET", "").strip(), contact, cfg["api_approval"], http=REDDIT_HTTP or RR.default_http,
                                timeout=cfg["timeout_seconds"])
        return SV.agent_reach_client(settings, "reddit", "content_research", environ=os.environ, timeout=cfg["timeout_seconds"], factory=OPENCLI_RUN)
    if what == "check":
        worst = 0
        for route in routes:
            try:
                rows, problems = RR.collect(route, make(route), [("r", (args.subreddit.split(",")[0].strip() or os.environ.get("REDDIT_CHECK_SUBREDDIT", "announcements")))], 2)
            except K.Failure as exc:
                rows, problems = [], [{"code": exc.code, "message": str(exc)}]
            ok = bool(rows)
            print(f"{route}: " + (f"ok: read {len(rows)} post(s)" if ok else f"FAILED ({problems[0]['code']}): {problems[0]['message']}" if problems else "FAILED: nothing came back"))
            worst = worst or (0 if ok else 1)
        return worst
    worst = 0
    for brand in brands:
        batches = store.read_collected(brand.slug)
        if what == "compare":
            health_row = next((r for r in ledger.rows(now) if r["source"] == "reddit" and r["route"] == "agent_reach"), None)
            result = AR.compare(batches, health_row, source="reddit", newcomer="agent_reach")
            text = AR.report_text(result, brand.name, now, established="Official API", newcomer="Agent-Reach", title="Reddit routes compared")
            store.write_export(brand.slug, "reddit-routes.md", text)
            print(text)
            continue
        file = OC.load_brand_file(brand.slug, args.brands_dir)["reddit"]
        subs = list(dict.fromkeys(file["subreddits"] + [s.strip().removeprefix("r/") for s in args.subreddit.split(",") if s.strip()]))
        queries = list(dict.fromkeys(file["queries"] + [q.strip() for q in args.query.split(",") if q.strip()]))
        targets = ([("r", s) for s in subs] + [("q", q) for q in queries])[:cfg["max_targets"]]
        if not targets:
            print(f"{brand.name}: no Reddit communities or searches to read. Add reddit: subreddits / queries in brands/{brand.slug}/observatory.yaml, or use --subreddit / --query.")
            continue
        succeeded, failed = 0, []
        for route in routes:
            if route == "agent_reach":                                     # an owner's switch is not a route failure: it is reported and never recorded in the route's health
                allowed, why = SV.agent_reach_allowed(settings, "reddit", "content_research")
                if not allowed:
                    failed.append(route)
                    print(f"{brand.name}: Reddit via {route}: NEED_SETUP: {why}")
                    continue
            holder = {}

            def read_all(route=route):
                rows, problems = RR.collect(route, make(route), targets, cfg["posts_per_target"])
                holder["problems"] = problems
                if not rows and problems:
                    raise K.Failure(problems[0]["code"], problems[0]["message"])
                return rows
            env = K.run_action("reddit", "collect", [(route, read_all)], health=ledger, now=now)
            if not env["ok"]:
                failed.append(route)
                print(f"{brand.name}: Reddit via {route}: {env['error']['code']}: {env['error']['message']}")
                continue
            rows, stats = (env["items"], None) if route == "api" else AR.merge_with_existing(store.read_collected(brand.slug), env["items"], source="reddit", newcomer="agent_reach")
            run_id = "obs-" + now
            store.append_collected(brand.slug, {"at": now, "run": run_id, "source": "reddit", "key": f"{route}:" + ",".join(label for _, label in targets), "seeds": [label for _, label in targets], "via": route,
                                                "from_site_data": 0, "from_links": 0, "items": rows, "provenance": evidence_mod.provenance(registry, "reddit", route, now, run_id, ledger.status("reddit", route, now))})
            succeeded += 1
            extra = "" if stats is None else f": {stats['new']} new, {stats['overlap']} already held by another route ({stats['filled']} gaps filled, {stats['conflicts']} count disagreement(s) noted)"
            print(f"=== {brand.name}: Reddit via {route}: {len(rows)} post(s) from {len(targets)} target(s){extra} ===")
            for p in holder.get("problems", []):
                print(f"  ::warning::{p['target']}: {p['code']}: {p['message']}")
        if routes and not succeeded or (args.route != "all" and failed):
            worst = 1
    return worst


def _controls(args, store, settings, brands, now):
    """The platform's shared-service controls: show them (with their initial defaults and where each value comes from), set or reset one, and what every competitor ends up with. Nothing is a
    secret here: cookies and proxies show as the secret's name."""
    what = args.what if args.what in ("show", "set", "reset") else "show"
    if what in ("set", "reset"):
        defn = SV.BY_KEY.get(args.key)
        if not defn:
            print(f"Error: no control called {args.key!r}. Run `observatory controls` to list them.")
            return 1
        values = SV.load_overrides(args.data_dir)
        try:
            typed = SV.coerce(defn, args.value) if what == "set" else defn["default"]
        except SV.ControlError as exc:
            print(f"Error: {exc}")
            return 1
        before = SV.display(SV.current(settings, args.key))
        if typed == defn["default"]:
            values.pop(args.key, None)
        else:
            values[args.key] = typed
        SV.save_overrides(args.data_dir, values, "command line", now, f"{args.key}: {before} -> {SV.display(typed)}")
        print(f"{args.key}: {before} -> {SV.display(typed)}" + ("  (back to its initial default)" if typed == defn["default"] else "") + "\nThe Controls tab shows it at the next sheet update.")
        SV.apply_controls(settings, {args.key: typed})
    on = settings["access_controls"]
    print("Shared-service controls (value now; initial default; where the value comes from). Change one: `observatory controls --what set --key K --value V`, or edit the Controls tab in a sheet.")
    scope = ""
    for r in SV.effective_rows(settings, args.data_dir):
        if r["scope"] != scope:
            scope = r["scope"]
            print(f"  [{scope}]")
        print(f"    {r['key']:48} {SV.display(r['value']):>8}   default {SV.display(r['default']):>8}   {r['source']}")
    for n, item in enumerate(settings["services"]["scrapling"]["overrides"], start=1):
        print(f"  override {n}: {item['match']} -> " + (report.controls_summary({k: v for k, v in item.items() if k != 'match'}) or "no options"))
    print("Owner switches (observatory.yaml, access_controls): " + ", ".join(f"{k} {'on' if v else 'OFF'}" for k, v in on.items()))
    print(f"Pages are read by: {settings['engine']['pages']}. Politeness minimum pause {settings['politeness']['min_delay_seconds']}s; a challenge, 401/403 or 429 always rests the host, whatever is set.")
    for brand in brands:
        print(f"=== {brand.name} ===")
        for comp in _brand_competitors(args, store, settings, brand):
            summary = report.controls_summary(comp.get("advanced") or {})
            print(f"  {comp['domain']}: " + (summary or "no advanced controls (honest identity, standard requests)"))
            off = [k for k, switch in (("impersonate", "evasion"), ("hide_canvas", "evasion"), ("block_webrtc", "evasion"), ("humanize", "evasion"), ("solve_challenges", "evasion"), ("browser", "browser"),
                                       ("cookies_env", "account_cookies"), ("proxy", "proxy"), ("session", "session"), ("identity", "custom_identity")) if (comp.get("advanced") or {}).get(k) and not on[switch]]
            if off:
                print(f"    not applied: {', '.join(off)} (switched off above, so this competitor is refused with a message until the switch is turned on)")
    return 0


def _agentreach(args, store, ledger, settings, brands, now):
    """Agent-Reach as a shared research service: search or read on a channel (YouTube, Reddit, Instagram) for any purpose, not only competitor records. The results are stored as collected records with
    their provenance and route health. Needs OpenCLI and a logged-in browser on a machine you control; never on a hosted runner."""
    import hashlib

    from .. import agent_reach_instagram as AR
    from .. import contract as K
    from .. import evidence as evidence_mod
    from .. import sources as sources_mod
    from ..collectors import _iso, _row
    what = args.what if args.what in CAT.every_action_name() else "search"
    purpose = CAT.purpose_for(args.ar_channel)
    extra = dict(p.split("=", 1) for p in (args.param or []) if "=" in p)
    spec = CAT.CHANNELS[args.ar_channel]
    first = (spec["actions"].get(what) or {"params": ("query",)})["params"][0]
    given = extra.get(first) or ((args.url or args.query) if first == "target" else args.query)
    if not given and spec["actions"].get(what):
        print(f"Error: {what} needs " + ("--url" if first == "target" else "--query"))
        return 1
    try:
        argv, clean = CAT.prepare(args.ar_channel, what, {**extra, first: given}, limit=10)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    target = clean[first]
    if args.ar_channel == "web":                                  # Agent-Reach's web channel is Jina Reader: the platform's own shared page readers do it
        return _agentreach_web(args, store, ledger, settings, brands, now, target)
    try:
        cli = SV.agent_reach_client(settings, args.ar_channel, purpose, environ=os.environ, timeout=settings["instagram"]["timeout_seconds"], factory=OPENCLI_RUN)
    except K.Failure as exc:
        print(f"Agent-Reach {args.ar_channel}: {exc.code}: {exc}")
        return 1
    if getattr(cli, "needs_browser", True) and AR.hosted_runner(cli.environ if getattr(cli, "environ", None) is not None else os.environ):
        print(f"Agent-Reach {args.ar_channel}: NEED_SETUP: this route does not run on a GitHub-hosted runner: it needs a browser logged in to {args.ar_channel.capitalize()}, which must stay on a machine you control")
        return 1
    registry = sources_mod.load_sources(args.sources_config)
    worst = 0
    for brand in brands:
        def fetch():
            rows = []
            for raw in AR.rows_from(cli.call(argv if argv is not None else [what, target] + (["--limit", clean["limit"]] if what == "search" else [])))[:25]:
                url = str(AR._pick(raw, "url", "link", "webpage_url") or "")
                title = str(AR._pick(raw, "title", "name", "text") or "")
                if not (url or title):
                    continue
                rows.append(_row("agent_reach_research", f"{args.ar_channel}:" + hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:12], handle=str(AR._pick(raw, "channel", "author", "uploader", "user") or ""),
                                 title=title, text=str(AR._pick(raw, "description", "content", "body", "transcript", "subtitle") or ""), url=url, posted_at=_iso(AR._pick(raw, "published_at", "upload_date", "date")),
                                 likes=AR._count(raw.get("likes"), raw.get("like_count")), comments=AR._count(raw.get("comments"), raw.get("comment_count")), views=AR._count(raw.get("views"), raw.get("view_count")),
                                 details=f"{args.ar_channel} {what}", found_via="agent_reach"))
            if not rows:
                raise K.Failure(K.UPSTREAM_BROKEN, "Agent-Reach finished but returned nothing usable")
            return rows
        env = K.run_action("agent_reach_research", what, [(args.ar_channel, fetch)], health=ledger, now=now)
        if not env["ok"]:
            print(f"{brand.name}: Agent-Reach {args.ar_channel} {what}: {env['error']['code']}: {env['error']['message']}")
            worst = 1
            continue
        run_id = "obs-" + now
        store.append_collected(brand.slug, {"at": now, "run": run_id, "source": "agent_reach_research", "key": f"{args.ar_channel}:{what}:{target}", "seeds": [target], "via": args.ar_channel, "from_site_data": 0,
                                            "from_links": 0, "items": env["items"], "provenance": evidence_mod.provenance(registry, "agent_reach_research", args.ar_channel, now, run_id, ledger.status("agent_reach_research", args.ar_channel, now))})
        print(f"=== {brand.name}: Agent-Reach {args.ar_channel} {what} for {target!r}: {len(env['items'])} result(s) ===")
        for r in env["items"][:10]:
            print(f"  {r['title'][:80]}  {r['url']}")
    return worst


def _agentreach_web(args, store, ledger, settings, brands, now, url):
    """Agent-Reach's web channel through the shared page readers (Scrapling, Jina, the built-in reader), under the owner's reader route; nothing is duplicated."""
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    worst = 0
    for brand in brands:
        page, error = _read_url(args, store, ledger, settings, brand, now, contact, url, "general_research", engine=args.engine or None)
        if page is None:
            print(f"{brand.name}: Agent-Reach web read: {error}")
            worst = 1
            continue
        print(f"=== {brand.name}: web read through the shared page readers ({page['engine']}): {len(page['text'])} characters ===")
        print(page["text"][:400])
    return worst


def _reliability(args, store, ledger, brands, now):
    """How each collection route for a platform has done: posts, completeness, overlap, unique finds and success rate. Advice only; nothing is switched off."""
    from .. import agent_reach_instagram as AR
    source = {"instagram": "instagram", "tiktok": "tiktok", "reddit": "reddit"}.get(args.platform)
    if not source:
        print("Error: --platform must be instagram, tiktok or reddit")
        return 1
    rows = [r for r in ledger.rows(now) if r["source"] == source]
    for brand in brands:
        text = AR.routes_report(store.read_collected(brand.slug), rows, source, brand.name, now)
        store.write_export(brand.slug, f"{source}-reliability.md", text)
        print(text)
    return 0


def _decide(args, store, brands, now):
    """A person's choice about a discovered competitor. It is permanent: automatic runs never change it. A competitor listed in a brand file is controlled by the file."""
    if not args.competitor or not args.decision:
        print("Error: decide needs --competitor (its name key or website) and --decision monitor, stop or reset")
        return 1
    worst = 0
    for brand in brands:
        book = report.read_discovery(store, brand.slug)
        wanted = (OC.host_of(args.competitor) or "").removeprefix("www.")
        if not any(c["key"] == args.competitor or (c.get("domain") and c["domain"].removeprefix("www.") == wanted) for c in book["candidates"]):
            print(f"{brand.name}: no discovered competitor matches {args.competitor!r}. Known: " + (", ".join(c["key"] for c in book["candidates"][:15]) or "none yet (run discover)"))
            worst = 1
            continue
        changed = discovery.apply_decisions(book, {args.competitor: args.decision}, now)
        if changed:
            report.write_discovery(store, brand.slug, book, now)
        print(f"{brand.name}: {args.competitor} -> {args.decision}" + ("" if changed else " (already so)"))
    return worst


def _discover_with_agent_reach(args, ledger, settings, corpus, refs, queries, max_channels, now):
    """Channels found by searching YouTube through Agent-Reach (yt-dlp), added beside the ones the Data API's search found. It spends none of the API's daily units, and is skipped quietly when the owner has
    switched it off or yt-dlp is not here. A failure is reported and never stops the run."""
    from .. import contract as K
    try:
        cli = SV.agent_reach_client(settings, "youtube", "youtube_research", environ=os.environ, timeout=settings["instagram"]["timeout_seconds"], factory=OPENCLI_RUN)
    except K.Failure:
        return
    if not cli.available():
        return
    rows = []

    def search_all():
        for q in queries:
            rows.extend((q, r) for r in AR_rows(cli.call(["search", q, "--limit", 10])))
        return [{"ok": True}]
    env = K.run_action("agent_reach_research", "search", [("youtube", search_all)], health=ledger, now=now)
    if not env["ok"]:
        print(f"  ::warning::Agent-Reach channel search stopped ({env['error']['code']}): {env['error']['message']}")
        return
    added = 0
    for ref, title, q in corpus_mod.channels_from_rows(rows):
        known = {c.lower() for c in refs} | {k.lower() for k in corpus["channels"]}
        if ref.lower() in known:
            continue
        corpus["channels"].setdefault(ref, {"id": ref, "title": title, "added_at": now, "found_by": f"agent_reach: {q}"})
        if len(refs) < max_channels:
            refs.append(ref)
        added += 1
    print(f"  Agent-Reach (yt-dlp) channel search: {added} new channel(s), no API units spent")


def AR_rows(data):
    from .. import agent_reach_instagram as AR
    return AR.rows_from(data)


def _corpus(args, store, ledger, settings, brands, snaps, now):
    from .. import contract as K
    from .. import sources as sources_mod
    formulas = corpus_mod.load_formulas()
    cfg = settings["youtube"]
    worst = 0
    for brand in brands:
        corpus = corpus_mod.load_corpus(store, brand.slug)
        if args.what == "youtube":
            key = os.environ.get("YOUTUBE_API_KEY", "").strip()
            if not key:
                print(f"{brand.name}: no key. Set the YOUTUBE_API_KEY secret (a free YouTube Data API key).")
                worst = 1
                continue
            file = OC.load_brand_file(brand.slug, args.brands_dir)
            refs = list(dict.fromkeys(file["youtube"]["channels"] + [c.strip() for c in args.channel.split(",") if c.strip()]
                                      + [c["youtube"] for c in _brand_competitors(args, store, settings, brand) if c.get("youtube")]))[:cfg["max_channels"]]
            quota = corpus_mod.Quota(args.data_dir, cfg["daily_unit_cap"], now)
            api = corpus_mod.YouTubeAPI(key, quota, get=YOUTUBE_GET or corpus_mod.default_get)
            if args.find_channels and file["youtube"]["queries"] and cfg["search_calls_per_run"]:
                try:
                    for cid, title, q in corpus_mod.discover_channels(api, file["youtube"]["queries"], cfg["search_calls_per_run"]):
                        corpus["channels"].setdefault(cid, {"id": cid, "title": title, "added_at": now, "found_by": q})
                        if cid not in refs and len(refs) < cfg["max_channels"]:
                            refs.append(cid)
                    print(f"  searched for channels: {len(corpus['channels'])} known")
                except K.Failure as exc:
                    print(f"  ::warning::channel search stopped ({exc.code}): {exc}")
            if args.find_channels and file["youtube"]["queries"] and cfg["search_calls_per_run"]:        # the same discovery through Agent-Reach's YouTube channel (yt-dlp): no API units spent
                _discover_with_agent_reach(args, ledger, settings, corpus, refs, file["youtube"]["queries"][:cfg["search_calls_per_run"]], cfg["max_channels"], now)
            if not refs:
                print(f"{brand.name}: no YouTube channels to read. List them under youtube.channels in brands/{brand.slug}/observatory.yaml, or use --channel @name.")
                continue
            holder = {}

            def read_all():
                try:
                    holder["got"] = corpus_mod.collect_channels(api, corpus, refs, {**cfg, "videos_per_channel": file["youtube"]["max_videos_per_channel"] or cfg["videos_per_channel"]}, now)
                except corpus_mod._Stopped as stop:
                    holder["got"] = stop.partial
                    raise stop.failure from None
                return [holder["got"]] if holder["got"]["channels"] else []
            env = K.run_action(corpus_mod.SOURCE, "channel_uploads", [(corpus_mod.ROUTE, read_all)], health=ledger, now=now, empty_ok=False)
            quota.save()
            corpus_mod.save_corpus(store, brand.slug, corpus)
            got = holder.get("got", {"channels": 0, "videos": 0, "new": 0, "errors": []})
            print(f"=== {brand.name}: YouTube: {got['channels']} channel(s), {got['videos']} video(s) read ({got['new']} new); {quota.used} of {cfg['daily_unit_cap']} units used today ===")
            for e in got["errors"]:
                print(f"  ::warning::{e['channel']}: {e['code']}: {e['message']}")
            if not env["ok"]:
                print(f"  ::warning::{env['error']['code']}: {env['error']['message']}")
                worst = 1
        if args.what in ("youtube", "outliers"):
            res = corpus_mod.outliers(list(corpus["videos"].values()), formulas, min_videos=cfg["min_videos_for_median"], min_multiple=cfg["outlier_min_multiple"])
            text = corpus_mod.report_text(res, brand.name, now)
            store.write_export(brand.slug, "youtube-outliers.md", text)
            print(text)
        if args.what in ("youtube", "trending"):
            trend = corpus_mod.velocity(list(corpus["videos"].values()), now, window_days=cfg["trend_window_days"], min_hours=cfg["trend_min_hours"], min_multiple=cfg["trend_min_multiple"])
            text = corpus_mod.trending_report_text(trend, brand.name, now)
            store.write_export(brand.slug, "youtube-trending.md", text)
            print(text)
        elif args.what == "stats":
            vids = list(corpus["videos"].values())
            print(f"{brand.name}: {len(corpus['channels'])} channel(s), {len(vids)} video(s); shorts {sum(1 for v in vids if v.get('shorts_class') == 'short')}, "
                  f"possible shorts {sum(1 for v in vids if v.get('shorts_class') == 'possible_short')}, long {sum(1 for v in vids if v.get('shorts_class') == 'long')}")
        elif args.what == "search":
            if not args.query.strip():
                print("Error: search needs --query")
                return 1
            registry = sources_mod.load_sources(args.sources_config)
            docs = corpus_mod.build_documents(store, snaps, brand.slug, registry, now, [s["id"] for s in report.read_brand_sites(store, brand.slug)])
            wanted = {x.strip() for x in args.source.split(",") if x.strip()}
            if wanted:
                docs = [d for d in docs if d["source"] in wanted]
            if args.since:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
                    print("Error: --since must look like 2026-11-01")
                    return 1
                docs = [d for d in docs if str(d.get("when", ""))[:10] >= args.since]
            hits = corpus_mod.search(docs, args.query, limit=10, snippet_chars=settings["corpus"]["snippet_max_chars"])
            print(f"{brand.name}: {len(hits)} match(es) for {args.query!r} in {len(docs)} item(s) of the research corpus")
            for h in hits:
                print(f"  {h['score']:6.2f}  [{h['source']}] {h['title'][:70]}  {h['url']}\n          {h['snippet'][:150]}")
        elif args.what == "read":
            code = _read_page(args, store, ledger, settings, brand, now)
            worst = max(worst, code)
        elif args.what == "deepread":
            worst = max(worst, _deepread(args, store, ledger, settings, [brand], now))
        elif args.what == "channelpages":
            worst = max(worst, _channel_pages(args, store, ledger, settings, [brand], now, snaps))
        elif args.what == "crawl":
            worst = max(worst, _crawl_site(args, store, settings, brand, now))
        elif args.what == "feeds":
            worst = max(worst, _feeds_of(args, settings, brand, now))
    return worst


_READER_LABEL = {"jina": "the reader", "scrapling": "the Scrapling worker", "core": "the built-in reader"}


def _read_url(args, store, ledger, settings, brand, now, contact, url, purpose, *, engine=None, sink="reader.jsonl"):
    """(page, '') or (None, 'CODE: message') for one public page through the shared page readers, appended to the brand's corpus file."""
    from .. import contract as K
    from .pauses import PauseLedger
    pauses = PauseLedger(args.data_dir)
    engines = _page_engines(args, ledger, settings, now, contact, purpose, pauses)
    choice = engine or SV.effective_engine(settings, purpose, scrapling_installed=bool(os.environ.get("SCRAPLING_PYTHON")))
    try:
        page = SV.read_page(settings, url, purpose, engines, engine=choice)
    except K.Failure as exc:
        return None, f"{exc.code}: {exc}"
    finally:
        pauses.save(now)
    path = store.dir(brand.slug) / "corpus" / sink
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**page, "reader": page.get("reader") if page["engine"] == "jina" else page["engine"], "purpose": purpose, "captured_at": now}, ensure_ascii=False, sort_keys=True) + "\n")
    return page, ""


def _crawl_site(args, store, settings, brand, now):
    """A polite crawl of one public site through Scrapling, for any purpose: every page is stored in the brand's corpus (and so searchable) and the stop reason is printed."""
    from .. import contract as K
    from .pauses import PauseLedger
    if not args.url:
        print("Error: crawl needs --url (the address to start from)")
        return 1
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    pauses = PauseLedger(args.data_dir)
    extract = []
    for spec in args.extract or []:
        name, _, rest = spec.partition("=")
        kind, _, value = rest.partition(":")
        if not (name and value and kind in ("css", "xpath", "text", "regex", "similar")):
            print(f"Error: --extract {spec!r} must look like name=kind:value, where kind is css, xpath, text, regex or similar")
            return 1
        extract.append({"name": name, kind: value})
    try:
        got = SV.scrapling_crawl(settings, args.url, "content_research", contact=contact, python=os.environ.get("SCRAPLING_PYTHON", ""), max_pages=args.max_pages or None,
                                 depth=None if args.depth < 0 else args.depth, pauses=pauses, now=now, sitemap=args.sitemap, extract=extract, state_dir=args.data_dir, resume=args.resume)
    except K.Failure as exc:
        print(f"{brand.name}: crawl: {exc.code}: {exc}")
        return 1
    finally:
        pauses.save(now)
    path = store.dir(brand.slug) / "corpus" / "reader.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for page in got["pages"]:
            found = "".join(f"\n{k}: " + (", ".join(v) if isinstance(v, list) else "") for k, v in (page.get("extracted") or {}).items())
            fh.write(json.dumps({"url": page["url"], "title": page.get("title") or "", "text": (page.get("markdown") or page.get("text") or "") + found, "engine": "scrapling", "reader": "scrapling",
                                 "purpose": "content_research", "captured_at": now, "crawl_root": args.url}, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"=== {brand.name}: crawled {len(got['pages'])} page(s) from {args.url} through Scrapling; stopped: {got['stopped']} ===")
    if got.get("sitemap"):
        print(f"  the sitemap listed {got['sitemap']['addresses']} address(es)")
    if got.get("resumed"):
        print("  carried on from the earlier crawl of this address")
    if got.get("state_saved"):
        print("  stopped early: run it again with --resume to carry on without reading anything twice")
    for page in got["pages"][:15]:
        print(f"  {(page.get('title') or page['url'])[:70]}  {page['url']}")
        for k, v in (page.get("extracted") or {}).items():
            print(f"      {k}: " + (" | ".join(v[:5]) if isinstance(v, list) else f"could not extract ({v.get('error')})"))
    for skip in got["skipped"][:10]:
        print(f"  not read: {skip['url']}  {skip['code']}: {skip['message'][:100]}")
    return 0


def _feeds_of(args, settings, brand, now):
    """The feeds a public site has, found through Scrapling's guarded fetch: advertised in its page, or answering at a usual address."""
    from .. import contract as K
    from .pauses import PauseLedger
    if not args.url:
        print("Error: feeds needs --url (the site's address)")
        return 1
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    pauses = PauseLedger(args.data_dir)
    try:
        found = SV.discover_feeds(settings, args.url, "content_research", contact=contact, python=os.environ.get("SCRAPLING_PYTHON", ""), pauses=pauses, now=now)
    except K.Failure as exc:
        print(f"{brand.name}: feeds: {exc.code}: {exc}")
        return 1
    finally:
        pauses.save(now)
    print(f"=== {brand.name}: {len(found)} feed(s) found for {args.url} ===")
    for feed in found:
        print(f"  {feed['url']}  ({feed['how']})")
    return 0


def _contact(settings):
    return (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()


def _read_page(args, store, ledger, settings, brand, now):
    if not args.url:
        print("Error: read needs --url")
        return 1
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    page, error = _read_url(args, store, ledger, settings, brand, now, contact, args.url, "content_research", engine=args.engine or None)
    if page is None:
        print(f"Error: {error}")
        return 1
    print(f"{page['title'] or page['url']}\n  {len(page['text'])} characters read through {_READER_LABEL.get(page['engine'], page['engine'])}" + (f"\n  NOTE: {page['robots_note']}" if page.get("robots_note") else ""))
    print(page["text"][:600])
    return 0


def _youtube_about_url(channel):
    channel = channel.strip()
    if channel.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_.\-]{2,60}", channel):
        return f"https://www.youtube.com/{channel}/about"
    if re.fullmatch(r"UC[A-Za-z0-9_\-]{22}", channel):
        return f"https://www.youtube.com/channel/{channel}/about"
    return ""


def _same_channel(a, b):
    norm = lambda v: str(v or "").strip().lstrip("@").lower()          # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def _record_page_social(comp, found, snaps, now):
    """The accounts a competitor's YouTube channel links to, recorded on that competitor the way its own home page's links are (what a person wrote always wins; one event when a new account appears)."""
    from types import SimpleNamespace
    run = SimpleNamespace(comp=comp, site=comp["id"], events=[])
    before = snaps.read_social(comp["id"])
    monitor._record_social(run, {k: v for k, v in (found or {}).items() if k in ("instagram", "tiktok")}, snaps, now)
    if run.events:
        snaps.append_events(comp["id"], run.events)
    after = snaps.read_social(comp["id"])
    return {k: v for k, v in after.items() if k not in before}                       # only what was newly recorded


def _channel_pages(args, store, ledger, settings, brands, now, snaps=None):
    """YouTube research through the shared page readers: each channel's About page (links, other accounts), read with the owner's route for YouTube research (auto: Scrapling, then Jina, then built-in)."""
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    worst = 0
    for brand in brands:
        listed = OC.load_brand_file(brand.slug, args.brands_dir)["youtube"]["channels"] + [c.strip() for c in args.channel.split(",") if c.strip()]
        channels = list(dict.fromkeys(listed))
        if not channels:
            print(f"{brand.name}: no YouTube channels to read. Add youtube: channels in brands/{brand.slug}/observatory.yaml, or use --channel @handle.")
            continue
        for ch in channels:
            url = _youtube_about_url(ch)
            if not url:
                print(f"  {ch}: not a channel handle (@name) or id (UC...); skipped")
                continue
            page, error = _read_url(args, store, ledger, settings, brand, now, contact, url, "youtube_research", engine=args.engine or None, sink="channel_pages.jsonl")
            if page is None:
                print(f"  {ch}: {error}")
                worst = 1
                continue
            linked = ", ".join(f"{k} {v}" for k, v in sorted((page.get("social") or {}).items())) or "no linked accounts found"
            print(f"  {ch}: read through {_READER_LABEL.get(page['engine'], page['engine'])}; {linked}")
            if snaps is not None and page.get("social"):
                for comp in _brand_competitors(args, store, settings, brand):
                    recorded = _record_page_social(comp, page["social"], snaps, now) if _same_channel(comp.get("youtube"), ch) else {}
                    if recorded:
                        print(f"    recorded on {comp['name']}: its YouTube channel links to " + ", ".join(f"{k} {v}" for k, v in sorted(recorded.items())))
    return worst


def _deepread(args, store, ledger, settings, brands, now):
    """General research reads the pages behind its own search results: the newest results not read yet, up to --top, through the owner's route for general research."""
    contact = _contact(settings)
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    worst = 0
    for brand in brands:
        done = set()
        path = store.dir(brand.slug) / "corpus" / "reader.jsonl"
        if path.is_file():
            done = {json.loads(line).get("url") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
        urls = []
        for rec in reversed(store.read_searches(brand.slug)):
            for r in rec.get("results", []):
                u = r.get("url", "")
                if u.startswith(("http://", "https://")) and u not in done and u not in urls:
                    urls.append(u)
        urls = urls[:max(1, args.top)]
        if not urls:
            print(f"{brand.name}: no search results waiting to be read (run research first).")
            continue
        ok = 0
        for u in urls:
            page, error = _read_url(args, store, ledger, settings, brand, now, contact, u, "general_research", engine=args.engine or None)
            if page is None:
                print(f"  {u}: {error}")
                continue
            ok += 1
            print(f"  {u}: {len(page['text'])} characters through {_READER_LABEL.get(page['engine'], page['engine'])}")
        print(f"{brand.name}: {ok} of {len(urls)} page(s) read into the research corpus")
        worst = worst or (0 if ok else 1)
    return worst


def _report(args, store, settings, brands, snaps, now):
    for b in brands:
        sites = [{"id": s["id"], "name": s["name"], "domain": s["domain"]} for s in report.read_brand_sites(store, b.slug)]
        if not sites:
            print(f"{b.name}: nothing is watched yet (run: observatory monitor).")
            continue
        text = report.build_report(b.name, sites, snaps, now, days=args.since_days)
        if args.json:
            print(json.dumps(report.recent_events(snaps, [s["id"] for s in sites], now, args.since_days), indent=1))
        else:
            print(text)
    return 0


def _history(args, snaps):
    if not args.site:
        print("Error: history needs --site (a competitor id, as shown by `observatory plan`)")
        return 1
    if args.product:
        rows = []
        for entry in snaps.history(args.site, "catalogue"):
            snap = snaps.load(args.site, "catalogue", entry)
            for p in snap["products"]:
                if str(p["id"]) == args.product or p["handle"] == args.product:
                    rows.append({"captured_at": entry["captured_at"], "confirmed_at": entry["confirmed_at"], "price_min": p["price_min"], "compare_at_max": p["compare_at_max"],
                                 "available": p["available"], "on_sale": p["on_sale"], "title": p["title"]})
        if not rows:
            print(f"No reading of {args.product!r} was found for {args.site}.")
            return 1
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print(f"{r['captured_at'][:16]} (held to {r['confirmed_at'][:10]})  {r['title']}: {r['price_min']} (compare-at {r['compare_at_max']}), "
                      f"{'in stock' if r['available'] else 'SOLD OUT'}{', on sale' if r['on_sale'] else ''}")
        return 0
    entries = snaps.history(args.site, args.kind)
    if not entries:
        print(f"No readings of {args.kind} for {args.site}. Kinds kept: {', '.join(snaps.kinds(args.site)) or 'none'}")
        return 1
    for e in entries:
        print(f"{e['captured_at']}  held to {e['confirmed_at'][:10]}  {e['count']} item(s)  {e['bytes']} bytes  {'complete' if e['complete'] else 'INCOMPLETE'}  {e['file']}")
    return 0


def _prune(settings, snaps, now, quiet=False):
    removed = 0
    for site in snaps.sites():
        for kind in snaps.kinds(site):
            removed += len(snaps.prune(site, kind, settings["retention"], now))
    if quiet and not removed:
        return 0
    print(f"Retention: removed {removed} old reading(s) beyond the policy (recent days kept in full, then weekly, then monthly; the newest {settings['retention']['always_keep_latest']} always kept).")
    return 0


def _status(args, store, settings, brands, snaps, now):
    pauses = PauseLedger(args.data_dir)
    for b in brands:
        sites = report.read_brand_sites(store, b.slug)
        print(f"=== {b.name}: {len(sites)} site(s) watched ===")
        for s in sites:
            rows = monitor.stale_report(snaps, s["id"], now, settings)
            stale = [r["kind"] for r in rows if r["stale"]]
            print(f"  {s['domain']}: {len(rows)} reading kind(s); " + (f"STALE: {', '.join(stale)}" if stale else "all fresh"))
    resting = [r for r in pauses.rows(now) if r["resting"]]
    for r in resting:
        print(f"  RESTING {r['host']} until {r['until']}: {r['reason']}")
    if not resting:
        print("No site is resting.")
    return 0
