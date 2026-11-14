"""Direct providers (OpenRouter, called straight from this tool) and the retired provider GitHub Models: kept as a record, never enabled, routed to or called."""
import copy

import pytest
import yaml

from contentintel import cli
from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import commands as CM
from contentintel.ai import gateway as GW
from contentintel.ai import lanes as LN
from contentintel.ai import registry as R
from conftest import ROOT

SHIPPED = yaml.safe_load((ROOT / "ai_providers.yaml").read_text(encoding="utf-8"))
TOKEN = "sk-or-" + "A" * 36
OR = "https://openrouter.ai/api/v1"
ROUTE = {"provider": "openrouter", "model": "vendor/some-model:free", "gateway_model": "vendor/some-model:free"}
RETIRED_URL = "https://models.github.ai/inference"


def load(tmp_path, mutate):
    data = copy.deepcopy(SHIPPED)
    mutate(data["providers"])
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return R.load_registry(p)


# ---- the registry only accepts endpoints this tool knows -------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://evil.example/v1", "http://openrouter.ai/api/v1", "https://openrouter.ai.evil.test/api/v1", "https://openrouter.ai/api/other", RETIRED_URL, ""])
def test_a_direct_provider_can_only_point_at_a_known_endpoint(tmp_path, url):
    with pytest.raises(R.RegistryError, match="must be one of the endpoints this tool knows"):
        load(tmp_path, lambda prov: prov["openrouter"]["direct"].update(base_url=url))


def test_other_direct_mistakes_are_refused(tmp_path):
    with pytest.raises(R.RegistryError, match="unknown setting"):
        load(tmp_path, lambda prov: prov["openrouter"]["direct"].update(proxy="http://x"))
    with pytest.raises(R.RegistryError, match="unknown setting"):
        load(tmp_path, lambda prov: prov["openrouter"]["direct"].update(api_versions=["2026-03-10"]))                  # the GitHub API-version setting is gone
    with pytest.raises(R.RegistryError, match="Claude has its own client"):
        load(tmp_path, lambda prov: prov["claude"].update(direct={"base_url": OR}))


# ---- GitHub Models is retired: the record stays, the integration is gone -----------------------------------------------------------------------

def test_github_models_is_kept_as_a_retired_record_that_cannot_be_used():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    p = reg.provider("github")
    assert p.retired["date"] == "2026-07-30" and p.retired["source"].startswith("https://github.blog/changelog/") and "plain-text OK" in p.retired["reason"]
    assert not p.enabled and p.direct == {} and p.credential_env == () and p.label == "GitHub Models"                    # no switch, no endpoint, no credential name
    assert p.override["approved_by"] == "Harish" and [m.id for m in p.models if m.approved] == ["openai/gpt-4.1-mini", "meta/Llama-3.3-70B-Instruct"]      # the history is kept as it was
    ok, why = reg.eligibility("github", "public", model="openai/gpt-4.1-mini")
    assert not ok and why == "RETIRED: GitHub Models was withdrawn by its provider on 2026-07-30; it is never called"
    assert sorted(pid for pid, x in reg.providers.items() if x.enabled) == ["claude", "cloudflare", "cohere", "google", "groq", "kilo", "mistral", "nvidia", "ollama", "openrouter", "zhipu"]
    assert [pid for pid, x in reg.providers.items() if x.retired] == ["github"]


def test_a_retired_provider_cannot_be_switched_back_on_or_given_an_endpoint_a_credential_or_an_incomplete_record(tmp_path):
    with pytest.raises(R.RegistryError, match="cannot be enabled and keeps no endpoint or credential name"):
        load(tmp_path, lambda prov: prov["github"].update(enabled=True))
    with pytest.raises(R.RegistryError, match="cannot be enabled and keeps no endpoint or credential name"):
        load(tmp_path, lambda prov: prov["github"].update(direct={"base_url": OR}))
    with pytest.raises(R.RegistryError, match="cannot be enabled and keeps no endpoint or credential name"):
        load(tmp_path, lambda prov: prov["github"].update(credentials={"env": ["GITHUB_TOKEN"]}))
    for missing in ("date", "source", "reason"):
        with pytest.raises(R.RegistryError, match="retired needs date, source and reason"):
            load(tmp_path, lambda prov, m=missing: prov["github"]["retired"].update({m: ""}))
    with pytest.raises(R.RegistryError, match="unknown setting"):
        load(tmp_path, lambda prov: prov["github"]["retired"].update(why="x"))


def test_no_lane_routes_to_the_retired_provider_and_a_lane_file_that_does_is_refused(tmp_path):
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    lanes = LN.load_lanes(ROOT / "ai_lanes.yaml", reg)
    assert not [r for lane in lanes.values() for r in lane.chain if r["provider"] == "github"] and len(lanes) >= 10
    raw = yaml.safe_load((ROOT / "ai_lanes.yaml").read_text(encoding="utf-8"))
    raw["lanes"]["general_text"]["chain"].append({"provider": "github", "model": "openai/gpt-4.1-mini"})
    bad = tmp_path / "lanes.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(LN.LaneError, match="github was retired by its provider \\(2026-07-30\\); remove this route"):
        LN.load_lanes(bad, reg)


@pytest.mark.parametrize("url", [RETIRED_URL, "https://models.github.ai/inference/", "https://MODELS.GITHUB.AI/inference", "https://eu.models.github.ai/inference"])
def test_the_client_refuses_the_retired_address_before_any_request_is_made(url):
    calls = []
    with pytest.raises(K.Failure) as seen:
        C.DirectClient(url, TOKEN, http=lambda *a, **k: calls.append(a))
    assert seen.value.code == K.PROVIDER_RETIRED == "PROVIDER_RETIRED" and "retired by GitHub on 30 July 2026" in str(seen.value) and "no token, permission or request change" in str(seen.value)
    assert "Groq, OpenRouter, Google, Cloudflare, Claude" in str(seen.value) and calls == []
    with pytest.raises(K.Failure) as seen:
        C.OpenRouterClient(url, TOKEN, http=lambda *a, **k: calls.append(a))
    assert seen.value.code == K.PROVIDER_RETIRED and calls == []


def test_a_retired_provider_is_a_final_condition_not_a_failure_worth_trying_again():
    assert K.PROVIDER_RETIRED in K.CODES and K.PROVIDER_RETIRED in K.FINAL_CODES
    assert K.PROVIDER_RETIRED not in (K.UPSTREAM_BROKEN, K.NEED_SETUP)                                          # never reported as an ordinary upstream failure or a missing key


def test_nothing_in_the_repository_calls_the_retired_address_except_the_refusal_and_the_record():
    allowed = {"contentintel/ai/clients.py"}                                                                       # the list of refused addresses
    skip = {".git", "data", "node_modules", "__pycache__", ".pytest_cache", "gateway-src", "ai-work", "external", "tests", "docs"}
    hits = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.is_file() and not (set(rel.parts) & skip) and path.suffix in (".py", ".yml", ".yaml", ".json", ".md", ".txt") and str(rel) not in allowed:
            if "models.github.ai" in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(rel))
    assert hits == [], hits


def test_no_workflow_asks_for_the_models_permission_or_a_models_token_any_more():
    import re
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s+models:\s*read", text, re.M) and "GH_MODELS_TOKEN" not in text and "GITHUB_MODELS_TOKEN" not in text and "models.github.ai" not in text, path.name
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GH_MODELS_TOKEN" not in readme and "retired by GitHub on 30 July 2026" in readme
    assert "30 July 2026" in (ROOT / "docs" / "ai" / "retired-providers.md").read_text(encoding="utf-8")


# ---- the direct client (OpenRouter) ----------------------------------------------------------------------------------------------------------

def http_returning(status, body, headers=None):
    calls = []

    def http(method, url, *, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return status, resp, (rheaders or {})
    resp, rheaders = body, headers
    http.calls = calls
    return http


OK = {"model": "vendor/some-model:free", "choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def test_a_request_goes_to_the_provider_with_the_token_in_a_header_only():
    http = http_returning(200, OK)
    got = C.DirectClient(OR, TOKEN, http=http).complete(ROUTE, "be brief", "say hi", 300)
    call = http.calls[0]
    assert call["url"] == OR + "/chat/completions" and call["headers"]["Authorization"] == "Bearer " + TOKEN and call["headers"]["User-Agent"] == C.USER_AGENT
    assert "X-GitHub-Api-Version" not in call["headers"] and call["headers"]["Accept"] == "application/json"
    assert call["body"] == {"model": "vendor/some-model:free", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "say hi"}], "max_tokens": 300, "temperature": 0}
    assert TOKEN not in str(call["body"]) and (got.text, got.tokens_in, got.tokens_out, got.model) == ("hello", 12, 3, "vendor/some-model:free")


@pytest.mark.parametrize("status, code", [(401, K.NEED_SETUP), (403, K.NEED_SETUP), (429, K.RATE_LIMITED), (402, K.RATE_LIMITED), (400, K.BAD_INPUT), (413, K.BAD_INPUT), (422, K.BAD_INPUT),
                                          (500, K.UPSTREAM_BROKEN), (503, K.UPSTREAM_BROKEN), (418, K.UPSTREAM_BROKEN)])
def test_failures_get_their_real_codes_and_the_token_never_appears_in_a_message(status, code):
    http = http_returning(status, {"error": {"message": f"bad token {TOKEN} for this call"}}, {"Retry-After": "12"})
    with pytest.raises(K.Failure) as seen:
        C.DirectClient(OR, TOKEN, http=http).complete(ROUTE, "", "x", 10)
    assert seen.value.code == code and TOKEN not in str(seen.value)
    if status == 429:
        assert seen.value.retry_after == 12
    if status in (401, 403):
        assert "rejected the token" in str(seen.value) and "GitHub" not in str(seen.value)


def test_network_errors_an_answer_with_no_message_and_plain_http_are_handled():
    def dead(*a, **k):
        raise OSError(f"connection failed for {TOKEN}")
    with pytest.raises(K.Failure) as seen:
        C.DirectClient(OR, TOKEN, http=dead).complete(ROUTE, "", "x", 10)
    assert TOKEN not in str(seen.value) and seen.value.code in K.CODES
    with pytest.raises(K.Failure) as seen:
        C.DirectClient(OR, TOKEN, http=http_returning(200, {"choices": []})).complete(ROUTE, "", "x", 10)
    assert seen.value.code == K.UPSTREAM_BROKEN
    with pytest.raises(ValueError, match="https"):
        C.DirectClient("http://openrouter.ai/api/v1", TOKEN)


# ---- it never goes through the gateway, and is built when the token exists -------------------------------------------------------------------

def test_the_gateway_is_never_given_the_retired_provider():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    baseline = GW.baseline_from_inventory()
    cfg, report = GW.build_config(reg, {"FREEAPI_ADMIN_PASSWORD": "long-enough-password", "GITHUB_TOKEN": TOKEN, "GH_MODELS_TOKEN": TOKEN, "GROQ_API_KEY": "gsk_" + "b" * 30}, baseline)
    platforms = {k["platform"] for k in cfg["keys"]} if "keys" in cfg else {k["platform"] for k in report["keys"]}
    assert "github" not in platforms and "groq" in platforms and TOKEN not in str(cfg)
    assert not any(m["platform"] == "github" for m in cfg.get("models", []))


def test_a_direct_client_is_made_only_when_the_token_is_present_and_never_for_the_retired_provider():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    notes = []
    clients, side = CM.live_clients(reg, {"OPENROUTER_API_KEY": TOKEN, "GITHUB_TOKEN": TOKEN, "GH_MODELS_TOKEN": TOKEN}, None, "/tmp", notes)
    assert isinstance(clients["openrouter"], C.OpenRouterClient) and side is None and "github" not in clients
    assert not [n for n in notes if "github" in n.lower()]                                                       # not even a "no credential" note: it is not a provider in use
    clients, _ = CM.live_clients(reg, {}, None, "/tmp", notes)
    assert "openrouter" not in clients and any(n.startswith("SKIPPED: openrouter: no credential") for n in notes)
    clients, _ = CM.live_clients(reg, {"OPENROUTER_API_KEY": "   "}, None, "/tmp", [])
    assert "openrouter" not in clients


def test_the_plan_never_offers_the_retired_provider_whatever_secrets_exist(tmp_path, monkeypatch, capsys):
    def plan(data_class):
        code = cli.main(["ai", "plan", "--data-class", data_class, "--data-dir", str(tmp_path / "d"), "--sources-config", str(ROOT / "sources.yaml"), "--browser-config", str(ROOT / "browser.yaml"),
                         "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"),
                         "--workdir", str(tmp_path / "w")])
        assert code == 0
        return capsys.readouterr().out
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GH_MODELS_TOKEN", TOKEN)
    for data_class in ("internal", "public", "restricted"):
        assert "github" not in plan(data_class)


def test_the_providers_command_lists_the_retired_provider_as_retired_and_counts_it_apart(tmp_path, capsys):
    assert cli.main(["ai", "providers", "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--data-dir", str(tmp_path / "d"), "--sources-config", str(ROOT / "sources.yaml"),
                     "--browser-config", str(ROOT / "browser.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"), "--workdir", str(tmp_path / "w")]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("github "))
    assert "RETIRED" in line and "ON" not in line.split()
    assert "11 enabled, 1 retired (kept as a record, never called)" in out


def test_cerebras_was_checked_found_to_need_a_card_and_stays_off():
    p = R.load_registry(ROOT / "ai_providers.yaml").provider("cerebras")
    assert not p.enabled and p.terms["status"] == "verified_secondary" and "payment method" in p.terms["note"] and "no-card" in p.terms["business_use"]


def test_the_cloudflare_quota_is_unknown_not_guessed_while_its_model_and_data_limits_stand():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    p = reg.provider("cloudflare")
    assert p.limits == {} and "QUOTA UNKNOWN until verified live" in p.terms["note"] and "10,000" in p.terms["note"]
    assert p.enabled and p.approved_classes == ("public", "internal") and p.override["approved_by"] == "Harish" and p.terms["trains_on_data"].startswith("no")
    assert [m.id for m in p.models if m.approved] == ["@cf/meta/llama-3.3-70b-instruct-fp8-fast"]          # one approved model, as before


def test_the_health_tab_says_the_quota_is_not_known_instead_of_leaving_it_blank():
    from contentintel.ai import sheetrows as SR
    from contentintel.ai import usage as U
    import tempfile
    from pathlib import Path
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    ledger = U.UsageLedger(Path(tempfile.mkdtemp()) / "u.json")
    ledger.record(reg.provider("cloudflare"), "research_extraction", "2026-11-04T10:00:00Z", ok=True, tokens_in=10, tokens_out=5)
    row = SR.rows(reg, ledger, {}, "2026-11-04T10:00:00Z")["cloudflare"]
    assert row["daily_limit"] is None and row["left_today"] is None and "free quota not known" in row["note"]
    assert "github" not in SR.rows(reg, ledger, {}, "2026-11-04T10:00:00Z")                           # a retired provider has no row: it is not in use


class FakeSidecar:
    made = []

    def __init__(self, gateway_dir, lock):
        self.isolated, self.base_url, self.result = [], "http://127.0.0.1:1", (True, "3 model(s) are each their own group")
        FakeSidecar.made.append(self)

    def start(self, env, port, log):
        pass

    def wait_ready(self):
        pass

    def bootstrap(self, password):
        return "unified-key"

    def isolate_models(self, password):
        self.isolated.append(password)
        return self.result

    def stop(self):
        pass


def _gateway_env(tmp_path):
    (tmp_path / "gw" / "server" / "dist").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gw" / "server" / "dist" / "index.js").write_text("")
    return {"FREEAPI_ENCRYPTION_KEY": "ab" * 32, "FREEAPI_ADMIN_PASSWORD": "long-enough-password", "GROQ_API_KEY": "gsk_" + "b" * 30}


def test_a_live_run_makes_each_providers_model_its_own_group_and_a_gateway_that_cannot_is_a_note_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(CM.gw, "Sidecar", FakeSidecar)
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    FakeSidecar.made.clear()
    notes = []
    clients, side = CM.live_clients(reg, _gateway_env(tmp_path), str(tmp_path / "gw"), str(tmp_path / "work"), notes)
    assert side is FakeSidecar.made[0] and side.isolated == ["long-enough-password"] and "groq" in clients
    assert any(n.startswith("SKIPPED: mistral: no credential") for n in notes) and not any(n.startswith("APPLICATION: mistral") for n in notes)        # no key on purpose: a skip, never a problem
    assert not [n for n in notes if "own group" in n]                                    # nothing to report when it worked (the Claude-key note is a different matter)
    FakeSidecar.made.clear()
    monkeypatch.setattr(FakeSidecar, "isolate_models", lambda self, pw: (False, "the gateway did not accept the grouping change (404)"))
    notes = []
    clients, _ = CM.live_clients(reg, _gateway_env(tmp_path), str(tmp_path / "gw"), str(tmp_path / "work"), notes)
    assert "groq" in clients and any(n.startswith("NOTE: could not make each provider") and "(404)" in n for n in notes) and not any("could not make each provider" in n and n.startswith("APPLICATION") for n in notes)        # a note, never an application problem
