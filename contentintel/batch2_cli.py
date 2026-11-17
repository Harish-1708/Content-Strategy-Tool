"""The `skills` and `canon` commands (Batch 2): the external skills repository and the two registries. Nothing here posts, sends or changes anything on any platform."""
import json
from pathlib import Path

from . import canon as CANON
from . import skills as SK
from .brands import list_brand_slugs


def add_parsers(sub, common):
    p = sub.add_parser("skills", help="the Aaron marketing skills, connected externally: status, verify, update-report, catalogue")
    common(p)
    p.add_argument("--action", choices=["status", "verify", "update-report", "catalogue"], default="status")
    p.add_argument("--skills-config", default=SK.CONFIG_FILE, help="the skills policy file (default external_skills.yaml)")
    p.add_argument("--skills-dir", default="", help="the checkout of the skills repository (default: AARON_SKILLS_DIR, then external/aaron-marketing-skills)")

    p = sub.add_parser("canon", help="the narrative canon and the offer-claims ledger: seed, status, gate, export, decisions")
    common(p)
    p.add_argument("--brand", default="all")
    p.add_argument("--action", choices=["seed", "status", "gate", "export"], default="status")
    p.add_argument("--text", default="", help="gate: the text to check against the claims ledger")


def run_skills(args, store, now, *, environ=None, runner=None):
    import subprocess
    runner = runner or subprocess.run
    cfg = SK.load_config(args.skills_config)
    root = Path(args.skills_dir) if args.skills_dir else SK.checkout_dir(cfg, environ)
    if args.action == "update-report":
        report = SK.update_report(cfg, root, runner=runner)
        print(f"skills: pinned {report['pinned'][:10]}, upstream {report['latest'][:10]}: " + ("up to date" if report["up_to_date"] else "CHANGED upstream (nothing is adopted until the pinned commit in external_skills.yaml is changed by hand)"))
        for label, key in (("changed", "skills_changed"), ("added", "skills_added"), ("removed", "skills_removed")):
            for name in report[key]:
                print(f"skills:   {label}: {name}")
        for line in report["runtime_changed"]:
            print(f"skills:   runtime file {line}")
        return 0
    result = SK.verify(cfg, root, runner=runner)
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
        target = Path(store.data_dir) / "_shared" / "skills_catalogue.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"at": now, "commit": result["commit"], "ok": result["ok"], "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"skills: catalogue written to {target} (each skill's hash: git keeps its history)")
    return 0 if result["ok"] else 1


def run_canon(args, store, now):
    slugs = list_brand_slugs(args.brands_dir) if args.brand in ("", "all") else [args.brand]
    worst = 0
    for slug in slugs:
        reg = CANON.Registry(store.data_dir, slug)
        if args.action == "seed":
            added, there = CANON.apply_seed(reg, CANON.load_seed(Path(args.brands_dir) / slug / "claims_seed.yaml"), now)
            print(f"canon: {slug}: claims seeded from the story-bank gaps: {added} added, {there} already in the ledger (never as verified)")
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
                  f"{len(reg.canonical())} section(s) approved by the owner" + (f"; {reg.damaged} damaged event line(s) skipped" if reg.damaged else ""))
    return worst if args.action == "gate" else 0
