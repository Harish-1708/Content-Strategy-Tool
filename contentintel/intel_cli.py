"""`intel`: Stage 4 (Creative Intelligence) and the Stage 5 checks that need no model. formulas / winners / opportunities / report read the Idea Bank of each brand and print what the code can show, with sample
size and confidence; report also saves data/<brand>/extras/creative_intel.json and the cross-brand view data/_shared/creative_intel.json. golden runs the golden set against the validator bank (exit 1 when it fails);
gate runs the same bank, the same sample brief and the same golden set for every brand with no brand-specific code (the Defi gate)."""
import argparse
from pathlib import Path

import yaml

from . import connections as CONN
from . import formulas as F
from . import storyboard as SB
from . import validators as V
from .brands import list_brand_slugs


def add_parsers(sub, common):
    p = sub.add_parser("intel", help="Stage 4 creative intelligence (formulas, winners, opportunities, cross-brand) and the Stage 5 golden set and brand gate; no model is called")
    common(p)
    p.add_argument("--action", choices=["formulas", "winners", "opportunities", "report", "golden", "gate", "refresh-library", "storyboard"], required=True)
    p.add_argument("--brand", default="all")
    p.add_argument("--top", type=int, default=10, help="how many winners / opportunities to print")
    p.add_argument("--brief-id", default="", help="storyboard: the brief record to use (BR-0001...); the latest when empty")
    p.add_argument("--concept", type=int, default=0, help="storyboard: which concept of the brief (1 = the first); all of them when 0")
    p.add_argument("--stills", action=argparse.BooleanOptionalAction, default=True, help="storyboard: find a styleframe (a licensed still) for every shot (ON by default)")


SAMPLE_BRIEF = {"title": "A short video for the overalls", "key_message": "Soft cotton overalls for every day",
                "concepts": [{"name": "Cost confession", "format": "30 second vertical video", "hook": "$18,000 is what one missing clause cost me.",
                              "script": "Show the contract on a desk. Say what the missing clause was. Show the new overalls on a hanger and say what changed.", "visual_direction": "Close-up of the contract, then a wide shot of the overalls on a hanger in daylight.",
                              "cta": "See the overalls at the link."}]}


def _slugs(args):
    return list_brand_slugs(args.brands_dir) if args.brand == "all" else [args.brand]


def _golden_extra(brands_dir, slug):
    path = Path(brands_dir) / slug / "golden.yaml"
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [c for c in (data.get("cases") or []) if isinstance(c, dict) and c.get("text") and c.get("expect") in ("pass", "flag")]


def _github_fetch(url):
    import os
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "contentintel", "Accept": "application/vnd.github+json", **({"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]} if os.environ.get("GITHUB_TOKEN") else {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _github_head(repo):
    """The newest commit of a repository: GitHub's API first, and `git ls-remote` when the API refuses (its anonymous limit is low and shared); a person's token lifts the limit."""
    import json
    import subprocess
    try:
        return json.loads(_github_fetch(f"https://api.github.com/repos/{repo}/commits/HEAD"))["sha"]
    except Exception:                                                                    # noqa: BLE001
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"], capture_output=True, text=True, timeout=60, check=True).stdout.split()
        if not out or len(out[0]) != 40:
            raise RuntimeError(f"no commit id for {repo}")
        return out[0]


def _default_finder(args, store):
    """The still picker (stills.py): the product's own pictures, the brand site's pictures that match the shot, then a picture generated for the shot. No stock library, no competitor pictures."""
    from . import research_tools as RT
    from . import stills as STL
    from .brands import load_brand
    brand = load_brand(args.brand, args.brands_dir)
    latest = store.load_latest(args.brand) or {}
    return STL.build_finder(brand, latest.get("products") or {}, aspect_of=lambda board: (board or {}).get("aspect", "9:16"), get=lambda url: RT.default_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=30))


def run_storyboard(args, store, now, finder=None):
    """Stage 7 layer 1 for one brand: a storyboard per concept of a brief, checked and given styleframes; saved as data/<brand>/extras/storyboard.json and storyboard.html."""
    from . import brief as BR
    from . import canon as canon_mod
    from . import rules as rules_mod
    if args.brand == "all":
        print("storyboard: name one brand (--brand)")
        return 1
    records = store.list_briefs(args.brand)
    record = next((r for r in records if r["id"] == args.brief_id), None) if args.brief_id else (records[-1] if records else None)
    if record is None:
        print(f"storyboard: {'there is no brief ' + args.brief_id if args.brief_id else 'no brief has been made for this brand yet'}: make one first (Brief workflow)")
        return 1
    content = record.get("content")
    if not content or not content.get("concepts"):
        print(f"storyboard: {record['id']} has no saved content (briefs made before Update 73 v4 kept none): make the brief again")
        return 1
    concepts = list(enumerate(content["concepts"], start=1))
    if args.concept:
        concepts = [c for c in concepts if c[0] == args.concept]
        if not concepts:
            print(f"storyboard: {record['id']} has {len(content['concepts'])} concept(s), not {args.concept}")
            return 1
    rules_path = Path(getattr(args, "briefs_dir", "briefs")) / str(record.get("brief", "")) / "rules.yaml"
    ruleset = rules_mod.load_ruleset(rules_path) if rules_path.is_file() else None
    reg = canon_mod.Registry(store.data_dir, args.brand)
    gate = (lambda b: BR.gate_violations(b, reg)) if (reg.state()["canon"] or reg.state()["claims"]) else None
    lib = F.load_library(CONN.external_dir())
    finder = finder or (_default_finder(args, store) if args.stills else None)
    boards = []
    for no, concept in concepts:
        board = SB.build(concept, brief_id=record["id"], concept_no=no)
        findings = SB.check(board, lib, rules=ruleset, claim_gate=gate)
        got = SB.add_styleframes(board, finder) if finder else 0
        board["findings"], board["styleframes"] = findings, got
        errors = [f for f in findings if f["severity"] == "error"]
        board["status"] = "blocked" if errors else "ok"                                   # one blocked concept is flagged and left out of the ready ones; it never stops the others from being storyboarded and saved
        print(f"{args.brand}: {record['id']} concept {no} ({board['concept']}): {len(board['shots'])} shot(s), {board['total_seconds']} s of {board['target_seconds']} s, {got} styleframe(s)" + (" (stills off)" if not finder else "") + f", {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
        for f in findings[:8]:
            print(f"  {f['severity']}: {f['where']}: {f['message']}")
        boards.append(board)
    blocked = [b for b in boards if b["status"] == "blocked"]
    worst = 1 if boards and len(blocked) == len(boards) else 0                          # the run fails only when NOTHING could be storyboarded cleanly
    print(f"{args.brand}: {len(boards) - len(blocked)} of {len(boards)} concept(s) storyboarded cleanly" + (f"; BLOCKED and flagged, not ready: concept(s) {', '.join(str(b['concept_no']) for b in blocked)} (fix the brief, or revise it, then run this again for them: --concept N)" if blocked else ""))
    store.write_extra(args.brand, "storyboard", {"generated_at": now, "brief": record["id"], "rules_version": record.get("rules_version"), "library": lib.source,
                                                 "ready": [b["concept_no"] for b in boards if b["status"] == "ok"], "blocked": [b["concept_no"] for b in blocked], "boards": boards})
    html_path = store._brand_file(args.brand, "extras", "storyboard.html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text("\n<hr>\n".join(SB.render_html(b, b["findings"]) for b in boards), encoding="utf-8")
    print(f"{args.brand}: saved data/{args.brand}/extras/storyboard.json and storyboard.html")
    return worst


def run_intel(args, store, now, fetch=None, head=None):
    if args.action == "storyboard":
        return run_storyboard(args, store, now)
    if args.action == "refresh-library":
        res = F.refresh(fetch or _github_fetch, now, golden=lambda lib: V.run_golden(lib), head=head or (None if fetch else _github_head))
        for line in res["lines"]:
            print(line)
        print(f"library refresh: {res['status']}; checked {now[:10]}")
        return 0 if res["status"] in ("up to date", "updated") else 1
    lib = F.load_library(CONN.external_dir())
    if args.action == "golden":
        res = V.run_golden(lib)
        print(f"golden set: {res['checked']} case(s) against the validator bank; library: {lib.source}")
        for line in res["failures"]:
            print(f"  FAILED: {line}")
        for line in res["library_mismatches"][:10]:
            print(f"  library mismatch (the example and the pattern come from the library, not from us): {line}")
        if len(res["library_mismatches"]) > 10:
            print(f"  ... and {len(res['library_mismatches']) - 10} more library mismatch(es)")
        print("golden set: " + ("PASSED" if not res["failures"] else f"FAILED ({len(res['failures'])})"))
        return 0 if not res["failures"] else 1
    if args.action == "gate":
        bad = 0
        for slug in _slugs(args):
            res = V.run_golden(lib, _golden_extra(args.brands_dir, slug))
            found = V.brief_bank(SAMPLE_BRIEF, lib)
            errors = [f for f in found if f["severity"] == "error"]
            ok = not res["failures"] and not errors
            bad += 0 if ok else 1
            print(f"{slug}: {'PASSED' if ok else 'FAILED'}: the same validator bank, sample brief and golden set as every other brand ({res['checked']} case(s), {len(errors)} error(s) on the sample brief)"
                  + "".join(f"\n  {x}" for x in res["failures"] + [e['message'] for e in errors]))
        print("brand gate: " + ("every brand passes with no brand-specific code" if not bad else f"{bad} brand(s) FAILED"))
        return 0 if not bad else 1
    reports = []
    for slug in _slugs(args):
        bank = store.read_ideabank(slug) or {"items": {}}
        rep = F.build_report(slug, bank, lib, now, top=max(1, args.top))
        reports.append(rep)
        for line in F.render(rep):
            print(line)
        if args.action == "report":
            store.write_extra(slug, "creative_intel", rep)
            print(f"{slug}: saved data/{slug}/extras/creative_intel.json")
    if args.action == "report":
        cross = F.cross_brand(reports)
        store.write_shared("creative_intel", {"generated_at": now, "weights": F.WEIGHTS, "cross_brand": cross, "brands": {r["brand"]: r["sample"] for r in reports}})
        print("cross-brand: " + (f"winning in several brands: {', '.join(cross['winning_in_several_brands']) or 'none'}; could transfer: " + (", ".join(f"{t['formula']} (wins in {'/'.join(t['wins_in'])}, unused in {'/'.join(t['unused_in'])})" for t in cross["could_transfer"]) or "none") if not cross["note"] else cross["note"]))
        print("saved data/_shared/creative_intel.json")
    return 0
