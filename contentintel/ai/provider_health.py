"""The daily provider-model health run: keeps the ranked lists of the free pool alive without anyone editing code when a provider retires a model.

WHAT IT DOES, in order: (1) DISCOVER the live free models of every provider that publishes a list (AnyAPI, OpenRouter's :free ids, OVH's keyless models); (2) PROBE every enabled provider's candidate models with ONE tiny call
(a few tokens) and record whether it answered, how fast, how many tokens, and why not (404 dead, 402 out of credits, 429 rate-limited, a timeout); (3) SCORE every probe-passed model per job with the seven weights the router
already uses (quality from the last measured order, headroom and reliability from the usage ledger and the probe, speed, cost, capability, terms); (4) apply the GUARDS: free providers only, never a paid model; the cost guard
(one call of the job must not be more than a small share of the provider's daily allowance); the capability guard (the model must fit the job); (5) let CLAUDE judge ONLY the lanes the rules cannot decide, in ONE small call
at most once a day, and only among candidates that already passed the guards; (6) WRITE the new lists, within the swap cap, to the generated files and nothing else.

SAFETY: only a model that PASSED the probe is ever added; a dead model drops out automatically; a PAID model is never added; the LAST working model of a job is never removed (the job keeps its list and is flagged);
at most `max_swaps` different routes change in one run; the owner's switch `auto_update` (ai_health.yaml) off turns writing off: the run then only reports; the router falls back to the static lists and the last pool
retest whenever the generated lists are missing or older than `stale_days`; the router's own runtime failover stays as the mid-day safety net."""
import dataclasses
import json
import re
from datetime import datetime
from pathlib import Path

import yaml

from . import connect as cn
from . import ranking as rk
from .. import openmontage as OM

CONFIG = "ai_health.yaml"
HEALTH_FILE = "provider_health.json"
DEFAULTS = {"auto_update": True, "max_swaps": 6, "cost_guard_share": 0.10, "stale_days": 3, "probe_prompt": "Reply with the single word OK.", "max_discovered_per_provider": 6,
            "tie_margin": 0.01, "judge": True, "judge_max_lanes": 6}
PERMANENT = ("dead", "credits")                              # a model that is gone or whose free credit is out stays out until a later probe passes
SKIP_ASSUMED_TOKENS_IN = 1500                                 # a typical prompt of a job, for the cost guard


def load_config(path=CONFIG):
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **raw}
    cfg["auto_update"] = cfg["auto_update"] is not False
    cfg["max_swaps"] = max(0, min(20, int(cfg["max_swaps"])))
    cfg["cost_guard_share"] = min(1.0, max(0.001, float(cfg["cost_guard_share"])))
    cfg["stale_days"] = max(1, int(cfg["stale_days"]))
    return cfg


def classify(reason):
    """dead (not found, removed), credits (402), rate_limited (429), timeout, auth, or error."""
    low = str(reason).lower()
    if "not found" in low or "404" in low or "removed" in low or "no longer" in low or "does not exist" in low:
        return "dead"
    if "402" in low or "balance" in low or "credit" in low or "payment" in low or "subscription" in low:
        return "credits"
    if "429" in low or "rate limit" in low or "rate-limit" in low or "too many" in low or "rate_limited" in low:
        return "rate_limited"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "401" in low or "403" in low or "key" in low and "reject" in low:
        return "auth"
    return "error"


def _age_days(stamp, now):
    try:
        a = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400
    except (ValueError, TypeError):
        return 10 ** 6


def fresh(stamp, now, stale_days):
    return _age_days(stamp, now) <= stale_days


# ---- discovery --------------------------------------------------------------------------------------------------------------------------------------
def discover(registry, clients, cfg):
    """{provider: [live model ids not yet in the registry]} for every enabled provider that publishes a list and is flagged for it (direct.discover_free: only ids that end in :free; direct.discover_all: every id of a
    keyless free endpoint). A provider whose list cannot be read is left out (its pinned models are still probed)."""
    out = {}
    for pid, p in registry.providers.items():
        d = getattr(p, "direct", None) or {}
        if not p.enabled or p.paid or not (d.get("discover_free") or d.get("discover_all")):
            continue
        c = clients.get(pid)
        if c is None or not hasattr(c, "http") or not hasattr(c, "base_url"):
            continue
        try:
            status, answer, _h = c.http("GET", c.base_url + "/models", headers=c._headers(), body=None, timeout=30)
        except Exception:                                                                           # noqa: BLE001
            continue
        rows = answer.get("data") if status == 200 and isinstance(answer, dict) else []
        ids = sorted({str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")})
        if d.get("discover_free"):
            ids = [i for i in ids if i.endswith(":free")]
        known = {m.id for m in p.models}
        fresh_ids = [i for i in ids if i not in known][: cfg["max_discovered_per_provider"]]
        if fresh_ids:
            out[pid] = fresh_ids
    return out


def list_live_models(registry, clients):
    """{provider: {kind, ids | why}} for EVERY enabled provider: the live model list of each one that publishes one (a direct provider's own /models; the local gateways' /v1/models, read once per gateway), and for the rest
    why there is none (their pinned models are probed directly). The lists of providers NOT flagged for discovery are used to tell which pinned models are no longer listed; they never add a model (free status is unknown)."""
    out, cache = {}, {}
    for pid, p in registry.providers.items():
        if pid == "claude" or not p.enabled:
            continue
        c = clients.get(pid)
        if c is None:
            out[pid] = {"kind": "none", "why": "no client in this run (no key): not probed"}
            continue
        if not hasattr(c, "http") or not hasattr(c, "base_url"):
            out[pid] = {"kind": "none", "why": "this provider publishes no model list we can read: its pinned models are probed directly"}
            continue
        direct = hasattr(c, "_headers")
        key = id(c)
        if key not in cache:
            try:
                if direct:
                    status, answer, _h = c.http("GET", c.base_url + "/models", headers=c._headers(), body=None, timeout=30)
                else:
                    status, answer, _h = c.http("GET", c.base_url + "/v1/models", headers={"Authorization": "Bearer " + str(getattr(c, "api_key", ""))}, body=None, timeout=30)
                rows = answer.get("data") if status == 200 and isinstance(answer, dict) else None
                cache[key] = sorted({str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}) if rows is not None else None
            except Exception:                                                                   # noqa: BLE001
                cache[key] = None
        ids = cache[key]
        out[pid] = {"kind": "direct" if direct else "gateway", "ids": ids} if ids is not None else {"kind": "none", "why": "its model list could not be read: its pinned models are probed directly"}
    return out


def with_discovered(registry, discovered):
    """The registry with the discovered free models added as approved TEXT models (nothing else is assumed about them). Paid providers never get any."""
    providers = dict(registry.providers)
    for pid, ids in (discovered or {}).items():
        p = providers.get(pid)
        if p is None or p.paid or not p.enabled:
            continue
        have = {m.id for m in p.models}
        extra = tuple(m for m in (R_model(i) for i in ids) if m.id not in have)
        providers[pid] = dataclasses.replace(p, models=tuple(p.models) + extra)
    return dataclasses.replace(registry, providers=providers)


def R_model(mid):
    from .registry import Model
    return Model(id=mid, approved=True, capabilities=("text",), price={}, note="discovered live by the daily provider health run (free; text only is assumed)")


def read_discovered(path, now, stale_days=DEFAULTS["stale_days"]):
    """{provider: [ids]} the last health run found AND probed OK, while that run is fresh; else {}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not fresh(data.get("at"), now, stale_days):
        return {}
    return {pid: list(ids) for pid, ids in (data.get("discovered_passed") or {}).items() if isinstance(ids, list)}


# ---- probing and scoring ---------------------------------------------------------------------------------------------------------------------------
def probe_all(router, lanes, cfg, now, progress=None):
    """{(provider, model): {ok, ms, tokens, class, error}}: one tiny call per distinct route of every job and the shared pool (Claude excluded)."""
    probes = {}
    for r in cn.targets(router, lanes):
        res = router.run_route("canary", r, instructions=cfg["probe_prompt"], now=now, ignore_rests=True)
        comp = res.get("completion")
        if res["status"] == "ok":
            rec = {"ok": True, "ms": int(getattr(comp, "latency_ms", 0) or 0), "tokens": int((getattr(comp, "tokens_in", 0) or 0) + (getattr(comp, "tokens_out", 0) or 0)), "class": "", "error": ""}
        else:
            why = str(res.get("reason", res["status"]))[:200]
            rec = {"ok": False, "ms": 0, "tokens": 0, "class": "skipped" if res["status"] == "skipped" else classify(why), "error": why}
        probes[(r["provider"], r["model"])] = rec
        if progress:
            progress(f"health: {r['provider']}/{r['model']}: " + (f"OK in {rec['ms']} ms, {rec['tokens']} tokens" if rec["ok"] else f"{rec['class'].upper()}: {rec['error'][:90]}"))
    return probes


def cost_guard(lane, provider, share):
    """(ok, why): one call of this job, at its largest, must not be more than `share` of the provider's daily token allowance, nor one request more than `share` of its daily requests."""
    lim = getattr(provider, "limits", {}) or {}
    if lim.get("tokens_per_day") and (lane.max_tokens + SKIP_ASSUMED_TOKENS_IN) > share * lim["tokens_per_day"]:
        return False, f"one call of {lane.name} (up to {lane.max_tokens + SKIP_ASSUMED_TOKENS_IN} tokens) is more than {int(share * 100)}% of the {lim['tokens_per_day']} tokens a day"
    if lim.get("requests_per_day") and 1.0 / lim["requests_per_day"] > share:
        return False, f"one request is more than {int(share * 100)}% of the {lim['requests_per_day']} requests a day"
    return True, ""


def terms_score(provider):
    status = getattr(provider, "terms", {}).get("status", "unverified")
    return 1.0 if status in ("verified", "upstream_documented", "verified_secondary") else 0.8 if status == "owner_decision" else 0.5


def score_lane(router, lane, probes, rankings, cfg, now):
    """[(score, route, probe)] best first for the routes of one job that passed the probe, are FREE, fit the job and pass the cost guard; plus {route: why} for the ones the guards left out."""
    order = {(r["provider"], r["model"]): i for i, r in enumerate(((rankings.get(lane.name) or {}).get("ranking")) or [])}
    left, rows = {}, []
    for i, r in enumerate(router._routes(lane)):
        key = (r["provider"], r["model"])
        if r["provider"] == "claude" or key not in probes:
            continue
        p = router.registry.providers.get(r["provider"])
        pr = probes[key]
        if p is None or p.paid:
            left[key] = "paid or unknown provider: never added"
            continue
        if not pr["ok"]:
            continue
        if not router.capable(lane, r):
            left[key] = "the model does not fit the job (capability guard)"
            continue
        ok, why = cost_guard(lane, p, cfg["cost_guard_share"])
        if not ok:
            left[key] = why + " (cost guard)"
            continue
        quality = max(0.3, 1.0 - 0.08 * order[key]) if key in order else max(0.3, 0.8 - 0.04 * i)
        cand = {"name": f"{key[0]}/{key[1]}", "quality": quality, "headroom": max(0.0, 1.0 - router.usage.load(p, now)), "reliability": router.usage.reliability(r["provider"], now),
                "latency_s": pr["ms"] / 1000.0, "cost_usd": 0.0, "capability": 1.0, "terms": terms_score(p)}
        rows.append((OM.score(cand), i, r, pr))
    rows.sort(key=lambda x: (-x[0], x[1]))
    return [(s, r, pr) for s, _i, r, pr in rows], left


# ---- the run ---------------------------------------------------------------------------------------------------------------------------------------
def _previous(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_health(router, lanes, registry, *, probes, discovered, now, cfg, health_path, rankings_path, claude_route=None, progress=None, judge_fn=None, live_lists=None):
    """Decide the new lists. Returns {report, health, rankings, lines, message}; `health` and `rankings` are the dicts to WRITE (or None when nothing may be written); the caller writes them."""
    prev = _previous(health_path)
    prev_rank = _previous(rankings_path)
    seen_before = {tuple(k.split("/", 1)) for k in (prev.get("models_seen") or [])}
    prev_top = {lane: (v.get("top") or [None])[0] for lane, v in (prev.get("lanes") or {}).items()}
    jobs_prev = (prev_rank.get("jobs") or {})
    scored, left_out = {}, {}
    for name in rk.JOBS:
        lane = lanes.get(name)
        if lane is None:
            continue
        scored[name], left_out[name] = score_lane(router, lane, probes, jobs_prev, cfg, now)
    dead = {k: v for k, v in probes.items() if not v["ok"] and v["class"] in PERMANENT}
    working = {k for k, v in probes.items() if v["ok"]}
    # the routes each job lists TODAY (the chain after the overlay): dead ones among them are candidates to drop
    current = {name: [(r["provider"], r["model"]) for r in router._routes(lanes[name]) if r["provider"] != "claude"] for name in scored}
    drop_candidates = sorted({k for name in current for k in current[name] if k in dead})
    top_new = {}
    for name, rows in scored.items():
        top_new[name] = [(r["provider"], r["model"]) for _s, r, _p in rows]
    add_candidates = []
    for name, keys in top_new.items():
        for k in keys[:3]:
            if k not in current[name] and k not in add_candidates:
                add_candidates.append(k)
    # never add a route that is not free: scored lists only hold free providers already; never remove the last working model of a job
    swaps, flagged = [], []
    allowed_drop, allowed_add = [], []
    budget = cfg["max_swaps"]
    for k in drop_candidates:
        lanes_with = [n for n in current if k in current[n]]
        blocked = [n for n in lanes_with if not [x for x in top_new.get(n, []) if x != k]]
        if blocked:
            flagged.append(f"{k[0]}/{k[1]} is dead but the LAST working model of {', '.join(blocked)} would go with it: kept, flagged")
            continue
        if budget > 0:
            allowed_drop.append(k)
            budget -= 1
            swaps.append(f"dropped dead {k[0]}/{k[1]} ({dead[k]['class']}: {dead[k]['error'][:60]})")
        else:
            flagged.append(f"swap cap reached: {k[0]}/{k[1]} is dead and stays listed until the next run")
    for k in add_candidates:
        if budget > 0:
            allowed_add.append(k)
            budget -= 1
            swaps.append(f"added {k[0]}/{k[1]} (it answered the probe)")
        else:
            flagged.append(f"swap cap reached: {k[0]}/{k[1]} passed the probe but is not added until the next run")
    # the lists to write: a job is written when a route in it is dropped or added, or its top pick changed with a real margin
    new_lists, undecided = {}, {}
    for name, rows in scored.items():
        keys = [(r["provider"], r["model"]) for _s, r, _p in rows]
        keys = [k for k in keys if k not in dead or k not in allowed_drop]
        if not keys:
            flagged.append(f"{name}: no model passed the probe: the last known-good list is kept")
            continue
        keys = [k for k in keys if k in current[name] or k in allowed_add]
        scores = {(r["provider"], r["model"]): s for s, r, _p in rows}
        changed = bool(set(current[name]) & set(allowed_drop)) or (set(keys) != set(current[name]) - set(allowed_drop)) or (keys[:1] != [k for k in current[name] if k in working][:1] and len(keys) > 1 and scores[keys[0]] - scores[keys[1]] > cfg["tie_margin"])
        if not changed:
            continue
        new_lists[name] = keys
        reasons = []
        if any(k not in seen_before for k in keys[:3]):
            reasons.append("a model never seen before is in the top 3")
        if len(keys) > 1 and abs(scores[keys[0]] - scores[keys[1]]) <= cfg["tie_margin"]:
            reasons.append("a tie at the top")
        if prev_top.get(name) and tuple(str(prev_top[name]).split("/", 1)) in dead:
            reasons.append("the previous top pick died")
        if reasons:
            undecided[name] = reasons
    judge = {"called": False, "lanes": sorted(undecided), "accepted": {}, "rejected": {}, "reasoning": "", "reply": ""}
    today = str(now)[:10]
    if undecided and cfg["judge"] and (claude_route is not None or judge_fn is not None) and str(prev.get("judge_at", ""))[:10] != today:
        lanes_asked = sorted(undecided)[: cfg["judge_max_lanes"]]
        table = "\n".join(f"{n}: " + "; ".join(f"{k[0]}/{k[1]} score {round(next(s for s, r, _p in scored[n] if (r['provider'], r['model']) == k), 3)}" for k in new_lists[n][:6]) + f" [why asked: {', '.join(undecided[n])}]" for n in lanes_asked)
        prompt = ("Several jobs need their free-model order decided. For each job below pick the best order of the listed models (best first), using only the models listed for that job. Reply with JSON only: "
                  '{"jobs": {"<job>": ["provider/model", ...]}, "reason": "<one or two sentences>"}.\n\n' + table)
        if judge_fn is not None:
            reply = judge_fn(prompt)
        else:
            res = router.run_route("canary", claude_route, instructions=prompt, system="You rank free language models for fixed jobs. Reply with JSON only.", now=now, ignore_rests=True)
            reply = res.get("output") if isinstance(res.get("output"), dict) else str(res.get("text") or res.get("output") or "")
        judge["called"], judge["reply"] = True, (json.dumps(reply) if isinstance(reply, dict) else str(reply))[:1500]
        try:
            blob = reply if isinstance(reply, dict) else json.loads(re.search(r"\{.*\}", judge["reply"], re.S).group(0))
            judge["reasoning"] = str(blob.get("reason", ""))[:400]
            for n, order in (blob.get("jobs") or {}).items():
                valid = [tuple(str(x).split("/", 1)) for x in order if isinstance(x, str)]
                if n in new_lists and valid and set(valid) <= set(new_lists[n]):              # its pick must be among candidates that already passed every guard
                    rest = [k for k in new_lists[n] if k not in valid]
                    new_lists[n] = valid + rest
                    judge["accepted"][n] = [f"{a}/{b}" for a, b in valid]
                else:
                    judge["rejected"][n] = "not among the candidates that passed the guards"
        except (ValueError, AttributeError, TypeError):
            judge["rejected"]["*"] = "its answer was not usable JSON: the rules decided"
    elif undecided and not judge["called"]:
        judge["reasoning"] = "the rules decided" + ("" if claude_route is not None else " (no Claude client in this run)") + ("; already asked today" if str(prev.get("judge_at", ""))[:10] == today else "")
    discovered_passed = {pid: [i for i in ids if probes.get((pid, i), {}).get("ok")] for pid, ids in (discovered or {}).items()}
    health = {"at": now, "config": {k: cfg[k] for k in ("auto_update", "max_swaps", "cost_guard_share", "stale_days")},
              "providers": {}, "discovered_passed": {k: v for k, v in discovered_passed.items() if v}, "swaps": swaps, "flagged": flagged,
              "lanes": {n: {"top": [f"{k[0]}/{k[1]}" for k in top_new.get(n, [])[:4]], "written": n in new_lists} for n in scored}, "judge": judge,
              "judge_at": today if judge["called"] else prev.get("judge_at", ""), "models_seen": sorted({f"{k[0]}/{k[1]}" for k in probes} | set(prev.get("models_seen") or [])), "guards_left_out": {n: {f"{k[0]}/{k[1]}": w for k, w in v.items()} for n, v in left_out.items() if v}}
    for (pid, mid), rec in sorted(probes.items()):
        health["providers"].setdefault(pid, {"models": {}})["models"][mid] = rec
    health["live_lists"] = {}
    for pid, info in (live_lists or {}).items():
        if info.get("ids") is None:
            health["live_lists"][pid] = {"kind": info.get("kind", "none"), "why": info.get("why", "")}
            continue
        ids = set(info["ids"])
        prov = registry.providers.get(pid)
        pinned = [m.id for m in (prov.models if prov else ()) if m.approved]
        gw = {m: next((r.get("gateway_model") or r["model"] for r in router.pool if r["provider"] == pid and r["model"] == m), m) for m in pinned}
        missing = [m for m in pinned if m not in ids and gw[m] not in ids]
        health["live_lists"][pid] = {"kind": info["kind"], "count": len(ids), "pinned_not_listed": missing, "added_free": len((discovered or {}).get(pid, []))}
        for m in missing:
            health["providers"].setdefault(pid, {"models": {}})["models"].setdefault(m, {})["not_in_live_list"] = True
    rankings = None
    if new_lists:
        rankings = json.loads(json.dumps(prev_rank)) if prev_rank else {"jobs": {}}
        rankings.setdefault("jobs", {})
        by = {n: {(r["provider"], r["model"]): (s, pr) for s, r, pr in rows} for n, rows in scored.items()}
        for n, keys in new_lists.items():
            rankings["jobs"][n] = {"ranking": [{"provider": k[0], "model": k[1], "passes": 1, "runs": 1, "mean_score": round(by[n][k][0], 3), "median_ms": by[n][k][1]["ms"]} for k in keys if k in by[n]],
                                   "excluded": [{"provider": k[0], "model": k[1], "runs": 1, "failures": [dead[k]["error"][:100]]} for k in allowed_drop if k in current[n]],
                                   "not_measured": [], "routes_tried": len(by[n]), "generated_by": "health", "generated_at": now}
        rankings["health_at"] = now
    lines = [f"Provider health {str(now)[:10]}: {sum(1 for v in probes.values() if v['ok'])} of {len(probes)} route(s) answered the probe; {len(dead)} dead or out of credits; {sum(len(v) for v in discovered_passed.values())} live free model(s) passed."]
    lines += [f"  swap: {s}" for s in swaps] + [f"  flagged: {f}" for f in flagged]
    lines += [f"  list written: {n}: " + " > ".join(f"{a}/{b}" for a, b in new_lists[n][:4]) for n in sorted(new_lists)]
    listed = {pid: v for pid, v in health["live_lists"].items() if v.get("count") is not None}
    if live_lists:
        lines.append(f"  live model lists read from {len(listed)} of {len(live_lists)} provider(s); {sum(len(v['pinned_not_listed']) for v in listed.values())} pinned model(s) are no longer in their provider's list" + (": " + ", ".join(f"{p}: {', '.join(v['pinned_not_listed'][:3])}" for p, v in listed.items() if v["pinned_not_listed"]) if any(v["pinned_not_listed"] for v in listed.values()) else ""))
    lines.append("  judge: " + (f"Claude asked about {', '.join(judge['lanes'])}; accepted {len(judge['accepted'])}; {judge['reasoning'][:160]}" if judge["called"] else judge["reasoning"] or "the rules decided every job (no Claude call)"))
    if not cfg["auto_update"]:
        lines.append("  auto_update is OFF (ai_health.yaml): this run only reports; nothing was written")
        health, rankings = None, None
    message = f"Provider health {str(now)[:10]}: " + (", ".join(swaps[:4]) if swaps else "no list changes") + (f" ({len(new_lists)} job list(s) written)" if new_lists else "")
    return {"health": health, "rankings": rankings, "lines": lines, "message": message[:200], "new_lists": {n: [f"{a}/{b}" for a, b in v] for n, v in new_lists.items()}, "judge": judge, "swaps": swaps, "flagged": flagged}


def write(result, health_path, rankings_path):
    """Write ONLY the two generated files. Returns the paths written."""
    written = []
    for data, path in ((result["health"], health_path), (result["rankings"], rankings_path)):
        if data is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
            written.append(str(path))
    return written
