"""Command line: python -m contentintel <command>.

Exit codes: 0 ok or warn, 1 error, 2 sync blocked, 3 baseline needs confirmation.
"""
import argparse
import json
import os

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
    p.add_argument("--research-config", default="research.yaml")

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
        dry_run=args.dry_run, max_queries=args.max_queries or None, root=args.brands_dir)
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
        if args.cmd == "status":
            state = sync_mod.catalogue_state(args.brand, store)
            print(json.dumps(state, indent=1))
            return 0 if state["usable"] else 2
    except (BrandError, StoreError, sync_mod.BaselineError, FetchError, sheets_mod.SheetsError,
            research_mod.ResearchError, KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    return 1
