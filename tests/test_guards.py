"""Guards: every protection is an owner switch and none acts unless the owner turns it ON."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contentintel import agent_reach_instagram as AR
from contentintel import collectors as COL
from contentintel import guards
from contentintel import health as HLTH
from contentintel import reddit_routes as RR
from contentintel import safety
from contentintel import services as SV
from contentintel.observatory import worker_client as WC
from contentintel.observatory.pauses import PauseLedger

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-11-09T10:00:00Z"


def _fresh(env_value):
    """Settings and controls as a clean process sees them: the environment hook set or not."""
    env = {k: v for k, v in os.environ.items() if k != "CONTENTINTEL_LEGACY_GUARDS"}
    if env_value:
        env["CONTENTINTEL_LEGACY_GUARDS"] = "1"
    code = ("import json; from contentintel import services as SV, guards; s = SV.load_settings('observatory.yaml', 'no-such-dir'); "
            "print(json.dumps({'channels': s['services']['guards']['channels'], 'controls': {c['key']: c['default'] for c in SV.CONTROLS if c['key'].startswith('services.guards.channels.')}, "
            "'on': {g: guards.on(g) for g in guards.GUARDS}, 'retry': s['retry'], 'plain': (guards.configure({'robots_txt': True}), {g: guards.on(g) for g in guards.GUARDS})[1]}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout)


def test_as_shipped_every_guard_is_off_so_nothing_blocks_and_the_controls_say_so():
    got = _fresh(False)
    assert set(got["channels"]) == set(guards.GUARDS) and not any(got["channels"].values()) and not any(got["on"].values())
    assert set(got["controls"]) == {f"services.guards.channels.{g}" for g in guards.GUARDS} and not any(got["controls"].values())
    assert got["retry"] == {"attempts": 3, "backoff_seconds": 2}
    assert got["plain"] == {g: g == "robots_txt" for g in guards.GUARDS}                      # a plain dict that names one guard leaves every other guard at its default: OFF


def test_the_legacy_setting_starts_every_guard_on_as_updates_1_to_40_behaved():
    got = _fresh(True)
    assert all(got["channels"].values()) and all(got["on"].values()) and all(got["controls"].values()) and all(got["plain"].values())


def test_every_guard_can_be_turned_on_and_off_by_the_owner_and_nothing_else_changes():
    s = SV.load_settings("observatory.yaml", "no-such-dir")
    for g in guards.GUARDS:
        s["services"]["guards"]["channels"] = {x: False for x in guards.GUARDS}
        s["services"]["guards"]["channels"][g] = True
        guards.configure(s)
        assert [x for x in guards.GUARDS if guards.on(x)] == [g]
    s["services"]["guards"]["enabled"] = False                                   # the master switch turns every guard off
    s["services"]["guards"]["channels"] = {x: True for x in guards.GUARDS}
    guards.configure(s)
    assert not any(guards.on(x) for x in guards.GUARDS) and guards.on("nonsense") is False
    guards.configure({"robots_txt": True})                                       # a plain dict (what the worker process gets)
    assert guards.on("robots_txt") and guards.on("rest_after_refusal") == guards.legacy()


def test_owner_changes_saved_through_the_controls_reach_the_guards(tmp_path):
    SV.save_overrides(str(tmp_path), {"services.guards.channels.robots_txt": True}, "owner", NOW, "on")
    s = SV.load_settings(None, str(tmp_path))
    assert s["services"]["guards"]["channels"]["robots_txt"] is True
    SV.save_overrides(str(tmp_path), {}, "owner", NOW, "reset")


def _set(**on):
    guards.configure({g: on.get(g, False) for g in guards.GUARDS})


def test_the_rest_guard_off_means_no_site_or_account_or_route_is_ever_rested():
    ledger = PauseLedger("no-such-dir")
    _set()
    ledger.pause("shop.example", 3600, "slow down", NOW)
    assert ledger.paused("shop.example", NOW) is None and "shop.example" not in ledger.data
    _set(rest_after_refusal=True)
    ledger.pause("shop.example", 3600, "slow down", NOW)
    assert ledger.paused("shop.example", NOW)[0] == "slow down"
    _set()
    assert ledger.paused("shop.example", NOW) is None                              # even a rest already written down is not held against the site
    assert COL.active_cooldowns({"instagram": {"until": "2099-01-01T00:00:00Z"}}, NOW) == {}
    _set(rest_after_refusal=True)
    assert "instagram" in COL.active_cooldowns({"instagram": {"until": "2099-01-01T00:00:00Z"}}, NOW)


def test_a_route_is_not_paused_after_failures_while_the_rest_guard_is_off(tmp_path):
    from contentintel import contract as K
    from contentintel import sources as SRC
    from contentintel.store import Store
    reg = SRC.load_sources(ROOT / "sources.yaml")
    for state, expect_paused in ((False, False), (True, True)):
        _set(rest_after_refusal=state)
        led = HLTH.HealthLedger.load(Store(str(tmp_path / str(state))), reg)
        led.observe("tiktok", "browser", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
        assert bool(led.is_paused("tiktok", "browser", "2026-11-09T10:01:00Z")) is expect_paused
        assert bool(led.state["routes"]["tiktok|browser"]["pause"]) is expect_paused                          # with the guard off the pause is never even written down
        if state:
            _set()                                                                                          # a pause written while the guard was ON stops blocking the moment the owner turns it OFF
            assert led.is_paused("tiktok", "browser", "2026-11-09T10:01:00Z") is None


def test_the_hosted_runner_the_reddit_approval_and_the_address_checks_follow_their_switches():
    hosted = {"RUNNER_ENVIRONMENT": "github-hosted"}
    _set()
    assert AR.hosted_runner(hosted) is False and RR.approval_problem({}) == "" and safety.normalize_fetch_url("http://127.0.0.1:8080/x") == "http://127.0.0.1:8080/x"
    _set(hosted_runner_block=True, reddit_approval_required=True)
    assert AR.hosted_runner(hosted) is True and "recorded approval" in RR.approval_problem({})
    for bad in ("ftp://shop.example/x", "http://a b.example/", "", "http://"):
        with pytest.raises(safety.UnsafeURL):
            safety.normalize_fetch_url(bad)                                         # still an address, even with the guard off
    with pytest.raises(safety.UnsafeURL):
        safety.normalize_public_url("http://127.0.0.1:8080/x")


def test_the_worker_job_carries_the_owners_guards_and_retry_settings():
    s = SV.load_settings("observatory.yaml", "no-such-dir")
    _set(robots_txt=True)
    job = WC.build_job([], s, "a@b.example")
    assert job["guards"]["robots_txt"] is True and job["guards"]["rest_after_refusal"] is False and job["retry"] == {"attempts": 3, "backoff_seconds": 2}
