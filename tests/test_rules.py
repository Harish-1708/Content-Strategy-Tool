import pytest
import yaml

from contentintel.rules import RuleError, check_product, check_text, load_ruleset

from conftest import BRIEFS_DIR, by_handle, by_title

SW = BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml"
DR = BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml"


@pytest.fixture
def sw_rules():
    return load_ruleset(SW)


@pytest.fixture
def dr_rules():
    return load_ruleset(DR)


def rules_of(violations):
    return sorted(v.rule for v in violations)


def test_em_dashes_and_exclamation_points_are_caught(sw_rules):
    found = check_text(sw_rules, "Sweatpants \u2014 but comfy!")
    assert rules_of(found) == ["banned_char", "banned_char"]
    assert {v.detail["char"] for v in found} == {"\u2014", "!"}


def test_clean_copy_passes(sw_rules):
    assert check_text(sw_rules, "The comfort of your favorite sweatpants.") == []


def test_ordering_rule_when_both_words_appear(sw_rules):
    assert check_text(sw_rules, "Sweatpants that happen to be overalls", scope="hook") == []
    bad = check_text(sw_rules, "Overalls that feel like sweatpants", scope="hook")
    assert rules_of(bad) == ["ordered_terms"]


def test_ordering_rule_ignores_words_inside_other_words(sw_rules):
    assert check_text(sw_rules, "sweatpantsish coveralls", scope="hook") == []


def test_ordering_rule_only_applies_to_its_scope(sw_rules):
    assert check_text(sw_rules, "Overalls that feel like sweatpants", scope="copy") == []


def test_the_two_readings_of_the_sweatpants_rule_really_differ(sw_rules):
    hook = "I DIDN'T THINK I'D EVER WEAR OVERALLS. THEN I PUT THESE ON."
    assert check_text(sw_rules, hook, scope="hook") == []
    strict = check_text(sw_rules, hook, scope="hook", ordered_mode="strict")
    assert rules_of(strict) == ["ordered_terms"] and strict[0].detail["mode"] == "strict"
    assert check_text(sw_rules, "THESE SWEATPANTS GREW STRAPS.", scope="hook", ordered_mode="strict") == []


def test_banned_words_match_whole_words_only(dr_rules):
    assert check_text(dr_rules, "Plenty of space to move") == []
    assert check_text(dr_rules, "Wellness for him")[0].detail["phrase"] == "wellness"
    assert rules_of(check_text(dr_rules, "A SPA day, self-care and RITUALS")) == ["banned_phrase"] * 3


def test_hyphenated_banned_phrase_is_found(dr_rules):
    assert check_text(dr_rules, "Pure self-care")[0].message.endswith("'self-care'")


def test_severity_is_carried_through(dr_rules):
    (v,) = check_text(dr_rules, "Robe time!")
    assert v.severity == "warn"
    (v,) = check_text(dr_rules, "Robe \u2014 time")
    assert v.severity == "error"


def test_each_brief_has_its_own_rules(sw_rules, dr_rules):
    assert check_text(sw_rules, "A spa day") == []                 # not banned in this brief
    assert check_text(dr_rules, "A spa day") != []
    assert sw_rules.digest != dr_rules.digest and sw_rules.brand != dr_rules.brand


def test_classic_black_navy_and_dark_grey_pass_the_new_audience_brief(sw_rules, swov_records):
    for handle in ("swoveralls-midnight-black", "swoveralls-navy", "swoveralls-dark-athletic-grey"):
        assert check_product(sw_rules, by_handle(swov_records, handle)) == [], handle


def test_other_classic_colours_are_not_on_the_approved_list(sw_rules, swov_records):
    for handle in ("swoveralls-forest-green", "swoveralls-heather-grey"):
        found = check_product(sw_rules, by_handle(swov_records, handle))
        assert rules_of(found) == ["not_approved_product"], handle


def test_limited_edition_prints_are_blocked(sw_rules, swov_records):
    found = check_product(sw_rules, by_title(swov_records, "Corduroy Swoveralls - Bordeaux"))
    assert {"blocked_flag", "not_approved_product"} <= set(rules_of(found))
    assert any(v.detail.get("flag") == "limited_edition" for v in found)


def test_comfyalls_in_black_are_not_swoveralls_for_this_brief(sw_rules, swov_records):
    found = check_product(sw_rules, by_title(swov_records, "Comfyalls - Midnight Black"))
    assert rules_of(found) == ["not_approved_product"]


def test_coming_soon_products_fail_the_status_rule(sw_rules, swov_records):
    found = check_product(sw_rules, by_title(swov_records, "Swoveralls - Garden Party - Coming Soon!"))
    assert "status_not_allowed" in rules_of(found)


def test_excluded_products_are_never_allowed(sw_rules, swov_records):
    found = check_product(sw_rules, by_title(swov_records, "Swoveralls Gift Card"))
    assert "excluded_product" in rules_of(found)


def test_a_brief_without_product_rules_allows_any_active_product(dr_rules):
    rec = {"excluded": False, "flags": ["licensed"], "status": "active", "handle": "anything"}
    assert check_product(dr_rules, rec) == []
    assert rules_of(check_product(dr_rules, dict(rec, status="inactive"))) == ["status_not_allowed"]


def test_digest_is_stable_and_changes_with_the_rules(tmp_path):
    first, again = load_ruleset(SW), load_ruleset(SW)
    assert first.digest == again.digest
    data = yaml.safe_load(SW.read_text(encoding="utf-8"))
    data["text_rules"]["banned_chars"].append({"char": "?", "name": "question mark"})
    edited = tmp_path / "rules.yaml"
    edited.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert load_ruleset(edited).digest != first.digest


def test_notes_do_not_change_the_digest(tmp_path):
    data = yaml.safe_load(SW.read_text(encoding="utf-8"))
    data["judgement_rules"].append("A new tone note")
    data["version"] = 2
    edited = tmp_path / "rules.yaml"
    edited.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert load_ruleset(edited).digest == load_ruleset(SW).digest


def test_judgement_rules_and_open_questions_are_carried_but_never_enforced(sw_rules):
    assert len(sw_rules.judgement_rules) == 4 and len(sw_rules.open_questions) == 2
    assert check_text(sw_rules, "Never show the classic overalls look in the first two seconds") == []


@pytest.mark.parametrize("bad", [
    {"mystery": 1},
    {"brief": None},
    {"text_rules": {"banned_words": []}},
    {"product_rules": {"colours": []}},
    {"text_rules": {"banned_chars": [{"name": "no char"}]}},
    {"text_rules": {"banned_phrases": [{"phrase": "x", "severity": "fatal"}]}},
    {"text_rules": {"ordered_terms": [{"first": "a"}]}},
    {"text_rules": {"ordered_terms": [{"first": "a", "then": "b", "mode": "loose"}]}},
])
def test_invalid_rule_files_are_rejected(tmp_path, bad):
    data = {"brief": "b", "brand": "x", "version": 1}
    data.update(bad)
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RuleError):
        load_ruleset(path)
