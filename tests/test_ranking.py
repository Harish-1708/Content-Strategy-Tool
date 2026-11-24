"""The pool retest: scoring, ranking, the overlay that reorders each job's list, the command and the workflow."""
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from contentintel.ai import commands as aic
from contentintel.ai import lanes as L
from contentintel.ai import ranking as RK
from contentintel.ai import registry as R

AT = "2026-11-17T12:00:00Z"
PERFECT = {"story_extract_json": {"facts": [{"fact": "Probe Brand was founded in 2017.", "quote": "The company was founded in 2017 in a small studio in Portland.", "category": "origin"},
                                           {"fact": "Probe Brand uses organic cotton.", "quote": "Its overalls are cut from heavy organic cotton and sold directly online.", "category": "product"}]},
           "story_review_json": {"verdicts": [{"i": 0, "supported": True, "about_brand": True}, {"i": 1, "supported": True, "about_brand": True}, {"i": 2, "supported": False, "about_brand": True}]},
           "conflict_check_json": {"relations": [{"i": i, "relation": lab} for i, (_a, _b, lab) in enumerate(RK.PAIRS)]},
           "foundation_json": {"text": "Probe Brand makes soft overalls for adults. " * 12, "needs_input": ["named competitors"], "claims_used": []},
           "claim_verify_json": [{"queries": ["Probe Brand 2026 Probe Innovation Award"]}, {"verdict": "supports", "quote": "Probe Brand won the 2026 Probe Innovation Award, the awarding body announced on 3 March 2026.", "about_brand": True}],
           "brief_json": {"title": "Organic overalls", "concepts": [{"name": "Soft"}]}}
for _lane in ("competitor_ideas_json", "creator_chain_json", "listen_learn_json", "voice_creative_json", "seo_lens_json", "storyboard_json"):
    PERFECT[_lane] = {"text": "Probe Brand makes soft overalls for adults and the audience likes the deep pockets. " * 4, "needs_input": ["named competitors"], "claims_used": []}


def test_each_probe_scores_a_perfect_answer_one_and_a_wrong_or_invented_answer_low():
    suite = RK.probes()
    assert set(suite) == set(RK.JOBS)
    for job, job_probes in suite.items():
        perfect = PERFECT[job] if isinstance(PERFECT[job], list) else [PERFECT[job]]
        assert len(perfect) == len(job_probes), job
        for (system, user, fn), good in zip(job_probes, perfect):
            assert fn(good) == 1.0 and fn({}) == 0.0 and fn(None) == 0.0 and fn("junk") == 0.0, job
    extract = suite["story_extract_json"][0][2]
    invented = {"facts": [{"fact": "Probe Brand won an award.", "quote": "Probe Brand won a national design award in 2020 for its overalls.", "category": "award"}]}
    assert extract(invented) == 0.0                                                                           # a quotation that is not in the page is worth nothing
    assert extract({"facts": PERFECT["story_extract_json"]["facts"][:1] + invented["facts"]}) < RK.PASS_AT      # one real, one invented: below the pass mark
    review = suite["story_review_json"][0][2]
    assert review({"verdicts": [{"i": 0, "supported": True, "about_brand": True}] * 3}) < RK.PASS_AT           # approving the fabricated fact fails
    assert suite["conflict_check_json"][0][2]({"relations": [{"i": i, "relation": "unrelated"} for i in range(6)]}) < RK.PASS_AT
    foundation = suite["foundation_json"][0][2]
    assert foundation({"text": "Probe Brand won a national design award. " * 12, "needs_input": []}) < 1.0 and foundation({"text": "short", "needs_input": []}) < RK.PASS_AT   # the blocked claim and a thin text cost points


def test_the_probes_use_the_projects_own_prompts():
    from contentintel import foundation as FD
    from contentintel import story_refresh as SR
    suite = RK.probes()
    assert suite["story_extract_json"][0][0] == SR.EXTRACT_SYSTEM and suite["story_review_json"][0][0] == SR.REVIEW_SYSTEM
    assert suite["conflict_check_json"][0][0] == SR.RELATION_SYSTEM and suite["foundation_json"][0][0] == FD.SYSTEM


class FakeRouter:
    """Routes with known behaviour: a answers perfectly and fast, b answers perfectly but slowly, c is right half the time, d always fails, e is skipped (no credential)."""

    def __init__(self):
        self.calls = []

    def _routes(self, lane):
        return [{"provider": p, "model": "m"} for p in ("c", "d", "b", "e", "a")]

    def run_route(self, lane_name, route, *, instructions, system="", now, ignore_rests=False, context=()):
        p = route["provider"]
        self.calls.append((lane_name, p))
        if p == "e":
            return {"status": "skipped", "reason": "no credential in the environment"}
        if p == "d":
            return {"status": "failed", "reason": "HTTP 500 upstream broke"}
        out = dict(PERFECT[lane_name])
        if p == "c" and sum(1 for c in self.calls if c[1] == "c") % 2 == 0:
            out = {}
        return {"status": "ok", "output": out, "completion": SimpleNamespace(latency_ms={"a": 300, "b": 4000, "c": 900}[p])}


def test_the_ranking_puts_the_best_provider_first_keeps_backups_in_order_and_leaves_out_one_that_cannot_do_the_job():
    router = FakeRouter()
    lanes = {n: SimpleNamespace(name=n) for n in RK.JOBS}
    report = RK.run_ranking(router, lanes, ["conflict_check_json"], now=AT, runs=2)
    job = report["conflict_check_json"]
    assert [r["provider"] for r in job["ranking"]] == ["a", "b", "c"]                                          # same score: the faster first; c passed only half
    assert [r["provider"] for r in job["excluded"]] == ["d"] and "HTTP 500" in job["excluded"][0]["failures"][0]
    assert [r["provider"] for r in job["not_measured"]] == ["e"] and job["not_measured"][0]["why"] == "no credential in the environment"
    assert job["ranking"][0]["passes"] == 2 and job["ranking"][2]["passes"] == 1 and job["routes_tried"] == 4
    assert len([c for c in router.calls if c[1] == "a"]) == 2                                                  # `runs` calls per probe and route
    only = RK.run_ranking(FakeRouter(), lanes, ["foundation_json"], now=AT, runs=1, only_providers={"b", "a"})
    assert [r["provider"] for r in only["foundation_json"]["ranking"]] == ["a", "b"]
    text = RK.format_report(report)
    assert "1. a/m: 2/2 probe runs passed" in text and "left out d/m" in text and "not measured e/m: no credential" in text


def lanes_from_file():
    registry = R.load_registry("ai_providers.yaml")
    return L.load_lanes("ai_lanes.yaml", registry)


def test_the_overlay_reorders_a_measured_jobs_chain_drops_the_failed_ones_keeps_the_rest_after_and_leaves_other_jobs_alone(tmp_path):
    lanes = lanes_from_file()
    seeded = [r["provider"] for r in lanes["story_extract_json"].chain]
    assert seeded[0] == "groq" and "RANKING NOT YET MEASURED" in lanes["story_extract_json"].description
    path = tmp_path / "ai_rankings.json"
    report = {"story_extract_json": {"ranking": [{"provider": "mistral", "model": "mistral-small-latest", "passes": 2, "runs": 2, "mean_score": 1.0, "median_ms": 500},
                                                 {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "passes": 2, "runs": 2, "mean_score": 0.9, "median_ms": 800}],
                                    "excluded": [{"provider": "groq", "model": "openai/gpt-oss-120b", "runs": 2, "failures": ["HTTP 500"]}], "not_measured": [], "routes_tried": 3}}
    RK.save(path, report, AT)
    out, notes = RK.apply_overlay(lanes, path)
    chain = [(r["provider"]) for r in out["story_extract_json"].chain]
    assert chain[:2] == ["mistral", "cloudflare"] and "groq" not in chain and chain[2:4] == ["zhipu", "openrouter"] and "anyapi" in chain[4:]          # measured best first; the failed one left out; unmeasured ones keep their seeded order after
    assert out["story_review_json"].chain == lanes["story_review_json"].chain and out["general_json"].chain == lanes["general_json"].chain
    assert len(notes) == 1 and "story_extract_json: measured order 2026-11-17: mistral > cloudflare; left out: groq" in notes[0]
    assert RK.apply_overlay(lanes, tmp_path / "missing.json") == (lanes, [])                                  # no measurement: the seeded order, no note
    path.write_text("{broken", encoding="utf-8")
    assert RK.apply_overlay(lanes, path)[1] == ["ai_rankings.json could not be read: the seeded orders are used"]
    path.write_text(json.dumps({"jobs": {"story_extract_json": {"ranking": [], "excluded": []}, "nonexistent": {"ranking": [{"provider": "x", "model": "y"}]}}}), encoding="utf-8")
    assert RK.apply_overlay(lanes, path) == (lanes, [])                                                       # an empty measurement and an unknown job change nothing


class Store:
    def __init__(self, d):
        self.data_dir = d


def rank_args(tmp_path, **kw):
    return SimpleNamespace(jobs=kw.get("jobs", "all"), runs=2, only="", apply=kw.get("apply", True), **{})


def test_the_rank_command_refuses_unknown_jobs_and_mock_mode_writes_only_with_apply_and_keeps_an_earlier_measured_order(tmp_path, capsys):
    router, lanes = FakeRouter(), {n: SimpleNamespace(name=n) for n in RK.JOBS}
    noop = SimpleNamespace(save=lambda *a, **k: None)
    usage = review = SimpleNamespace(save=lambda: None)
    store = Store(str(tmp_path))
    assert aic._rank(rank_args(tmp_path, jobs="nope"), lanes, router, usage, review, None, store, AT, "live", []) == 2 and "no such job: nope" in capsys.readouterr().out
    assert aic._rank(rank_args(tmp_path), lanes, router, usage, review, None, store, AT, "mock", []) == 2 and "needs live mode" in capsys.readouterr().out and not router.calls
    path = tmp_path / "_shared" / RK.RANKINGS_FILE
    assert aic._rank(rank_args(tmp_path, jobs="conflict_check_json", apply=False), lanes, router, usage, review, None, store, AT, "live", []) == 0 and not path.exists()
    assert aic._rank(rank_args(tmp_path, jobs="conflict_check_json"), lanes, FakeRouter(), usage, review, None, store, AT, "live", []) == 0
    first = json.loads(path.read_text(encoding="utf-8"))
    assert list(first["jobs"]) == ["conflict_check_json"] and first["jobs"]["conflict_check_json"]["ranking"][0]["provider"] == "a"
    assert aic._rank(rank_args(tmp_path, jobs="foundation_json"), lanes, FakeRouter(), usage, review, None, store, AT, "live", []) == 0
    both = json.loads(path.read_text(encoding="utf-8"))
    assert set(both["jobs"]) == {"conflict_check_json", "foundation_json"}                                    # measuring one job keeps the other's earlier measured order

    class Nothing(FakeRouter):
        def run_route(self, *a, **k):
            return {"status": "failed", "reason": "down"}
    before = path.read_text(encoding="utf-8")
    assert aic._rank(rank_args(tmp_path, jobs="story_review_json"), lanes, Nothing(), usage, review, None, store, AT, "live", []) == 0
    assert path.read_text(encoding="utf-8") == before                                                         # nothing passed: the earlier order is not touched
    assert noop is not None


def test_build_stack_in_live_mode_applies_the_measured_order_and_mock_mode_never_does(tmp_path):
    from contentintel.store import Store as RealStore
    RK.save(tmp_path / "data" / "_shared" / RK.RANKINGS_FILE, {"story_extract_json": {"ranking": [{"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "passes": 2, "runs": 2, "mean_score": 1.0, "median_ms": 1}],
                                                                                          "excluded": [], "not_measured": [], "routes_tried": 1}}, AT)
    args = SimpleNamespace(providers_config="ai_providers.yaml", lanes_config="ai_lanes.yaml", workdir=str(tmp_path / "w"))
    store = RealStore(tmp_path / "data")
    live = aic.build_stack(args, store, None, mode="live", clients={})[1]
    assert live["story_extract_json"].chain[0]["provider"] == "cloudflare"
    mock = aic.build_stack(args, store, None, mode="mock", clients={})[1]
    assert mock["story_extract_json"].chain[0]["provider"] == "groq"


def test_the_pool_retest_workflow_is_manual_commits_only_its_own_files_and_sets_the_pool_up():
    wf = yaml.safe_load(Path(".github/workflows/pool-retest.yml").read_text(encoding="utf-8"))
    assert list(wf[True]) == ["workflow_dispatch"] and wf["permissions"] == {"contents": "write"} and wf["concurrency"]["group"] == "catalogue-data"
    steps = wf["jobs"]["retest"]["steps"]
    assert any(s.get("uses") == "./.github/actions/ai-pool" for s in steps)
    run = next(s for s in steps if s.get("name") == "Measure each job's providers")
    assert "python -u -m contentintel ai rank" in run["run"] and "--apply" in run["run"] and "args=(--live " in run["run"]          # without --live the command would run as a scripted stand-in and measure nothing
    save = next(s for s in steps if s.get("name") == "Save the measured order")
    assert "add_each data/_shared/ai_rankings.json data/_shared/ai_usage.json" in save["run"] and "git add -A" not in save["run"] and save["if"] == "always() && inputs.apply"
    env = wf["jobs"]["retest"]["env"]
    assert env["FREEAPI_ENCRYPTION_KEY"] and env["SAMBANOVA_API_KEY"] and env["ANTHROPIC_API_KEY"]


NO_MODEL_JOBS = {("sessions-password-backup", "backup"): "logs in once, only when the cookie session cannot be proved: no language model", ("batch3-tools", "batch3"): "forecasting (TimesFM), the CLIP stock corpus and the MoneyPrinterTurbo status: no language model", ("concat-gate", "notice"): "Concat is dropped: the workflow only prints a notice: no language model", ("mpt-deploy", "deploy"): "deploys the MoneyPrinterTurbo host to Cloud Run: no language model", ("catalogue-admin", "admin"): "edits the catalogue only", ("stage-tools", "stage"): "transcription (Groq Whisper, Faster-Whisper) and the public-page bake-off call no language model", ("sessions", "sessions"): "logs a session in, no model", ("sheets", "sheets"): "writes the sheet, no model",
                 ("sync", "sync"): "reads the store, no model", ("ai-gateway", "mock"): "a scripted stand-in: no network, no providers", ("agent-reach-cloudrun", "cloudrun"): "deploys a service",
                 ("tests", "test"): "runs the tests", ("tests", "sabotage-updated"): "runs the tests", ("tests", "sabotage-full-observatory"): "runs the tests", ("tests", "sabotage-full-main"): "runs the tests"}


def test_every_workflow_job_that_runs_the_tool_sets_up_the_free_pool_and_so_will_every_future_one():
    """The rule (owner's, 2026-11-17): every workflow that uses AI uses FreeLLMAPI and OmniRoute. A job that runs `python -m contentintel` must have the shared pool step and the provider secrets, unless it is
    on the short list of jobs that never call a model (each with its reason). A NEW workflow fails this test until it has the step or is added to the list with a reason."""
    assert len(yaml.safe_load(Path(".github/actions/ai-pool/action.yml").read_text(encoding="utf-8"))["runs"]["steps"]) == 6
    seen_pool, skipped = [], []
    for path in sorted(Path(".github/workflows").glob("*.yml")):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job, body in (wf.get("jobs") or {}).items():
            steps = body.get("steps") or []
            runs_tool = "python -m contentintel" in yaml.safe_dump(steps)
            has_pool = any(s.get("uses") == "./.github/actions/ai-pool" for s in steps)
            if (path.stem, job) in NO_MODEL_JOBS:
                skipped.append((path.stem, job))
                continue
            if not runs_tool and not has_pool:
                continue
            assert has_pool, f"{path.name}:{job} runs the tool but has no free-pool step: add `uses: ./.github/actions/ai-pool` and the provider secrets, or list it in NO_MODEL_JOBS with a reason"
            env = body.get("env") or {}
            assert env.get("FREEAPI_ENCRYPTION_KEY") == "${{ secrets.FREEAPI_ENCRYPTION_KEY }}" and env.get("GROQ_API_KEY") and env.get("SAMBANOVA_API_KEY"), f"{path.name}:{job} lacks the provider secrets"
            seen_pool.append((path.stem, job))
    assert {"skills", "brief", "collect", "request", "research", "research-tools", "acceptance", "observatory", "pool-retest"} <= {w for w, _j in seen_pool} and ("ai-gateway", "live") in seen_pool
    assert len(skipped) == len(NO_MODEL_JOBS)                                                                        # the list names only jobs that exist
    action = yaml.safe_load(Path(".github/actions/ai-pool/action.yml").read_text(encoding="utf-8"))
    assert all(s.get("continue-on-error") is True for s in action["runs"]["steps"][:-1])                           # every setup step may fail without failing the job
    text = Path(".github/actions/ai-pool/action.yml").read_text(encoding="utf-8")
    assert "PINNED_VERSION" in text and "CONTENTINTEL_OMNIROUTE_DIR" in text and "tashfeenahmed/freellmapi" in text and "hashFiles('gateway-src/server/dist/index.js') == ''" in text
    skills = yaml.safe_load(Path(".github/workflows/skills.yml").read_text(encoding="utf-8"))["jobs"]["skills"]["steps"]
    pool = next(s for s in skills if s.get("uses") == "./.github/actions/ai-pool")
    assert "foundation" in pool["if"] and "refresh-story-bank" in pool["if"] and "reclassify-conflicts" in pool["if"] and "'verify'" not in pool["if"]          # not built for a plain verify
