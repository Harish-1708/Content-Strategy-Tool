"""`stage`: transcription (stage 5), its retest probe, and the browser bake-off (stage 10). The skill stages (4, 6, 7, 8, 9) run under `canon --action stage` because they read the claims ledger."""
import json
from pathlib import Path

from . import bakeoff as BO
from .brands import list_brand_slugs


def add_parsers(sub, common):
    p = sub.add_parser("stage", help="transcription, its probe and the browser bake-off")
    common(p)
    p.add_argument("--action", choices=["transcribe", "probe-transcribe", "bakeoff"], default="probe-transcribe")
    p.add_argument("--brand", default="")
    p.add_argument("--file", default="", help="transcribe: an audio or video file in the checkout")
    p.add_argument("--url", default="", help="transcribe: a public https address of an audio or video file (read once, up to 24 MB, no login)")
    p.add_argument("--source", default="", help="transcribe: where the audio came from, kept with the transcript")
    p.add_argument("--language", default="", help="transcribe: the spoken language code (empty = detect)")
    p.add_argument("--bakeoff-config", default=BO.CONFIG_FILE)
    p.add_argument("--bakeoff-urls", default="", help="bakeoff: public addresses, comma-separated (overrides the file's list)")


def _download(url, dest, limit=24 * 1024 * 1024):
    import urllib.request
    if not url.startswith("https://"):
        return "only https addresses are read"
    req = urllib.request.Request(url, headers={"User-Agent": "KelsonResearchBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:           # noqa: S310 - an address the owner typed, https only, size capped
            size = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    return ""
                size += len(chunk)
                if size > limit:
                    return f"the file is larger than {limit // 1048576} MB: it was not read"
                fh.write(chunk)
    except Exception as exc:                                                                  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:120]}"


def run_stage(args, store, now, *, environ=None, post=None, loader=None, runner=None):
    import os
    import subprocess
    from . import transcribe as TR                                                         # imported here: the command line does not load the AI package at start-up
    environ = environ if environ is not None else os.environ
    shared = Path(args.data_dir) / "_shared"
    if args.action == "probe-transcribe":
        res = TR.probe(Path(args.data_dir) / "_probe", key=TR.env_key(environ), post=post, loader=loader, cloudflare=TR.env_cloudflare(environ), vibevoice=TR.env_vibevoice(environ), runner=runner)
        for r in res["rows"]:
            print(f"stage: transcription {r['backend']}: {'TRANSCRIBED' if r['ok'] and r.get('heard') else 'CONNECTED' if r['ok'] else 'FAILED'} in {r['seconds']} s" + (f" (heard: {r['heard']!r})" if r["ok"] and r.get("heard") else "") + ("" if r["ok"] else f": {r['why']}"))
        shared.mkdir(parents=True, exist_ok=True)
        (shared / TR.RANKINGS_FILE).write_text(json.dumps({"measured_at": now, "ranking": res["ranking"], "rows": res["rows"]}, indent=1) + "\n", encoding="utf-8")
        print(f"stage: transcription order now: {', '.join(res['ranking']) or 'none answered'} (then the others; VibeVoice runs only when VIBEASR_COMMAND is set)")
        return 0
    if args.action == "bakeoff":
        try:
            cfg = BO.load_config(args.bakeoff_config)
        except (OSError, ValueError) as exc:
            print(f"stage: {exc}")
            return 1
        urls = [u.strip() for u in args.bakeoff_urls.split(",") if u.strip()] or cfg["urls"]
        if not urls:
            print("stage: bake-off: no public addresses: give them in bakeoff.yaml (urls) or with --bakeoff-urls; none is built into the tool")
            return 1
        report = BO.run_bakeoff(cfg, urls=urls, runner=runner or subprocess.run)
        print(BO.format_report(report, urls))
        store.write_shared("bakeoff", {"at": now, "urls": urls, "report": report})                                  # the comparison is kept in the repository, not only in the run log
        print("stage: bake-off saved: data/_shared/bakeoff.json")
        return 0
    slugs = list_brand_slugs(args.brands_dir)
    if args.brand not in slugs:
        print(f"stage: transcribe needs --brand (one of: {', '.join(slugs)})")
        return 1
    if bool(args.file) == bool(args.url):
        print("stage: transcribe needs exactly one of --file and --url")
        return 1
    path = Path(args.file) if args.file else Path(args.data_dir) / "_probe" / "download.bin"
    if args.url:
        path.parent.mkdir(parents=True, exist_ok=True)
        problem = _download(args.url, path)
        if problem:
            print(f"stage: {args.brand}: the address was not read: {problem}")
            return 1
    if not path.is_file():
        print(f"stage: {args.brand}: {path} does not exist")
        return 1
    try:
        out = TR.transcribe(path, workdir=Path(args.data_dir) / "_probe", which=(lambda name: name) if runner else None, backends=TR.order(shared), key=TR.env_key(environ), post=post, loader=loader, language=args.language or None, cloudflare=TR.env_cloudflare(environ), vibevoice=TR.env_vibevoice(environ), runner=runner)
    except Exception as exc:                                                                  # noqa: BLE001
        print(f"stage: {args.brand}: transcription failed: {getattr(exc, 'message', exc)}")
        return 1
    if out.get("converted"):
        print(f"stage: {args.brand}: the download ({path.name}) was converted to {out['converted']} because the hosted backend only accepts audio and video files of known types")
    TR.save_transcript(store, args.brand, {"sha": TR.sha_of(path), "source": args.source or args.url or args.file, "backend": out["backend"], "language": out["language"], "text": out["text"], "seconds": out["seconds"]}, now)
    print(f"stage: {args.brand}: transcribed {len(out['text'])} characters with {out['backend']} in {out['seconds']} s" + (f" (after: {'; '.join(f'{b}: {w}' for b, w in out['tried'])})" if out["tried"] else ""))
    return 0
