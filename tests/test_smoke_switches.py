"""The gateway smoke test's check of which models are on: exactly the approved models of the enabled providers that have a credential (or need none), and nothing else. Update 57 switched Kilo on (no key), so
'no model may be on' stopped being true: the check now compares with what is meant to be on, and names a model that is not."""
from contentintel.ai import registry as R
from contentintel.ai import smoke as SM

from ai_helpers import PROVIDERS, edited, write
from conftest import ROOT

SHIPPED = R.load_registry(ROOT / "ai_providers.yaml")
KILO = ("kilo", "nvidia/nemotron-3-super-120b-a12b:free")
ALL_KEYS = {n: "k" for p in SHIPPED.providers.values() for n in p.credential_env}


def row(platform, model, enabled, key="modelId"):
    return {"platform": platform, key: model, "enabled": enabled}


def test_with_no_key_only_kilos_one_approved_model_is_meant_to_be_on():
    assert SM.expected_enabled(SHIPPED, {}) == {KILO}


def test_with_every_key_the_approved_models_of_every_gateway_provider_are_meant_to_be_on_and_claude_and_direct_providers_never():
    want = SM.expected_enabled(SHIPPED, ALL_KEYS)
    per = {}
    for platform, _ in want:
        per[platform] = per.get(platform, 0) + 1
    assert len(want) == 39 and per == {"groq": 2, "mistral": 2, "cloudflare": 1, "google": 2, "nvidia": 13, "ollama": 11, "cohere": 4, "zhipu": 3, "kilo": 1}
    assert not any(p in ("claude", "openrouter") for p, _ in want)                                              # Claude has its own client; OpenRouter is called directly
    assert SM.expected_enabled(SHIPPED, {"NVIDIA_API_KEY": "k"}) == {KILO} | {p for p in want if p[0] == "nvidia"}      # a key switches on only its own provider's approved models
    assert SM.expected_enabled(SHIPPED, {"NVIDIA_API_KEY": "  "}) == {KILO}                                      # an empty key is no key
    assert ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast") not in SM.expected_enabled(SHIPPED, {"CLOUDFLARE_API_TOKEN": "k"})      # a provider needing two credentials needs both


def test_the_check_passes_when_exactly_the_meant_models_are_on_and_names_what_is_wrong_otherwise():
    rows = [row("kilo", KILO[1], True), row("kilo", "poolside/laguna-m.1:free", False), row("nvidia", "deepseek-ai/deepseek-v4-pro", False), row("custom", "smoke-model", True)]
    ok, detail = SM.switch_check(rows, SHIPPED, {})
    assert ok and detail == "4 rows, 0 wrongly enabled, 0 approved but off, 1 approved and on"                   # the custom smoke model is ignored
    ok, detail = SM.switch_check([row("kilo", KILO[1], True), row("kilo", "poolside/laguna-m.1:free", True)], SHIPPED, {})
    assert not ok and "1 wrongly enabled: kilo/poolside/laguna-m.1:free" in detail                                # an unapproved model that is on is named
    ok, detail = SM.switch_check([row("kilo", KILO[1], True), row("nvidia", "deepseek-ai/deepseek-v4-pro", True)], SHIPPED, {})
    assert not ok and "wrongly enabled: nvidia/deepseek-ai/deepseek-v4-pro" in detail                              # an approved model whose provider has no key must be off
    ok, detail = SM.switch_check([row("kilo", KILO[1], False)], SHIPPED, {})
    assert not ok and "1 approved but off: kilo/nvidia/nemotron-3-super-120b-a12b:free" in detail                  # an approved model that is off is named too
    ok, detail = SM.switch_check([row("nvidia", "deepseek-ai/deepseek-v4-pro", True), row("kilo", KILO[1], True)], SHIPPED, {"NVIDIA_API_KEY": "k"})
    assert ok and detail.endswith("2 approved and on")                                                            # with the key, the same row is right
    assert SM.switch_check([row("custom", "smoke-model", True)], SHIPPED, {})[0] is False                           # no gateway rows at all is a failure, not a pass
    assert SM.switch_check([row("kilo", KILO[1], True, key="model_id")], SHIPPED, {})[0] is True                    # the gateway may spell the id either way


def test_a_model_the_gateway_has_that_is_not_in_the_registry_at_all_is_wrongly_enabled_when_on(tmp_path):
    data = edited(PROVIDERS, lambda d: d["providers"]["beta"].update(credentials={"env": []}))
    p, _ = write(tmp_path, data)
    reg = R.load_registry(p)
    assert SM.expected_enabled(reg, {}) == {("gamma", "g-1"), ("beta", "b-1")}
    ok, detail = SM.switch_check([row("gamma", "g-1", True), row("beta", "b-1", True), row("beta", "b-2", True)], reg, {})
    assert not ok and "1 wrongly enabled: beta/b-2" in detail
