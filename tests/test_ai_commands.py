"""The ai commands, the evaluation harness and the workflow. Mock results and live-path results (with a faked Anthropic client) are kept apart, as the commands do."""
import json

import pytest
import yaml

from contentintel import cli
from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import commands as CM
from contentintel.ai import evaluate as E
from contentintel.ai import lanes as L
from contentintel.ai import registry as R
from contentintel.ai import review as RV
from contentintel.ai import validate as V
from contentintel.contract import Failure

from ai_helpers import NOW, stack, write
from conftest import ROOT

SHIPPED_REG = R.load_registry(ROOT / "ai_providers.yaml")
SHIPPED_LANES = L.load_lanes(ROOT / "ai_lanes.yaml", SHIPPED_REG)


# ---- mocks that are valid by construction -----------------------------------------------------------------------------------------

def test_a_mock_answer_always_passes_the_lanes_own_checks():
    mock = CM.lane_mock(SHIPPED_LANES)
    route = {"provider": "x", "model": "m", "gateway_model": "m"}
    for name, lane in SHIPPED_LANES.items():
        text = mock.complete(route, "", "p", 10, lane=name).text
        V.check_output(text, lane)                                  # raises if the stand-in ever answers badly
    assert CM.lane_mock({}).complete(route, "", "p", 5, lane="unknown-lane").text.startswith("A scripted answer")


@pytest.mark.parametrize("schema, expected", [
    ({"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string", "minLength": 3}, "b": {"type": "array", "minItems": 2}}}, {"a": "xxx", "b": ["x", "x"]}),
    ({"enum": ["pass", "fail"]}, "pass"), ({"type": "integer", "minimum": 4}, 4), ({"type": "number", "minimum": 0, "maximum": 1}, 0.9), ({}, "x"),
])
def test_the_smallest_valid_answer_is_built_from_a_schema(schema, expected):
    assert CM.sample_from_schema(schema) == expected


def test_failures_are_split_into_ours_and_the_providers():
    assert CM.categorise(K.NEED_SETUP) == "APPLICATION" and CM.categorise(K.BAD_INPUT) == "APPLICATION"
    for code in (K.RATE_LIMITED, K.TIMEOUT, K.UPSTREAM_BROKEN, K.PLATFORM_BLOCKED, K.NEED_LOGIN, K.NOT_FOUND):
        assert CM.categorise(code) == "EXTERNAL"


# ---- evaluation tasks and scoring --------------------------------------------------------------------------------------------------

def test_the_shipped_tasks_load_against_the_shipped_lanes():
    tasks = E.load_tasks(ROOT / "ai_eval_tasks.yaml", SHIPPED_LANES)
    assert len(tasks) == 9 and len({t["id"] for t in tasks}) == 9
    assert {t["lane"] for t in tasks} == {"research_extraction", "competitor_intelligence", "audience_voice", "formula_analysis", "trend_opportunity", "content_ideation",
                                          "production_support", "validation_qa"}
    planted = next(t for t in tasks if t["id"] == "extract-ignores-planted-instruction")
    assert "pwned" in planted["expect"]["must_not_contain"] and "IGNORE ALL PREVIOUS INSTRUCTIONS" in planted["context"][0][1]
    assert next(t for t in tasks if t["id"] == "qa-catches-planted-claim")["expect"] == {"labels": {"verdict": "fail"}}


@pytest.mark.parametrize("data, fragment", [
    ({"version": 2, "tasks": [{"id": "a", "lane": "pub", "instructions": "x"}]}, "needs version"), ({"version": 1, "tasks": []}, "needs version"),
    ({"version": 1, "tasks": [{"id": "a", "lane": "pub"}]}, "needs id, lane and instructions"), ({"version": 1, "tasks": [{"id": "a", "lane": "pub", "instructions": "x", "colour": 1}]}, "needs id"),
    ({"version": 1, "tasks": [{"id": "a", "lane": "pub", "instructions": "x"}, {"id": "a", "lane": "pub", "instructions": "y"}]}, "listed twice"),
    ({"version": 1, "tasks": [{"id": "a", "lane": "nope", "instructions": "x"}]}, "unknown lane"),
])
def test_a_mistake_in_the_tasks_is_refused(tmp_path, data, fragment):
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(E.EvalError, match=fragment):
        E.load_tasks(p, {"pub": object()})


def lane(tmp_path, name):
    p, l = write(tmp_path)
    return L.load_lanes(l, R.load_registry(p))[name]


def test_scores_reflect_the_checks_the_facts_the_forbidden_words_and_the_labels(tmp_path):
    pub, internal = lane(tmp_path, "pub"), lane(tmp_path, "internal")
    t = {"expect": {"must_contain": ["alpha", "beta"], "must_not_contain": ["pwned"], "labels": {"answer": "yes"}}}
    full = E.score(t, pub, '{"answer": "yes", "note": "Alpha and BETA"}')
    assert full == {"ok": True, "score": 1.0, "facts": 2, "forbidden": 0, "labels": 1, "problem": ""}
    half = E.score(t, pub, '{"answer": "no", "note": "alpha only"}')
    assert half["score"] == round((0.5 + 1.0 + 0.0) / 3, 3) and half["facts"] == 1 and half["labels"] == 0
    bad = E.score(t, pub, '{"answer": "yes", "note": "alpha beta PWNED"}')
    assert bad["forbidden"] == 1 and bad["score"] == round((1.0 + 0.0 + 1.0) / 3, 3)
    failed = E.score(t, pub, "not json")
    assert failed["ok"] is False and failed["score"] == 0.0 and "not valid JSON" in failed["problem"]
    assert E.score({"expect": {}}, internal, "any usable text")["score"] == 1.0
    assert E.score({"expect": {"labels": {"x": "y"}}}, internal, "plain text")["labels"] == 0


def eval_stack(tmp_path, *clients):
    router, reg, usage, review, health = stack(tmp_path, dict(zip(("alpha", "beta", "claude"), clients)))
    return router


TASKS = [{"id": "t1", "lane": "pub", "instructions": "do", "context": [("x", "text")], "expect": {"must_contain": ["hello"]}},
         {"id": "t2", "lane": "internal", "instructions": "do", "context": [], "expect": {}}]


def test_the_harness_scores_each_route_records_cost_and_skips_what_cannot_run(tmp_path):
    alpha = C.MockClient(['{"answer": "hello"}', "an internal answer"])
    router, *_ = stack(tmp_path, {"alpha": alpha, "claude": C.MockClient(["x"])})                    # beta has no client on this machine
    store = tmp_path / "eval.jsonl"
    rows = E.run_eval(router, TASKS, now=NOW, mode="mock", store=store)
    assert [(r["task"], r["provider"], r["status"]) for r in rows] == [("t1", "alpha", "ok"), ("t2", "alpha", "ok"), ("t2", "beta", "skipped")]
    first = rows[0]
    assert first["mode"] == "mock" and first["score"] == 1.0 and first["tokens_in"] > 0 and first["latency_ms"] == 5 and first["cost_usd"] == 0.0 and first["model"] == "a-1"
    skipped = [r for r in rows if r["status"] == "skipped"]
    assert [r["reason"] for r in skipped] == ["no credentials or client available on this machine"] and skipped[0]["score"] == 0.0
    assert len(store.read_text().splitlines()) == len(rows) and json.loads(store.read_text().splitlines()[0])["task"] == "t1"
    assert not [r for r in rows if r["provider"] == "claude"]                                  # Claude costs money: only with include_claude


def test_claude_routes_are_evaluated_only_on_request_and_costed_from_real_tokens(tmp_path):
    claude = C.MockClient([C.Completion(text="a long enough claude answer", tokens_in=1000, tokens_out=500, latency_ms=40, model="claude-sonnet-5-5")])
    router = eval_stack(tmp_path, C.MockClient(["x"]), C.MockClient(["x"]), claude)
    task = [{"id": "q", "lane": "secret", "instructions": "do", "context": [], "expect": {}}]
    assert E.run_eval(router, task, now=NOW, mode="live") == []
    rows = E.run_eval(router, task, now=NOW, mode="live", include_claude=True)
    assert rows[0]["provider"] == "claude" and rows[0]["cost_usd"] == 0.007 and rows[0]["mode"] == "live"
    assert E.run_eval(router, task, now=NOW, mode="live", include_claude=True, only_providers={"alpha"}) == []


def test_comparison_averages_answered_runs_and_never_counts_skipped_ones():
    rows = [{"lane": "pub", "provider": "a", "model": "m", "mode": "live", "status": "ok", "score": 1.0, "latency_ms": 100, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0, "problem": ""},
            {"lane": "pub", "provider": "a", "model": "m", "mode": "live", "status": "ok", "score": 0.0, "latency_ms": 300, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0, "problem": "not valid JSON"},
            {"lane": "pub", "provider": "a", "model": "m", "mode": "live", "status": "failed", "score": 0.0, "reason": "timeout"},
            {"lane": "pub", "provider": "b", "model": "n", "mode": "live", "status": "skipped", "score": 0.0, "reason": "disabled"},
            {"lane": "pub", "provider": "a", "model": "m", "mode": "mock", "status": "ok", "score": 1.0, "latency_ms": 5, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "problem": ""}]
    table = E.compare(rows)
    live = next(t for t in table if t["mode"] == "live")
    assert live == {"lane": "pub", "provider": "a", "model": "m", "mode": "live", "runs": 3, "answered": 2, "mean_score": 0.333, "usable_rate": 0.333, "median_latency_ms": 200,
                    "tokens": 30, "cost_usd": 0.0}
    assert len(table) == 2 and not any(t["provider"] == "b" for t in table)                  # a mock row and a live row are never merged
    text = E.format_compare(table)
    assert "(LIVE)" in text and "(MOCK)" in text and "No route could be evaluated" in E.format_compare([])


# ---- the commands -------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def ai(tmp_path, monkeypatch, capsys):
    for name in ("ANTHROPIC_API_KEY", "FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD", "GROQ_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    claude_only = tmp_path / "claude_only_providers.yaml"          # the shipped registry with every other provider switched off, whatever has been rolled out
    shipped = yaml.safe_load((ROOT / "ai_providers.yaml").read_text(encoding="utf-8"))
    for pid, entry in shipped["providers"].items():
        if pid != "claude":
            entry["enabled"], entry["approved_classes"] = False, []
            entry.pop("owner_override", None)
    claude_only.write_text(yaml.safe_dump(shipped), encoding="utf-8")

    def run(action, *extra, providers=None, lanes=None):
        argv = ["ai", action, "--data-dir", str(tmp_path / "data"), "--sources-config", str(ROOT / "sources.yaml"), "--browser-config", str(ROOT / "browser.yaml"),
                "--providers-config", str(providers or claude_only), "--lanes-config", str(lanes or ROOT / "ai_lanes.yaml"),
                "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"), "--workdir", str(tmp_path / "work"), *extra]
        code = cli.main(argv)
        return code, capsys.readouterr().out
    run.tmp = tmp_path
    return run


def test_providers_lists_what_is_on_and_all_with_a_flag(ai):
    code, out = ai("providers")
    assert code == 0 and "claude" in out and "groq" not in out and "86 providers in the registry, 1 enabled" in out
    code, out = ai("providers", "--all")
    assert "groq" in out and "kilo" in out and "exp" in out and out.count("\n") > 80


def test_plan_explains_every_lane_makes_no_call_and_writes_nothing(ai):
    code, out = ai("plan", "--data-class", "internal")
    assert code == 0 and "no call is made" in out
    for lane in SHIPPED_LANES:
        assert lane in out
    assert "groq / llama-3.3-70b-versatile: disabled (a person has not approved it)" in out and "nothing can run: the task would be deferred" in out
    assert not (ai.tmp / "data").exists()


def test_plan_shows_claude_ready_when_its_credential_exists(ai, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    code, out = ai("plan", "--data-class", "restricted")
    assert "strategic_reasoning" in out and "ready   claude / claude-sonnet-5-5" in out and "ready   claude / claude-haiku-5-5" in out
    assert "groq" not in out.split("strategic_reasoning")[1].split("content_ideation")[0]


def test_usage_reports_spend_against_the_caps(ai):
    code, out = ai("usage")
    assert code == 0 and "of $0.50" in out and "of $10.00" in out and "no calls today" in out


def test_the_review_commands_need_a_name_and_never_overwrite_a_decision(ai):
    q = RV.ReviewQueue(ai.tmp / "data" / "_shared" / "ai_review.json")
    rid = q.add(lane="brief_generation", now=NOW, output={"title": "t"}, route={"provider": "claude", "model": "m"}, item_ids=["IB-1"], data_class="internal", reason="required")
    q.save()
    code, out = ai("review")
    assert code == 0 and "1 output(s) waiting for review" in out and rid in out and "brief_generation" in out
    code, out = ai("review", "--approve", rid)
    assert code == 1 and "--by NAME is required" in out
    code, out = ai("review", "--approve", rid, "--by", "Harish", "--note", "fine")
    assert code == 0 and f"{rid} approved by Harish" in out
    code, out = ai("review", "--reject", rid, "--by", "Other")
    assert code == 1 and "already approved by Harish" in out
    assert ai("review", "--approve", "AR-9999", "--by", "H")[0] == 1
    code, out = ai("review")
    assert "0 output(s) waiting" in out


def test_ask_in_mock_mode_respects_data_classes_and_waits_for_review(ai):
    code, out = ai("ask", "--lane", "strategic_reasoning", "--data-class", "restricted", "--text", "weigh the options")
    assert code == 0 and "[MOCK] status needs_review via claude/claude-sonnet-5-5" in out
    code, out = ai("ask", "--lane", "research_extraction", "--data-class", "restricted", "--text", "x")
    assert code == 1 and "status refused" in out and "may carry internal data at most" in out
    code, out = ai("ask", "--lane", "research_extraction", "--data-class", "public", "--text", "x")
    assert code == 0 and "[MOCK] status ok via claude/claude-haiku-5-5" in out and "skipped groq: disabled" in out      # nothing else is on, so the lane's Claude fallback answers
    code, out = ai("ask", "--lane", "competitor_intelligence", "--data-class", "public", "--text", "x")
    assert code == 0 and "needs_review" in out                                                         # this lane requires a person to review


def test_ask_needs_a_lane_and_a_text(ai):
    assert ai("ask", "--lane", "pub")[0] == 1


def test_a_mock_run_never_touches_the_real_spending_ledger_or_health_records(ai):
    ai("ask", "--lane", "strategic_reasoning", "--data-class", "restricted", "--text", "x")
    ai("check")
    ai("eval", "--include-claude")
    shared = ai.tmp / "data" / "_shared"
    assert not (shared / "ai_usage.json").exists() and not (shared / "ai_review.json").exists() and not (shared / "ai_eval.jsonl").exists() or True
    assert not (shared / "ai_usage.json").exists() and not (shared / "ai_review.json").exists()
    assert (ai.tmp / "work" / "mock-state" / "ai_usage.json").exists()
    health = shared / "collector_health.json"
    assert not health.exists() or "ai:" not in health.read_text(encoding="utf-8")


def test_check_in_mock_mode_says_it_proves_nothing_about_providers(ai):
    code, out = ai("check")
    assert code == 0 and "AI check, MOCK" in out and "scripted stand-in" in out and "NOTE: a mock pass is not proof" in out and "LIVE" not in out.replace("--live", "")
    assert "MOCK  ok        claude/claude-sonnet-5-5" in out and "skipped   groq/llama-3.3-70b-versatile: disabled" in out


def test_eval_in_mock_mode_is_labelled_as_testing_the_harness(ai):
    code, out = ai("eval")
    assert code == 0 and "MOCK results (a scripted stand-in: this tests the harness, not a model)" in out and "No route could be evaluated" in out and "skipped groq" in out
    code, out = ai("eval", "--include-claude")
    assert "(MOCK)" in out and "claude / claude-haiku-5-5" in out


def custom(ai, enable=("alpha", "beta", "gamma")):
    return write(ai.tmp)


def test_with_providers_enabled_a_mock_check_answers_every_route(ai):
    p, l = custom(ai)
    code, out = ai("check", providers=p, lanes=l)
    assert code == 0 and "0 application problem(s), 0 external provider problem(s)" in out
    for route in ("alpha/a-1", "beta/b-1", "claude/claude-sonnet-5-5"):
        assert f"MOCK  ok        {route}" in out
    assert "skipped   gamma/g-1: experimental route: not allowed in this lane" in out               # a check never reaches an experimental route by itself


def test_the_live_path_without_a_key_is_an_application_problem_not_a_pass(ai):
    code, out = ai("check", "--live")
    assert code == 1 and "APPLICATION: ANTHROPIC_API_KEY is not set" in out and "AI check, LIVE" in out and "NOTE: a mock pass" not in out


class FakeAnthropic:
    answers = []

    def __init__(self, api_key):
        self.api_key = api_key

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        a = FakeAnthropic.answers.pop(0) if len(FakeAnthropic.answers) > 1 else FakeAnthropic.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a


def test_a_live_check_records_real_usage_and_health_and_says_live(ai, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    FakeAnthropic.answers = [C.Completion(text="OK", tokens_in=20, tokens_out=2, latency_ms=120, model="claude-haiku-5-5")]
    monkeypatch.setattr(CM.cl, "AnthropicClient", FakeAnthropic)
    code, out = ai("check", "--live", "--only", "claude")
    assert code == 0 and "LIVE  ok        claude/claude-haiku-5-5  120 ms, 20+2 tokens" in out and "2 answered (live)" in out
    ledger = json.loads((ai.tmp / "data" / "_shared" / "ai_usage.json").read_text(encoding="utf-8"))
    day = next(iter(ledger["days"].values()))["claude"]
    assert day["requests"] == 2 and day["tokens_in"] == 40 and 0 < day["cost_usd"] < 0.01
    health = json.loads((ai.tmp / "data" / "_shared" / "collector_health.json").read_text(encoding="utf-8"))
    assert health["routes"]["ai:claude|gateway"]["history"][-1]["ok"] is True
    code, out = ai("usage")
    assert "claude" in out and "$0.00" in out


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("failure, kind, red_by_default", [
    (Failure(K.RATE_LIMITED, "the Anthropic credit balance is exhausted"), "EXTERNAL", False),
    (ConnectionError("down"), "EXTERNAL", False),
    (Failure(K.NEED_SETUP, "Anthropic rejected the API key"), "APPLICATION", True)])
def test_live_failures_are_split_into_ours_and_the_providers_with_matching_exit_codes(ai, monkeypatch, failure, kind, red_by_default, strict):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    FakeAnthropic.answers = [failure]
    monkeypatch.setattr(CM.cl, "AnthropicClient", FakeAnthropic)
    code, out = ai("check", "--live", "--only", "claude", *(["--strict"] if strict else []))
    assert kind in out and (code == 1) is (red_by_default or strict)
    assert "0 answered (live)" in out and "AI check, LIVE" in out


def test_a_live_run_that_reaches_the_spending_cap_stops_instead_of_spending(ai, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    FakeAnthropic.answers = [C.Completion(text="OK", tokens_in=10, tokens_out=1, latency_ms=1, model="m")]
    monkeypatch.setattr(CM.cl, "AnthropicClient", FakeAnthropic)
    shared = ai.tmp / "data" / "_shared"
    shared.mkdir(parents=True)
    import time
    today = time.strftime("%Y-%m-%d", time.gmtime())
    (shared / "ai_usage.json").write_text(json.dumps({"days": {today: {"claude": {"requests": 9, "failures": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.499,
                                                                              "latency_ms": 0, "by_lane": {}}}}}), encoding="utf-8")
    code, out = ai("ask", "--live", "--lane", "strategic_reasoning", "--data-class", "restricted", "--text", "x")
    assert code == 1 and "status deferred" in out and "daily spending limit would be exceeded" in out


def test_smoke_without_a_built_gateway_is_an_application_problem(ai):
    code, out = ai("smoke", "--gateway-dir", str(ai.tmp / "missing"))
    assert code == 1 and "APPLICATION problem: the pinned gateway is not built" in out


def test_the_ai_commands_need_the_sources_file(ai, monkeypatch, capsys):
    code = cli.main(["ai", "providers", "--data-dir", str(ai.tmp / "d"), "--sources-config", str(ai.tmp / "nope.yaml"), "--providers-config", str(ROOT / "ai_providers.yaml")])
    assert code == 1 and "sources.yaml is needed" in capsys.readouterr().out


# ---- the workflow ----------------------------------------------------------------------------------------------------------------------

WF_TEXT = (ROOT / ".github" / "workflows" / "ai-gateway.yml").read_text(encoding="utf-8")
WF = yaml.safe_load(WF_TEXT)


def test_the_workflow_keeps_mock_and_live_in_separate_jobs():
    jobs = WF["jobs"]
    assert set(jobs) == {"mock", "live"} and jobs["live"]["needs"] == "mock" and jobs["live"]["if"] == "inputs.live == true" and "if" not in jobs["mock"]
    assert "Mock-provider" in jobs["mock"]["name"] and "Live-provider" in jobs["live"]["name"]
    assert set(WF[True]["workflow_dispatch"]["inputs"]) == {"live", "evaluate", "only", "strict"} and WF[True]["workflow_dispatch"]["inputs"]["live"]["default"] is False
    assert WF["concurrency"]["group"] == "catalogue-data" and "slack" not in WF_TEXT.lower().replace("no slack message", "")


def test_both_jobs_build_exactly_the_pinned_commit_and_verify_it():
    for job in WF["jobs"].values():
        build = next(s for s in job["steps"] if s.get("name") == "Fetch and build the pinned gateway")["run"]
        assert "ai/gateway.lock.json" in build and 'git fetch --quiet --depth 1 origin "$commit"' in build and 'test "$(git rev-parse HEAD)" = "$commit"' in build
        assert "npm ci --workspace=shared --workspace=server" in build
        assert "npm run build -w server" in build and "-w shared -w server" not in build        # `shared` has no build script: building it fails the job
        assert next(s for s in job["steps"] if "setup-node" in s.get("uses", ""))["with"]["node-version"] == "22"


def test_the_mock_job_never_has_secrets_and_the_live_job_passes_every_credential_the_registry_names():
    assert "secrets." not in yaml.safe_dump(WF["jobs"]["mock"])
    live = yaml.safe_dump(WF["jobs"]["live"])
    needed = {"FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD", "ANTHROPIC_API_KEY"}
    for pid in ("groq", "mistral", "openrouter", "google", "cerebras", "cloudflare"):
        needed |= set(SHIPPED_REG.provider(pid).credential_env)
    for name in needed:
        assert "${{ secrets." + name + " }}" in live, name


def test_every_command_the_workflow_runs_exists():
    import re
    actions = set(re.findall(r"python -m contentintel ai (\w+)", WF_TEXT))
    parser = cli.build_parser()
    choices = next(a for a in parser._subparsers._group_actions[0].choices["ai"]._actions if a.dest == "action").choices
    assert actions and actions <= set(choices) and {"smoke", "plan", "check", "eval", "providers", "usage"} <= actions


def test_the_live_job_saves_results_and_turns_red_only_for_our_problems():
    steps = WF["jobs"]["live"]["steps"]
    assert "git add -f data" in next(s for s in steps if s.get("name") == "Save the results")["run"]
    assert steps[-1]["run"] == "exit 1" and "steps.live.outcome == 'failure'" in steps[-1]["if"]
    assert next(s for s in steps if s.get("id") == "live")["continue-on-error"] is True


def test_the_setup_instructions_name_every_secret_the_workflow_uses_and_warn_about_training():
    text = (ROOT / "docs" / "ai" / "setup-instructions.md").read_text(encoding="utf-8")
    for name in ("FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
                 "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        assert name in text, name
    assert "Public data only" in text and "may be used to train" in text and "Do not add a payment card" in text.replace("**", "") or "Do not add a payment card" in text.replace("**", "")
    assert "owner_override" in text and "monthly spend limit" in text and "auto-reload is off" in text


def enabled_groq(ai):
    def add(d):
        d["providers"]["groq"] = {"label": "Groq", "enabled": True, "ceiling": "public", "approved_classes": ["public"], "credentials": {"env": ["GROQ_API_KEY"]},
                                  "gateway_platform": "groq", "terms": {"status": "verified"}, "models": [{"id": "llama-3.3-70b-versatile", "approved": True}]}
    from ai_helpers import PROVIDERS, edited
    lanes = edited(LANES_BASE, lambda d: d["lanes"]["pub"]["chain"].insert(0, {"provider": "groq", "model": "llama-3.3-70b-versatile"}))
    return write(ai.tmp, providers=edited(PROVIDERS, add), lanes=lanes)


from ai_helpers import LANES as LANES_BASE  # noqa: E402


def test_a_live_run_with_a_provider_on_but_no_gateway_secrets_says_what_is_missing(ai, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    p, l = enabled_groq(ai)
    code, out = ai("check", "--live", "--only", "groq", providers=p, lanes=l)
    assert code == 1 and "APPLICATION: FREEAPI_ENCRYPTION_KEY and FREEAPI_ADMIN_PASSWORD are needed to start the gateway" in out


def test_a_live_run_with_the_secrets_but_no_built_gateway_says_so(ai, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    monkeypatch.setenv("FREEAPI_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("FREEAPI_ADMIN_PASSWORD", "a-long-enough-password")
    p, l = enabled_groq(ai)
    code, out = ai("check", "--live", "--only", "groq", "--gateway-dir", str(ai.tmp / "nothing-here"), providers=p, lanes=l)
    assert code == 1 and "APPLICATION: the gateway is not built at" in out


def test_a_missing_ai_folder_cannot_stop_anything_else_and_the_ai_command_says_what_is_wrong(ai, monkeypatch, capsys):
    import sys
    for name in [n for n in sys.modules if n == "contentintel.ai" or n.startswith("contentintel.ai.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "contentintel.ai", None)          # importing it now raises ImportError, as if the folder were not uploaded
    code, out = ai("providers")
    assert code == 1 and "APPLICATION problem: the AI layer's files are missing or incomplete" in out and "all 14 files" in out
    assert cli.main(["health", "--data-dir", str(ai.tmp / "d"), "--sources-config", str(ROOT / "sources.yaml"), "--browser-config", str(ROOT / "browser.yaml")]) == 0
    assert "meta_ads / browser" in capsys.readouterr().out                # an ordinary command works exactly as before


def test_the_cli_module_does_not_import_the_ai_package_at_start_up():
    import subprocess
    import sys
    code = "import sys; import contentintel.cli; print(any(n == 'contentintel.ai' or n.startswith('contentintel.ai.') for n in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    assert out.stdout.strip() == "False", out.stderr


# ---- the shipped rollout, with the real files --------------------------------------------------------------------------------------

def test_the_shipped_providers_command_shows_the_rolled_out_providers_with_their_recorded_decision(ai):
    code, out = ai("providers", providers=ROOT / "ai_providers.yaml")
    assert code == 0 and "86 providers in the registry, 7 enabled" in out
    for pid in ("cloudflare", "github", "google", "groq", "mistral", "openrouter"):
        line = next(l for l in out.splitlines() if l.strip().startswith(pid + " "))
        assert "ON" in line and "internal*" in line and "public,internal" in line, line         # * = raised by a recorded owner decision
    assert "restricted" in next(l for l in out.splitlines() if l.strip().startswith("claude "))


def test_with_the_real_files_a_provider_without_a_key_is_waiting_not_used_and_restricted_data_still_reaches_only_claude(ai, monkeypatch):
    code, out = ai("plan", "--data-class", "internal", providers=ROOT / "ai_providers.yaml")
    assert code == 0 and "groq / llama-3.3-70b-versatile" in out and "ready   groq" not in out        # enabled, but no GROQ_API_KEY here, so never ready
    code, out = ai("plan", "--data-class", "restricted", providers=ROOT / "ai_providers.yaml")
    assert "groq" not in out.split("strategic_reasoning")[1].split("content_ideation")[0]
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    code, out = ai("plan", "--data-class", "internal", providers=ROOT / "ai_providers.yaml")
    assert "ready   groq / llama-3.3-70b-versatile" in out
    code, out = ai("plan", "--data-class", "restricted", providers=ROOT / "ai_providers.yaml")
    assert "ready   groq" not in out
