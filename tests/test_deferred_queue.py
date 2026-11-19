"""Search-first hashtags, graceful failure on a security check or login wall, and the deferred queue.

Covers the captcha-layer build in collectors.py: a site can search `#tag` first (hashtag_via_search_first,
default off) with the tag page as the fallback; one source failing never fails the whole run (partial success,
unless nothing was delivered at all); and the missed items are queued in data/_shared/deferred-queue.json
for the next Collect, at most 3 deferrals, at most 50 entries.
"""
import json
import shutil
import types
from dataclasses import replace

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeBrowser, GeminiFake
from test_collectors import BROWSER_YAML, ENV, FULL, collect, site, statuses, world
from test_tiktok_puzzle_and_search import TAG, TikTok

NOW = "2026-10-20T07:00:00Z"


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def search_first_site(cfg, **extra):
    site_cfg = {**cfg["sites"]["tiktok"], "settle_seconds": 0, "hashtag_via_search_first": True, **extra}
    return site_cfg


def read_tag(cfg, browser, *, site_cfg=None):
    task = C.Task("tiktok", "tags", {"hashtags": ["overalls"]}, "k")
    return C.run_browser_task(browser, task, 10, site_cfg or {**cfg["sites"]["tiktok"], "settle_seconds": 0}, cfg, {})


def challenged(cfg, name):
    return FakeBrowser({}, present_always={site(cfg, name)["challenge_any"][0]})


def queue_file(tmp_path):
    return tmp_path / "data" / "_shared" / "deferred-queue.json"


def read_queue(tmp_path):
    return json.loads(queue_file(tmp_path).read_text(encoding="utf-8"))


# --- search-first -----------------------------------------------------------------------------------------------

def test_search_first_reads_from_search_and_never_opens_the_tag_page(cfg):
    b = TikTok(cfg, data_on={"typed"})
    rows, stats = read_tag(cfg, b, site_cfg=search_first_site(cfg))
    assert len(rows) == 2 and b.typed_queries == ["#overalls"] and TAG not in b.opened
    assert stats["search_first"] == ["#overalls"] and stats["search_first_how"] == ["typed"]
    assert stats["tag_fallback"] == [] and stats["fallbacks"] == []
    assert stats["pages"] == 1                                   # the one search that was typed counts as a page, though the tag page was never opened
    assert all(r["found_via"] == "#overalls" for r in rows)


def test_search_first_falls_back_to_the_tag_page_when_the_search_gives_nothing(cfg):
    b = TikTok(cfg, data_on={"tag"})
    rows, stats = read_tag(cfg, b, site_cfg=search_first_site(cfg))
    assert len(rows) == 2 and b.typed_queries == ["#overalls"] and TAG in b.opened
    assert stats["search_first"] == ["#overalls"] and stats["tag_fallback"] == ["#overalls"]
    assert stats["pages"] == 2                                   # the search page, then the tag page


def test_without_the_flag_the_tag_page_is_opened_first_as_before(cfg):
    b = TikTok(cfg, data_on={"tag"})
    # The shipped browser.yaml has the flag on for TikTok, so this test switches it off to cover the old order.
    rows, stats = read_tag(cfg, b, site_cfg={**cfg["sites"]["tiktok"], "settle_seconds": 0, "hashtag_via_search_first": False})
    assert len(rows) == 2 and b.opened[1] == TAG and b.typed_queries == []
    assert stats["search_first"] == [] and stats["tag_fallback"] == []


def test_a_puzzle_on_the_search_page_stops_the_task_without_trying_the_tag_page(cfg):
    b = TikTok(cfg, puzzle_on={"typed"}, closable=False, data_on={"tag"})
    with pytest.raises(B.Challenge, match="security check"):
        read_tag(cfg, b, site_cfg=search_first_site(cfg))
    assert TAG not in b.opened and b.clicks == ["#captcha_close_button"]      # one press, no retry loop


def test_search_first_is_skipped_when_the_site_has_no_search_to_use(cfg):
    site_cfg = {k: v for k, v in cfg["sites"]["tiktok"].items() if k not in ("search_url", "search_box")}
    site_cfg.update(settle_seconds=0, hashtag_via_search_first=True)
    b = TikTok(cfg, data_on={"tag"})
    rows, stats = read_tag(cfg, b, site_cfg=site_cfg)
    assert len(rows) == 2 and b.opened[1] == TAG and b.typed_queries == [] and stats["search_first"] == []


def test_tag_pages_are_found_for_both_tag_and_tags_addresses():
    assert C._tag_in_url("https://www.tiktok.com/tag/overalls") == "overalls"
    assert C._tag_in_url("https://www.instagram.com/explore/tags/overalls/") == "overalls"
    assert C._tag_in_url("https://www.tiktok.com/@acme") is None


def test_page_notes_describe_search_first_and_the_tag_fallback():
    note = C._page_notes({"search_first": ["#overalls"], "search_first_how": ["typed"], "tag_fallback": ["#overalls"]})
    assert "searched first" in note and "typed into the search box" in note and "tag page was opened instead" in note
    assert "searched first" in C._page_notes({"search_first": ["#a"], "search_first_how": ["address"]})
    assert C._page_notes({"search_first": [], "tag_fallback": []}) == ""


# --- graceful failure -------------------------------------------------------------------------------------------

def test_a_challenged_source_is_skipped_and_the_rest_of_the_run_is_a_partial_success(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "skipped"), ("instagram", "ran")]
    assert result.partial_success and not result.problems
    assert result.skipped_sources["tiktok"]["reason"] == "challenge"
    assert result.skipped_sources["tiktok"]["url"] == "https://www.tiktok.com/"
    assert result.skipped_sources["tiktok"]["screenshot"] == "challenge-tiktok"
    text = C.format_collect_report(result, settings)
    assert "PARTIAL SUCCESS" in text and "SKIPPED SOURCES" in text and "challenge-tiktok" in text
    assert "To clear it: log in once as the dummy account" in text


def test_skip_details_carry_the_human_score_and_egress_ip(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    f.browsers["tiktok"].read_log = types.SimpleNamespace(summary=lambda: {
        "pages": 1, "engine": "patchright", "identity": "tiktok", "proxy": None,
        "egress_ip": "57.151.122.144", "human_score": 76, "dwell_seconds": 12.3})
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    detail = result.skipped_sources["tiktok"]
    assert (detail["human_score"], detail["egress_ip"], detail["engine"]) == (76, "57.151.122.144", "patchright")
    text = C.format_collect_report(result, settings)
    assert "human score 76/100" in text and "egress IP 57.151.122.144" in text


def test_a_login_wall_is_skipped_the_same_way(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "skipped")]
    assert result.partial_success and not result.problems
    assert result.skipped_sources["instagram"]["reason"] == "login_wall"
    assert result.skipped_sources["instagram"]["screenshot"] == "login-wall-instagram"


def test_when_every_source_is_blocked_the_run_fails(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    f.browsers["instagram"] = challenged(cfg, "instagram")
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("tiktok", "instagram"))
    assert statuses(result) == [("tiktok", "skipped"), ("instagram", "skipped")]
    assert not result.partial_success and result.problems
    assert "FAILED: every source was stopped" in C.format_collect_report(result, settings)


def test_a_deep_research_login_failure_still_fails_the_run(tmp_path, store, swov, cfg, settings):
    gem = GeminiFake(site(cfg, "gemini"), logged_in=False)
    result = collect(store, tmp_path, [swov], {"deep_research": {"questions": ["Who are the competitors?"]}},
                     world(cfg, gem=gem), cfg, settings, sources=("deep_research",))
    assert statuses(result) == [("deep_research", "failed")] and result.problems_for == ["gemini"]
    assert result.skipped_sources["gemini"]["reason"] == "login_wall"


# --- the deferred queue -----------------------------------------------------------------------------------------

def test_a_missed_source_is_queued_for_the_next_run(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    (entry,) = read_queue(tmp_path)["entries"]
    assert (entry["brand"], entry["source"], entry["seeds"], entry["deferrals"], entry["reason"]) == \
        ("swoveralls", "tiktok", {"hashtags": ["overalls"]}, 1, "challenge")
    text = C.format_collect_report(result, settings)
    assert "Deferred queue (data/_shared/deferred-queue.json)" in text
    assert "queued: swoveralls tiktok: 1 hashtags/profiles/searches (deferral 1 of 3)" in text
    assert "waiting: swoveralls tiktok: 1 hashtags/profiles/searches (deferral 1 of 3" in text


def test_a_queued_item_is_retried_and_drops_out_when_collected(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("tiktok",))
    assert len(read_queue(tmp_path)["entries"]) == 1
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings, sources=("tiktok",),
                     now="2026-10-21T07:00:00Z")
    assert statuses(result) == [("tiktok", "ran")] and read_queue(tmp_path)["entries"] == []
    assert "collected after deferral: swoveralls tiktok: 1 hashtags/profiles/searches" in \
        C.format_collect_report(result, settings)


def test_a_queued_item_the_plan_does_not_cover_runs_first(tmp_path, store, swov, cfg, settings):
    entry = {"brand": "swoveralls", "source": "tiktok", "seeds": {"hashtags": ["b"]},
             "label": "1 hashtag(s)/profile(s)/search(es)", "reason": "challenge", "detail": "a puzzle",
             "first_deferred": NOW, "last_deferred": NOW, "deferrals": 1}
    C.save_deferred_queue(store, {"entries": [entry]})
    result = collect(store, tmp_path, [swov], {"social": {"tiktok": {"hashtags": ["a"]}}}, world(cfg), cfg, settings,
                     sources=("tiktok",))
    reports = result.brands["swoveralls"]
    assert [r.seeds for r in reports] == ["1 hashtag(s)/profile(s)/search(es)", "1 hashtags/profiles/searches"]
    assert [r.status for r in reports] == ["ran", "ran"] and read_queue(tmp_path)["entries"] == []


def test_a_miss_after_three_deferrals_is_dropped_with_a_note(tmp_path, store, swov, cfg, settings):
    entry = {"brand": "swoveralls", "source": "tiktok", "seeds": {"hashtags": ["overalls"]},
             "label": "1 hashtags/profiles/searches", "reason": "challenge", "detail": "a puzzle",
             "first_deferred": NOW, "last_deferred": NOW, "deferrals": 3}
    C.save_deferred_queue(store, {"entries": [entry]})
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("tiktok",))
    assert read_queue(tmp_path)["entries"] == []
    text = C.format_collect_report(result, settings)
    assert "dropped: swoveralls tiktok: 1 hashtags/profiles/searches was missed again after 3 deferrals" in text


def test_the_queue_never_grows_past_its_cap():
    queue = {"entries": [{"brand": f"b{i}", "source": "tiktok", "seeds": {"hashtags": [f"t{i}"]},
                          "label": "1 hashtags/profiles/searches", "reason": "challenge", "detail": "",
                          "first_deferred": f"2026-10-{i + 1:02d}T07:00:00Z",
                          "last_deferred": f"2026-10-{i + 1:02d}T07:00:00Z", "deferrals": 1} for i in range(50)]}
    task = C.Task("tiktok", "1 hashtags/profiles/searches", {"hashtags": ["new"]}, "k", 1)
    entry, dropped = C.defer_blocked(queue, "swoveralls", task, "challenge", "a puzzle", NOW)
    assert entry is not None and len(queue["entries"]) == 50
    assert {e["brand"] for e in queue["entries"]} == {f"b{i}" for i in range(1, 50)} | {"swoveralls"}
    assert len(dropped) == 1 and "deferred queue is full (50 entries)" in dropped[0]


def test_a_corrupt_queue_file_is_read_as_empty_and_rewritten(tmp_path, store, swov, cfg, settings):
    path = queue_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{", encoding="utf-8")
    assert C.load_deferred_queue(store) == {"entries": []}
    f = world(cfg)
    f.browsers["tiktok"] = challenged(cfg, "tiktok")
    collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("tiktok",))
    assert len(read_queue(tmp_path)["entries"]) == 1


def test_a_dry_run_never_writes_the_queue(tmp_path, store, swov, cfg, settings):
    entry = {"brand": "swoveralls", "source": "tiktok", "seeds": {"hashtags": ["overalls"]},
             "label": "1 hashtags/profiles/searches", "reason": "challenge", "detail": "a puzzle",
             "first_deferred": NOW, "last_deferred": NOW, "deferrals": 1}
    C.save_deferred_queue(store, {"entries": [entry]})
    before = queue_file(tmp_path).read_text(encoding="utf-8")
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings, dry_run=True)
    assert queue_file(tmp_path).read_text(encoding="utf-8") == before
    assert statuses(result)[1] == ("tiktok", "would_run")
    assert "waiting: swoveralls tiktok: 1 hashtags/profiles/searches (deferral 1 of 3" in \
        C.format_collect_report(result, settings)


def test_a_deferred_entry_for_a_resting_site_stays_queued_untried(tmp_path, store, swov, cfg, settings):
    store.write_cooldowns({"instagram": {"until": "2026-10-23T07:00:00Z", "at": NOW, "reason": "a warning"}})
    entry = {"brand": "swoveralls", "source": "instagram", "seeds": {"hashtags": ["overalls"]},
             "label": "1 hashtags", "reason": "challenge", "detail": "a puzzle",
             "first_deferred": NOW, "last_deferred": NOW, "deferrals": 1}
    C.save_deferred_queue(store, {"entries": [entry]})
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings, sources=("instagram",),
                     now="2026-10-21T07:00:00Z")
    assert statuses(result) == [("instagram", "skipped")] and len(read_queue(tmp_path)["entries"]) == 1


# --- the command line -------------------------------------------------------------------------------------------

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

    def run(*args, env=ENV):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return cli.main(["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands),
                         "--research-config", "research.yaml", "--browser-config", BROWSER_YAML,
                         "--debug-dir", str(tmp_path / "debug"), *args])
    return state, run, cfg


def test_cli_partial_success_exits_zero_and_says_so(cli_env, capsys):
    state, run, cfg = cli_env
    state["factory"].browsers["tiktok"] = challenged(cfg, "tiktok")
    assert run("--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "PARTIAL SUCCESS" in out and "SKIPPED SOURCES" in out and "::error::" not in out


def test_cli_all_blocked_exits_one(cli_env, capsys):
    state, run, cfg = cli_env
    state["factory"].browsers["tiktok"] = challenged(cfg, "tiktok")
    state["factory"].browsers["instagram"] = challenged(cfg, "instagram")
    assert run("--brand", "swoveralls", "--sources", "tiktok,instagram") == 1
    assert "FAILED: every source was stopped" in capsys.readouterr().out


# ---- Kling's prompt editor: a placeholder overlay takes the pointer click (video trial run #15) ------------------------------------------------------------------------------------------------------------
class _Loc:
    def __init__(self, click_ok=True, focus_ok=True, force_ok=True, present=True):
        self.click_ok, self.focus_ok, self.force_ok, self.present, self.calls, self.filled = click_ok, focus_ok, force_ok, present, [], None
        self.first = self

    def count(self):
        return 1 if self.present else 0

    def click(self, timeout=0, force=False):
        self.calls.append(("force-click" if force else "click", timeout))
        if not (self.force_ok if force else self.click_ok):
            raise TimeoutError("an element intercepts pointer events")

    def focus(self, timeout=0):
        self.calls.append(("focus", timeout))
        if not self.focus_ok:
            raise TimeoutError("not focusable")

    def fill(self, text):
        self.filled = text

    def press(self, key):
        self.calls.append(("press", key))


def _typing_browser(locs):
    br = object.__new__(B.PlaywrightBrowser)
    br.human = None
    br.page = types.SimpleNamespace(locator=lambda sel: locs[sel])
    return br


def test_a_prompt_box_under_an_overlay_is_still_typed_into_by_focusing_it():
    box = _Loc(click_ok=False, focus_ok=True)
    assert _typing_browser({"div.tiptap.ProseMirror": box}).type_text(["div.tiptap.ProseMirror"], "a red bicycle") is True
    assert [c[0] for c in box.calls] == ["click", "focus"] and box.filled == "a red bicycle"                    # the blocked click does not stop it, and nothing is forced when focus works


def test_a_box_that_can_neither_be_clicked_nor_focused_is_clicked_with_the_hit_check_off_and_a_box_that_cannot_be_reached_at_all_moves_to_the_next_selector():
    forced = _Loc(click_ok=False, focus_ok=False, force_ok=True)
    assert _typing_browser({"a": forced}).type_text(["a"], "x") is True and [c[0] for c in forced.calls] == ["click", "focus", "force-click"] and forced.filled == "x"
    dead, good = _Loc(click_ok=False, focus_ok=False, force_ok=False), _Loc()
    assert _typing_browser({"dead": dead, "good": good}).type_text(["dead", "good"], "y") is True and dead.filled is None and good.filled == "y"
    assert _typing_browser({"dead": _Loc(click_ok=False, focus_ok=False, force_ok=False)}).type_text(["dead"], "z") is False
    plain = _Loc()
    _typing_browser({"p": plain}).type_text(["p"], "ok", submit=True)
    assert [c[0] for c in plain.calls] == ["click", "press"] and plain.calls[0][1] == 3000                      # an ordinary box is clicked as before, only without the 8 second wait on a blocked one
