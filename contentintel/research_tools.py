"""The commands and hooks behind Update 64's connections: audience voice (last30days), video facts and pace (yt-dlp, ffmpeg), pool candidates (OmniRoute's catalogue), licence-safe assets (Openverse) and motion formats.

Every command here is read-only towards the platforms: it reads public data, keeps evidence next to the brand's data (data/<brand>/extras/), prints what it did and what failed, and changes nothing
the owner has not switched on (each connection has its ON/OFF, route, version and secrets switches; contentintel/connections.py).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import connections as CONN
from . import contract as K
from . import motion_prompts as MOTION
from . import openverse as OPENVERSE
from . import apify as apify_mod
from . import apify_budget as apify_budget_mod
from . import pace as PACE
from . import providers as providers_mod
from . import poolcatalog as POOL
from . import services as SERVICES
from . import videometa as VMETA
from . import voice as VOICE
from .brands import list_brand_slugs, load_brand
from .store import Store

UA = "Mozilla/5.0 (compatible; content-strategy-tool; read-only research)"


def default_get(url, headers=None, timeout=25):
    """(status, text) of a public address. Never raises: a refusal or an outage is a status."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                  # noqa: S310 - fixed public API addresses
            return resp.status, resp.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


def default_post(url, data, timeout=25):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                  # noqa: S310
            return resp.status, resp.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


def add_parsers(sub, common):
    p = sub.add_parser("voice", help="audience voice: what people say about a topic, the brand or its competitors (last30days-skill, with the platform's own backup)")
    common(p)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--brand")
    group.add_argument("--all", action="store_true")
    p.add_argument("--topic", action="append", default=[], help="a topic to ask about (repeat for several); default: the brand's biggest product categories and its name")
    p.add_argument("--mode", choices=VOICE.MODES, default="research", help="research (a topic), competitors (the brand's followed benchmark brands side by side) or discover (what is accelerating in a field)")
    p.add_argument("--peers", default="", help="competitors mode: comma-separated names (default: the brand's benchmark brands)")
    p.add_argument("--depth", choices=("quick", "deep"), default="quick")
    p.add_argument("--max-peers", type=int, default=6, help="competitors mode: how many benchmark brands to compare (the engine takes up to 6)")
    p.add_argument("--max-topics", type=int, default=5, help="research mode: how many of the brand's own topics to ask about when none is given")
    p.add_argument("--apify", action=argparse.BooleanOptionalAction, default=True, help="ON by default (research and competitors modes, needs APIFY_TOKEN; --no-apify switches it off): also ask Apify's TikTok and Instagram scrapers about each topic or competitor, within the voice allowance (60 items a month, 40 a workflow run in all; each call has a cost ceiling Apify enforces, 0.50 dollars for TikTok and 0.25 for Instagram) and never beyond the Collect step's Apify allowance from research.yaml")
    p.add_argument("--seed", action="store_true", help="discover mode: also add the discovered topics to the brand's auto seeds as Meta Ad Library keywords (only when auto seeds already exist)")
    p.add_argument("--dry-run", action="store_true", help="say what would be asked; ask nothing")

    p = sub.add_parser("analyze", help="video facts (yt-dlp) for an address, the comments' text, what is said in the video (transcription) and/or the pace of a video file (ffmpeg); prints and saves the analysis")
    common(p)
    p.add_argument("--url", default="")
    p.add_argument("--file", default="")
    p.add_argument("--brand", default="", help="keep the analysis under this brand (empty: data/_shared/analyses.json)")
    p.add_argument("--speech-language", default="", help="the language the speech is expected in (en, es...); empty: the video's own language, else English")
    p.add_argument("--speech", action=argparse.BooleanOptionalAction, default=True, help="ON by default: transcribe what is said in the video (needs the video downloaded)")
    p.add_argument("--comments", action="store_true")
    p.add_argument("--pace", action="store_true", help="with --url: download the video (about 720p at most) and measure how it moves; needs ffmpeg on the machine")

    p = sub.add_parser("analytics", help="GA4 and Search Console for each brand, read-only and only with access (otherwise NEEDS_INPUT, never a made-up number), plus public Serper rank checks")
    common(p)
    p.add_argument("--brand", default="all")

    p = sub.add_parser("apify-budget", help="print the money Apify says is left on each key (no Apify call is made that costs anything); says why when Apify's account endpoint cannot be read")
    common(p)

    p = sub.add_parser("pageid", help="try the four places a Facebook page id can be read from and say what each one gave (public pages only; nothing is changed)")
    common(p)
    p.add_argument("--url", required=True, help="the page's address, for example https://www.facebook.com/Chobani")
    p.add_argument("--name", default="", help="the brand's name (Chobani): lets the check tell a regional page (ChobaniAu, chobaniau) from the brand's own")
    p.add_argument("--cross-check", action="store_true", help="also list the numeric ids a search finds when an id was read from the page, to compare them (uses Serper searches)")

    p = sub.add_parser("pool", help="free AI capacity worth adding to the pool, read from OmniRoute's catalogue as data (changes nothing)")
    common(p)
    p.add_argument("--gateway-dir", default="")

    p = sub.add_parser("assets", help="licence-safe pictures or sounds for a query (Openverse, with Wikimedia Commons as the backup)")
    common(p)
    p.add_argument("--brand", default="")
    p.add_argument("--query", required=True)
    p.add_argument("--kind", choices=OPENVERSE.KINDS, default="images")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--any-licence", action="store_true", help="lift Openverse's filter to licences that allow use in advertising (every result still shows its licence and credit line)")

    p = sub.add_parser("motion", help="motion formats close to a text, from the prompt library (inspiration only, authors credited)")
    common(p)
    p.add_argument("--query", required=True)
    p.add_argument("--category", default="")
    p.add_argument("--limit", type=int, default=3)


def _settings(args):
    try:
        return SERVICES.load_settings(None, getattr(args, "data_dir", "data"))
    except Exception:                                                              # noqa: BLE001 - a broken observatory.yaml never stops an unrelated command
        return None


def _peer_names(store, slug, limit=6):
    state = store.read_benchmarks(slug) or {}
    names = []
    for entry in (state.get("items") or {}).values():
        if isinstance(entry, dict) and entry.get("name") and entry.get("status", "resolved") in ("resolved", "ambiguous", "unresolved", ""):
            names.append(str(entry["name"]))
    return list(dict.fromkeys(names))[:limit]


def _brand_yaml(args, slug):
    import yaml
    path = Path(getattr(args, "brands_dir", "brands")) / slug / "research.yaml"
    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _seed_keywords(args, store, slug):
    """Short search phrases the brand already uses to find its market: the keywords in its own research.yaml, then the auto seeds' direct and adjacent keywords. These are what an audience says, unlike the shop's category labels."""
    ads = (_brand_yaml(args, slug).get("ads") or {})
    mine = [str(k) for k in (ads.get("keywords") or []) if isinstance(k, str)]
    seeds = store.read_auto_seeds(slug) or {}
    auto = [k for scope in ("direct", "adjacent") for k in ((seeds.get("keywords") or {}).get(scope) or [])]
    return list(dict.fromkeys([*mine, *auto]))


def _skipped_names(args, slug):
    """Benchmark brands the owner set to skip in research.yaml (ads.benchmark_pages: Name: skip): they are never asked about as competitors."""
    from . import benchmarks as bench
    pages = ((_brand_yaml(args, slug).get("ads") or {}).get("benchmark_pages")) or {}
    return {bench.norm_name(n): str(n) for n, v in pages.items() if str(v).strip().lower() in bench.SKIP_WORDS} if isinstance(pages, dict) else {}


def run_voice(args, store, now, *, get=default_get, runner=subprocess.run, environ=None, clock=None):
    import os
    import time
    environ = dict(os.environ) if environ is None else environ
    settings = _settings(args)
    if settings is None:
        print("Audience voice skipped: observatory.yaml could not be read.")
        return 0
    slugs = list_brand_slugs(args.brands_dir) if args.all else [args.brand]
    if not slugs or slugs == [None]:
        print("Error: choose --brand NAME or --all")
        return 1
    handed = [n for n in CONN.CONNECTIONS["last30days"]["secrets"] if n in CONN.external_env(settings, "last30days", environ)]
    print("Keys the engine is given (names only, never values): " + (", ".join(handed) if handed else "none")
          + f". SCRAPECREATORS_API_KEY is {'set in this run' if environ.get('SCRAPECREATORS_API_KEY') else 'NOT set in this run'}"
          + (" and is handed to the engine: when the engine answers (research and competitors modes), TikTok, Instagram and Meta Ads are asked too. Each keyed query spends ScrapeCreators credits (100 free, once): try one topic first." if "SCRAPECREATORS_API_KEY" in handed else "; those three lanes are off."))
    external = VOICE.ExternalLast30Days(settings, environ=environ, runner=runner)
    builtin = VOICE.BuiltinVoice(get, (clock or time.time)())
    worst = 0
    apify_all = _apify_lane(args, store, now, environ, slugs) if args.apify and args.mode in ("research", "competitors") and not args.dry_run else (None, None)          # ONE lane for the whole run: the per-run allowance is shared by every brand under --all
    for slug in slugs:
        brand = load_brand(slug, args.brands_dir)
        if args.mode == "discover":
            jobs = [("", ())] if not args.topic else [(t, ()) for t in args.topic]
        elif args.mode == "competitors":
            skipped = _skipped_names(args, slug)
            from . import benchmarks as bench
            listed = tuple(x.strip() for x in args.peers.split(",") if x.strip())
            pool = listed or tuple(_peer_names(store, slug, 50))
            peers = tuple(n for n in pool if bench.norm_name(n) not in skipped and bench.norm_name(n) != bench.norm_name(brand.name))
            peers = peers if listed else peers[:max(1, args.max_peers)]
            dropped = [n for n in listed if n not in peers]
            if dropped:
                print(f"{slug}: not asked about (the brand itself, or set to skip in research.yaml; remove it from benchmark_pages skip to ask): {', '.join(dropped)}")
            jobs = [(brand.name, peers)] if peers else []
            if peers:
                print(f"{slug}: competitors asked about: {', '.join(peers)}" + (f" (left out because you set them to skip: {', '.join(sorted(skipped.values()))})" if skipped and not listed else ""))
            if not peers:
                print(f"{slug}: competitors mode needs peers: none were given and the brand has no benchmark brands yet")
        else:
            jobs = [(t, ()) for t in (args.topic or VOICE.topics_for(brand, store, limit=args.max_topics, seeds=_seed_keywords(args, store, slug)))]
        if args.dry_run:
            for topic, peers in jobs:
                print(f"{slug}: would ask {args.mode} about {topic or 'trending topics'!r}" + (f" against {', '.join(peers)}" if peers else ""))
            continue
        kept = store.read_extra(slug, "voice") or {}
        total_new = 0
        run_entities = {}
        apify_lane, ledger = apify_all if args.mode in ("research", "competitors") and args.apify else (None, None)
        for topic, peers in jobs:
            try:
                items, status, which = VOICE.gather(settings, topic, mode=args.mode, peers=peers, depth=args.depth, now=now, external=external, builtin=builtin)
            except K.Failure as exc:
                worst = max(worst, 0)
                print(f"{slug}: {args.mode} {topic or 'trending'!r}: {exc.code}: {exc.message}" + (f" (the last30days engine had also failed: {external.last_error})" if external.last_error else ""))
                items, status, which = [], {}, "none"                                  # the Apify lane still runs below: one reader failing does not silence the other
            kept, fresh = VOICE.merge(kept, items, now)
            total_new += fresh
            for it in items:
                run_entities[it.get("entity") or ""] = run_entities.get(it.get("entity") or "", 0) + 1
            if apify_lane is not None:
                asks = [(topic, "")] if args.mode == "research" else [(p, p) for p in peers]                # competitors: each competitor, never the brand itself (research mode covers the brand)
                for idx, (ask, entity) in enumerate(asks):
                    share = None if args.mode == "research" else max(2, apify_lane.room() // (len(asks) - idx))          # what is left is split between the competitors still to ask: nothing is stranded when one reads less
                    extra, notes = apify_lane.run(ask, share=share, entity=entity, reddit_backup=status.get("reddit") in VOICE.FAILED)          # Reddit's Apify actor only when the public Reddit search failed
                    store.save_usage(ledger.usage)                                         # saved after every ask: spent is spent, whatever happens next
                    for note in notes:
                        print(f"{slug}: {note}")
                    if extra:
                        kept, fresh_a = VOICE.merge(kept, extra, now)
                        total_new += fresh_a
                        for it in extra:
                            run_entities[it.get("entity") or ""] = run_entities.get(it.get("entity") or "", 0) + 1
            if which not in ("builtin", "none") and external.engine_notes and VOICE.failed_sources(status):
                print(f"{slug}: some sources failed; the engine said: {external.engine_notes}")
            if which not in ("builtin", "none") and external.engine_summary and "SCRAPECREATORS_API_KEY" in handed:
                print(f"{slug}: the engine's own account of the keyed lanes: {external.engine_summary}")
            if external.sc_out_of_credits and "SCRAPECREATORS_API_KEY" in handed:
                print(f"{slug}: ScrapeCreators answered 'payment required' (HTTP 402): its credits are spent, so the keyed lanes return nothing until credits are added; check the balance on its dashboard.")
            if which == "builtin" and external.last_error:
                print(f"{slug}: the last30days engine could not answer ({external.last_error}); the platform's own reader answered instead")
            bad = VOICE.failed_sources(status)
            if which == "none":
                continue
            print(f"{slug}: {args.mode} {topic or 'trending'!r}: {len(items)} item(s) from {which} ({fresh} new)" + (f"; sources that FAILED (not 'nothing said'): {', '.join(bad)}" if bad else "")
                  + "; " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())))
        if args.mode == "competitors" and jobs:
            asked = jobs[0][1]
            print(f"{slug}: this run, items per competitor: " + "; ".join(f"{p}: {run_entities.get(p, 0)}" for p in asked) + f"; the brand itself: {run_entities.get('', 0) + run_entities.get(jobs[0][0], 0)}")
        if args.mode != "discover":
            from .collectors import collected_rows
            posts = VOICE.reddit_items(collected_rows(store.read_collected(slug) or []))
            if posts:
                kept, fresh_r = VOICE.merge(kept, posts, now)
                total_new += fresh_r
                via = {}
                for p in posts:
                    via[p.get("via") or "unknown"] = via.get(p.get("via") or "unknown", 0) + 1
                names = {"api": "official Reddit API", "agent_reach": "stored session through Agent-Reach", "unknown": "route not recorded"}
                print(f"{slug}: {len(posts)} Reddit post(s) the platform already collected were added to the voice ({fresh_r} new); read through: " + ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(via.items())))
            else:
                print(f"{slug}: no Reddit posts are collected yet: Reddit refuses the runner's public read, so run Actions > Observatory > reddit (official API once Reddit approves the app, or the stored dummy-account session through Agent-Reach) and the next voice run adds them")
            comments = VOICE.comment_items((store.read_ideabank(slug) or {}).get("items"))
            if comments:
                kept, fresh_c = VOICE.merge(kept, comments, now)
                total_new += fresh_c
                print(f"{slug}: {len(comments)} viewer comment(s) from videos already read were added to the voice ({fresh_c} new): the audience's own words, with their likes")
        if jobs or args.mode != "discover":
            store.write_extra(slug, "voice", kept)
            top = VOICE.prompt_lines(kept, 3)
            kept["seed_topics"] = VOICE.seed_topics(kept)
            store.write_extra(slug, "voice", kept)
            if args.seed and args.mode == "discover":
                print(f"{slug}: " + seed_auto_seeds(store, slug, kept["seed_topics"]))
            ents = VOICE.by_entity(kept)
            if ents:
                print(f"{slug}: talk about each competitor: " + "; ".join(f"{e}: {n} item(s), {w:,} engagement" for e, (n, w) in sorted(ents.items(), key=lambda kv: -kv[1][1])))
            if kept["seed_topics"]:
                print(f"{slug}: topics people are discussing (suggested next research topics): " + "; ".join(kept["seed_topics"]))
            print(f"{slug}: audience voice now holds {len(kept.get('items', []))} item(s), {total_new} new this run." + (f"\n  Strongest:\n{top}" if top else ""))
    if apify_all[0] is not None and apify_all[0].asks_made:                              # once per run, after every brand, and only when something was asked
        lane, led = apify_all
        print(f"Apify usage after this run: voice {led.month_used('voice_apify_items'):g}/{lane.monthly} items this month, all Apify use {led.month_used('apify_items'):g}/{lane.shared_cap} (this run read {lane.spent_this_run}" + ("; no call was made: every ask was refused, see the notes above" if not lane.attempted else "") + ").")
        for event in getattr(lane.apify, "events", []):
            print(f"Apify keys: {event}")
    return worst


def _collect_apify_cap(args, slugs=()):
    """The monthly Apify items the owner configured for the Collect step: collectors.apify.monthly_items in research.yaml (the file Collect itself reads, one allowance for every brand). The built-in default only when the file cannot be read."""
    from . import collectors as COL
    try:
        return COL.load_collector_settings(getattr(args, "research_config", None) or "research.yaml").apify_monthly_items
    except Exception:                                                                  # noqa: BLE001 - an unreadable file falls back to the default
        return COL.DEFAULT_COLLECTORS["apify"]["monthly_items"]


def _env_number(environ, name, default, low, high, cast):
    """An override from the environment, kept inside [low, high]; a missing or unreadable value is the default. An override can tighten or loosen the lane only within these bounds."""
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return cast(min(high, max(low, value)))


def _apify_lane(args, store, now, environ, slugs=()):
    """(the Apify voice lane, the usage ledger) or (None, None) with the reason printed. The lane exists only when the owner asked for it in this run and APIFY_TOKEN is set."""
    client = apify_mod.build_apify(environ)
    if client is None:
        print("Apify lane skipped: APIFY_TOKEN is not set in this run.")
        return None, None
    from . import collectors as COL
    from . import research as research_mod
    ledger = research_mod.Ledger(store.load_usage(), now[:7])
    shared = _collect_apify_cap(args, slugs)
    cost = _env_number(environ, "CONTENTINTEL_VOICE_APIFY_COST", None, 0.0, VOICE.ApifyVoice.HARD_CEILING, float)
    keys = len(getattr(client, "clients", [client]))                                                   # each key brings its own 5 dollars a month: the item limits scale with the keys held
    lines, left = apify_mod.budget_report(client)
    for line in lines:
        print(line)

    def budget_fn():                                                                                   # re-read before each call (a transient failure at set-up no longer switches the guard off for the run); the best single key, since a call runs on one key
        return apify_mod.best_key_left(client)
    lane = VOICE.ApifyVoice(client, ledger, per_run=_env_number(environ, "CONTENTINTEL_VOICE_APIFY_PER_RUN", 40 * keys, 1, 300, int), monthly=_env_number(environ, "CONTENTINTEL_VOICE_APIFY_MONTHLY", apify_budget_mod.items("voice", keys), 1, 2000, int),
                            shared_cap=shared, max_cost_usd=cost, normalize=COL.normalize_items, budget_fn=budget_fn, run_usd_cap=_env_number(environ, "CONTENTINTEL_VOICE_APIFY_RUN_USD", 3.0, 0.25, 10.0, float), day=now[:10], keys=keys, daily=_env_number(environ, "CONTENTINTEL_VOICE_APIFY_DAILY", apify_budget_mod.plan(keys)["voice"]["per_day"], 0, 1000, int))
    for env_name, attr in (("CONTENTINTEL_VOICE_APIFY_PER_RUN", "per_run"), ("CONTENTINTEL_VOICE_APIFY_MONTHLY", "monthly")):      # an override outside the lane's bounds is changed: say so
        raw = (environ.get(env_name) or "").strip()
        if raw.isdigit() and int(raw) != getattr(lane, attr):
            print(f"Note: {env_name}={raw} is outside what the lane allows (1 to {300 if attr == 'per_run' else 2000}); {getattr(lane, attr)} is used.")
    print(f"Apify lane ON for this run (switch off with --no-apify): at most {lane.per_run} items in this whole run, {lane.monthly} a month for voice (used so far this month: {ledger.month_used('voice_apify_items'):g}), "
          f"and never beyond the Collect step's {shared} a month (used: {ledger.month_used('apify_items'):g}); each call has a cost ceiling Apify enforces: {lane.ceiling('tiktok'):g} dollars for TikTok, {lane.ceiling('instagram'):g} for Instagram.")
    return lane, ledger


def run_analytics(args, store, now, *, environ=None, search=None, fetch=None):
    """GA4 and Search Console per brand (read-only, only with access) and the public Serper ranks. A source without access is reported as such and has NO numbers: the report is saved with its needs_input lines for the skills that need the stores' own data."""
    from . import analytics as AN
    environ = dict(os.environ if environ is None else environ)
    slugs = list_brand_slugs(args.brands_dir) if args.brand in ("", "all") else [args.brand]
    if search is None:
        serper = providers_mod.build_providers(environ).get("serper_dev")
        search = (lambda query: [{"url": r.url, "title": r.title} for r in serper.search(query, num=10)]) if serper is not None else None
    for slug in slugs:
        report = AN.build_report(args.brands_dir, slug, environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""), search=search, fetch=fetch)
        for name, label in (("ga4", "GA4"), ("search_console", "Search Console")):
            part = report[name]
            print(f"{slug}: {label}: {part['status']}" + (f" ({part['reason']})" if part["reason"] else f", {len(part['rows'] or [])} row(s)"))
        for r in report["serper_ranks"]:
            print(f"{slug}: public signal (Serper): '{r['keyword']}': " + (f"position {r['position']} ({r['url']})" if r["position"] else "not in the first results"))
        for line in report["needs_input"]:
            print(f"{slug}: {line}")
        store.write_extra(slug, "analytics", {**report, "at": now})
    return 0


def run_apify_budget(args, *, environ=None):
    """Prove, on the machine that runs it, what Apify itself reports: dollars used and allowed per key, in total, and the day each cycle ends. Spends nothing. Exit 0 when at least one key was read, 1 when none was (the item counters still guard the spending)."""
    environ = dict(os.environ if environ is None else environ)
    client = apify_mod.build_apify(environ)
    if client is None:
        print("Apify budget: no Apify key is set in this run (APIFY_TOKEN, APIFY_TOKEN_2, APIFY_TOKEN_3, APIFY_TOKEN_4).")
        return 1
    lines, left = apify_mod.budget_report(client)
    for line in lines:
        print(line)
    print("Item counters (this calendar month, always applied): " + _usage_summary(args))
    return 0 if left is not None else 1


def _usage_summary(args):
    from . import research as research_mod
    store = Store(args.data_dir)
    led = research_mod.Ledger(store.load_usage(), datetime.now(timezone.utc).strftime("%Y-%m"))
    return f"all Apify use {led.month_used('apify_items'):g}, voice {led.month_used('voice_apify_items'):g}"


def run_pageid(args, *, get=default_get, search=None, environ=None):
    """Read a Facebook page's id from each of the four places the finder tries, and say what each answered. Public addresses only; a login page or a refusal is reported, never worked around."""
    from . import main_page as MP
    url = args.url.strip()
    numeric = re.search(r"facebook\.com/profile\.php\?id=(\d{6,})", url)
    if numeric:
        print(f"The address already carries the page id: {numeric.group(1)} (nothing to read; no page was asked)")
        return 0
    if not MP.slug_of(url):
        print("Error: that is not a facebook.com page address")
        return 1
    name = " ".join(str(getattr(args, "name", "") or "").split())
    if name and ("://" in name or "/" in name or len(name) > 60 or len(name.split()) > 4):
        print(f"The brand name {name[:40]!r} does not look like a name (a web address or a long phrase): it is ignored. Put just the brand's name, for example Chobani, in the topic box.")
        name = ""
    if not name:
        print("No brand name was given: only the unambiguous signs of a regional page are checked (a region after a separator, an upper-case or Capitalised region ending). Put the brand's name in the topic box for the exact check.")
    region = MP.regional_hint(MP.slug_of(url), name)
    if region:
        print(f"WARNING: the address looks like a regional page ({region}). Its id belongs to that region, not to the brand's main page: do not use it for the main page.")
    found = ""
    for address in MP.page_id_addresses(url):
        status, text = get(address, {"Accept": "text/html"})
        page_id = MP.read_page_id(text) if status == 200 else ""
        host = address.split("/")[2] + ("/plugins" if "/plugins/page.php" in address else "")
        print(f"  {host}: HTTP {status}, {len(text):,} characters, " + (f"PAGE ID {page_id}" if page_id else ("a login page" if "log in" in text.lower()[:20000] or "login" in text.lower()[:3000] else "no page id in it")))
        found = found or page_id
    if not found or getattr(args, "cross_check", False):                           # --cross-check also lists the search's candidates when an id WAS read, to compare them
        if search is None:
            serper = providers_mod.build_providers(dict(os.environ if environ is None else environ)).get("serper_dev")
            search = (lambda query: [{"url": r.url, "title": r.title, "snippet": r.snippet} for r in serper.search(query, num=10)]) if serper is not None else None
        if search is None:
            print("Search for the numeric id: not tried (SERPER_API_KEY is not set in this run)")
        else:
            name = name or MP.slug_of(url)
            results = []
            for query in MP.ID_SEARCHES:
                try:
                    results += list(search(query.format(name=name)) or [])
                except Exception as exc:                                              # noqa: BLE001 - a failed search is reported
                    print(f"Search for the numeric id: failed ({type(exc).__name__})")
                    break
            cands = MP.id_candidates(results)
            print(f"Search for the numeric id of '{name}': {len(cands)} candidate id(s) found in search results (the Collect step's reviewer confirms which, if any, is this page)")
            for c in cands[:8]:
                print(f"  id {c['id']} from {c['url']} | {c['title'][:80]}")
    print(f"Result for {url}: " + (f"page id {found}" + (f" (a REGIONAL page: {region})" if region else "") if found else "no page id could be read from any of the four places (the Collect finder searches for the id itself and its reviewer confirms it)"))
    return 0


def seed_auto_seeds(store, slug, topics, limit=5):
    """Add discovered topics to the brand's auto seeds as Meta Ad Library keywords (cross-industry scope: ideas from what is accelerating). Opt-in (voice --seed); only when auto seeds already exist; short generic phrases only
    (1 to 4 words, no brand names beyond what the seeds already allow, no # or @); they last until the seeds are next refreshed. Returns what was done, in words."""
    import re
    seeds = store.read_auto_seeds(slug)
    if not isinstance(seeds, dict) or not isinstance(seeds.get("keywords"), dict):
        return "no auto seeds exist yet, so nothing was added (run Collect with mode 'seeds' first); the topics stay in the voice file as suggestions"
    pool = seeds["keywords"].setdefault("cross_industry", [])
    added = []
    for topic in topics:
        phrase = " ".join(re.sub(r"[#@]", "", str(topic)).lower().split()[:4])
        if phrase and len(phrase) <= 60 and phrase not in {p.lower() for p in pool} and len(added) < limit:
            pool.append(phrase)
            added.append(phrase)
    if added:
        store.write_auto_seeds(slug, seeds)
    return ("added to the auto seeds as Meta Ad Library keywords: " + "; ".join(added)) if added else "every discovered topic was already a seed"


def run_analyze(args, now="", *, runner=subprocess.run, store=None, environ=None, apify=None, post=None, loader=None):
    if not (args.url or args.file):
        print("Error: give --url and/or --file")
        return 1
    env = dict(os.environ) if environ is None else environ
    record = {"url": args.url or args.file, "at": now, "routes": {}}
    facts = {}
    if args.url:
        print("Video facts route: yt-dlp (python -m yt_dlp --dump-single-json, nothing is downloaded for the facts)")
        try:
            facts = VMETA.fetch(args.url, run=runner, comments=args.comments)
            print("Video facts: " + json.dumps(facts, ensure_ascii=False, indent=1))
            record["facts"], record["routes"]["facts"] = facts, "yt-dlp"
        except K.Failure as exc:
            print(f"Video facts: {exc.code}: {exc.message}")
            record["routes"]["facts"] = f"yt-dlp failed: {exc.code}"
    if args.url and getattr(args, "comments", False):
        comments = list(facts.get("top_comments") or [])
        if comments:
            record["routes"]["comments"] = "yt-dlp"
            print(f"Comments route: yt-dlp ({len(comments)} comment text(s))")
        else:
            counted = facts.get("comments")
            print(f"Comments route: yt-dlp gave {'a count of ' + str(counted) if counted else 'no comments'} but no comment text for this site; trying the Apify comments route")
            client = apify if apify is not None else apify_mod.build_apify(env)
            usage = None
            if client is not None and store is not None:
                from . import research as research_mod
                usage = research_mod.Ledger(store.load_usage(), (now or "0000-00-00")[:7])
                if apify_budget_mod.room("analyze_comments", usage, (now or "0000-00-00")[:10]) < 20:
                    print("Comments: no comment text captured: this month's (or today's) share of the Apify money for comments is used up")
                    record["routes"]["comments"] = "none: budget share used"
                    client = None
            if client is None:
                print("Comments: no comment text captured: yt-dlp does not read this site's comment text and no Apify key (APIFY_TOKEN) is set in this run")
                record["routes"]["comments"] = "none: no Apify key"
            else:
                try:
                    comments = VMETA.comments_via_apify(args.url, client)
                    if usage is not None:
                        apify_budget_mod.charge("analyze_comments", usage, (now or "0000-00-00")[:10], max(1, len(comments)))
                        store.save_usage(usage.usage)
                    record["routes"]["comments"] = "apify (" + VMETA.TIKTOK_COMMENTS_ACTOR + ", at most 20 comments, call cost ceiling 0.5 dollars)"
                    print(f"Comments route: Apify {VMETA.TIKTOK_COMMENTS_ACTOR} (at most 20 comments, call cost ceiling 0.5 dollars): {len(comments)} comment text(s)")
                except K.Failure as exc:
                    print(f"Comments: no comment text captured: {exc.message}")
                    record["routes"]["comments"] = f"apify failed: {exc.code}"
        if comments:
            record["comments"] = comments
            for c in comments[:10]:
                print(f"  - ({c['likes']} likes) {c['text'][:200]}")
    import tempfile
    code = 0
    with tempfile.TemporaryDirectory(prefix="pace-") as tmp:                         # the downloaded video is deleted when this block ends, whatever happens
        path = args.file
        want_speech = bool(getattr(args, "speech", True))
        if args.url and (getattr(args, "pace", False) or want_speech) and not path:
            print("Download route: yt-dlp (the video, about 720p at most, into a temporary folder that is deleted afterwards)")
            try:
                path = VMETA.fetch_file(args.url, tmp, run=runner)
                record["routes"]["download"] = "yt-dlp"
            except K.Failure as exc:
                print(f"Download: the video could not be downloaded by yt-dlp: {exc.code}: {exc.message}")
                record["routes"]["download"] = f"yt-dlp failed: {exc.code}"
        if path and getattr(args, "pace", False):
            try:
                profile = PACE.analyse(path, runner)
                print("Pace: " + PACE.describe(profile))
                print(json.dumps({k: v for k, v in profile.items() if k != "cut_times"}, indent=1))
                record["pace"] = {k: v for k, v in profile.items() if k != "cut_times"}
            except PACE.PaceError as exc:
                print(f"Pace: not measured: {exc}")
        if path and want_speech:
            lang = (getattr(args, "speech_language", "") or facts.get("language") or "en")           # the language the speech is expected in: the owner's setting, else the video's own, else English (every brand here is English): a transcript in another script is thrown away
            record["speech"] = _speech(path, tmp, runner, env, post, loader, getattr(args, "data_dir", "data"), lang)
    if store is not None:
        _save_analysis(store, getattr(args, "brand", "") or "_shared", record)
    return code


def _speech(path, tmp, runner, env, post, loader, data_dir, language="en"):
    """What is said in the video: the audio is made sendable with ffmpeg and the transcription backends are tried in their measured order (Groq, Cloudflare, Faster-Whisper, VibeVoice). Printed and returned."""
    from . import transcribe as TR
    try:
        out = TR.transcribe(path, workdir=tmp, which=(lambda name: name) if runner is not subprocess.run else None, language=language, backends=TR.order(Path(data_dir) / "_shared"), key=TR.env_key(env), post=post, loader=loader, cloudflare=TR.env_cloudflare(env), vibevoice=TR.env_vibevoice(env), runner=runner)
    except Exception as exc:                                                                 # noqa: BLE001 - a transcript that cannot be made is reported, never a crash
        why = getattr(exc, "message", str(exc))
        print(f"Speech: not captured: {why}")
        return {"captured": False, "why": why[:400]}
    print(f"Speech route: {out['backend']} ({out['seconds']} s)" + (f" after: {'; '.join(f'{b}: {w[:60]}' for b, w in out['tried'])}" if out["tried"] else "") + f": {out['text'][:600]}")
    return {"captured": True, "backend": out["backend"], "language": out["language"], "text": out["text"][:6000], "tried": out["tried"]}


def _save_analysis(store, slug, record):
    """Keep the analysis in the repository (data/<brand or _shared>/extras/analyses.json), newest first, one per address, so it is not lost with the run's log."""
    shared = slug == "_shared"
    kept = (store.read_shared("analyses") if shared else store.read_extra(slug, "analyses")) or {}
    items = [i for i in kept.get("items", []) if i.get("url") != record["url"]]
    items.insert(0, record)
    body = {"note": "video analyses: facts, comment text, speech and pace, with the route each came by", "items": items[:100]}
    (store.write_shared("analyses", body) if shared else store.write_extra(slug, "analyses", body))
    print("Analysis saved: " + ("data/_shared/analyses.json" if shared else f"data/{slug}/extras/analyses.json") + f" ({len(items)} kept)")


def run_pool(args, *, gateway_dir=None):
    import yaml
    providers = {}
    p = Path("ai_providers.yaml")
    if p.is_file():
        providers = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("providers") or {}
    entries, where = POOL.read(CONN.external_dir() / "omniroute_catalog")
    gw = POOL.gateway_platforms(gateway_dir or getattr(args, "gateway_dir", "") or None)
    if not entries:
        print("Pool candidates: " + where)
        return 0
    print("\n".join(POOL.render(POOL.candidates(entries, providers, gw), where)))
    print("Nothing was changed. To add a provider, approve it in ai_providers.yaml (and give the gateway its key); the catalogue is a researched list, not a promise.")
    return 0


def run_assets(args, store, now, *, get=default_get, post=default_post, environ=None):
    import os
    environ = dict(os.environ) if environ is None else environ
    settings = _settings(args)
    use = True
    if settings is not None and "openverse" in settings.get("services", {}):          # a settings file that predates the service leaves it on, as every new switch is
        use = SERVICES.allowed(settings, "openverse", "brief")[0]
    bearer = OPENVERSE.token(post, environ.get("OPENVERSE_CLIENT_ID", ""), environ.get("OPENVERSE_CLIENT_SECRET", "")) if use else ""
    if not args.query.strip():
        print("Assets: the topic is empty, so nothing was searched (an empty topic used to fetch random pictures and save them)")
        return 1
    from . import assets_find as AF
    brand, research = None, None
    if args.brand:
        try:
            brand = load_brand(args.brand, getattr(args, "brands_dir", "brands"))
            import yaml
            rp = Path(getattr(args, "brands_dir", "brands")) / args.brand / "research.yaml"
            research = yaml.safe_load(rp.read_text(encoding="utf-8")) if rp.is_file() else None
        except Exception:                                                                    # noqa: BLE001 - a brand that cannot be read only means no derived queries
            brand = None
    found, report = AF.find_usable(get, args.query, args.kind, brand=brand, store=store, research=research, limit=args.limit, bearer=bearer, use_openverse=use, licence_ok_for_ads=not args.any_licence)
    which = next((t_["source"] for t_ in reversed(report["tried"]) if t_["kept"]), "nothing")
    for line in AF.report_lines(report):
        print(line)
    if not found:
        print(f"Assets: none usable found for {args.query!r} ({len(report['tried'])} search(es) tried); nothing was saved.")
        return 0
    print(f"Assets: {len(found)} {args.kind} result(s) from {which} for {args.query!r}" + (f" (found through {report['used']!r}, after the query itself gave nothing usable)" if report["used"] != args.query else "") + f" ({'a token' if bearer else 'anonymous'}):")
    for a in found:
        print(f"  - {a['title']} [{a['license'].upper()}] {a['page'] or a['url']} | credit: {OPENVERSE.credit_line(a)[:160]}")
    if args.brand and found:
        kept = store.read_extra(args.brand, "assets") or {"assets": []}
        if brand is not None:                                                                      # old junk is taken out whenever the list is written (a parliament, a coat of arms, a competitor's shoes)
            kept["assets"], removed = AF.clean_saved(kept["assets"], AF.brand_terms(brand, store, research))
            if removed:
                print(f"Cleaned {len(removed)} saved asset(s) that are not this brand's: " + "; ".join(str(a.get("title") or a.get("url"))[:40] for a in removed[:6]))
        known = {a["url"] for a in kept["assets"]}
        kept["assets"] = ([dict(a, query=args.query, found_at=now) for a in found if a["url"] not in known] + kept["assets"])[:60]
        store.write_extra(args.brand, "assets", kept)
        print(f"Kept for {args.brand}: briefs can point the creative team to these (data/{args.brand}/extras/assets.json).")
    return 0


def run_motion(args):
    entries, where = MOTION.read(CONN.external_dir() / "opus_video_prompts")
    found = MOTION.search(entries, args.query, args.limit, args.category)
    print(f"Motion formats from {where} ({len(entries)} prompts; categories: {', '.join(f'{k} {v}' for k, v in list(MOTION.categories(entries).items())[:5])}):")
    for e in found:
        print("  - " + MOTION.line(e))
    if not found:
        print("  nothing close enough (two shared words are needed; nothing is forced)")
    return 0


def ideabank_hooks(args, store, *, environ=None, runner=subprocess.run):
    """(video_facts, pace_fn, motion_entries) for the Idea Bank run, each None when switched off or not possible here: the owner's yt_dlp connection, ffmpeg on the machine, and the owner's opus_video_prompts connection."""
    import os
    import tempfile
    from . import videofetch
    environ = dict(os.environ) if environ is None else environ
    settings = _settings(args)
    on = settings is None or CONN.decide(settings, "yt_dlp")["on"]
    facts = None
    if on and (shutil.which("yt-dlp") or _has_module("yt_dlp")):
        followers = VMETA.followers_index(getattr(args, "data_dir", "data"))

        def read_facts(items, ids):
            with tempfile.TemporaryDirectory() as tmp:
                jar = videofetch.cookie_file([environ.get("INSTAGRAM_COOKIES"), environ.get("TIKTOK_COOKIES")], Path(tmp) / "cookies.txt")
                return VMETA.enrich_items(items, ids, run=runner, followers=followers, cookies=jar, now=_now(), limit=int(environ.get("CONTENTINTEL_VIDEO_FACTS_LIMIT") or 40))
        facts = read_facts
    pace = (lambda path: PACE.analyse(path, runner)) if shutil.which("ffmpeg") and shutil.which("ffprobe") else None
    motion = None
    if settings is None or CONN.decide(settings, "opus_video_prompts")["on"]:
        try:
            motion = MOTION.read(CONN.external_dir() / "opus_video_prompts")[0] or None
        except Exception:                                                          # noqa: BLE001 - a missing or damaged library only means no motion formats
            motion = None
    return facts, pace, motion


def _has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _now():
    from . import sync as sync_mod
    return sync_mod.utc_now()


if __name__ == "__main__":                                                            # pragma: no cover
    sys.exit("run this through: python -m contentintel")
