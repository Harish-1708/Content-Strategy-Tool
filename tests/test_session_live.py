"""Stored login sessions in use (Update 50): each of the eight platforms is checked on its own through the Scrapling worker, the result is kept without any secret value, and the fallback chain only ever uses a
session on its own platform's hosts, after a live check passed, and only while the owner has it on."""
import base64
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import contract as K
from contentintel import services as SV
from contentintel import session_live as SL
from contentintel import sessions as S
from conftest import ROOT

NOW = "2026-11-11T10:00:00Z"
PLATFORMS = list(S.PLATFORMS)
VALUE = "SECRETVALUE0123456789ABCDEF"


def epoch(date):
    return datetime.fromisoformat(date + "T00:00:00+00:00").timestamp()


def good(platform, expires="2027-01-20"):
    spec = S.PLATFORMS[platform]
    names = list(spec["required"]) or [spec["any_of"][0]]
    cookies = [{"name": n, "value": VALUE + n, "domain": "." + spec["hosts"][0], "path": "/", "httpOnly": True, "secure": True, "expires": epoch(expires)} for n in names]
    return {"cookies": cookies, "origins": []}


def secret(platform, obj=None):
    return base64.b64encode(json.dumps(obj if obj is not None else good(platform)).encode()).decode()


def environ(*platforms, **more):
    return {**{S.PLATFORMS[p]["secret"]: secret(p) for p in platforms}, **more}


def logged_in_page(platform):
    spec = S.PLATFORMS[platform]
    return {"ok": True, "status": 200, "final_url": spec["check_url"], "title": spec["label"], "text": "your home " * 40, "html": f"<html>{spec['logged_in'][0]} " + "page content " * 40 + "</html>"}


def login_wall(platform):
    spec = S.PLATFORMS[platform]
    return {"ok": False, "status": 200, "final_url": spec["login_url"], "html": f"<html>{spec['logged_out'][0]} " + "x " * 150 + "</html>", "text": "",
            "error": {"code": "UPSTREAM_BROKEN", "message": f"{spec['check_url']} shows only a login page"}}


class Worker:
    """A stand-in for the Scrapling worker process: it reads the job the core wrote and writes the result file, and records every job and the environment it was given."""

    def __init__(self, answer):
        self.answer, self.jobs, self.envs = answer, [], []

    def __call__(self, argv, env=None, **kw):
        job = json.loads(Path(argv[-2]).read_text(encoding="utf-8"))
        self.jobs.append(job)
        self.envs.append(dict(env or {}))
        Path(argv[-1]).write_text(json.dumps({"ok": True, "results": [self.answer(item) for item in job["items"]]}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


def platform_of(item):
    return next(p for p, spec in S.PLATFORMS.items() if any(h in item["url"] for h in spec["hosts"]))


@pytest.fixture()
def settings(tmp_path):
    return SV.load_settings(None, str(tmp_path / "data"))


@pytest.fixture()
def ledger(tmp_path):
    return SL.SessionLedger(tmp_path / "sessions_health.json")


def check(platform, settings, ledger, worker, env=None, **kw):
    return SL.check_platform(platform, settings, "owner@example.com", sys.executable, environ=env if env is not None else environ(platform), runner=worker, now=kw.pop("now", NOW), ledger=ledger, **kw)


# ---- every platform on its own ---------------------------------------------------------------------------------------------------------------------

def test_each_of_the_eight_platforms_gets_its_own_result_and_one_platforms_trouble_never_spills_onto_another(settings, ledger):
    env = environ(*[p for p in PLATFORMS if p != "boss"], ANTHROPIC_API_KEY="sk-ant-NOT-FOR-THE-WORKER", GOOGLE_SERVICE_ACCOUNT_JSON="{}")
    env[S.PLATFORMS["linkedin"]["secret"]] = secret("linkedin", good("linkedin", expires="2026-10-01"))                # expired
    env[S.PLATFORMS["reddit"]["secret"]] = secret("reddit", {"cookies": [], "origins": []})                                # not a session
    answers = {"instagram": login_wall("instagram"), "tiktok": {"ok": False, "status": 403, "final_url": "https://www.tiktok.com/", "html": "", "error": {"code": "UPSTREAM_BROKEN", "message": "tiktok.com answered 403"}},
               "x": logged_in_page("x"), "facebook": logged_in_page("facebook"), "xiaohongshu": logged_in_page("xiaohongshu")}
    worker = Worker(lambda item: answers[platform_of(item)])
    rows = SL.check_all(settings, "owner@example.com", sys.executable, environ=env, runner=worker, now=NOW, ledger=ledger)
    assert [r["platform"] for r in rows] == PLATFORMS and len(rows) == 8
    assert {r["platform"]: r["state"] for r in rows} == {"instagram": SL.REJECTED, "x": SL.ACCEPTED, "tiktok": SL.REJECTED, "reddit": SL.BAD_CAPTURE, "facebook": SL.ACCEPTED,
                                                         "linkedin": SL.EXPIRED, "xiaohongshu": SL.ACCEPTED, "boss": SL.NOT_SET_UP}
    asked = sorted(platform_of(j["items"][0]) for j in worker.jobs)
    assert asked == ["facebook", "instagram", "tiktok", "x", "xiaohongshu"]                                               # an expired, unusable or missing session makes no request at all
    for job, got in zip(worker.jobs, worker.envs):
        platform = platform_of(job["items"][0])
        name = f"SESSION_COOKIES_{platform.upper()}"
        assert job["items"][0]["advanced"] == {"stealth": True, "cookies_env": name} and job["return_html"] is True
        assert json.loads(got[name]) == {n: VALUE + n for n in (list(S.PLATFORMS[platform]["required"]) or [S.PLATFORMS[platform]["any_of"][0]])}
        assert not [k for k in got if k.startswith("SESSION_COOKIES_") and k != name] and "ANTHROPIC_API_KEY" not in got and "GOOGLE_SERVICE_ACCOUNT_JSON" not in got     # one named secret, nothing else of ours
        assert not [k for k in got if k.endswith("_SESSION_B64")]
    assert SL.exit_code(rows) == 1                                                                                          # a refused, expired or unusable session needs a new capture: the run says so


def test_a_check_that_passes_is_live_proof_for_fourteen_days_and_a_later_refusal_voids_it(settings, ledger):
    good_worker = Worker(lambda item: logged_in_page("x"))
    row = check("x", settings, ledger, good_worker)
    assert row["state"] == SL.ACCEPTED and row["proved_on"] == NOW and ledger.proved("x", NOW) and ledger.proved("x", "2026-11-25T10:00:00Z") and not ledger.proved("x", "2026-11-25T10:00:01Z")
    assert not ledger.proved("x", "2026-11-10T10:00:00Z") and not ledger.proved("instagram", NOW)                         # not before it happened, not for another platform
    bad = check("x", settings, ledger, Worker(lambda item: login_wall("x")), now="2026-11-12T10:00:00Z")
    assert bad["state"] == SL.REJECTED and bad["proved_on"] == "" and not ledger.proved("x", "2026-11-12T10:00:00Z")      # a refusal voids the proof
    assert check("x", settings, ledger, good_worker, now="2026-11-13T10:00:00Z")["proved_on"] == "2026-11-13T10:00:00Z"
    kept = check("x", settings, ledger, Worker(lambda item: {"ok": False, "status": 503, "html": "", "error": {"code": "UPSTREAM_BROKEN", "message": "x.com answered 503"}}), now="2026-11-14T10:00:00Z")
    assert kept["state"] == SL.CANNOT_CHECK and kept["proved_on"] == "2026-11-13T10:00:00Z"                               # a check that could not be made learns nothing: the proof stays
    off = check("x", {**settings, "services": {**settings["services"], "stored_sessions": {**settings["services"]["stored_sessions"], "enabled": False}}}, ledger, good_worker, now="2026-11-15T10:00:00Z")
    assert off["state"] == SL.OFF and off["proved_on"] == "2026-11-13T10:00:00Z"
    gone = check("x", settings, ledger, good_worker, env={}, now="2026-11-16T10:00:00Z")
    assert gone["state"] == SL.NOT_SET_UP and gone["proved_on"] == ""                                                       # the secret was removed: nothing is proved any more


def test_the_ledger_holds_no_secret_value_and_a_damaged_file_is_ignored(settings, ledger, tmp_path):
    check("instagram", settings, ledger, Worker(lambda item: login_wall("instagram")))
    ledger.save()
    text = ledger.path.read_text(encoding="utf-8")
    assert VALUE not in text and secret("instagram") not in text and json.loads(text)["instagram"]["state"] == SL.REJECTED and ledger.path.parent.is_dir()
    assert SL.SessionLedger(ledger.path).data["instagram"]["state"] == SL.REJECTED
    for junk in ("not json", "[1, 2]", json.dumps({"instagram": "nope", "unknown": {"state": "x"}, "x": {"state": SL.ACCEPTED, "proved_on": NOW}})):
        ledger.path.write_text(junk, encoding="utf-8")
        assert list(SL.SessionLedger(ledger.path).data) in ([], ["x"])


def test_what_the_page_looked_like_decides_between_accepted_rejected_and_unconfirmed(settings, ledger):
    why = "BrowserType.launch: Executable doesn't exist at /home/runner/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell"
    plain = dict(logged_in_page("x"), browser_note="no browser is installed here, so the page was read over plain HTTP", browser_error=why)
    assert check("x", settings, ledger, Worker(lambda item: plain))["state"] == SL.ACCEPTED
    assert f"read over plain HTTP because the browser could not be used here ({why})" in ledger.data["x"]["reason"]
    shell = dict(login_wall("x"), browser_note="no browser is installed here, so the page was read over plain HTTP", browser_error=why)
    row = check("x", settings, ledger, Worker(lambda item: shell))
    assert row["state"] == SL.UNCONFIRMED and f"because the browser could not be used here ({why})" in row["reason"] and SL.exit_code([row]) == 0      # plain HTTP cannot tell: never called a refusal
    assert "browser box" not in row["reason"] and "tick" not in row["reason"]                                                     # it never points at an input that does not exist
    no_reason = dict(login_wall("x"), browser_note="no browser is installed here, so the page was read over plain HTTP")
    assert "the browser could not be used here (no reason was given)" in check("x", settings, ledger, Worker(lambda item: no_reason))["reason"]
    wrong = dict(logged_in_page("x"), final_url="https://evil.example/home")
    assert check("x", settings, ledger, Worker(lambda item: wrong))["state"] == SL.REJECTED
    challenge = dict(logged_in_page("instagram"), final_url="https://www.instagram.com/challenge/")
    assert check("instagram", settings, ledger, Worker(lambda item: challenge))["state"] == SL.REJECTED
    nothing = {"ok": False, "status": 0, "html": "", "error": {"code": "NEED_SETUP", "message": "the browser could not start"}}
    row = check("x", settings, ledger, Worker(lambda item: nothing))
    assert row["state"] == SL.CANNOT_CHECK and "the browser could not start" in row["reason"]
    forbidden = {"ok": False, "status": 403, "html": "", "error": {"code": "UPSTREAM_BROKEN", "message": "x.com answered 403"}}
    row = check("x", settings, ledger, Worker(lambda item: forbidden))
    assert row["state"] == SL.REJECTED and "blocked this runner" in row["reason"]


def test_a_worker_that_is_missing_or_broken_is_reported_as_cannot_check_and_never_raises(settings, ledger):
    row = SL.check_platform("x", settings, "owner@example.com", "", environ=environ("x"), now=NOW, ledger=ledger)
    assert row["state"] == SL.CANNOT_CHECK and "Scrapling environment is not set up" in row["reason"]
    class Broken(Worker):
        def __call__(self, argv, env=None, **kw):
            return subprocess.CompletedProcess(argv, 3, "", "boom with " + VALUE)
    row = check("x", settings, ledger, Broken(lambda item: {}))
    assert row["state"] == SL.CANNOT_CHECK and "the Scrapling worker failed" in row["reason"]
    empty = Worker(lambda item: None)
    empty.answer = lambda item: {}
    assert check("x", settings, ledger, Worker(lambda item: {}))["state"] == SL.CANNOT_CHECK


def test_the_owner_switches_stop_a_session_from_being_loaded_at_all(settings, ledger):
    worker = Worker(lambda item: logged_in_page("x"))
    sec = settings["services"]["stored_sessions"]
    assert sec["enabled"] is True and sec["channels"] == {p: True for p in PLATFORMS}                                       # every platform is ON until the owner turns one off
    one = {**settings, "services": {**settings["services"], "stored_sessions": {**sec, "channels": {**sec["channels"], "x": False}}}}
    row = check("x", one, ledger, worker, env=environ("x") | {"X_SESSION_B64": "garbage"})
    assert row["state"] == SL.OFF and "services.stored_sessions.channels.x" in row["reason"] and worker.jobs == []          # not even decoded: switched off means never loaded
    assert check("instagram", one, ledger, Worker(lambda item: logged_in_page("instagram")))["state"] == SL.ACCEPTED         # the others are unaffected
    assert "x" not in ledger.data                                                                                           # an OFF platform is not recorded as a result


def test_the_offline_status_makes_no_request_and_says_whether_the_session_was_ever_proved(settings, ledger):
    worker = Worker(lambda item: logged_in_page("x"))
    first = check("x", settings, ledger, worker, offline=True)
    assert first["state"] == "PRESENT" and "not yet proved against the live site" in first["reason"] and first["expires_on"] == "2027-01-20" and worker.jobs == []
    check("x", settings, ledger, worker)
    again = check("x", settings, ledger, worker, offline=True)
    assert "proved live on 2026-11-11" in again["reason"] and len(worker.jobs) == 1
    assert check("tiktok", settings, ledger, worker, env={}, offline=True)["state"] == SL.NOT_SET_UP
    assert check("x", settings, ledger, worker, env=environ("x") | {"X_SESSION_B64": secret("x", good("x", expires="2026-10-01"))}, offline=True)["state"] == SL.EXPIRED


def test_only_a_session_that_needs_a_new_capture_turns_the_run_red():
    rows = lambda *states: [{"state": s} for s in states]
    assert SL.exit_code(rows(SL.ACCEPTED, SL.NOT_SET_UP, SL.OFF, SL.CANNOT_CHECK, SL.UNCONFIRMED, "PRESENT")) == 0
    for state in (SL.REJECTED, SL.EXPIRED, SL.BAD_CAPTURE):
        assert SL.exit_code(rows(SL.ACCEPTED, state)) == 1
    assert SL.exit_code([]) == 0


def test_the_report_has_one_line_per_platform_a_count_line_and_says_what_accepted_does_not_mean(settings, ledger):
    worker = Worker(lambda item: logged_in_page("x") if platform_of(item) == "x" else login_wall("instagram"))
    rows = SL.check_all(settings, "owner@example.com", sys.executable, environ=environ("x", "instagram"), runner=worker, now=NOW, ledger=ledger)
    text = SL.format_report(rows)
    lines = text.splitlines()
    assert lines[0].startswith("Stored login sessions: each platform is judged on its own, live") and len([ln for ln in lines if not ln.startswith("      ")]) == 8 + 3          # the evidence lines are indented further
    by = {r["label"]: next(ln for ln in lines if ln.strip().startswith(r["label"])) for r in rows}
    assert "ACCEPTED" in by["X (Twitter)"] and "expires 2027-01-20" in by["X (Twitter)"] and "last proved 2026-11-11" in by["X (Twitter)"]
    assert "REJECTED" in by["Instagram"] and "NOT SET UP" in by["Boss (Zhipin)"] and "INSTAGRAM_SESSION_B64" not in by["Instagram"] and "BOSS_SESSION_B64" in by["Boss (Zhipin)"]
    assert lines[-2].strip() == "1 ACCEPTED, 1 REJECTED, 6 NOT SET UP" and "does not mean every kind of read works" in lines[-1]
    assert VALUE not in text and secret("x") not in text
    offline = SL.format_report(SL.check_all(settings, "", "", environ=environ("x"), now=NOW, ledger=ledger, offline=True), offline=True)
    assert offline.splitlines()[0].startswith("Stored login sessions: no request was made") and "1 PRESENT, 7 NOT SET UP" in offline and "does not mean" not in offline


# ---- one logged-in page through the fallback chain -------------------------------------------------------------------------------------------------

URL = "https://x.com/someaccount"


def page_for(with_cookies, without):
    def answer(item):
        return with_cookies if item["advanced"].get("cookies_env") else without
    return answer


PAGE = {"ok": True, "status": 200, "url": URL, "final_url": URL, "title": "Some account", "text": "posts and replies " * 30}


def read(settings, ledger, worker, url=URL, platform="x", env=None, **kw):
    return SL.read_page(platform, url, settings, "owner@example.com", sys.executable, environ=env if env is not None else environ(platform), runner=worker, now=NOW, ledger=ledger, **kw)


def test_the_plain_reader_is_tried_first_and_the_session_is_never_touched_when_it_works(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    worker = Worker(page_for(PAGE, PAGE))
    got = read(settings, ledger, worker)
    assert got.route == "primary" and got.rows[0]["route"] == "primary" and len(worker.jobs) == 1 and "cookies_env" not in worker.jobs[0]["items"][0]["advanced"]
    assert not [k for k in worker.envs[0] if k.startswith("SESSION_COOKIES_")]


WALL = {"ok": False, "status": 200, "url": URL, "final_url": "https://x.com/i/flow/login", "html": "log in", "error": {"code": "UPSTREAM_BROKEN", "message": f"{URL} shows only a login page"}}


def test_a_login_wall_sends_the_read_to_the_session_only_after_a_live_check_passed(settings, ledger):
    worker = Worker(page_for(PAGE, WALL))
    unproved = read(settings, ledger, worker)
    assert unproved.skipped and unproved.route == "" and [t[:2] for t in unproved.trail] == [("primary", K.NEED_LOGIN), ("session", "off"), ("discovery", K.NEED_SETUP)]
    assert "run the sessions check first" in unproved.reason and len(worker.jobs) == 1                                        # never tried with the session
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    worker = Worker(page_for(PAGE, WALL))
    got = read(settings, ledger, worker)
    assert got.route == "session" and got.rows[0]["route"] == "session" and got.rows[0]["title"] == "Some account" and len(worker.jobs) == 2
    assert "cookies_env" not in worker.jobs[0]["items"][0]["advanced"] and worker.jobs[1]["items"][0]["advanced"]["cookies_env"] == "SESSION_COOKIES_X"
    assert [k for k in worker.envs[1] if k.startswith("SESSION_COOKIES_")] == ["SESSION_COOKIES_X"]
    assert got.trail[0][1] == K.NEED_LOGIN and got.trail[1][1] == "ok"


def test_a_proof_that_is_old_or_voided_or_a_switch_that_is_off_keeps_the_session_out_of_the_chain(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    old = SL.read_page("x", URL, settings, "o@example.com", sys.executable, environ=environ("x"), runner=Worker(page_for(PAGE, WALL)), now="2026-12-01T10:00:00Z", ledger=ledger)
    assert old.skipped and old.trail[1][1] == "off" and "run the sessions check first" in old.reason
    sec = settings["services"]["stored_sessions"]
    off = {**settings, "services": {**settings["services"], "stored_sessions": {**sec, "channels": {**sec["channels"], "x": False}}}}
    worker = Worker(page_for(PAGE, WALL))
    got = read(off, ledger, worker)
    assert got.skipped and "services.stored_sessions.channels.x" in got.reason and len(worker.jobs) == 1
    check("x", settings, ledger, Worker(lambda item: login_wall("x")))                                                      # the platform now refuses the session
    got = read(settings, ledger, Worker(page_for(PAGE, WALL)))
    assert got.skipped and got.trail[1][1] == "off"


def test_a_stored_session_is_never_sent_to_another_site(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    worker = Worker(page_for(PAGE, PAGE))
    for url in ("https://evil.example/x.com", "https://x.com.evil.example/home", "https://notx.com/", "http://127.0.0.1/x.com", "https://example.com/?u=https://x.com/"):
        with pytest.raises(K.Failure) as seen:
            read(settings, ledger, worker, url=url)
        assert seen.value.code == K.BAD_INPUT and "never sent to another site" in str(seen.value)
    assert worker.jobs == []                                                                                                 # nothing was requested, nothing was loaded
    assert read(settings, ledger, worker, url="https://twitter.com/someaccount").route == "primary"                           # the platform's other host is its own
    with pytest.raises(K.Failure) as seen:
        read(settings, ledger, worker, platform="myspace", env={})
    assert seen.value.code == K.BAD_INPUT and "no stored-session platform called 'myspace'" in str(seen.value)


def test_a_failing_primary_and_a_failing_session_end_in_an_honest_skip_with_both_reasons_and_one_attempt_each(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    gone = {"ok": False, "status": 404, "url": URL, "error": {"code": "NOT_FOUND", "message": f"{URL} does not exist (404)"}}
    worker = Worker(page_for(WALL, gone))
    got = read(settings, ledger, worker)
    assert got.skipped and len(worker.jobs) == 2 and [t[1] for t in got.trail] == [K.NOT_FOUND, K.NEED_LOGIN, K.NEED_SETUP]
    assert "primary: NOT_FOUND" in got.reason and "session: NEED_LOGIN" in got.reason and "discovery: NEED_SETUP" in got.reason and VALUE not in got.reason
    empty = {"ok": True, "status": 200, "url": URL, "final_url": URL, "title": "", "text": ""}
    got = read(settings, ledger, Worker(page_for(empty, empty)))
    assert not got.skipped and got.route == "" and "found nothing" in got.reason                                              # an empty page is an answer, not a failure


# ---- the command, the controls and the workflows ---------------------------------------------------------------------------------------------------

def run_cli(tmp_path, monkeypatch, capsys, *argv, **env):
    for key in [k for k in list(__import__("os").environ) if k.endswith("_SESSION_B64") or k == "SCRAPLING_PYTHON"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OBSERVATORY_CONTACT", "owner@example.com")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = cli.main(["observatory", "sessions", "--all", "--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr().out


def test_the_sessions_command_lists_checks_and_reads_each_platform_and_explains_its_errors(tmp_path, monkeypatch, capsys):
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "list")
    assert code == 0 and out.count("NOT SET UP") == 8 + 1 and "8 NOT SET UP" in out and "no request was made" in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "list", **{"X_SESSION_B64": secret("x")})
    assert code == 0 and "1 PRESENT, 7 NOT SET UP" in out and "VALUE" not in out and VALUE not in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "check", **{"X_SESSION_B64": secret("x")})
    assert code == 0 and "the Scrapling environment is not installed here" in out and "1 CANNOT CHECK" in out                    # no worker here: honest, and not a red run
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "check", "--key", "x", **{"X_SESSION_B64": secret("x", good("x", expires="2026-10-01"))})
    assert code == 1 and "1 EXPIRED" in out and "expired on 2026-10-01" in out and out.count("Instagram") == 0                      # only the one asked for; an expired one needs a new capture
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "check", "--key", "myspace")
    assert code == 1 and "no platform called 'myspace'" in out and "instagram, x, tiktok, reddit, facebook, linkedin, xiaohongshu, boss" in out
    for argv in (["--what", "read"], ["--what", "read", "--key", "x"], ["--what", "read", "--url", URL]):
        code, out = run_cli(tmp_path, monkeypatch, capsys, *argv)
        assert code == 1 and "read needs one platform (--key) and the address (--url)" in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "read", "--key", "x", "--url", "https://evil.example/")
    assert code == 1 and "never sent to another site" in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "read", "--key", "x", "--url", URL)
    assert code == 0 and "primary: NEED_SETUP" in out and "not read: " in out                                                      # no worker here: skipped, with the reason for each route


def test_the_eight_platform_switches_and_a_master_switch_are_owner_controls_that_default_to_on():
    keys = [c["key"] for c in SV.CONTROLS if "stored_sessions" in c["key"]]
    assert keys == ["services.stored_sessions.enabled"] + [f"services.stored_sessions.channels.{p}" for p in PLATFORMS]
    assert all(SV.BY_KEY[k]["default"] is True and SV.BY_KEY[k]["kind"] == "bool" for k in keys)
    assert "never loaded" in SV.BY_KEY["services.stored_sessions.channels.x"]["text"] and "master switch" in SV.BY_KEY["services.stored_sessions.enabled"]["text"]


def test_the_sessions_workflow_and_acceptance_hand_over_the_eight_optional_secrets_and_the_observatory_workflow_gets_none_of_them():
    names = [S.PLATFORMS[p]["secret"] for p in PLATFORMS]
    wf = ROOT / ".github" / "workflows"
    obs_text = (wf / "observatory.yml").read_text(encoding="utf-8")
    assert not [n for n in names if n in obs_text]                                                                           # the stored sessions live in their own workflow and in Acceptance, nowhere else
    sess = yaml.safe_load((wf / "sessions.yml").read_text(encoding="utf-8"))
    dispatch = (sess.get(True) or sess.get("on"))["workflow_dispatch"]["inputs"]
    assert dispatch["what"]["options"] == ["check", "list", "read"] and dispatch["platform"]["options"] == ["all", *sorted(PLATFORMS, key=lambda p: ["all", "x", "instagram", "tiktok", "reddit", "facebook", "linkedin", "xiaohongshu", "boss"].index(p))]
    steps = sess["jobs"]["sessions"]["steps"]
    run = next(s for s in steps if s.get("name") == "Stored login sessions")
    assert run["continue-on-error"] is True and all(run["env"][n] == "${{ secrets." + n + " }}" for n in names) and run["env"]["READ_URL"] == "${{ inputs.url }}"
    assert 'args=(observatory sessions --all --what "$WHAT")' in run["run"] and '--key "$PLATFORM"' in run["run"] and "${{ inputs" not in run["run"]            # inputs reach the script only as environment variables
    install = next(s for s in steps if str(s.get("name", "")).startswith("Install the isolated Scrapling"))
    assert install["if"] == "${{ inputs.what != 'list' }}" and "-m patchright install --with-deps chromium" in install["run"] and "-m playwright install" not in install["run"]      # the browser Scrapling's stealth fetcher actually runs on
    save = next(s for s in steps if str(s.get("name", "")).startswith("Save what was found"))
    assert "git add -f data/_shared/sessions_health.json" in save["run"] and "git add -f data\n" not in save["run"]
    assert next(s for s in steps if str(s.get("name", "")).startswith("Show the run as red"))["if"] == "steps.run.outcome == 'failure'"
    acc = yaml.safe_load((wf / "acceptance.yml").read_text(encoding="utf-8"))["jobs"]["acceptance"]["steps"]
    step = next(s for s in acc if str(s.get("name", "")).startswith("Stored login sessions"))
    assert step["continue-on-error"] is True and all(step["env"][n] == "${{ secrets." + n + " }}" for n in names) and step["id"] == "sessions"
    assert "observatory sessions --all --what check" in step["run"] and "observatory sessions --all --what list" in step["run"] and "-m patchright install --with-deps chromium" in step["run"] and "-m playwright install" not in step["run"]
    red = next(s for s in acc if s.get("name") == "Show the run as red when a route failed")
    assert "steps.sessions.outcome == 'failure'" in red["if"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for n in names:
        assert f"| `{n}` |" in readme, n


def test_a_platform_that_shows_the_runner_a_challenge_page_is_blocked_not_rejected_and_the_session_is_asked_once_only(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    blocked = {"ok": False, "status": 0, "html": "", "error": {"code": "PLATFORM_BLOCKED", "message": "x.com still refused after 1 attempt(s) (a bot-check or challenge page was shown)"}}
    worker = Worker(lambda item: blocked)
    row = check("x", settings, ledger, worker, now="2026-11-12T10:00:00Z")
    assert row["state"] == SL.BLOCKED and "bot-check or challenge page to this runner, so the session itself could not be judged" in row["reason"] and "not a bad session" in row["reason"]
    assert row["proved_on"] == NOW and SL.exit_code([row]) == 0 and "BLOCKED" in SL.ORDER                                       # a block learns nothing about the session: the proof stays and the run is not red
    assert worker.jobs[0]["retry"] == {"attempts": 1, "backoff_seconds": 0} and len(worker.jobs) == 1                              # a challenge page is never asked for again with the same session
    with_note = dict(blocked, browser_note="no browser is installed here, so the page was read over plain HTTP", browser_error="Executable doesn't exist")
    assert "; and the browser could not be used here (Executable doesn't exist)." in check("x", settings, ledger, Worker(lambda item: with_note))["reason"] + "."
    nothing = {"ok": False, "status": 503, "html": "", "error": {"code": "UPSTREAM_BROKEN", "message": "x.com answered 503"}}
    assert check("x", settings, ledger, Worker(lambda item: nothing))["state"] == SL.CANNOT_CHECK                              # only a bot-check is a block


def test_the_days_left_say_less_than_a_day_instead_of_zero_days(settings, ledger):
    rows = [{"platform": "reddit", "label": "Reddit", "state": SL.UNCONFIRMED, "reason": "r", "expires_on": "2026-11-13", "days_left": 0, "proved_on": ""},
            {"platform": "x", "label": "X (Twitter)", "state": SL.ACCEPTED, "reason": "r", "expires_on": "2027-11-12", "days_left": 364, "proved_on": ""},
            {"platform": "tiktok", "label": "TikTok", "state": SL.ACCEPTED, "reason": "r", "expires_on": "2026-11-14", "days_left": 1, "proved_on": ""}]
    lines = SL.format_report(rows).splitlines()
    assert "expires 2026-11-13 (less than a day left)" in lines[1] and "0 day(s)" not in "\n".join(lines)
    assert "expires 2027-11-12 (364 day(s) left)" in lines[2] and "expires 2026-11-14 (1 day(s) left)" in lines[3]
    assert lines[-2].strip() == "2 ACCEPTED, 1 UNCONFIRMED"


def test_every_workflow_that_installs_scrapling_s_browser_does_it_through_patchright_which_is_the_library_its_stealth_fetcher_runs_on():
    import re
    for name in ("sessions.yml", "acceptance.yml", "observatory.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        scrapling_lines = [ln for ln in text.splitlines() if "scrapling-env/bin/python" in ln and " -m " in ln and "install" in ln]
        assert scrapling_lines and all("-m patchright install --with-deps chromium" in ln for ln in scrapling_lines), name
        assert not [ln for ln in text.splitlines() if "scrapling-env" in ln and "-m playwright install" in ln], name
    worker = (ROOT / "contentintel" / "observatory" / "scrapling_worker.py").read_text(encoding="utf-8")
    assert "from scrapling.fetchers import StealthyFetcher" in worker
    pin = (ROOT / "requirements-scrapling.txt").read_text(encoding="utf-8")
    assert re.search(r"^scrapling\[fetchers\]==0\.5$", pin, re.M)                                                              # the version whose stealth fetcher imports patchright: a new one is checked before it is adopted


# ---- Update 52: what each page showed, the visible-text rule, the last resort for X, and discovery ----------------------------------------------------

def page_with(platform, **over):
    base = {"ok": False, "status": 200, "final_url": S.PLATFORMS[platform]["login_url"] + "?next=%2F&token=SECRETQUERYVALUE#frag", "title": "Log in",
            "html": '<html><head><title> Log in  </title></head><body><form><input type="password"></form>' + "x " * 120 + "</body></html>",
            "text": "Log in to continue. " + "word " * 80, "fetched_with": "browser", "cookie_names_back": ["csrftoken", "mid"],
            "error": {"code": "UPSTREAM_BROKEN", "message": "https://x.example/ shows only a login page"}}
    return {**base, **over}


def test_a_platform_that_was_really_checked_shows_what_its_page_showed_and_never_a_secret_or_a_query(settings, ledger):
    wall = page_with("instagram")
    row = check("instagram", settings, ledger, Worker(lambda item: wall))
    ev = row["evidence"]
    assert row["state"] == SL.REJECTED and ev["fetched_with"] == "browser" and ev["status"] == 200 and ev["title"] == "Log in" and ev["landed"] == "https://www.instagram.com/accounts/login/"
    assert ev["password_field"] is True and ev["login_word_in_address"] == "login" and ev["cookies_back"] == ["csrftoken", "mid"] and ev["cookies_sent"] == ["csrftoken", "sessionid"]
    assert ev["logged_in_found"] == [] and len(ev["logged_in_missing"]) == 4 and len(ev["text_starts"]) <= 240 and ev["text_starts"].startswith("Log in to continue.")
    lines = SL.evidence_lines(row)
    assert lines[0] == "      page: read with the browser; HTTP 200; landed on https://www.instagram.com/accounts/login/; title 'Log in'"
    assert lines[1] == "      cookies sent (names): csrftoken, sessionid; the platform sent back (names): csrftoken, mid"
    assert lines[2].startswith("      logged-in markers found: none of 4 (") and "password field on the page: yes; login or challenge word in the address: login" in lines[2]
    assert lines[3].startswith("      visible text starts: 'Log in to continue.") and lines[4] == "      the worker said: UPSTREAM_BROKEN: https://x.example/ shows only a login page"
    text = "\n".join(lines) + json.dumps(row)
    assert "SECRETQUERYVALUE" not in text and "frag" not in ev["landed"] and "?" not in ev["landed"] and VALUE not in text and secret("instagram") not in text          # no query, no fragment, no cookie value
    good = check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    one = SL.evidence_lines(good)
    assert len(one) == 1 and one[0].startswith("      page: read with the browser; HTTP 200; landed on https://x.com/home; title '") or one[0].startswith("      page: read with the browser; HTTP 200; landed on https://x.com/home; no title")
    assert "logged-in markers found: " in one[0] and "visible text" not in one[0] and "cookies sent" not in one[0]                                          # an ACCEPTED platform gets one line
    assert SL.evidence_lines({"state": SL.NOT_SET_UP}) == []                                                                                              # nothing was loaded: nothing to show


def test_the_report_prints_the_evidence_under_each_platform_that_was_checked_and_not_under_the_offline_list(settings, ledger):
    worker = Worker(lambda item: page_with("instagram") if platform_of(item) == "instagram" else logged_in_page("x"))
    rows = SL.check_all(settings, "owner@example.com", sys.executable, environ=environ("instagram", "x"), runner=worker, now=NOW, ledger=ledger)
    live = SL.format_report(rows).splitlines()
    at = next(i for i, ln in enumerate(live) if ln.strip().startswith("Instagram"))
    assert live[at + 1].startswith("      page: read with the browser") and live[at + 3].startswith("      logged-in markers found: none of")
    offline = SL.format_report(SL.check_all(settings, "", "", environ=environ("x"), now=NOW, ledger=ledger, offline=True), offline=True)
    assert "      page:" not in offline
    assert "If a session fails, a read falls back to discovery (the search layer, no login): not available in this run" in SL.format_report(rows, discovery=SL.discovery_status({}))


def test_the_logged_out_wording_counts_only_in_the_visible_text_not_in_the_pages_translations():
    html = '<html><title>TikTok</title><script>{"i18n":{"login":"Log in to TikTok"}}</script><div data-e2e="profile-icon">x</div>' + "filler " * 60 + "</html>"
    assert S.check_logged_in("tiktok", "https://www.tiktok.com/", html, 200)[0] is False                                    # no visible text given: the old rule, the whole page
    assert S.check_logged_in("tiktok", "https://www.tiktok.com/", html, 200, text="For You Following Upload Messages") == (True, "logged in")
    assert S.check_logged_in("tiktok", "https://www.tiktok.com/", html, 200, text="Log in to TikTok  Manage your account") == (False, "the page shows the logged-out state")
    assert S.check_logged_in("tiktok", "https://www.tiktok.com/", html, 200, text="  LOG IN   to   tiktok ")[0] is False                     # case and spacing do not hide it
    assert S.check_logged_in("tiktok", "https://www.tiktok.com/", html.replace('data-e2e="profile-icon"', "x"), 200, text="For You")[0] is False  # no logged-in marker at all is still not logged in
    ev = S.page_evidence("tiktok", "https://user:pw@www.tiktok.com/login?token=SECRET#frag", html, 200, "Log in to TikTok  Manage")
    assert ev["logged_out_in_text"] == ["log in to tiktok"] and ev["logged_out_only_in_code"] == [] and ev["landed"] == "https://www.tiktok.com/login" and "pw" not in ev["landed"]
    ev = S.page_evidence("tiktok", "https://www.tiktok.com/", html, 200, "For You")
    assert ev["logged_out_in_text"] == [] and ev["logged_out_only_in_code"] == ["log in to tiktok"] and ev["logged_in_found"] == ['data-e2e="profile-icon"'] and ev["login_word_in_address"] == ""


def test_the_worker_is_asked_for_the_visible_text_and_a_logged_in_page_with_wording_only_in_its_code_is_accepted(settings, ledger):
    page = dict(logged_in_page("tiktok"), html=logged_in_page("tiktok")["html"] + ' {"i18n":"Log in to TikTok"}', text="For You Following Upload")
    assert check("tiktok", settings, ledger, Worker(lambda item: page))["state"] == SL.ACCEPTED
    wall = dict(page, text="Log in to TikTok Use phone / email / username")
    assert check("tiktok", settings, ledger, Worker(lambda item: wall))["state"] == SL.REJECTED


# ---- the last resort for X ---------------------------------------------------------------------------------------------------------------------------

BLOCKED_X = {"ok": False, "status": 0, "html": "", "fetched_with": "browser", "error": {"code": "PLATFORM_BLOCKED", "message": "x.com showed a challenge or bot-check page"}}


def tw_python(tmp_path):
    path = tmp_path / "twscrape-python"
    path.write_text("#!/bin/sh\n")
    return str(path)


class TwRunner:
    """A stand-in for running the checker: records the command and the environment it was given, and answers with the given standard output."""

    def __init__(self, answer, code=0):
        self.answer, self.code, self.calls = answer, code, []

    def __call__(self, argv, env=None, **kw):
        self.calls.append((list(argv), dict(env or {}), kw))
        return subprocess.CompletedProcess(argv, self.code, self.answer, "log line on stderr")


def with_tw(worker, tw, tmp_path, **more):
    def run(argv, env=None, **kw):
        return tw(argv, env=env, **kw) if str(argv[0]) == tw_python(tmp_path) else worker(argv, env=env, **kw)
    return run, {**environ("x"), "TWSCRAPE_PYTHON": tw_python(tmp_path), "ANTHROPIC_API_KEY": "sk-ant-NOT-FOR-IT", **more}


def x_row(settings, ledger, tmp_path, answer, **kw):
    tw = TwRunner(answer, **kw)
    runner, env = with_tw(Worker(lambda item: BLOCKED_X), tw, tmp_path)
    row = SL.check_platform("x", settings, "owner@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)
    return row, tw


def test_when_x_shows_a_challenge_its_own_profile_interface_is_asked_with_the_stored_cookies_alone_and_a_good_answer_is_accepted(settings, ledger, tmp_path):
    row, tw = x_row(settings, ledger, tmp_path, json.dumps({"state": "accepted", "detail": "the profile of account id 123456789 was read through X's own interface with the stored cookies"}) + "\n")
    assert row["state"] == SL.ACCEPTED and row["via"] == "twscrape" and "so the browser read of x.com is not proved" in row["reason"] and "checked through twscrape because x.com's web page showed the runner a challenge" in row["reason"]
    argv, env, kw = tw.calls[0]
    assert argv == [tw_python(tmp_path), str(SL.TWSCRAPE_SCRIPT)] and SL.TWSCRAPE_SCRIPT.name == "x_twscrape_check.py" and kw["timeout"] == 150
    assert sorted(env) == ["DO_NOT_TRACK", "HOME", "PATH", "PYTHONDONTWRITEBYTECODE", "TWS_TELEMETRY", "X_SESSION_COOKIES"] and env["TWS_TELEMETRY"] == "0" and env["DO_NOT_TRACK"] == "1"           # one session's cookies, telemetry off, nothing else of ours
    assert json.loads(env["X_SESSION_COOKIES"]) == {n: VALUE + n for n in S.PLATFORMS["x"]["required"]} and "ANTHROPIC_API_KEY" not in env
    assert ledger.proved_via("x") == "twscrape" and ledger.proved_on("x") == NOW
    assert not ledger.proved("x", NOW) and ledger.proved("x", NOW, via="twscrape")                                              # a different route's proof is not proof for the browser read
    assert "last proved 2026-11-11 (through twscrape, not the browser)" in SL.format_report([row])
    assert SL.exit_code([row]) == 0


def test_a_rejection_from_x_voids_the_proof_and_turns_the_run_red_while_anything_that_judges_nothing_keeps_the_block(settings, ledger, tmp_path):
    reasons = {"rejected": "X did not accept the two cookies (error 32, could not authenticate you): the session has expired or was ended"}
    row, _ = x_row(settings, ledger, tmp_path, json.dumps({"state": "rejected", "detail": reasons["rejected"]}))
    assert row["state"] == SL.REJECTED and "error 32" in row["reason"] and "(checked through twscrape" in row["reason"] and row["proved_on"] == "" and SL.exit_code([row]) == 1
    for answer in (json.dumps({"state": "unreachable", "detail": "NoAccountError: No account available for queue UserByRestId"}), "not json at all", "", json.dumps({"state": "weird", "detail": "x"})):
        row, _ = x_row(settings, ledger, tmp_path, answer)
        assert row["state"] == SL.BLOCKED and "not a bad session" in row["reason"] and "The last-resort twscrape check could not judge it either:" in row["reason"] and SL.exit_code([row]) == 0
    row, _ = x_row(settings, ledger, tmp_path, "", code=3)
    assert "gave no answer (exit 3)" in row["reason"] and row["state"] == SL.BLOCKED
    class Dies(TwRunner):
        def __call__(self, argv, env=None, **kw):
            raise subprocess.TimeoutExpired(argv, 150)
    runner, env = with_tw(Worker(lambda item: BLOCKED_X), Dies(""), tmp_path)
    row = SL.check_platform("x", settings, "o@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)
    assert row["state"] == SL.BLOCKED and "could not be run (TimeoutExpired)" in row["reason"]
    row = SL.check_platform("x", settings, "o@example.com", sys.executable, environ=environ("x"), runner=Worker(lambda item: BLOCKED_X), now=NOW, ledger=ledger)
    assert row["state"] == SL.BLOCKED and "the twscrape environment is not installed here (TWSCRAPE_PYTHON), so X's profile interface was not asked" in row["reason"]      # nothing is installed or run when it is not there


def test_only_x_gets_the_last_resort_and_only_when_the_browser_was_blocked(settings, ledger, tmp_path):
    tw = TwRunner(json.dumps({"state": "accepted", "detail": "ok"}))
    runner, env = with_tw(Worker(lambda item: dict(BLOCKED_X)), tw, tmp_path)
    env = {**env, **environ("instagram")}
    row = SL.check_platform("instagram", settings, "o@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)
    assert row["state"] == SL.BLOCKED and tw.calls == [] and "twscrape" not in row["reason"]                                    # another platform is never sent to it
    runner, env = with_tw(Worker(lambda item: login_wall("x")), tw, tmp_path)
    assert SL.check_platform("x", settings, "o@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)["state"] == SL.REJECTED and tw.calls == []       # a page that was loaded and said logged out is not second-guessed
    runner, env = with_tw(Worker(lambda item: logged_in_page("x")), tw, tmp_path)
    assert SL.check_platform("x", settings, "o@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)["via"] == "browser" and tw.calls == []
    sec = settings["services"]["stored_sessions"]
    off = {**settings, "services": {**settings["services"], "stored_sessions": {**sec, "channels": {**sec["channels"], "x": False}}}}
    runner, env = with_tw(Worker(lambda item: dict(BLOCKED_X)), tw, tmp_path)
    assert SL.check_platform("x", off, "o@example.com", sys.executable, environ=env, runner=runner, now=NOW, ledger=ledger)["state"] == SL.OFF and tw.calls == []


# ---- the program that asks X (it runs in its own environment; here a stand-in for twscrape) ---------------------------------------------------------

from contentintel import x_twscrape_check as XT        # noqa: E402

AUTH, CT0, TWID = "AUTHTOKENVALUE0123456789", "CTZEROVALUE0123456789", "u%3D1234567890"
JAR = {"auth_token": AUTH, "ct0": CT0, "twid": TWID, "kdt": "ignored-cookie-value"}


class FakePool:
    def __init__(self, error_msg=None, active=True, fail_add=False):
        self.added, self.error_msg, self.active, self.fail_add = [], error_msg, active, fail_add

    async def add_account_cookies(self, name, header):
        if self.fail_add:
            raise RuntimeError("cannot add " + header)
        self.added.append((name, header))

    async def get_all(self):
        return [type("Account", (), {"error_msg": self.error_msg, "active": self.active})()]


class FakeApi:
    def __init__(self, user=None, pool=None, raises=None, slow=False):
        self.user, self.pool, self.raises, self.slow, self.asked = user, pool or FakePool(), raises, slow, []

    async def user_by_id(self, uid):
        self.asked.append(uid)
        if self.slow:
            import asyncio
            await asyncio.sleep(5)
        if self.raises:
            raise self.raises
        return self.user


def run_check(jar, api, timeout=60):
    import asyncio
    return asyncio.run(XT.check(jar, api_factory=lambda path: api, timeout=timeout))


def test_the_x_program_reads_one_profile_with_the_two_cookies_and_calls_a_returned_profile_accepted():
    api = FakeApi(user=object())
    got = run_check(JAR, api)
    assert got == {"state": "accepted", "detail": "the profile of account id 1234567890 was read through X's own interface with the stored cookies"}
    assert api.asked == [1234567890] and api.pool.added == [("session-check", f"auth_token={AUTH}; ct0={CT0}; twid={TWID}")]          # one profile; only the cookies it needs
    assert "ignored-cookie-value" not in json.dumps(api.pool.added)


def test_the_x_program_reports_what_x_said_about_the_account_and_never_guesses():
    cases = [("(32) Could not authenticate you", True, "rejected", "error 32"), ("(326) Authorization: Denied by access control: To protect our users ... this account is temporarily locked", True, "rejected", "error 326"),
             ("(88) Rate limit exceeded", True, "rejected", "error 88"), (None, False, "rejected", "answered 403")]
    for msg, active, state, word in cases:
        got = run_check(JAR, FakeApi(pool=FakePool(msg, active)))
        assert got["state"] == state and word in got["detail"], msg
    nothing = run_check(JAR, FakeApi(pool=FakePool(None, True)))
    assert nothing["state"] == "unreachable" and "a bot-check, a block or a network problem" in nothing["detail"]                 # no profile and no word from X: nothing is judged
    assert run_check(JAR, FakeApi(slow=True), timeout=0.05) == {"state": "unreachable", "detail": "the profile read timed out"}
    boom = run_check(JAR, FakeApi(raises=RuntimeError(f"connection broke while sending {AUTH} and {CT0}")))
    assert boom["state"] == "unreachable" and AUTH not in boom["detail"] and CT0 not in boom["detail"] and "RuntimeError: connection broke while sending *** and ***" == boom["detail"]
    fail = run_check(JAR, FakeApi(pool=FakePool(fail_add=True)))
    assert fail["state"] == "unreachable" and AUTH not in fail["detail"]


def test_the_x_program_needs_the_cookies_and_the_profile_id_and_says_so_without_asking_x_anything():
    api = FakeApi(user=object())
    for jar, word in (({"ct0": CT0, "twid": TWID}, "auth_token"), ({"auth_token": AUTH, "twid": TWID}, "ct0"), ({"auth_token": AUTH, "ct0": CT0}, "twid"), ({}, "auth_token or ct0")):
        got = run_check(jar, api)
        assert got["state"] == "unreachable" and word in got["detail"]
    assert api.asked == [] and api.pool.added == []
    assert [XT.user_id_from({"twid": v}) for v in ("u%3D1234567890", "u=1234567890", '"u=1234567890"', "1234567890", "u%3D12", "", "garbage")] == ["1234567890", "1234567890", "1234567890", "1234567890", "", "", ""]
    assert XT.explain(None, None) is None and XT.explain("", True) is None and XT.explain("(404) not found", True) is None


def test_the_x_program_prints_one_json_line_switches_telemetry_off_before_twscrape_loads_and_never_prints_a_cookie_value():
    import io
    out = io.StringIO()
    assert XT.main({"X_SESSION_COOKIES": json.dumps(JAR)}, out=out, api_factory=lambda path: FakeApi(user=object())) == 0
    assert json.loads(out.getvalue()) == {"state": "accepted", "detail": "the profile of account id 1234567890 was read through X's own interface with the stored cookies"} and out.getvalue().count("\n") == 1
    for bad in ("", "not json", "[1, 2]", "null"):
        out = io.StringIO()
        XT.main({"X_SESSION_COOKIES": bad}, out=out)
        got = json.loads(out.getvalue())
        assert got["state"] == "unreachable" and ("not handed over as an object" in got["detail"] or "no auth_token or ct0" in got["detail"])
    assert not [v for v in (AUTH, CT0) if v in out.getvalue()]
    source = Path(XT.__file__).read_text(encoding="utf-8")
    assert source.index('os.environ["TWS_TELEMETRY"] = "0"') < source.index("from twscrape import API") and 'os.environ["DO_NOT_TRACK"] = "1"' in source
    assert not [w for w in ("api.search", "api.tweet", "user_tweets", "tweet_details", "followers", "following", "retweeters") if w in source]            # it reads one profile and calls no search or timeline interface
    pin = (ROOT / "requirements-twscrape.txt").read_text(encoding="utf-8").split()
    assert pin == ["twscrape==0.20.1"]


@pytest.mark.skipif(not __import__("os").environ.get("TWSCRAPE_PYTHON"), reason="needs TWSCRAPE_PYTHON: an environment with twscrape installed")
def test_the_real_twscrape_library_runs_the_program_to_the_point_of_asking_x_and_reports_what_happened_in_one_json_line():
    env = {"X_SESSION_COOKIES": json.dumps(JAR), "PATH": __import__("os").environ.get("PATH", ""), "HOME": "/tmp"}
    done = subprocess.run([__import__("os").environ["TWSCRAPE_PYTHON"], str(SL.TWSCRAPE_SCRIPT)], env=env, capture_output=True, text=True, timeout=180)
    last = json.loads([ln for ln in done.stdout.splitlines() if ln.startswith("{")][-1])
    assert last["state"] in ("accepted", "rejected", "unreachable") and AUTH not in done.stdout and CT0 not in done.stdout and AUTH not in last["detail"]


# ---- discovery: the fallback when neither the plain reader nor the session can read the page --------------------------------------------------------

class FakeSearch:
    def __init__(self, results=None, raises=None):
        self.results, self.raises, self.queries = results or [], raises, []

    def search(self, query, *, kind="web", num=10, **kw):
        self.queries.append((query, kind, num))
        if self.raises:
            raise self.raises
        return self.results


def result(rank, title, url, snippet):
    from contentintel.providers import Result
    return Result(rank, title, url, snippet)


def test_when_the_page_and_the_session_cannot_be_read_discovery_answers_with_search_snippets_and_says_what_they_are(settings, ledger):
    check("x", settings, ledger, Worker(lambda item: logged_in_page("x")))
    search = FakeSearch([result(1, "Some account (@someaccount) / X", "https://x.com/someaccount", "Posts about overalls"), result(2, "A post", "https://x.com/someaccount/status/1", "A short post"),
                         result(3, "", "https://example.com/empty", "")])
    got = read(settings, ledger, Worker(page_for(WALL, WALL)), providers=[("exa", search)])
    assert got.route == "discovery" and not got.skipped and [t[:2] for t in got.trail][:2] == [("primary", K.NEED_LOGIN), ("session", K.NEED_LOGIN)]
    assert [(r["title"], r["route"], r["provider"]) for r in got.rows] == [("Some account (@someaccount) / X", "discovery", "exa"), ("A post", "discovery", "exa")] and got.rows[0]["text"] == "Posts about overalls"
    assert search.queries == [("x.com someaccount", "web", 5)]                                                                  # one search, about the address, five results at most
    unproved = read(settings, SL.SessionLedger(tmp_ledger_path(ledger)), Worker(page_for(WALL, WALL)), providers=[("serper_dev", FakeSearch([result(1, "T", "https://x.com/a", "s")]))])
    assert unproved.route == "discovery" and [t[:2] for t in unproved.trail][:2] == [("primary", K.NEED_LOGIN), ("session", "off")]          # a session that was never proved is skipped, and discovery still answers


def tmp_ledger_path(ledger):
    return ledger.path.with_name("empty_sessions_health.json")


def test_a_failing_search_ends_in_an_honest_skip_with_the_true_reason_and_a_search_is_made_only_once(settings, ledger):
    from contentintel import providers as P
    for exc, code in ((P.AuthError("Exa rejected the API key (HTTP 401)"), K.NEED_SETUP), (P.QuotaError("Exa is out of credits (HTTP 402)"), K.RATE_LIMITED), (P.ProviderError("Exa: HTTP 500"), K.UPSTREAM_BROKEN)):
        search = FakeSearch(raises=exc)
        got = read(settings, ledger, Worker(page_for(WALL, WALL)), providers=[("exa", search)])
        assert got.skipped and got.trail[-1][:2] == ("discovery", code) and len(search.queries) == 1 and "discovery:" in got.reason
    empty = FakeSearch([])
    got = read(settings, ledger, Worker(page_for(WALL, WALL)), providers=[("exa", empty)])
    assert not got.skipped and got.route == "" and len(empty.queries) == 1                                                      # a real, empty answer
    primary = Worker(page_for(PAGE, PAGE))
    search = FakeSearch([result(1, "T", "https://x.com/a", "s")])
    assert read(settings, ledger, primary, providers=[("exa", search)]).route == "primary" and search.queries == []             # discovery is never asked when the page was read


def test_the_discovery_status_names_the_provider_that_would_be_asked_and_says_when_none_can():
    assert SL.discovery_status({}) == "not available in this run: no search key (EXA_API_KEY, SERPER_API_KEY or TAVILY_API_KEY) is passed to it"
    assert SL.discovery_status({"SERPER_API_KEY": "k"}) == "available here, through serper_dev"
    assert SL.discovery_status({"TAVILY_API_KEY": "k", "EXA_API_KEY": "k", "SERPER_API_KEY": "k"}) == "available here, through exa (then serper_dev, tavily)"
    assert [n for n, _ in SL.discovery_providers({"EXA_API_KEY": "k", "SERPER_API_KEY": "k"})] == ["exa", "serper_dev"] and SL.SEARCH_ORDER[0] == "exa"
    assert [SL._query_for(u) for u in ("https://www.instagram.com/some_account/?hl=en#x", "https://x.com/someaccount", "https://www.reddit.com/r/overalls-fans/")] == ["instagram.com some account", "x.com someaccount", "reddit.com r overalls fans"]


def test_the_command_says_when_it_fell_back_to_discovery_and_prints_the_discovery_line_in_the_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(SL, "discovery_rows", lambda url, environ, providers=None: [{"url": "https://x.com/a", "final_url": "https://x.com/a", "title": "A title", "text": "A snippet", "chars": 9, "route": "discovery", "provider": "exa"}])
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "read", "--key", "x", "--url", URL)
    assert code == 0 and "The page itself could not be read, so this is discovery (the search layer, exa, no login): titles and snippets about the address, not the page." in out and "- A title" in out and "A snippet" in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "list")
    assert "If a session fails, a read falls back to discovery (the search layer, no login): not available in this run" in out
    code, out = run_cli(tmp_path, monkeypatch, capsys, "--what", "list", EXA_API_KEY="k")
    assert "available here, through exa" in out


def test_the_workflows_pass_the_search_keys_to_the_read_and_install_twscrape_only_when_an_x_session_is_stored():
    wf = ROOT / ".github" / "workflows"
    sess = yaml.safe_load((wf / "sessions.yml").read_text(encoding="utf-8"))["jobs"]["sessions"]["steps"]
    run = next(s for s in sess if s.get("name") == "Stored login sessions")["env"]
    assert all(run[n] == "${{ secrets." + n + " }}" for n in ("SERPER_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY"))
    install = next(s for s in sess if str(s.get("name", "")).startswith("Install the isolated twscrape environment"))
    assert install["if"] == "${{ inputs.what == 'check' }}" and install["env"] == {"X_SESSION_B64": "${{ secrets.X_SESSION_B64 }}"} and 'if [ -n "$X_SESSION_B64" ]; then' in install["run"]
    assert 'pip" install -q -r requirements-twscrape.txt' in install["run"] and 'TWSCRAPE_PYTHON=$RUNNER_TEMP/twscrape-env/bin/python" >> "$GITHUB_ENV"' in install["run"]
    acc = yaml.safe_load((wf / "acceptance.yml").read_text(encoding="utf-8"))["jobs"]["acceptance"]["steps"]
    step = next(s for s in acc if str(s.get("name", "")).startswith("Stored login sessions"))["run"]
    assert 'if [ -n "$X_SESSION_B64" ]; then' in step and "install -q -r requirements-twscrape.txt" in step and 'export TWSCRAPE_PYTHON=' in step
    assert step.index("twscrape-env") < step.index("observatory sessions --all --what check")
