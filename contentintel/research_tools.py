"""The commands and hooks behind Update 64's connections: audience voice (last30days), video facts and pace (yt-dlp, ffmpeg), pool candidates (OmniRoute's catalogue), licence-safe assets (Openverse) and motion formats.

Every command here is read-only towards the platforms: it reads public data, keeps evidence next to the brand's data (data/<brand>/extras/), prints what it did and what failed, and changes nothing
the owner has not switched on (each connection has its ON/OFF, route, version and secrets switches; contentintel/connections.py).
"""
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import connections as CONN
from . import contract as K
from . import motion_prompts as MOTION
from . import openverse as OPENVERSE
from . import pace as PACE
from . import poolcatalog as POOL
from . import services as SERVICES
from . import videometa as VMETA
from . import voice as VOICE
from .brands import list_brand_slugs, load_brand

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
    p.add_argument("--dry-run", action="store_true", help="say what would be asked; ask nothing")

    p = sub.add_parser("analyze", help="video facts (yt-dlp) for an address and/or the pace of a video file (ffmpeg); prints, saves nothing")
    common(p)
    p.add_argument("--url", default="")
    p.add_argument("--file", default="")
    p.add_argument("--comments", action="store_true")

    p = sub.add_parser("pool", help="free AI capacity worth adding to the pool, read from OmniRoute's catalogue as data (changes nothing)")
    common(p)
    p.add_argument("--gateway-dir", default="")

    p = sub.add_parser("assets", help="licence-safe pictures or sounds for a query (Openverse, with Wikimedia Commons as the backup)")
    common(p)
    p.add_argument("--brand", default="")
    p.add_argument("--query", required=True)
    p.add_argument("--kind", choices=OPENVERSE.KINDS, default="images")
    p.add_argument("--limit", type=int, default=8)

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


def _peer_names(store, slug, limit=4):
    state = store.read_benchmarks(slug) or {}
    names = []
    for entry in (state.get("items") or {}).values():
        if isinstance(entry, dict) and entry.get("name") and entry.get("status", "resolved") in ("resolved", "ambiguous", "unresolved", ""):
            names.append(str(entry["name"]))
    return list(dict.fromkeys(names))[:limit]


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
    external = VOICE.ExternalLast30Days(settings, environ=environ, runner=runner)
    builtin = VOICE.BuiltinVoice(get, (clock or time.time)())
    worst = 0
    for slug in slugs:
        brand = load_brand(slug, args.brands_dir)
        if args.mode == "discover":
            jobs = [("", ())] if not args.topic else [(t, ()) for t in args.topic]
        elif args.mode == "competitors":
            peers = tuple(x.strip() for x in args.peers.split(",") if x.strip()) or tuple(_peer_names(store, slug))
            jobs = [(brand.name, peers)] if peers else []
            if not peers:
                print(f"{slug}: competitors mode needs peers: none were given and the brand has no benchmark brands yet")
        else:
            jobs = [(t, ()) for t in (args.topic or VOICE.topics_for(brand, store))]
        if args.dry_run:
            for topic, peers in jobs:
                print(f"{slug}: would ask {args.mode} about {topic or 'trending topics'!r}" + (f" against {', '.join(peers)}" if peers else ""))
            continue
        kept = store.read_extra(slug, "voice") or {}
        total_new = 0
        for topic, peers in jobs:
            try:
                items, status, which = VOICE.gather(settings, topic, mode=args.mode, peers=peers, depth=args.depth, now=now, external=external, builtin=builtin)
            except K.Failure as exc:
                worst = max(worst, 0)
                print(f"{slug}: {args.mode} {topic or 'trending'!r}: {exc.code}: {exc.message}")
                continue
            kept, fresh = VOICE.merge(kept, items, now)
            total_new += fresh
            bad = VOICE.failed_sources(status)
            print(f"{slug}: {args.mode} {topic or 'trending'!r}: {len(items)} item(s) from {which} ({fresh} new)" + (f"; sources that FAILED (not 'nothing said'): {', '.join(bad)}" if bad else "")
                  + "; " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())))
        if jobs:
            store.write_extra(slug, "voice", kept)
            top = VOICE.prompt_lines(kept, 3)
            kept["seed_topics"] = VOICE.seed_topics(kept)
            store.write_extra(slug, "voice", kept)
            ents = VOICE.by_entity(kept)
            if ents:
                print(f"{slug}: talk about each competitor: " + "; ".join(f"{e}: {n} item(s), {w:,} engagement" for e, (n, w) in sorted(ents.items(), key=lambda kv: -kv[1][1])))
            if kept["seed_topics"]:
                print(f"{slug}: topics people are discussing (suggested next research topics): " + "; ".join(kept["seed_topics"]))
            print(f"{slug}: audience voice now holds {len(kept.get('items', []))} item(s), {total_new} new this run." + (f"\n  Strongest:\n{top}" if top else ""))
    return worst


def run_analyze(args, now="", *, runner=subprocess.run):
    if not (args.url or args.file):
        print("Error: give --url and/or --file")
        return 1
    code = 0
    if args.url:
        try:
            facts = VMETA.fetch(args.url, run=runner, comments=args.comments)
            print("Video facts: " + json.dumps(facts, ensure_ascii=False, indent=1))
        except K.Failure as exc:
            print(f"Video facts: {exc.code}: {exc.message}")
            code = 0
    if args.file:
        try:
            profile = PACE.analyse(args.file, runner)
            print("Pace: " + PACE.describe(profile))
            print(json.dumps({k: v for k, v in profile.items() if k != "cut_times"}, indent=1))
        except PACE.PaceError as exc:
            print(f"Pace: not measured: {exc}")
    return code


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
    try:
        found, which = OPENVERSE.find(get, args.query, args.kind, limit=args.limit, bearer=bearer, use_openverse=use)
    except K.Failure as exc:
        print(f"Assets: {exc.code}: {exc.message}")
        return 0
    print(f"Assets: {len(found)} {args.kind} result(s) from {which} for {args.query!r} ({'a token' if bearer else 'anonymous'}):")
    for a in found:
        print(f"  - {a['title']} [{a['license'].upper()}] {a['page'] or a['url']} | credit: {OPENVERSE.credit_line(a)[:160]}")
    if args.brand and found:
        kept = store.read_extra(args.brand, "assets") or {"assets": []}
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
    """(video_facts, pace_fn) for the Idea Bank run, each None when switched off or not possible here: the owner's yt_dlp connection, and ffmpeg on the machine."""
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
                return VMETA.enrich_items(items, ids, run=runner, followers=followers, cookies=jar, now=_now())
        facts = read_facts
    pace = (lambda path: PACE.analyse(path, runner)) if shutil.which("ffmpeg") and shutil.which("ffprobe") else None
    return facts, pace


def _has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _now():
    from . import sync as sync_mod
    return sync_mod.utc_now()


if __name__ == "__main__":                                                            # pragma: no cover
    sys.exit("run this through: python -m contentintel")
