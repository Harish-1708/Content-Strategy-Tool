"""AI configuration: data classes, the provider registry and model allowlist, the lanes, and the shipped files."""
import json

import pytest
import yaml

from contentintel.ai import data_class as dc
from contentintel.ai import lanes as L
from contentintel.ai import registry as R

from ai_helpers import LANES, PROVIDERS, edited, write
from conftest import ROOT

SHIPPED_P, SHIPPED_L = ROOT / "ai_providers.yaml", ROOT / "ai_lanes.yaml"
INVENTORY = json.loads((ROOT / "docs" / "ai" / "provider_inventory.json").read_text(encoding="utf-8"))


# ---- data classes -------------------------------------------------------------------------------------------------------------

def test_data_classes_are_ordered_and_unknown_ones_fail_closed():
    assert dc.ORDER == ("public", "internal", "restricted")
    assert dc.rank("public") < dc.rank("internal") < dc.rank("restricted")
    assert dc.within("internal", "public") and dc.within("internal", "internal") and not dc.within("internal", "restricted")
    assert dc.highest("public", "restricted", "internal") == "restricted" and dc.highest("public", "public") == "public"
    assert dc.highest() == "restricted"                                       # no information means the most careful class
    for bad in ("secret", "", None, "PUBLIC"):
        with pytest.raises(dc.DataClassError):
            dc.rank(bad)
    assert dc.validate("internal") == "internal"


# ---- the registry ---------------------------------------------------------------------------------------------------------------

def load(tmp_path, fn):
    p, _ = write(tmp_path, providers=edited(PROVIDERS, fn))
    return R.load_registry(p)


def test_a_valid_registry_loads_with_the_recorded_override_and_prices(tmp_path):
    p, _ = write(tmp_path)
    reg = R.load_registry(p)
    assert set(reg.providers) == {"alpha", "beta", "gamma", "delta", "claude"} and (reg.daily_usd, reg.monthly_usd) == (0.05, 0.2)
    beta = reg.provider("beta")
    assert beta.ceiling == "public" and beta.effective_ceiling == "internal" and beta.can_receive("internal") and not beta.can_receive("restricted")
    assert reg.provider("claude").price_for("claude-haiku-5-5") == {"input": 0.1, "output": 0.5}
    assert reg.provider("claude").price_for("claude-sonnet-5-5") == {"input": 2.0, "output": 10.0}
    assert reg.provider("alpha").price_for("a-1") == {}


@pytest.mark.parametrize("fn, fragment", [
    (lambda d: d.update(version=2), "version must be 1"),
    (lambda d: d.update(extra=1), "unknown setting"),
    (lambda d: d["providers"]["alpha"].update(colour="red"), "unknown setting"),
    (lambda d: d["providers"]["alpha"].update(ceiling="secret"), "not a data class"),
    (lambda d: d["providers"]["alpha"].update(approved_classes=["internal"]), "above the ceiling"),
    (lambda d: d["providers"]["alpha"].update(approved_classes=["restricted"]), "above the ceiling"),
    (lambda d: (d["providers"]["beta"]["owner_override"].update(ceiling="restricted"), d["providers"]["beta"].update(approved_classes=["public", "internal", "restricted"])),
     "Restricted data may be approved for claude only"),
    (lambda d: d["providers"]["beta"].update(approved_classes=["public", "internal", "restricted"]), "above the ceiling"),
    (lambda d: d["providers"]["beta"]["owner_override"].pop("reason"), "an override is a recorded decision"),
    (lambda d: d["providers"]["beta"]["owner_override"].update(approved_by=" "), "an override is a recorded decision"),
    (lambda d: d["providers"]["beta"]["owner_override"].update(ceiling="secret"), "not a data class"),
    (lambda d: d["providers"]["beta"]["owner_override"].update(extra=1), "unknown setting"),
    (lambda d: d["providers"]["beta"]["terms"].update(status="unverified"), "cannot be overridden"),
    (lambda d: d["providers"]["alpha"]["terms"].update(status="unverified"), "cannot be enabled"),
    (lambda d: d["providers"]["alpha"]["terms"].update(status="declared_only"), "cannot be enabled"),
    (lambda d: d["providers"]["alpha"]["terms"].update(status="made up"), "terms.status must be"),
    (lambda d: d["providers"]["alpha"]["terms"].update(unknown=1), "unknown setting"),
    (lambda d: d["providers"]["alpha"].update(enabled="yes"), "must be true or false"),
    (lambda d: d["providers"]["alpha"].update(paid="no"), "must be true or false"),
    (lambda d: d["providers"]["alpha"]["limits"].update(requests_per_day=-1), "number of 0 or more"),
    (lambda d: d["providers"]["alpha"]["limits"].update(requests_per_day=True), "number of 0 or more"),
    (lambda d: d["providers"]["alpha"]["limits"].update(bogus=1), "unknown setting"),
    (lambda d: d["providers"]["alpha"].update(paid=True), "paid provider needs price_per_million"),
    (lambda d: d["providers"]["claude"]["models"][0].update(price_per_million={"input": 1, "output": 2, "extra": 3}), "unknown setting"),
    (lambda d: d["providers"]["alpha"]["models"].append({"id": "a-1"}), "listed twice"),
    (lambda d: d["providers"]["alpha"]["models"].append({"nope": 1}), "needs an id"),
    (lambda d: d["providers"]["alpha"]["models"][0].update(approved="yes"), "must be true or false"),
    (lambda d: d["budgets"].update(daily_usd=-1), "number of 0 or more"),
    (lambda d: d["budgets"].update(weekly=1), "unknown setting"),
    (lambda d: d.update(providers={}), "at least one provider"),
])
def test_a_mistake_in_the_registry_is_refused_with_a_reason(tmp_path, fn, fragment):
    with pytest.raises(R.RegistryError, match=fragment):
        load(tmp_path, fn)


def test_a_missing_or_unreadable_registry_is_refused(tmp_path):
    with pytest.raises(R.RegistryError, match="is missing"):
        R.load_registry(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(R.RegistryError, match="could not be read"):
        R.load_registry(tmp_path / "bad.yaml")
    (tmp_path / "list.yaml").write_text("- a\n", encoding="utf-8")
    with pytest.raises(R.RegistryError, match="list of settings"):
        R.load_registry(tmp_path / "list.yaml")


@pytest.mark.parametrize("pid, data_class, model, experimental_ok, ok, why", [
    ("alpha", "public", "a-1", False, True, ""),
    ("alpha", "internal", "a-1", False, False, "not approved for internal data"),
    ("alpha", "public", "a-2", False, False, "not in the approved-model allowlist"),
    ("alpha", "public", "unlisted", False, False, "not in the approved-model allowlist"),
    ("beta", "internal", "b-1", False, True, ""),
    ("beta", "restricted", "b-1", False, False, "not approved for restricted data"),
    ("gamma", "public", "g-1", False, True, ""),                                    # an experimental route is a label, not a guard: nothing in the code keeps data from it
    ("gamma", "public", "g-1", True, True, ""),
    ("delta", "public", "d-1", False, False, "disabled"),
    ("claude", "restricted", "claude-sonnet-5-5", False, True, ""),
    ("claude", "restricted", "claude-opus-5-5", False, False, "not in the approved-model allowlist"),
])
def test_eligibility_by_configuration(tmp_path, pid, data_class, model, experimental_ok, ok, why):
    p, _ = write(tmp_path)
    got_ok, got_why = R.load_registry(p).eligibility(pid, data_class, experimental_ok=experimental_ok, model=model)
    assert got_ok is ok and why in got_why


def test_asking_for_an_unknown_provider_says_so(tmp_path):
    p, _ = write(tmp_path)
    with pytest.raises(R.RegistryError, match="not in ai_providers.yaml"):
        R.load_registry(p).provider("myspace")


# ---- the lanes -------------------------------------------------------------------------------------------------------------------

def lanes(tmp_path, fn):
    p, l = write(tmp_path, lanes=edited(LANES, fn))
    return L.load_lanes(l, R.load_registry(p))


def test_valid_lanes_load_with_their_defaults(tmp_path):
    p, l = write(tmp_path)
    got = L.load_lanes(l, R.load_registry(p))
    assert set(got) == {"pub", "internal", "secret", "exp", "noexp", "sampled", "confident"}
    assert got["pub"].claude_model == "claude-haiku-5-5" and got["pub"].chain[0] == {"provider": "alpha", "model": "a-1", "gateway_model": "a-1"}
    assert got["secret"].claude_leads and got["secret"].review == "required" and got["sampled"].review_sample_every == 1 and got["confident"].min_confidence == 0.6


@pytest.mark.parametrize("fn, fragment", [
    (lambda d: d["lanes"]["pub"].update(colour="red"), "unknown setting"),
    (lambda d: d["lanes"]["pub"].update(max_data_class="secret"), "not a data class"),
    (lambda d: d["lanes"]["pub"].update(review="maybe"), "review must be"),
    (lambda d: d["lanes"]["pub"]["output"].update(format="xml"), "output.format"),
    (lambda d: d["lanes"]["pub"]["chain"].append({"provider": "nobody", "model": "x"}), "not in ai_providers.yaml"),
    (lambda d: d["lanes"]["pub"]["chain"].append({"provider": "alpha", "model": "a-2"}), "not an approved model"),
    (lambda d: d["lanes"]["pub"]["chain"].append({"provider": "alpha", "model": "never-listed"}), "not an approved model"),
    (lambda d: d["lanes"]["pub"]["chain"].append({"provider": "alpha"}), "needs provider and model"),
    (lambda d: d["lanes"]["pub"]["chain"].append({"provider": "alpha", "model": "a-1", "x": 1}), "needs provider and model"),
    (lambda d: d["lanes"]["pub"].update(claude_leads=True), "claude_leads needs a claude route"),
    (lambda d: d["lanes"]["pub"].update(claude_model="claude-opus-5-5"), "not an approved Claude model"),
    (lambda d: d["lanes"]["pub"].update(min_confidence=2), "between 0 and 1"),
    (lambda d: d["lanes"]["pub"].update(min_confidence=True), "between 0 and 1"),
    (lambda d: d.update(version=2), "needs version: 1"),
    (lambda d: d.update(lanes={}), "needs version: 1"),
])
def test_a_mistake_in_the_lanes_is_refused_with_a_reason(tmp_path, fn, fragment):
    with pytest.raises(L.LaneError, match=fragment):
        lanes(tmp_path, fn)


def test_missing_lanes_file_is_refused(tmp_path):
    with pytest.raises(L.LaneError, match="is missing"):
        L.load_lanes(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("a: [", encoding="utf-8")
    with pytest.raises(L.LaneError, match="could not be read"):
        L.load_lanes(tmp_path / "bad.yaml")


# ---- the shipped files -----------------------------------------------------------------------------------------------------------

ROLLED_OUT = ["cloudflare", "cohere", "google", "groq", "kilo", "mistral", "nvidia", "ollama", "openrouter", "zhipu"]        # the providers the owner has approved (Update 57 added cohere, kilo, nvidia, ollama and zhipu)


def test_the_shipped_registry_enables_only_checked_providers_and_claude():
    reg = R.load_registry(SHIPPED_P)
    assert len(reg.providers) == 86
    assert sorted(p.id for p in reg.providers.values() if p.enabled) == sorted(ROLLED_OUT + ["claude"])
    for p in reg.providers.values():
        if p.retired:
            assert p.id == "github" and not p.enabled and not p.direct and not p.credential_env, p.id                 # the record keeps its history (approved classes, owner decision); nothing is switched on
        elif p.id not in ROLLED_OUT + ["claude"]:
            assert not p.enabled and p.approved_classes == () and p.effective_ceiling == "public" and not p.override and not p.paid, p.id
        if p.enabled:
            assert p.terms.get("status") not in ("unverified", "declared_only"), p.id        # the registry itself refuses otherwise
    assert {p.id for p in reg.providers.values() if p.experimental} == {"kilo", "ovh", "aihorde"}
    assert [p.id for p in reg.providers.values() if p.paid] == ["claude"]


def test_each_rolled_out_provider_carries_a_dated_named_owner_decision_for_internal_data_and_never_restricted():
    reg = R.load_registry(SHIPPED_P)
    for pid in [x for x in ROLLED_OUT if x != "kilo"]:                                                     # Kilo trains on prompts: Public data only, no override (see the next test)
        p = reg.provider(pid)
        assert p.approved_classes == ("public", "internal") and p.effective_ceiling == "internal", pid
        assert p.override["approved_by"] == "Harish" and p.override["date"] in ("2026-11-03", "2026-11-14") and p.override["ceiling"] == "internal", pid
        assert "Restricted data" in p.override["reason"] and "never sent" in p.override["reason"] and "credentials" in p.override["reason"], pid
        assert "restricted" not in p.approved_classes


def test_free_providers_can_only_route_to_models_the_owner_listed_as_free_to_use():
    reg = R.load_registry(SHIPPED_P)
    for pid in ROLLED_OUT:
        p = reg.provider(pid)
        assert not p.paid and not getattr(p, "price_per_million", None)
        approved = [m.id for m in p.models if m.approved]
        assert approved, pid
        if pid == "openrouter":
            assert all(m.endswith(":free") or m == "openrouter/free" for m in approved), approved                  # OpenRouter charges for models without the :free suffix (its free router is the one exception)
    assert [m.id for m in reg.provider("claude").models if m.approved] == ["claude-haiku-5-5", "claude-sonnet-5-5"]


def test_the_spending_cap_is_the_owners_ten_dollars_a_month_and_fifty_cents_a_day():
    reg = R.load_registry(SHIPPED_P)
    assert (reg.daily_usd, reg.monthly_usd) == (0.5, 10.0)
    assert 0 < reg.daily_usd <= reg.monthly_usd <= 10


def test_claude_is_the_only_provider_that_may_receive_restricted_data_and_unapproved_models_are_off():
    reg = R.load_registry(SHIPPED_P)
    claude = reg.provider("claude")
    assert claude.approved_classes == ("public", "internal", "restricted") and claude.effective_ceiling == "restricted"
    approved = {m.id: m.price for m in claude.models if m.approved}
    assert approved == {"claude-haiku-5-5": {"input": 0.1, "output": 0.5}, "claude-sonnet-5-5": {"input": 2.0, "output": 10.0}}
    assert {m.id for m in claude.models if not m.approved} == {"claude-opus-5-5", "claude-fable-5-1"}


def test_every_platform_in_the_pinned_gateway_is_in_the_registry_and_nowhere_else():
    reg = R.load_registry(SHIPPED_P)
    ids = {p["id"] for p in INVENTORY["platforms"]}
    through_gateway = {pid for pid, p in reg.providers.items() if not p.direct and not p.retired}
    assert len(ids) == 84 and through_gateway == (ids - {"openrouter"}) | {"claude"} and {pid for pid, p in reg.providers.items() if p.direct} == {"openrouter"}      # OpenRouter is called directly (GitHub Models, retired, has no endpoint)
    for pid in ids:
        assert reg.provider(pid).gateway_platform == pid


ADDED = {"kilo": {"kilo-auto/free", "nvidia/nemotron-3.5-super-vl:free"}}


def test_the_registry_allowlist_is_a_subset_of_the_models_the_pinned_gateway_ships_except_the_two_added_through_the_gateway():
    reg = R.load_registry(SHIPPED_P)
    shipped = {p["id"]: {m["modelId"] for m in p["models"]} for p in INVENTORY["platforms"]}
    for p in reg.providers.values():
        if p.id == "claude" or p.direct or p.retired:                                       # a retired provider is a record: the pinned gateway no longer ships it
            continue
        extra = {m.id for m in p.models} - shipped[p.id]
        assert extra == ADDED.get(p.id, set()), p.id                                           # only Kilo's two free routes are not in the pinned catalogue; the gateway's add-model call adds them
        assert all("add-model call adds it" in m.note for m in p.models if m.id in extra), p.id
    assert sum(len(p.models) for p in reg.providers.values() if p.id != "claude" and not p.direct and not p.retired) == 87


def test_the_shipped_lanes_load_against_the_allowlist_and_cover_the_ten_kinds_of_work_and_the_two_platform_lanes():
    reg = R.load_registry(SHIPPED_P)
    got = L.load_lanes(SHIPPED_L, reg)
    assert list(got) == ["research_extraction", "competitor_intelligence", "audience_voice", "formula_analysis", "trend_opportunity", "strategic_reasoning",
                         "content_ideation", "brief_generation", "production_support", "validation_qa", "general_text", "general_json"]       # the ten kinds of work, then the two platform lanes
    assert got["general_text"].max_data_class == "internal" and got["general_json"].max_data_class == "internal" and not got["general_text"].claude_fallback and not got["general_json"].claude_fallback
    assert got["general_text"].review == "none" and got["general_json"].review == "none" and all(r["provider"] != "claude" for r in got["general_text"].chain + got["general_json"].chain)
    for lane in got.values():
        for r in lane.chain:
            assert reg.model_ok(r["provider"], r["model"]), (lane.name, r)
    assert got["brief_generation"].review == "required" and got["brief_generation"].claude_leads and got["strategic_reasoning"].review == "required"
    assert all(r["provider"] == "claude" for r in got["brief_generation"].chain + got["strategic_reasoning"].chain + got["validation_qa"].chain)


def test_restricted_data_only_ever_reaches_claude_in_the_shipped_configuration():
    reg = R.load_registry(SHIPPED_P)
    for lane in L.load_lanes(SHIPPED_L, reg).values():
        if lane.max_data_class == "restricted":
            assert all(r["provider"] == "claude" for r in lane.chain) and not lane.allow_experimental, lane.name
    for p in reg.providers.values():
        assert (p.id == "claude") or not p.can_receive("restricted")


def test_the_provider_file_documents_how_to_switch_one_on():
    text = SHIPPED_P.read_text(encoding="utf-8")
    assert "owner_override" in text and "enabled: true" in text and "Restricted data can be approved for claude only" in text
    assert yaml.safe_load(text)["version"] == 1


def test_a_provider_that_says_nothing_is_off_and_public_only_and_has_no_approvals(tmp_path):
    p, _ = write(tmp_path, providers={"version": 1, "providers": {"bare": {"label": "Bare", "terms": {"status": "verified"}}}})
    bare = R.load_registry(p).provider("bare")
    assert bare.enabled is False and bare.ceiling == "public" and bare.effective_ceiling == "public" and bare.approved_classes == () and not bare.paid and not bare.experimental
    assert not bare.can_receive("public") and bare.models == () and bare.gateway_platform == "bare" and bare.credential_env == ()


def test_both_the_terms_ceiling_and_a_persons_approval_are_needed_to_receive_data(tmp_path):
    import dataclasses
    p, _ = write(tmp_path, providers=edited(PROVIDERS, lambda d: d["providers"]["beta"].update(approved_classes=["public"])))
    beta = R.load_registry(p).provider("beta")                     # the owner raised the ceiling to internal but approved public only
    assert beta.effective_ceiling == "internal" and not beta.can_receive("internal") and beta.can_receive("public")
    alpha = R.load_registry(write(tmp_path)[0]).provider("alpha")
    forced = dataclasses.replace(alpha, approved_classes=("public", "internal"))      # even a provider built with a wrong approval cannot exceed its ceiling
    assert forced.effective_ceiling == "public" and not forced.can_receive("internal") and forced.can_receive("public")


def test_a_lane_that_says_nothing_may_carry_public_data_only(tmp_path):
    p, l = write(tmp_path, lanes={"version": 1, "lanes": {"bare": {"chain": [{"provider": "alpha", "model": "a-1"}]}}})
    bare = L.load_lanes(l, R.load_registry(p))["bare"]
    assert bare.max_data_class == "public" and bare.review == "none" and not bare.claude_fallback and not bare.claude_leads and bare.output == {"format": "text"}
    assert bare.max_tokens == 1200 and bare.min_confidence == 0 and bare.forbid == () and bare.review_sample_every == 0


def test_the_documents_and_the_file_header_name_exactly_the_providers_that_are_switched_on():
    """Regression: the comparison document said 'all disabled' and the README said 84 platforms while five providers were on."""
    import re
    reg = R.load_registry(SHIPPED_P)
    on = sorted(p.id for p in reg.providers.values() if p.enabled)
    for path, marker in (("ai_providers.yaml", r"# Switched on at present: ([a-z0-9, ]+?)\.? A test"), ("docs/ai/provider-comparison.md", r"\*\*Switched on at present \(read from `ai_providers.yaml`\): ([a-z0-9, ]+?)\.\*\*")):
        text = open(ROOT / path, encoding="utf-8").read()
        found = re.search(marker, text)
        assert found, path
        named = sorted(x.strip() for x in re.split(r",| and ", found.group(1)) if x.strip())
        assert named == on, (path, named, on)
    assert f"{len(reg.providers)} in all" in open(ROOT / "README.md", encoding="utf-8").read()
    assert "all disabled until you approve" not in open(ROOT / "docs/ai/provider-comparison.md", encoding="utf-8").read()


# ---- the shipped catalogue never approves or routes to a model its provider has retired --------------------------------------------------------------

RETIRED = {("groq", "llama-3.3-70b-versatile"), ("groq", "llama-3.1-8b-instant"), ("groq", "qwen/qwen3-32b"), ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"), ("google", "gemini-2.5-flash"), ("openrouter", "openai/gpt-oss-120b:free"), ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"), ("openrouter", "qwen/qwen3-next-80b-a3b-instruct:free")}


def test_no_retired_model_is_approved_and_every_lane_route_uses_an_approved_model():
    import yaml
    providers = yaml.safe_load(open(ROOT / "ai_providers.yaml", encoding="utf-8"))["providers"]
    lanes = yaml.safe_load(open(ROOT / "ai_lanes.yaml", encoding="utf-8"))["lanes"]
    approved = {(pid, m["id"]) for pid, p in providers.items() for m in (p.get("models") or []) if m.get("approved")}
    assert not RETIRED & approved                                                           # a provider that has shut a model down is never offered it
    used = {(r["provider"], r["model"]) for lane in lanes.values() for r in lane.get("chain", [])}
    assert not RETIRED & used and used <= approved                                          # and no lane routes to a model nobody approved
    assert {("groq", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b"), ("google", "gemini-3.5-flash"), ("openrouter", "openrouter/free")} <= approved
    assert [r["model"] for lane in lanes.values() for r in lane.get("chain", []) if r["provider"] == "openrouter"] == ["openrouter/free"] * 3     # OpenRouter free models rotate every few days: the lane asks for the free router and the client picks today's free models


# ---- Update 57: the five providers added, and what is recorded for each ------------------------------------------------------------------------------

def test_kilo_is_enabled_like_the_others_with_internal_open_one_model_in_the_pool_and_the_owner_switch_named_in_its_record():
    p = R.load_registry(SHIPPED_P).provider("kilo")
    assert p.enabled and p.experimental and p.approved_classes == ("public", "internal") and p.effective_ceiling == "internal" and p.credential_env == ()
    assert p.override["approved_by"] == "Harish" and p.override["date"] == "2026-11-14" and "Internal stays open everywhere" in p.override["reason"] and "Restricted data" in p.override["reason"]
    assert [m.id for m in p.models if m.approved] == ["kilo-auto/free", "nvidia/nemotron-3.5-super-vl:free"] and len(p.models) == 6               # Kilo answered 404 for its nemotron model (live check #22): off; its other three are candidates
    assert p.terms["trains_on_data"].startswith("yes") and "Public and Internal data" in p.terms["note"] and "services.ai_internal_data.channels.kilo" in p.terms["note"] and "internal: false" in p.terms["note"]
    assert p.limits == {"requests_per_hour": 200} and "Retry-After" in p.terms["note"]


def test_nvidia_ollama_cohere_and_zhipu_carry_the_personal_use_decision_dated_with_the_commercial_use_question_answered():
    reg = R.load_registry(SHIPPED_P)
    for pid in ("nvidia", "ollama", "cohere", "zhipu"):
        p = reg.provider(pid)
        assert p.enabled and p.terms["status"] == "owner_decision" and p.terms["date"] == "2026-11-14", pid
        assert p.terms["business_use"].startswith("Commercial use allowed?"), pid                              # the question is asked and answered in every record
        assert "personal" in p.terms["business_use"] and "personal" in p.terms["note"].lower() or pid == "zhipu", pid
        assert "if one is, the owner stops" in p.terms["note"] or "owner stops" in p.terms["note"], pid
        assert p.approved_classes == ("public", "internal") and p.override["date"] == "2026-11-14" and p.override["approved_by"] == "Harish", pid
        assert sum(1 for m in p.models if m.approved) == {"nvidia": 1, "ollama": 7, "cohere": 4, "zhipu": 3}[pid] and len(p.models) == {"nvidia": 13, "ollama": 11, "cohere": 4, "zhipu": 3}[pid], pid      # the rest were switched off after the live check of 2026-11-14 (see test_stale_models)
        assert p.credential_env == ({"nvidia": "NVIDIA_API_KEY", "ollama": "OLLAMA_API_KEY", "cohere": "COHERE_API_KEY", "zhipu": "ZHIPU_API_KEY"}[pid],), pid
    z = reg.provider("zhipu").terms
    assert z["note"].startswith("DATA RESIDENCY IS IN CHINA") and "before any client data goes to it" in z["note"] and "z.ai" in z["note"]
    assert "not permitted for production or commercial use" in reg.provider("cohere").terms["business_use"] and "decision for later" in reg.provider("cohere").terms["business_use"]
    assert "production use needs an NVIDIA AI Enterprise licence" in reg.provider("nvidia").terms["business_use"] and "may end the free service at any time" in reg.provider("nvidia").terms["business_use"]


def test_each_new_providers_limits_are_its_own_stated_ones_and_nothing_of_ours():
    reg = R.load_registry(SHIPPED_P)
    assert reg.provider("nvidia").limits == {"requests_per_minute": 30}                                      # a SETTING, started conservatively: sources say 30 to 40 and it varies by model and load
    assert reg.provider("ollama").limits == {"concurrency": 1}                                               # one request at a time; no rate figure is published, so none is invented
    assert reg.provider("cohere").limits == {"requests_per_minute": 20, "requests_per_month": 1000}           # the trial key's own stated limits
    assert reg.provider("zhipu").limits == {"requests_per_minute": 60}
    for pid in ("nvidia", "ollama", "cohere", "zhipu", "kilo"):
        assert not {"requests_per_day", "tokens_per_day"} & set(reg.provider(pid).limits), pid                 # no daily budget of ours on any of them
    assert reg.pacing == {"fast_wait_seconds": 2, "max_wait_seconds": 120} and reg.daily_usd == 0.5 and reg.monthly_usd == 10      # and the Claude spending cap is untouched


def test_the_registry_accepts_the_new_limits_and_a_pacing_section_and_refuses_nonsense(tmp_path):
    base = {"version": 1, "budgets": {"daily_usd": 0.5, "monthly_usd": 10}, "providers": {"p": {"label": "P", "enabled": False, "limits": {"requests_per_minute": 5, "requests_per_hour": 100, "requests_per_month": 900, "concurrency": 2}}}}
    path = tmp_path / "p.yaml"
    import yaml as _y
    path.write_text(_y.safe_dump(base), encoding="utf-8")
    assert R.load_registry(path).provider("p").limits == {"requests_per_minute": 5, "requests_per_hour": 100, "requests_per_month": 900, "concurrency": 2} and R.load_registry(path).pacing == {"fast_wait_seconds": 2, "max_wait_seconds": 120}
    path.write_text(_y.safe_dump({**base, "pacing": {"fast_wait_seconds": 5, "max_wait_seconds": 30}}), encoding="utf-8")
    assert R.load_registry(path).pacing == {"fast_wait_seconds": 5, "max_wait_seconds": 30}
    for bad_limits in ({"concurrency": 0}, {"concurrency": 1.5}, {"requests_per_minute": -1}, {"requests_per_second": 1}):
        path.write_text(_y.safe_dump({**base, "providers": {"p": {"label": "P", "limits": bad_limits}}}), encoding="utf-8")
        with __import__("pytest").raises(R.RegistryError):
            R.load_registry(path)
    for bad_pacing in ({"max_wait_seconds": -1}, {"hold_seconds": 3}, "slow"):
        path.write_text(_y.safe_dump({**base, "pacing": bad_pacing}), encoding="utf-8")
        with __import__("pytest").raises(R.RegistryError):
            R.load_registry(path)
