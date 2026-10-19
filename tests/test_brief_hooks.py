"""Run the real hooks from each brief PDF through that brief's own rules.

Fixtures were extracted from the two uploaded PDFs with pdftotext.
"""
import pytest

from contentintel.rules import check_text, load_ruleset

from conftest import BRIEFS_DIR, load_json


@pytest.fixture
def sw_hooks():
    return load_json("swoveralls_brief_hooks.json")


@pytest.fixture
def dr_hooks():
    return load_json("duderobe_brief_hooks.json")


def test_the_fixtures_hold_every_hook(sw_hooks, dr_hooks):
    assert len(sw_hooks) == 14 and len(dr_hooks) == 40         # hook + alternative hook per concept
    assert {h["concept"] for h in sw_hooks} == set(range(1, 8))
    assert {h["concept"] for h in dr_hooks} == set(range(1, 21))
    assert all(h["text"] for h in sw_hooks + dr_hooks)


def test_no_swoveralls_hook_breaks_the_banned_character_rules(sw_hooks):
    rules = load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")
    assert [h for h in sw_hooks if check_text(rules, h["text"], scope="hook")] == []


def test_swoveralls_hooks_pass_the_loose_reading_of_the_sweatpants_rule(sw_hooks):
    rules = load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")
    assert all(check_text(rules, h["text"], scope="hook", ordered_mode="when_both_present") == []
               for h in sw_hooks)


def test_four_swoveralls_hooks_fail_the_strict_reading_of_the_sweatpants_rule(sw_hooks):
    """An open question for the brief owner: the brief's own hooks 1, 1-alt, 5 and 6 say
    'overalls' with no 'sweatpants' before it. If the rule means every hook, these break it."""
    rules = load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")
    failing = [(h["concept"], h["kind"]) for h in sw_hooks
               if check_text(rules, h["text"], scope="hook", ordered_mode="strict")]
    assert failing == [(1, "hook"), (1, "alt_hook"), (5, "hook"), (6, "hook")]


def test_no_duderobe_hook_uses_a_banned_word_or_dash(dr_hooks):
    rules = load_ruleset(BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml")
    errors = [(h["concept"], v.message) for h in dr_hooks
              for v in check_text(rules, h["text"], scope="hook") if v.severity == "error"]
    assert errors == []


def test_the_rules_would_catch_a_bad_hook_in_each_brief(sw_hooks, dr_hooks):
    sw = load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")
    dr = load_ruleset(BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml")
    assert check_text(sw, sw_hooks[0]["text"] + "!", scope="hook")
    assert check_text(dr, dr_hooks[0]["text"] + " Pure wellness", scope="hook")
