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
    assert lines[0].startswith("Stored login sessions: each platform is judged on its own, live") and len(lines) == 8 + 3
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
    assert unproved.skipped and unproved.route == "" and [t[:2] for t in unproved.trail] == [("primary", K.NEED_LOGIN), ("session", "off")]
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
    assert got.skipped and len(worker.jobs) == 2 and [t[1] for t in got.trail] == [K.NOT_FOUND, K.NEED_LOGIN]
    assert "primary: NOT_FOUND" in got.reason and "session: NEED_LOGIN" in got.reason and VALUE not in got.reason
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
