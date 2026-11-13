"""Stored login sessions: decoding, loading, expiry, status, the logged-in check, capture, the fallback chain, the Gmail code reader, and that nothing secret is ever committed or printed."""
import base64
import json
import stat
from datetime import datetime
from pathlib import Path

import pytest

from contentintel import contract as K
from contentintel import gmail_otp as OTP
from contentintel import session_chain as CH
from contentintel import sessions as S
from conftest import ROOT

NOW = "2026-11-11T10:00:00Z"
SECRET_VALUE = "SUPERSECRETVALUE0123456789"


def epoch(date):
    return datetime.fromisoformat(date + "T00:00:00+00:00").timestamp()


def cookie(name, value=SECRET_VALUE, domain=".instagram.com", expires=None, **kw):
    out = {"name": name, "value": value, "domain": domain, "path": "/", "httpOnly": True, "secure": True}
    if expires is not None:
        out["expires"] = epoch(expires) if isinstance(expires, str) else expires
    return {**out, **kw}


def state(*cookies, origins=None):
    return {"cookies": list(cookies), "origins": origins or []}


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def good_instagram(expires="2027-01-20"):
    return state(cookie("sessionid", expires=expires), cookie("csrftoken", expires="2027-06-01"), cookie("mid", "x", expires="2028-01-01"))


def env(platform="instagram", obj=None):
    return {S.PLATFORMS[platform]["secret"]: b64(obj if obj is not None else good_instagram())}


# ---- decoding ----------------------------------------------------------------------------------------------------------------------------------------

def test_a_session_survives_encoding_and_decoding_unchanged():
    original = good_instagram()
    text, notes = S.encode_session(original)
    assert notes == [] and S.decode_session(text) == original
    assert S.decode_session("  " + text[:20] + "\n" + text[20:] + "  ") == original                      # a pasted secret may have stray spaces and line breaks


@pytest.mark.parametrize("bad, expect", [("", "it is empty"), ("   ", "it is empty"), ("not base64 at all!!", "not valid base64"), (base64.b64encode(b"just text").decode(), "not to a session or a cookie export"),
                                         (b64({"cookies": []}), "holds no cookies"), (b64({"nothing": 1}), "holds no cookies"), (b64({"cookies": [{"name": "a"}]}), "not in the expected shape"),
                                         (b64({"cookies": [{"name": "a", "value": 1, "domain": "x"}]}), "not in the expected shape"), (b64([1, 2]), "not in the expected shape"),
                                         (b64({"cookies": [cookie("a")] * (S.MAX_COOKIES + 1)}), "unreasonable number")])
def test_a_secret_that_is_not_a_session_is_refused_with_a_reason_that_never_contains_it(bad, expect):
    with pytest.raises(S.SessionProblem) as seen:
        S.decode_session(bad)
    assert expect in str(seen.value) and SECRET_VALUE not in str(seen.value)


def test_a_session_too_large_for_a_github_secret_loses_its_page_data_largest_first_and_then_says_so():
    big = {"origin": "https://big.example", "localStorage": [{"name": "k", "value": "x" * 40000}]}
    small = {"origin": "https://small.example", "localStorage": [{"name": "k", "value": "y" * 100}]}
    text, notes = S.encode_session({**good_instagram(), "origins": [small, big]})
    assert len(text) <= S.MAX_ENCODED_CHARS and any("big.example" in n for n in notes) and not any("small.example" in n for n in notes)
    assert [o["origin"] for o in S.decode_session(text)["origins"]] == ["https://small.example"]
    with pytest.raises(S.SessionProblem) as seen:
        S.encode_session(state(*[cookie(f"c{i}", "z" * 1200) for i in range(S.MAX_COOKIES - 1)]))
    assert "more than a GitHub secret can hold" in str(seen.value)


# ---- loading and expiry -----------------------------------------------------------------------------------------------------------------------------

def failure(fn, *a, **k):
    with pytest.raises(K.Failure) as seen:
        fn(*a, **k)
    return seen.value


def test_a_missing_secret_is_need_setup_and_names_the_secret_and_the_guide():
    err = failure(S.load, "x", {}, NOW)
    assert err.code == K.NEED_SETUP and "X_SESSION_B64 is not set" in str(err) and "session-capture-guide" in str(err)
    assert failure(S.load, "x", {"X_SESSION_B64": "   "}, NOW).code == K.NEED_SETUP


def test_a_secret_that_is_not_a_session_is_need_setup_and_never_shows_its_value():
    err = failure(S.load, "instagram", {"INSTAGRAM_SESSION_B64": SECRET_VALUE + "!!"}, NOW)
    assert err.code == K.NEED_SETUP and "cannot be used" in str(err) and SECRET_VALUE not in str(err)


def test_cookies_from_the_wrong_site_or_a_capture_that_was_not_logged_in_are_need_setup():
    wrong = state(cookie("sessionid", domain=".example.com"), cookie("csrftoken", domain=".example.com"))
    err = failure(S.load, "instagram", env("instagram", wrong), NOW)
    assert err.code == K.NEED_SETUP and "wrong site" in str(err)
    err = failure(S.load, "instagram", env("instagram", state(cookie("csrftoken"), cookie("mid"))), NOW)
    assert err.code == K.NEED_SETUP and "missing the cookie sessionid" in str(err) and "not logged in" in str(err)
    reddit = state(cookie("loid", domain=".reddit.com"))
    assert "reddit_session or token_v2" in str(failure(S.load, "reddit", env("reddit", reddit), NOW))
    assert S.load("reddit", env("reddit", state(cookie("token_v2", domain=".reddit.com"))), NOW).platform == "reddit"      # either of the two is enough


def test_an_expired_session_is_need_login_with_the_date_and_how_often_to_capture():
    err = failure(S.load, "instagram", env("instagram", good_instagram(expires="2026-11-01")), NOW)
    assert err.code == K.NEED_LOGIN and "expired on 2026-11-01" in str(err) and "about 90 days" in str(err) and "INSTAGRAM_SESSION_B64" in str(err) and SECRET_VALUE not in str(err)


def test_the_expiry_is_that_of_the_cookies_the_route_depends_on_not_of_any_other():
    s = S.load("instagram", env("instagram", state(cookie("sessionid", expires="2026-12-25"), cookie("csrftoken", expires="2027-06-01"), cookie("mid", "x", expires="2026-11-12"))), NOW)
    assert s.expires_on() == "2026-12-25" and s.days_left() == 43 and not s.expired()                       # `mid` expires sooner but is not needed
    boundary = S.load("instagram", env("instagram", state(cookie("sessionid", expires="2026-11-11"), cookie("csrftoken", expires="2026-11-11"))), "2026-11-10T23:00:00Z")
    assert boundary.days_left() == 0 and not boundary.expired()
    session_only = S.load("instagram", env("instagram", state(cookie("sessionid", expires=-1), cookie("csrftoken", expires=0))), NOW)
    assert session_only.expires_on() == "" and session_only.days_left() is None and not session_only.expired()      # no expiry written down: not treated as expired


def test_status_says_unknown_down_degraded_or_healthy_with_the_true_reason_and_makes_no_request():
    assert S.status("x", {}, NOW)["state"] == "unknown" and S.status("x", {}, NOW)["reason"] == "not set up"
    down = S.status("instagram", {"INSTAGRAM_SESSION_B64": "garbage"}, NOW)
    assert down["state"] == "down" and "cannot be used" in down["reason"]
    expired = S.status("instagram", env("instagram", good_instagram(expires="2026-10-01")), NOW)
    assert expired["state"] == "degraded" and "expired on 2026-10-01" in expired["reason"]                    # expired shows as degraded with the reason: not healthy, not unknown
    soon = S.status("instagram", env("instagram", state(cookie("sessionid", expires="2026-11-15"), cookie("csrftoken", expires="2027-01-01"))), NOW)
    assert soon["state"] == "degraded" and "expires in 3 day(s)" in soon["reason"] and soon["days_left"] == 3
    ok = S.status("instagram", env(), NOW)
    assert ok["state"] == "healthy" and ok["expires_on"] == "2027-01-20" and ok["verified"] == "no" and "not yet proved against the live site" in ok["reason"]
    assert all(SECRET_VALUE not in str(v) for v in (down, expired, soon, ok))


# ---- using a session --------------------------------------------------------------------------------------------------------------------------------

def loaded():
    return S.load("instagram", env("instagram", state(cookie("sessionid", "sess-1", expires="2027-01-20"), cookie("csrftoken", "csrf-1", expires="2027-06-01"),
                                                    cookie("other", "not-mine", domain=".example.com"))), NOW)


def test_printing_or_logging_a_session_never_shows_a_value():
    s = loaded()
    for text in (repr(s), str(s), f"{s}", "%s" % s, f"{[s]}"):
        assert "sess-1" not in text and "csrf-1" not in text and "2 cookies" in text and "instagram" in text


def test_only_the_platforms_own_cookies_are_ever_handed_on():
    s = loaded()
    assert s.names() == {"sessionid", "csrftoken"} and "not-mine" not in s.cookie_header() and s.cookie_header() == "sessionid=sess-1; csrftoken=csrf-1"
    name, text = s.worker_env()
    assert name == "SESSION_COOKIES_INSTAGRAM" and json.loads(text) == {"sessionid": "sess-1", "csrftoken": "csrf-1"} and "not-mine" not in text          # the worker reads an object of name and value, not a list
    net = s.netscape_text().splitlines()
    assert net[0] == "# Netscape HTTP Cookie File" and net[1].split("\t") == [".instagram.com", "TRUE", "/", "TRUE", str(int(epoch("2027-01-20"))), "sessionid", "sess-1"]


def test_a_cookie_file_is_written_only_for_its_owner_and_into_the_folder_it_is_given(tmp_path):
    path = loaded().write_private(tmp_path / "runner-temp")
    assert path.parent == tmp_path / "runner-temp" and path.name == "instagram_cookies.txt" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sess-1" in path.read_text()
    json_path = loaded().write_private(tmp_path / "runner-temp", kind="json")
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o600 and json.loads(json_path.read_text())[0]["name"] == "sessionid"


# ---- is it still logged in ---------------------------------------------------------------------------------------------------------------------------

PAGE = "<html>" + "welcome " * 40 + '"is_logged_in":true</html>'


def test_the_logged_in_check_accepts_only_the_real_logged_in_site():
    assert S.check_logged_in("facebook", "https://www.facebook.com/", PAGE) == (True, "logged in")
    assert S.check_logged_in("instagram", "https://www.instagram.com/", "<html>" + "x " * 200 + '"viewerId":"1"</html>')[0]


@pytest.mark.parametrize("url", ["https://www.instagram.com/Login", "https://www.instagram.com/accounts/login/?next=/", "https://www.instagram.com/LOGIN/", "https://www.instagram.com/challenge/abc",
                                 "https://www.instagram.com/accounts/suspended/", "https://www.instagram.com/auth_platform/checkpoint"])
def test_a_redirect_to_a_login_or_challenge_in_any_letter_case_is_not_logged_in_even_when_the_page_looks_fine(url):
    ok, why = S.check_logged_in("instagram", url, '<html>' + "x " * 200 + '"viewerId":"1"</html>')      # the R_C_E incident: a case-sensitive check let a dead session through for 40 minutes
    assert not ok and ("login" in why or "challenge" in why or "checkpoint" in why)


@pytest.mark.parametrize("url, html, status, expect", [
    ("https://www.instagram.com/", "", 200, "empty"), ("https://www.instagram.com/", "<html>tiny</html>", 200, "empty"),
    ("https://www.instagram.com/", PAGE, 403, "403"), ("https://www.instagram.com/", PAGE, 401, "401"),
    ("https://evil.example/", PAGE, 200, "different site"), ("https://instagram.com.evil.example/", PAGE, 200, "different site"),
    ("https://www.instagram.com/", "<html>" + "x " * 200 + "Log in to Instagram</html>", 200, "logged-out"),
    ("https://www.instagram.com/", "<html>" + "x " * 200 + "</html>", 200, "any doubt counts"),
    ("", PAGE, 200, "different site")])
def test_any_doubt_about_the_session_means_not_logged_in(url, html, status, expect):
    ok, why = S.check_logged_in("instagram", url, html, status)
    assert ok is False and expect in why


def test_the_logged_in_check_is_strict_about_the_host():
    assert S.check_logged_in("x", "https://x.com/home", '<html>' + "a " * 200 + '"isLoggedIn":true</html>')[0]
    assert S.check_logged_in("x", "https://twitter.com/home", '<html>' + "a " * 200 + '"isLoggedIn":true</html>')[0]
    assert not S.check_logged_in("x", "https://notx.com/home", '<html>' + "a " * 200 + '"isLoggedIn":true</html>')[0]


def test_an_unknown_platform_is_refused_everywhere():
    for call in (lambda: S.load("myspace", {}, NOW), lambda: S.status("myspace"), lambda: S.check_logged_in("myspace", "https://x", ""), lambda: S.refresh_session("myspace")):
        err = failure(call)
        assert err.code == K.BAD_INPUT and "unknown platform" in str(err)


def test_an_automatic_login_is_never_attempted_and_the_answer_says_to_capture_again():
    for platform, spec in S.PLATFORMS.items():
        err = failure(S.refresh_session, platform)
        assert err.code == K.NEED_LOGIN and spec["secret"] in str(err) and "by hand" in str(err)
    assert all(spec["verified"] is False for spec in S.PLATFORMS.values())                                    # nothing is called proved until a real run proves it


# ---- capture, by hand ---------------------------------------------------------------------------------------------------------------------------------

class FakeContext:
    def __init__(self, st):
        self.st = st

    def storage_state(self):
        return self.st


def fake_launch(st, closed):
    from contextlib import contextmanager

    @contextmanager
    def launch(url):
        launch.url = url
        yield FakeContext(st), lambda: closed.append(True)
    return launch


def test_capture_saves_an_owner_only_file_and_tells_the_owner_what_to_do_next(tmp_path):
    out, shown, closed = tmp_path / "ig_session.json", [], []
    launch = fake_launch(good_instagram(), closed)
    S.capture_session_interactive("instagram", out, launch=launch, input_fn=lambda prompt: "", print_fn=shown.append)
    assert launch.url == "https://www.instagram.com/accounts/login/" and closed == [True]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and S.decode_session(base64.b64encode(out.read_bytes()).decode())["cookies"][0]["name"] == "sessionid"
    text = "\n".join(shown)
    assert "dedicated account" in text and "INSTAGRAM_SESSION_B64" in text and "Never share or commit" in text and SECRET_VALUE not in text and "WARNING" not in text


def test_capture_warns_when_the_owner_was_not_actually_logged_in(tmp_path):
    shown = []
    S.capture_session_interactive("instagram", tmp_path / "s.json", launch=fake_launch(state(cookie("csrftoken")), []), input_fn=lambda p: "", print_fn=shown.append)
    assert any("WARNING" in line and "missing the cookie sessionid" in line and "run the capture again" in line for line in shown)


def test_capture_refuses_a_session_that_cannot_fit_a_secret(tmp_path):
    huge = state(*[cookie(f"c{i}", "z" * 1200) for i in range(S.MAX_COOKIES - 1)])
    err = failure(S.capture_session_interactive, "instagram", tmp_path / "s.json", launch=fake_launch(huge, []), input_fn=lambda p: "", print_fn=lambda *a: None)
    assert err.code == K.NEED_SETUP and "more than a GitHub secret can hold" in str(err) and not (tmp_path / "s.json").exists()


def test_the_encode_command_writes_the_secret_text_to_a_file_and_prints_none_of_it(tmp_path, capsys):
    src = tmp_path / "x_session.json"
    src.write_text(json.dumps(state(cookie("auth_token", domain=".x.com"), cookie("ct0", domain=".x.com"))))
    assert S.main(["encode", "--in", str(src)]) == 0
    out = capsys.readouterr().out
    target = Path(str(src) + ".b64.txt")
    assert "Wrote" in out and SECRET_VALUE not in out and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert S.load("x", {"X_SESSION_B64": target.read_text()}, NOW).names() == {"auth_token", "ct0"}


def test_the_status_command_reads_the_environment_offline_and_prints_no_value(monkeypatch, capsys):
    for spec in S.PLATFORMS.values():
        monkeypatch.delenv(spec["secret"], raising=False)
    monkeypatch.setenv("INSTAGRAM_SESSION_B64", b64(good_instagram(expires="2026-10-01")))
    monkeypatch.setenv("X_SESSION_B64", "garbage")
    assert S.main(["status"]) == 0
    out = capsys.readouterr().out
    lines = {ln.split()[0]: ln for ln in out.splitlines()}
    assert "degraded" in lines["Instagram"] and "expired on 2026-10-01" in lines["Instagram"] and "down" in lines["X"] and "unknown" in lines["TikTok"] and SECRET_VALUE not in out
    assert S.main(["encode", "--in", "/no/such/file"]) == 1 and "Error" in capsys.readouterr().out


# ---- the fallback chain -----------------------------------------------------------------------------------------------------------------------------

def step(name, result, calls):
    def run():
        calls.append(name)
        if isinstance(result, Exception):
            raise result
        return result
    return CH.Step(name, run)


def test_the_first_route_that_returns_items_ends_the_chain_and_each_route_is_tried_once():
    calls = []
    res = CH.run_chain("instagram", [step("primary", K.Failure(K.NEED_SETUP, "OpenCLI is not installed"), calls), step("session", [{"t": 1}, {"t": 2}], calls), step("discovery", [{"t": 9}], calls)])
    assert res.route == "session" and res.rows == [{"t": 1}, {"t": 2}] and calls == ["primary", "session"] and not res.skipped
    assert res.trail == [("primary", K.NEED_SETUP, "OpenCLI is not installed"), ("session", "ok", "2 item(s)")]
    assert "route used: session (2 item(s))" in res.summary() and "primary: NEED_SETUP (OpenCLI is not installed)" in res.summary()


def test_a_working_primary_route_means_the_session_is_never_touched():
    calls = []
    res = CH.run_chain("x", [step("primary", [{"a": 1}], calls), step("session", [{"b": 1}], calls)])
    assert res.route == "primary" and calls == ["primary"]


def test_when_every_route_fails_the_channel_is_skipped_with_the_true_reason_of_each_and_nothing_raises():
    calls = []
    res = CH.run_chain("facebook", [step("primary", K.Failure(K.NEED_SETUP, "OpenCLI is not installed"), calls), step("session", K.Failure(K.NEED_LOGIN, "the stored Facebook session expired on 2026-10-01"), calls),
                                    step("discovery", K.Failure(K.RATE_LIMITED, "slow down"), calls)])
    assert res.skipped and res.route == "" and res.rows == [] and calls == ["primary", "session", "discovery"]
    assert "primary: NEED_SETUP OpenCLI is not installed" in res.reason and "session: NEED_LOGIN the stored Facebook session expired on 2026-10-01" in res.reason and "discovery: RATE_LIMITED slow down" in res.reason


def test_a_route_that_answers_with_nothing_is_a_real_empty_answer_not_a_failure_and_not_a_skip():
    calls = []
    res = CH.run_chain("linkedin", [step("primary", K.Failure(K.NEED_SETUP, "not installed"), calls), step("session", [], calls)])
    assert not res.skipped and res.route == "" and res.rows == [] and "answered normally and found nothing" in res.reason and "earlier routes could not run: 1" in res.reason
    assert CH.run_chain("linkedin", [step("session", [], [])]).reason == "a route answered normally and found nothing that matches"
    res = CH.run_chain("linkedin", [step("session", [], calls), step("discovery", [{"t": 1}], calls)])
    assert res.route == "discovery"                                                                           # an empty answer is not the end while another route may have results


def test_a_route_the_owner_switched_off_is_shown_and_skipped_and_an_empty_chain_says_so():
    calls = []
    off = CH.Step("session", lambda: calls.append("ran") or [{"a": 1}], enabled=False, off_reason="switched off by the owner (session_instagram)")
    res = CH.run_chain("instagram", [step("primary", K.Failure(K.NEED_SETUP, "x"), []), off])
    assert calls == [] and res.skipped and "session: off switched off by the owner (session_instagram)" in res.reason
    assert CH.run_chain("instagram", [CH.Step("session", lambda: [], enabled=False)]).reason == "session: off switched off by the owner"
    empty = CH.run_chain("instagram", [])
    assert empty.skipped and empty.reason == "no route is configured"


def test_an_unexpected_error_in_a_route_is_recorded_by_kind_only_never_by_message():
    def boom():
        raise RuntimeError("cookie header was " + SECRET_VALUE)
    res = CH.run_chain("x", [CH.Step("session", boom)], log=None)
    assert res.skipped and res.trail == [("session", K.UPSTREAM_BROKEN, "unexpected RuntimeError")] and SECRET_VALUE not in res.reason and SECRET_VALUE not in res.summary()


def test_the_chain_logs_which_route_ran_and_why_the_others_did_not():
    lines = []
    CH.run_chain("reddit", [step("primary", K.Failure(K.NEED_SETUP, "no key"), []), step("session", [{"a": 1}], [])], log=lines.append)
    assert lines == ["reddit: route used: session (1 item(s)) | primary: NEED_SETUP (no key); session: ok (1 item(s))"]


# ---- the Gmail code reader --------------------------------------------------------------------------------------------------------------------------

def mail(sender, body, when):
    return (f"From: {sender}\nDate: {when}\nSubject: code\nContent-Type: text/plain\n\n{body}\n").encode()


class FakeBox:
    def __init__(self, messages, login_error=None, arrive_after=0):
        self.messages, self.login_error, self.arrive_after, self.selects, self.loggedout, self.logins = messages, login_error, arrive_after, 0, False, []

    def login(self, address, password):
        self.logins.append((address, password))
        if self.login_error:
            raise self.login_error

    def select(self, name):
        self.selects += 1

    def search(self, charset, criterion):
        visible = self.messages if self.selects > self.arrive_after else []
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(visible)))]

    def fetch(self, number, parts):
        return "OK", [(b"1", self.messages[int(number) - 1])]

    def logout(self):
        self.loggedout = True


def test_the_code_comes_from_the_newest_matching_message_after_the_time_given():
    old = mail("security@instagram.com", "Your code is 111111", "Tue, 10 Nov 2026 08:00:00 +0000")
    wrong = mail("news@shop.example", "Your code is 222222", "Wed, 11 Nov 2026 09:58:00 +0000")
    right = mail("security@instagram.com", "Your code is 333333.", "Wed, 11 Nov 2026 09:59:00 +0000")
    box = FakeBox([old, wrong, right])
    code = OTP.fetch_code("me@gmail.example", "abcd efgh ijkl mnop", sender_hint="instagram.com", since_epoch=epoch("2026-11-11"), imap_factory=lambda: box, sleep=lambda s: None)
    assert code == "333333" and box.loggedout and box.logins == [("me@gmail.example", "abcd efgh ijkl mnop")]
    assert OTP.fetch_code("me@gmail.example", "p", sender_hint="instagram.com", since_epoch=epoch("2026-11-10"), imap_factory=lambda: FakeBox([old]), sleep=lambda s: None) == "111111"
    assert OTP.fetch_code("me@gmail.example", "p", sender_hint="instagram.com", since_epoch=epoch("2026-11-12"), polls=1, imap_factory=lambda: FakeBox([old, right]), sleep=lambda s: None) is None   # too old


def test_the_reader_looks_a_bounded_number_of_times_waits_between_looks_and_gives_up_with_none():
    waits = []
    box = FakeBox([mail("security@instagram.com", "code 444444", "Wed, 11 Nov 2026 09:59:00 +0000")], arrive_after=2)
    assert OTP.fetch_code("a@b.example", "p", polls=4, wait_seconds=7, imap_factory=lambda: box, sleep=waits.append) == "444444" and waits == [7, 7]          # it arrived on the third look
    waits.clear()
    assert OTP.fetch_code("a@b.example", "p", polls=3, wait_seconds=5, imap_factory=lambda: FakeBox([]), sleep=waits.append) is None and waits == [5, 5]


def test_a_failed_mail_login_or_missing_credentials_is_need_setup_and_never_shows_the_account():
    err = failure(OTP.fetch_code, "me@gmail.example", "secret app password", imap_factory=lambda: FakeBox([], login_error=OSError("auth failed for me@gmail.example")))
    assert err.code == K.NEED_SETUP and "app password" in str(err) and "me@gmail.example" not in str(err) and "secret app password" not in str(err)
    assert failure(OTP.fetch_code, "", "p").code == K.NEED_SETUP and failure(OTP.fetch_code, "a@b.example", "").code == K.NEED_SETUP


# ---- nothing secret is committed or printed -----------------------------------------------------------------------------------------------------------

SKIP_DIRS = {".git", "data", "node_modules", "__pycache__", ".pytest_cache", "gateway-src", "ai-work", "external"}


def repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


def test_a_session_file_can_never_be_committed_the_ignore_rules_cover_every_name_the_tool_writes():
    rules = (ROOT / ".gitignore").read_text().splitlines()
    for pattern in ("*_session.json", "*_b64.txt", "session_b64.txt", "*_cookies.txt", "*_cookies.json"):
        assert pattern in rules, pattern
    assert not [p for p in repo_files() if p.name.endswith(("_session.json", "_b64.txt", "_cookies.txt", "_cookies.json"))]


def test_no_workflow_or_source_file_holds_a_session_value_only_references_to_secrets():
    secrets = {spec["secret"] for spec in S.PLATFORMS.values()}
    for path in repo_files():
        if path.suffix not in (".yml", ".yaml", ".py", ".md", ".txt", ".json") or "tests" in path.relative_to(ROOT).parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in secrets:
                if name in line and ":" in line and path.suffix in (".yml", ".yaml"):
                    assert "${{ secrets." + name + " }}" in line, f"{path}:{number}: a session secret is set to something other than a reference"
            assert not (any(name + "=" in line for name in secrets) and len(line) > 400), f"{path}:{number}: a long value follows a session secret name"


# ---- a cookie export from a browser extension is accepted as it is -----------------------------------------------------------------------------------

EXPORT = [{"name": "auth_token", "value": SECRET_VALUE, "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "no_restriction", "hostOnly": False, "session": False,
           "expirationDate": epoch("2027-03-01"), "storeId": "0"},
          {"name": "ct0", "value": SECRET_VALUE, "domain": ".x.com", "path": "/", "secure": True, "sameSite": "lax", "session": False, "expirationDate": epoch("2027-03-01")},
          {"name": "gt", "value": "1", "domain": ".x.com", "path": "/", "session": True, "sameSite": "unspecified"}]


@pytest.mark.parametrize("how", ["raw", "base64", "wrapped"])
def test_the_json_a_cookie_export_extension_makes_works_as_the_secret_pasted_raw_or_as_base64(how):
    value = {"raw": json.dumps(EXPORT), "base64": base64.b64encode(json.dumps(EXPORT).encode()).decode(), "wrapped": json.dumps({"cookies": EXPORT})}[how]
    s = S.load("x", {"X_SESSION_B64": "\n  " + value + "\n"}, NOW)
    assert s.names() == {"auth_token", "ct0", "gt"} and s.expires_on() == "2027-03-01" and s.days_left() == 109
    cookies = {c["name"]: c for c in s.playwright_cookies()}
    assert cookies["auth_token"]["expires"] == epoch("2027-03-01") and cookies["auth_token"]["sameSite"] == "None" and cookies["ct0"]["sameSite"] == "Lax"
    assert cookies["gt"]["expires"] == -1 and "sameSite" not in cookies["gt"] and "expirationDate" not in cookies["auth_token"] and "hostOnly" not in cookies["auth_token"]


def test_an_expired_cookie_export_is_reported_with_its_date_and_a_broken_paste_never_shows_what_was_pasted():
    old = [{**EXPORT[0], "expirationDate": epoch("2026-10-05")}, EXPORT[1]]
    assert "expired on 2026-10-05" in str(failure(S.load, "x", {"X_SESSION_B64": json.dumps(old)}, NOW))
    broken = json.dumps(EXPORT)[:-30]                                                                         # a paste cut short
    err = failure(S.load, "x", {"X_SESSION_B64": broken}, NOW)
    assert err.code == K.NEED_SETUP and SECRET_VALUE not in str(err) and "JSON" in str(err)
    assert S.status("x", {"X_SESSION_B64": json.dumps(EXPORT)}, NOW)["state"] == "healthy"


def test_the_owner_guide_exists_names_every_secret_and_says_what_a_session_cannot_do():
    guide = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    for spec in S.PLATFORMS.values():
        assert spec["secret"] in guide, spec["secret"]
    assert "not keyword search" in guide and "dedicated account" in guide and "log out of all sessions" in guide and "never logs in with a password" in guide
    assert "contentintel.sessions status" in guide and "GEMINI_COOKIES" in guide


def test_stray_symbols_inside_a_pasted_secret_are_refused_not_silently_dropped():
    good = S.encode_session(good_instagram())[0]
    assert S.decode_session(good) == good_instagram()
    for stray in ("!!", "*", "#", "$%", "\u00e9", "<>"):
        damaged = good[:30] + stray + good[30:]
        with pytest.raises(S.SessionProblem) as seen:
            S.decode_session(damaged)
        assert "not valid base64" in str(seen.value) and SECRET_VALUE not in str(seen.value), stray                    # base64's own decoder would quietly skip these and hand back a damaged session


def test_a_decoded_session_larger_than_the_limit_is_refused_even_when_the_pasted_text_itself_was_allowed(monkeypatch):
    monkeypatch.setattr(S, "MAX_ENCODED_CHARS", 10_000_000)                                                      # the earlier, text-size limit is out of the way
    big = {"cookies": [cookie("sessionid", "x" * (S.MAX_DECODED_BYTES + 10))], "origins": []}
    for text in (b64(big), json.dumps(big)):
        with pytest.raises(S.SessionProblem) as seen:
            S.decode_session(text)
        assert "too large to be a session" in str(seen.value)
    ok = {"cookies": [cookie("sessionid", "x" * 1000)], "origins": []}
    assert S.decode_session(b64(ok))["cookies"][0]["value"] == "x" * 1000


def test_when_two_valid_codes_compete_the_newest_wins_and_a_newer_message_from_another_sender_is_never_used():
    earlier = mail("security@instagram.com", "Your code is 555555", "Wed, 11 Nov 2026 09:00:00 +0000")
    later = mail("security@instagram.com", "Your code is 666666", "Wed, 11 Nov 2026 09:30:00 +0000")
    got = lambda *msgs, **kw: OTP.fetch_code("me@gmail.example", "p", since_epoch=epoch("2026-11-11"), polls=1, imap_factory=lambda: FakeBox(list(msgs)), sleep=lambda s: None, **kw)
    assert got(earlier, later, sender_hint="instagram.com") == "666666" and got(later, earlier, sender_hint="instagram.com") == "555555"      # the order in the mailbox, not the code, decides: the last message is the newest
    shop = mail("news@shop.example", "Your code is 888888", "Wed, 11 Nov 2026 09:59:00 +0000")
    assert got(earlier, shop, sender_hint="instagram.com") == "555555"                                         # the newest message is from another sender: it is skipped
    assert got(earlier, shop) == "888888"                                                                       # with no sender named, the newest message counts
    assert got(earlier, shop, sender_hint="INSTAGRAM.com") == "555555"                                          # the sender is matched without regard to case


# ---- Update 54: Reddit's words in the visible text, the either-or expiry, and the page shape in the evidence ---------------------------------------------

def reddit_state(**dates):
    return state(*[cookie(name, domain=".reddit.com", expires=date) for name, date in dates.items()])


def test_reddit_is_logged_in_when_its_visible_text_has_the_logged_in_menu_even_though_its_code_holds_no_marker():
    page = "<html><title>Reddit - The heart of the internet</title><body><shreddit-app>" + "x " * 150 + "</shreddit-app></body></html>"
    assert S.PLATFORMS["reddit"]["logged_in_text"] == ("expand user menu", "open profile menu")
    ok, why = S.check_logged_in("reddit", "https://www.reddit.com/settings/", page, 200, text="Skip to main content Open inbox Expand user menu Open profile menu # Settings Account Profile")
    assert (ok, why) == (True, "logged in")
    assert S.check_logged_in("reddit", "https://www.reddit.com/settings/", page, 200, text="Skip to main content Log In Sign Up Get app")[0] is False       # no logged-in words
    assert S.check_logged_in("reddit", "https://www.reddit.com/settings/", page, 200, text="Open inbox Expand user menu Log in to Reddit")[0] is False           # logged-out wording still wins
    assert S.check_logged_in("reddit", "https://www.reddit.com/login/", page, 200, text="Expand user menu")[0] is False                                         # a login address is never logged in
    assert S.check_logged_in("reddit", "https://www.reddit.com/settings/", page, 200, text=None)[0] is False                                                    # no visible text given: the code is searched, and holds nothing
    ev = S.page_evidence("reddit", "https://www.reddit.com/settings/", page, 200, "Open inbox Expand user menu # Settings")
    assert ev["logged_in_text_found"] == ["expand user menu"] and ev["logged_in_text_missing"] == ["open profile menu"] and ev["logged_in_found"] == []


def test_the_expiry_is_the_earliest_of_the_cookies_that_are_all_needed_and_the_latest_of_the_either_or_ones():
    both = S.load("reddit", env("reddit", reddit_state(reddit_session="2027-05-01", token_v2="2026-11-14")), NOW)
    assert both.expires_on() == "2027-05-01" and both.days_left() == 170                                           # a short token the platform renews does not set the date
    short_only = S.load("reddit", env("reddit", reddit_state(token_v2="2026-11-14")), NOW)
    assert short_only.expires_on() == "2026-11-14" and short_only.days_left() == 2
    undated = S.load("reddit", env("reddit", state(cookie("reddit_session", domain=".reddit.com", expires=-1), cookie("token_v2", domain=".reddit.com", expires="2026-11-14"))), NOW)
    assert undated.expires_on() == "" and undated.days_left() is None                                              # one either-or cookie with no expiry lasts the browser session: no date, never "expired"
    mixed = S.load("facebook", env("facebook", state(cookie("c_user", domain=".facebook.com", expires="2027-01-01"), cookie("xs", domain=".facebook.com", expires="2026-12-01"))), NOW)
    assert mixed.expires_on() == "2026-12-01"                                                                      # cookies that are all needed: the earliest
    gone = S.load("reddit", env("reddit", reddit_state(reddit_session="2027-05-01", token_v2="2026-11-01")), NOW)
    assert not gone.expired() and gone.expires_on() == "2027-05-01"                                                # the short one has expired, the long one has not: the session is not expired
    err = failure(S.load, "reddit", env("reddit", reddit_state(reddit_session="2026-11-01", token_v2="2026-11-02")), NOW)
    assert err.code == "NEED_LOGIN" and "expired on 2026-11-02" in str(err)                                          # both either-or cookies have expired: the session has


def test_the_page_evidence_describes_the_shape_of_a_script_only_page_without_a_secret_in_it():
    html = ('<html lang="en"><head><title>Instagram</title></head><body><div id="react-root"></div><div id="mount_0_0_Zk"></div><noscript>You need to enable JavaScript to run this app.</noscript>'
            + "<script>var t=\"CSRFTOKENVALUE123456\";</script>" * 3 + "</body></html>")
    ev = S.page_evidence("instagram", "https://www.instagram.com/", html, 200, "")
    assert ev["scripts"] == 3 and ev["noscript"] == "You need to enable JavaScript to run this app." and ev["body_ids"] == ["react-root", "mount_0_0_Zk"] and ev["visible_text_available"] is False
    assert "CSRFTOKENVALUE123456" not in json.dumps(ev)                                                              # ids and a no-script note, never what is inside a script
    plain = S.page_evidence("instagram", "https://www.instagram.com/", "<html>nothing</html>", 200, "")
    assert plain["scripts"] == 0 and plain["noscript"] == "" and plain["body_ids"] == []
    assert S.PLATFORMS["instagram"]["adv"] == {"network_idle": True} and all("adv" not in spec for p, spec in S.PLATFORMS.items() if p != "instagram")
