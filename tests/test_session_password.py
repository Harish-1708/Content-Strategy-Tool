"""The password-login backup: cookies first, the backup only when the cookie session cannot be proved, hard limits, and nothing secret kept."""
import json
from pathlib import Path

import pytest

from contentintel import session_password as SP

NOW = "2026-11-19T12:00:00Z"
CREDS = {"username": "dummy_user", "password": "S3cret-pw!"}


def entry(**kw):
    return {"state": "UNCONFIRMED", "unconfirmed_streak": 3, "cannot_check_streak": 0, "proved_on": "", **kw}


def test_the_cookie_session_comes_first_so_the_backup_waits_for_three_checks_with_no_sign_or_a_rejection():
    ok, why = SP.eligible(entry(unconfirmed_streak=2), [], "", NOW, has_credentials=True)
    assert not ok and "2 of 3 checks in a row" in why
    assert SP.eligible(entry(), [], "", NOW, has_credentials=True) == (True, "")                                                      # three unconfirmed checks in a row (TikTok)
    assert SP.eligible(entry(state="CANNOT CHECK", unconfirmed_streak=0, cannot_check_streak=3), [], "", NOW, has_credentials=True)[0]  # three checks that could not be made (Instagram)
    assert SP.eligible(entry(state="REJECTED", unconfirmed_streak=0), [], "", NOW, has_credentials=True)[0] and SP.eligible(entry(state="EXPIRED", unconfirmed_streak=0), [], "", NOW, has_credentials=True)[0]
    ok, why = SP.eligible(entry(state="ACCEPTED", proved_on="2026-11-18", unconfirmed_streak=0), [], "", NOW, has_credentials=True)
    assert not ok and "cookie session is always first" in why
    assert SP.eligible(entry(), [], "", NOW, has_credentials=False)[1].startswith("no username and password")
    assert SP.eligible(None, [], "", NOW, has_credentials=True)[0] is False                                                            # a platform with no record yet is never logged into


def test_the_hard_limits_one_attempt_a_week_two_a_month_and_the_rests_after_a_challenge_or_a_warning():
    last_week = {"at": "2026-11-15T10:00:00Z", "outcome": "x"}
    ok, why = SP.eligible(entry(), [last_week], "", NOW, has_credentials=True)
    assert not ok and "at most one attempt a week" in why
    assert SP.eligible(entry(), [{"at": "2026-11-10T10:00:00Z", "outcome": "x"}], "", NOW, has_credentials=True)[0]
    two = [{"at": "2026-11-01T10:00:00Z", "outcome": "x"}, {"at": "2026-11-09T10:00:00Z", "outcome": "x"}]
    ok, why = SP.eligible(entry(), two, "", NOW, has_credentials=True)
    assert not ok and "2 times this month" in why
    ok, why = SP.eligible(entry(), [], "2026-11-25T00:00:00Z", NOW, has_credentials=True)
    assert not ok and "resting until 2026-11-25" in why and SP.eligible(entry(), [], "2026-11-19T11:00:00Z", NOW, has_credentials=True)[0]


class Challenge(Exception):
    pass


class AccountWarning(Exception):
    pass


def run(tmp_path, login, prove, ent=None, creds=CREDS, now=NOW):
    ledger = SP.AttemptLedger(tmp_path / "session_password.json")
    return SP.attempt("tiktok", ent or entry(), creds, ledger, now, login_fn=login, prove_fn=prove), ledger


def test_one_attempt_logs_in_once_captures_cookies_and_proves_the_session_and_keeps_no_secret(tmp_path):
    calls = []

    def login(platform, username, password):
        calls.append((platform, username, password))
        return '[{"name": "sessionid", "value": "FRESH-COOKIE-VALUE"}]'
    seen = []

    def prove(platform, cookies):
        seen.append(cookies)
        return {"state": "ACCEPTED", "why": "logged in, markers found"}
    res, ledger = run(tmp_path, login, prove)
    assert res == {"status": "proved", "detail": "logged in, markers found", "state": "ACCEPTED"} and calls == [("tiktok", "dummy_user", "S3cret-pw!")] and "FRESH-COOKIE-VALUE" in seen[0]
    text = (tmp_path / "session_password.json").read_text(encoding="utf-8")
    assert "S3cret" not in text and "FRESH-COOKIE" not in text and "dummy_user" not in text and json.loads(text)["tiktok"]["attempts"][0]["outcome"] == "proved"
    again = SP.attempt("tiktok", entry(), CREDS, ledger, "2026-11-20T12:00:00Z", login_fn=login, prove_fn=prove)
    assert again["status"] == "skipped" and len(calls) == 1                                                                           # never twice in a week


def test_a_session_the_platform_still_does_not_accept_is_reported_not_proved_and_a_missing_cookie_set_is_a_stop(tmp_path):
    res, _l = run(tmp_path / "a", lambda *a: "cookies", lambda p, c: {"state": "UNCONFIRMED", "why": "no sign"})
    assert res["status"] == "not_proved" and res["state"] == "UNCONFIRMED"
    res2, _l2 = run(tmp_path / "b", lambda *a: "", lambda p, c: {})
    assert res2["status"] == "stopped" and "no cookies were captured" in res2["detail"]


def test_a_security_check_or_a_warning_ends_the_attempt_and_rests_the_backup_and_the_message_is_never_kept(tmp_path):
    def boom(kind):
        def login(platform, username, password):
            raise kind("the page said S3cret-pw! for dummy_user")
        return login
    res, ledger = run(tmp_path / "c", boom(Challenge), lambda *a: {})
    assert res["status"] == "stopped" and "Challenge" in res["detail"] and "14 days" in res["detail"] and "S3cret" not in json.dumps(res)
    assert ledger.rest_until("tiktok") == "2026-12-03T12:00:00Z"
    res2, ledger2 = run(tmp_path / "d", boom(AccountWarning), lambda *a: {})
    assert "30 days" in res2["detail"] and ledger2.rest_until("tiktok") == "2026-12-19T12:00:00Z"
    res3, ledger3 = run(tmp_path / "e", boom(ValueError), lambda *a: {})
    assert res3["status"] == "stopped" and ledger3.rest_until("tiktok") == ""
    skipped, _l = run(tmp_path / "f", boom(Challenge), lambda *a: {}, creds={"username": "", "password": ""})
    assert skipped["status"] == "skipped"                                                                                               # nothing to log in with: nothing is tried


def test_the_session_ledger_counts_cannot_check_streaks_and_the_command_and_workflow_are_owner_triggered_only(tmp_path):
    from contentintel import session_live as SL
    led = SL.SessionLedger(tmp_path / "s.json")
    for n in range(3):
        led.record("instagram", "CANNOT CHECK", "too many redirects", f"2026-11-1{n}T10:00:00Z")
    assert led.data["instagram"]["cannot_check_streak"] == 3 and led.data["instagram"]["unconfirmed_streak"] == 0
    led.record("instagram", "ACCEPTED", "ok", "2026-11-14T10:00:00Z")
    assert led.data["instagram"]["cannot_check_streak"] == 0
    wf = Path(".github/workflows/sessions-password-backup.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in wf and "\n  schedule:" not in wf and "--what password-backup" in wf and "never the default, never scheduled" in wf
    text = Path("contentintel/session_password.py").read_text(encoding="utf-8")
    assert "password_fallback: never" in text and "narrow exception" in text and "sessions-PROOF" in text
    import yaml
    assert yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["sites"]["instagram"].get("password_fallback", "never") in ("never", "no_cookies_only", "when_cookies_fail")
    with pytest.raises(SystemExit):
        from contentintel import cli
        cli.build_parser().parse_args(["observatory", "sessions", "--what", "password-everything"])
