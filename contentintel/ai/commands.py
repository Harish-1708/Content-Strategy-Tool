"""The `ai` commands: plan, providers, usage, review, ask, check, eval, smoke.

Everything these commands print is labelled by what it can prove:
  MOCK     a scripted stand-in answered: it proves our code, never a provider.
  LIVE     a real provider answered through the real gateway or API.
  APPLICATION problem: our configuration or code (a missing key, a bad request). It is ours to fix.
  EXTERNAL problem: the provider (a rate limit, an outage, a timeout, an unusable answer). It is not a bug in this tool.
Lint and mock results are never reported as live integration.
"""
import json
import os
import secrets
from pathlib import Path

from .. import contract
from . import clients as cl
from . import data_class as dc
from . import evaluate as ev
from . import gateway as gw
from . import lanes as lanes_mod
from . import registry as reg_mod
from . import review as review_mod
from . import router as router_mod
from . import usage as usage_mod
from .lanes import Lane

APPLICATION = {contract.NEED_SETUP, contract.BAD_INPUT}


def categorise(code):
    """APPLICATION for a problem on our side, EXTERNAL for one on the provider's side."""
    if code in APPLICATION:
        return "APPLICATION"
    return "EXTERNAL"


def sample_from_schema(schema):
    """The smallest answer that passes a (subset) JSON schema: used by mocks, so a mock never fails on shape."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object" or "properties" in schema or "required" in schema:
        props = schema.get("properties", {})
        return {k: sample_from_schema(props.get(k, {"type": "string"})) for k in schema.get("required", list(props))}
    if t == "array":
        return [sample_from_schema(schema.get("items", {"type": "string"})) for _ in range(max(1, schema.get("minItems", 1)))]
    if t in ("number", "integer"):
        return max(schema.get("minimum", 0), 0.9 if t == "number" and schema.get("maximum", 1) <= 1 else schema.get("minimum", 1))
    return "x" * max(1, schema.get("minLength", 1)) if t == "string" or t is None else "x"


def lane_mock(lanes):
    """A client that answers each lane with a valid answer for that lane. MOCK only."""
    def answer(route, prompt, lane):
        spec = lanes[lane].output if lane in lanes else {"format": "text"}
        if spec["format"] == "json":
            return json.dumps(sample_from_schema(spec.get("schema") or {}))
        need = int(spec.get("min_chars", 1))
        text = "A scripted answer for the " + str(lane) + " lane, long enough to pass the length check of the lane. "
        return (text * (need // len(text) + 1)).strip()
    return cl.MockClient([answer], name="lane-mock")


CANARY_MAX_TOKENS = 512          # the tiny live check asks for one word, but a thinking model (Gemini 3.x) spends part of the limit on hidden reasoning: 24 left it nothing to answer with ("empty completion")


def canary_lane():
    return Lane(name="canary", description="a tiny live check", max_data_class=dc.PUBLIC, chain=(), claude_fallback=False, claude_model="claude-haiku-5-5", claude_leads=False,
                output={"format": "text", "min_chars": 1}, forbid=(), review="none", review_sample_every=0, allow_experimental=False, max_tokens=CANARY_MAX_TOKENS, min_confidence=0)


def load_all(args):
    registry = reg_mod.load_registry(args.providers_config)
    return registry, lanes_mod.load_lanes(args.lanes_config, registry)


def build_stack(args, store, health, *, mode, clients):
    registry, lanes = load_all(args)
    pool = lanes_mod.load_pool(args.lanes_config, registry)                                  # the shared pool: every approved model, available to every lane that draws on it
    lanes = {**lanes, "canary": canary_lane()}
    shared = Path(store.data_dir) / "_shared" if mode == "live" else Path(args.workdir) / "mock-state"     # a mock run never touches the real spending ledger
    if mode == "live":                                                                      # the measured order of each job's list (the pool retest, the daily health run), where one has been measured
        from . import ranking as rk
        from . import provider_health as ph
        from datetime import datetime, timezone
        try:
            hcfg = ph.load_config(getattr(args, "health_config", ph.CONFIG))
        except ValueError:
            hcfg = dict(ph.DEFAULTS)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        found = ph.read_discovered(Path(store.data_dir) / "_shared" / ph.HEALTH_FILE, stamp, hcfg["stale_days"])           # live free models the last health run found AND probed OK, while it is fresh
        if found:
            registry = ph.with_discovered(registry, found)
            pool = list(pool) + [{"provider": pid, "model": mid, "gateway_model": mid} for pid, ids in found.items() for mid in ids if (pid, mid) not in {(r["provider"], r["model"]) for r in pool}]
        lanes, _ranked = rk.apply_overlay(lanes, Path(store.data_dir) / "_shared" / rk.RANKINGS_FILE, stamp, hcfg["stale_days"])
    usage, review = usage_mod.UsageLedger(shared / "ai_usage.json"), review_mod.ReviewQueue(shared / "ai_review.json")
    return registry, lanes, usage, review, router_mod.Router(registry, lanes, usage, health, clients, review, pool=pool)


def save_state(usage, review, health, store, mode="live"):
    usage.save()
    review.save()
    if health is not None and mode == "live":              # mock calls say nothing about a provider's health
        health.save(store)


def mock_clients(registry, lanes):
    mock = lane_mock(lanes)
    return {pid: mock for pid, p in registry.providers.items() if p.enabled or pid == "claude"}


def live_clients(registry, environ, gateway_dir, workdir, notes):
    """Real clients for enabled providers that have a credential. Starts the gateway on localhost when any gateway provider is usable."""
    clients, side = {}, None
    gateway_providers = [p for p in registry.providers.values() if p.enabled and p.id != "claude" and not p.direct and not p.omniroute]
    for p in registry.providers.values():                    # providers called straight from here (GitHub Models), with the credential from the environment
        if p.enabled and p.direct:
            token = next((environ[n].strip() for n in p.credential_env if environ.get(n, "").strip()), "")
            if token or p.direct.get("keyless"):                                    # a keyless provider (OVH's free tier, AI Horde's anonymous key) needs no secret: none is sent
                if p.direct.get("kind") == "horde":
                    clients[p.id] = cl.HordeClient(p.direct["base_url"])
                else:
                    klass = cl.OpenRouterClient if str(p.direct["base_url"]).startswith("https://openrouter.ai") else cl.FreeDiscoveryClient if p.direct.get("discover_free") else cl.DirectClient
                    clients[p.id] = klass(p.direct["base_url"], "" if p.direct.get("keyless") else token)
            else:
                notes.append("SKIPPED: " + p.id + ": no credential in the environment (" + ", ".join(p.credential_env) + "), so it is not tried; add the secret or switch the provider off to remove this line")
    if "ANTHROPIC_API_KEY" in environ and environ["ANTHROPIC_API_KEY"].strip() and registry.providers["claude"].enabled:
        clients["claude"] = cl.AnthropicClient(environ["ANTHROPIC_API_KEY"].strip())
    elif registry.providers["claude"].enabled:
        notes.append("APPLICATION: ANTHROPIC_API_KEY is not set, so Claude is unavailable (mock mode still works)")
    if gateway_providers:
        if not gw.env_ready(environ):
            notes.append("APPLICATION: FREEAPI_ENCRYPTION_KEY and FREEAPI_ADMIN_PASSWORD are needed to start the gateway")
        elif not gateway_dir or not Path(gateway_dir, "server/dist/index.js").is_file():
            notes.append("APPLICATION: the gateway is not built at " + str(gateway_dir))
        else:
            lock = gw.load_lock()
            config, report = gw.build_config(registry, environ, gw.baseline_from_inventory())
            notes.extend("SKIPPED: " + s["provider"] + ": no credential in the environment (" + s["reason"].split(": ", 1)[-1] + "), so it is not tried; add the secret or switch the provider off to remove this line" for s in report["skipped"])
            if report["keys"]:
                port = gw.free_port()
                side = gw.Sidecar(gateway_dir, lock)
                side.start(gw.gateway_env(lock, environ, port=port, db_path=Path(workdir) / "gateway.db", config=config), port, Path(workdir) / "gateway.log")
                side.wait_ready()
                client = cl.GatewayClient(side.base_url, side.bootstrap(environ["FREEAPI_ADMIN_PASSWORD"]))
                added, add_problems = side.add_missing_models(environ["FREEAPI_ADMIN_PASSWORD"], registry, environ)
                if added:
                    notes.append(f"NOTE: {len(added)} approved model(s) the pinned gateway's catalogue does not hold were added through its add-model call: " + ", ".join(added))
                for problem in add_problems:
                    notes.append("NOTE: could not add an approved model to the gateway: " + problem)
                isolated, why = side.isolate_models(environ["FREEAPI_ADMIN_PASSWORD"])
                if not isolated:
                    notes.append("NOTE: could not make each provider's model its own group in the gateway (" + why + "); a call that falls back to another provider is refused, not accepted")
                for p in gateway_providers:
                    if p.gateway_platform in report["keys"] or not p.credential_env:
                        clients[p.id] = client
    omni_side = _start_omniroute(registry, environ, clients, workdir, notes)
    if omni_side is not None:
        from .omniroute import Sidecars
        side = Sidecars(side, omni_side) if side is not None else omni_side
    return clients, side


def _start_omniroute(registry, environ, clients, workdir, notes):
    """OmniRoute (the second gateway) when an enabled provider is held by it and has a credential here; every reason it is not started is a note, never silent."""
    from . import omniroute as om
    wanted = [p for p in registry.providers.values() if p.enabled and p.omniroute]
    if not wanted:
        return None
    ready = [p for p in wanted if any(environ.get(n, "").strip() for n in p.credential_env)]
    for p in wanted:
        if p not in ready:
            notes.append("SKIPPED: " + p.id + ": no credential in the environment (" + ", ".join(p.credential_env) + "), so it is not tried; add the secret to use it")
    if not ready:
        return None
    omni_dir = environ.get("CONTENTINTEL_OMNIROUTE_DIR", "")
    if om.entry_point(omni_dir) is None:
        notes.append("APPLICATION: OmniRoute is not installed at " + (omni_dir or "(CONTENTINTEL_OMNIROUTE_DIR is not set)") + ", so " + ", ".join(p.id for p in ready) + " cannot be tried")
        return None
    side = om.OmniRouteSidecar(omni_dir)
    try:
        side.start(gw.free_port(), Path(workdir) / "omniroute-data", Path(workdir) / "omniroute.log")
        side.wait_ready()
        try:
            key = side.bootstrap()
        except om.OmniRouteError as exc:
            if "refused the login" not in str(exc):
                raise
            notes.append("NOTE: " + str(exc)[:240] + "; starting OmniRoute again with a new password and new folders")
            side.restart_fresh()
            key = side.bootstrap()
        client = om.OmniRouteClient(side.base_url, key)
    except om.OmniRouteError as exc:
        tail = getattr(side, "log_tail", lambda: "")()
        notes.append("APPLICATION: OmniRoute could not be started: " + str(exc)[:300] + ((" | its log: " + tail) if tail else ""))
        side.stop()
        return None
    for p in ready:                                                                      # one provider OmniRoute does not accept never takes the others down with it
        token = next(environ[n].strip() for n in p.credential_env if environ.get(n, "").strip())
        try:
            side.add_provider(p.omniroute["provider"], p.id, token)
            clients[p.id] = client
        except om.OmniRouteError as exc:
            notes.append(f"APPLICATION: OmniRoute did not accept {p.id} (provider id {p.omniroute['provider']}): {str(exc)[:200]}")
    if not any(p.id in clients for p in ready):
        side.stop()
        return None
    return side


def _fmt_rows(rows, keys):
    return "\n".join("  " + "  ".join(str(r[k]) for k in keys) for r in rows)


def run_ai(args, store, health, now):
    action = args.action
    if action == "catalog":
        from . import catalog as cat
        registry, _ = load_all(args)
        print(cat.format_report(cat.run(registry, os.environ, cat.inventory())))
        return 0
    if action == "smoke":
        registry, _ = load_all(args)
        if not Path(args.gateway_dir, "server/dist/index.js").is_file():
            print(f"APPLICATION problem: the pinned gateway is not built at {args.gateway_dir} (the workflow fetches and builds it first)")
            return 1
        lock = gw.load_lock()
        results, report = __import__("contentintel.ai.smoke", fromlist=["run_smoke"]).run_smoke(args.gateway_dir, registry, lock, args.workdir, gw.baseline_from_inventory())
        print("MOCK-PROVIDER result through the REAL pinned gateway (no provider account is used; this proves the plumbing, not any provider):")
        for name, ok, detail in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
        failed = [n for n, ok, _ in results if not ok]
        print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
        return 1 if failed else 0
    registry, lanes = load_all(args)
    if action == "providers":
        rows = [{"id": p.id, "on": "RETIRED" if p.retired else ("ON" if p.enabled else "off"), "ceiling": p.effective_ceiling + ("*" if p.override else ""), "classes": ",".join(p.approved_classes) or "-",
                 "terms": p.terms.get("status", "?"), "models": f"{sum(1 for m in p.models if m.approved)}/{len(p.models)}", "exp": "exp" if p.experimental else ""}
                for p in registry.providers.values() if args.all or p.enabled or p.retired]
        print("AI providers (* = ceiling raised by a recorded owner decision; models = approved/listed):")
        print(_fmt_rows(rows, ("id", "on", "ceiling", "classes", "terms", "models", "exp")) if rows else "  none enabled")
        print(f"  {len(registry.providers)} providers in the registry, {sum(1 for p in registry.providers.values() if p.enabled)} enabled, {sum(1 for p in registry.providers.values() if p.retired)} retired (kept as a record, never called)")
        return 0
    if action == "plan":
        dcl = args.data_class
        clients = {pid: None for pid, p in registry.providers.items() if (p.direct.get("keyless") or p.credential_env) and (p.direct.get("keyless") or (any if p.direct else all)(os.environ.get(n, "").strip() for n in p.credential_env))}      # a direct provider may list alternative names: the first one that is set is used
        _, lanes2, usage, review, router = build_stack(args, store, health, mode="live", clients=clients)
        print(f"Plan for {dcl} data (no call is made). A route is used only if every check passes:")
        for name in lanes:
            rows = router.explain(name, dcl, now)
            lane = lanes[name]
            used = [r for r in rows if r["eligible"]]
            print(f"{name}  [{lane.max_data_class} max; review {lane.review}{'; Claude leads' if lane.claude_leads else ''}]  ->  " + (f"{len(used)} route(s) ready" if used else "nothing can run: the task would be deferred"))
            for r in rows:
                print(f"    {'ready  ' if r['eligible'] else 'skipped'} {r['provider']} / {r['model']}" + (" (pool)" if r.get("source") == "pool" else "") + ("" if r["eligible"] and not r["reason"] else f": {r['reason']}"))
        return 0
    if action == "usage":
        usage = usage_mod.UsageLedger(Path(store.data_dir) / "_shared" / "ai_usage.json")
        rows = usage.summary(now)
        print(f"AI usage today (spend today ${usage.spend(now):.4f} of ${registry.daily_usd:.2f}; this month ${usage.spend(now, month=True):.4f} of ${registry.monthly_usd:.2f}):")
        print(_fmt_rows(rows, ("provider", "requests", "failures", "tokens", "cost_usd", "avg_latency_ms")) if rows else "  no calls today")
        for line in usage_mod.billing_lines(usage.billing(now), registry):
            print(line)
        from . import pricing as pricing_mod
        for line in pricing_mod.lines(registry):
            print(line)
        return 0
    if action == "review":
        q = review_mod.ReviewQueue(Path(store.data_dir) / "_shared" / "ai_review.json")
        if args.approve or args.reject:
            rid, decision = (args.approve, "approved") if args.approve else (args.reject, "rejected")
            if not args.by:
                print("Error: --by NAME is required: a decision is a person's, recorded with their name")
                return 1
            try:
                q.decide(rid, decision, args.by, now, args.note)
            except (KeyError, ValueError) as exc:
                print("Error:", str(exc).strip("'\""))
                return 1
            q.save()
            print(f"{rid} {decision} by {args.by}.")
            return 0
        pending = q.pending()
        print(f"{len(pending)} output(s) waiting for review:")
        for i in pending:
            print(f"  {i['id']}  {i['lane']}  ({i['data_class']}; {i['reason']})  via {i['route']['provider']}/{i['route']['model']}\n      " + json.dumps(i["output"], ensure_ascii=False)[:300])
        return 0
    # actions that call models
    mode = "mock" if args.mock else "live"
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    notes, side = [], None
    try:
        environ = os.environ
        if action == "connect":                                                           # the connection test tries every provider, switched on or not, and NEVER with the repository's Claude key
            registry = all_providers_on(registry)
            environ = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        if mode == "mock":
            clients = mock_clients(registry, lanes)
        else:
            clients, side = live_clients(registry, environ, args.gateway_dir, workdir, notes)
        registry2, lanes2, usage, review, router = build_stack(args, store, health, mode=mode, clients=clients)
        if action == "connect":
            router.registry.providers.update(all_providers_on(router.registry).providers)
        if action == "ask":
            res = router.run(args.lane, data_class=args.data_class, instructions=args.text, now=now)
            print(f"[{mode.upper()}] status {res['status']}" + (f" via {res['route']['provider']}/{res['route']['model']}" if res["route"] else "") + (f": {res.get('reason')}" if res.get("reason") else ""))
            for s in res["skipped"]:
                print(f"    skipped {s['provider']}: {s['reason']}")
            for a in res["attempts"]:
                print(f"    tried {a['provider']}/{a['model']}: {a['result']}" + (f" {a.get('message', '')}" if a["result"] != "ok" else ""))
            if res["output"] is not None:
                print(json.dumps(res["output"], ensure_ascii=False)[:600] if not isinstance(res["output"], str) else res["output"][:600])
            save_state(usage, review, health, store, mode)
            return 0 if res["status"] in ("ok", "needs_review") else 1
        if action == "eval":
            tasks = ev.load_tasks(args.tasks_config, lanes)
            only = {x.strip() for x in args.only.split(",") if x.strip()} or None
            rows = ev.run_eval(router, tasks, now=now, mode=mode, store=Path(store.data_dir) / "_shared" / "ai_eval.jsonl", include_claude=args.include_claude, only_providers=only)
            print(f"Evaluation, {mode.upper()} results ({'a scripted stand-in: this tests the harness, not a model' if mode == 'mock' else 'real calls'}):")
            print(ev.format_compare(ev.compare(rows)))
            skipped = sorted({(r['provider'], r['reason']) for r in rows if r['status'] == 'skipped'})
            for p, why in skipped[:12]:
                print(f"  skipped {p}: {why}")
            save_state(usage, review, health, store, mode)
            return 0
        if action == "check":
            return _check(args, registry2, lanes2, router, usage, review, health, store, now, mode, notes)
        if action == "rank":
            return _rank(args, lanes2, router, usage, review, health, store, now, mode, notes)
        if action == "health":
            return _health(args, lanes2, router, usage, review, health, store, now, mode, clients)
        if action == "connect":
            return _connect(registry2, lanes2, router, usage, review, health, store, now, mode, notes)
    finally:
        if side is not None:
            side.stop()
    return 1


def all_providers_on(registry):
    """The same registry with every provider except Claude switched on, for the connection test only (nothing is written back): a provider the owner has not enabled yet can still be tried with its key."""
    import dataclasses
    for pid, p in list(registry.providers.items()):
        if pid != "claude" and not p.enabled and p.terms.get("status") not in ("unverified", "declared_only") and not p.retired:
            registry.providers[pid] = dataclasses.replace(p, enabled=True)
    return registry


def _connect(registry, lanes, router, usage, review, health, store, now, mode, notes):
    """Try every route of every provider once with a tiny prompt and report, per provider: connected (how many routes, how fast), failed (why) or not tried (why). Claude is never used."""
    from . import connect as cn
    if mode != "live":
        print("AI connect needs live mode (do not pass --mock)")
        return 2
    report = cn.run_connect(router, registry, lanes, notes, now)
    print(cn.format_report(report))
    save_state(usage, review, health, store, mode)
    return 0


def _rank(args, lanes, router, usage, review, health, store, now, mode, notes):
    """The pool retest: measure each job's providers on that job's real prompts and, with --apply, write the measured order (data/_shared/ai_rankings.json) that every later run uses."""
    from . import ranking as rk
    jobs = [j.strip() for j in args.jobs.split(",") if j.strip() and j.strip() != "all"] or list(rk.JOBS)
    unknown = [j for j in jobs if j not in rk.JOBS]
    if unknown:
        print(f"AI rank: no such job: {', '.join(unknown)}; the jobs are: {', '.join(rk.JOBS)}")
        return 2
    if mode != "live":
        print("AI rank needs live mode (do not pass --mock): a scripted stand-in measures nothing about a provider")
        return 2
    print(f"AI rank, LIVE: {len(jobs)} job(s), {args.runs} run(s) per probe and route (public synthetic inputs only):")
    for n in notes:
        print("  " + n)
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    report = rk.run_ranking(router, lanes, jobs, now=now, runs=args.runs, only_providers=only, progress=lambda s: print(s, flush=True))
    print(rk.format_report(report))
    save_state(usage, review, health, store, mode)
    measured = sum(job["routes_tried"] for job in report.values())
    if args.apply and measured:
        path = Path(store.data_dir) / "_shared" / rk.RANKINGS_FILE
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"jobs": {}}
        merged = {**(previous.get("jobs") or {}), **{k: v for k, v in report.items() if v["ranking"]}}          # a job that measured nothing keeps its earlier measured order
        rk.save(path, merged, now)
        print(f"AI rank: the measured order was written to {path} for: {', '.join(k for k, v in report.items() if v['ranking']) or 'no job'}")
    elif args.apply:
        print("AI rank: nothing could be measured (no provider answered), so no order was written and the seeded orders stay")
    return 0 if measured else 1


def _health(args, lanes, router, usage, review, health, store, now, mode, clients):
    """The daily provider-model health run (see provider_health.py): discover, probe, score, guard, let Claude judge only what the rules cannot decide, and write ONLY data/_shared/provider_health.json and ai_rankings.json."""
    from . import provider_health as ph
    if mode != "live":
        print("AI health needs live mode (do not pass --mock): a scripted stand-in probes nothing")
        return 2
    cfg = ph.load_config(args.health_config)
    discovered = ph.discover(router.registry, clients, cfg)
    live_lists = ph.list_live_models(router.registry, clients)
    router.registry = ph.with_discovered(router.registry, discovered)
    router.pool = list(router.pool) + [{"provider": pid, "model": mid, "gateway_model": mid} for pid, ids in discovered.items() for mid in ids]
    print(f"AI health, LIVE: {sum(len(v) for v in discovered.values())} live free model(s) discovered at {', '.join(sorted(discovered)) or 'no provider'}; probing every route with one tiny call:")
    probes = ph.probe_all(router, lanes, cfg, now, progress=lambda s: print(s, flush=True))
    claude_route = None
    if "claude" in clients and "foundation_json" in lanes:
        model = lanes["foundation_json"].claude_model
        claude_route = {"provider": "claude", "model": model, "gateway_model": model}
    shared = Path(store.data_dir) / "_shared"
    result = ph.run_health(router, lanes, router.registry, probes=probes, discovered=discovered, now=now, cfg=cfg, health_path=shared / ph.HEALTH_FILE, rankings_path=shared / "ai_rankings.json",
                           claude_route=claude_route, live_lists=live_lists)
    written = ph.write(result, shared / ph.HEALTH_FILE, shared / "ai_rankings.json")
    for line in result["lines"]:
        print(line)
    print("AI health: wrote " + (", ".join(written) if written else "nothing"))
    if args.message_file:
        Path(args.message_file).write_text(result["message"] + "\n", encoding="utf-8")
    save_state(usage, review, health, store, mode)
    return 0


def _check(args, registry, lanes, router, usage, review, health, store, now, mode, notes):
    rows, app_fail, ext_fail = [], 0, 0
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    print(f"AI check, {mode.upper()} ({'a scripted stand-in answers: this proves our code only' if mode == 'mock' else 'real calls to real providers'}):")
    for n in notes:
        skipped_for = n.split(":")[1].strip() if n.startswith("SKIPPED:") else ""
        if skipped_for and only and skipped_for not in only:                  # a provider that is not being tried in this run does not clutter it
            continue
        print("  " + n)
        app_fail += n.startswith("APPLICATION")
    targets = []
    for lane in lanes.values():
        if lane.name == "canary":
            continue
        for r in lane.chain:
            if (r["provider"], r["model"]) not in [(t["provider"], t["model"]) for t in targets] and (not only or r["provider"] in only):
                targets.append(r)
    for r in router.pool:                           # the shared pool is checked too: each model is one real call, reported under its own provider
        if (r["provider"], r["model"]) not in [(t["provider"], t["model"]) for t in targets] and (not only or r["provider"] in only):
            targets.append(r)
    outcome = {}                                    # provider -> {"ok": n, "failed": [codes], "skipped": [reasons]}
    for r in targets:
        res = router.run_route("canary", r, instructions="Reply with the single word OK.", now=now, ignore_rests=True)      # a check tries EVERY route, even one an earlier run left resting: finding out which work is its purpose
        slot = outcome.setdefault(r["provider"], {"ok": 0, "failed": [], "skipped": []})
        if res["status"] == "ok":
            c = res["completion"]
            slot["ok"] += 1
            rows.append(f"  {mode.upper():<5} ok        {r['provider']}/{r['model']}  {c.latency_ms} ms, {c.tokens_in}+{c.tokens_out} tokens")
        elif res["status"] == "skipped":
            slot["skipped"].append(res["reason"])
            rows.append(f"  skipped   {r['provider']}/{r['model']}: {res['reason']}")
        else:
            code = res["reason"] and next((a["result"] for a in reversed(res["attempts"])), "UPSTREAM_BROKEN")
            kind = categorise(code if code in contract.CODES else contract.UPSTREAM_BROKEN)
            app_fail += kind == "APPLICATION"
            ext_fail += kind == "EXTERNAL"
            slot["failed"].append(code)
            rows.append(f"  {kind:<11} {r['provider']}/{r['model']}: {code}: {res['reason']}")
    print("\n".join(rows) if rows else "  nothing to check")
    ok = sum(1 for x in rows if x.split()[1] == "ok")
    print(f"  {ok} answered ({mode}), {app_fail} application problem(s), {ext_fail} external provider problem(s), {sum(1 for x in rows if x.startswith('  skipped'))} skipped (not enabled or no credential)")
    print(provider_status_block(registry, outcome, mode, only, notes))
    if mode == "mock":
        print("  NOTE: a mock pass is not proof that any live provider works. Run with --live for that.")
    save_state(usage, review, health, store, mode)
    return 1 if app_fail or (args.strict and ext_fail) else 0


def provider_status(provider, outcome, mode, only, switched_off):
    """(label, detail) for one provider. LIVE only when a real call answered; a scripted stand-in is MOCK, never LIVE. RETIRED and DISABLED are never 'called', whatever else the run shows."""
    if provider.retired:
        return "RETIRED", f"{provider.label} was withdrawn by its provider on {provider.retired['date']}; never called"
    if provider.id in switched_off or not provider.enabled:
        return "DISABLED", "switched off by the owner"
    if only and provider.id not in only:
        return "SKIPPED", f"not in this run (only={','.join(sorted(only))})"
    slot = outcome.get(provider.id)
    if slot and slot["ok"]:
        word = "LIVE" if mode == "live" else "MOCK"
        return word, f"{slot['ok']} answered" + (f", {len(slot['failed'])} failed ({', '.join(sorted(set(slot['failed'])))})" if slot["failed"] else "") + ("" if mode == "live" else " (a scripted stand-in: not proof the provider works)")
    if slot and slot["failed"]:
        return "FAILED", ", ".join(sorted(set(slot["failed"]))) + " (the provider is enabled and was tried)"
    if slot and slot["skipped"]:
        return "SKIPPED", slot["skipped"][0]
    return "SKIPPED", "nothing was tried"


def provider_status_block(registry, outcome, mode, only, notes):
    """The per-provider summary printed at the end of a check: which providers answered, which failed, which were skipped, which the owner switched off and which have been retired."""
    switched_off = {n.split(":")[1].strip() for n in notes if n.startswith("SKIPPED:") and "switched off by the owner" in n}
    shown = [p for p in registry.providers.values() if p.enabled or p.retired or p.id in switched_off]
    order = {"LIVE": 0, "MOCK": 1, "FAILED": 2, "SKIPPED": 3, "DISABLED": 4, "RETIRED": 5}
    rows = sorted(((provider_status(p, outcome, mode, only, switched_off), p.id) for p in shown), key=lambda x: (order[x[0][0]], x[1]))
    counts = {}
    for (label, _), _pid in rows:
        counts[label] = counts.get(label, 0) + 1
    lines = ["  Provider status (a green run does not mean a provider is live: read this list):"] + [f"    {label:<8} {pid:<12} {detail}" for (label, detail), pid in rows]
    lines.append("    " + ", ".join(f"{counts.get(k, 0)} {k}" for k in ("LIVE", "MOCK", "FAILED", "SKIPPED", "DISABLED", "RETIRED") if counts.get(k) or k in ("LIVE", "FAILED", "RETIRED")))
    return "\n".join(lines)


def new_run_id():
    return secrets.token_hex(4)
