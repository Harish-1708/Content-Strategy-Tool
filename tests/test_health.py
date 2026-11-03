"""The health ledger: attempts, rests, a status that does not flap, and trust that has to be earned by a live pass."""
import json

import pytest

from contentintel import contract as K
from contentintel import health as H
from contentintel.store import Store

from stage1_helpers import NOW, at, registry

SRC, RT = "tiktok", "browser"


def ledger(tmp_path, **defaults):
    return H.HealthLedger({}, registry(tmp_path, **defaults))


def passing(ledger_, when=NOW, source=SRC, route=RT, **extra):
    ledger_.record_acceptance({"at": when, "run": "r", "source": source, "route": route, "items": 3, "passed": True, "code": "", "message": "ok",
                               "version": "abc", "route_hash": ledger_.route_hash(source, route), **extra})


def fail(ledger_, code=K.UPSTREAM_BROKEN, when=NOW, source=SRC, route=RT):
    ledger_.observe(source, route, ok=False, code=code, message="boom", now=when)


def win(ledger_, n=1, when=NOW, items=2):
    for _ in range(n):
        ledger_.observe(SRC, RT, ok=True, items=items, now=when)


def entry(ledger_, source=SRC, route=RT):
    return ledger_.state["routes"][f"{source}|{route}"]


# ---- recording --------------------------------------------------------------------------------------------------------------

def test_a_success_and_a_failure_are_recorded_with_their_details(tmp_path):
    led = ledger(tmp_path)
    led.observe(SRC, RT, ok=True, items=4, now=NOW, elapsed_ms=900)
    fail(led, K.TIMEOUT, at(NOW, minutes=5))
    e = entry(led)
    assert e["history"][0] == {"at": NOW, "ok": True, "code": "", "items": 4, "ms": 900}
    assert e["history"][1]["ok"] is False and e["history"][1]["code"] == K.TIMEOUT
    assert e["last_success"] == NOW and e["last_error"] == {"at": at(NOW, minutes=5), "code": K.TIMEOUT, "message": "boom"}


def test_secrets_never_reach_the_ledger(tmp_path):
    led = ledger(tmp_path)
    led.observe(SRC, RT, ok=False, code=K.NEED_SETUP, message="bad key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV and APIFY_TOKEN=apify_api_SECRETSECRETSECRET1", now=NOW)
    text = json.dumps(led.state)
    assert "sk-ant" not in text and "SECRETSECRET" not in text


def test_a_request_problem_says_nothing_about_the_source(tmp_path):
    led = ledger(tmp_path)
    for code in (K.NOT_FOUND, K.BAD_INPUT):
        fail(led, code)
    e = entry(led)
    assert e["consecutive_failures"] == 0 and e["pause"] is None and e["last_error"] is None and e["state"] == "healthy"
    assert [h["ok"] for h in e["history"]] == [None, None]
    assert led.success_rate(SRC, RT, NOW) is None                               # neither a win nor a loss


def test_history_is_capped(tmp_path):
    led = ledger(tmp_path, history_cap=5)
    for i in range(12):
        led.observe(SRC, RT, ok=True, items=i, now=at(NOW, minutes=i))
    assert [h["items"] for h in entry(led)["history"]] == [7, 8, 9, 10, 11]


# ---- rests ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code, seconds", [(K.RATE_LIMITED, 3600), (K.PLATFORM_BLOCKED, 86400), (K.NEED_LOGIN, 43200), (K.TIMEOUT, 60),
                                           (K.UPSTREAM_BROKEN, 120), (K.NEED_SETUP, 60)])
def test_each_failure_rests_the_route_for_its_own_time(tmp_path, code, seconds):
    led = ledger(tmp_path)
    fail(led, code)
    pause = led.is_paused(SRC, RT, NOW)
    assert pause["code"] == code and pause["retry_after_seconds"] == seconds
    assert led.is_paused(SRC, RT, at(NOW, seconds=seconds - 1)) is not None
    assert led.is_paused(SRC, RT, at(NOW, seconds=seconds)) is None            # over exactly when the time is up
    assert SRC in pause["message"] and code in pause["message"]


def test_a_longer_rest_is_never_shortened_by_a_milder_failure(tmp_path):
    led = ledger(tmp_path)
    fail(led, K.PLATFORM_BLOCKED)
    fail(led, K.TIMEOUT, at(NOW, minutes=1))
    assert led.is_paused(SRC, RT, at(NOW, hours=23))["code"] == K.PLATFORM_BLOCKED
    fail(led, K.RATE_LIMITED, at(NOW, hours=23))                               # ends at hour 24, exactly when the block does: nothing changes
    assert led.is_paused(SRC, RT, at(NOW, hours=23, minutes=30))["code"] == K.PLATFORM_BLOCKED
    fail(led, K.NEED_LOGIN, at(NOW, hours=20))                                 # twelve hours from hour 20 reaches hour 32: a longer rest does extend it
    assert led.is_paused(SRC, RT, at(NOW, hours=26))["code"] == K.NEED_LOGIN
    assert led.is_paused(SRC, RT, at(NOW, hours=32)) is None


def test_a_success_ends_the_rest(tmp_path):
    led = ledger(tmp_path)
    fail(led, K.RATE_LIMITED)
    win(led, when=at(NOW, minutes=1))
    assert led.is_paused(SRC, RT, at(NOW, minutes=2)) is None and entry(led)["pause"] is None


def test_a_rest_is_per_route(tmp_path):
    led = ledger(tmp_path)
    fail(led, K.RATE_LIMITED, route="apify")
    assert led.is_paused(SRC, "apify", NOW) and led.is_paused(SRC, "browser", NOW) is None and led.is_paused("instagram", "apify", NOW) is None


def test_a_person_can_clear_a_rest(tmp_path):
    led = ledger(tmp_path)
    fail(led, K.PLATFORM_BLOCKED)
    fail(led, K.PLATFORM_BLOCKED, route="apify")
    fail(led, K.PLATFORM_BLOCKED, source="instagram")
    assert led.clear(SRC, RT) == 1 and led.is_paused(SRC, RT, NOW) is None and led.is_paused(SRC, "apify", NOW)
    assert led.clear(SRC) == 1 and led.is_paused(SRC, "apify", NOW) is None and led.is_paused("instagram", RT, NOW)
    assert led.clear("tiktok") == 0 and entry(led)["consecutive_failures"] == 0


# ---- a status that does not flap --------------------------------------------------------------------------------------------

def test_a_route_turns_degraded_only_after_enough_failures_in_a_row(tmp_path):
    led = ledger(tmp_path)                                                      # shipped thresholds: 3 failures, 2 successes
    fail(led)
    fail(led)
    assert entry(led)["state"] == "healthy"
    win(led)                                                                    # a success in between starts the count again
    fail(led)
    fail(led)
    assert entry(led)["state"] == "healthy"
    fail(led)
    assert entry(led)["state"] == "degraded" and entry(led)["consecutive_failures"] == 3


def test_a_degraded_route_recovers_only_after_enough_successes_in_a_row(tmp_path):
    led = ledger(tmp_path, degrade_after_failures=2, recover_after_successes=3)
    fail(led)
    fail(led)
    assert entry(led)["state"] == "degraded"
    win(led, 2)
    assert entry(led)["state"] == "degraded"
    fail(led)                                                                   # a flicker resets the recovery
    win(led, 2)
    assert entry(led)["state"] == "degraded"
    win(led)
    assert entry(led)["state"] == "healthy"


# ---- trust ------------------------------------------------------------------------------------------------------------------

def test_nothing_is_healthy_without_a_live_acceptance_pass(tmp_path):
    led = ledger(tmp_path)
    assert led.status(SRC, RT, NOW) == "unverified" and not led.verified(SRC, RT, NOW)
    win(led, 50)                                                                # fifty ordinary successes do not earn trust
    assert led.status(SRC, RT, NOW) == "unverified"
    passing(led)
    assert led.status(SRC, RT, NOW) == "healthy" and led.verified(SRC, RT, NOW)


def test_a_pass_goes_stale(tmp_path):
    led = ledger(tmp_path)
    passing(led)
    assert led.status(SRC, RT, at(NOW, days=14)) == "healthy"
    assert led.status(SRC, RT, at(NOW, days=14, seconds=1)) == "unverified"
    assert led.verified(SRC, RT, at(NOW, days=14)) and not led.verified(SRC, RT, at(NOW, days=14, seconds=1))


def test_a_pass_belongs_to_the_route_as_it_was_declared(tmp_path):
    reg = registry(tmp_path)
    box = {"hash": "aaa"}
    led = H.HealthLedger({}, reg, hash_for=lambda s, r: box["hash"])
    passing(led, route_hash="aaa")
    assert led.status(SRC, RT, NOW) == "healthy"
    box["hash"] = "bbb"                                                         # the route's settings changed since
    assert led.status(SRC, RT, NOW) == "unverified" and not led.verified(SRC, RT, NOW)


def test_a_failed_acceptance_is_degraded_and_a_rest_comes_first(tmp_path):
    led = ledger(tmp_path)
    led.record_acceptance({"at": NOW, "run": "r", "source": SRC, "route": RT, "items": 0, "passed": False, "code": K.UPSTREAM_BROKEN, "message": "nothing",
                           "version": "v", "route_hash": led.route_hash(SRC, RT)})
    assert led.status(SRC, RT, at(NOW, minutes=5)) == "degraded"
    fail(led, K.PLATFORM_BLOCKED, at(NOW, minutes=6))
    assert led.status(SRC, RT, at(NOW, minutes=7)) == "paused"


def test_degraded_state_from_ordinary_runs_shows_even_when_verified(tmp_path):
    led = ledger(tmp_path)
    passing(led)
    for i in range(3):
        fail(led, K.TIMEOUT, at(NOW, minutes=i))
    assert led.status(SRC, RT, at(NOW, hours=1)) == "degraded"


def test_planned_and_disabled_routes_are_never_trusted(tmp_path):
    led = ledger(tmp_path)
    assert led.status("reddit_public", "public_web", NOW) == "planned"
    assert led.status("instagram", "agent_reach", NOW) == "disabled"
    assert not led.scoring_ready("reddit_public", NOW)


def test_scoring_may_depend_only_on_a_source_with_a_healthy_route(tmp_path):
    led = ledger(tmp_path)
    assert not led.scoring_ready("meta_ads", NOW)
    passing(led, source="meta_ads", route="apify")
    assert led.scoring_ready("meta_ads", NOW)
    assert not led.scoring_ready("tiktok", NOW)


def test_undeclared_sources_are_reported_not_ignored(tmp_path):
    led = ledger(tmp_path)
    with pytest.raises(Exception, match="not declared"):
        led.status("myspace", "x", NOW)


# ---- numbers ----------------------------------------------------------------------------------------------------------------

def test_the_success_rate_looks_only_at_the_window(tmp_path):
    led = ledger(tmp_path)
    for days_ago, ok in ((10, False), (9, False), (3, True), (2, True), (1, False), (0, True)):
        led.observe(SRC, RT, ok=ok, code="" if ok else K.UPSTREAM_BROKEN, items=2 if ok else 0, now=at(NOW, days=-days_ago))
    assert led.success_rate(SRC, RT, NOW) == 0.75                               # the two old failures are outside the 7 days
    assert led.success_rate("tiktok", "apify", NOW) is None and led.success_rate("nothing", "x", NOW) is None
    row = next(r for r in led.rows(NOW) if r["route_key"] == "tiktok|browser")
    assert row["success_rate_7d"] == 75 and row["attempts_7d"] == 4 and row["avg_items"] == 2.0


# ---- keeping it -------------------------------------------------------------------------------------------------------------

def test_the_ledger_survives_a_save_and_a_load(tmp_path):
    store = Store(tmp_path / "data")
    reg = registry(tmp_path)
    led = H.HealthLedger.load(store, reg)
    fail(led, K.RATE_LIMITED)
    passing(led, source="meta_ads", route="browser")
    led.save(store)
    again = H.HealthLedger.load(store, reg)
    assert again.state == led.state and again.is_paused(SRC, RT, NOW) and again.status("meta_ads", "browser", NOW) == "healthy"


@pytest.mark.parametrize("junk", ["not json at all", "[1, 2]", '{"routes": []}', '{"routes": {"a|b": 5, "c|d": {"source": "x"}}}', ""])
def test_a_damaged_file_means_an_empty_ledger_never_a_crash(tmp_path, junk):
    store = Store(tmp_path / "data")
    path = tmp_path / "data" / "_shared"
    path.mkdir(parents=True)
    (path / "collector_health.json").write_text(junk, encoding="utf-8")
    led = H.HealthLedger.load(store, registry(tmp_path))
    assert led.state["routes"] == {}
    fail(led)                                                                   # and it is usable straight away
    assert led.state["routes"]


def test_a_partial_entry_is_filled_in(tmp_path):
    led = H.HealthLedger({"routes": {"tiktok|browser": {"source": "tiktok", "route": "browser", "history": [{"at": NOW, "ok": True, "items": 1, "code": "", "ms": 0}, "junk"]}}},
                         registry(tmp_path))
    e = entry(led)
    assert e["pause"] is None and e["state"] == "healthy" and len(e["history"]) == 1


# ---- showing it -------------------------------------------------------------------------------------------------------------

def test_every_declared_route_has_a_row_with_every_column(tmp_path):
    led = ledger(tmp_path)
    passing(led, source="meta_ads", route="browser")
    fail(led, K.RATE_LIMITED)
    rows = led.rows(NOW)
    assert len(rows) == 19 and all(list(r) == H.SHEET_COLUMNS for r in rows)
    by = {r["route_key"]: r for r in rows}
    assert by["meta_ads|browser"]["status"] == "healthy" and by["meta_ads|browser"]["verified"] == "yes" and by["meta_ads|browser"]["acceptance_result"] == "pass"
    t = by["tiktok|browser"]
    assert t["status"] == "paused" and t["error_code"] == K.RATE_LIMITED and t["pause_until"] == at(NOW, hours=1) and t["verified"] == "no"
    assert t["tier"] == "session_based" and t["access_type"] == "site_data" and "refused video downloads" in t["limits"]
    assert by["instagram|agent_reach"]["status"] == "disabled" and "stealth" in by["instagram|agent_reach"]["limits"]
    assert by["reddit_public|public_web"]["status"] == "planned" and by["youtube_public|yt_dlp"]["status"] == "unverified"
    assert by["claude|api"]["route_hash"] == led.route_hash("claude", "api")


def test_the_printed_report_and_matrix_say_what_matters(tmp_path):
    led = ledger(tmp_path)
    passing(led, source="meta_ads", route="browser")
    fail(led, K.PLATFORM_BLOCKED)
    reg, rows = led.registry, led.rows(NOW)
    report = H.format_report(rows)
    assert "meta_ads / browser" in report and "healthy" in report and "resting until" in report and "last error PLATFORM_BLOCKED" in report
    assert "acceptance pass 2026-11-02" in report
    matrix = H.format_matrix(reg, rows)
    assert "instagram: Instagram [established]" in matrix and "route agent_reach: session_based, site_data, login dummy_account OFF EXPERIMENTAL -> disabled" in matrix
    assert "gives:  posts; captions" in matrix and "cannot: " in matrix and "reddit_public: Reddit and community discussions [planned]" in matrix
