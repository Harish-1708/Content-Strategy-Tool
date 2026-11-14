"""When a site WARNS an account (suspected automation, an action blocked, a lock), the run stops using that account at once and
the account rests for a few days. Resting accounts are skipped, never failed, and Apify (when switched on) still runs.

The phrases and addresses that count as a warning are in browser.yaml; the page text in these tests is shaped like the real
wording but has NOT been checked against a real warning: that is said in the README and in browser.yaml.
"""
import shutil
from dataclasses import replace

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import research as R
from contentintel.browser import AccountWarning, BrowserError, Challenge
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeBrowser
from test_apify_backend import ONE, apify_world
from test_collectors import BROWSER_YAML, CREDS, ENV, FULL, NOW, site, statuses, world, write_research

WARNING = "We suspect automated behavior on your account. Try again later."
THREE_DAYS = "2026-10-23T07:00:00Z"                      # NOW plus the default rest of 3 days


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



def warned_page(cfg, *, text=WARNING, tag_only=True):
    ig = site(cfg, "instagram")
    spec = {"present": [ig["warning_any"][0]], "text": text}
    pages = {"https://www.instagram.com/explore/tags/": spec} if tag_only else {"https://www.instagram.com/": spec}
    return FakeBrowser(pages, present_always={ig["logged_in_any"][0]})


class LateWarning(FakeBrowser):
    """A page that is fine when it opens and shows the warning once it has been scrolled."""

    def __init__(self, cfg):
        ig = site(cfg, "instagram")
        super().__init__({}, present_always={ig["logged_in_any"][0]})
        self.marker, self.text_shown = ig["warning_any"][0], WARNING

    def dynamic_present(self):
        return {self.marker} if self.scrolls >= 1 else set()

    def dynamic_text(self):
        return self.text_shown


def go(store, tmp_path, swov, cfg, settings, f, *, now=NOW, backend="browser", apify=None, sources=("instagram",), dry_run=False,
       data=ONE, creds=CREDS):
    write_research(tmp_path, swov, data)
    return C.run_collect([swov], store, browser_factory=f, creds=creds, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=list(sources), now=now, run_id="r", root=str(tmp_path / "brands"), dry_run=dry_run,
                         apify=apify, backend=backend)


# ---- seeing a warning -------------------------------------------------------------------------------------------------------

def test_the_warning_phrases_are_strict_and_the_addresses_are_listed(cfg):
    ig, tt = site(cfg, "instagram"), site(cfg, "tiktok")
    assert ig["warning_any"] == ["text=detected automated behavior", "text=no other users or tools have access to your account",
                                 "text=We suspect automated behavior", "text=Action Blocked", "text=We restrict certain activity",
                                 "text=Your account has been temporarily locked"]
    assert ig["warning_url_any"] == ["/accounts/suspended", "/accounts/disabled", "/challenge/"]
    assert tt["warning_any"] and "warning_any" not in site(cfg, "meta_ads") and "warning_any" not in site(cfg, "gemini")
    assert not any("again later" in p.lower() for p in ig["warning_any"] + tt["warning_any"])      # an ordinary caption could say that


def test_a_warning_on_the_page_is_reported_with_the_pages_own_words(cfg):
    b = warned_page(cfg)
    b.open("https://www.instagram.com/explore/tags/a/")
    assert C.warning_seen(b, site(cfg, "instagram")) == WARNING


def test_the_reason_is_tidied_and_cut_to_160_characters(cfg):
    b = warned_page(cfg, text="  We suspect\n\n automated   behavior " + "x" * 300)
    b.open("https://www.instagram.com/explore/tags/a/")
    reason = C.warning_seen(b, site(cfg, "instagram"))
    assert reason.startswith("We suspect automated behavior xxx") and len(reason) == 160 and "\n" not in reason


def test_a_warning_with_no_text_still_says_one_appeared(cfg):
    b = warned_page(cfg, text="")
    b.open("https://www.instagram.com/explore/tags/a/")
    assert C.warning_seen(b, site(cfg, "instagram")) == "a warning message appeared"


def test_no_warning_means_no_reason(cfg):
    b = FakeBrowser({}, present_always={site(cfg, "instagram")["logged_in_any"][0]})
    b.open("https://www.instagram.com/explore/tags/a/")
    assert C.warning_seen(b, site(cfg, "instagram")) == ""


@pytest.mark.parametrize("url", ["https://www.instagram.com/accounts/suspended/", "https://www.instagram.com/accounts/disabled/",
                                 "https://www.instagram.com/challenge/?next=/explore/"])
def test_a_warning_address_counts_as_a_warning(cfg, url):
    b = FakeBrowser({})
    b.open(url)
    part = next(p for p in site(cfg, "instagram")["warning_url_any"] if p in url)
    assert C.warning_seen(b, site(cfg, "instagram")) == f"the page address contains {part}"


def test_a_normal_address_is_not_a_warning(cfg):
    b = FakeBrowser({})
    b.open("https://www.instagram.com/explore/tags/overalls/")
    assert C.warning_seen(b, site(cfg, "instagram")) == ""


def test_the_pages_own_words_come_before_the_address(cfg):
    b = warned_page(cfg)
    b.open("https://www.instagram.com/explore/tags/accounts/suspended/")
    assert C.warning_seen(b, site(cfg, "instagram")) == WARNING


def test_a_site_with_no_warning_settings_never_sees_one(cfg):
    b = warned_page(cfg)
    b.open("https://www.instagram.com/explore/tags/a/")
    assert C.warning_seen(b, {k: v for k, v in site(cfg, "instagram").items() if not k.startswith("warning")}) == ""


# ---- stopping ---------------------------------------------------------------------------------------------------------------

def test_a_warning_is_a_challenge_so_every_existing_handler_catches_it():
    err = AccountWarning("instagram", "We suspect automated behavior")
    assert isinstance(err, Challenge) and isinstance(err, BrowserError)
    assert (err.site, err.reason) == ("instagram", "We suspect automated behavior")
    assert "instagram showed an account warning (We suspect automated behavior)" in str(err)
    assert "stopped using the account straight away" in str(err) and "follow what the warning says" in str(err)


def test_check_warning_stops_with_a_picture(cfg):
    b = warned_page(cfg)
    b.open("https://www.instagram.com/explore/tags/a/")
    with pytest.raises(AccountWarning) as err:
        C.check_warning(b, "instagram", site(cfg, "instagram"))
    assert err.value.reason == WARNING and b.shots == ["warning-instagram"]


def test_check_warning_does_nothing_on_a_normal_page(cfg):
    b = FakeBrowser({}, present_always={site(cfg, "instagram")["logged_in_any"][0]})
    b.open("https://www.instagram.com/explore/tags/a/")
    C.check_warning(b, "instagram", site(cfg, "instagram"))
    assert b.shots == []


def test_a_warning_on_the_home_page_stops_the_login_before_anything_is_typed(cfg):
    b = warned_page(cfg, tag_only=False)
    with pytest.raises(AccountWarning):
        C.ensure_session(b, "instagram", {**site(cfg, "instagram")}, {"username": "u", "password": "p", "cookies": "x"})
    assert b.typed == [] and b.clicks == [] and b.shots == ["warning-instagram"]


def test_a_warning_that_appears_after_the_password_login_stops_there(cfg):
    ig = site(cfg, "instagram")
    fields = {s for step in ig["login_steps"] if "fill" in step for s in step["fill"]} | {s for step in ig["login_steps"] if "click" in step for s in step["click"]}

    class AfterLogin(FakeBrowser):
        def dynamic_present(self):
            return {ig["warning_any"][0]} if self.typed else set()

    b = AfterLogin({}, typeable=fields, clickable=fields, present_always={ig["login_wall_any"][0]})
    with pytest.raises(AccountWarning):
        C.ensure_session(b, "instagram", ig, {"username": "u", "password": "p"})
    assert b.typed == ["u", "p"] and "warning-instagram" in b.shots


def test_a_warning_after_opening_a_page_stops_the_collection(cfg):
    b = warned_page(cfg)
    task = C.Task("instagram", "1 hashtags", {"hashtags": ["a"]}, "k")
    with pytest.raises(AccountWarning, match="We suspect automated behavior"):
        C.run_browser_task(b, task, 10, {**site(cfg, "instagram"), "settle_seconds": 0}, cfg, CREDS["instagram"])
    assert b.shots[-1] == "warning-instagram"
    assert b.scrolls == 0 and b.opened[-1] == "https://www.instagram.com/explore/tags/a/"      # stopped before reading anything
    assert b.shots == ["warning-instagram"]


def test_a_warning_that_appears_while_reading_stops_it_too(cfg):
    b = LateWarning(cfg)
    task = C.Task("instagram", "1 hashtags", {"hashtags": ["a"]}, "k")
    with pytest.raises(AccountWarning):
        C.run_browser_task(b, task, 10, {**site(cfg, "instagram"), "settle_seconds": 0}, cfg, CREDS["instagram"])
    assert b.scrolls >= 1


def test_the_ad_library_needs_no_login_so_it_is_never_checked_for_a_warning(cfg):
    meta = {**site(cfg, "meta_ads"), "warning_any": [site(cfg, "instagram")["warning_any"][0]]}
    b = FakeBrowser({C.AD_LIBRARY: {"json": [{"ads": [{"ad_archive_id": "1", "page_name": "A", "snapshot": {"body": {"text": "t"}}}]}],
                                     "present": [meta["warning_any"][0]]}}, clickable=set(meta["consent_any"]))
    task = C.Task("meta_ads", "1 keywords", {"urls": [C.ad_library_url("a", "us", "active")]}, "k")
    rows, _ = C.run_browser_task(b, task, 10, meta, cfg, {})
    assert len(rows) == 1


# ---- resting ----------------------------------------------------------------------------------------------------------------

def test_a_warned_account_stops_the_run_for_that_site_and_is_rested(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, sources=("meta_ads", "tiktok", "instagram"))
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "skipped")]
    assert list(result.needs_you) == ["instagram"] and not result.problems_for
    detail = result.brands["swoveralls"][2].detail
    assert "instagram showed an account warning (We suspect automated behavior" in detail
    assert "The account will rest until 2026-10-23 (3 days)" in detail and "collectors.safety.cooldown_days" in detail
    assert store.read_cooldowns() == {"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}}
    assert result.resting["instagram"]["until"] == THREE_DAYS


def test_the_next_run_skips_the_resting_account_without_calling_it_a_failure(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    go(store, tmp_path, swov, cfg, settings, f)
    f2 = world(cfg)
    result = go(store, tmp_path / "again", swov, cfg, settings, f2, now="2026-10-21T07:00:00Z", sources=("meta_ads", "tiktok", "instagram"))
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "skipped")]
    assert result.brands["swoveralls"][2].detail.startswith("resting until 2026-10-23 after an account warning (We suspect automated behavior")
    assert "instagram" not in f2.opened and not result.problems and "instagram" in result.resting


def test_the_report_says_which_accounts_are_resting_and_that_apify_still_runs(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}})
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), now="2026-10-21T07:00:00Z")
    text = C.format_collect_report(result, settings)
    assert text.splitlines()[1].startswith("Resting: instagram until 2026-10-23 after an account warning (We suspect automated behavior")
    assert "its browser tasks are skipped, and Apify still runs if it is switched on" in text


def test_a_rest_that_has_ended_is_over(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}})
    f = world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, now="2026-10-23T07:00:01Z")
    assert statuses(result) == [("instagram", "ran")] and result.resting == {} and "instagram" in f.opened
    exactly = go(Store(tmp_path / "other"), tmp_path / "x", swov, cfg, settings, world(cfg), now=THREE_DAYS)
    assert statuses(exactly) == [("instagram", "ran")]                                    # the moment it ends, it is over


def test_the_rest_is_as_long_as_the_setting_says(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    result = go(store, tmp_path, swov, cfg, replace(settings, cooldown_days={"instagram": 7, "tiktok": 3}), f)
    assert store.read_cooldowns()["instagram"]["until"] == "2026-10-27T07:00:00Z" and "(7 days)" in result.brands["swoveralls"][0].detail


def test_a_rest_of_zero_stops_this_run_only_and_remembers_nothing(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    result = go(store, tmp_path, swov, cfg, replace(settings, cooldown_days={"instagram": 0}), f)
    assert statuses(result) == [("instagram", "skipped")] and "It will not be used again in this run." in result.brands["swoveralls"][0].detail
    assert store.read_cooldowns() == {} and result.resting == {}


def test_a_site_with_no_rest_setting_is_not_rested(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    go(store, tmp_path, swov, cfg, replace(settings, cooldown_days={}), f)
    assert store.read_cooldowns() == {}


def test_each_site_rests_on_its_own(tmp_path, store, swov, cfg, settings):
    tt = site(cfg, "tiktok")
    f = world(cfg)
    f.browsers["tiktok"] = FakeBrowser({"https://www.tiktok.com/tag/": {"present": [tt["warning_any"][0]], "text": "Your account has been suspended"}},
                                      present_always={tt["logged_in_any"][0]})
    result = go(store, tmp_path, swov, cfg, settings, f, sources=("tiktok", "instagram"))
    assert statuses(result) == [("tiktok", "skipped"), ("instagram", "ran")]
    assert list(store.read_cooldowns()) == ["tiktok"]


def test_a_warning_in_one_brand_keeps_the_account_away_from_the_next_brand_in_the_same_run(tmp_path, store, brands, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    for slug in ("swoveralls", "duderobe"):
        write_research(tmp_path, brands[slug], ONE)
    result = C.run_collect([brands["swoveralls"], brands["duderobe"]], store, browser_factory=f, creds=CREDS, settings=settings,
                           browser_cfg=cfg, ledger_cls=R.Ledger, sources=["instagram"], now=NOW, run_id="r", root=str(tmp_path / "brands"))
    assert statuses(result, "swoveralls") == [("instagram", "skipped")] and statuses(result, "duderobe") == [("instagram", "skipped")]
    assert result.brands["duderobe"][0].detail.startswith("instagram: instagram showed an account warning")
    assert f.browsers["instagram"].opened == ["https://www.instagram.com/", "https://www.instagram.com/explore/tags/a/"]   # opened once, then left alone


def test_a_plain_security_check_is_not_an_account_warning_and_starts_no_rest(tmp_path, store, swov, cfg, settings):
    tt = site(cfg, "tiktok")
    f = world(cfg)
    f.browsers["tiktok"] = FakeBrowser({"https://www.tiktok.com/": {"present": [tt["challenge_any"][0]]}}, present_always={tt["logged_in_any"][0]})
    result = go(store, tmp_path, swov, cfg, settings, f, sources=("tiktok",))
    assert statuses(result) == [("tiktok", "skipped")] and "security check" in result.brands["swoveralls"][0].detail
    assert store.read_cooldowns() == {} and result.resting == {} and "will rest" not in result.brands["swoveralls"][0].detail


def test_a_dry_run_shows_a_rest_but_never_starts_or_changes_one(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}})
    before = (tmp_path / "data" / "_shared" / "cooldowns.json").read_text()
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), now="2026-10-21T07:00:00Z", dry_run=True)
    assert statuses(result) == [("instagram", "skipped")] and (tmp_path / "data" / "_shared" / "cooldowns.json").read_text() == before


def test_a_dry_run_never_writes_a_new_rest(tmp_path, store, swov, cfg, settings):
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), dry_run=True)
    assert statuses(result) == [("instagram", "would_run")] and store.read_cooldowns() == {}


# ---- with Apify ---------------------------------------------------------------------------------------------------------------

def test_while_an_account_rests_apify_in_fallback_mode_still_collects(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}})
    http, apify = apify_world()
    f = world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, now="2026-10-21T07:00:00Z", backend="fallback", apify=apify)
    assert statuses(result) == [("instagram", "ran")] and "instagram" not in f.opened and not result.problems
    assert result.brands["swoveralls"][0].detail == "1 items via Apify" and len(http.calls) == 1


def test_a_warning_in_fallback_mode_is_rescued_by_apify_but_still_flagged_and_rested(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, backend="fallback", apify=apify)
    assert statuses(result) == [("instagram", "ran")] and list(result.needs_you) == ["instagram"] and not result.problems_for
    assert "via Apify (the browser failed: instagram: instagram showed an account warning" in result.brands["swoveralls"][0].detail
    assert list(store.read_cooldowns()) == ["instagram"]


def test_in_extra_mode_a_resting_account_is_apify_alone(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": WARNING}})
    http, apify = apify_world()
    f = world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, f, now="2026-10-21T07:00:00Z", backend="extra", apify=apify)
    assert statuses(result) == [("instagram", "ran")] and "instagram" not in f.opened


# ---- the memory of a rest ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry,active", [
    ({"until": "2026-10-23T07:00:00Z"}, True), ({"until": "2026-10-21T07:00:01Z"}, True), ({"until": "2026-10-21T07:00:00Z"}, False),
    ({"until": "2026-10-20T00:00:00Z"}, False), ({}, False), ({"until": "soon"}, False), ({"until": None}, False), ({"until": 5}, False),
    ("a string", False), (None, False), ([], False)])
def test_a_rest_is_active_only_until_its_end(entry, active):
    assert bool(C.active_cooldowns({"instagram": entry}, "2026-10-21T07:00:00Z")) is active


def test_no_memory_means_nothing_resting():
    assert C.active_cooldowns(None, NOW) == {} and C.active_cooldowns({}, NOW) == {} and C.active_cooldowns("x", NOW) == {}


@pytest.mark.parametrize("days,expected", [(1, "2026-10-21T07:00:00Z"), (3, "2026-10-23T07:00:00Z"), (30, "2026-11-19T07:00:00Z")])
def test_the_end_of_a_rest_is_the_start_plus_its_days(days, expected):
    assert C.rest_until(NOW, days) == expected


def test_freeing_accounts_removes_only_the_resting_ones(store):
    store.write_cooldowns({"instagram": {"until": THREE_DAYS, "at": NOW, "reason": "a"}, "tiktok": {"until": "2026-10-01T00:00:00Z", "at": NOW, "reason": "old"}})
    assert C.clear_cooldowns(store, "2026-10-21T07:00:00Z") == 1
    assert list(store.read_cooldowns()) == ["tiktok"]


def test_freeing_when_nothing_rests_changes_nothing(store, tmp_path):
    assert C.clear_cooldowns(store, NOW) == 0 and not (tmp_path / "data").exists()


def test_reading_the_memory_creates_nothing_and_a_round_trip_keeps_everything(store, tmp_path):
    assert store.read_cooldowns() == {} and not (tmp_path / "data").exists()
    data = {"instagram": {"until": THREE_DAYS, "at": NOW, "reason": "é — x"}}
    store.write_cooldowns(data)
    assert store.read_cooldowns() == data


@pytest.mark.parametrize("content", ["{ not json", "[]", '"text"', "null", "5"])
def test_a_damaged_memory_means_nothing_resting_never_a_crash(store, tmp_path, content):
    path = tmp_path / "data" / "_shared" / "cooldowns.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert store.read_cooldowns() == {}


# ---- the settings -----------------------------------------------------------------------------------------------------------

def test_the_defaults_never_rest_instagram_and_rest_tiktok_for_three_days():
    assert C.load_collector_settings("does-not-exist.yaml").cooldown_days == {"instagram": 0, "tiktok": 3}
    assert C.load_collector_settings("research.yaml").cooldown_days == {"instagram": 0, "tiktok": 3}


def test_one_site_can_be_changed_without_losing_the_other(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("collectors:\n  safety:\n    cooldown_days:\n      instagram: 7\n")
    assert C.load_collector_settings(path).cooldown_days == {"instagram": 7, "tiktok": 3}


def test_zero_and_gemini_are_allowed(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("collectors:\n  safety:\n    cooldown_days:\n      instagram: 0\n      gemini: 2\n")
    assert C.load_collector_settings(path).cooldown_days == {"instagram": 0, "tiktok": 3, "gemini": 2}


@pytest.mark.parametrize("body,match", [
    ("      facebook: 3\n", "unknown 'facebook'"), ("      instagram: -1\n", "instagram must be a number of at least 0"),
    ("      instagram: 1.5\n", "must be a whole number"), ("      instagram: lots\n", "instagram must be a number"),
    ("      instagram: true\n", "instagram must be a number")])
def test_a_bad_rest_is_refused_naming_it(tmp_path, body, match):
    path = tmp_path / "r.yaml"
    path.write_text("collectors:\n  safety:\n    cooldown_days:\n" + body)
    with pytest.raises(C.CollectError, match=match):
        C.load_collector_settings(path)


def test_a_rest_list_that_is_not_a_list_of_pairs_is_refused(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("collectors:\n  safety:\n    cooldown_days: 3\n")
    with pytest.raises(C.CollectError, match="cooldown_days must be a list of site: days pairs"):
        C.load_collector_settings(path)


def test_an_unknown_safety_setting_is_refused(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("collectors:\n  safety:\n    colour: 1\n")
    with pytest.raises(C.CollectError, match="unknown 'colour'"):
        C.load_collector_settings(path)


def test_research_yaml_explains_the_rest_and_the_way_out():
    text = open("research.yaml", encoding="utf-8").read()
    assert "cooldown_days:" in text and "ignore_cooldown" in text and "rests it for this many days" in text


# ---- the command line --------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg = B.load_browser_settings("browser.yaml")
    state = {"factory": world(cfg)}
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda browser_cfg, creds, debug: state["factory"])
    for name in list(ENV) + ["INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TIKTOK_USERNAME", "TIKTOK_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    brands = tmp_path / "brands"
    shutil.copytree(BRANDS_DIR, brands)
    (brands / "swoveralls" / "research.yaml").write_text(yaml.safe_dump(FULL), encoding="utf-8")
    Store(tmp_path / "data").write_cooldowns({"instagram": {"until": "2099-01-01T00:00:00Z", "at": NOW, "reason": WARNING}})

    def run(*args, env=ENV):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return cli.main(["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands), "--research-config", "research.yaml",
                         "--browser-config", BROWSER_YAML, "--debug-dir", str(tmp_path / "debug"), *args])
    return state, run, tmp_path


def test_cli_check_leaves_a_resting_account_alone_and_says_so(cli_env, capsys):
    state, run, _ = cli_env
    assert run("--check", "--sources", "instagram,tiktok") == 0
    out = capsys.readouterr().out
    assert "instagram: skipped: resting until 2099-01-01 after an account warning" in out and "add --ignore-cooldown once you have dealt with it" in out
    assert "instagram" not in state["factory"].opened and "tiktok" in state["factory"].opened


def test_cli_recon_leaves_a_resting_account_alone_too(cli_env, capsys):
    state, run, _ = cli_env
    assert run("--recon", "--sources", "instagram") == 0
    assert "instagram: skipped: resting until 2099-01-01" in capsys.readouterr().out and "instagram" not in state["factory"].opened


def test_cli_ignore_cooldown_uses_the_account_again_and_forgets_the_rest(cli_env, capsys):
    state, run, tmp_path = cli_env
    assert run("--check", "--sources", "instagram", "--ignore-cooldown") == 0
    out = capsys.readouterr().out
    assert "1 resting account(s) will be used again: you said the warning has been dealt with." in out
    assert "instagram" in state["factory"].opened and Store(tmp_path / "data").read_cooldowns() == {}


def test_cli_ignore_cooldown_on_a_plan_changes_nothing(cli_env, capsys):
    state, run, tmp_path = cli_env
    assert run("--all", "--dry-run", "--sources", "instagram", "--ignore-cooldown") == 0
    assert "will be used again" not in capsys.readouterr().out and list(Store(tmp_path / "data").read_cooldowns()) == ["instagram"]


def test_cli_a_real_run_reports_the_rest_and_skips_the_account(cli_env, capsys):
    state, run, _ = cli_env
    assert run("--all", "--sources", "instagram,tiktok") == 0
    out = capsys.readouterr().out
    assert "Resting: instagram until 2099-01-01" in out and "instagram" not in state["factory"].opened


def test_cli_region_reaches_the_ad_searches_and_worldwide_is_the_default(cli_env):
    state, run, _ = cli_env
    assert run("--all", "--sources", "meta_ads") == 0
    searches = [u for u in state["factory"].browsers["meta_ads"].opened if "&q=" in u]           # the keyword searches (a page link is used as written)
    assert searches and all("country=ALL&" in u for u in searches)
    state["factory"] = world(B.load_browser_settings(BROWSER_YAML))
    assert run("--all", "--sources", "meta_ads", "--region", "gb") == 0
    searches = [u for u in state["factory"].browsers["meta_ads"].opened if "&q=" in u]
    assert searches and all("country=GB&" in u for u in searches)


def test_the_workflow_offers_ignore_cooldown_and_passes_it_on():
    wf = yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))
    box = wf[True]["workflow_dispatch"]["inputs"]["ignore_cooldown"]
    assert box["type"] == "boolean" and box["default"] is False and "dealt with the warning" in box["description"]
    step = next(s for s in wf["jobs"]["collect"]["steps"] if "IGNORE_COOLDOWN" in (s.get("env") or {}))
    assert step["env"]["IGNORE_COOLDOWN"] == "${{ inputs.ignore_cooldown }}" and '--ignore-cooldown' in step["run"]
