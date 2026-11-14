"""The shared pool: every approved model in one list, available to every lane, every future stage and the website; lane caps raised; the Claude spending cap untouched."""
import pytest
import yaml

from contentintel import cli
from contentintel.ai import clients as C
from contentintel.ai import lanes as L
from contentintel.ai import registry as R
from contentintel.ai import review as RV
from contentintel.ai import router as RT
from contentintel.ai import usage as U
from contentintel.health import HealthLedger

from ai_helpers import NOW, PROVIDERS, LANES, edited, write
from conftest import ROOT
from stage1_helpers import registry as sources_registry

REG = R.load_registry(ROOT / "ai_providers.yaml")
LANE_MAP = L.load_lanes(ROOT / "ai_lanes.yaml", REG)
POOL = L.load_pool(ROOT / "ai_lanes.yaml", REG)
CLAUDE_LANES = {"strategic_reasoning", "brief_generation", "validation_qa"}


def keys(routes):
    return [(r["provider"], r["model"]) for r in routes]


# ---- the shipped pool ------------------------------------------------------------------------------------------------------------

def test_the_shipped_pool_holds_every_approved_model_of_every_enabled_free_provider_once_and_nothing_else():
    approved = [(p.id, m.id) for p in REG.providers.values() if p.enabled and p.id != "claude" for m in p.models if m.approved]
    assert sorted(keys(POOL)) == sorted(approved) and len(set(keys(POOL))) == len(POOL) == 27
    assert not [r for r in POOL if r["provider"] == "claude"]                                               # Claude leads or falls back: it is never part of the free pool
    per = {}
    for r in POOL:
        per[r["provider"]] = per.get(r["provider"], 0) + 1
    assert per == {"groq": 2, "mistral": 2, "openrouter": 3, "cloudflare": 1, "google": 1, "nvidia": 1, "ollama": 7, "cohere": 4, "zhipu": 3, "kilo": 3}      # the models the live check of 2026-11-14 proved gone are out; Kilo has its three candidates
    assert [r["provider"] for r in POOL][:10] == ["groq", "groq", "mistral", "mistral", "openrouter", "openrouter", "openrouter", "cloudflare", "google", "nvidia"]      # the proven providers first
    assert ("kilo", "poolside/laguna-m.1:free") in keys(POOL) and ("kilo", "nvidia/nemotron-3-super-120b-a12b:free") not in keys(POOL) and ("zhipu", "glm-4.6v-flash") in keys(POOL) and ("nvidia", "google/gemma-4-31b-it") in keys(POOL) and ("nvidia", "meta/llama-4-maverick-17b-128e-instruct") not in keys(POOL)


def test_every_lane_draws_on_the_pool_except_the_three_that_stay_with_claude():
    assert {n for n, lane in LANE_MAP.items() if not lane.use_pool} == CLAUDE_LANES
    for name in CLAUDE_LANES:
        assert [c["provider"] for c in LANE_MAP[name].chain] == ["claude"] and LANE_MAP[name].claude_leads
    assert len([n for n, lane in LANE_MAP.items() if lane.use_pool]) == 9


def test_a_lane_has_its_own_preferred_routes_first_then_the_pool_without_repeats_then_claude(tmp_path):
    reg = REG
    router = RT.Router(reg, LANE_MAP, U.UsageLedger("/nonexistent/u.json"), HealthLedger({}, sources_registry(tmp_path)), {}, RV.ReviewQueue("/nonexistent/r.json"), pool=POOL)
    routes = router._routes(LANE_MAP["trend_opportunity"])
    own = keys(LANE_MAP["trend_opportunity"].chain)
    assert keys(routes)[:len(own)] == own and len(set(keys(routes))) == len(routes) == len(POOL) + 1                # the lane's own routes are in the pool too: each appears once
    assert keys(routes)[-1][0] == "claude" and routes[0]["provider"] == "mistral" and routes[1]["model"] == "command-a-reasoning-08-2025"      # Claude is the fallback, last
    assert [r["provider"] for r in router._routes(LANE_MAP["strategic_reasoning"])] == ["claude"]                  # a Claude lane never draws on the pool
    only_claude_leads = router._routes(LANE_MAP["validation_qa"])
    assert keys(only_claude_leads) == [("claude", "claude-haiku-5-5")]


def test_a_new_lane_with_no_routes_of_its_own_still_works_from_the_pool_alone_which_is_how_future_stages_get_every_model(tmp_path):
    lanes = edited(LANES, lambda d: d["lanes"].update(future_stage={"max_data_class": "internal", "chain": [], "output": {"format": "text", "min_chars": 1}, "max_tokens": 50}))
    p, l = write(tmp_path, PROVIDERS, lanes)
    reg = R.load_registry(p)
    lane_map = L.load_lanes(l, reg)
    assert lane_map["future_stage"].chain == () and lane_map["future_stage"].use_pool is True
    pool = [{"provider": "beta", "model": "b-1", "gateway_model": "b-1"}, {"provider": "alpha", "model": "a-1", "gateway_model": "a-1"}]
    beta = C.MockClient(["an answer"])
    router = RT.Router(reg, lane_map, U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), {"beta": beta, "alpha": C.MockClient(["x"])}, RV.ReviewQueue(tmp_path / "r.json"), pool=pool)
    res = router.run("future_stage", data_class="internal", instructions="go", now=NOW)
    assert res["status"] == "ok" and res["route"] == {"provider": "beta", "model": "b-1"}
    rows = router.explain("future_stage", "internal", NOW)
    assert [(r["provider"], r["source"]) for r in rows] == [("beta", "pool"), ("alpha", "pool")]


def test_the_pool_is_filtered_by_the_same_rules_as_a_chain_data_class_credentials_and_what_is_enabled(tmp_path):
    p, l = write(tmp_path, PROVIDERS, LANES)
    reg = R.load_registry(p)
    pool = [{"provider": "gamma", "model": "g-1", "gateway_model": "g-1"}, {"provider": "delta", "model": "d-1", "gateway_model": "d-1"}, {"provider": "beta", "model": "b-1", "gateway_model": "b-1"}]
    router = RT.Router(reg, L.load_lanes(l, reg), U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), {"beta": C.MockClient(["x"])}, RV.ReviewQueue(tmp_path / "r.json"), pool=pool)
    by = {r["provider"]: r for r in router.explain("internal", "internal", NOW)}
    assert by["gamma"]["eligible"] is False and by["gamma"]["reason"] == "not approved for internal data"          # an experimental route is refused for Internal data only because the owner has not approved it, not because it is experimental
    assert by["delta"]["eligible"] is False and "disabled" in by["delta"]["reason"] and by["beta"]["eligible"] is True and by["beta"]["source"] == "lane" and by["gamma"]["source"] == "pool"       # beta is in this lane's own chain and in the pool: it is shown once, as the lane's
    public = {r["provider"]: r for r in router.explain("internal", "public", NOW)}
    assert public["gamma"]["eligible"] is False and public["gamma"]["reason"] == "no credentials or client available on this machine"        # for Public data it is allowed; here there is simply no client


def test_the_pool_loader_refuses_what_would_make_the_pool_unsafe_or_confusing(tmp_path):
    reg = REG

    def load(entries):
        path = tmp_path / "lanes.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "lanes": {"x": {"chain": []}}, "pool": entries}), encoding="utf-8")
        return L.load_pool(path, reg)
    good = {"provider": "groq", "model": "openai/gpt-oss-120b"}
    assert load([good]) == [{"provider": "groq", "model": "openai/gpt-oss-120b", "gateway_model": "openai/gpt-oss-120b"}] and load([]) == [] and load(None) == []
    assert L.load_pool(tmp_path / "missing.yaml", reg) == []
    for bad, words in (([good, good], "listed twice"), ([{"provider": "nope", "model": "m"}], "not in ai_providers.yaml"), ([{"provider": "groq", "model": "not-a-model"}], "not an approved model"),
                       ([{"provider": "claude", "model": "claude-haiku-5-5"}], "never part of the free pool"), ([{"provider": "github", "model": "openai/gpt-4.1-mini"}], "retired"),
                       ([{"provider": "groq"}], "needs provider and model"), ([{"provider": "groq", "model": "x", "extra": 1}], "needs provider and model"), ("groq", "must be a list"),
                       ([{"provider": "groq", "model": "meta-llama/llama-4-scout-17b-16e-instruct"}], "not an approved model")):
        with pytest.raises(L.LaneError) as seen:
            load(bad)
        assert words in str(seen.value), bad
    path = tmp_path / "lanes.yaml"
    path.write_text("version: 1\nlanes: {x: {chain: []}}\npool: [", encoding="utf-8")
    with pytest.raises(L.LaneError):
        L.load_pool(path, reg)
    path.write_text(yaml.safe_dump({"version": 1, "lanes": {"x": {"chain": [], "use_pool": "yes"}}}), encoding="utf-8")
    with pytest.raises(L.LaneError) as seen:
        L.load_lanes(path, reg)
    assert "use_pool must be true or false" in str(seen.value)


# ---- the lane caps ----------------------------------------------------------------------------------------------------------------

def test_the_lane_caps_are_raised_so_the_stronger_models_can_be_used_fully():
    caps = {n: lane.max_tokens for n, lane in LANE_MAP.items()}
    assert caps == {name: 8000 for name in LANE_MAP} and len(caps) == 12                                              # every lane, none left lower: quality comes first
    was = {"research_extraction": 1500, "competitor_intelligence": 1500, "audience_voice": 1500, "formula_analysis": 800, "trend_opportunity": 2000, "strategic_reasoning": 2500,
           "content_ideation": 1800, "brief_generation": 3000, "production_support": 1500, "validation_qa": 1200, "general_text": 2000, "general_json": 2500}
    assert all(caps[n] >= 2 * was[n] for n in caps) and min(caps.values()) == 8000


def test_the_claude_spending_cap_and_claude_s_place_are_untouched():
    assert REG.daily_usd == 0.5 and REG.monthly_usd == 10 and REG.provider("claude").paid is True
    for name in CLAUDE_LANES:
        assert LANE_MAP[name].claude_leads and not LANE_MAP[name].use_pool


def test_each_lane_keeps_a_proven_provider_in_its_own_chain_and_the_new_reasoning_models_lead_where_only_claude_reasoned():
    for name, lane in LANE_MAP.items():
        if name in CLAUDE_LANES:
            continue
        assert any(c["provider"] in ("groq", "mistral", "cloudflare", "openrouter", "google") for c in lane.chain), name
    assert LANE_MAP["trend_opportunity"].chain[1]["model"] == "command-a-reasoning-08-2025" and LANE_MAP["formula_analysis"].chain[1]["model"] == "command-a-reasoning-08-2025"      # the proven live reasoning model sits behind the lane's proven lead
    assert LANE_MAP["formula_analysis"].chain[0]["provider"] == "groq" and LANE_MAP["content_ideation"].chain[0]["provider"] == "mistral"


# ---- the plan and the live check cover the whole pool -------------------------------------------------------------------------------

def run_cli(tmp_path, monkeypatch, capsys, action, *extra):
    for name in ("ANTHROPIC_API_KEY", "FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    sources = tmp_path / "sources.yaml"                                  # a copy: the command may rewrite the file it is given, and the repository's own must never be touched by a test
    if not sources.exists():
        sources.write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    argv = ["ai", action, "--data-dir", str(tmp_path / "data"), "--sources-config", str(sources), "--browser-config", str(ROOT / "browser.yaml"),
            "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"),
            "--workdir", str(tmp_path / "work"), *extra]
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_a_mock_check_tries_every_pool_model_and_reports_each_provider_on_its_own(tmp_path, monkeypatch, capsys):
    code, out = run_cli(tmp_path, monkeypatch, capsys, "check")
    assert code == 0
    for provider, model in (("kilo", "poolside/laguna-m.1:free"), ("zhipu", "glm-4.6v-flash"), ("nvidia", "google/gemma-4-31b-it"), ("ollama", "gemma4:31b"),
                            ("cohere", "command-r-08-2024"), ("groq", "openai/gpt-oss-20b"), ("google", "gemini-3.5-flash")):
        assert f"MOCK  ok        {provider}/{model}" in out, (provider, model)
    block = out[out.index("Provider status"):]
    for provider in ("kilo", "nvidia", "ollama", "cohere", "zhipu", "groq", "mistral", "openrouter", "cloudflare", "google", "claude"):
        assert any(ln.split()[:2] == ["MOCK", provider] for ln in block.splitlines()), provider                  # one line per provider, each its own result
    assert "0 application problem(s)" in out and "NOTE: a mock pass is not proof that any live provider works" in out


def test_the_plan_shows_the_pool_routes_and_which_data_may_go_where(tmp_path, monkeypatch, capsys):
    code, out = run_cli(tmp_path, monkeypatch, capsys, "plan", "--data-class", "internal")
    assert code == 0 and "(pool)" in out
    kilo_line = "kilo / poolside/laguna-m.1:free (pool): no credentials or client available on this machine"
    assert kilo_line in out and "experimental route" not in out and "not approved for internal" not in out.split("kilo / poolside")[1].split("\n")[0]      # Internal is open through Kilo as through the others; here it only lacks a client
    public_code, public = run_cli(tmp_path, monkeypatch, capsys, "plan", "--data-class", "public")
    assert public_code == 0 and "kilo / poolside/laguna-m.1:free (pool): no credentials or client available on this machine" in public    # for Public data it is allowed (here there is no client)
