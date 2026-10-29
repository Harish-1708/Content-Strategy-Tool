"""Command line: python -m contentintel <command>.

Exit codes: 0 ok or warn, 1 error, 2 sync blocked, 3 baseline needs confirmation.
"""
import argparse
import copy
import dataclasses
import json
import secrets
from pathlib import Path
import os

from . import autoseeds as autoseeds_mod
from . import browser as browser_mod
from .apify import Apify
from .claude_client import Claude
from . import brief as brief_mod
from . import collectors as collectors_mod
from . import drive as drive_mod
from . import ideabank as ib_mod
from . import planner as planner_mod
from . import rules as rules_mod
from . import docs_archive as docs_mod
from . import providers as providers_mod
from . import research as research_mod
from . import sheets as sheets_mod
from . import sync as sync_mod
from . import videofetch as videofetch_mod
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

    p = sub.add_parser("docs", help="add each new Deep Research report to the Google Doc, one tab per report")
    _common(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--check", action="store_true", help="prove the Doc can be written (adds a test tab and removes it)")
    p.add_argument("--dry-run", action="store_true", help="show which tabs would be added; write nothing")

    p = sub.add_parser("ideabank", help="take new sourced items into each brand's Idea Bank: score them and save their files in Drive")
    _common(p)
    p.add_argument("--research-config", default="research.yaml")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--check", action="store_true", help="prove the Drive folder can be written (makes a test folder and removes it)")
    p.add_argument("--dry-run", action="store_true", help="say how many items would be added; change nothing")
    p.add_argument("--sheets-config", default="sheets.yaml", help="ideabank: where the brand's sheet is, to read the Rejected tab's Rescue marks and your Decisions")
    p.add_argument("--debug-dir", default="debug")
    p.add_argument("--browser-config", default="browser.yaml", help="ideabank: the browser settings, used to fetch a TikTok or Instagram video through the login when yt-dlp is refused")
    p.add_argument("--audit", action="store_true", help="download every saved file back from Drive and check it again (whole, readable, right type)")

    p = sub.add_parser("request", help="advanced mode: plan a request in your own words, then run it")
    _common(p)
    p.add_argument("action", choices=["plan", "run"])
    p.add_argument("--brand", required=True)
    p.add_argument("--research-config", default="research.yaml")
    p.add_argument("--browser-config", default="browser.yaml")
    p.add_argument("--sheets-config", default="sheets.yaml", help="run: where the brand's sheet is, to protect the rows you approved and read your rescues")
    p.add_argument("--debug-dir", default="debug")
    p.add_argument("--text", default="", help="plan: what you want, in your own words")
    p.add_argument("--tools", choices=["auto", "manual"], default="auto", help="plan: let Claude choose the tools, or choose them yourself")
    p.add_argument("--allow", default="", help="plan, with --tools manual: the tools allowed, comma separated (" + ", ".join(planner_mod.TOOLS) + ")")
    p.add_argument("--region", default="", help="plan: worldwide (the default) or a two-letter country code")
    p.add_argument("--apify-extra", action="store_true", help="plan: run Apify beside the browser for this request")
    p.add_argument("--formats", default="", help="plan: the kinds of content to look for, comma separated: " + ", ".join(planner_mod.FORMATS)
                   + " (words such as posts, reels, creators and AI content are understood)")
    p.add_argument("--request", default="", help="run: the request id, for example RQ-0001")
    p.add_argument("--changes", default="", help="run: changes to make to the plan before it runs")

    p = sub.add_parser("brief", help="write a creative brief from the Idea Bank rows marked Use")
    _common(p)
    p.add_argument("--brand", required=True)
    p.add_argument("--brief", required=True, help="the rules folder name under briefs/, for example swoveralls-new-audience")
    p.add_argument("--ask", default="", help="what the brief is for, in your own words")
    p.add_argument("--request", default="", help="only use items found by this request")
    p.add_argument("--include-maybe", action="store_true", help="also use rows marked Maybe")
    p.add_argument("--research-config", default="research.yaml")
    p.add_argument("--sheets-config", default="sheets.yaml")
    p.add_argument("--briefs-dir", default="briefs")

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
    p.add_argument("--region", default="", help="worldwide or a two-letter country code, for this run only")
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
    p.add_argument("--region", default="", help="worldwide or a two-letter country code for the ad searches, for this run only")
    p.add_argument("--rest", default="",
                   help="rest these accounts now (instagram, tiktok, gemini), without opening them: use it when a warning was seen on the site")
    p.add_argument("--rest-days", type=int, default=0, help="how many days --rest lasts (0: the setting for each site)")
    p.add_argument("--ignore-cooldown", action="store_true",
                   help="use accounts that are resting after a warning (do this only once you have dealt with the warning)")
    p.add_argument("--backend", choices=collectors_mod.BACKENDS, default="auto",
                   help="browser, apify, fallback (browser first, Apify if it fails), extra (both, merged), or auto (as research.yaml says)")
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


_DOCS_CONNECT = None            # tests replace this; None means a real connection to Google
_DRIVE_CONNECT = None           # likewise for Google Drive
_HTTP_GET = None                # likewise for downloading pictures and videos; None means requests.get
_SHEETS_CONNECT = None          # likewise for reading the Idea Bank decisions


def _claude(settings, model=None):
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return Claude(key, model=model or settings.claude_model, max_tokens=settings.claude_max_tokens,
                  prompt_max_tokens=settings.claude_prompt_max_tokens) if key else None


def _drive():
    """A Drive client for the Idea Bank folder, or None (with the reason) when the secrets are not set."""
    key, raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""), os.environ.get("IDEA_BANK_FOLDER_ID", "")
    folder = drive_mod.parse_folder_id(raw)
    if not folder or not key.strip():
        return None, "IDEA_BANK_FOLDER_ID" if not folder else "GOOGLE_SERVICE_ACCOUNT_JSON"
    session, email = (_DRIVE_CONNECT or drive_mod.connect)(key)
    return drive_mod.DriveClient(session, folder, email), ""


def _get():
    if _HTTP_GET is not None:
        return _HTTP_GET
    import requests
    return requests.get


_VIDEO_RUNNER = None                  # tests put a fake here


def _video_browser_factory(args):
    """The logged-in browser as a second route to a post's video, if there is a login to use (None if not)."""
    path = getattr(args, "browser_config", None)
    if not path:
        return None
    try:
        browser_cfg = browser_mod.load_browser_settings(path)
        creds = _site_creds(browser_cfg, os.environ)
    except Exception:                                                     # no browser settings: yt-dlp alone is still tried
        return None
    if not any((creds.get(site) or {}).get("cookies") for site in ("tiktok", "instagram") if site in creds):
        return None
    return lambda site: (_BROWSER_FACTORY or _real_factory)(browser_cfg, creds, getattr(args, "debug_dir", None) or "debug")(site)


def _ad_browser_factory(args):
    """The browser, for fetching a fresh picture or video link from a Meta ad's own Ad Library page (None if its settings cannot be read)."""
    path = getattr(args, "browser_config", None)
    if not path:
        return None
    try:
        browser_cfg = browser_mod.load_browser_settings(path)
        creds = _site_creds(browser_cfg, os.environ)
    except Exception:
        return None
    return lambda site: (_BROWSER_FACTORY or _real_factory)(browser_cfg, creds, getattr(args, "debug_dir", None) or "debug")(site)


def _video_runner(args=None):
    """The function that fetches a post's real video: yt-dlp first, then the logged-in browser if the platform refuses yt-dlp. None when
    neither is available (the cover picture is kept)."""
    if _VIDEO_RUNNER is not None:
        return _VIDEO_RUNNER
    import shutil
    ytdlp = videofetch_mod.ytdlp_download if shutil.which("yt-dlp") else None
    factory = _video_browser_factory(args) if args is not None else None
    return videofetch_mod.layered_runner(factory=factory, ytdlp=ytdlp) if (ytdlp or factory) else None


def _read_sheet(what, reader, brand, store, args):
    """(what the sheet says, a note). Never an error that stops the run: the sheet may not be set up yet, or Google may hiccup. A mistake in this
    program (a missing option, a wrong call) is NOT hidden: it is raised, so it shows in the first run and in the tests."""
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not key or getattr(args, "dry_run", False):
        return None, ""
    try:
        config = sheets_mod.load_sheet_config(args.sheets_config, set(list_brand_slugs(args.brands_dir)))
        if not sheets_mod.has_spreadsheet(brand, store, config):          # nothing to read, and reading must never create a spreadsheet
            return None, ""
        return reader(brand, store, key_json=key, config=config, share_emails=[], folder_id=None, connect_fn=_SHEETS_CONNECT or sheets_mod.connect), ""
    except (AttributeError, TypeError, NameError):
        raise
    except Exception as exc:
        return None, f"{brand.slug}: {what} could not be read ({exc.__class__.__name__}: {str(exc)[:100]})"


def _rescues(brand, store, args):
    """(ids a person marked Use or Maybe on the Rejected tab, a note)."""
    got, note = _read_sheet("the Rejected tab (for rescues)", sheets_mod.fetch_rescues, brand, store, args)
    return got or [], note


def _decided(brand, store, args):
    """(ids a person marked Use or Maybe on the Idea Bank tab, a note): the tool never removes what a person has approved. None, not an empty list,
    when the sheet could not be read: 'nobody is approved' and 'we do not know' are different, and only the first may lift a protection."""
    got, note = _read_sheet("your Decisions", sheets_mod.fetch_decisions, brand, store, args)
    return (None if got is None else sorted(i for i, d in got.items() if d.get("decision") in ("use", "maybe"))), note


def _cookie_secrets():
    return {"instagram": os.environ.get("INSTAGRAM_COOKIES", ""), "tiktok": os.environ.get("TIKTOK_COOKIES", "")}


def _skipped_brands(brand, brands_dir):
    """The brand names the owner marked `skip` under ads.benchmark_pages in the brand's research.yaml."""
    pages = research_mod.load_brand_research(brand, root=brands_dir)["collect"]["ads"]["benchmark_pages"]
    return [name for name, value in pages.items() if value == "skip"]


def _ideabank(args, store):
    settings = ib_mod.load_settings(args.research_config)
    csettings = collectors_mod.load_collector_settings(args.research_config)
    drive, missing = _drive()
    if args.check:
        if drive is None:
            print(f"Idea Bank Drive check skipped: the {missing} secret is not set.")
            return 0
        facts = drive.check()
        if facts["left_behind"]:
            print(f"Idea Bank Drive works: the folder '{facts['name']}' is in a Shared Drive and a test folder could be made in it, but the "
                  f"test folder could not be removed ({facts['left_behind']}) Delete the folder called '{drive_mod.TEST_FOLDER}' by hand. "
                  f"Saving files is not affected, because the tool never deletes anything. Shared with {drive.email}.")
        else:
            print(f"Idea Bank Drive works: the folder '{facts['name']}' is in a Shared Drive, and a test folder was made and removed. "
                  f"Shared with {drive.email}.")
        return 0
    if not settings.enabled:
        print("The Idea Bank is switched off (ideabank.enabled in research.yaml).")
        return 0
    if not (args.all or args.brand):
        print("Error: choose --brand NAME, --all or --check")
        return 1
    if drive is None:
        print(f"Files will not be saved in Drive: the {missing} secret is not set.")
    now = sync_mod.utc_now()
    claude = _claude(csettings)
    budget = ib_mod.ClaudeBudget(store, now, settings.claude_monthly_calls) if claude else None
    for slug in list_brand_slugs(args.brands_dir) if args.all else [args.brand]:
        brand = load_brand(slug, args.brands_dir)
        rescues, note = _rescues(brand, store, args)
        protected, note2 = _decided(brand, store, args)
        _, lines = ib_mod.run_ideabank(brand, store, settings=settings, now=now, claude=claude, drive=drive, get=_get(),
                                       dry_run=args.dry_run, budget=budget, skip_brands=_skipped_brands(brand, args.brands_dir),
                                       audit=args.audit, video_runner=_video_runner(args), cookies=_cookie_secrets(), rescues=rescues, protected=protected,
                                       browser_factory=_ad_browser_factory(args))
        print("\n".join([n for n in (note, note2) if n] + lines))
    return 0


def _request(args, store):
    brand = load_brand(args.brand, args.brands_dir)
    settings = ib_mod.load_settings(args.research_config)
    csettings = collectors_mod.load_collector_settings(args.research_config)
    now = sync_mod.utc_now()
    claude = _claude(csettings)
    budget = ib_mod.ClaudeBudget(store, now, settings.claude_monthly_calls)
    context = ib_mod.brand_context(brand, store)
    try:
        if args.action == "plan":
            region = collectors_mod.ads_country(args.region, "--region") if args.region.strip() else "ALL"
            region = "worldwide" if region == "ALL" else region.lower()
            ticked = [t.strip() for t in args.allow.split(",") if t.strip()]
            request = planner_mod.plan_request(store, brand, claude, budget, settings, text=args.text, mode=args.tools, ticked=ticked,
                                               region=region, now=now, apify_extra=args.apify_extra, context=context,
                                               formats=args.formats)
            print("\n".join(planner_mod.render_plan(request, planner_mod.estimate(request["plan"], settings))))
            print(f"\nNothing has been searched yet. To run it as it is: run mode of the Request workflow with request id {request['id']}. "
                  "To change it first, put what you want changed in the changes box.")
            return 0
        request = store.read_request(brand.slug, args.request)
        if request is None:
            print(f"Error: there is no request {args.request or '(none given)'} for {brand.slug}")
            return 1
        request = planner_mod.revise_request(store, brand, request, claude, budget, settings, changes=args.changes, now=now, context=context)
        if args.changes.strip():
            print("The plan after your changes:")
            print("\n".join(planner_mod.render_plan(request, planner_mod.estimate(request["plan"], settings))))
        return _run_request(args, store, brand, request, settings, csettings, claude, budget, now)
    except (planner_mod.PlanError, drive_mod.DriveError) as exc:
        print(f"Error: {exc}")
        return 1


def _run_request(args, store, brand, request, settings, csettings, claude, budget, now):
    rsettings = dataclasses.replace(research_mod.load_settings(args.research_config), region_override=research_mod._country(request["region"], "region"))
    browser_cfg = browser_mod.load_browser_settings(args.browser_config)
    creds = _site_creds(browser_cfg, os.environ)
    apify_token = (os.environ.get("APIFY_TOKEN") or "").strip()
    apify = Apify(apify_token) if apify_token else None
    factory = (_BROWSER_FACTORY or _real_factory)(browser_cfg, creds, args.debug_dir)
    drive, missing = _drive()
    started = now

    def collect_runner(cfg_raw, sources):
        cfg = collectors_mod.parse_collect_config(cfg_raw, f"{brand.slug} request {request['id']}")
        backend = "extra" if request["apify_extra"] and apify else collectors_mod.effective_backend("auto", csettings)
        result = collectors_mod.run_collect(
            [brand], store, browser_factory=factory, creds=creds, settings=dataclasses.replace(csettings, benchmarks_enabled=False, auto_enabled=False),
            browser_cfg=browser_cfg, ledger_cls=research_mod.Ledger, sources=sources, now=now, run_id=secrets.token_hex(4),
            configs={brand.slug: cfg}, root=args.brands_dir, apify=apify, claude=claude, backend=backend)
        return [f"{r.source} {r.status}: {r.detail}"[:160] for r in result.brands.get(brand.slug, [])]

    def research_runner(queries):
        providers = providers_mod.build_providers(os.environ)
        if not providers:
            raise RuntimeError("no search API key is set (TAVILY_API_KEY, SERPER_API_KEY, SERPAPI_API_KEY)")
        base = copy.deepcopy(research_mod.load_brand_research(brand, args.brands_dir))
        base["queries"] = [{"q": q} for q in queries]
        base["auto"] = {"brand": False, "competitors": False, "topics": False}
        result = research_mod.run_research([brand], store, providers=providers, settings=rsettings, now=now, configs={brand.slug: base},
                                           root=args.brands_dir)
        rep = result.brands[brand.slug]
        return [f"{len(queries)} searches planned, {len(rep.failed)} failed"]

    def ideabank_runner(req, queries):
        web = planner_mod.web_sources_for(research_mod.research_sources(store.read_searches(brand.slug)), queries) if queries else None
        rescues, note = _rescues(brand, store, args)
        protected, note2 = _decided(brand, store, args)
        new_items, lines = ib_mod.run_ideabank(brand, store, settings=settings, now=now, claude=claude, drive=drive, get=_get(),
                                   request={"id": req["id"], "region": req["region"], "occasion": req.get("occasion", ""), "text": req["text"],
                                            "formats": req.get("formats") or []},
                                   web_sources=web, budget=budget if claude else None, since=started,
                                   skip_brands=_skipped_brands(brand, args.brands_dir), video_runner=_video_runner(args), cookies=_cookie_secrets(),
                                   rescues=rescues, protected=protected, browser_factory=_ad_browser_factory(args))
        return new_items, [n for n in (note, note2) if n] + lines

    done = planner_mod.execute_request(store, brand, request, now=now, collect_runner=collect_runner, research_runner=research_runner,
                                       ideabank_runner=ideabank_runner)
    print(f"Request {done['id']} finished: {done['status']}")
    print("\n".join(done["run"]["notes"]))
    print(f"{len(done['run']['items_added'])} item(s) are now in the Idea Bank. Run the Sheets update, review the Idea Bank tab, "
          "mark the rows you want as Use, then run the Brief workflow.")
    return 0 if done["status"] in ("done", "partial") else 1


def _brief(args, store):
    brand = load_brand(args.brand, args.brands_dir)
    path = Path(args.briefs_dir) / args.brief / "rules.yaml"
    if not path.is_file():
        print(f"Error: there is no rules file {path}")
        return 1
    ruleset = rules_mod.load_ruleset(path)
    if ruleset.brand != brand.slug:
        print(f"Error: the brief '{args.brief}' belongs to {ruleset.brand}, not {brand.slug}")
        return 1
    settings = ib_mod.load_settings(args.research_config)
    csettings = collectors_mod.load_collector_settings(args.research_config)
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        print("Error: writing a brief needs the ANTHROPIC_API_KEY secret")
        return 1
    if not key.strip():
        print("Error: the approved rows are read from the Google Sheet, so the GOOGLE_SERVICE_ACCOUNT_JSON secret is needed")
        return 1
    now = sync_mod.utc_now()
    config = sheets_mod.load_sheet_config(args.sheets_config, set(list_brand_slugs(args.brands_dir)))
    try:
        decisions = sheets_mod.fetch_decisions(
            brand, store, key_json=key, config=config, share_emails=sheets_mod.parse_emails(os.environ.get("SHEET_SHARE_EMAIL", "")),
            folder_id=sheets_mod.parse_folder_id(os.environ.get("SHEET_FOLDER_ID", "")), connect_fn=_SHEETS_CONNECT or sheets_mod.connect)
        drive, missing = _drive()
        if drive is None:
            print(f"Pictures will not be placed in the brief and the file will not be saved in Drive: the {missing} secret is not set.")
        record = brief_mod.make_brief(
            brand, store, brief_name=args.brief, ask=args.ask, ruleset=ruleset, decisions=decisions,
            claude_for=lambda model: _claude(csettings, model), settings=settings,
            budget=ib_mod.ClaudeBudget(store, now, settings.claude_monthly_calls), now=now, drive=drive,
            include_maybe=args.include_maybe, request_id=args.request.strip())
    except (brief_mod.BriefError, drive_mod.DriveError, sheets_mod.SheetsError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Brief {record['id']} ({record['status']}): {record['title']}")
    print(f"  built from {len(record['items'])} approved item(s); {len(record['violations'])} rule problem(s) left; model: {record['models'][0]}")
    print(f"  saved in the repository as data/{brand.slug}/briefs/{record['id']}.docx" + (f" and in Drive: {record['drive_link']}" if record["drive_link"] else ""))
    for note in record["notes"]:
        print(f"  note: {note}")
    for v in record["violations"]:
        print(f"  {v['severity']}: {v['where']}: {v['message']}")
    return 0


def _docs(args, store):
    key, raw_id = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""), os.environ.get("DEEP_RESEARCH_DOC_ID", "")
    doc_id = docs_mod.parse_doc_id(raw_id)
    if not doc_id or not key.strip():
        missing = "DEEP_RESEARCH_DOC_ID" if not doc_id else "GOOGLE_SERVICE_ACCOUNT_JSON"
        print(f"Google Doc archive skipped: the {missing} secret is not set.")
        print(f"::notice::Google Doc archive skipped: {missing} is not set")
        return 0
    session, email = (_DOCS_CONNECT or docs_mod.connect)(key)
    client = docs_mod.DocClient(session, doc_id, email)
    if args.check:
        facts = client.check()
        print(f"Google Doc archive works: '{facts['title']}' has {facts['tabs']} tab(s); a test tab was added and removed. "
              f"Shared with {email}.")
        return 0
    slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
    lines, added, failed = docs_mod.archive_reports(store, slugs, client, sync_mod.utc_now(), dry_run=args.dry_run)
    print("\n".join(lines))
    if failed:
        print(f"::error::{failed} Deep Research report(s) could not be added to the Google Doc; they will be tried again next run")
        return 1
    return 0


def _research(args, store):
    settings = research_mod.load_settings(args.research_config)
    if args.region.strip():
        settings = dataclasses.replace(settings, region_override=research_mod._country(args.region, "--region"))
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
        return browser_mod.open_browser(site, browser_cfg["sites"][site], browser_mod.site_pacing(browser_cfg, site),
                                        creds[site].get("cookies") or "", debug_dir)
    return factory


EXTRA_SECRETS = ("ANTHROPIC_API_KEY", "APIFY_TOKEN")


def _secrets_status(browser_cfg, env):
    """(name, set or not set) for every secret the collectors can use. Never the values."""
    names = [n for cfg in browser_cfg["sites"].values() for n in cfg.get("secrets", {}).values()] + list(EXTRA_SECRETS)
    return [(n, "set" if (env.get(n) or "").strip() else "not set") for n in names]


def _without_resting(wanted, store, now):
    """Check and Recon touch the account, so a resting one is left alone and the report says so."""
    resting = collectors_mod.active_cooldowns(store.read_cooldowns(), now)
    for source in wanted:
        entry = resting.get(collectors_mod.SITE_OF[source])
        if entry:
            print(f"  {source}: skipped: resting until {entry['until'][:10]} after an account warning "
                  f"({str(entry.get('reason', ''))[:100]}); add --ignore-cooldown once you have dealt with it")
    return [s for s in wanted if collectors_mod.SITE_OF[s] not in resting]


def _collect(args, store):
    settings = collectors_mod.load_collector_settings(args.research_config)
    if args.rest.strip():                                   # nothing is opened: only the memory of the rest is written
        print("\n".join(collectors_mod.rest_by_hand(store, args.rest, sync_mod.utc_now(), args.rest_days, settings)))
        return 0
    browser_cfg = browser_mod.load_browser_settings(args.browser_config)
    wanted = [n.strip() for n in args.sources.split(",") if n.strip()] or list(collectors_mod.BROWSER_SOURCES)
    unknown = [n for n in wanted if n not in collectors_mod.SOURCES]
    if unknown:
        print(f"Error: unknown source {', '.join(unknown)} (choose from {', '.join(collectors_mod.SOURCES)})")
        return 1
    creds = _site_creds(browser_cfg, os.environ)
    apify_token, claude_key = (os.environ.get("APIFY_TOKEN") or "").strip(), (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    apify = Apify(apify_token) if apify_token else None
    claude = Claude(claude_key, model=settings.claude_model, max_tokens=settings.claude_max_tokens,
                    prompt_max_tokens=settings.claude_prompt_max_tokens) if claude_key else None
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
        if source in collectors_mod.BROWSER_SOURCES and backend in ("fallback", "extra"):
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
    if args.ignore_cooldown and not args.dry_run:
        freed = collectors_mod.clear_cooldowns(store, now)
        print(f"{freed} resting account(s) will be used again: you said the warning has been dealt with.")
    if args.recon or args.check:
        wanted = _without_resting(wanted, store, now)
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
        dry_run=args.dry_run, root=args.brands_dir, apify=apify, claude=claude, backend=backend,
        region=args.region.strip() or None)
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
        if args.cmd == "docs":
            return _docs(args, store)
        if args.cmd == "ideabank":
            return _ideabank(args, store)
        if args.cmd == "request":
            return _request(args, store)
        if args.cmd == "brief":
            return _brief(args, store)
        if args.cmd == "research":
            return _research(args, store)
        if args.cmd == "collect":
            return _collect(args, store)
        if args.cmd == "status":
            state = sync_mod.catalogue_state(args.brand, store)
            print(json.dumps(state, indent=1))
            return 0 if state["usable"] else 2
    except (BrandError, StoreError, sync_mod.BaselineError, FetchError, sheets_mod.SheetsError,
            docs_mod.DocsError, ib_mod.IdeaBankError, rules_mod.RuleError, drive_mod.DriveError, planner_mod.PlanError, brief_mod.BriefError, research_mod.ResearchError, collectors_mod.CollectError, browser_mod.BrowserError, KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    return 1
