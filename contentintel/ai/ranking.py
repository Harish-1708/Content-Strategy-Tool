"""The pool retest: each job's REAL prompts are run against every provider and model that could serve it, each answer is scored by a check the code can make, and the job's ranked list (its lane's chain) is
reordered from the results: best first, backups in order, a provider that cannot do the job left out of that list. Nothing is guessed: a list with no measurement keeps its seeded order and says so.

The probes use the project's own prompts (the refresh's extraction, review and conflict check, and the foundation chain's system prompt) with small synthetic inputs whose right answers are known, so a score is
something checkable: a quotation that really is in the page, a verdict that is right, a label that matches, a text that avoids a blocked claim. Public data only: nothing about a client is sent.
"""
import dataclasses
import json
import statistics
from pathlib import Path

PASS_AT = 0.8                                                                    # a probe run passes at this score or better
JOBS = ("foundation_json", "story_extract_json", "story_review_json", "conflict_check_json", "claim_verify_json", "brief_json", "competitor_ideas_json", "brief_review_json", "seed_judge_json", "creator_chain_json", "listen_learn_json", "voice_creative_json", "seo_lens_json", "storyboard_json")
RANKINGS_FILE = "ai_rankings.json"                                               # under data/_shared

PAGE = ("Probe Brand makes soft overalls for adults. The company was founded in 2017 in a small studio in Portland. Its overalls are cut from heavy organic cotton and sold directly online. "
        "Unrelated note: the weather in Portland is often rainy in November, and the local bakery opens at seven.")
FACT_PHRASES = ("founded in 2017", "organic cotton")
ITEMS = (("Probe Brand was founded in 2017.", "The company was founded in 2017 in a small studio in Portland.", True),
         ("Probe Brand sells overalls made of organic cotton.", "Its overalls are cut from heavy organic cotton and sold directly online.", True),
         ("Probe Brand won a national design award.", "The company was founded in 2017 in a small studio in Portland.", False))
PAIRS = (("Probe Brand was founded in 2017.", "The company started in 2017.", "same"),
         ("Probe Brand was founded in 2017.", "Probe Brand was founded in 2019.", "contradicts"),
         ("Probe Brand offers free returns for 30 days.", "Probe Brand has a 4.8 rating from 900 reviews.", "unrelated"),
         ("Probe Brand's overalls cost $90.", "A pair of Probe Brand overalls costs $140.", "contradicts"),
         ("Probe Brand ships from Portland.", "Orders leave from a warehouse in Portland.", "same"),
         ("Probe Brand sells hats.", "Probe Brand was founded in Portland.", "unrelated"))


def _norm(text):
    return " ".join(str(text or "").lower().split())


def _extract_score(out):
    facts = out.get("facts") if isinstance(out, dict) else None
    if not isinstance(facts, list) or not facts:
        return 0.0
    page = _norm(PAGE)
    ok = [f for f in facts if isinstance(f, dict) and len(_norm(f.get("quote"))) >= 20 and _norm(f.get("quote")) in page and "probe brand" in _norm(str(f.get("fact")) + " " + str(f.get("quote")))]
    verbatim = len(ok) / len(facts)                                              # every fact must rest on a sentence that is really in the page
    found = sum(any(p in _norm(f.get("quote")) for f in ok) for p in FACT_PHRASES) / len(FACT_PHRASES)
    return round(0.5 * verbatim + 0.5 * found, 3)


def _review_score(out):
    verdicts = out.get("verdicts") if isinstance(out, dict) else None
    if not isinstance(verdicts, list):
        return 0.0
    by = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
    right = 0
    for i, (_fact, _quote, supported) in enumerate(ITEMS):
        v = by.get(i)
        right += bool(v and (v.get("supported") is True and v.get("about_brand") is True) == supported)
    return round(right / len(ITEMS), 3)


def _conflict_score(out):
    relations = out.get("relations") if isinstance(out, dict) else None
    if not isinstance(relations, list):
        return 0.0
    by = {r.get("i"): r.get("relation") for r in relations if isinstance(r, dict)}
    return round(sum(by.get(i) == label for i, (_a, _b, label) in enumerate(PAIRS)) / len(PAIRS), 3)


def _foundation_score(out):
    text = str(out.get("text") or "") if isinstance(out, dict) else ""
    needs = out.get("needs_input") if isinstance(out, dict) else None
    return round(0.25 * (len(text) >= 300) + 0.25 * isinstance(needs, list) + 0.25 * (bool(text.strip()) and "award" not in text.lower() and "press" not in text.lower()) + 0.25 * ("probe brand" in text.lower()), 3)


def probes():
    """{lane: [(system, user, score_fn)]}: the real prompts of each job with inputs whose right answers are known."""
    from .. import foundation as FD
    from .. import story_refresh as SR
    items = "\n".join(f"{i}. FACT: {f}\n   QUOTATION: {q}" for i, (f, q, _s) in enumerate(ITEMS))
    pairs = "\n".join(f"PAIR {i}:\n  A (already in the ledger): {a}\n  B (new): {b}" for i, (a, b, _l) in enumerate(PAIRS))
    foundation_user = ("TASK: write the 'positioning' section for the brand below.\n\n=== BRAND FACTS ===\nBrand name: Probe Brand. Soft overalls for adults, cut from organic cotton, sold online, founded in 2017 in Portland.\n\n"
                       "=== CLAIMS LEDGER ===\nDO NOT STATE (the ledger blocks or disputes these):\n- Won a national design award (blocked)\n- Featured in a national magazine (blocked)\n\nMAY STATE (verified by the owner):\n- (none yet)")
    from .. import stages as ST
    from .. import verify_claims as VC
    from .. import brief as BF
    page = "Probe Brand won the 2026 Probe Innovation Award, the awarding body announced on 3 March 2026. The brand also makes overalls."
    verify_user = f"Brand: Probe Brand\nClaim: Probe Brand won the 2026 Probe Innovation Award.\n\nPAGE TEXT:\n{page}"

    def verify_score(out):
        ok = isinstance(out, dict) and out.get("verdict") == "supports" and out.get("about_brand") is True and "2026 Probe Innovation Award" in str(out.get("quote", ""))
        return 1.0 if ok else (0.4 if isinstance(out, dict) and out.get("verdict") in ("supports", "contradicts", "silent") else 0.0)

    def query_score(out):
        qs = out.get("queries") if isinstance(out, dict) else None
        return 1.0 if isinstance(qs, list) and 1 <= len(qs) <= 3 and all(isinstance(q, str) and "http" not in q.lower() and q.strip() for q in qs) else 0.0

    def brief_score(out):
        concepts = out.get("concepts") if isinstance(out, dict) else None
        return round(0.5 * bool(isinstance(out, dict) and str(out.get("title", "")).strip()) + 0.5 * bool(isinstance(concepts, list) and concepts), 3)
    from .. import validators as VAL

    def review_score(out):
        if not isinstance(out, dict) or out.get("verdict") not in ("pass", "revise"):
            return 0.0
        problems = " ".join(str(p.get("issue", "")) for p in (out.get("problems") or []) if isinstance(p, dict)).lower()
        return round(0.5 * (out["verdict"] == "revise") + 0.5 * any(w in problems for w in ("claim", "best", "guarantee", "unverif", "proof")), 3)
    review_user = ("Title: Probe Brand overalls\nKey message: the best overalls in the world\nConcept 1 (30 second vertical video) Proof\n  Hook: Voted the best overalls in the world, 100% guaranteed.\n"
                   "  Script: Show the overalls. Say they are the best in the world and that every customer is guaranteed to love them.\n  Visual direction: Close-up of the overalls on a hanger.\n  Call to action: Buy now.\nValidator findings (exact checks already run): none")
    from .. import seed_judge as SJ

    def seed_score(out):
        verdicts = SJ.parse(out, 3)
        return round(sum(1 for i, want in ((0, True), (1, False), (2, False)) if verdicts.get(i) is want) / 3, 3)
    seed_user = ("Brand: Probe Brand. Its own terms (what it is called, sells and researches): probe brand; soft organic cotton overalls; loungewear for adults\n\nTopics:\n"
                 "0. Why adults are choosing soft loungewear over jeans at home\n1. Central bank raises interest rates again\n2. Quarterback signs a new contract")
    extra = {"seed_judge_json": [(SJ.SYSTEM, seed_user, seed_score)], "brief_review_json": [(VAL.REVIEW_SYSTEM, review_user, review_score)], "claim_verify_json": [(VC.QUERY_SYSTEM, "Brand: Probe Brand\nClaim: Probe Brand won the 2026 Probe Innovation Award.", query_score), (VC.CHECK_SYSTEM, verify_user, verify_score)],
             "brief_json": [(BF.BRIEF_SYSTEM, "Ask: a short video for Probe Brand's organic cotton overalls.\nFacts you may state: Probe Brand overalls are cut from organic cotton and sold online.\nWrite 1 concept.", brief_score)]}
    extra.update(ST.probes())
    return {**extra, "story_extract_json": [(SR.EXTRACT_SYSTEM, f"Brand: Probe Brand\nPage address: https://probe.example/about\nPage text:\n{PAGE}", _extract_score)],
            "story_review_json": [(SR.REVIEW_SYSTEM, f"Brand: Probe Brand\nSource: https://probe.example/about\n{items}", _review_score)],
            "conflict_check_json": [(SR.RELATION_SYSTEM, f"Brand: Probe Brand\n{pairs}", _conflict_score)],
            "foundation_json": [(FD.SYSTEM, foundation_user, _foundation_score)]}


def run_ranking(router, lanes, jobs, *, now, runs=2, only_providers=None, progress=None, min_headroom=0.15):
    """Measure every eligible route of each job. Returns {lane: {"ranking": [...], "excluded": [...], "routes_tried": n}}. Each probe is run `runs` times per route; every call goes through the router's own
    single-route call, so the pacing, the spending ledger and the provider rests apply exactly as they do in a real run."""
    suite, report = probes(), {}
    for name in jobs:
        lane = lanes[name]
        rows = []
        for route in router._routes(lane):
            if only_providers and route["provider"] not in only_providers:
                continue
            scores, latencies, failures, skipped = [], [], [], ""
            prov = getattr(getattr(router, "registry", None), "providers", {}).get(route["provider"])
            if prov is not None and route["provider"] != "claude" and 1.0 - router.usage.load(prov, now) < min_headroom:
                rows.append({"provider": route["provider"], "model": route["model"], "status": "not measured", "why": f"today's free allowance of {route['provider']} is {int(router.usage.load(prov, now) * 100)}% used: not measured, so the retest does not burn what the other jobs need (run it again when the allowance has reset)"})
                if progress:
                    progress(f"rank: {name}: {route['provider']}/{route['model']}: not measured: allowance nearly used")
                continue
            for system, user, score_fn in suite[name]:
                for _ in range(max(1, runs)):
                    res = router.run_route(name, route, instructions=user, system=system, now=now, ignore_rests=True)
                    if res["status"] == "skipped":
                        skipped = str(res.get("reason", "skipped"))
                        break
                    if res["status"] != "ok":
                        scores.append(0.0)
                        failures.append(str(res.get("reason", "failed"))[:160])
                        continue
                    scores.append(score_fn(res["output"]))
                    latencies.append(getattr(res.get("completion"), "latency_ms", 0) or 0)
                if skipped:
                    break
            if skipped:
                rows.append({"provider": route["provider"], "model": route["model"], "status": "not measured", "why": skipped})
            else:
                if progress:
                    progress(f"rank: {name}: {route['provider']}/{route['model']}: {sum(s >= PASS_AT for s in scores)}/{len(scores)} passed" + (f" ({failures[0][:90]})" if failures else ""))
                rows.append({"provider": route["provider"], "model": route["model"], "status": "measured", "runs": len(scores), "passes": sum(s >= PASS_AT for s in scores),
                             "mean_score": round(statistics.mean(scores), 3), "median_ms": int(statistics.median(latencies)) if latencies else 0, "failures": failures[:3]})
        measured = [r for r in rows if r["status"] == "measured"]
        ranking = sorted((r for r in measured if r["passes"] > 0), key=lambda r: (-r["passes"] / r["runs"], -r["mean_score"], r["median_ms"]))
        report[name] = {"ranking": ranking, "excluded": [r for r in measured if r["passes"] == 0], "not_measured": [r for r in rows if r["status"] != "measured"], "routes_tried": len(measured)}
    return report


def save(path, report, now):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {"measured_at": now, "pass_at": PASS_AT, "jobs": report}
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def apply_overlay(lanes, path, now=None, stale_days=3):
    """The lanes with each MEASURED job's chain reordered from the retest: the measured routes best first, then the routes that were not measured in their seeded order. A route that failed every probe is dropped
    from that job's chain (it stays available to other jobs and to the shared pool). A job with no measurement is untouched. Returns (lanes, [one line per job that was reordered])."""
    p = Path(path)
    if not p.is_file():
        return lanes, []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return lanes, [f"{p.name} could not be read: the seeded orders are used"]
    out, notes = dict(lanes), []
    from datetime import datetime, timezone
    nowiso = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, job in (data.get("jobs") or {}).items():
        lane = lanes.get(name)
        ranked = job.get("ranking") or []
        if lane is None or not ranked:
            continue
        if job.get("generated_by") == "health":                                    # a list the daily health run wrote is used only while that run is fresh; older, the static lists (and the last retest) stand
            try:
                age = (datetime.fromisoformat(nowiso.replace("Z", "+00:00")) - datetime.fromisoformat(str(job.get("generated_at", "")).replace("Z", "+00:00"))).total_seconds() / 86400
            except (ValueError, TypeError):
                age = 10 ** 6
            if age > stale_days:
                notes.append(f"{name}: the health run's list is {int(min(age, 9999))} day(s) old (more than {stale_days}): the static list is used")
                continue
        keep = {(r["provider"], r["model"]) for r in ranked}
        dropped = {(r["provider"], r["model"]) for r in job.get("excluded") or []}
        chain = [{"provider": r["provider"], "model": r["model"]} for r in ranked]
        chain += [dict(r) for r in lane.chain if (r["provider"], r["model"]) not in keep and (r["provider"], r["model"]) not in dropped]
        out[name] = dataclasses.replace(lane, chain=tuple(chain))
        notes.append(f"{name}: measured order {data.get('measured_at', '')[:10]}: " + " > ".join(f"{r['provider']}" for r in ranked[:4]) + (f"; left out: {', '.join(sorted(p for p, _m in dropped))}" if dropped else ""))
    return out, notes


def format_report(report):
    lines = []
    for name, job in report.items():
        lines.append(f"{name}: {job['routes_tried']} route(s) measured")
        for i, r in enumerate(job["ranking"], 1):
            lines.append(f"  {i}. {r['provider']}/{r['model']}: {r['passes']}/{r['runs']} probe runs passed, mean score {r['mean_score']}, median {r['median_ms']} ms")
        for r in job["excluded"]:
            lines.append(f"  left out {r['provider']}/{r['model']}: 0/{r['runs']} passed" + (f" ({r['failures'][0]})" if r["failures"] else ""))
        for r in job["not_measured"]:
            lines.append(f"  not measured {r['provider']}/{r['model']}: {r['why']}")
    return "\n".join(lines)
