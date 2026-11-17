"""Update 68: the rest of a warned account does not wait for the general rest guard (which ships OFF)."""
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import guards
from contentintel.store import Store

from test_account_warning import go, warned_page, world


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def test_a_warned_account_is_rested_for_a_week_even_when_the_general_rest_guard_is_off(tmp_path, store, swov, cfg, monkeypatch):
    monkeypatch.setattr(guards, "on", lambda name: False)                                                      # as in production: guards ship OFF (the tests normally run with them ON)
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    result = go(store, tmp_path, swov, cfg, C.load_collector_settings("research.yaml"), f)
    assert store.read_cooldowns()["instagram"]["until"] == "2026-10-27T07:00:00Z" and "The account will rest until 2026-10-27 (7 days)" in result.brands["swoveralls"][0].detail
    f2 = world(cfg)
    f2.browsers["instagram"] = warned_page(cfg)
    (tmp_path / "zero").mkdir()
    store2 = Store(tmp_path / "zero")
    result2 = go(store2, tmp_path / "zero", swov, cfg, replace(C.load_collector_settings("research.yaml"), cooldown_days={"instagram": 0, "tiktok": 7}), f2)
    assert store2.read_cooldowns() == {} and "It will not be used again in this run." in result2.brands["swoveralls"][0].detail      # 0 still means this run only


def test_ignore_cooldown_also_skips_the_gap_between_reads_for_that_run_and_the_caps_still_apply(tmp_path, swov, monkeypatch):
    from careful import careful_text
    from pathlib import Path
    from test_careful_mode import go as cgo, ig_world, statuses
    text = careful_text(Path("browser.yaml").read_text(encoding="utf-8"), gap=True)
    (tmp_path / "browser.yaml").write_text(text, encoding="utf-8")
    cfg = B.load_browser_settings(str(tmp_path / "browser.yaml"))
    assert cfg["sites"]["instagram"]["min_hours_between_runs"] == 12
    store = Store(tmp_path / "data")
    store.write_page_log({"days": {}, "cursor": {}, "last": {"instagram": "2026-10-20T03:00:00Z"}})              # read 4 hours ago
    settings = C.load_collector_settings("research.yaml")
    blocked = cgo(store, tmp_path, swov, cfg, settings, ig_world(cfg))
    assert statuses(blocked) == [("instagram", "skipped")]
    f = ig_world(cfg)
    allowed = cgo(store, tmp_path, swov, cfg, settings, f, ignore_gap=True)
    assert statuses(allowed) == [("instagram", "ran")] and len([u for u in f.browsers["instagram"].opened if "/explore/tags/" in u]) == 2          # the gap was skipped; the 2-page cap was not
    import re
    assert re.search(r"ignore_gap=bool\(args\.ignore_cooldown\)", Path("contentintel/cli.py").read_text(encoding="utf-8")) and "also skips the 12-hour gap" in Path(".github/workflows/collect.yml").read_text(encoding="utf-8")


def test_a_written_rest_is_honoured_by_the_next_run_even_with_the_general_guard_off(tmp_path, store, swov, cfg, monkeypatch):
    monkeypatch.setattr(guards, "on", lambda name: False)                                                        # production: guards ship OFF; the rest used to be written and never seen
    f = world(cfg)
    f.browsers["instagram"] = warned_page(cfg)
    settings = C.load_collector_settings("research.yaml")
    go(store, tmp_path, swov, cfg, settings, f)
    assert "instagram" in C.active_cooldowns(store.read_cooldowns(), "2026-10-21T07:00:00Z")                      # the helper itself sees it
    again = world(cfg)
    (tmp_path / "again").mkdir()
    result = go(store, tmp_path / "again", swov, cfg, settings, again, now="2026-10-21T07:00:00Z")
    assert [(r.source, r.status) for r in result.brands["swoveralls"]] == [("instagram", "skipped")] and "instagram" not in again.opened and "instagram" in result.resting
    assert result.brands["swoveralls"][0].detail.startswith("resting until 2026-10-27 after an account warning")
    assert C.clear_cooldowns(store, "2026-10-21T07:00:00Z") == 1                                                  # and the tick "ignore_cooldown" really frees exactly the resting account


class Log:
    def __init__(self):
        self.entries = [{"human_score": 30}, {"human_score": 100}, {"human_score": 30}]

    def summary(self):
        return {"human_score": round(sum(e["human_score"] for e in self.entries) / len(self.entries))}


class Browser:
    def __init__(self):
        self.read_log = Log()

    def _note_read(self):
        self.read_log.entries[-1]["human_score"] = 58                                                            # the last read's final figures arrive when it is noted


def test_the_report_summary_is_taken_after_the_last_read_is_final():
    assert C.final_read_summary(Browser()) == {"human_score": 63}                                                # not 53
    assert C.final_read_summary(type("B", (), {"read_log": None})()) is None and C.final_read_summary(object()) is None
    broken = Browser()
    broken._note_read = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert C.final_read_summary(broken) == {"human_score": 53}                                                   # a noting that fails still gives a summary


def test_an_old_rest_without_a_kind_is_honoured_when_the_tool_wrote_it_and_a_bare_entry_is_not(monkeypatch):
    monkeypatch.setattr(guards, "on", lambda name: False)
    now = "2026-11-17T08:00:00Z"
    legacy = {"instagram": {"until": "2026-11-19T00:00:00Z", "at": "2026-11-15T08:00:00Z", "reason": "rested by hand"}}          # what Collect #62 wrote, before the kind field existed
    assert "instagram" in C.active_cooldowns(legacy, now)
    assert C.active_cooldowns({"instagram": {"until": "2026-11-19T00:00:00Z", "kind": "account_warning"}}, now) and C.active_cooldowns({"tiktok": {"until": "2026-11-19T00:00:00Z", "kind": "owner"}}, now)
    assert C.active_cooldowns({"instagram": {"until": "2099-01-01T00:00:00Z"}}, now) == {}                                      # a bare entry is the guard's business, and the guard is off
    assert C.active_cooldowns({"instagram": {"until": "2026-11-16T00:00:00Z", "reason": "x"}}, now) == {}                       # an ended rest is over
    monkeypatch.setattr(guards, "on", lambda name: True)
    assert "instagram" in C.active_cooldowns({"instagram": {"until": "2099-01-01T00:00:00Z"}}, now)                          # with the guard ON, as before
