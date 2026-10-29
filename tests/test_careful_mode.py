"""Careful mode for an account that has been warned: slower, smaller, spread over several runs, and restable by hand.

The popup text in tests/fixtures is the REAL wording Instagram showed the dummy account on 2026-10-26 ("We've detected
automated behavior on your account ... ensure that no other users or tools have access to your account"). The first version
of the warning phrases did not match it (it looked for "We suspect ..."), which is why these tests check the phrases against it.
"""
import copy
import inspect
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import research as R
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeBrowser
from test_apify_backend import apify_world
from test_collectors import BROWSER_YAML, CREDS, ENV, FULL, NOW, site, statuses, world, write_research

POPUP = (Path(__file__).parent / "fixtures" / "instagram_warning_popup.txt").read_text(encoding="utf-8")
FIVE = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["a", "b", "c", "d", "e"]}}}


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    """Collector settings with the automatic rest switched on for Instagram (the shipped default is off), since these tests are about the rest."""
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False, cooldown_days={"instagram": 3, "tiktok": 3})

@pytest.fixture(autouse=True)
def careful_mode_is_switched_on_for_these_tests(monkeypatch, tmp_path_factory):
    """These tests are about careful mode and the account rest, so they run with Instagram's careful-mode lines switched on."""
    from careful import careful_yaml
    path = careful_yaml(tmp_path_factory.mktemp("careful"))
    real = B.load_browser_settings
    monkeypatch.setattr(B, "load_browser_settings", lambda p="browser.yaml": real(path if str(p) in ("browser.yaml", BROWSER_YAML) else p))



def playwright_text_match(selector, page_text):
    """What a `text=...` selector does: a case-insensitive match of the words anywhere in the visible text."""
    assert selector.startswith("text=")
    norm = lambda t: " ".join(t.replace("\u2019", "'").lower().split())          # noqa: E731
    return norm(selector[5:]) in norm(page_text)


# ---- the real warning ------------------------------------------------------------------------------------------------------

def test_the_real_popup_is_recognised_by_the_phrases(cfg):
    phrases = site(cfg, "instagram")["warning_any"]
    matching = [p for p in phrases if playwright_text_match(p, POPUP)]
    assert "text=detected automated behavior" in matching and "text=no other users or tools have access to your account" in matching


def test_the_phrase_that_used_to_be_the_only_one_would_have_missed_it():
    assert not playwright_text_match("text=We suspect automated behavior", POPUP)


@pytest.mark.parametrize("caption", [
    "Try again later! These overalls are so comfy", "Action figures and overalls haul", "Loving my new overalls, no tools needed",
    "Automated my whole wardrobe, behavior unchanged", "Terms of Use for my online shop", "Restrict yourself to two pairs",
    "My account has been quiet this week", "no other users have access to my closet"])
def test_an_ordinary_caption_never_matches_a_warning_phrase(cfg, caption):
    ig, tt = site(cfg, "instagram"), site(cfg, "tiktok")
    assert not [p for p in ig["warning_any"] + tt["warning_any"] if playwright_text_match(p, caption)]


# ---- the careful settings ---------------------------------------------------------------------------------------------------

def test_instagram_ships_in_careful_mode_and_the_other_sites_are_untouched(cfg):
    ig = site(cfg, "instagram")
    assert ig["pacing"] == {"min_delay_seconds": 20, "max_delay_seconds": 60, "scroll_pause_seconds": 8}
    assert (ig["max_scrolls_per_page"], ig["max_pages_per_run"], ig["max_pages_per_day"]) == (3, 2, 4)
    for name in ("meta_ads", "tiktok", "gemini"):
        assert not {"pacing", "max_scrolls_per_page", "max_pages_per_run", "max_pages_per_day"} & set(site(cfg, name)), name


def test_a_site_uses_its_own_pauses_over_the_general_ones_and_the_general_ones_otherwise(cfg):
    assert B.site_pacing(cfg, "instagram") == {"min_delay_seconds": 20, "max_delay_seconds": 60, "scroll_pause_seconds": 8}
    assert B.site_pacing(cfg, "tiktok") == cfg["pacing"] == {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}


def test_a_partial_pacing_keeps_the_general_value_for_the_rest(cfg):
    c = copy.deepcopy(cfg)
    c["sites"]["tiktok"]["pacing"] = {"scroll_pause_seconds": 10}
    assert B.site_pacing(c, "tiktok") == {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 10}


def test_a_site_scrolls_as_far_as_its_own_setting_says_and_the_general_one_otherwise(cfg):
    assert B.site_scrolls(cfg, "instagram") == 3 and B.site_scrolls(cfg, "tiktok") == cfg["limits"]["max_scrolls_per_page"] == 8


def edited(tmp_path, **site_changes):
    data = yaml.safe_load(open("browser.yaml", encoding="utf-8"))
    data["sites"]["instagram"].update(site_changes)
    path = tmp_path / "b.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("key,value,match", [
    ("max_pages_per_run", 0, "must be a number of at least 1"), ("max_pages_per_day", -3, "must be a number of at least 1"),
    ("max_scrolls_per_page", 1.5, "must be a whole number"), ("max_pages_per_run", True, "must be a number"), ("max_pages_per_run", "2", "must be a number"),
    ("pacing", {}, "must be some of"), ("pacing", {"pause": 3}, "must be some of"), ("pacing", [20, 60], "must be some of"),
    ("pacing", {"min_delay_seconds": -1}, "must be a number of at least 0"), ("pacing", {"min_delay_seconds": 90}, "min_delay_seconds cannot be more than max_delay_seconds"),
    ("pacing", {"max_delay_seconds": 1}, "min_delay_seconds cannot be more than max_delay_seconds")])
def test_a_bad_careful_setting_is_refused_naming_it(tmp_path, key, value, match):
    with pytest.raises(B.BrowserError, match=match):
        B.load_browser_settings(edited(tmp_path, **{key: value}))


def test_good_careful_settings_are_accepted_on_any_site(tmp_path):
    data = yaml.safe_load(open("browser.yaml", encoding="utf-8"))
    data["sites"]["tiktok"].update({"pacing": {"min_delay_seconds": 5}, "max_pages_per_run": 3, "max_pages_per_day": 9, "max_scrolls_per_page": 2})
    path = tmp_path / "b.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert B.load_browser_settings(path)["sites"]["tiktok"]["max_pages_per_day"] == 9


# ---- taking turns over the pages -----------------------------------------------------------------------------------------

URLS = ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("budget,cursor,expected", [
    (None, 0, URLS), (None, 3, URLS), (5, 2, URLS), (9, 0, URLS), (2, 0, ["a", "b"]), (2, 2, ["c", "d"]), (2, 4, ["e", "a"]),
    (2, 7, ["c", "d"]), (3, 3, ["d", "e", "a"]), (1, 1, ["b"]), (0, 0, [])])
def test_each_run_takes_its_turn_from_where_the_last_one_stopped(budget, cursor, expected):
    assert C.take_pages(URLS, budget, cursor) == (expected, 5)


def test_taking_pages_changes_nothing_and_copes_with_no_pages():
    urls = list(URLS)
    C.take_pages(urls, 2, 3)
    assert urls == URLS and C.take_pages([], 2, 5) == ([], 0) and C.take_pages([], None, 0) == ([], 0)


def ig_task(*tags):
    return C.Task("instagram", f"{len(tags)} hashtags", {"hashtags": list(tags)}, "k")


def run_task(cfg, browser, task, budget=None, cursor=0, n=30):
    return C.run_browser_task(browser, task, n, {**site(cfg, "instagram"), "settle_seconds": 0}, cfg, CREDS["instagram"],
                              page_budget=budget, cursor=cursor)


def feed(cfg, **pages):
    return FakeBrowser({f"https://www.instagram.com/explore/tags/{t}/": {"json": [data]} for t, data in pages.items()},
                       present_always={site(cfg, "instagram")["logged_in_any"][0]})


def ig_json(prefix, n=2):
    return {"data": {"items": [{"code": f"{prefix}{i}", "caption": {"text": f"post {prefix}{i}"}, "like_count": 5, "user": {"username": "u"}}
                               for i in range(n)]}}


def test_a_task_opens_only_the_pages_its_budget_allows_and_says_how_many_there_were(cfg):
    b = feed(cfg, **{t: ig_json(t) for t in "abcde"})
    rows, stats = run_task(cfg, b, ig_task(*"abcde"), budget=2, cursor=0)
    tag_pages = [u for u in b.opened if "/explore/tags/" in u]
    assert tag_pages == ["https://www.instagram.com/explore/tags/a/", "https://www.instagram.com/explore/tags/b/"]
    assert (stats["pages"], stats["pages_planned"], stats["pages_total"]) == (2, 2, 5)


def test_a_task_starts_at_its_cursor(cfg):
    b = feed(cfg, **{t: ig_json(t) for t in "abcde"})
    run_task(cfg, b, ig_task(*"abcde"), budget=2, cursor=4)
    assert [u.rsplit("/", 2)[-2] for u in b.opened if "/explore/tags/" in u] == ["e", "a"]


def test_with_no_budget_every_page_is_opened_as_before(cfg):
    b = feed(cfg, **{t: ig_json(t) for t in "abc"})
    _, stats = run_task(cfg, b, ig_task(*"abc"))
    assert (stats["pages_planned"], stats["pages_total"]) == (3, 3)


def test_the_sites_own_scroll_limit_and_pause_are_what_the_browser_is_told(cfg):
    b = FakeBrowser({"https://www.instagram.com/explore/tags/": {"json": [ig_json("x", 1)]}}, present_always={site(cfg, "instagram")["logged_in_any"][0]})
    run_task(cfg, b, ig_task("a"), n=50)
    assert b.scrolls == 3 and set(b.pauses) == {8}
    tt = site(cfg, "tiktok")
    t = FakeBrowser({"https://www.tiktok.com/tag/": {"json": []}}, present_always={tt["logged_in_any"][0]})
    C.run_browser_task(t, C.Task("tiktok", "t", {"hashtags": ["a"]}, "k"), 50, {**tt, "settle_seconds": 0}, cfg, CREDS["tiktok"])
    assert t.scrolls == 8 and set(t.pauses) == {3}                                 # another site keeps the general settings


def test_the_notes_say_when_some_pages_were_left_for_next_time():
    note = C._page_notes({"pages_planned": 2, "pages_total": 5, "pages": 2})
    assert note == " - careful mode: opened 2 of 5 pages this run (the page caps in browser.yaml); the rest are taken on the next runs"
    assert C._page_notes({"pages_planned": 5, "pages_total": 5, "pages": 5}) == "" and C._page_notes({}) == ""


# ---- a run in careful mode ----------------------------------------------------------------------------------------------------

def go(store, tmp_path, swov, cfg, settings, f, *, now=NOW, backend="browser", apify=None, dry_run=False, data=FIVE, sources=("instagram",)):
    write_research(tmp_path, swov, data)
    return C.run_collect([swov], store, browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=list(sources), now=now, run_id="r", root=str(tmp_path / "brands"), dry_run=dry_run, apify=apify, backend=backend)


def ig_world(cfg):
    f = world(cfg)
    f.browsers["instagram"] = feed(cfg, **{t: ig_json(t) for t in "abcde"})
    return f


def tags_opened(f):
    return [u.rsplit("/", 2)[-2] for u in f.browsers["instagram"].opened if "/explore/tags/" in u]


def test_the_first_run_opens_two_pages_and_remembers_it_covered_some(tmp_path, store, swov, cfg, settings):
    f = ig_world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f)
    assert statuses(result) == [("instagram", "ran")] and tags_opened(f) == ["a", "b"]
    assert "careful mode: opened 2 of 5 pages this run" in result.brands["swoveralls"][0].detail
    (record,) = store.read_collected("swoveralls")
    assert record["partial"] is True and (record["pages_done"], record["pages_total"]) == (2, 5)
    assert store.read_page_log() == {"days": {"2026-10-20": {"instagram": 2}}, "cursor": {"instagram": 2}}


def test_the_next_runs_carry_on_where_it_stopped_until_the_days_cap(tmp_path, store, swov, cfg, settings):
    f1, f2, f3 = ig_world(cfg), ig_world(cfg), ig_world(cfg)
    go(store, tmp_path, swov, cfg, settings, f1)
    second = go(store, tmp_path / "2", swov, cfg, settings, f2, now="2026-10-20T09:00:00Z")
    assert statuses(second) == [("instagram", "ran")] and tags_opened(f2) == ["c", "d"]
    third = go(store, tmp_path / "3", swov, cfg, settings, f3, now="2026-10-20T11:00:00Z")
    assert statuses(third) == [("instagram", "skipped")] and f3.opened == []                      # not even logged in again
    assert third.brands["swoveralls"][0].detail == "careful mode: instagram has opened its 4 pages for today (2026-10-20); it continues tomorrow"
    assert not third.problems and store.read_page_log()["days"]["2026-10-20"] == {"instagram": 4}


def test_tomorrow_it_continues_and_wraps_round_to_the_first_page(tmp_path, store, swov, cfg, settings):
    for n, now in enumerate(("2026-10-20T07:00:00Z", "2026-10-20T09:00:00Z")):
        go(store, tmp_path / str(n), swov, cfg, settings, ig_world(cfg), now=now)
    f = ig_world(cfg)
    result = go(store, tmp_path / "next", swov, cfg, settings, f, now="2026-10-21T07:00:00Z")
    assert statuses(result) == [("instagram", "ran")] and tags_opened(f) == ["e", "a"]
    assert store.read_page_log()["days"] == {"2026-10-20": {"instagram": 4}, "2026-10-21": {"instagram": 2}}


def test_a_partial_collection_is_never_reused_as_if_it_were_finished(tmp_path, store, swov, cfg, settings):
    go(store, tmp_path, swov, cfg, settings, ig_world(cfg))
    f = ig_world(cfg)
    result = go(store, tmp_path / "2", swov, cfg, settings, f, now="2026-10-20T07:30:00Z")
    assert statuses(result) == [("instagram", "ran")] and tags_opened(f) == ["c", "d"]            # not "reused"


def test_when_every_page_fits_the_collection_is_complete_and_is_reused(tmp_path, store, swov, cfg, settings):
    two = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["a", "b"]}}}
    go(store, tmp_path, swov, cfg, settings, ig_world(cfg), data=two)
    (record,) = store.read_collected("swoveralls")
    assert "partial" not in record
    again = go(store, tmp_path / "2", swov, cfg, settings, ig_world(cfg), now="2026-10-20T09:00:00Z", data=two)
    assert statuses(again) == [("instagram", "reused")]


def test_the_days_cap_can_leave_less_than_the_runs_cap(tmp_path, store, swov, cfg, settings):
    c = copy.deepcopy(cfg)
    c["sites"]["instagram"].update({"max_pages_per_run": 3, "max_pages_per_day": 4})
    go(store, tmp_path, swov, c, settings, ig_world(c))
    f = ig_world(c)
    go(store, tmp_path / "2", swov, c, settings, f, now="2026-10-20T09:00:00Z")
    assert tags_opened(f) == ["d"]                                                                 # 4 a day, 3 already used


def test_a_site_with_only_one_cap_uses_that_cap(tmp_path, store, swov, cfg, settings):
    c = copy.deepcopy(cfg)
    del c["sites"]["instagram"]["max_pages_per_day"]
    f = ig_world(c)
    go(store, tmp_path, swov, c, settings, f)
    assert tags_opened(f) == ["a", "b"]
    f2 = ig_world(c)
    go(store, tmp_path / "2", swov, c, settings, f2, now="2026-10-20T09:00:00Z")
    assert tags_opened(f2) == ["c", "d"]                                                          # no daily cap: another run, another two


def test_a_site_with_no_caps_opens_every_page(tmp_path, store, swov, cfg, settings):
    c = copy.deepcopy(cfg)
    for key in ("max_pages_per_run", "max_pages_per_day"):
        del c["sites"]["instagram"][key]
    f = ig_world(c)
    go(store, tmp_path, swov, c, settings, f)
    assert tags_opened(f) == list("abcde") and "partial" not in store.read_collected("swoveralls")[0]


def test_tiktok_is_not_capped_unless_told_to_be(tmp_path, store, swov, cfg, settings):
    data = {"track_own_brand": False, "social": {"tiktok": {"hashtags": list("abcde")}}}
    f = world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, data=data, sources=("tiktok",))
    assert statuses(result) == [("tiktok", "ran")] and "careful mode" not in result.brands["swoveralls"][0].detail
    assert len([u for u in f.browsers["tiktok"].opened if "/tag/" in u]) == 5                    # all five hashtag pages, no cap


def test_a_dry_run_says_how_many_pages_it_would_open_and_writes_nothing(tmp_path, store, swov, cfg, settings):
    result = go(store, tmp_path, swov, cfg, settings, ig_world(cfg), dry_run=True)
    assert statuses(result) == [("instagram", "would_run")] and result.brands["swoveralls"][0].detail.endswith("(careful mode: opens at most 2 pages)")
    assert not (tmp_path / "data" / "_shared" / "page_log.json").exists()


def test_a_dry_run_shows_what_is_left_of_the_day(tmp_path, store, swov, cfg, settings):
    store.write_page_log({"days": {"2026-10-20": {"instagram": 3}}, "cursor": {"instagram": 3}})
    result = go(store, tmp_path, swov, cfg, settings, ig_world(cfg), dry_run=True)
    assert result.brands["swoveralls"][0].detail.endswith("(careful mode: opens at most 1 pages)")


def test_when_the_browser_is_capped_out_apify_in_fallback_mode_still_collects(tmp_path, store, swov, cfg, settings):
    store.write_page_log({"days": {"2026-10-20": {"instagram": 4}}, "cursor": {"instagram": 4}})
    http, apify = apify_world()
    f = ig_world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, backend="fallback", apify=apify, data={"track_own_brand": False, "social": {"instagram": {"hashtags": ["a"]}}})
    assert statuses(result) == [("instagram", "ran")] and f.opened == [] and len(http.calls) == 1
    assert "via Apify (the browser failed: careful mode: instagram has opened its 4 pages for today" in result.brands["swoveralls"][0].detail


def test_a_run_that_is_warned_still_stops_and_rests_in_careful_mode(tmp_path, store, swov, cfg, settings):
    ig = site(cfg, "instagram")
    f = ig_world(cfg)
    f.browsers["instagram"] = FakeBrowser({"https://www.instagram.com/explore/tags/": {"present": [ig["warning_any"][0]], "text": POPUP.strip()}},
                                          present_always={ig["logged_in_any"][0]})
    result = go(store, tmp_path, swov, cfg, settings, f)
    assert statuses(result) == [("instagram", "failed")] and list(store.read_cooldowns()) == ["instagram"]
    assert "We've detected automated behavior on your account" in store.read_cooldowns()["instagram"]["reason"]


# ---- the page log ------------------------------------------------------------------------------------------------------------

def test_reading_the_page_log_creates_nothing_and_a_round_trip_keeps_it(store, tmp_path):
    assert store.read_page_log() == {"days": {}, "cursor": {}} and not (tmp_path / "data").exists()
    log = {"days": {"2026-10-20": {"instagram": 3}}, "cursor": {"instagram": 7}}
    store.write_page_log(log)
    assert store.read_page_log() == log


def test_only_the_last_two_weeks_are_kept(store):
    days = {f"2026-10-{d:02d}": {"instagram": 1} for d in range(1, 21)}
    store.write_page_log({"days": days, "cursor": {}})
    assert sorted(store.read_page_log()["days"]) == [f"2026-10-{d:02d}" for d in range(7, 21)]


@pytest.mark.parametrize("content", ["{ not json", "[]", "null", '"x"', '{"days": 5, "cursor": []}'])
def test_a_damaged_page_log_means_nothing_has_been_opened_never_a_crash(store, tmp_path, content):
    path = tmp_path / "data" / "_shared" / "page_log.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert store.read_page_log() == {"days": {}, "cursor": {}}


def test_a_run_with_a_damaged_page_log_just_starts_again(tmp_path, store, swov, cfg, settings):
    path = tmp_path / "data" / "_shared" / "page_log.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ broken", encoding="utf-8")
    f = ig_world(cfg)
    assert statuses(go(store, tmp_path, swov, cfg, settings, f)) == [("instagram", "ran")] and tags_opened(f) == ["a", "b"]


# ---- resting by hand --------------------------------------------------------------------------------------------------------

def test_resting_by_hand_starts_a_rest_without_opening_anything(store, settings):
    lines = C.rest_by_hand(store, "instagram", NOW, 7, settings)
    assert lines == ["instagram: resting until 2026-10-27 (7 days), as you asked; nothing was opened"]
    assert store.read_cooldowns() == {"instagram": {"until": "2026-10-27T07:00:00Z", "at": NOW, "reason": "rested by hand"}}


@pytest.mark.parametrize("sites,expected", [("instagram", ["instagram"]), ("tiktok", ["tiktok"]), (" Instagram , TIKTOK ", ["instagram", "tiktok"]),
                                            ("gemini", ["gemini"])])
def test_the_accounts_are_named_loosely_and_recorded_each(store, settings, sites, expected):
    C.rest_by_hand(store, sites, NOW, 2, settings)
    assert sorted(store.read_cooldowns()) == sorted(expected)


def test_without_days_each_site_uses_its_own_setting(store, settings):
    C.rest_by_hand(store, "instagram,tiktok,gemini", NOW, 0, replace(settings, cooldown_days={"instagram": 5, "tiktok": 2}))
    data = store.read_cooldowns()
    assert data["instagram"]["until"] == "2026-10-25T07:00:00Z" and data["tiktok"]["until"] == "2026-10-22T07:00:00Z"
    assert data["gemini"]["until"] == "2026-10-23T07:00:00Z"                                      # no setting: three days


def test_resting_by_hand_keeps_the_other_rests(store, settings):
    store.write_cooldowns({"tiktok": {"until": "2026-11-01T00:00:00Z", "at": NOW, "reason": "x"}})
    C.rest_by_hand(store, "instagram", NOW, 3, settings)
    assert sorted(store.read_cooldowns()) == ["instagram", "tiktok"]


@pytest.mark.parametrize("sites,days,match", [("", 3, "--rest needs one or more of instagram, tiktok, gemini"),
                                              ("facebook", 3, "facebook is not one"), ("instagram,facebook", 3, "facebook is not one"),
                                              ("instagram", -1, "--rest-days must be 0")])
def test_a_bad_request_to_rest_is_refused_and_writes_nothing(store, settings, tmp_path, sites, days, match):
    with pytest.raises(C.CollectError, match=match):
        C.rest_by_hand(store, sites, NOW, days, settings)
    assert not (tmp_path / "data").exists()


def test_a_rest_started_by_hand_is_obeyed_by_the_next_run(tmp_path, store, swov, cfg, settings):
    C.rest_by_hand(store, "instagram", NOW, 7, settings)
    f = ig_world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, now="2026-10-21T07:00:00Z")
    assert statuses(result) == [("instagram", "skipped")] and f.opened == []
    assert result.brands["swoveralls"][0].detail.startswith("resting until 2026-10-27 after an account warning (rested by hand)")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    state = {"factory": world(B.load_browser_settings(BROWSER_YAML))}
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda browser_cfg, creds, debug: state["factory"])
    for name in list(ENV) + ["INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TIKTOK_USERNAME", "TIKTOK_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    brands = tmp_path / "brands"
    shutil.copytree(BRANDS_DIR, brands)
    (brands / "swoveralls" / "research.yaml").write_text(yaml.safe_dump(FULL), encoding="utf-8")

    def run(*args):
        return cli.main(["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands), "--research-config", "research.yaml",
                         "--browser-config", BROWSER_YAML, "--debug-dir", str(tmp_path / "debug"), *args])
    return state, run, tmp_path


def test_cli_rest_starts_a_rest_opens_nothing_and_needs_no_secrets(cli_env, capsys):
    state, run, tmp_path = cli_env
    assert run("--rest", "instagram", "--rest-days", "7") == 0
    out = capsys.readouterr().out
    assert "instagram: resting until" in out and "(7 days), as you asked; nothing was opened" in out
    assert state["factory"].opened == [] and list(Store(tmp_path / "data").read_cooldowns()) == ["instagram"]
    assert Store(tmp_path / "data").read_cooldowns()["instagram"]["reason"] == "rested by hand"


def test_cli_rest_uses_the_configured_days_when_none_are_given(cli_env, capsys):
    state, run, tmp_path = cli_env
    assert run("--rest", "instagram,tiktok") == 0
    out = capsys.readouterr().out
    assert out.count("(3 days), as you asked") == 2


def test_cli_a_bad_rest_is_an_error_with_the_fix(cli_env, capsys):
    state, run, tmp_path = cli_env
    assert run("--rest", "facebook") == 1
    assert "Error: --rest needs one or more of instagram, tiktok, gemini; facebook is not one" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()


def test_the_workflow_can_rest_an_account_without_opening_it():
    wf = yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert "rest" in inputs["mode"]["options"] and inputs["rest_account"]["default"] == "instagram" and inputs["rest_days"]["default"] == ""
    step = next(s for s in wf["jobs"]["collect"]["steps"] if "REST_ACCOUNT" in (s.get("env") or {}))
    assert step["env"]["REST_ACCOUNT"] == "${{ inputs.rest_account }}" and step["env"]["REST_DAYS"] == "${{ inputs.rest_days }}"
    assert 'rest)               args=(--rest "${REST_ACCOUNT:-instagram}")' in step["run"] and '--rest-days "$REST_DAYS"' in step["run"]
    assert '[ "$MODE" != "rest" ] && [ -z "$sources" ]' in step["run"]                              # no ticked source is needed to rest


# ---- what the browser is launched with ---------------------------------------------------------------------------------------

def test_the_browser_is_not_launched_with_the_option_that_hides_automation():
    source = inspect.getsource(B.open_browser)
    assert "AutomationControlled" not in source and '"--no-sandbox"' in source


def test_the_real_factory_gives_each_site_its_own_pauses(monkeypatch, cfg):
    seen = {}
    monkeypatch.setattr(B, "open_browser", lambda site_name, site_cfg, pacing, cookies, debug: seen.setdefault(site_name, pacing))
    factory = cli._real_factory(cfg, {"instagram": {"cookies": "c"}, "tiktok": {"cookies": "c"}}, "dbg")
    factory("instagram")
    factory("tiktok")
    assert seen["instagram"]["max_delay_seconds"] == 60 and seen["tiktok"] == cfg["pacing"]


# ---- every page load counts, and brands share a run's pages ---------------------------------------------------------------------

def test_a_page_opened_by_a_typed_search_counts_as_a_page_load_but_not_as_one_of_the_planned_pages(cfg):
    from test_tiktok_puzzle_and_search import TikTok
    b = TikTok(cfg, error_on={"tag"}, data_on={"typed"})
    task = C.Task("tiktok", "tags", {"hashtags": ["overalls"]}, "k")
    _, stats = C.run_browser_task(b, task, 10, {**cfg["sites"]["tiktok"], "settle_seconds": 0}, cfg, {})
    assert (stats["pages"], stats["pages_planned"], stats["pages_total"]) == (2, 1, 1)


def test_two_brands_in_one_run_share_the_runs_pages(tmp_path, store, brands, cfg, settings):
    f = ig_world(cfg)
    first = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["a", "b"]}}}
    second = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["c"]}}}
    write_research(tmp_path, brands["swoveralls"], first)
    write_research(tmp_path, brands["duderobe"], second)
    result = C.run_collect([brands["swoveralls"], brands["duderobe"]], store, browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg,
                           ledger_cls=R.Ledger, sources=["instagram"], now=NOW, run_id="r", root=str(tmp_path / "brands"))
    assert statuses(result, "swoveralls") == [("instagram", "ran")] and statuses(result, "duderobe") == [("instagram", "skipped")]
    assert result.brands["duderobe"][0].detail == "careful mode: instagram has opened its 2 pages for this run; the next run continues"
    assert tags_opened(f) == ["a", "b"] and not result.problems


def test_the_next_run_gives_the_second_brand_its_turn(tmp_path, store, brands, cfg, settings):
    first = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["a", "b"]}}}
    second = {"track_own_brand": False, "social": {"instagram": {"hashtags": ["c"]}}}
    write_research(tmp_path, brands["swoveralls"], first)
    write_research(tmp_path, brands["duderobe"], second)
    for now in ("2026-10-20T07:00:00Z", "2026-10-21T07:00:00Z"):
        f = ig_world(cfg)
        result = C.run_collect([brands["swoveralls"], brands["duderobe"]], store, browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg,
                               ledger_cls=R.Ledger, sources=["instagram"], now=now, run_id="r", root=str(tmp_path / "brands"))
    assert statuses(result, "swoveralls") == [("instagram", "reused")] and statuses(result, "duderobe") == [("instagram", "ran")]
