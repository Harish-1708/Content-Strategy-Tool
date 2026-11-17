"""The `skills` and `canon` commands (Batch 2): the external skills repository and the two registries. Nothing here posts, sends or changes anything on any platform."""
import json
from pathlib import Path

from . import canon as CANON
from . import skills as SK
from .brands import list_brand_slugs


def add_parsers(sub, common):
    p = sub.add_parser("skills", help="the Aaron marketing skills, connected externally: status, verify, update-report, catalogue")
    common(p)
    p.add_argument("--action", choices=["status", "verify", "update-report", "catalogue", "read"], default="status")
    p.add_argument("--skill", default="", help="read: the skill path (for example launch/research/positioning-mapper); refused with the reason when the skill waits for numbers or never auto-runs")
    p.add_argument("--requested", action="store_true", help="read: the skill is being asked for by name (lets a reachable-only skill be read; never a waiting one)")
    p.add_argument("--skills-config", default=SK.CONFIG_FILE, help="the skills policy file (default external_skills.yaml)")
    p.add_argument("--skills-dir", default="", help="the checkout of the skills repository (default: AARON_SKILLS_DIR, then external/aaron-marketing-skills)")

    p = sub.add_parser("canon", help="the narrative canon and the offer-claims ledger: seed, status, gate, export, history")
    common(p)
    p.add_argument("--brand", default="all")
    p.add_argument("--action", choices=["seed", "status", "gate", "export", "history", "refresh", "foundation"], default="status")
    p.add_argument("--text", default="", help="gate: the text to check against the claims ledger")
    p.add_argument("--steps", default="", help="foundation: only these sections, comma-separated (positioning, audience_beliefs, category_narrative, strategic_narrative, message_house, message_system, language, story_bank, proof_points)")
    p.add_argument("--redo", action="store_true", help="foundation: propose a new version of a section that already has a proposal or approved text")
    p.add_argument("--max-steps", type=int, default=0, help="foundation: at most this many steps in one run (0 = all); the next run continues")
    p.add_argument("--max-queries", type=int, default=12, help="refresh: searches per brand")
    p.add_argument("--max-pages", type=int, default=12, help="refresh: pages read per brand")
    p.add_argument("--skills-config", default=SK.CONFIG_FILE, help="foundation: the skills policy file")
    p.add_argument("--skills-dir", default="", help="foundation: the checkout of the skills repository")


def run_skills(args, store, now, *, environ=None, runner=None):
    import subprocess
    runner = runner or subprocess.run
    cfg = SK.load_config(args.skills_config)
    root = Path(args.skills_dir) if args.skills_dir else SK.checkout_dir(cfg, environ)
    if args.action == "read":
        trusted = SK.verify(cfg, root, runner=runner)                                         # the one door to skill text checks provenance first
        if not trusted["ok"]:
            for problem in trusted["problems"]:
                print(f"skills: PROBLEM: {problem}")
            print("skills: the skill text is NOT handed over: the connection cannot be trusted")
            return 1
        try:
            skill = SK.read_skill(cfg, root, args.skill, requested=args.requested)
        except SK.SkillsError as exc:
            print(f"skills: {exc}")
            return 2
        print(SK.as_data(skill))
        return 0
    trusted = SK.verify(cfg, root, runner=runner)
    if args.action == "update-report":
        for problem in trusted["problems"]:
            print(f"skills: PROBLEM: {problem}")
        try:
            report = SK.update_report(cfg, root, runner=runner)
        except SK.SkillsError as exc:
            print(f"skills: {exc}")
            return 1
        print(f"skills: pinned {report['pinned'][:10]}, upstream {report['latest'][:10]}: " + ("up to date" if report["up_to_date"] else "CHANGED upstream (nothing is adopted until the pinned commit in external_skills.yaml is changed by hand)"))
        for label, key in (("changed", "skills_changed"), ("added", "skills_added"), ("removed", "skills_removed"), ("renamed", "skills_renamed")):
            for name in report[key]:
                print(f"skills:   {label}: {name}")
        for line in report["runtime_changed"]:
            print(f"skills:   runtime file {line}")
        return 0 if trusted["ok"] else 1
    result = trusted
    for problem in result["problems"]:
        print(f"skills: PROBLEM: {problem}")
    for note in result["notes"]:
        print(f"skills: note: {note}")
    if args.action == "verify":
        print(f"skills: {'OK' if result['ok'] else 'NOT OK'}: {result['count']} skills at {result['commit'][:10] or 'no commit'}")
        return 0 if result["ok"] else 1
    rows = SK.catalogue(cfg, root)
    by_verdict, waiting, never = {}, [r for r in rows if r["state"] == SK.WAITING], [r for r in rows if not r["auto_run"]]
    for r in rows:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    print(f"skills: {len(rows)} skills ({', '.join(f'{v} {k}' for k, v in sorted(by_verdict.items()))}); {len(waiting)} built ready and waiting for access (NEEDS_INPUT), {len(never)} reachable but never auto-run; connection {'OK' if result['ok'] else 'NOT OK'}")
    for r in waiting:
        print(f"skills:   waiting for the stores' own numbers: {r['path']}")
    if args.action == "catalogue":
        if not result["ok"]:
            print("skills: the catalogue is NOT written: the connection cannot be trusted (see the problems above)")
            return 1
        target = Path(store.data_dir) / "_shared" / "skills_catalogue.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"at": now, "commit": result["commit"], "ok": result["ok"], "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"skills: catalogue written to {target} (each skill's hash: git keeps its history)")
    return 0 if result["ok"] else 1


def run_canon(args, store, now, *, clients=None):
    """clients: a function returning (claude, search, get) for the refresh and the foundation chain (the CLI builds it from the routed AI client and Serper)."""
    slugs = list_brand_slugs(args.brands_dir) if args.brand in ("", "all") else [args.brand]
    if not slugs:
        print(f"canon: no brand folders found in {args.brands_dir}: nothing was done")
        return 1
    worst = 0
    for slug in slugs:
        reg = CANON.Registry(store.data_dir, slug)
        if args.action == "seed":
            seed_file = Path(args.brands_dir) / slug / "claims_seed.yaml"
            seeds = CANON.load_seed(seed_file)
            if not seeds:
                print(f"canon: {slug}: NO claims seed ({seed_file} is missing, damaged or empty): nothing was seeded")
                worst = max(worst, 1)
                continue
            added, there = CANON.apply_seed(reg, seeds, now)
            print(f"canon: {slug}: claims seeded from the story-bank gaps: {added} added, {there} already in the ledger (never as verified)")
        elif args.action == "refresh":
            from . import brands as BR
            from . import story_refresh as SR
            claude, search, get = clients() if clients else (None, None, None)
            if claude is None or search is None or get is None:
                print(f"canon: {slug}: the refresh needs the AI client (ANTHROPIC_API_KEY) and Serper (SERPER_API_KEY): "
                      + ("no AI client" if claude is None else "no Serper search") + " is available in this run, so nothing was refreshed")
                worst = max(worst, 1)
                continue
            try:
                name = getattr(BR.load_brand(slug, args.brands_dir), "name", slug) or slug
            except Exception:                                                                  # noqa: BLE001 - a brand file problem must not stop the refresh
                name = slug
            seeds = [s["text"] for s in CANON.load_seed(Path(args.brands_dir) / slug / "claims_seed.yaml")]
            rep = SR.run(reg, name, seeds, search=search, get=get, claude=claude, now=now, max_queries=args.max_queries, max_pages=args.max_pages)
            print(f"canon: {slug}: refresh: {rep['queries']} searches, {rep['pages']} pages read, {rep['facts_seen']} facts seen: {rep['added']} added as UNVERIFIED, {rep['conflicts']} kept as CONFLICTS "
                  f"for you to decide, {rep['duplicates']} already known, {len(rep['rejected'])} dropped (no source, quotation not in the page, or not confirmed); {rep['calls']} AI call(s). "
                  "Nothing was deleted or changed, and nothing is verified or canon.")
            for line in rep["rejected"][:8]:
                print(f"canon:   dropped: {line}")
            for line in rep["notes"][:8]:
                print(f"canon:   note: {line}")
        elif args.action == "foundation":
            from . import foundation as FD
            cfg_path, trusted_root = args.skills_config, None
            try:
                cfg = SK.load_config(cfg_path)
            except SK.SkillsError as exc:
                print(f"canon: {exc}")
                return 1
            trusted_root = Path(args.skills_dir) if args.skills_dir else SK.checkout_dir(cfg)
            trusted = SK.verify(cfg, trusted_root)
            if not trusted["ok"]:
                for problem in trusted["problems"]:
                    print(f"canon: PROBLEM: {problem}")
                print("canon: the foundation chain does not run: the skills connection cannot be trusted")
                return 1
            claude = clients()[0] if clients else None
            if claude is None:
                print(f"canon: {slug}: the foundation chain needs the AI client (ANTHROPIC_API_KEY): none is available in this run")
                worst = max(worst, 1)
                continue
            brand_text, story_text = FD.brand_material(args.brands_dir, slug)
            only = {s.strip() for s in args.steps.split(",") if s.strip()} or None
            rep = FD.run_chain(reg, cfg, trusted_root, claude, brand_text=brand_text, story_text=story_text, now=now, only=only, redo=args.redo, max_steps=args.max_steps or None)
            for line in FD.report_lines(slug, rep):
                print(line)
            proposed = sum(1 for r in rep if r["result"] == "proposed")
            print(f"canon: {slug}: foundation: {proposed} section(s) PROPOSED for your decision in the Canon tab; nothing is canonical until you approve it")
            if any(r["result"] in ("failed", "rejected") for r in rep):
                worst = max(worst, 1)
        elif args.action == "history":
            import os
            import subprocess
            target = Path(args.data_dir) / slug / "registry"
            try:
                spec = os.path.relpath(target.resolve(), Path.cwd())
            except ValueError:
                spec = ""
            if not spec or spec.startswith(".."):
                print(f"canon: {slug}: the data directory {args.data_dir} is outside this repository, so its git history cannot be read")
                worst = max(worst, 1)
                continue
            done = subprocess.run(["git", "log", "--format=%h %cs %s", "--", spec], capture_output=True, text=True)
            lines = (done.stdout or "").strip().splitlines()
            print(f"canon: {slug}: {len(lines)} commit(s) touched the registry" + ("" if done.returncode == 0 else f" (git said: {(done.stderr or '').strip()[:100]})"))
            for line in lines[:20]:
                print(f"canon:   {line}")
            if len(lines) > 20:
                print(f"canon:   ... {len(lines) - 20} older commit(s) not shown")
        elif args.action == "export":
            target = Path(store.data_dir) / slug / "registry"
            target.mkdir(parents=True, exist_ok=True)
            (target / "canon_rows.json").write_text(json.dumps(reg.canon_rows(), indent=1, ensure_ascii=False), encoding="utf-8")
            (target / "claim_rows.json").write_text(json.dumps(reg.claim_rows(), indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"canon: {slug}: rows written to {target}")
        elif args.action == "gate":
            if not args.text.strip():
                print("canon: gate needs --text")
                return 2
            result = reg.gate(args.text)
            print(f"canon: {slug}: {result['verdict']}")
            for f in result["findings"]:
                print(f"canon:   {f['result']}: {f['sentence'][:100]}: {f['why']}")
            worst = max(worst, {"OK": 0, "NEEDS_REVIEW": 1, "BLOCKED": 2}[result["verdict"]])
        else:
            state = reg.state()
            counts = {}
            for c in state["claims"].values():
                counts[c["status"]] = counts.get(c["status"], 0) + 1
            canon_counts = {}
            for e in state["canon"].values():
                canon_counts[e["status"]] = canon_counts.get(e["status"], 0) + 1
            print(f"canon: {slug}: claims {dict(sorted(counts.items()))}; canon {dict(sorted(canon_counts.items())) or 'nothing proposed yet'}; "
                  f"{len(reg.canonical())} section(s) approved by the owner" + (f"; {reg.damaged} damaged event line(s) skipped" if reg.damaged else "")
                  + (f"; WARNING: {len(reg.chain_problems)} line(s) of the log do not follow the line before them (edited, shortened or reordered): {reg.chain_problems[0]}" if reg.chain_problems else "")
                  + (f"; {len(reg.rejected)} event(s) ignored because they break the rules" if reg.rejected else ""))
    return worst if args.action in ("gate", "seed", "history", "refresh", "foundation") else 0
