"""Update 68: the rest of a warned account does not wait for the general rest guard (which ships OFF)."""
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.store import Store

from test_account_warning import go, warned_page, world


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def test_a_warned_account_is_rested_for_a_week_even_when_the_general_rest_guard_is_off(tmp_path, store, swov, cfg, monkeypatch):
    monkeypatch.setattr(C.guards, "on", lambda name: False)                                                      # as in production: guards ship OFF (the tests normally run with them ON)
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
