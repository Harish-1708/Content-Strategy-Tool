"""GitHub Models: a direct provider (not through the local gateway), free with a GitHub token, limited to what the registry approves."""
import copy

import pytest
import yaml

from contentintel import cli
from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import commands as CM
from contentintel.ai import gateway as GW
from contentintel.ai import registry as R
from conftest import ROOT

SHIPPED = yaml.safe_load((ROOT / "ai_providers.yaml").read_text(encoding="utf-8"))
TOKEN = "ghp_" + "A" * 36
ROUTE = {"provider": "github", "model": "openai/gpt-4.1-mini", "gateway_model": "openai/gpt-4.1-mini"}


def load(tmp_path, mutate):
    data = copy.deepcopy(SHIPPED)
    mutate(data["providers"])
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return R.load_registry(p)


# ---- the registry only accepts endpoints this tool knows -------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://evil.example/v1", "http://models.github.ai/inference", "https://models.github.ai.evil.test/inference", "https://models.github.ai/other", ""])
def test_a_direct_provider_can_only_point_at_a_known_endpoint(tmp_path, url):
    with pytest.raises(R.RegistryError, match="must be one of the endpoints this tool knows"):
        load(tmp_path, lambda prov: prov["github"]["direct"].update(base_url=url))


def test_other_direct_mistakes_are_refused(tmp_path):
    with pytest.raises(R.RegistryError, match="unknown setting"):
        load(tmp_path, lambda prov: prov["github"]["direct"].update(proxy="http://x"))
    with pytest.raises(R.RegistryError, match="Claude has its own client"):
        load(tmp_path, lambda prov: prov["claude"].update(direct={"base_url": "https://models.github.ai/inference"}))


def test_github_models_is_a_free_direct_provider_with_the_owners_recorded_decision():
    p = R.load_registry(ROOT / "ai_providers.yaml").provider("github")
    assert p.enabled and p.direct == {"base_url": "https://models.github.ai/inference"} and not p.paid and p.credential_env == ("GITHUB_TOKEN",)
    assert p.approved_classes == ("public", "internal") and p.override["approved_by"] == "Harish" and p.override["date"] == "2026-11-03"
    assert [m.id for m in p.models if m.approved] == ["openai/gpt-4.1-mini", "meta/Llama-3.3-70B-Instruct"]      # only the small-model tier, whose free limits the provider limits describe
    assert p.limits == {"requests_per_day": 150, "requests_per_minute": 15} and "prototyping" in p.terms["business_use"] and "must stay off" in p.terms["business_use"]


# ---- the client -------------------------------------------------------------------------------------------------------------------------------

def http_returning(status, body, headers=None):
    calls = []

    def http(method, url, *, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return status, resp, (rheaders or {})
    resp, rheaders = body, headers
    http.calls = calls
    return http


OK = {"model": "openai/gpt-4.1-mini", "choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def test_a_request_goes_to_the_provider_with_the_token_in_a_header_only():
    http = http_returning(200, OK)
    got = C.DirectClient("https://models.github.ai/inference", TOKEN, http=http).complete(ROUTE, "be brief", "say hi", 300)
    call = http.calls[0]
    assert call["url"] == "https://models.github.ai/inference/chat/completions" and call["headers"]["Authorization"] == "Bearer " + TOKEN
    assert call["body"] == {"model": "openai/gpt-4.1-mini", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "say hi"}], "max_tokens": 300, "temperature": 0}
    assert TOKEN not in str(call["body"]) and (got.text, got.tokens_in, got.tokens_out, got.model) == ("hello", 12, 3, "openai/gpt-4.1-mini")


@pytest.mark.parametrize("status, code", [(401, K.NEED_SETUP), (403, K.NEED_SETUP), (429, K.RATE_LIMITED), (402, K.RATE_LIMITED), (400, K.BAD_INPUT), (413, K.BAD_INPUT), (422, K.BAD_INPUT),
                                          (500, K.UPSTREAM_BROKEN), (503, K.UPSTREAM_BROKEN), (418, K.UPSTREAM_BROKEN)])
def test_failures_get_their_real_codes_and_the_token_never_appears_in_a_message(status, code):
    http = http_returning(status, {"error": {"message": f"bad token {TOKEN} for this call"}}, {"Retry-After": "12"})
    with pytest.raises(K.Failure) as seen:
        C.DirectClient("https://models.github.ai/inference", TOKEN, http=http).complete(ROUTE, "", "x", 10)
    assert seen.value.code == code and TOKEN not in str(seen.value)
    if status == 429:
        assert seen.value.retry_after == 12
    if status in (401, 403):
        assert "models: read" in str(seen.value)


def test_network_errors_an_answer_with_no_message_and_plain_http_are_handled():
    def dead(*a, **k):
        raise OSError(f"connection failed for {TOKEN}")
    with pytest.raises(K.Failure) as seen:
        C.DirectClient("https://models.github.ai/inference", TOKEN, http=dead).complete(ROUTE, "", "x", 10)
    assert TOKEN not in str(seen.value) and seen.value.code in K.CODES
    with pytest.raises(K.Failure) as seen:
        C.DirectClient("https://models.github.ai/inference", TOKEN, http=http_returning(200, {"choices": []})).complete(ROUTE, "", "x", 10)
    assert seen.value.code == K.UPSTREAM_BROKEN
    with pytest.raises(ValueError, match="https"):
        C.DirectClient("http://models.github.ai/inference", TOKEN)


# ---- it never goes through the gateway, and is built when the token exists -------------------------------------------------------------------

def test_the_gateway_is_never_given_github_models():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    baseline = GW.baseline_from_inventory()
    cfg, report = GW.build_config(reg, {"FREEAPI_ADMIN_PASSWORD": "long-enough-password", "GITHUB_TOKEN": TOKEN, "GROQ_API_KEY": "gsk_" + "b" * 30}, baseline)
    platforms = {k["platform"] for k in cfg["keys"]} if "keys" in cfg else {k["platform"] for k in report["keys"]}
    assert "github" not in platforms and "groq" in platforms and TOKEN not in str(cfg)
    assert not any(m["platform"] == "github" for m in cfg.get("models", []))


def test_a_direct_client_is_made_only_when_the_token_is_present_and_without_starting_a_gateway():
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    notes = []
    clients, side = CM.live_clients(reg, {"GITHUB_TOKEN": TOKEN}, None, "/tmp", notes)
    assert isinstance(clients["github"], C.DirectClient) and side is None and "github" in clients
    notes = []
    clients, _ = CM.live_clients(reg, {}, None, "/tmp", notes)
    assert "github" not in clients and any("github: enabled, but no credential" in n and "GITHUB_TOKEN" in n for n in notes)
    notes = []
    clients, _ = CM.live_clients(reg, {"GITHUB_TOKEN": "   "}, None, "/tmp", notes)
    assert "github" not in clients


def test_the_plan_offers_github_only_for_internal_lanes_and_only_when_the_token_exists(tmp_path, monkeypatch, capsys):
    def plan(data_class):
        code = cli.main(["ai", "plan", "--data-class", data_class, "--data-dir", str(tmp_path / "d"), "--sources-config", str(ROOT / "sources.yaml"), "--browser-config", str(ROOT / "browser.yaml"),
                         "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"),
                         "--workdir", str(tmp_path / "w")])
        assert code == 0
        return capsys.readouterr().out
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "ready   github" not in plan("internal")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    internal = plan("internal")
    assert "ready   github / openai/gpt-4.1-mini" in internal and "github / openai/gpt-4.1: disabled" not in internal.replace("github / openai/gpt-4.1-mini", "")
    assert "ready   github" not in plan("restricted")                                          # restricted data reaches Claude only


def test_cerebras_was_checked_found_to_need_a_card_and_stays_off():
    p = R.load_registry(ROOT / "ai_providers.yaml").provider("cerebras")
    assert not p.enabled and p.terms["status"] == "verified_secondary" and "payment method" in p.terms["note"] and "no-card" in p.terms["business_use"]
