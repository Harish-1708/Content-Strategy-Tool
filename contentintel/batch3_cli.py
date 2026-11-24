"""`batch3`: the Batch 3 tools. forecast (TimesFM 3.0: forecast, anomalies, momentum, Idea Bank timeliness), stock (the CLIP-indexed stock corpus), mpt (MoneyPrinterTurbo drafts and hook A/B renders: OFF until a
host is ready, never publishes) and concat (the Concat go/no-go gate on GitHub Actions)."""
import json
import os
import subprocess
import urllib.request
from pathlib import Path

from . import concat_gate as CG
from . import forecast as FC
from . import mpt as MPT
from . import openmontage as OM
from .brands import list_brand_slugs


def add_parsers(sub, common):
    p = sub.add_parser("batch3", help="forecasting (TimesFM), the CLIP stock corpus, MoneyPrinterTurbo drafts (off until a host is ready) and the Concat gate")
    common(p)
    p.add_argument("--action", choices=["forecast", "stock-index", "stock-search", "mpt-drafts", "mpt-hooks", "mpt-status", "concat-discover", "concat-gate", "concat-auto"], required=True)
    p.add_argument("--brand", default="all")
    p.add_argument("--query", default="", help="stock-search: the script line or idea to find a picture for")
    p.add_argument("--hooks", default="", help="mpt-hooks: other opening lines, separated by |")
    p.add_argument("--concept", type=int, default=1, help="mpt-hooks: which concept of the latest brief (1 = the first)")
    p.add_argument("--prove-load", action=argparse_bool(), default=True, help="forecast: load the model and forecast a built-in test series even when there is too little history for a backtest, so the install, the weights download and the call are proven (ON by default)")
    p.add_argument("--backtest", action=argparse_bool(), default=True, help="forecast: run the backtest gate before trusting the model (ON by default)")
    p.add_argument("--mpt-config", default=MPT.CONFIG)
    p.add_argument("--concat-config", default="concat.yaml")
    p.add_argument("--force-dropped", action="store_true", help="concat-*: run the Concat gate although Concat is DROPPED (concat.yaml): for a person who wants to look at it once more")


def argparse_bool():
    import argparse
    return argparse.BooleanOptionalAction


def _slugs(args):
    return list_brand_slugs(args.brands_dir) if args.brand == "all" else [args.brand]


def _ideas(store, slug):
    bank = store.read_ideabank(slug) if hasattr(store, "read_ideabank") else {"items": {}}
    return [{"id": iid, "title": str(i.get("title", "")), "topic": str(i.get("topic") or i.get("category") or "")} for iid, i in list(bank["items"].items())[-60:] if isinstance(i, dict)]


def run_forecast(args, store, now, *, model_loader=FC.load_timesfm):
    """Per brand: the series this tool really has, the forecast, anomalies and momentum, and the timeliness of the Idea Bank items. The model is used only if the backtest across every brand's series passes."""
    all_series = {slug: FC.series_for_brand(store, slug) for slug in _slugs(args)}
    pooled = [v for s in all_series.values() for v in s.values()]
    model_fn, gate = None, None
    try:
        loaded = model_loader()
    except ImportError as exc:
        loaded = None
        print(f"forecast: TimesFM is not on this machine ({exc}); the naive baseline is used and every answer says so")
    except Exception as exc:                                                                                    # noqa: BLE001
        loaded = None
        print(f"forecast: TimesFM could not be loaded ({type(exc).__name__}: {str(exc)[:120]}); the naive baseline is used")
    if loaded is not None and getattr(args, "prove_load", True):
        try:
            sample = [10 + (i % 7) * 2 + i * 0.5 for i in range(48)]
            out = loaded(sample, 4)
            print(f"forecast: MODEL LOAD PROVEN: TimesFM 3.0 forecast a 48-point test series: {[round(x, 1) for x in out]} (this proves the install, the weights and the call, not that it beats the baseline)")
        except Exception as exc:                                                                                # noqa: BLE001
            print(f"forecast: MODEL LOAD FAILED: {type(exc).__name__}: {str(exc)[:200]}; the model is NOT used")
            loaded = None
    if loaded is not None and args.backtest:
        gate = FC.backtest(pooled, loaded)
        model_fn = loaded if gate["gate"] == "PASS" else None
        if model_fn is None:
            print(f"forecast: the model did NOT pass the backtest ({gate['gate']}: {gate['model_wins']} of {gate['tested']}); the naive baseline is used until it does")
    elif loaded is not None:
        print("forecast: --no-backtest: the model is used WITHOUT having been shown to beat the baseline")
        model_fn = loaded
    for slug in all_series:
        analysis = FC.analyse_brand(store, slug, model_fn=model_fn, ideas=_ideas(store, slug))
        analysis["backtest"], analysis["at"] = gate, now
        store.write_extra(slug, "forecasts", analysis)
        for line in FC.format_report(slug, analysis, gate):
            print(line)
    return 0


def _fetch(url):
    if not url.startswith("https://"):
        raise ValueError("only https pictures are read")
    req = urllib.request.Request(url, headers={"User-Agent": "KelsonResearchBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:                                                      # noqa: S310 - an https picture address from the brand's own assets
        return resp.read(8 * 1024 * 1024)


def _brand_terms(args, store, slug):
    """The words that make a picture relevant to a brand, read from the brand's own files at run time: its name, the keywords and topics in its research file and its catalogue's product terms. NEVER its competitors' names (Allbirds shoes are not Swoveralls pictures)."""
    from . import assets_find as AF
    return AF.terms_from_files(args.brands_dir, slug, store)


def run_stock(args, store, now, *, embedders=OM.clip_embedders, fetch=_fetch):
    code = 0
    try:
        embed_text, embed_image = embedders()
    except ImportError as exc:
        print(f"stock: the CLIP model is not on this machine: {exc}")
        return 1
    for slug in _slugs(args):
        corpus = OM.StockCorpus(Path(args.data_dir) / slug / "extras" / "stock_corpus.json")
        if args.action == "stock-index":
            assets = [a for a in (store.read_extra(slug, "assets") or {}).get("assets", []) if a.get("kind", "image") == "image"]
            terms = _brand_terms(args, store, slug)
            fit = [a for a in assets if OM.on_brand(a, terms)]
            for a in assets:
                if a not in fit:
                    print(f"stock: {slug}:   dropped {str(a.get('title') or a.get('url'))[:60]!r}: off-target for this brand (its title mentions none of: {', '.join(terms[:4])})")
            done, skipped = OM.index_assets(fit, corpus, embed_image, fetch)
            corpus.save()
            print(f"stock: {slug}: {done} picture(s) indexed, {len(corpus.items)} in the corpus, {len(skipped)} skipped, {len(assets) - len(fit)} dropped as off-target")
            for title, why in skipped[:8]:
                print(f"stock: {slug}:   skipped {title[:60]!r}: {why}")
        else:
            if not args.query.strip():
                print("stock: --query is empty")
                return 1
            found = corpus.search(embed_text(args.query))
            kept = store.read_extra(slug, "stock_matches") or {"searches": []}
            kept["searches"] = ([{"at": now, "query": args.query, "matches": found, "corpus_size": len(corpus.items)}] + kept["searches"])[:30]
            store.write_extra(slug, "stock_matches", kept)
            print(f"stock: {slug}: {len(found)} picture(s) match {args.query!r} out of {len(corpus.items)}" + ("" if found else " (nothing close enough: none is forced)"))
            for f in found:
                print(f"stock: {slug}:   {f['score']:.2f} {f['title'][:60]!r} [{f['license'].upper()}] {f['url']} | {f['credit'][:80]}")
    return code


def run_mpt(args, store, now, *, environ=None, http=MPT.default_http, fetch=None, probe=OM.probe_video, sleep=None, getter=None):
    environ = environ if environ is not None else os.environ
    cfg = MPT.load_config(args.mpt_config)
    if args.action == "mpt-status":
        on = "ON" if cfg["enabled"] else "OFF"
        print(f"mpt: MoneyPrinterTurbo is {on}; host {'set' if (environ.get('MPT_URL') or '').startswith('https://') else 'NOT set'}; stock footage from {MPT.stock_source(cfg, environ)} (Pexels has paused new keys; Pixabay is the default until one can be made); publishing is off and has no switch (there is no publish function in this tool)")
        for provider, state, detail in MPT.check_stock_keys(environ, getter or MPT.default_get_json):
            print(f"mpt: {provider} key: {state}: {detail}")
        return 0
    try:
        base = MPT.ensure_on(cfg, environ)
    except MPT.MptOff as exc:
        print(f"mpt: {exc}")
        return 0
    if environ.get("MPT_TOKEN") and http is MPT.default_http:
        import functools
        http = functools.partial(MPT.default_http, token=environ["MPT_TOKEN"].strip())                       # the identity token of the private host
    client = MPT.Client(base, http=http, **({"sleep": sleep} if sleep else {}))
    code = 0
    for slug in _slugs(args):
        last = store.read_extra(slug, "last_brief")
        if not last:
            print(f"mpt: {slug}: no brief has been written yet")
            continue
        try:
            if args.action == "mpt-drafts":
                jobs = MPT.jobs_from_brief(last.get("brief") or {}, cfg, status=last.get("status", ""), violations=last.get("violations") or [], brand=slug, source=MPT.stock_source(cfg, environ))
            else:
                concept = (last.get("brief") or {}).get("concepts", [])[max(0, args.concept - 1):][:1]
                if not concept:
                    raise MPT.MptError(f"the brief has no concept {args.concept}")
                if last.get("status") != "ready":
                    raise MPT.MptError("the brief is not executable: its status is not 'ready'")
                jobs = MPT.hook_variants(concept[0], [h for h in args.hooks.split("|")], cfg, brand=slug, source=MPT.stock_source(cfg, environ))
        except MPT.MptError as exc:
            print(f"mpt: {slug}: {exc}")
            code = 1
            continue
        manifest = OM.Manifest(f"mpt-{slug}-{now[:10]}", Path(args.data_dir) / slug / "extras" / "mpt_manifest.json", now)
        gate = {"min_duration_s": 5, "has_audio": True, "min_bytes": 50_000}
        results = MPT.render(client, jobs, cfg, manifest=manifest, outdir=Path(args.data_dir) / "_drafts" / slug, fetch=fetch or (lambda u: urllib.request.urlopen(u, timeout=120).read()), probe=probe, gate=gate)   # noqa: S310
        manifest.close(now)
        kept = store.read_extra(slug, "mpt_drafts") or {"drafts": []}
        kept["drafts"] = ([{"at": now, **{k: v for k, v in r.items() if k != "measured"}, "measured": r.get("measured")} for r in results] + kept["drafts"])[:60]
        store.write_extra(slug, "mpt_drafts", kept)
        for r in results:
            print(f"mpt: {slug}: {r['label']}: " + (f"error: {r['error']}" if r.get("error") else f"task {r.get('task_id')} done" + (f", {'passed' if r.get('passed') else 'FAILED'} the quality gate" + ("" if r.get("passed") else f" ({'; '.join(r.get('failures', []))})") if "passed" in r else "")))
        code = code or (1 if any(r.get("error") or r.get("passed") is False for r in results) else 0)
    return code


def run_concat(args, store, now, *, runner=subprocess.run, probe=OM.probe_video):
    cfg = CG.load_config(args.concat_config)
    if cfg.get("dropped") and not getattr(args, "force_dropped", False):
        print(f"concat: Concat is DROPPED (decided {cfg.get('dropped_on', '')}): it is a desktop GUI editor with no headless render command (Concat gate #2: every help command opened a window, none of six guessed render commands made a video), and a host for it would need a desktop session and GUI automation. Nothing is run. Video drafts come from MoneyPrinterTurbo. Pass --force-dropped to look at it once more.")
        return 0
    work = Path(args.data_dir) / "_probe" / "concat"
    if args.action == "concat-discover":
        for line in CG.format_report({"verdict": "NOT RUN", "runs": [], "reason": "discovery only: this step prints what the installed Concat says about its command line"}, CG.discover(cfg, runner)):
            print(line)
        return 0
    if args.action == "concat-auto":
        result, discovered, tried = CG.run_auto(cfg, work, runner=runner, probe=probe)
        for line in CG.format_report(result, discovered):
            print(line)
        print(f"concat: {len(tried)} render command(s) tried: " + " | ".join(tried))
    else:
        result = CG.run_gate(cfg, work, runner=runner, probe=probe)
        for line in CG.format_report(result):
            print(line)
    Path(args.data_dir, "_shared").mkdir(parents=True, exist_ok=True)
    Path(args.data_dir, "_shared", "concat_gate.json").write_text(json.dumps({"at": now, **result}, indent=1), encoding="utf-8")
    return 0 if result["verdict"] == "GO" or (result["verdict"] == "NOT RUN" and args.action != "concat-auto") else 1


def run_batch3(args, store, now, **kw):
    if args.action == "forecast":
        return run_forecast(args, store, now, **{k: v for k, v in kw.items() if k == "model_loader"})
    if args.action in ("stock-index", "stock-search"):
        return run_stock(args, store, now, **{k: v for k, v in kw.items() if k in ("embedders", "fetch")})
    if args.action in ("mpt-drafts", "mpt-hooks", "mpt-status"):
        return run_mpt(args, store, now, **{k: v for k, v in kw.items() if k in ("environ", "http", "fetch", "probe", "sleep", "getter")})
    return run_concat(args, store, now, **{k: v for k, v in kw.items() if k in ("runner", "probe")})
