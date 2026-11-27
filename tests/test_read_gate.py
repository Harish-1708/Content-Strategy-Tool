"""One gate for every read of a careful account, shared by Collect and Acceptance (Update 73 v6)."""
from types import SimpleNamespace

from contentintel import cli
from contentintel import collectors as C
from contentintel import read_gate as RG
from contentintel.store import Store

CFG = {"min_hours_between_runs": 12, "max_pages_per_day": 4}


def test_a_read_is_recorded_with_its_clock_its_pages_for_the_day_and_how_it_ended_and_the_history_keeps_the_last_ten():
    log = {"days": {}, "cursor": {}, "last": {}}
    entry = RG.record(log, "instagram", "2026-11-26T09:38:00Z", by="collect", state=RG.LOGIN_WALL, pages=0, detail="  instagram: the site showed   its login page ")
    assert log["last"]["instagram"] == "2026-11-26T09:38:00Z" and log["outcomes"]["instagram"] == entry and entry["detail"] == "instagram: the site showed its login page" and entry["by"] == "collect" and log["days"] == {}
    RG.record(log, "instagram", "2026-11-26T22:00:00Z", by="acceptance", state=RG.OK, pages=1, detail="ok")
    RG.record(log, "instagram", "2026-11-26T22:30:00Z", by="collect", state=RG.OK, pages=2, detail="ok")
    assert log["days"]["2026-11-26"]["instagram"] == 3                                                                                            # pages count toward the day, whoever read them
    for k in range(15):
        RG.record(log, "tiktok", f"2026-11-2{k % 10}T0{k % 9}:00:00Z", by="collect", state=RG.OK, pages=0)
    assert len(log["history"]["tiktok"]) == RG.HISTORY == 10 and len(log["history"]["instagram"]) == 3
    assert RG.record({}, "x", "2026-11-26T09:00:00Z", by="collect", state=RG.OK)["pages"] == 0                                                      # a bare dict works


def test_how_a_read_ended_is_told_from_its_message():
    assert RG.classify("instagram: the site showed its login page, so the session was not accepted from this runner") == RG.LOGIN_WALL
    assert RG.classify("tiktok: a security check (captcha) appeared") == RG.CHALLENGE and RG.classify("instagram: Action Blocked: we suspect automated behavior") == RG.WARNING
    assert RG.classify("the account was warned") == RG.WARNING and RG.classify("the browser crashed") == RG.ERROR and RG.classify("") == RG.ERROR and RG.classify(None) == RG.ERROR


def test_the_gate_lets_a_read_through_only_past_the_gap_and_under_the_daily_cap_and_otherwise_hands_back_the_last_outcome():
    log = {"days": {}, "cursor": {}, "last": {}}
    assert RG.decide(log, "instagram", CFG, "2026-11-26T16:58:00Z") == ("read", None)                                                              # never read: go ahead
    RG.record(log, "instagram", "2026-11-26T09:38:00Z", by="collect", state=RG.LOGIN_WALL, pages=0, detail="wall")
    action, outcome = RG.decide(log, "instagram", CFG, "2026-11-26T16:58:00Z")                                                                    # 7.3 hours later: inside the 12 hour gap
    assert action == "reuse" and outcome["state"] == RG.LOGIN_WALL and outcome["by"] == "collect"
    assert RG.decide(log, "instagram", CFG, "2026-11-26T21:38:00Z")[0] == "read"                                                                  # exactly 12 hours: the gap has passed
    assert RG.decide(log, "instagram", {**CFG, "min_hours_between_runs": 0}, "2026-11-26T10:00:00Z")[0] == "read"                                 # no gap set: none enforced
    log["days"] = {"2026-11-27": {"instagram": 4}}
    log["last"]["instagram"] = "2026-11-26T09:00:00Z"
    capped, again = RG.decide(log, "instagram", CFG, "2026-11-27T20:00:00Z")
    assert capped == "capped" and again["state"] == RG.LOGIN_WALL
    assert RG.decide(log, "instagram", {"min_hours_between_runs": 12}, "2026-11-27T20:00:00Z")[0] == "read"                                       # no cap set: none enforced
    old = {"days": {}, "cursor": {}, "last": {"instagram": "2026-11-26T12:00:00Z"}}
    unknown = RG.decide(old, "instagram", CFG, "2026-11-26T14:00:00Z")
    assert unknown[0] == "reuse" and unknown[1]["state"] == "unknown" and unknown[1]["by"] == "an earlier run"                                    # a clock written before outcomes existed still holds the gap


def test_the_acceptance_row_reports_the_route_from_the_last_read_and_says_it_did_not_read_again():
    ok = RG.reuse_row("instagram", "reuse", {"at": "2026-11-26T09:38:00Z", "by": "collect", "state": RG.OK, "detail": "Collect read 2 page(s)"}, CFG, "2026-11-26T16:58:00Z")
    assert ok == {"source": "instagram", "ok": True, "detail": "proven by collect's read at 09:38 UTC (7.3 h ago): Collect read 2 page(s); not read again (inside the 12 hour gap)"}
    wall = RG.reuse_row("instagram", "reuse", {"at": "2026-11-26T09:38:00Z", "by": "collect", "state": RG.LOGIN_WALL, "detail": "the site showed its login page"}, CFG, "2026-11-26T16:58:00Z")
    assert wall["ok"] is False and wall["detail"] == "not read again (inside the 12 hour gap); collect's read at 09:38 UTC (7.3 h ago) ended with login wall: the site showed its login page"
    unknown = RG.reuse_row("instagram", "reuse", {"at": "2026-11-26T12:00:00Z", "by": "an earlier run", "state": "unknown", "detail": ""}, CFG, "2026-11-26T14:00:00Z")
    assert unknown["ok"] is False and "recorded no outcome, so this route is neither proven nor refuted now; it is checked after the gap" in unknown["detail"]
    capped = RG.reuse_row("instagram", "capped", {"at": "2026-11-27T08:00:00Z", "by": "acceptance", "state": RG.OK, "detail": "fine"}, CFG, "2026-11-27T20:00:00Z")
    assert capped["ok"] is True and "not read again (the day's 4 pages are used)" in capped["detail"]


def test_the_page_log_in_the_store_keeps_the_outcomes_and_history(tmp_path):
    store = Store(tmp_path / "data")
    log = store.read_page_log()
    RG.record(log, "instagram", "2026-11-26T09:38:00Z", by="collect", state=RG.OK, pages=2, detail="fine")
    store.write_page_log(log)
    back = store.read_page_log()
    assert back["outcomes"]["instagram"]["by"] == "collect" and back["history"]["instagram"][0]["pages"] == 2 and back["last"]["instagram"] == "2026-11-26T09:38:00Z" and back["days"]["2026-11-26"]["instagram"] == 2


class Args:
    brand, brands_dir, research_config, browser_config, debug_dir = "swoveralls", "brands", "research.yaml", "browser.yaml", "debug"


def test_acceptance_asks_the_gate_first_and_a_read_it_makes_is_recorded_so_collect_then_waits(tmp_path, monkeypatch):
    import test_stage1_wiring as W
    store = Store(tmp_path / "data")
    log = store.read_page_log()
    RG.record(log, "instagram", "2026-11-26T09:38:00Z", by="collect", state=RG.LOGIN_WALL, pages=0, detail="the site showed its login page")
    store.write_page_log(log)
    reads = []
    monkeypatch.setattr(C, "check_collectors", lambda *a, **k: reads.append(a[-1] if False else 1) or [{"source": "instagram", "ok": True, "detail": "login ok, 3 items"}])
    ctx = cli._acceptance_context(Args, store, W.REG, "2026-11-26T16:58:00Z")
    row = ctx.collect_check("instagram", "browser")
    assert reads == [] and row["ok"] is False and "not read again (inside the 12 hour gap)" in row["detail"] and "ended with login wall" in row["detail"]               # Collect's read 7.3 hours ago stands
    assert store.read_page_log()["last"]["instagram"] == "2026-11-26T09:38:00Z"                                                                      # Acceptance did not move the clock
    cli._BROWSER_FACTORY = lambda *a, **k: None
    try:
        later = cli._acceptance_context(Args, store, W.REG, "2026-11-26T22:00:00Z")
        row2 = later.collect_check("instagram", "browser")
    finally:
        cli._BROWSER_FACTORY = None
    assert reads == [1] and row2["ok"] is True
    after = store.read_page_log()
    assert after["last"]["instagram"] == "2026-11-26T22:00:00Z" and after["outcomes"]["instagram"]["by"] == "acceptance" and after["outcomes"]["instagram"]["state"] == RG.OK and after["days"]["2026-11-26"]["instagram"] == 1
    again = cli._acceptance_context(Args, store, W.REG, "2026-11-26T23:00:00Z").collect_check("instagram", "browser")
    assert again["ok"] is True and "proven by acceptance's read at 22:00 UTC" in again["detail"] and reads == [1]                                     # a second look an hour later reads nothing
    apify = cli._acceptance_context(Args, store, W.REG, "2026-11-26T23:30:00Z")
    assert apify is not None and SimpleNamespace is not None


def test_a_login_wall_met_by_collect_starts_the_gap_and_is_recorded_with_its_outcome(tmp_path, monkeypatch):
    from test_update68 import _gap_world
    cfg, store, Cmod, go, ig_world, statuses, swov = _gap_world(tmp_path, "2026-10-19T18:00:00Z")                                                       # 13 hours ago: a read is due
    monkeypatch.setattr(Cmod, "session_state", lambda br, c: "login_wall")
    result = go(store, tmp_path, swov, cfg, Cmod.load_collector_settings("research.yaml"), ig_world(cfg))
    log = store.read_page_log()
    assert statuses(result) != [("instagram", "ran")] and log["last"]["instagram"] == "2026-10-20T07:00:00Z" and log["outcomes"]["instagram"]["state"] == RG.LOGIN_WALL and log["outcomes"]["instagram"]["by"] == "collect"
    assert "login page" in log["outcomes"]["instagram"]["detail"]


def test_a_collect_read_that_works_is_recorded_as_ok_by_collect(tmp_path):
    from test_update68 import _gap_world
    cfg, store, Cmod, go, ig_world, statuses, swov = _gap_world(tmp_path, "2026-10-19T18:00:00Z")
    result = go(store, tmp_path, swov, cfg, Cmod.load_collector_settings("research.yaml"), ig_world(cfg))
    log = store.read_page_log()
    assert statuses(result) == [("instagram", "ran")] and log["outcomes"]["instagram"]["state"] == RG.OK and log["outcomes"]["instagram"]["by"] == "collect" and log["last"]["instagram"] == "2026-10-20T07:00:00Z"
