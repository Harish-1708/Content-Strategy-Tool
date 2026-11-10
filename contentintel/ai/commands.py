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


def canary_lane():
    return Lane(name="canary", description="a tiny live check", max_data_class=dc.PUBLIC, chain=(), claude_fallback=False, claude_model="claude-haiku-5-5", claude_leads=False,
                output={"format": "text", "min_chars": 1}, forbid=(), review="none", review_sample_every=0, allow_experimental=False, max_tokens=24, min_confidence=0)


def load_all(args):
    registry = reg_mod.load_registry(args.providers_config)
    return registry, lanes_mod.load_lanes(args.lanes_config, registry)


def build_stack(args, store, health, *, mode, clients):
    registry, lanes = load_all(args)
    lanes = {**lanes, "canary": canary_lane()}
    shared = Path(store.data_dir) / "_shared" if mode == "live" else Path(args.workdir) / "mock-state"     # a mock run never touches the real spending ledger
    usage, review = usage_mod.UsageLedger(shared / "ai_usage.json"), review_mod.ReviewQueue(shared / "ai_review.json")
    return registry, lanes, usage, review, router_mod.Router(registry, lanes, usage, health, clients, review)


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
    gateway_providers = [p for p in registry.providers.values() if p.enabled and p.id != "claude" and not p.direct]
    for p in registry.providers.values():                    # providers called straight from here (GitHub Models), with the credential from the environment
        if p.enabled and p.direct:
            token = next((environ[n].strip() for n in p.credential_env if environ.get(n, "").strip()), "")
            if token:
                clients[p.id] = cl.DirectClient(p.direct["base_url"], token)
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
                isolated, why = side.isolate_models(environ["FREEAPI_ADMIN_PASSWORD"])
                if not isolated:
                    notes.append("NOTE: could not make each provider's model its own group in the gateway (" + why + "); a call that falls back to another provider is refused, not accepted")
                for p in gateway_providers:
                    if p.gateway_platform in report["keys"] or not p.credential_env:
                        clients[p.id] = client
    return clients, side


def _fmt_rows(rows, keys):
    return "\n".join("  " + "  ".join(str(r[k]) for k in keys) for r in rows)


def run_ai(args, store, health, now):
    action = args.action
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
        rows = [{"id": p.id, "on": "ON" if p.enabled else "off", "ceiling": p.effective_ceiling + ("*" if p.override else ""), "classes": ",".join(p.approved_classes) or "-",
                 "terms": p.terms.get("status", "?"), "models": f"{sum(1 for m in p.models if m.approved)}/{len(p.models)}", "exp": "exp" if p.experimental else ""}
                for p in registry.providers.values() if args.all or p.enabled]
        print("AI providers (* = ceiling raised by a recorded owner decision; models = approved/listed):")
        print(_fmt_rows(rows, ("id", "on", "ceiling", "classes", "terms", "models", "exp")) if rows else "  none enabled")
        print(f"  {len(registry.providers)} providers in the registry, {sum(1 for p in registry.providers.values() if p.enabled)} enabled")
        return 0
    if action == "plan":
        dcl = args.data_class
        clients = {pid: None for pid, p in registry.providers.items() if p.credential_env and all(os.environ.get(n, "").strip() for n in p.credential_env)}
        _, lanes2, usage, review, router = build_stack(args, store, health, mode="live", clients=clients)
        print(f"Plan for {dcl} data (no call is made). A route is used only if every check passes:")
        for name in lanes:
            rows = router.explain(name, dcl, now)
            lane = lanes[name]
            used = [r for r in rows if r["eligible"]]
            print(f"{name}  [{lane.max_data_class} max; review {lane.review}{'; Claude leads' if lane.claude_leads else ''}]  ->  " + (f"{len(used)} route(s) ready" if used else "nothing can run: the task would be deferred"))
            for r in rows:
                print(f"    {'ready  ' if r['eligible'] else 'skipped'} {r['provider']} / {r['model']}" + ("" if r["eligible"] else f": {r['reason']}"))
        return 0
    if action == "usage":
        usage = usage_mod.UsageLedger(Path(store.data_dir) / "_shared" / "ai_usage.json")
        rows = usage.summary(now)
        print(f"AI usage today (spend today ${usage.spend(now):.4f} of ${registry.daily_usd:.2f}; this month ${usage.spend(now, month=True):.4f} of ${registry.monthly_usd:.2f}):")
        print(_fmt_rows(rows, ("provider", "requests", "failures", "tokens", "cost_usd", "avg_latency_ms")) if rows else "  no calls today")
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
        if mode == "mock":
            clients = mock_clients(registry, lanes)
        else:
            clients, side = live_clients(registry, os.environ, args.gateway_dir, workdir, notes)
        registry2, lanes2, usage, review, router = build_stack(args, store, health, mode=mode, clients=clients)
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
    finally:
        if side is not None:
            side.stop()
    return 1


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
    for r in targets:
        res = router.run_route("canary", r, instructions="Reply with the single word OK.", now=now)
        if res["status"] == "ok":
            c = res["completion"]
            rows.append(f"  {mode.upper():<5} ok        {r['provider']}/{r['model']}  {c.latency_ms} ms, {c.tokens_in}+{c.tokens_out} tokens")
        elif res["status"] == "skipped":
            rows.append(f"  skipped   {r['provider']}/{r['model']}: {res['reason']}")
        else:
            code = res["reason"] and next((a["result"] for a in reversed(res["attempts"])), "UPSTREAM_BROKEN")
            kind = categorise(code if code in contract.CODES else contract.UPSTREAM_BROKEN)
            app_fail += kind == "APPLICATION"
            ext_fail += kind == "EXTERNAL"
            rows.append(f"  {kind:<11} {r['provider']}/{r['model']}: {code}: {res['reason']}")
    print("\n".join(rows) if rows else "  nothing to check")
    ok = sum(1 for x in rows if x.split()[1] == "ok")
    print(f"  {ok} answered ({mode}), {app_fail} application problem(s), {ext_fail} external provider problem(s), {sum(1 for x in rows if x.startswith('  skipped'))} skipped (not enabled or no credential)")
    if mode == "mock":
        print("  NOTE: a mock pass is not proof that any live provider works. Run with --live for that.")
    save_state(usage, review, health, store, mode)
    return 1 if app_fail or (args.strict and ext_fail) else 0


def new_run_id():
    return secrets.token_hex(4)
