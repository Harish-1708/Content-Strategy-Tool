"""The evaluation harness: run representative tasks through approved routes and compare models on measured results.

A task names a lane, an instruction and the (untrusted) text to work on, and what a good answer contains. Each route is scored on:
did the answer pass the lane's own output checks, did it contain the required facts, did it avoid forbidden ones, did it give the
expected labels. Latency, tokens and cost come from the real call. Results are appended to data/_shared/ai_eval.jsonl, marked
mock or live, so a routing decision can cite a measurement and a mock run can never be mistaken for a live one.
"""
import json
from pathlib import Path
from statistics import median

import yaml

from . import validate


class EvalError(Exception):
    pass


def load_tasks(path="ai_eval_tasks.yaml", lanes=None):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if raw.get("version") != 1 or not isinstance(raw.get("tasks"), list) or not raw["tasks"]:
        raise EvalError(f"{path}: needs version: 1 and a tasks list")
    tasks, seen = [], set()
    for t in raw["tasks"]:
        if set(t) - {"id", "lane", "instructions", "context", "expect"} or not t.get("id") or not t.get("lane") or not t.get("instructions"):
            raise EvalError(f"{path}: a task needs id, lane and instructions (and may have context and expect)")
        if t["id"] in seen:
            raise EvalError(f"{path}: task {t['id']} is listed twice")
        seen.add(t["id"])
        if lanes is not None and t["lane"] not in lanes:
            raise EvalError(f"{path}: task {t['id']} names an unknown lane {t['lane']!r}")
        ctx = [(c["label"], c["text"]) for c in t.get("context") or []]
        tasks.append({"id": t["id"], "lane": t["lane"], "instructions": t["instructions"], "context": ctx, "expect": t.get("expect") or {}})
    return tasks


def score(task, lane, text):
    """{ok, score, facts, forbidden, labels, problem}: 0 when the answer fails the lane's checks, else the share of expectations met."""
    try:
        parsed = validate.check_output(text, lane)
    except validate.OutputProblem as problem:
        return {"ok": False, "score": 0.0, "facts": None, "forbidden": None, "labels": None, "problem": str(problem)}
    low, exp = text.lower(), task["expect"]
    parts, facts, forbidden, labels = [], None, None, None
    need = [x.lower() for x in exp.get("must_contain", [])]
    if need:
        facts = sum(1 for x in need if x in low)
        parts.append(facts / len(need))
    banned = [x.lower() for x in exp.get("must_not_contain", [])]
    if banned:
        forbidden = sum(1 for x in banned if x in low)
        parts.append(0.0 if forbidden else 1.0)
    want = exp.get("labels", {})
    if want:
        labels = sum(1 for k, v in want.items() if isinstance(parsed, dict) and str(parsed.get(k)).lower() == str(v).lower())
        parts.append(labels / len(want))
    return {"ok": True, "score": round(sum(parts) / len(parts), 3) if parts else 1.0, "facts": facts, "forbidden": forbidden, "labels": labels, "problem": ""}


def run_eval(router, tasks, *, now, mode, store=None, include_claude=False, only_providers=None):
    """One row per task and route. Routes that cannot be used (disabled, no key, over budget) are recorded as skipped, with the reason."""
    rows = []
    for task in tasks:
        lane = router.lanes[task["lane"]]
        for route in lane.chain:
            if route["provider"] == "claude" and not include_claude:
                continue
            if only_providers and route["provider"] not in only_providers:
                continue
            res = router.run_route(task["lane"], route, instructions=task["instructions"], context=task["context"], now=now)
            row = {"at": now, "mode": mode, "task": task["id"], "lane": task["lane"], "provider": route["provider"], "model": route["model"], "status": res["status"]}
            if res["status"] == "ok":
                comp = res["completion"]
                s = score(task, lane, comp.text)
                price = router.registry.provider(route["provider"]).price_for(route["model"]) if router.registry.provider(route["provider"]).paid else {}
                cost = round((comp.tokens_in * price.get("input", 0) + comp.tokens_out * price.get("output", 0)) / 1e6, 6)
                row.update({"score": s["score"], "latency_ms": comp.latency_ms, "tokens_in": comp.tokens_in, "tokens_out": comp.tokens_out, "cost_usd": cost, "problem": s["problem"]})
            else:
                row.update({"score": 0.0, "reason": res.get("reason", "")})
            rows.append(row)
    if store is not None:
        path = Path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return rows


def compare(rows):
    """Per lane, provider and model: runs, mean score, how often it passed the lane's checks, median latency, tokens, cost. Skipped rows are not averaged."""
    groups = {}
    for r in rows:
        if r["status"] == "skipped":
            continue
        groups.setdefault((r["lane"], r["provider"], r["model"], r["mode"]), []).append(r)
    out = []
    for (lane, provider, model, mode), rs in sorted(groups.items()):
        done = [r for r in rs if r["status"] == "ok"]
        out.append({"lane": lane, "provider": provider, "model": model, "mode": mode, "runs": len(rs), "answered": len(done),
                    "mean_score": round(sum(r["score"] for r in rs) / len(rs), 3), "usable_rate": round(sum(1 for r in done if not r.get("problem")) / len(rs), 3),
                    "median_latency_ms": int(median(r["latency_ms"] for r in done)) if done else None,
                    "tokens": sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in done), "cost_usd": round(sum(r.get("cost_usd", 0) for r in done), 6)})
    return out


def format_compare(table):
    if not table:
        return "No route could be evaluated (every route was skipped: nothing is enabled yet)."
    lines = ["lane / provider / model (mode): mean score, usable, median latency, tokens, cost"]
    for r in table:
        lines.append(f"  {r['lane']} / {r['provider']} / {r['model']} ({r['mode'].upper()}): score {r['mean_score']}, usable {r['usable_rate']}, "
                     f"{r['median_latency_ms']} ms, {r['tokens']} tokens, ${r['cost_usd']}")
    return "\n".join(lines)
