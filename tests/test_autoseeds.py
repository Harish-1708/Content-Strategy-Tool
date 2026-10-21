import json
import shutil
from dataclasses import replace

import pytest
import yaml

from contentintel import autoseeds as A
from contentintel import browser as B
from contentintel import claude_client as CC
from contentintel import cli
from contentintel import collectors as C
from contentintel import providers as P
from contentintel import research as R
from contentintel.store import Store
from contentintel.sync import confirm_baseline, run_sync

from conftest import BRANDS_DIR, raw_products
from fake_browser import FakeClaude
from fake_http import FakeHttp
from test_collectors import BROWSER_YAML, NOW, world, write_research

T0 = "2026-10-20T07:00:00Z"
SEEDS = {"context": "Swoveralls sells sweatpant overalls at $99-$159.",
         "keywords": {"direct": ["sweatpant overalls", "jogger overalls"], "adjacent": ["fleece loungewear", "comfy jumpsuit"],
                      "cross_industry": ["Oodie", "Gymshark"]},
         "tiktok_hashtags": ["#swoveralls", "sweatpantoveralls"], "instagram_hashtags": ["swoveralls"],
         "questions": ["Which ad hooks, formats and angles are performing best right now across the best consumer brands in any industry, "
                       "and how could Swoveralls adapt them?", "Who are the main competitors to Swoveralls and what angles do they use?"]}


@pytest.fixture
def settings():
    return C.load_collector_settings("does-not-exist.yaml")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


def seeded(tmp_path, brand, fixture="swoveralls_full_72.json", now=T0):
    store = Store(tmp_path / "data")
    result = run_sync(brand, store, raw_products=raw_products(fixture), now=now)
    confirm_baseline(brand.slug, store, result.count, "tester", now)
    return store


def ledger(store, month="2026-10"):
    return R.Ledger(store.load_usage(), month)


def ensure(brand, store, claude, settings, now=NOW, **kw):
    return A.ensure_auto_seeds(brand, store, claude, settings, ledger(store), now, **kw)


def claude_with(*completions, **kw):
    c = FakeClaude(**kw)
    c.completions = [x if isinstance(x, str) else json.dumps(x) for x in completions]
    return c


# --- the profile, built from the real catalogue --------------------------------------------------------------------------

def profile_of(tmp_path, brand, fixture="swoveralls_full_72.json"):
    store = seeded(tmp_path, brand, fixture)
    return A.brand_profile(brand, store.load_latest(brand.slug))


def test_the_profile_describes_what_the_real_swoveralls_catalogue_sells_today(tmp_path, swov):
    p = profile_of(tmp_path, swov)
    assert p["name"] == "Swoveralls" and p["live_products"] == 61 and sorted(p["categories"]) == ["core", "kids", "merch"]
    assert (p["categories"]["kids"]["price_low"], p["categories"]["merch"]["price_low"], p["categories"]["merch"]["price_high"]) == (49, 25, 36)
    assert p["pocket_claims"] == ["5", "6", "7"] and "polyester" in p["materials"] and "poly" not in p["materials"]
    assert "Swovie Shorts" in p["categories"]["core"]["families"] and p["categories"]["core"]["limited_edition"] > 10


def test_only_things_on_sale_or_coming_are_in_the_profile(tmp_path, swov):
    p = profile_of(tmp_path, swov)
    everything = " ".join(f for c in p["categories"].values() for f in c["families"])
    assert "Sidekick Crew" not in everything and "Gift Card" not in everything and "Personalization" not in everything and "Sticker" not in everything


def test_the_profile_text_is_plain_stable_and_changes_when_the_catalogue_does(tmp_path, swov):
    store = seeded(tmp_path, swov)
    one = A.profile_text(A.brand_profile(swov, store.load_latest("swoveralls")))
    two = A.profile_text(A.brand_profile(swov, store.load_latest("swoveralls")))
    assert one == two and A.profile_hash(one) == A.profile_hash(two) and len(A.profile_hash(one)) == 16
    assert one.splitlines()[0] == "Brand: Swoveralls (swoveralls.com), a shopify store with 61 products on sale or coming soon."
    assert "kids: 4 products, $49." in one and "Pocket claims: 5, 6, 7." in one and "Materials seen:" in one
    raw = raw_products("swoveralls_full_72.json")
    raw[0]["variants"][0]["price"] = "999.00"
    run_sync(swov, store, raw_products=raw, now="2026-10-21T07:00:00Z")
    changed = A.profile_text(A.brand_profile(swov, store.load_latest("swoveralls")))
    assert A.profile_hash(changed) != A.profile_hash(one)


def test_every_brand_gets_a_profile_without_any_hand_written_context(tmp_path, brands):
    for slug, fixture in (("swoveralls", "swoveralls_full_72.json"), ("duderobe", "duderobe_fetch_a.json"), ("defi_snacks", "defi_snacks.json")):
        p = profile_of(tmp_path / slug, brands[slug], fixture)
        assert p["live_products"] > 0 and p["categories"], slug


# --- reading Claude's answer ------------------------------------------------------------------------------------------------

def parse(text, settings, **over):
    return A.parse_seeds(text, replace(settings, **over))


def test_the_json_is_found_inside_fences_and_chatter(settings):
    plain = json.dumps(SEEDS)
    for wrapped in (plain, f"```json\n{plain}\n```", f"Here you go:\n{plain}\nHope that helps!", f"```\n{plain}\n```"):
        assert parse(wrapped, settings)["keywords"]["direct"] == ["sweatpant overalls", "jogger overalls"]


def test_every_value_is_tidied_deduplicated_and_cut_down(settings):
    messy = {"context": "  a\n\n b ", "keywords": {"direct": ["  Sweatpant   Overalls ", "sweatpant overalls", "#hashed", 5, None, "", "x" * 200],
                                                    "adjacent": "not a list"},
             "tiktok_hashtags": ["#One", "@two", "one", "three", "four", "five", "six", "seven"], "instagram_hashtags": [], "questions": []}
    out = parse(json.dumps(messy), settings)
    assert out["keywords"]["direct"] == ["Sweatpant Overalls", "hashed", "x" * 60] and out["keywords"]["adjacent"] == []
    assert out["tiktok_hashtags"] == ["One", "two", "three", "four", "five"] and out["context"] == "a b"


def test_the_keyword_limit_is_shared_between_the_three_kinds(settings):
    many = {s: [f"{s}{i}" for i in range(6)] for s in A.SCOPES}
    out = parse(json.dumps({"keywords": many}), settings, auto_max_keywords=8)
    assert [len(out["keywords"][s]) for s in A.SCOPES] == [3, 3, 2] and out["keywords"]["direct"][:2] == ["direct0", "direct1"]
    assert sum(len(v) for v in parse(json.dumps({"keywords": many}), settings, auto_max_keywords=2)["keywords"].values()) == 2


def test_questions_that_are_too_short_are_dropped_and_the_rest_capped(settings):
    q = {"keywords": {"direct": ["a"]}, "questions": ["Too short?", "x" * 600, "A real research question about advertising?", "Another real research question here?"]}
    out = parse(json.dumps(q), settings, auto_max_questions=2)
    assert len(out["questions"]) == 2 and out["questions"][0] == "x" * 500 and out["questions"][1].startswith("A real research")
    assert parse(json.dumps(q), settings, auto_max_questions=0)["questions"] == []


def test_the_context_is_cut_to_a_sensible_length(settings):
    assert len(parse(json.dumps({"context": "w " * 500, "keywords": {"direct": ["a"]}}), settings)["context"]) == 600


@pytest.mark.parametrize("bad", ["not json at all", "[1, 2, 3]", '{"keywords": ["a"]}', '{"keywords": {}}', '{"keywords": {"direct": []}}',
                                 '{"keywords": {"direct": [5, null]}}', "", None, '{"keywords": {"direct": ["a"]}'])
def test_an_answer_that_cannot_be_used_is_refused(settings, bad):
    with pytest.raises(A.AutoSeedError):
        A.parse_seeds(bad, settings)


def test_with_no_claude_the_fallback_is_the_brand_s_own_name(swov):
    f = A.fallback_seeds(swov, "profile text")
    assert f == {"context": "profile text", "keywords": {"direct": ["Swoveralls"], "adjacent": [], "cross_industry": []},
                 "tiktok_hashtags": ["swoveralls"], "instagram_hashtags": ["swoveralls"], "questions": []}
    assert A.fallback_seeds(replace(swov, name="Defi Snacks!"), "t")["tiktok_hashtags"] == ["defisnacks"]


# --- when seeds are written, reused and refreshed -----------------------------------------------------------------------------

def test_the_first_run_asks_claude_once_with_the_profile_and_saves_what_it_says(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    claude.model = "claude-test-model"
    result = ensure(swov, store, claude, settings)
    assert (result.source, result.note) == ("claude", "regenerated (none existed yet)") and result.seeds["keywords"]["direct"][0] == "sweatpant overalls"
    (system, user, max_tokens), = claude.complete_calls
    assert system == A.SEED_SYSTEM and max_tokens == 1600
    assert result.profile in user and "Brand: Swoveralls" in user and "at most 8 keywords in total, 5 hashtags per platform and 2 questions" in user
    saved = store.read_auto_seeds("swoveralls")
    assert saved["generated_at"] == NOW and saved["profile_hash"] == A.profile_hash(result.profile) and saved["model"] == "claude-test-model"
    assert saved["seeds"] == result.seeds and store.load_usage()["months"]["2026-10"] == {"claude_calls": 1}


def test_seeds_are_reused_while_the_catalogue_is_the_same_and_they_are_recent(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    first = ensure(swov, store, claude, settings)
    again = ensure(swov, store, claude, settings, now="2026-11-10T07:00:00Z")
    assert again.source == "cache" and again.note == "reused (written 2026-10-20)" and again.seeds == first.seeds and len(claude.complete_calls) == 1


def test_seeds_are_regenerated_when_the_catalogue_changes(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS, {**SEEDS, "keywords": {"direct": ["new keyword"]}})
    ensure(swov, store, claude, settings)
    raw = raw_products("swoveralls_full_72.json")
    raw[0]["variants"][0]["price"] = "999.00"
    run_sync(swov, store, raw_products=raw, now="2026-10-22T07:00:00Z")
    changed = ensure(swov, store, claude, settings, now="2026-10-22T08:00:00Z")
    assert changed.note == "regenerated (the catalogue changed)" and changed.seeds["keywords"]["direct"] == ["new keyword"] and len(claude.complete_calls) == 2


def test_old_seeds_are_regenerated_after_the_refresh_period(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS, SEEDS)
    ensure(swov, store, claude, settings)
    assert ensure(swov, store, claude, settings, now="2026-11-19T06:59:59Z").source == "cache"          # one second short of 30 days
    late = ensure(swov, store, claude, settings, now="2026-11-19T07:00:00Z")                           # exactly 30 days
    assert late.note == "regenerated (they are old)" and len(claude.complete_calls) == 2


def test_force_regenerates_even_fresh_seeds(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS, SEEDS)
    ensure(swov, store, claude, settings)
    assert ensure(swov, store, claude, settings, force=True).source == "claude" and len(claude.complete_calls) == 2


def test_without_a_claude_key_old_seeds_are_kept_and_otherwise_the_profile_alone_is_used(tmp_path, swov, settings):
    store = seeded(tmp_path, swov)
    none_yet = ensure(swov, store, None, settings)
    assert none_yet.source == "profile-only" and "ANTHROPIC_API_KEY is not set; using profile-only seeds" in none_yet.note
    assert none_yet.seeds["context"] == none_yet.profile and store.read_auto_seeds("swoveralls") is None
    ensure(swov, store, claude_with(SEEDS), settings)
    kept = ensure(swov, store, None, settings, now="2027-01-01T07:00:00Z")
    assert kept.source == "cache" and "they are old" in kept.note and kept.seeds["keywords"]["direct"][0] == "sweatpant overalls"


def test_a_used_up_claude_allowance_keeps_what_there_is(tmp_path, swov, settings):
    store = seeded(tmp_path, swov)
    store.save_usage({"months": {"2026-10": {"claude_calls": 60}}, "lifetime": {}})
    claude = claude_with(SEEDS)
    result = ensure(swov, store, claude, settings)
    assert "the monthly Claude allowance is used up; using profile-only seeds" in result.note and claude.complete_calls == []


@pytest.mark.parametrize("failure", [P.ProviderError("Claude: busy"), P.AuthError("Claude rejected the API key"), P.QuotaError("Claude: limit"), "garbage"])
def test_when_claude_fails_the_previous_seeds_survive_and_nothing_is_counted(tmp_path, swov, settings, failure):
    store = seeded(tmp_path, swov)
    ensure(swov, store, claude_with(SEEDS), settings)
    before = store.read_auto_seeds("swoveralls")
    claude = claude_with(failure) if isinstance(failure, str) else FakeClaude()
    if not isinstance(failure, str):
        claude.fail_complete = failure
    result = ensure(swov, store, claude, settings, now="2026-12-30T07:00:00Z")
    assert result.source == "cache" and "could not be regenerated" in result.note and result.seeds == before["seeds"]
    assert store.read_auto_seeds("swoveralls") == before and store.load_usage()["months"]["2026-10"] == {"claude_calls": 1}


def test_a_dry_run_never_calls_claude_or_writes_anything(tmp_path, swov, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    result = ensure(swov, store, claude, settings, dry_run=True)
    assert result.note.startswith("would be regenerated (none existed yet)") and claude.complete_calls == [] and store.read_auto_seeds("swoveralls") is None
    assert store.load_usage() == {"months": {}, "lifetime": {}}


def test_a_brand_with_no_catalogue_yet_has_no_seeds_and_no_noise(tmp_path, swov, settings):
    store = Store(tmp_path / "data")
    assert ensure(swov, store, claude_with(SEEDS), settings) == A.SeedResult(None, "", "", "none") and not (tmp_path / "data").exists()


def test_advertisers_already_seen_are_ranked_and_given_to_claude(tmp_path, swov, settings):
    store = seeded(tmp_path, swov)
    def ad(n, who, days):
        return C._row("meta_ads", n, handle=who, days_running=days, text=f"t{n}")
    store.append_collected("swoveralls", {"at": T0, "run": "r", "source": "meta_ads", "key": "k", "seeds": "s", "items": [
        ad(1, "Rebel Babe", 150), ad(1, "Rebel Babe", 150), ad(2, "Rebel Babe", 20), ad(3, "Swoveralls", 99), ad(4, "swoveralls", 5), ad(5, "Quince", 400)]})
    store.append_collected("swoveralls", {"at": T0, "run": "r", "source": "tiktok", "key": "k2", "seeds": "s", "items": [C._row("tiktok", "9", handle="tiktoker")]})
    assert A.advertisers_seen(store.read_collected("swoveralls"), "Swoveralls") == [("Rebel Babe", 2, 150), ("Quince", 1, 400)]
    claude = claude_with(SEEDS)
    ensure(swov, store, claude, settings)
    assert "- Rebel Babe (2 ads found, longest running 150 days)" in claude.complete_calls[0][1] and "- Swoveralls (" not in claude.complete_calls[0][1]
    assert "(none yet)" in A._user_message("p", [], settings)


def test_the_printout_shows_the_profile_the_seeds_and_where_they_came_from(tmp_path, swov, settings):
    store = seeded(tmp_path, swov)
    text = A.format_seeds("swoveralls", ensure(swov, store, claude_with(SEEDS), settings))
    assert "=== swoveralls ===" in text and "Auto seeds regenerated (none existed yet)" in text and "Brand profile (built from the catalogue" in text
    assert "Meta ad keywords, direct: sweatpant overalls; jogger overalls" in text and "Meta ad keywords, cross industry: Oodie; Gymshark" in text
    assert "TikTok hashtags: #swoveralls, #sweatpantoveralls" in text and "Instagram hashtags: #swoveralls" in text
    assert "Context for Deep Research: Swoveralls sells" in text and "Deep Research question: Which ad hooks" in text
    assert "No catalogue yet" in A.format_seeds("x", A.SeedResult(None, "", "", "none"))


# --- merging with a brand's own seeds -------------------------------------------------------------------------------------------

CLEAN = A.parse_seeds(json.dumps(SEEDS), C.load_collector_settings("does-not-exist.yaml"))


def collect_cfg(tmp_path, brand, data):
    return R.load_brand_research(brand, write_research(tmp_path, brand, data))["collect"]


def test_a_brands_own_seeds_come_first_and_auto_seeds_add_without_repeating(tmp_path, swov, settings):
    cfg = collect_cfg(tmp_path, swov, {"ads": {"keywords": ["My Keyword", "swoveralls"]}, "social": {"tiktok": {"hashtags": ["mine"]}},
                                      "deep_research": {"questions": ["My own question about the market?"], "context": "Hand written."}})
    auto = {**CLEAN, "keywords": {**CLEAN["keywords"], "direct": ["SWOVERALLS", "sweatpant overalls"]}}
    merged = C.merge_auto_seeds(cfg, auto, settings)
    assert merged["ads"]["keywords"][:2] == ["My Keyword", "swoveralls"] and "SWOVERALLS" not in merged["ads"]["keywords"]
    assert merged["ads"]["keywords"][2:] == ["sweatpant overalls", "fleece loungewear", "comfy jumpsuit", "Oodie", "Gymshark"]
    assert merged["tiktok"]["hashtags"] == ["mine", "swoveralls", "sweatpantoveralls"] and merged["instagram"]["hashtags"] == ["swoveralls"]
    assert merged["deep_research"]["context"] == "Hand written." and merged["deep_research"]["questions"][0] == "My own question about the market?"
    assert len(merged["deep_research"]["questions"]) == 3 and cfg["ads"]["keywords"] == ["My Keyword", "swoveralls"]      # the original is untouched


def test_auto_seeds_supply_the_context_and_questions_a_brand_has_not_written(tmp_path, swov, settings):
    merged = C.merge_auto_seeds(collect_cfg(tmp_path, swov, {}), CLEAN, settings)
    assert merged["deep_research"]["context"] == CLEAN["context"] and merged["deep_research"]["questions"] == CLEAN["questions"]
    capped = C.merge_auto_seeds(collect_cfg(tmp_path / "b", swov, {"deep_research": {"questions": ["Mine one is a long question?"]}}), CLEAN, replace(settings, auto_max_questions=1))
    assert len(capped["deep_research"]["questions"]) == 2         # one of its own plus one automatic


def test_only_the_chosen_kinds_of_keyword_are_used(tmp_path, swov, settings):
    only = C.merge_auto_seeds(collect_cfg(tmp_path, swov, {}), CLEAN, replace(settings, auto_scopes=("direct", "cross_industry")))
    assert only["ads"]["keywords"] == ["sweatpant overalls", "jogger overalls", "Oodie", "Gymshark"]


def test_a_brand_can_switch_auto_seeds_off_and_nothing_changes(tmp_path, swov, settings):
    cfg = collect_cfg(tmp_path, swov, {"auto_seeds": False, "ads": {"keywords": ["mine"]}})
    assert cfg["auto_seeds"] is False and C.merge_auto_seeds(cfg, CLEAN, settings) is cfg
    assert C.merge_auto_seeds(cfg, None, settings) is cfg
    with pytest.raises(R.ResearchError, match="auto_seeds must be true or false"):
        collect_cfg(tmp_path / "bad", swov, {"auto_seeds": "no"})


def test_auto_seeds_become_collection_tasks_even_when_a_brand_has_written_nothing(tmp_path, swov, settings):
    tasks = C.plan_collections(collect_cfg(tmp_path, swov, {}), CLEAN, settings)
    by = {t.source: t for t in tasks if t.source != "deep_research"}
    assert [t.source for t in tasks] == ["meta_ads", "tiktok", "instagram", "deep_research", "deep_research"]
    assert len(by["meta_ads"].payload["urls"]) == 6 and "q=sweatpant+overalls" in by["meta_ads"].payload["urls"][0]
    assert by["tiktok"].payload == {"hashtags": ["swoveralls", "sweatpantoveralls"]} and tasks[3].payload["context"] == CLEAN["context"]
    assert C.plan_collections(collect_cfg(tmp_path / "e", swov, {})) == []


def test_the_task_key_changes_when_the_auto_seeds_change_so_new_seeds_are_collected(tmp_path, swov, settings):
    cfg = collect_cfg(tmp_path, swov, {})
    one = C.plan_collections(cfg, CLEAN, settings)[0]
    two = C.plan_collections(cfg, {**CLEAN, "keywords": {"direct": ["something else"]}}, settings)[0]
    assert one.key != two.key and one.key == C.plan_collections(cfg, CLEAN, settings)[0].key


# --- the settings --------------------------------------------------------------------------------------------------------------------

def test_auto_seed_settings_default_to_a_monthly_refresh_with_all_three_kinds(settings):
    assert (settings.auto_enabled, settings.auto_refresh_days, settings.auto_max_keywords, settings.auto_max_hashtags,
            settings.auto_max_questions, settings.auto_scopes) == (True, 30, 8, 5, 2, ("direct", "adjacent", "cross_industry"))


@pytest.mark.parametrize("data,match", [
    ({"enabled": "yes"}, "enabled must be true or false"), ({"scopes": []}, "scopes must be a list"), ({"scopes": "direct"}, "scopes must be a list"),
    ({"scopes": ["direct", "rivals"]}, "scopes must be a list"), ({"refresh_days": 0}, "refresh_days"), ({"max_keywords": 0}, "max_keywords"),
    ({"max_hashtags": 1.5}, "whole number"), ({"max_questions": -1}, "max_questions"), ({"colour": 1}, "unknown 'colour'")])
def test_bad_auto_seed_settings_are_rejected(tmp_path, data, match):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"auto_seeds": data}}))
    with pytest.raises(C.CollectError, match=match):
        C.load_collector_settings(path)


def test_zero_questions_is_allowed_and_a_longer_refresh_can_be_chosen(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"auto_seeds": {"max_questions": 0, "refresh_days": 90, "scopes": ["direct"]}}}))
    s = C.load_collector_settings(path)
    assert (s.auto_max_questions, s.auto_refresh_days, s.auto_scopes) == (0, 90, ("direct",))


def test_claude_can_be_asked_for_a_longer_answer_than_its_usual_limit():
    http = FakeHttp()
    http.on("POST", "https://api.anthropic.com/v1/messages", (200, {"content": [{"type": "text", "text": "x"}]}))
    claude = CC.Claude("k", http=http, max_tokens=700)
    claude.complete("s", "u")
    claude.complete("s", "u", max_tokens=1600)
    assert [c["json"]["max_tokens"] for c in http.calls] == [700, 1600]


# --- a whole collection run uses them -------------------------------------------------------------------------------------------------

def run(store, tmp_path, brand, cfg, settings, factory, claude, data=None, **kw):
    write_research(tmp_path, brand, data or {})
    return C.run_collect([brand], store, browser_factory=factory, creds={"meta_ads": {}}, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=["meta_ads"], now=NOW, run_id="run1", root=str(tmp_path / "brands"), claude=claude, **kw)


def test_a_brand_with_no_hand_written_seeds_is_still_researched_from_its_catalogue(tmp_path, swov, cfg, settings):
    store, claude, f = seeded(tmp_path, swov), claude_with(SEEDS), world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude)
    assert [r.status for r in result.brands["swoveralls"]] == ["ran"] and "swoveralls: auto seeds regenerated (none existed yet)" in result.notes
    opened = " ".join(f.browsers["meta_ads"].opened)
    assert "q=sweatpant+overalls" in opened and "q=Oodie" in opened and store.load_usage()["months"]["2026-10"]["claude_calls"] == 1


def test_the_second_run_reuses_the_seeds_without_calling_claude(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, world(cfg), claude)
    again = run(store, tmp_path / "again", swov, cfg, settings, world(cfg), claude)
    assert len(claude.complete_calls) == 1 and any("reused (written 2026-10-20)" in n for n in again.notes)


def test_a_dry_run_plans_from_the_saved_seeds_and_spends_nothing(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    ensure(swov, store, claude, settings)
    plan = run(store, tmp_path, swov, cfg, settings, world(cfg), claude, dry_run=True)
    assert plan.brands["swoveralls"][0].status == "would_run" and len(claude.complete_calls) == 1
    fresh = seeded(tmp_path / "other", swov)
    nothing = run(fresh, tmp_path / "o2", swov, cfg, settings, world(cfg), claude, dry_run=True)
    assert len(claude.complete_calls) == 1 and fresh.read_auto_seeds("swoveralls") is None and any("would be regenerated" in n for n in nothing.notes)


def test_a_brand_that_opts_out_or_a_global_off_switch_calls_no_claude(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    out = run(store, tmp_path, swov, cfg, settings, world(cfg), claude, data={"auto_seeds": False})
    assert out.brands["swoveralls"] == [] and claude.complete_calls == []
    off = run(seeded(tmp_path / "x", swov), tmp_path / "x2", swov, cfg, replace(settings, auto_enabled=False), world(cfg), claude)
    assert off.brands["swoveralls"] == [] and claude.complete_calls == []


def test_a_brand_with_hand_written_seeds_keeps_them_even_when_claude_is_unavailable(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, None, data={"ads": {"keywords": ["my own words"]}})
    opened = " ".join(f.browsers["meta_ads"].opened)
    assert "q=my+own+words" in opened and "q=Swoveralls" in opened                      # its own, plus the profile-only fallback
    assert any("ANTHROPIC_API_KEY is not set; using profile-only seeds" in n for n in result.notes)


# --- the command line -------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def seeds_cli(tmp_path, monkeypatch, swov):
    http = FakeHttp()
    http.on("POST", "https://api.anthropic.com/v1/messages", (200, {"content": [{"type": "text", "text": json.dumps(SEEDS)}]}))
    monkeypatch.setattr(P, "default_http", http)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    for name in ("ANTHROPIC_API_KEY", "APIFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")
    store = seeded(tmp_path, swov)

    def go(*args, key="sk-ant-KEYMARK"):
        if key:
            monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        return cli.main(["collect", *args, "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands"),
                         "--research-config", "research.yaml", "--browser-config", BROWSER_YAML])
    return go, http, store


def test_the_seeds_command_writes_and_shows_a_brands_seeds(seeds_cli, capsys):
    go, http, store = seeds_cli
    assert go("--seeds", "--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "Auto seeds regenerated (none existed yet)" in out and "Meta ad keywords, direct: sweatpant overalls" in out and "KEYMARK" not in out
    assert store.read_auto_seeds("swoveralls")["seeds"]["tiktok_hashtags"] == ["swoveralls", "sweatpantoveralls"] and len(http.calls) == 1


def test_the_seeds_command_always_refreshes_and_covers_every_brand_it_can(seeds_cli, capsys):
    go, http, store = seeds_cli
    go("--seeds", "--brand", "swoveralls")
    go("--seeds", "--brand", "swoveralls")
    assert len(http.calls) == 2                              # asking for seeds means asking again, even when they are fresh
    capsys.readouterr()
    assert go("--seeds") == 0
    out = capsys.readouterr().out
    assert "=== swoveralls ===" in out and out.count("No catalogue yet") == 2        # duderobe and defi_snacks have no data here


def test_the_seeds_command_without_a_key_shows_the_profile_alone(seeds_cli, capsys):
    go, http, store = seeds_cli
    assert go("--seeds", "--brand", "swoveralls", key=None) == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY is not set; using profile-only seeds" in out and "Brand: Swoveralls" in out and http.calls == []
    assert store.read_auto_seeds("swoveralls") is None


def test_the_workflow_has_a_seeds_mode_that_needs_no_browser_and_no_ticked_source():
    text = open(".github/workflows/collect.yml", encoding="utf-8").read()
    assert "seeds)              args+=(--seeds) ;;" in text and '[ "$MODE" != "seeds" ] && [ -z "$sources" ]' in text
    assert text.count("inputs.mode != 'plan' && inputs.mode != 'seeds'") == 2


# --- the store ------------------------------------------------------------------------------------------------------------------------

def test_reading_from_the_store_never_creates_folders_but_writing_still_does(tmp_path):
    store = Store(tmp_path / "data")
    for read in (store.load_latest, store.load_sheet_info, store.load_last_checked, store.read_changes, store.load_baseline, store.load_pending,
                 store.load_registry, store.read_failed, store.read_searches, store.read_research_runs, store.read_collected, store.read_auto_seeds):
        read("swoveralls")
    assert not (tmp_path / "data").exists()
    store.append_collected("swoveralls", {"at": T0, "items": []})
    assert (tmp_path / "data" / "swoveralls" / "research" / "collected.jsonl").is_file()
