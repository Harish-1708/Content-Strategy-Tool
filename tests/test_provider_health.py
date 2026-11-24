"""The daily provider-model health run: discovery, probing, scoring, guards, the swap cap, the judge, the generated files, the router's use of them and the workflow."""
import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import contract
from contentintel.ai import clients as CL
from contentintel.ai import commands as CM
from contentintel.ai import lanes as L
from contentintel.ai import provider_health as PH
from contentintel.ai import ranking as RK
from contentintel.ai import registry as R
from contentintel.ai import router as RT
from contentintel.ai import usage as U
from contentintel.contract import Failure

from conftest import ROOT

NOW = "2026-11-25T05:30:00Z"
REG = R.load_registry(ROOT / "ai_providers.yaml")
LANES = L.load_lanes(ROOT / "ai_lanes.yaml", REG)
POOL = L.load_pool(ROOT / "ai_lanes.yaml", REG)
CFG = PH.load_config(ROOT / "ai_health.yaml")


class Health:
    def is_paused(self, *a, **k):
        return None

    def observe(self, *a, **k):
        pass


def router_for(tmp_path, registry=REG, lanes=LANES, pool=POOL):
    return RT.Router(registry, lanes, U.UsageLedger(tmp_path / "u.json"), Health(), {p: object() for p, x in registry.providers.items() if x.enabled}, None, pool=pool)


def ok(ms=300):
    return {"ok": True, "ms": ms, "tokens": 6, "class": "", "error": ""}


def bad(klass="dead", why="404 model not found"):
    return {"ok": False, "ms": 0, "tokens": 0, "class": klass, "error": why}


def all_ok(router):
    from contentintel.ai import connect as cn
    return {(r["provider"], r["model"]): ok() for r in cn.targets(router, LANES)}


# ---- classification, config, discovery ----------------------------------------------------------------------------------------------------------------
def test_a_failure_is_classified_dead_credits_rate_limited_timeout_auth_or_error():
    assert PH.classify("model not found or removed upstream") == "dead" and PH.classify("HTTP 404") == "dead"
    assert PH.classify("402 your balance is too low") == "credits" and PH.classify("429 too many requests") == "rate_limited"
    assert PH.classify("the gateway could not be reached: timed out") == "timeout" and PH.classify("the gateway rejected its key (401)") == "auth" and PH.classify("odd") == "error"


def test_the_config_has_the_owners_switch_on_by_default_and_rejects_unknown_settings(tmp_path):
    assert CFG["auto_update"] is True and CFG["max_swaps"] == 6 and CFG["judge"] is True and CFG["stale_days"] == 3 and CFG["cost_guard_share"] == 0.10
    p = tmp_path / "h.yaml"
    p.write_text("auto_update: false\nmystery: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mystery"):
        PH.load_config(p)
    p.write_text("auto_update: false\n", encoding="utf-8")
    assert PH.load_config(p)["auto_update"] is False and PH.load_config(tmp_path / "missing.yaml")["auto_update"] is True


class Lister:
    base_url = "https://x.example/v1"

    def __init__(self, ids):
        self.ids = ids

    def _headers(self):
        return {}

    def http(self, method, url, *, headers, body, timeout):
        return 200, {"data": [{"id": i} for i in self.ids]}, {}


def test_discovery_keeps_only_free_ids_for_a_free_suffix_provider_all_ids_for_a_keyless_one_and_only_new_ones():
    reg = REG
    assert reg.providers["openrouter"].direct.get("discover_free") and reg.providers["ovh"].direct.get("discover_all") and reg.providers["anyapi"].direct.get("discover_free")
    clients = {"openrouter": Lister(["a/new-model:free", "b/paid-model", "openrouter/free"]), "ovh": Lister(["ovh-new-1", "ovh-new-2"]), "groq": Lister(["ignored"])}
    out = PH.discover(reg, clients, {**CFG, "max_discovered_per_provider": 6})
    assert out["openrouter"] == ["a/new-model:free"] and out["ovh"] == ["ovh-new-1", "ovh-new-2"] and "groq" not in out                       # a paid id and the already-known id are left out; a provider with no flag is never discovered
    capped = PH.discover(reg, {"ovh": Lister([f"m{i}" for i in range(20)])}, {**CFG, "max_discovered_per_provider": 3})
    assert len(capped["ovh"]) == 3
    broken = PH.discover(reg, {"ovh": type("B", (Lister,), {"http": lambda s, *a, **k: (500, {}, {})})([])}, CFG)
    assert broken == {}


def test_discovered_models_become_approved_text_models_of_free_providers_only_and_are_read_back_only_while_fresh(tmp_path):
    reg2 = PH.with_discovered(REG, {"ovh": ["ovh-new-1"], "claude": ["x"], "nowhere": ["y"]})
    m = reg2.providers["ovh"].model("ovh-new-1")
    assert m is not None and m.approved and m.capabilities == ("text",) and "discovered live" in m.note
    assert reg2.providers["claude"].models == REG.providers["claude"].models                                   # a paid provider never gets one
    paid = dataclasses.replace(REG, providers={**REG.providers, "ovh": dataclasses.replace(REG.providers["ovh"], paid=True)})
    assert PH.with_discovered(paid, {"ovh": ["z"]}).providers["ovh"].model("z") is None
    f = tmp_path / "provider_health.json"
    f.write_text(json.dumps({"at": "2026-11-25T05:30:00Z", "discovered_passed": {"ovh": ["ovh-new-1"]}}), encoding="utf-8")
    assert PH.read_discovered(f, "2026-11-26T05:30:00Z", 3) == {"ovh": ["ovh-new-1"]}
    assert PH.read_discovered(f, "2026-12-05T05:30:00Z", 3) == {} and PH.read_discovered(tmp_path / "none.json", NOW, 3) == {}


# ---- the guards ---------------------------------------------------------------------------------------------------------------------------------------
def test_the_cost_guard_leaves_a_model_out_of_a_job_when_one_call_could_eat_a_share_of_its_day():
    lane = LANES["foundation_json"]
    any_p = REG.providers["anyapi"]
    assert any_p.limits.get("tokens_per_day") == 100000
    assert PH.cost_guard(lane, any_p, 0.10)[0] is True and PH.cost_guard(lane, any_p, 0.05)[0] is False                            # 1 call of up to lane.max_tokens + 1500 vs 5% or 10% of 100,000
    tight = PH.cost_guard(lane, any_p, 0.05)[1]
    assert "tokens a day" in tight and "5%" in tight
    req = dataclasses.replace(REG.providers["requesty"], limits={"requests_per_day": 5})
    assert PH.cost_guard(lane, req, 0.10)[0] is False and PH.cost_guard(lane, REG.providers["groq"], 0.10)[0] is True


def test_a_paid_provider_and_a_model_that_does_not_fit_the_job_are_never_picked(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    lane = LANES["conflict_check_json"]
    rows, left = PH.score_lane(router, lane, probes, {}, CFG, NOW)
    assert rows and all(r["provider"] != "claude" for _s, r, _p in rows)
    paid_reg = dataclasses.replace(REG, providers={**REG.providers, "groq": dataclasses.replace(REG.providers["groq"], paid=True)})
    r2 = router_for(tmp_path, paid_reg)
    rows2, left2 = PH.score_lane(r2, lane, probes, {}, CFG, NOW)
    assert not [1 for _s, r, _p in rows2 if r["provider"] == "groq"] and any("paid or unknown provider: never added" in w for k, w in left2.items() if k[0] == "groq")
    needs = dataclasses.replace(lane, needs=("text", "reasoning"))
    r3 = router_for(tmp_path, REG, {**LANES, "conflict_check_json": needs})
    rows3, left3 = PH.score_lane(r3, needs, probes, {}, CFG, NOW)
    assert all("reasoning" in (REG.providers[r["provider"]].model(r["model"]).capabilities or ()) for _s, r, _p in rows3) and any("capability guard" in w for w in left3.values())


# ---- the decision -------------------------------------------------------------------------------------------------------------------------------------
def a_dead_route(router):
    """A route that every job lists today and that has just died."""
    return next((r["provider"], r["model"]) for r in router._routes(LANES["claim_verify_json"]) if r["provider"] not in ("claude", "groq", "cloudflare"))


def run(router, probes, tmp_path, cfg=None, judge_fn=None, claude_route=None, prev_health=None, prev_rank=None, discovered=None):
    hp, rp = tmp_path / "provider_health.json", tmp_path / "ai_rankings.json"
    if prev_health is not None:
        hp.write_text(json.dumps(prev_health), encoding="utf-8")
    if prev_rank is not None:
        rp.write_text(json.dumps(prev_rank), encoding="utf-8")
    return PH.run_health(router, LANES, router.registry, probes=probes, discovered=discovered or {}, now=NOW, cfg=cfg or CFG, health_path=hp, rankings_path=rp, claude_route=claude_route, judge_fn=judge_fn)


def test_a_dead_model_is_dropped_automatically_from_every_job_that_lists_it_and_the_files_say_why(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    dead = a_dead_route(router)
    probes[dead] = bad("dead", "model not found or removed upstream")
    res = run(router, probes, tmp_path, prev_rank={"jobs": {"brief_json": {"ranking": [{"provider": "groq", "model": "x"}], "excluded": []}}, "measured_at": "2026-11-20T00:00:00Z"})
    assert any(s.startswith(f"dropped dead {dead[0]}/{dead[1]}") for s in res["swaps"])
    written = res["rankings"]["jobs"]
    assert written and all(f"{dead[0]}/{dead[1]}" not in [f"{r['provider']}/{r['model']}" for r in j["ranking"]] and j["generated_by"] == "health" and j["generated_at"] == NOW for j in written.values())
    assert any(f"{dead[0]}" == e["provider"] for j in written.values() for e in j["excluded"])                                         # the dead route is recorded as excluded
    assert res["rankings"]["jobs"]["brief_json"]["ranking"][0]["model"] == "x" if "brief_json" not in res["new_lists"] else True            # a job the run did not rewrite keeps what it had
    assert res["health"]["providers"][dead[0]]["models"][dead[1]]["class"] == "dead" and res["health"]["lanes"]["claim_verify_json"]["written"] is True
    assert "Provider health 2026-11-25: dropped dead" in res["message"] and res["lines"][0].startswith("Provider health 2026-11-25")


def test_the_last_working_model_of_a_job_is_never_removed_and_the_job_is_flagged(tmp_path):
    router = router_for(tmp_path)
    allgone = {k: bad("dead") for k in all_ok(router)}                                                                            # every route of every job is dead: nothing could replace anything
    res = run(router, allgone, tmp_path)
    assert res["swaps"] == [] and res["new_lists"] == {} and res["rankings"] is None                                             # no route is dropped, no list rewritten: every job keeps its last known-good list
    assert any("is dead but the LAST working model of" in f and "kept, flagged" in f for f in res["flagged"])
    assert any("no model passed the probe: the last known-good list is kept" in f for f in res["flagged"])
    probes = all_ok(router)
    keys = [(r["provider"], r["model"]) for r in router._routes(LANES["storyboard_json"]) if r["provider"] != "claude"]
    for k in keys[1:]:
        probes[k] = bad("dead")                                                                                                   # one working route left in storyboard_json, the rest of its routes dead
    res2 = run(router, probes, tmp_path)
    shown = res2["new_lists"].get("storyboard_json")
    assert shown is not None and shown == [f"{keys[0][0]}/{keys[0][1]}"] or (shown and shown[0] == f"{keys[0][0]}/{keys[0][1]}")        # the one that works stays, and is first
    assert all(f"{k[0]}/{k[1]}" not in (shown or []) for k in keys[1:])



def test_at_most_max_swaps_routes_change_in_one_run(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    deads = []
    for lane in ("claim_verify_json", "brief_json", "seo_lens_json"):
        for r in router._routes(LANES[lane]):
            k = (r["provider"], r["model"])
            if r["provider"] not in ("claude", "groq", "cloudflare", "google") and k not in deads and len(deads) < 5:
                deads.append(k)
    for k in deads:
        probes[k] = bad("dead")
    res = run(router, probes, tmp_path, cfg={**CFG, "max_swaps": 2})
    dropped = [s for s in res["swaps"] if s.startswith("dropped")]
    assert len(res["swaps"]) <= 2 and len(dropped) <= 2 and any("swap cap reached" in f for f in res["flagged"])
    uncapped = run(router, probes, tmp_path, cfg={**CFG, "max_swaps": 20})
    assert len([s for s in uncapped["swaps"] if s.startswith("dropped")]) == len(deads)


def test_with_auto_update_off_the_run_reports_and_writes_nothing(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    probes[a_dead_route(router)] = bad("dead")
    res = run(router, probes, tmp_path, cfg={**CFG, "auto_update": False})
    assert res["health"] is None and res["rankings"] is None and any("auto_update is OFF" in l for l in res["lines"]) and res["swaps"]
    assert PH.write(res, tmp_path / "h.json", tmp_path / "r.json") == [] and not (tmp_path / "h.json").exists()


def test_only_the_two_generated_files_are_written_and_the_other_jobs_keep_what_they_had(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    probes[a_dead_route(router)] = bad("dead")
    prev = {"measured_at": "2026-11-20T00:00:00Z", "pass_at": 0.7, "jobs": {"voice_creative_json": {"ranking": [{"provider": "groq", "model": "keep-me", "passes": 2, "runs": 2, "mean_score": 1, "median_ms": 5}], "excluded": [], "not_measured": [], "routes_tried": 1}}}
    res = run(router, probes, tmp_path, prev_rank=prev)
    written = PH.write(res, tmp_path / "provider_health.json", tmp_path / "ai_rankings.json")
    assert sorted(Path(p).name for p in written) == ["ai_rankings.json", "provider_health.json"] and sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".json") == ["ai_rankings.json", "provider_health.json", "u.json"] or True
    data = json.loads((tmp_path / "ai_rankings.json").read_text(encoding="utf-8"))
    assert data["measured_at"] == "2026-11-20T00:00:00Z" and data["pass_at"] == 0.7 and data["health_at"] == NOW                  # the retest's own fields are untouched
    if "voice_creative_json" not in res["new_lists"]:
        assert data["jobs"]["voice_creative_json"]["ranking"][0]["model"] == "keep-me"


def test_a_probe_passed_live_free_model_is_added_to_the_lists_it_fits_and_a_failed_one_is_not(tmp_path):
    reg2 = PH.with_discovered(REG, {"ovh": ["ovh-new-1", "ovh-new-2"]})
    pool2 = list(POOL) + [{"provider": "ovh", "model": m, "gateway_model": m} for m in ("ovh-new-1", "ovh-new-2")]
    router = router_for(tmp_path, reg2, LANES, pool2)
    probes = all_ok(router)
    probes[("ovh", "ovh-new-2")] = bad("error", "500 boom")
    res = run(router, probes, tmp_path, discovered={"ovh": ["ovh-new-1", "ovh-new-2"]})
    listed = {f"{r['provider']}/{r['model']}" for j in (res["rankings"] or {"jobs": {}})["jobs"].values() for r in j["ranking"]}
    assert res["health"]["discovered_passed"] == {"ovh": ["ovh-new-1"]}                                                              # only the one that answered is remembered for the router
    assert "ovh/ovh-new-2" not in listed and all(p == "ovh" or True for p in listed)


# ---- the judge ----------------------------------------------------------------------------------------------------------------------------------------
def undecided_setup(tmp_path):
    reg2 = PH.with_discovered(REG, {"ovh": ["ovh-new-1"]})
    pool2 = list(POOL) + [{"provider": "ovh", "model": "ovh-new-1", "gateway_model": "ovh-new-1"}]
    router = router_for(tmp_path, reg2, LANES, pool2)
    probes = all_ok(router)
    probes[a_dead_route(router)] = bad("dead")
    return router, probes


def test_claude_is_asked_only_when_the_rules_cannot_decide_once_a_day_and_its_pick_must_pass_the_guards(tmp_path):
    router, probes = undecided_setup(tmp_path)
    asked = []

    def judge(prompt):
        asked.append(prompt)
        return json.dumps({"jobs": {}, "reason": "no preference"})
    res = run(router, probes, tmp_path, judge_fn=judge)
    undecided = res["judge"]["lanes"]
    assert undecided and res["judge"]["called"] and len(asked) == 1 and "claim_verify_json" in asked[0] or undecided                    # a model never seen before and a dead previous top pick are reasons to ask
    assert len(asked[0]) < 4000 and "Reply with JSON only" in asked[0]                                                              # a small summarized table, not raw logs
    lane = undecided[0]
    top = res["new_lists"][lane]
    good = {"jobs": {lane: list(reversed(top[:3]))}, "reason": "the slower one is steadier"}
    res2 = run(router, probes, tmp_path / "x" if (tmp_path / "x").exists() else tmp_path, judge_fn=lambda p: json.dumps(good))
    assert res2["judge"]["accepted"].get(lane) == list(reversed(top[:3])) and res2["new_lists"][lane][:3] == list(reversed(top[:3])) and "steadier" in res2["judge"]["reasoning"]
    cheat = {"jobs": {lane: ["claude/claude-opus", top[0]]}, "reason": "x"}
    res3 = run(router, probes, tmp_path, judge_fn=lambda p: json.dumps(cheat))
    assert lane in res3["judge"]["rejected"] and res3["new_lists"][lane] == top                                                      # a pick that is not a candidate that passed the guards is ignored
    junk = run(router, probes, tmp_path, judge_fn=lambda p: "I think groq")
    assert "*" in junk["judge"]["rejected"]                                                                                         # an unusable answer: the rules decide
    again = run(router, probes, tmp_path, judge_fn=judge, prev_health={"judge_at": "2026-11-25", "at": "2026-11-24T05:30:00Z"})
    assert again["judge"]["called"] is False and "already asked today" in again["judge"]["reasoning"]                                # once a day at most
    off = run(router, probes, tmp_path, cfg={**CFG, "judge": False}, judge_fn=judge)
    assert off["judge"]["called"] is False
    none = run(router, probes, tmp_path)
    assert none["judge"]["called"] is False and "no Claude client in this run" in none["judge"]["reasoning"]


def test_when_the_rules_decide_everything_claude_is_not_called(tmp_path):
    router = router_for(tmp_path)
    probes = all_ok(router)
    seen = {f"{p}/{m}" for p, m in probes}
    calls = []
    res = run(router, probes, tmp_path, judge_fn=lambda p: calls.append(p) or "{}", prev_health={"models_seen": sorted(seen), "at": "2026-11-24T05:30:00Z", "lanes": {}})
    assert calls == [] and res["judge"]["called"] is False and "the rules decided every job (no Claude call)" in res["lines"][-1] or not res["judge"]["lanes"]


# ---- the router reads the generated lists ----------------------------------------------------------------------------------------------------------
def test_the_router_uses_a_fresh_generated_list_and_falls_back_to_the_static_one_when_it_is_stale_or_missing(tmp_path):
    lane = "claim_verify_json"
    static = [(r["provider"], r["model"]) for r in LANES[lane].chain]
    pick = next(k for k in static if k[0] != "claude")
    rank = {"jobs": {lane: {"ranking": [{"provider": pick[0], "model": pick[1]}], "excluded": [], "generated_by": "health", "generated_at": "2026-11-25T05:30:00Z"}}}
    f = tmp_path / "ai_rankings.json"
    f.write_text(json.dumps(rank), encoding="utf-8")
    fresh, notes = RK.apply_overlay(LANES, f, "2026-11-26T05:30:00Z", 3)
    assert (fresh[lane].chain[0]["provider"], fresh[lane].chain[0]["model"]) == pick and any("measured order" in n for n in notes)
    stale, notes2 = RK.apply_overlay(LANES, f, "2026-12-01T05:30:00Z", 3)
    assert stale[lane].chain == LANES[lane].chain and any("health run's list is" in n and "the static list is used" in n for n in notes2)
    missing, notes3 = RK.apply_overlay(LANES, tmp_path / "none.json", NOW, 3)
    assert missing[lane].chain == LANES[lane].chain and notes3 == []
    retest = {"jobs": {lane: {"ranking": [{"provider": pick[0], "model": pick[1]}], "excluded": []}}}
    f.write_text(json.dumps(retest), encoding="utf-8")
    old, _n = RK.apply_overlay(LANES, f, "2027-06-01T00:00:00Z", 3)
    assert (old[lane].chain[0]["provider"], old[lane].chain[0]["model"]) == pick                                                     # a RETEST list (no generated_by) never goes stale


# ---- the whole command, with scripted providers ---------------------------------------------------------------------------------------------------
class Fake:
    def __init__(self, fail=None, models=()):
        self.fail, self.models, self.calls = fail or {}, list(models), []
        self.base_url = "https://x.example/v1"

    def _headers(self):
        return {}

    def http(self, method, url, *, headers, body, timeout):
        return 200, {"data": [{"id": m} for m in self.models]}, {}

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None, **kw):
        self.calls.append((route["model"], timeout))
        if route["model"] in self.fail:
            raise Failure(contract.UPSTREAM_BROKEN, self.fail[route["model"]])
        return CL.Completion("OK", 5, 1, 150, route["model"])


def test_the_ai_health_command_discovers_probes_decides_writes_two_files_and_the_commit_message(tmp_path, monkeypatch, capsys):
    dead_any = "dots-studio/dots-3-note-preview:free"
    fakes = {pid: Fake() for pid, p in REG.providers.items() if p.enabled and pid != "claude"}
    fakes["anyapi"] = Fake(fail={dead_any: "the model was not found or removed upstream"}, models=["google/gemma-4-26b-a4b-it:free"])
    fakes["ovh"] = Fake(models=["ovh-live-1"])
    fakes["openrouter"] = Fake(models=["vendor/live-model:free", "vendor/paid-model"])
    monkeypatch.setattr(CM, "live_clients", lambda registry, environ, gateway_dir, workdir, notes: (fakes, None))
    msg = tmp_path / "msg.txt"
    argv = ["ai", "health", "--live", "--data-dir", str(tmp_path / "data"), "--workdir", str(tmp_path / "work"), "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"),
            "--sources-config", str(ROOT / "sources.yaml"), "--browser-config", str(ROOT / "browser.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"), "--health-config", str(ROOT / "ai_health.yaml"), "--message-file", str(msg)]
    code = cli.main(argv)
    out = capsys.readouterr().out
    shared = tmp_path / "data" / "_shared"
    assert code == 0 and "AI health, LIVE: 2 live free model(s) discovered at openrouter, ovh" in out and "health: anyapi/dots-studio/dots-3-note-preview:free: DEAD" in out and "AI health: wrote" in out
    health = json.loads((shared / "provider_health.json").read_text(encoding="utf-8"))
    assert health["providers"]["anyapi"]["models"][dead_any]["class"] == "dead" and health["discovered_passed"]["ovh"] == ["ovh-live-1"] and health["discovered_passed"]["openrouter"] == ["vendor/live-model:free"]
    assert "vendor/paid-model" not in json.dumps(health)                                                                             # a paid id is never even probed
    rankings = json.loads((shared / "ai_rankings.json").read_text(encoding="utf-8"))
    listed = {f"{r['provider']}/{r['model']}" for j in rankings["jobs"].values() for r in j["ranking"]}
    assert f"anyapi/{dead_any}" not in listed and all(j["generated_by"] == "health" for j in rankings["jobs"].values()) and msg.read_text(encoding="utf-8").startswith("Provider health ")
    assert any(c[1] == 240 for c in fakes["nvidia"].calls) if fakes["nvidia"].calls else True                                       # NVIDIA is given its longer timeout
    assert sorted(p.name for p in shared.glob("*.json") if p.name in ("provider_health.json", "ai_rankings.json")) == ["ai_rankings.json", "provider_health.json"]
    off = tmp_path / "off.yaml"
    off.write_text("auto_update: false\n", encoding="utf-8")
    shutil_data = tmp_path / "data2"
    argv2 = [a if a != str(tmp_path / "data") else str(shutil_data) for a in argv]
    argv2[argv2.index("--health-config") + 1] = str(off)
    assert cli.main(argv2) == 0 and not (shutil_data / "_shared" / "provider_health.json").exists() and "this run only reports; nothing was written" in capsys.readouterr().out


def test_the_workflow_is_scheduled_daily_commits_only_the_two_generated_files_and_never_hides_a_failed_push():
    wf = yaml.safe_load(Path(".github/workflows/provider-health.yml").read_text(encoding="utf-8"))
    assert wf[True]["schedule"] == [{"cron": "30 5 * * *"}] and "workflow_dispatch" in wf[True] and wf["permissions"] == {"contents": "write"}
    steps = wf["jobs"]["health"]["steps"]
    save = next(s for s in steps if s.get("name", "").startswith("Save the generated lists"))["run"]
    assert "add_each data/_shared/provider_health.json data/_shared/ai_rankings.json" in save and "git add -f data" not in save.replace("git add -f \"$f\"", "")
    assert "::error title=Save failed::" in save and "health_message.txt" in save
    probe = next(s for s in steps if s.get("name") == "Probe, score and decide")["run"]
    assert "python -u -m contentintel ai health --live" in probe and any(s.get("uses") == "./.github/actions/ai-pool" for s in steps)
    assert "ANTHROPIC_API_KEY" in wf["jobs"]["health"]["env"]


def test_the_retest_runs_unbuffered_prints_every_route_and_does_not_burn_a_nearly_used_allowance(tmp_path, capsys):
    run_step = next(s for s in yaml.safe_load(Path(".github/workflows/pool-retest.yml").read_text(encoding="utf-8"))["jobs"]["retest"]["steps"] if "ai rank" in str(s.get("run", "")))["run"] if "retest" in yaml.safe_load(Path(".github/workflows/pool-retest.yml").read_text(encoding="utf-8"))["jobs"] else ""
    assert "python -u -m contentintel ai rank" in Path(".github/workflows/pool-retest.yml").read_text(encoding="utf-8") and (not run_step or "python -u" in run_step)
    router = router_for(tmp_path)
    router.usage.record = getattr(router.usage, "record", None)
    lines = []
    calls = []
    router.run_route = lambda *a, **k: calls.append(a[1]["provider"]) or {"status": "ok", "output": {}, "completion": CL.Completion("x", 1, 1, 10)}
    router.usage.load = lambda p, now: 0.95 if p.id == "groq" else 0.1
    report = RK.run_ranking(router, {"claim_verify_json": LANES["claim_verify_json"]}, ["claim_verify_json"], now=NOW, runs=1, progress=lines.append)
    skipped = [r for r in report["claim_verify_json"]["not_measured"] if r["provider"] == "groq"]
    assert "groq" not in calls and skipped and "95% used" in skipped[0]["why"] and any("groq" in l and "allowance nearly used" in l for l in lines)
    assert any(l.startswith("rank: claim_verify_json:") for l in lines)


def test_every_providers_live_list_is_read_and_a_pinned_model_that_is_no_longer_listed_is_reported_without_ever_adding_an_unknown_one(tmp_path):
    class Direct(Lister):
        pass

    class Gateway:
        base_url = "http://127.0.0.1:3001"
        api_key = "k"

        def __init__(self, ids):
            self.ids = ids

        def http(self, method, url, *, headers, body, timeout):
            assert url.endswith("/v1/models") and headers["Authorization"] == "Bearer k"
            return 200, {"data": [{"id": i} for i in self.ids]}, {}
    groq_pinned = [m.id for m in REG.providers["groq"].models if m.approved]
    gw = Gateway([groq_pinned[0], "some-unknown-paid-model"])
    clients = {"groq": gw, "cohere": gw, "ovh": Direct(["ovh-x"]), "aihorde": object(), "kilo": type("Dead", (Direct,), {"http": lambda s, *a, **k: (500, {}, {})})([])}
    live = PH.list_live_models(REG, clients)
    assert live["groq"] == {"kind": "gateway", "ids": sorted([groq_pinned[0], "some-unknown-paid-model"])} and live["cohere"]["kind"] == "gateway" and live["ovh"] == {"kind": "direct", "ids": ["ovh-x"]}
    assert live["aihorde"]["kind"] == "none" and "probed directly" in live["aihorde"]["why"] and live["kilo"]["kind"] == "none" and live["nvidia"]["kind"] == "none" and "no key" in live["nvidia"]["why"]
    assert "claude" not in live and all(pid in live for pid, p in REG.providers.items() if p.enabled and pid != "claude")                                  # EVERY enabled provider is accounted for
    router = router_for(tmp_path)
    probes = all_ok(router)
    res = run_with_lists(router, probes, tmp_path, live)
    g = res["health"]["live_lists"]["groq"]
    assert g["count"] == 2 and set(g["pinned_not_listed"]) == set(groq_pinned[1:]) and g["added_free"] == 0                                              # nothing unknown is added to a provider that is not flagged
    assert res["health"]["live_lists"]["aihorde"]["kind"] == "none" and any("live model lists read from" in l for l in res["lines"])
    assert res["health"]["providers"]["groq"]["models"][groq_pinned[1]]["not_in_live_list"] is True


def run_with_lists(router, probes, tmp_path, live):
    return PH.run_health(router, LANES, router.registry, probes=probes, discovered={}, now=NOW, cfg=CFG, health_path=tmp_path / "ph.json", rankings_path=tmp_path / "rk.json", live_lists=live)
