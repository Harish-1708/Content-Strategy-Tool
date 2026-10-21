"""Command line: python -m contentintel <command>.

Exit codes: 0 ok or warn, 1 error, 2 sync blocked, 3 baseline needs confirmation.
"""
import argparse
import json
import secrets
import os

from . import autoseeds as autoseeds_mod
from . import browser as browser_mod
from .apify import Apify
from .claude_client import Claude
from . import collectors as collectors_mod
from . import providers as providers_mod
from . import research as research_mod
from . import sheets as sheets_mod
from . import sync as sync_mod
from .brands import BrandError, list_brand_slugs, load_brand
from .shopify_fetch import FetchError, fetch_sitemap_handles
from .store import Store, StoreError

EXIT = {"ok": 0, "warn": 0, "blocked": 2, "needs_baseline": 3}


def _common(p):
    p.add_argument("--data-dir", default="data")
    p.add_argument("--brands-dir", default="brands")


def build_parser():
    parser = argparse.ArgumentParser(prog="contentintel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="fetch a brand catalogue and check it")
    _common(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--from-file", help="use a saved products.json instead of fetching")
    p.add_argument("--sitemap-file", help="saved sitemap handles (one per line) for the cross-check")
    p.add_argument("--sitemap", action="store_true", help="cross-check against the live sitemap")

    p = sub.add_parser("confirm-baseline", help="a person confirms the product count")
    _common(p)
    p.add_argument("--brand", required=True)
    p.add_argument("--count", required=True, type=int)
    p.add_argument("--by", required=True)

    p = sub.add_parser("reset-baseline", help="retire the baseline on purpose")
    _common(p)
    p.add_argument("--brand", required=True)
    p.add_argument("--by", required=True)

    p = sub.add_parser("confirm-removed", help="confirm a suspected product removal")
    _common(p)
    p.add_argument("--brand", required=True)
    p.add_argument("--product-id", required=True)
    p.add_argument("--by", required=True)

    p = sub.add_parser("sheets", help="update each brand's Google Sheet from the catalogue data")
    _common(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p.add_argument("--sheets-config", default="sheets.yaml")

    p = sub.add_parser("research", help="search the web for each brand, within your credit caps")
    _common(p)
    group = p.add_mutually_exclusive_group(required=False)
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--check-providers", action="store_true",
                   help="prove each service works with one web and one news search (a few credits)")
    p.add_argument("--providers", default="",
                   help="comma-separated subset of serper_dev, tavily, serpapi (default: all with a key)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan and which provider would take each query; spend nothing")
    p.add_argument("--max-queries", type=int, default=0, help="override the per-run limit")
    p.add_argument("--compare", action="store_true",
                   help="run each of the first few searches on EVERY chosen service and compare")
    p.add_argument("--compare-queries", type=int, default=0,
                   help="how many searches compare mode may run on every service (default 5)")
    p.add_argument("--research-config", default="research.yaml")

    p = sub.add_parser("collect", help="collect competitor ads, social posts and deep research with a browser")
    _common(p)
    group = p.add_mutually_exclusive_group(required=False)
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--sources", default="",
                   help="comma-separated: meta_ads, tiktok, instagram, deep_research "
                        "(default: the first three; Deep Research takes 10+ minutes and uses the account's quota, so name it)")
    p.add_argument("--dry-run", action="store_true", help="show the plan; open no browser")
    p.add_argument("--check", action="store_true",
                   help="open each site, confirm the login, and collect a handful of items")
    p.add_argument("--recon", action="store_true",
                   help="save pictures, page HTML and data samples from each site, for tuning browser.yaml")
    p.add_argument("--seeds", action="store_true",
                   help="write (or refresh) each brand's auto seeds with Claude and print them, without collecting")
    p.add_argument("--test-deep-research", action="store_true",
                   help="run ONE real, short Deep Research report to prove the whole path (takes 10+ minutes)")
    p.add_argument("--backend", choices=collectors_mod.BACKENDS, default="auto",
                   help="browser, apify, fallback (browser first, Apify if it fails), or auto (as research.yaml says)")
    p.add_argument("--research-config", default="research.yaml")
    p.add_argument("--browser-config", default="browser.yaml")
    p.add_argument("--debug-dir", default="debug")

    p = sub.add_parser("status", help="can generation use this catalogue right now?")
    _common(p)
    p.add_argument("--brand", required=True)
    return parser


def _sync_one(args, slug, store):
    brand = load_brand(slug, args.brands_dir)
    raw = None
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data["products"] if isinstance(data, dict) else data
    handles = None
    if args.sitemap_file:
        with open(args.sitemap_file, encoding="utf-8") as fh:
            handles = [line.strip() for line in fh if line.strip()]
    elif args.sitemap and raw is None:
        try:
            handles = fetch_sitemap_handles(brand.domain)
        except FetchError as exc:
            print(f"Sitemap cross-check skipped: {exc}")
    result = sync_mod.run_sync(brand, store, raw_products=raw, sitemap_handles=handles)
    print(sync_mod.format_report(result))
    if result.state == "warn":
        print(f"::warning::{slug}: sync finished with warnings")
    return EXIT[result.state]


def _sheets(args, store):
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
    brands = [load_brand(slug, args.brands_dir) for slug in slugs]
    if not key.strip():
        print("Google Sheets update skipped: the GOOGLE_SERVICE_ACCOUNT_JSON secret is not set.")
        print("::warning::Google Sheets update skipped: GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        return 0
    config = sheets_mod.load_sheet_config(args.sheets_config, set(list_brand_slugs(args.brands_dir)))
    results = sheets_mod.run_sheets(
        brands, store, key_json=key, config=config, now=sync_mod.utc_now(),
        share_emails=sheets_mod.parse_emails(os.environ.get("SHEET_SHARE_EMAIL", "")),
        folder_id=sheets_mod.parse_folder_id(os.environ.get("SHEET_FOLDER_ID", "")),
        dry_run=args.dry_run, connect_fn=sheets_mod.connect)
    worst = 0
    for slug, report, error in results:
        if error:
            worst = 1
            print(f"{slug}: FAILED. {error}")
            print(f"::error::{slug}: Google Sheets update failed")
        else:
            print("\n".join(report.lines()))
    return worst


def _research(args, store):
    settings = research_mod.load_settings(args.research_config)
    wanted = [n.strip() for n in args.providers.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in providers_mod.PROVIDER_NAMES]
    if unknown:
        print(f"Error: unknown provider {', '.join(unknown)} "
              f"(choose from {', '.join(providers_mod.PROVIDER_NAMES)})")
        return 1
    available = providers_mod.build_providers(os.environ)
    for name in wanted:
        if name not in available:
            print(f"::warning::{name} was asked for but its API key is not set; leaving it out")
    providers = {n: p for n, p in available.items() if not wanted or n in wanted}
    if not providers:
        print("Research skipped: no search API key is set (TAVILY_API_KEY, SERPER_API_KEY, "
              "SERPAPI_API_KEY).")
        print("::warning::Research skipped: no search API key is set")
        return 0
    if args.check_providers:
        rows = research_mod.check_providers(providers, store, settings, sync_mod.utc_now())
        print(research_mod.format_check_report(rows))
        for r in rows:
            if not r["ok"] and not r["problem"].startswith("skipped"):
                print(f"::error::{r['provider']} {r['kind']} search failed: {r['problem']}")
        return 0 if all(r["ok"] or r["problem"].startswith("skipped") for r in rows) else 1
    if not (args.all or args.brand):
        print("Error: choose --brand NAME, --all, or --check-providers")
        return 1
    slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
    brands = [load_brand(slug, args.brands_dir) for slug in slugs]
    result = research_mod.run_research(
        brands, store, providers=providers, settings=settings, now=sync_mod.utc_now(),
        dry_run=args.dry_run, max_queries=args.max_queries or None, root=args.brands_dir,
        compare=args.compare, compare_limit=args.compare_queries or None)
    if args.compare and len(providers) < 2:
        print("::warning::Compare mode needs at least two services; running normally")
    print(research_mod.format_research_report(result))
    for name in result.notes_with_problems:
        print(f"::error::{name}: the API key was rejected. Check the repository secret.")
    for slug, rep in result.brands.items():
        if rep.failed:
            print(f"::error::{slug}: {len(rep.failed)} searches failed")
        if rep.skipped or rep.deferred:
            print(f"::warning::{slug}: {len(rep.skipped)} searches skipped for credits, "
                  f"{len(rep.deferred)} left for the next run")
    return 1 if result.problems else 0


_BROWSER_FACTORY = None       # tests replace this; None means a real Chromium through Playwright


def _site_creds(browser_cfg, env):
    """Each site's cookies / username / password, read from the secrets named in browser.yaml."""
    return {site: {k: ((env.get(name) or "").strip() or None) for k, name in cfg.get("secrets", {}).items()}
            for site, cfg in browser_cfg["sites"].items()}


def _real_factory(browser_cfg, creds, debug_dir):
    def factory(site):
        return browser_mod.open_browser(site, browser_cfg["sites"][site], browser_cfg["pacing"],
                                        creds[site].get("cookies") or "", debug_dir)
    return factory


EXTRA_SECRETS = ("ANTHROPIC_API_KEY", "APIFY_TOKEN")


def _secrets_status(browser_cfg, env):
    """(name, set or not set) for every secret the collectors can use. Never the values."""
    names = [n for cfg in browser_cfg["sites"].values() for n in cfg.get("secrets", {}).values()] + list(EXTRA_SECRETS)
    return [(n, "set" if (env.get(n) or "").strip() else "not set") for n in names]


def _collect(args, store):
    settings = collectors_mod.load_collector_settings(args.research_config)
    browser_cfg = browser_mod.load_browser_settings(args.browser_config)
    wanted = [n.strip() for n in args.sources.split(",") if n.strip()] or list(collectors_mod.BROWSER_SOURCES)
    unknown = [n for n in wanted if n not in collectors_mod.SOURCES]
    if unknown:
        print(f"Error: unknown source {', '.join(unknown)} (choose from {', '.join(collectors_mod.SOURCES)})")
        return 1
    creds = _site_creds(browser_cfg, os.environ)
    apify_token, claude_key = (os.environ.get("APIFY_TOKEN") or "").strip(), (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    apify = Apify(apify_token) if apify_token else None
    claude = Claude(claude_key, model=settings.claude_model, max_tokens=settings.claude_max_tokens) if claude_key else None
    backend = collectors_mod.effective_backend(args.backend, settings)
    print("Secrets this run can see (names only; values are never shown):")
    for name, state in _secrets_status(browser_cfg, os.environ):
        print(f"  {name:<22} {state}")
    print(f"Backend for ads and social: {backend}\n")

    def usable(source):
        site = collectors_mod.SITE_OF[source]
        browser_ok = collectors_mod.has_access(browser_cfg["sites"][site], creds[site])
        if source in collectors_mod.BROWSER_SOURCES and backend == "apify":
            return apify is not None
        if source in collectors_mod.BROWSER_SOURCES and backend == "fallback":
            return browser_ok or apify is not None
        return browser_ok

    make = _BROWSER_FACTORY or _real_factory
    factory = make(browser_cfg, creds, args.debug_dir)
    now = sync_mod.utc_now()
    if args.seeds:
        ledger = research_mod.Ledger(store.load_usage(), now[:7])
        slugs = [args.brand] if args.brand else list_brand_slugs(args.brands_dir)
        for slug in slugs:
            brand = load_brand(slug, args.brands_dir)
            result = autoseeds_mod.ensure_auto_seeds(brand, store, claude, settings, ledger, now, False, force=True)
            print(autoseeds_mod.format_seeds(slug, result))
            if result.seeds is not None:
                print(collectors_mod.format_benchmark_status(brand, store, result.seeds, settings, now, args.brands_dir))
            print()
        return 0
    if args.test_deep_research:
        outcome = collectors_mod.run_deep_research_test(factory, creds, browser_cfg, settings, store, research_mod.Ledger,
                                                        now, claude, args.debug_dir)
        print(f"Deep Research test: {'OK' if outcome['ok'] else 'FAILED'}: {outcome['detail']}")
        if not outcome["ok"] and not outcome["detail"].startswith("no login"):
            print(f"::error::Deep Research test failed: {outcome['detail']}")
            return 1
        return 0
    if args.recon:
        lines = collectors_mod.recon(factory, creds, browser_cfg, wanted, args.debug_dir)
        print("\n".join(lines))
        for line in lines:
            if "nothing readable" in line:
                print(f"::warning::Recon read nothing from {line.split(':')[0].strip()}: open the debug artifact")
        return 0
    if args.check:
        rows = collectors_mod.check_collectors(factory, creds, settings, browser_cfg, store, research_mod.Ledger, now,
                                               wanted, apify=apify, claude=claude, backend=backend)
        print(collectors_mod.format_check_collectors(rows))
        bad = [r for r in rows if not r["ok"] and not r["detail"].startswith(("skipped", "no login", "no key"))]
        for r in bad:
            print(f"::error::{r['source']} check failed: {r['detail']}")
        return 1 if bad else 0
    if not [s for s in wanted if usable(s)]:
        print("Collection skipped: no login or key is set for any chosen source.")
        for s in wanted:
            site = collectors_mod.SITE_OF[s]
            print(f"  {s}: " + ("no key: set APIFY_TOKEN" if backend == "apify" and s in collectors_mod.BROWSER_SOURCES
                                else collectors_mod.missing_access_message(browser_cfg["sites"][site])))
        print("::warning::Collection skipped: no login or key is set")
        return 0
    if not (args.all or args.brand):
        print("Error: choose --brand NAME, --all, --check, --recon, --seeds or --test-deep-research")
        return 1
    slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
    brands = [load_brand(slug, args.brands_dir) for slug in slugs]
    result = collectors_mod.run_collect(
        brands, store, browser_factory=factory, creds=creds, settings=settings, browser_cfg=browser_cfg,
        ledger_cls=research_mod.Ledger, sources=wanted, now=now, run_id=secrets.token_hex(4),
        dry_run=args.dry_run, root=args.brands_dir, apify=apify, claude=claude, backend=backend)
    ledger = None if args.dry_run else research_mod.Ledger(store.load_usage(), now[:7])
    print(collectors_mod.format_collect_report(result, settings, ledger))
    for name in result.problems_for:
        print(f"::error::{name}: the login did not work. Re-export the dummy account's cookies (see browser.yaml)."
              if name != "apify" else "::error::apify: the token was rejected. Check the APIFY_TOKEN secret.")
    for slug, reports in result.brands.items():
        failed = [r for r in reports if r.status == "failed"]
        if failed:
            print(f"::error::{slug}: {len(failed)} collection tasks failed")
    return 1 if result.problems else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = Store(args.data_dir)
    try:
        if args.cmd == "sync":
            slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
            if args.all and args.from_file:
                print("--from-file needs a single --brand")
                return 1
            worst = 0
            for slug in slugs:
                print(f"=== {slug} ===")
                worst = max(worst, _sync_one(args, slug, store))
                print()
            return worst
        if args.cmd == "confirm-baseline":
            baseline, _ = sync_mod.confirm_baseline(args.brand, store, args.count, args.by)
            print(f"Baseline confirmed: {baseline['count']} products by {baseline['confirmed_by']}.")
            return 0
        if args.cmd == "reset-baseline":
            old = sync_mod.reset_baseline(args.brand, store, args.by)
            print(f"Baseline of {old['count']} products retired. The next clean sync needs confirming.")
            return 0
        if args.cmd == "confirm-removed":
            entry = sync_mod.confirm_removed(args.brand, store, args.product_id, args.by)
            print(f"Removal confirmed for {entry['title']}.")
            return 0
        if args.cmd == "sheets":
            return _sheets(args, store)
        if args.cmd == "research":
            return _research(args, store)
        if args.cmd == "collect":
            return _collect(args, store)
        if args.cmd == "status":
            state = sync_mod.catalogue_state(args.brand, store)
            print(json.dumps(state, indent=1))
            return 0 if state["usable"] else 2
    except (BrandError, StoreError, sync_mod.BaselineError, FetchError, sheets_mod.SheetsError,
            research_mod.ResearchError, collectors_mod.CollectError, browser_mod.BrowserError, KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    return 1
