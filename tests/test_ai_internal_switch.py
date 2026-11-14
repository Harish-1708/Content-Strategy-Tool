"""Internal data is open everywhere (experimental routes included); the OWNER may turn it off for a provider (Controls) or for one model (internal: false). Nothing in the code forbids it."""
import re

import pytest
import yaml

from contentintel import services as SV
from contentintel.ai import registry as R
from contentintel.observatory import config as OC

from ai_helpers import PROVIDERS, edited, write
from conftest import ROOT

SHIPPED = R.load_registry(ROOT / "ai_providers.yaml")


def test_every_enabled_free_provider_receives_internal_data_including_kilo_which_is_experimental():
    for p in SHIPPED.providers.values():
        if p.enabled:
            assert "internal" in p.approved_classes and p.effective_ceiling in ("internal", "restricted"), p.id
            assert "restricted" not in p.approved_classes or p.id == "claude", p.id                                # Restricted data is a separate rule and goes to Claude only
    kilo = SHIPPED.provider("kilo")
    assert kilo.experimental and kilo.can_receive("internal") and SHIPPED.eligibility("kilo", "internal", model="poolside/laguna-m.1:free") == (True, "")


def test_no_shipped_model_has_internal_switched_off_so_the_default_is_on_everywhere():
    assert all(m.internal for p in SHIPPED.providers.values() for m in p.models)
    assert not re.search(r"(?m)^\s+internal: false", (ROOT / "ai_providers.yaml").read_text(encoding="utf-8"))                 # no model entry carries the switch


def test_an_owner_can_take_internal_data_off_one_model_and_only_that_model_for_internal_data_only(tmp_path):
    data = edited(PROVIDERS, lambda d: d["providers"]["beta"]["models"].append({"id": "b-2", "approved": True, "internal": False}))
    p, _ = write(tmp_path, data)
    reg = R.load_registry(p)
    assert reg.eligibility("beta", "internal", model="b-2") == (False, "Internal data is switched off for model 'b-2' by the owner (internal: false in ai_providers.yaml)")
    assert reg.eligibility("beta", "public", model="b-2") == (True, "")                                              # Public data still goes to it
    assert reg.eligibility("beta", "internal", model="b-1") == (True, "")                                            # the provider's other model is untouched
    assert reg.provider("beta").model("b-2").internal is False and reg.provider("beta").model("b-1").internal is True
    bad = edited(PROVIDERS, lambda d: d["providers"]["beta"]["models"][0].update(internal="no"))
    (tmp_path / "bad").mkdir()
    p2, _ = write(tmp_path / "bad", bad)
    with pytest.raises(R.RegistryError, match="internal must be true or false"):
        R.load_registry(p2)


def test_the_registry_no_longer_refuses_an_experimental_route_with_an_internal_ceiling(tmp_path):
    data = edited(PROVIDERS, lambda d: d["providers"]["gamma"].update(ceiling="internal", approved_classes=["public", "internal"]))
    p, _ = write(tmp_path, data)
    assert R.load_registry(p).provider("gamma").can_receive("internal")


# ---- the owner's switch in Controls -------------------------------------------------------------------------------------------------

def test_there_is_an_internal_data_switch_for_every_provider_and_each_defaults_on_like_everything_else():
    settings = OC.load_settings("missing.yaml")
    assert sorted(OC.GATEWAY_PROVIDERS) == sorted(p for p, x in yaml.safe_load((ROOT / "ai_providers.yaml").read_text(encoding="utf-8"))["providers"].items() if x.get("enabled") and p != "claude")
    channels = settings["services"]["ai_internal_data"]["channels"]
    assert channels == {p: True for p in OC.GATEWAY_PROVIDERS} and len(channels) == 10
    for p in OC.GATEWAY_PROVIDERS:
        defn = SV.BY_KEY[f"services.ai_internal_data.channels.{p}"]
        assert defn["default"] is True and SV.current(settings, defn["key"]) is True and "Internal data may be sent" in defn["text"] and "Public data only" in defn["text"], p
        assert SV.BY_KEY[f"services.ai_gateway.channels.{p}"]["default"] is True                                     # and the provider's own switch, the five new ones included, defaults on
    for p in ("nvidia", "ollama", "cohere", "zhipu", "kilo"):
        assert settings["services"]["ai_gateway"]["channels"][p] is True and settings["services"]["ai_internal_data"]["channels"][p] is True
    assert settings["services"]["ai_internal_data"]["use_for"] == {}                                                # no purpose switches: one switch per provider


def test_the_internal_data_switch_can_be_turned_off_from_the_controls_file_and_comes_back_on_with_it(tmp_path):
    path = tmp_path / "observatory.yaml"
    path.write_text(yaml.safe_dump({"services": {"ai_internal_data": {"channels": {"zhipu": False}}}}), encoding="utf-8")
    s = OC.load_settings(str(path))["services"]["ai_internal_data"]["channels"]
    assert s["zhipu"] is False and s["nvidia"] is True and sum(1 for v in s.values() if v) == 9


# ---- the wiring of the four new keys ------------------------------------------------------------------------------------------------

def test_every_enabled_providers_key_reaches_the_runner_in_every_workflow_that_uses_the_gateway_and_is_a_known_secret():
    wanted = sorted({n for p in SHIPPED.providers.values() if p.enabled and p.id != "claude" for n in p.credential_env})
    assert wanted == ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "COHERE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "OLLAMA_API_KEY", "OPENROUTER_API_KEY", "ZHIPU_API_KEY"]
    gateway = (ROOT / ".github/workflows/ai-gateway.yml").read_text(encoding="utf-8")
    observatory = (ROOT / ".github/workflows/observatory.yml").read_text(encoding="utf-8")
    for name in wanted:
        line = f"{name}: ${{{{ secrets.{name} }}}}"
        assert gateway.count(line) in (2, 3) and observatory.count(line) == 1, name                                    # ai-gateway.yml: the live check and the evaluation (and, for the keys with a model-list address, the catalog step); observatory.yml once
    secrets = next(g for g in re.findall(r'"secrets": \(([^)]*)\)', (ROOT / "contentintel/connections.py").read_text(encoding="utf-8")) if "FREEAPI_ENCRYPTION_KEY" in g)
    for name in ("NVIDIA_API_KEY", "ZHIPU_API_KEY", "OLLAMA_API_KEY", "COHERE_API_KEY"):
        assert f'"{name}"' in secrets, name


def test_the_setup_instructions_and_the_pool_guide_exist_and_say_what_the_owner_needs():
    setup = (ROOT / "docs/ai/setup-instructions.md").read_text(encoding="utf-8")
    assert "Commercial use allowed?" in setup and "Does it train on prompts?" in setup and setup.index("Does it train on prompts?") < setup.index("Commercial use allowed?") < setup.index("Is a card asked for?")
    for name in ("NVIDIA_API_KEY", "ZHIPU_API_KEY", "OLLAMA_API_KEY", "COHERE_API_KEY"):
        assert f"`{name}`" in setup, name
    guide = (ROOT / "docs/ai/shared-pool-and-pacing.md").read_text(encoding="utf-8")
    for phrase in ("shared pool", "Retry-After", "requests_per_minute", "concurrency", "internal: false", "services.ai_internal_data", "8,000", "halves", "dashboard"):
        assert phrase in guide, phrase


@pytest.mark.parametrize("provider", OC.GATEWAY_PROVIDERS)
def test_each_internal_data_switch_can_be_turned_off_and_back_on_from_the_controls_and_affects_only_itself(provider, tmp_path):
    key = f"services.ai_internal_data.channels.{provider}"
    SV.save_overrides(str(tmp_path), {key: False}, "owner", "2026-11-14T10:00:00Z", "off")
    off = SV.load_settings("observatory.yaml", str(tmp_path))["services"]["ai_internal_data"]["channels"]
    assert off[provider] is False and sum(1 for v in off.values() if v) == 9
    SV.save_overrides(str(tmp_path), {key: True}, "owner", "2026-11-14T10:01:00Z", "on")
    assert all(SV.load_settings("observatory.yaml", str(tmp_path))["services"]["ai_internal_data"]["channels"].values())
