"""The `skills` and `canon` commands (Batch 2): the external skills repository and the two registries. Nothing here posts, sends or changes anything on any platform."""
import json
from pathlib import Path

from . import canon as CANON
from . import verify_claims as CANON_VERIFY
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
    p.add_argument("--action", choices=["seed", "status", "gate", "export", "history", "refresh", "foundation", "reclassify-conflicts", "verify-claims", "stage"], default="status")
    p.add_argument("--text", default="", help="gate: the text to check against the claims ledger")
    p.add_argument("--steps", default="", help="foundation: only these sections, comma-separated (positioning, audience_beliefs, category_narrative, strategic_narrative, message_house, message_system, language, story_bank, proof_points)")
    p.add_argument("--redo", action="store_true", help="foundation: propose a new version of a section that already has a proposal or approved text")
    p.add_argument("--max-steps", type=int, default=0, help="foundation: at most this many steps in one run (0 = all); the next run continues")
    p.add_argument("--max-queries", type=int, default=12, help="refresh: searches per brand")
    p.add_argument("--max-pages", type=int, default=12, help="refresh: pages read per brand")
    p.add_argument("--max-new", type=int, default=20, help="refresh: new entries added per brand in one refresh (the pages after that are left for the next refresh and spend no AI calls)")
    p.add_argument("--stage", default="", help="stage: which stage to run (competitor_ideas, creator_chain, listen_learn, voice_creative, seo_lens, storyboard)")
    p.add_argument("--max-claims", type=int, default=0, help="verify-claims: check at most this many claims in one run (0 = every claim that is waiting, up to 60)")
    p.add_argument("--max-minutes", type=int, default=0, help="verify-claims: stop starting new claims after this many minutes (0 = no limit); the claims left are picked up by the next run, and every claim's result is printed as soon as it is known")
    p.add_argument("--verify-calls", type=int, default=0, help="the AI and page-reading calls the claim check may use in one run (0 = sized to the claims waiting: about five a claim)")
    p.add_argument("--max-calls", type=int, default=0, help="at most this many model calls in one run (a retry counts; one batch of up to 20 pairs is one call). 0 = no limit for foundation and reclassify-conflicts, 48 for refresh")
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
    by_verdict, waiting, never = {}, [r for r in rows if r["state"] == SK.WAITING], [r for r in rows if not r["auto_run"] and r["state"] != SK.WAITING]          # the waiting ones are counted once, as waiting
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


def _claim_verifier(reg, slug, args, claude, search, get, now, announce=False):
    """(verifier, report): verifier(ids) checks those claims from public sources with the pool model (the tool finds the sources itself: Serper search and a page fetch) and returns the ids that became verified.
    None when this run has no AI client, no search or no page fetch (the foundation then works as before; verify-claims says what is missing)."""
    report = {"calls": 0, "errors": 0, "notes": [], "verified": [], "checked": 0, "outcomes": {}}
    lacking = [label for label, piece in (("AI client", claude), ("Serper search (SERPER_API_KEY)", search), ("page fetch", get)) if piece is None]
    if lacking:
        if announce:
            print(f"canon: {slug}: checking claims needs the AI client, Serper search and a page fetch; this run has no {' and no '.join(lacking)}, so nothing was checked")
        return None, report
    from . import brands as BR
    try:
        name = getattr(BR.load_brand(slug, args.brands_dir), "name", slug) or slug
    except Exception:                                                                  # noqa: BLE001 - a brand file problem must not stop the check
        name = slug
    tried = set()
    waiting = len(CANON_VERIFY.targets(reg, 1000)) if not args.max_claims else args.max_claims
    budget = args.verify_calls or CANON_VERIFY.auto_budget(waiting)                      # sized to the work waiting, never a fixed small number; the rate-limit-aware routing is what makes a big budget safe

    def progress(event, cid, what, position, total):
        """Printed the moment each claim starts and again when it is done, so a run that is cut off still leaves its evidence in the log."""
        if event == "start":
            print(f"canon: {slug}: progress: claim {position} of {total} starting: {cid}: {str(what.get('text', ''))[:100]}", flush=True)
        else:
            entry = report["log"][-1] if report.get("log") else None
            print(f"canon: {slug}: progress: claim {position} of {total} done: {cid} -> {str(what).upper()}", flush=True)
            if entry:
                for line in CANON_VERIFY.log_lines({"log": [entry]}):
                    print(line, flush=True)

    def verifier(ids):
        new = [i for i in ids if i not in tried]
        report["checked"] += len(new)
        done = CANON_VERIFY.verify_ids(reg, name, ids, search=search, get=get, claude=claude, now=now, max_calls=budget, report=report, tried=tried,
                                       progress=progress if announce else None, deadline=getattr(args, "_deadline", None), recheck=bool(getattr(args, "action", "") == "reclassify-conflicts"))
        verified_now = [i for i in done if report["outcomes"].get(i) == "verified"]
        report["verified"].extend(verified_now)
        report.setdefault("single", []).extend(i for i in verified_now if reg.state()["claims"][i].get("single_source"))
        return done
    return verifier, report


def _outcome_tail(vreport):
    """What else the check found: claims left unverified for the owner (no source close to the fact), claims blocked because sources contradict."""
    outcomes = list(vreport.get("outcomes", {}).values())
    parts = []
    if vreport.get("single"):
        parts.append(f"{len(vreport['single'])} VERIFIED FROM ONE SOURCE, flagged for you to spot-check (the Claims tab Reason column says so): " + ", ".join(vreport["single"][:8]))
    if outcomes.count("unverified"):
        parts.append(f"{outcomes.count('unverified')} with no source close to the fact left UNVERIFIED for you to decide (a blocked one is no longer blocked)")
    if outcomes.count("conflicted"):
        parts.append(f"{outcomes.count('conflicted')} blocked because sources contradict")
    return ("; " + "; ".join(parts)) if parts else ""


def route_report(slug, claude, label):
    """After a run that asked a model: how many calls the free pool answered and how many Claude did, and every note the routed client recorded (a pool that could not be used is said, never silent)."""
    calls = list(getattr(claude, "calls", []) or [])
    notes = list(dict.fromkeys(getattr(claude, "notes", []) or []))
    if not calls and not notes:
        return
    pool, plain = calls.count("gateway"), calls.count("claude")
    print(f"canon: {slug}: {label}: model route: {pool} call(s) answered by the free pool, {plain} by Claude")
    for note in notes[:10]:
        print(f"canon:   route note: {note}")


def run_canon(args, store, now, *, clients=None):
    """clients: a function returning (claude, search, get) for the refresh and the foundation chain (the CLI builds it from the routed AI client and Serper)."""
    slugs = list_brand_slugs(args.brands_dir) if args.brand in ("", "all") else [args.brand]
    if not slugs:
        print(f"canon: no brand folders found in {args.brands_dir}: nothing was done")
        return 1
    worst = 0
    known = set(list_brand_slugs(args.brands_dir))
    missing = [s for s in slugs if s not in known and not (Path(args.brands_dir) / s).is_dir()]
    if missing:
        print(f"canon: no brand folder named {', '.join(missing)} in {args.brands_dir} (the brands are: {', '.join(sorted(known)) or 'none'}): nothing was done")
        return 1
    import time as _t
    args._deadline = (_t.monotonic() + args.max_minutes * 60) if getattr(args, "max_minutes", 0) else None
    claims_left = args.max_claims if args.action == "verify-claims" and args.max_claims else None
    for slug in slugs:
        reg = CANON.Registry(store.data_dir, slug)
        if args.action == "seed":
            seed_file = Path(args.brands_dir) / slug / "claims_seed.yaml"
            seeds = CANON.load_seed(seed_file)
            if not seeds:
                print(f"canon: {slug}: NO claims seed ({seed_file} is missing, damaged or empty): nothing was seeded")
                worst = max(worst, 1)
                continue
            added, there, differs = CANON.apply_seed(reg, seeds, now)
            print(f"canon: {slug}: claims seeded from the story-bank gaps: {added} added, {there} already in the ledger (never as verified)"
                  + (f"; {differs} of those carry another status or category than the seed says (the ledger is append-only, so a seed never changes an existing claim)" if differs else ""))
        elif args.action == "refresh":
            from . import brands as BR
            from . import story_refresh as SR
            claude, search, get = clients() if clients else (None, None, None)
            lacking = [label for label, piece in (("AI client (ANTHROPIC_API_KEY)", claude), ("Serper search (SERPER_API_KEY)", search), ("page fetch", get)) if piece is None]
            if lacking:
                print(f"canon: {slug}: the refresh needs the AI client, Serper search and a page fetch; this run has no {' and no '.join(lacking)}, so nothing was refreshed")
                worst = max(worst, 1)
                continue
            try:
                name = getattr(BR.load_brand(slug, args.brands_dir), "name", slug) or slug
            except Exception:                                                                  # noqa: BLE001 - a brand file problem must not stop the refresh
                name = slug
            seeds = [s["text"] for s in CANON.load_seed(Path(args.brands_dir) / slug / "claims_seed.yaml")]
            rep = SR.run(reg, name, seeds, search=search, get=get, claude=claude, now=now, max_queries=args.max_queries, max_pages=args.max_pages, max_new=args.max_new, max_calls=args.max_calls or 48)
            print(f"canon: {slug}: refresh: {rep['queries']} searches, {rep['pages']} pages read, {rep['facts_seen']} facts seen: {rep['added']} added as UNVERIFIED, {rep['conflicts']} kept as CONFLICTS "
                  f"for you to decide, {rep['duplicates']} already known, {len(rep['rejected'])} dropped (no source, quotation not in the page, or not confirmed); {rep['calls']} AI call(s). "
                  "Nothing was deleted or changed, and nothing is verified or canon.")
            for line in rep["rejected"][:20]:
                print(f"canon:   dropped: {line}")
            if len(rep["rejected"]) > 20:
                print(f"canon:   dropped: ... and {len(rep['rejected']) - 20} more")                          # the count printed above and the lines printed now always agree
            for line in rep["notes"][:20]:
                print(f"canon:   note: {line}")
            if rep["errors"] and not rep["pages_ok"]:
                print(f"canon: {slug}: the refresh FAILED: {rep['errors']} error(s) and not one page was read and reviewed to the end (see the notes above)")
                worst = max(worst, 1)
            route_report(slug, claude, "refresh")
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
            claude, search, get = clients() if clients else (None, None, None)
            brand_text, story_text = FD.brand_material(args.brands_dir, slug)
            only = {s.strip() for s in args.steps.split(",") if s.strip()} or None
            verifier, vreport = _claim_verifier(reg, slug, args, claude, search, get, now)
            rep = FD.run_chain(reg, cfg, trusted_root, claude, brand_text=brand_text, story_text=story_text, now=now, only=only, redo=args.redo, max_steps=args.max_steps or None,
                               max_calls=args.max_calls or None, verify_connection=False, verifier=verifier)               # the connection was verified above
            for line in FD.report_lines(slug, rep):
                print(line)
            proposed = sum(1 for r in rep if r["result"] == "proposed")
            print(f"canon: {slug}: foundation: {proposed} section(s) PROPOSED for your decision in the Canon tab; nothing is canonical until you approve it")
            route_report(slug, claude, "foundation")
            for line in CANON_VERIFY.log_lines(vreport):
                print(line)
            if vreport["checked"]:
                print(f"canon: {slug}: claims checked from public sources by the pool model: {len(vreport['verified'])} of {vreport['checked']} became VERIFIED (one source close to the fact in any medium, exact quotation, nothing contradicting), "
                      f"{vreport['calls']} AI call(s)" + ("; verified: " + ", ".join(vreport["verified"]) if vreport["verified"] else "") + _outcome_tail(vreport))
            rejected = [r for r in rep if r["result"] == "rejected"]
            if rejected:
                print(f"canon: {slug}: {len(rejected)} section(s) REJECTED by the gate and recorded ({', '.join(r['section'] for r in rejected)}): the run continues and does not fail for it. "
                      "To unblock: run Skills > verify-claims, or decide the claims in the Claims tab, then run the section again.")
            if any("is not a section of the chain" in r["detail"] for r in rep):
                worst = max(worst, 2)
            else:
                broke = [r for r in rep if r["result"] in ("failed", "refused") and "limit" not in r["detail"] and (r["result"] != "refused" or "NEEDS_INPUT" not in r["detail"])]
                if broke and not proposed and not rejected:                  # nothing worked and nothing was merely blocked: the run itself broke (no AI client, a lock, the model down): that fails
                    worst = max(worst, 1)
                elif broke:
                    print(f"canon: {slug}: note: {len(broke)} section(s) could not be run ({'; '.join(r['section'] + ': ' + r['detail'][:80] for r in broke[:3])}); the others were saved")
        elif args.action == "stage":
            from . import foundation as FD
            from . import stages as ST
            if args.stage not in ST.STAGES:
                print(f"canon: stage needs --stage, one of: {', '.join(ST.STAGES)}")
                return 2
            try:
                cfg = SK.load_config(args.skills_config)
            except SK.SkillsError as exc:
                print(f"canon: {exc}")
                return 1
            trusted_root = Path(args.skills_dir) if args.skills_dir else SK.checkout_dir(cfg)
            trusted = SK.verify(cfg, trusted_root)
            if not trusted["ok"]:
                for problem in trusted["problems"]:
                    print(f"canon: PROBLEM: {problem}")
                print("canon: the stage does not run: the skills connection cannot be trusted")
                return 1
            claude, search, get = clients() if clients else (None, None, None)
            brand_text, story_text = FD.brand_material(args.brands_dir, slug)
            verifier, vreport = _claim_verifier(reg, slug, args, claude, search, get, now)
            try:
                from . import brands as BR
                name = getattr(BR.load_brand(slug, args.brands_dir), "name", slug) or slug
            except Exception:                                                              # noqa: BLE001
                name = slug
            rep = ST.run_stage(args.stage, reg, cfg, trusted_root, claude, slug=slug, name=name, brand_text=brand_text, story_text=story_text, store=store, now=now,
                               max_calls=args.max_calls or None, verifier=verifier, verify_connection=False)
            for line in ST.report_lines(slug, args.stage, rep):
                print(line)
            proposed = sum(1 for r in rep if r["result"] == "proposed")
            rejected = [r for r in rep if r["result"] == "rejected"]
            print(f"canon: {slug}: {ST.STAGES[args.stage]['title']}: {proposed} section(s) PROPOSED (saved under data/{slug}/extras and catalogue; nothing is canon, posted or sent)"
                  + (f"; {len(rejected)} REJECTED by the gate and recorded: the run does not fail for it (run verify-claims, or decide the claims in the Claims tab)" if rejected else ""))
            for line in CANON_VERIFY.log_lines(vreport):
                print(line)
            if vreport["checked"]:
                print(f"canon: {slug}: claims checked from public sources by the pool model: {len(vreport['verified'])} of {vreport['checked']} became VERIFIED")
            route_report(slug, claude, args.stage)
            broke = [r for r in rep if r["result"] in ("failed", "refused") and (r["result"] != "refused" or "NEEDS_INPUT" not in r["detail"])]
            if broke and not proposed and not rejected:
                worst = max(worst, 1)
        elif args.action == "verify-claims":
            claude, search, get = clients() if clients else (None, None, None)
            verifier, vreport = _claim_verifier(reg, slug, args, claude, search, get, now, announce=True)
            if verifier is None:
                worst = max(worst, 1)
                continue
            limit = claims_left if claims_left is not None else CANON_VERIFY.MAX_PER_RUN              # the cap is for the WHOLE run across brands, not per brand
            if limit <= 0:
                print(f"canon: {slug}: the claim cap of this run ({args.max_claims}) was used by the brands before it: nothing checked here")
                continue
            ids = CANON_VERIFY.targets(reg, limit)
            done = verifier(ids)
            if claims_left is not None:
                claims_left -= len(vreport["outcomes"])
            for note in vreport["notes"][:12]:
                print(f"canon:   note: {note}")
            print(f"canon: {slug}: {len(ids)} claim(s) checked from public sources by the pool model: {len(vreport['verified'])} became VERIFIED (one source close to the fact in any medium, exact quotation, nothing contradicting)"
                  f"{_outcome_tail(vreport)}; {vreport['calls']} AI call(s). The owner can still change any status in the Claims tab.")
            route_report(slug, claude, "claim verification")
        elif args.action == "reclassify-conflicts":
            from . import story_refresh as SR
            claude = clients()[0] if clients else None
            if claude is None:
                print(f"canon: {slug}: re-checking conflicts needs the AI client (ANTHROPIC_API_KEY): none is available in this run")
                worst = max(worst, 1)
                continue
            report = {"calls": 0, "errors": 0, "notes": []}
            claims = reg.state()["claims"]
            todo = [(cid, other) for cid, c in claims.items() if c["status"] == "conflicted" and not c.get("decided_at") and str(c.get("source", "")).startswith("http") and c.get("conflicts_with")
                    for other in c["conflicts_with"] if other in claims]
            relations = SR.classify_pairs(claude, slug, [(claims[o]["text"] + " " + (claims[o].get("reason") or ""), claims[c]["text"]) for c, o in todo], report=report, fallback="contradicts", max_calls=args.max_calls or None)
            real = {c for (c, _o), r in zip(todo, relations) if r == "contradicts"}                # no answer: it stays a conflict, the owner decides
            taken_back = kept = 0
            for cid in dict.fromkeys(c for c, _o in todo):
                if cid in real:
                    kept += 1
                else:
                    reg.reconsider_conflict(cid, now, "re-checked: the two statements are about different things or say the same thing; not a conflict")
                    taken_back += 1
            for note in report["notes"]:
                print(f"canon:   note: {note}")
            print(f"canon: {slug}: conflicts re-checked: {kept} kept as real conflicts, {taken_back} taken back to UNVERIFIED (the link and the old claim are untouched; nothing deleted); {report['calls']} AI call(s)")
            route_report(slug, claude, "conflict re-check")
            if report["errors"] and not (kept or taken_back):
                worst = max(worst, 1)
            # The OTHER kind of conflict: a claim that verify-claims marked CONFLICTED because a source contradicted it. It has no `conflicts_with` link to another claim (so the pair check above never sees it): it is
            # re-checked by running the public-source check on it again, which can now find the contradiction gone, a close source, or the conflict confirmed. Skills #45 re-checked nothing for want of this.
            by_evidence = [cid for cid, c in reg.state()["claims"].items() if c["status"] == "conflicted" and not c.get("owner_decided") and (c.get("evidence") or {}).get("contradicts")]
            if by_evidence:
                _c2, search2, get2 = clients() if clients else (None, None, None)
                verifier, vrep = _claim_verifier(reg, slug, args, claude, search2, get2, now, announce=True)
                if verifier is None:
                    worst = max(worst, 1)
                else:
                    verifier(by_evidence)
                    after = [reg.state()["claims"][c]["status"] for c in by_evidence]
                    outcomes = [vrep["outcomes"].get(c, "not checked") for c in by_evidence]
                    print(f"canon: {slug}: {len(by_evidence)} claim(s) conflicted by a source were re-checked from public sources: {after.count('verified')} now VERIFIED, {after.count('unverified')} now UNVERIFIED, {after.count('conflicted')} still CONFLICTED; "
                          f"what the new check found: {outcomes.count('verified')} verified, {outcomes.count('unverified')} no source and no contradiction, {outcomes.count('conflicted')} contradicted again; {vrep['calls']} AI call(s).")
                    route_report(slug, claude, "evidence-conflict re-check")
        elif args.action == "history":
            import os
            import subprocess
            target = Path(args.data_dir) / slug / "registry"
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
            root = (top.stdout or "").strip() or str(Path.cwd())                                 # the repository's own root, wherever the command is run from
            try:
                spec = os.path.relpath(target.resolve(), root)
            except ValueError:
                spec = ""
            if not spec or spec.startswith(".."):
                print(f"canon: {slug}: the data directory {args.data_dir} is outside this repository, so its git history cannot be read")
                worst = max(worst, 1)
                continue
            done = subprocess.run(["git", "-C", root, "log", "--format=%h %cs %s", "--", spec], capture_output=True, text=True)
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
                  + (f"; {len(reg.rejected)} event(s) ignored because they break the rules" if reg.rejected else "")
                  + (f"; foundation: {state['foundation'].get('foundation_needs_input', 0)} step(s) waiting for input, {state['foundation'].get('foundation_rejected', 0)} refused by the gate" if state.get("foundation") else ""))
    return worst if args.action in ("gate", "seed", "history", "refresh", "foundation", "reclassify-conflicts", "verify-claims", "stage") else 0
