"""The `observatory` command: plan, monitor, report, history, prune, status.

Loaded only when the command is used (see cli.py), so a missing or damaged observatory folder can never stop the rest of the tool.
"""
import json
import os

from ..brands import BrandError, list_brand_slugs, load_brand
from ..contract import Failure
from . import config as OC
from . import corpus as corpus_mod
from . import discovery, monitor, report
from . import polite
from . import readers as readers_mod
from .pauses import PauseLedger
from .snapshots import SnapshotError, SnapshotStore

ACTIONS = ("plan", "discover", "decide", "monitor", "corpus", "report", "history", "prune", "status")
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
    p.add_argument("--what", default="youtube", choices=("youtube", "outliers", "search", "stats", "read"), help="corpus: what to do")
    p.add_argument("--query", default="", help="corpus search: the words to look for")
    p.add_argument("--url", default="", help="corpus read: a public page to read through the reader")
    p.add_argument("--channel", default="", help="corpus youtube: extra channels (handles like @name or ids), comma-separated")
    p.add_argument("--find-channels", action="store_true", help="corpus youtube: also search for channels using the brand's youtube.queries (each search costs 100 of the free 10,000 daily units)")
    p.add_argument("--no-lookup", action="store_true", help="discover: do not search for the websites of candidates that have none")
    p.add_argument("--no-probe", action="store_true", help="discover: do not check whether candidate sites are Shopify stores")
    p.add_argument("--research-config", default="research.yaml")


def _brands(args):
    slugs = list_brand_slugs(args.brands_dir) if args.all or not args.brand else [args.brand]
    return [load_brand(s, args.brands_dir) for s in slugs]


def _plan(args, store, settings, brands):
    discovered = {b.slug: report.discovered_competitors(store, b.slug) for b in brands}
    return monitor.plan_sites(brands, settings, root=args.brands_dir, data_dir=args.data_dir, discovered=discovered)


def run(args, store, ledger, now):
    try:
        settings = OC.load_settings(args.settings)
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
    if settings["engine"]["pages"] == "scrapling":
        from . import worker_client as wc
        page_reader = lambda comp, urls: wc.WorkerPages(comp, urls, settings, contact, os.environ.get("SCRAPLING_PYTHON", ""), snaps)   # noqa: E731
    results = monitor.run_sites(sites, make_client=make, snaps=snaps, settings=settings, hl=ledger, now=now, only=only, workers=max(1, args.workers), ids=ids or None,
                                page_reader=page_reader)
    pauses.save(now)
    for b in brands:
        mine = [slot["comp"] for sid, slot in sorted(sites.items()) if b.slug in slot["brands"]]
        report.write_brand_sites(store, b.slug, [{"id": c["id"], "name": c["name"], "domain": c["domain"], "origin": c["origin"], "kind": c["kind"]} for c in mine], now)
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
    return 1 if results and failed == len(results) else 0


def _lookup_domains(args, store, ledger, settings, brand, names, now):
    """{key: (domain, title)} for the names a search finds a matching website for. Uses the research search services within their caps."""
    from .. import providers as providers_mod
    from .. import research
    providers = PROVIDERS_FACTORY() if PROVIDERS_FACTORY else providers_mod.build_providers(os.environ)
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
    for brand in brands:
        book = report.read_discovery(store, brand.slug)
        entities = discovery.extract_entities(store, brand, now)
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
        elsewhere = len({c["id"] for c in report.read_brand_sites(store, brand.slug) if c.get("origin") != "discovery"})
        notes += discovery.merge(book, entities, scores, settings, now, probes=probes, monitored_elsewhere=elsewhere)
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
                                      + [c["youtube"] for c in report.discovered_competitors(store, brand.slug) if c.get("youtube")]
                                      + [c["youtube"] for sid, c in {s["id"]: s for s in report.read_brand_sites(store, brand.slug)}.items() if c.get("youtube")]))[:cfg["max_channels"]]
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
            hits = corpus_mod.search(docs, args.query, limit=10)
            print(f"{brand.name}: {len(hits)} match(es) for {args.query!r} in {len(docs)} item(s) of the research corpus")
            for h in hits:
                print(f"  {h['score']:6.2f}  [{h['source']}] {h['title'][:70]}  {h['url']}\n          {h['snippet'][:150]}")
        elif args.what == "read":
            code = _read_page(args, store, ledger, settings, brand, now)
            worst = max(worst, code)
    return worst


def _read_page(args, store, ledger, settings, brand, now):
    from .. import contract as K
    from .pauses import PauseLedger
    if not args.url:
        print("Error: read needs --url")
        return 1
    contact = (os.environ.get("OBSERVATORY_CONTACT") or settings["politeness"]["contact"]).strip()
    if not contact:
        print("Error: no contact address. Set OBSERVATORY_CONTACT.")
        return 1
    pauses = PauseLedger(args.data_dir)
    client = CLIENT_FACTORY() if CLIENT_FACTORY else polite.PoliteClient(settings, contact=contact, ledger=pauses, now_fn=lambda: now)
    rd = readers_mod.JinaReader(settings, client, http=READER_HTTP or polite.default_http)
    env = K.run_action("competitor_pages", "read_via_reader", [("reader", lambda: [rd.read(args.url)])], health=ledger, now=now)
    pauses.save(now)
    if not env["ok"]:
        print(f"Error: {env['error']['code']}: {env['error']['message']}")
        return 1
    page = env["items"][0]
    path = store.dir(brand.slug) / "corpus" / "reader.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**page, "captured_at": now}, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{page['title'] or page['url']}\n  {len(page['text'])} characters read through the reader" + (f"\n  NOTE: {page['robots_note']}" if page["robots_note"] else ""))
    print(page["text"][:600])
    return 0


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


def _prune(settings, snaps, now):
    removed = 0
    for site in snaps.sites():
        for kind in snaps.kinds(site):
            removed += len(snaps.prune(site, kind, settings["retention"], now))
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

