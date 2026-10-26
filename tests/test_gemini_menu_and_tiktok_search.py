"""Two things the 2026-10-21 runs showed on the real pages.

Gemini changed its menu: there is no "Tools" button any more. The + at the left of the prompt box (labelled
"Upload & tools") opens a menu with "More tools", and that opens a list with "Deep research". The Check now presses
that path (typing and sending nothing), so a change like this is found by Check and not by a failed report.

TikTok's hashtag page said "Something went wrong" in the automated browser while the same search worked by hand,
so a tag page that errors is read through TikTok's search instead.
"""
from pathlib import Path

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.browser import BrowserError

from fake_browser import FakeBrowser, GeminiFake, TT_JSON
from test_collectors import CREDS, NOW, collect, site, world
from test_session_checks import matches

FIXTURES = Path(__file__).parent / "fixtures"
GEMINI_REAL = (FIXTURES / "gemini_plus_button_excerpt.html").read_text(encoding="utf-8")
TIKTOK_REAL = (FIXTURES / "tiktok_error_excerpt.html").read_text(encoding="utf-8")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


# ---- the settings, against the real pages ---------------------------------------------------------------------------

def test_the_deep_research_path_is_the_menu_the_real_page_has(cfg):
    steps = cfg["sites"]["gemini"]["deep_research_clicks"]
    assert len(steps) == 3
    assert steps[0][0] == 'button[aria-label="Upload & tools"]'
    assert steps[1][-1] == "text=More tools" and steps[2][-1] == "text=Deep research"
    assert all('"Tools"' not in s for group in steps for s in group)            # the old button that no longer exists


def test_the_first_step_matches_the_real_plus_button(cfg):
    assert matches(cfg["sites"]["gemini"]["deep_research_clicks"][0][0], GEMINI_REAL)
    assert 'aria-haspopup="menu"' in GEMINI_REAL                                  # it is a menu button, as the fallback expects


def test_the_prompt_box_is_on_the_same_real_page(cfg):
    assert any(matches(s, GEMINI_REAL) for s in cfg["sites"]["gemini"]["logged_in_any"])


def test_the_deep_research_marker_does_not_match_the_page_in_normal_chat_mode(cfg):
    """If the marker matched a normal chat, confirming the switch would prove nothing."""
    for selector in cfg["sites"]["gemini"]["deep_research_active_any"]:
        if selector.startswith("["):
            assert not matches(selector, GEMINI_REAL), selector
    assert 'data-placeholder="Type @ for connectors' in GEMINI_REAL


def test_the_marker_would_match_the_prompt_box_once_it_asks_what_to_research(cfg):
    switched = GEMINI_REAL.replace('data-placeholder="Type @ for connectors, skills and more"',
                                   'data-placeholder="What do you want to research?"')
    assert switched != GEMINI_REAL and matches(cfg["sites"]["gemini"]["deep_research_active_any"][0], switched)


def test_the_tiktok_error_text_is_what_the_real_page_says(cfg):
    assert "Something went wrong" in TIKTOK_REAL and cfg["sites"]["tiktok"]["error_any"] == ["text=Something went wrong"]
    assert cfg["sites"]["tiktok"]["hashtag_via_search_on_error"] is True


# ---- pressing the menu path -------------------------------------------------------------------------------------------

def menu_browser(cfg, *, missing_step=None, confirmed=True):
    g = cfg["sites"]["gemini"]
    clickable = {s for n, group in enumerate(g["deep_research_clicks"], start=1) if n != missing_step for s in group}
    here = {g["deep_research_active_any"][0]} if confirmed else set()
    return FakeBrowser(clickable=clickable, present_always=here)


def test_all_three_steps_are_pressed_in_order_and_a_confirmed_switch_says_nothing(cfg):
    b = menu_browser(cfg)
    assert C.switch_to_deep_research(b, cfg["sites"]["gemini"]) == ""
    assert b.clicks == ['button[aria-label="Upload & tools"]', '[role="menuitem"]:has-text("More tools")',
                        '[role="menuitem"]:has-text("Deep research")'] and b.shots == []
    assert b.waits == [1, 1, 1, 1]                      # a second after each press, and one for the page to settle


@pytest.mark.parametrize("step", [1, 2, 3])
def test_a_missing_button_names_its_step_and_saves_a_picture(cfg, step):
    b = menu_browser(cfg, missing_step=step)
    with pytest.raises(BrowserError, match=rf"could not find the button to switch to Deep Research \(step {step}\)"):
        C.switch_to_deep_research(b, cfg["sites"]["gemini"])
    assert b.shots == ["gemini-no-deep-research-button"] and len(b.clicks) == step - 1


def test_buttons_pressed_but_no_sign_of_the_switch_is_noted_with_a_picture_and_does_not_stop(cfg):
    b = menu_browser(cfg, confirmed=False)
    note = C.switch_to_deep_research(b, cfg["sites"]["gemini"])
    assert "did not show Deep Research as switched on" in note and "gemini-deep-research-unconfirmed" in note
    assert b.shots == ["gemini-deep-research-unconfirmed"] and len(b.clicks) == 3


def test_with_no_marker_configured_there_is_nothing_to_confirm(cfg):
    g = {k: v for k, v in cfg["sites"]["gemini"].items() if k != "deep_research_active_any"}
    b = menu_browser(cfg, confirmed=False)
    assert C.switch_to_deep_research(b, g) == "" and b.shots == []


# ---- a real report run, and the check ---------------------------------------------------------------------------------

def test_a_report_run_goes_on_when_the_switch_is_unconfirmed_and_says_so(cfg):
    g = site(cfg, "gemini")
    out = C.collect_deep_research(GeminiFake(g, mode_confirmed=False), "Who are the competitors?", g)
    assert len(out["text"]) > 1500 and any("did not show Deep Research as switched on" in n for n in out["notes"])
    quiet = C.collect_deep_research(GeminiFake(g), "Who are the competitors?", g)
    assert not any("switched on" in n for n in quiet["notes"])


def run_check(cfg, tmp_path, browser):
    from contentintel import research as R
    from contentintel.store import Store
    f = world(cfg)
    f.browsers["gemini"] = browser
    rows = C.check_collectors(f, CREDS, C.load_collector_settings("does-not-exist.yaml"), cfg, Store(tmp_path / "data"),
                              R.Ledger, NOW, ["deep_research"])
    return {r["source"]: r for r in rows}["deep_research"], browser


def gemini_check_browser(cfg, **kw):
    g = cfg["sites"]["gemini"]
    base = menu_browser(cfg, **kw)
    base.present_always |= {g["logged_in_any"][0], g["prompt_box"][0]}
    return base


def test_the_check_presses_the_menu_and_passes_without_typing_or_sending(cfg, tmp_path):
    row, b = run_check(cfg, tmp_path, gemini_check_browser(cfg))
    assert row["ok"] is True and "Deep Research button was found and pressed; no research was started" in row["detail"]
    assert b.typed == [] and len(b.clicks) == 3 and not any(c in cfg["sites"]["gemini"]["send"] for c in b.clicks)
    assert "check-gemini-deep-research" in b.shots


@pytest.mark.parametrize("step", [1, 2, 3])
def test_the_check_now_fails_when_gemini_changes_its_menu(cfg, tmp_path, step):
    row, b = run_check(cfg, tmp_path, gemini_check_browser(cfg, missing_step=step))
    assert row["ok"] is False and f"(step {step})" in row["detail"] and "deep_research_clicks" in row["detail"]
    assert "gemini-no-deep-research-button" in b.shots


def test_the_check_fails_when_the_switch_is_not_confirmed(cfg, tmp_path):
    row, b = run_check(cfg, tmp_path, gemini_check_browser(cfg, confirmed=False))
    assert row["ok"] is False and "did not show Deep Research as switched on" in row["detail"]
    assert "button was found and pressed" not in row["detail"]


# ---- TikTok: a tag page that errors is read through search ------------------------------------------------------------

ERR = "text=Something went wrong"
TAG = "https://www.tiktok.com/tag/overalls"
SEARCH = "https://www.tiktok.com/search?q=%23overalls"


def tiktok(cfg, pages):
    return FakeBrowser(pages, present_always={cfg["sites"]["tiktok"]["logged_in_any"][0]})


def read(cfg, browser, hashtags=("overalls",), site_cfg=None, profiles=()):
    task = C.Task("tiktok", "tags", {"hashtags": list(hashtags), "profiles": list(profiles)}, "k")
    s = {**cfg["sites"]["tiktok"], "settle_seconds": 0, **(site_cfg or {})}
    return C.run_browser_task(browser, task, 10, s, cfg, {})


def test_an_erroring_tag_page_is_replaced_by_the_search_for_that_tag(cfg):
    b = tiktok(cfg, {TAG: {"present": [ERR]}, SEARCH: {"json": [TT_JSON]}})
    rows, stats = read(cfg, b)
    assert b.opened[-2:] == [TAG, SEARCH] and len(rows) == 2
    assert stats["fallbacks"] == ["#overalls"] and stats["page_errors"] == 0
    assert all(r["found_via"] == "#overalls" for r in rows)                       # still credited to the tag that was asked for


def test_a_tag_page_that_works_is_read_and_search_is_not_opened(cfg):
    b = tiktok(cfg, {TAG: {"json": [TT_JSON]}})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and SEARCH not in b.opened and stats["fallbacks"] == [] and stats["page_errors"] == 0


def test_when_search_errors_too_the_error_is_counted_and_nothing_is_invented(cfg):
    b = tiktok(cfg, {TAG: {"present": [ERR]}, SEARCH: {"present": [ERR]}})
    rows, stats = read(cfg, b)
    assert rows == [] and stats["fallbacks"] == ["#overalls"] and stats["page_errors"] == 1
    text = C._page_notes(stats)
    assert "would not load (an error or a security check)" in text and "1 page(s) still showed 'Something went wrong'" in text


def test_only_the_tag_that_errored_is_searched(cfg):
    b = tiktok(cfg, {"https://www.tiktok.com/tag/good": {"json": [TT_JSON]}, "https://www.tiktok.com/tag/bad": {"present": [ERR]},
                     "https://www.tiktok.com/search?q=%23bad": {"json": [{"itemList": []}]}})
    rows, stats = read(cfg, b, hashtags=("good", "bad"))
    assert stats["fallbacks"] == ["#bad"] and "https://www.tiktok.com/search?q=%23bad" in b.opened
    assert "https://www.tiktok.com/search?q=%23good" not in b.opened and len(rows) == 2


def test_an_error_on_a_profile_page_does_not_trigger_the_tag_fallback(cfg):
    profile = "https://www.tiktok.com/@acme"
    b = tiktok(cfg, {profile: {"present": [ERR]}})
    rows, stats = read(cfg, b, hashtags=(), profiles=("acme",))
    assert stats["fallbacks"] == [] and stats["page_errors"] == 1 and b.opened[-1] == profile


def test_the_fallback_is_off_unless_the_settings_turn_it_on(cfg):
    b = tiktok(cfg, {TAG: {"present": [ERR]}, SEARCH: {"json": [TT_JSON]}})
    rows, stats = read(cfg, b, site_cfg={"hashtag_via_search_on_error": False})
    assert rows == [] and SEARCH not in b.opened and stats["fallbacks"] == [] and stats["page_errors"] == 1


def test_a_tag_with_accents_is_searched_with_its_hash_and_encoded(cfg):
    b = tiktok(cfg, {"https://www.tiktok.com/tag/caf%C3%A9": {"present": [ERR]},
                     "https://www.tiktok.com/search?q=%23caf%C3%A9": {"json": [TT_JSON]}})
    rows, stats = read(cfg, b, hashtags=("café",))
    assert stats["fallbacks"] == ["#café"] and len(rows) == 2


def test_a_site_without_error_settings_never_looks_for_an_error(cfg):
    plain = {k: v for k, v in cfg["sites"]["tiktok"].items() if k not in ("error_any", "hashtag_via_search_on_error")}
    task = C.Task("tiktok", "tags", {"hashtags": ["overalls"]}, "k")
    b = tiktok(cfg, {TAG: {"present": [ERR]}, SEARCH: {"json": [TT_JSON]}})
    rows, stats = C.run_browser_task(b, task, 10, {**plain, "settle_seconds": 0}, cfg, {})
    assert rows == [] and SEARCH not in b.opened and stats["fallbacks"] == [] and stats["page_errors"] == 0


def test_the_page_notes_say_nothing_when_nothing_went_wrong():
    assert C._page_notes({}) == "" and C._page_notes({"fallbacks": [], "page_errors": 0}) == ""


def erroring_tiktok_world(cfg):
    f = world(cfg)
    f.browsers["tiktok"] = tiktok(cfg, {"https://www.tiktok.com/tag/": {"present": [ERR]}, "https://www.tiktok.com/search": {"json": [TT_JSON]}})
    return f


def test_the_check_tells_a_person_the_search_was_used(cfg, tmp_path):
    from contentintel import research as R
    from contentintel.store import Store
    settings = C.load_collector_settings("does-not-exist.yaml")
    rows = {r["source"]: r for r in C.check_collectors(erroring_tiktok_world(cfg), CREDS, settings, cfg, Store(tmp_path / "data"),
                                                       R.Ledger, NOW, ["tiktok"])}
    assert rows["tiktok"]["ok"] is True
    assert ("the page for #overalls would not load (an error or a security check), so TikTok's search results were read instead "
            "(the search address)") in rows["tiktok"]["detail"]


def test_the_run_report_tells_a_person_the_search_was_used(cfg, tmp_path, swov):
    from dataclasses import replace
    from contentintel.store import Store
    settings = replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)
    result = collect(Store(tmp_path / "data"), tmp_path, [swov], {"social": {"tiktok": {"hashtags": ["overalls"]}}},
                     erroring_tiktok_world(cfg), cfg, settings, sources=("tiktok",))
    (task,) = result.brands["swoveralls"]
    assert task.status == "ran" and task.items == 2
    assert "the page for #overalls would not load (an error or a security check), so TikTok's search results were read instead" in task.detail
