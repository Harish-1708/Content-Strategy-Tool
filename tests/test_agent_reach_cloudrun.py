"""Agent-Reach on Google Cloud Run: the HTTP wrapper, the cookie seeding, the client in the platform, how a call is routed, and what the Dockerfile and the deploy workflow must and must not contain.
Nothing here builds the image or touches Google Cloud: that is the deferred live check (docs/deferred-live-checks.md)."""
import importlib
import json
import re
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from contentintel import agent_reach_catalog as CAT
from contentintel import agent_reach_external as ARX
from contentintel import agent_reach_remote as REMOTE
from contentintel import connections as CONN
from contentintel import contract as K
from contentintel import services as S

from conftest import ROOT

APP = ROOT / "cloudrun" / "agent-reach"
TOKEN = "t" * 32
SEVEN = ("boss", "facebook", "instagram", "linkedin", "reddit", "twitter", "xiaohongshu")


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    """The wrapper exactly as the image holds it: its own files plus the two copied from the platform, in one folder, imported as top-level modules."""
    app = tmp_path / "app"
    app.mkdir()
    for name in ("server.py", "cookies.py"):
        shutil.copy(APP / name, app / name)
    for name in ("agent_reach_catalog.py", "human.py"):
        shutil.copy(ROOT / "contentintel" / name, app / name)
    monkeypatch.syspath_prepend(str(app))
    for name in ("server", "cookies", "agent_reach_catalog", "human"):
        sys.modules.pop(name, None)
    server, cookies = importlib.import_module("server"), importlib.import_module("cookies")
    yield server, cookies
    for name in ("server", "cookies", "agent_reach_catalog", "human"):
        sys.modules.pop(name, None)


class Done:
    def __init__(self, stdout="", stderr=""):
        self.stdout, self.stderr = stdout, stderr


class Runner:
    def __init__(self, reply=None):
        self.calls, self.reply = [], reply

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if self.reply is not None else Done(json.dumps({"ok": True, "items": [{"title": "x"}], "error": None}))


def service(server, runner=None, **kw):
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    svc = server.Service(TOKEN, runner=runner or Runner(), environ={"PATH": "/usr/bin", "HOME": "/home/reach", "AGENT_REACH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": "sk-secret-key-123", "LANG": "C"}, **kw)
    svc.sleeps = sleeps
    return svc


def post(svc, body, token=TOKEN, path="/run"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return svc.handle("POST", path, {"X-Agent-Reach-Token": token} if token is not None else {}, raw)


GOOD = {"channel": "instagram", "action": "profile", "values": {"username": "swoveralls_"}}


# ---- the wrapper: who may call it --------------------------------------------------------------------------------------------------------------------------

def test_the_service_will_not_start_without_a_long_token(wrapper):
    server, _ = wrapper
    for bad in ("", None, "short", "x" * 23):
        with pytest.raises(ValueError, match="at least 24"):
            server.Service(bad)


def test_health_is_open_and_names_only_while_run_needs_the_token(wrapper):
    server, _ = wrapper
    svc = service(server, status=lambda: {"cookies": {"instagram": "seeded", "reddit": "missing"}})
    status, body = svc.handle("GET", "/healthz", {}, b"")
    assert status == 200 and body["ok"] and body["channels"] == list(SEVEN) and body["cookies"]["instagram"] == "seeded" and TOKEN not in json.dumps(body)
    assert post(svc, GOOD, token=None)[0] == 401 and post(svc, GOOD, token="wrong" * 8)[0] == 401 and post(svc, GOOD)[0] == 200
    assert svc.handle("GET", "/run", {}, b"")[0] == 404 and svc.handle("POST", "/healthz", {}, b"")[0] == 404 and svc.handle("GET", "/", {}, b"")[0] == 404
    assert "compare_digest" in open(APP / "server.py", encoding="utf-8").read()                      # the token is compared in constant time


def test_ten_wrong_tokens_lock_every_call_out_for_five_minutes_even_the_right_one(wrapper):
    server, _ = wrapper
    now = [1000.0]
    svc = service(server, clock=lambda: now[0])
    assert [post(svc, GOOD, token="nope" * 8)[0] for _ in range(10)] == [401] * 10
    assert post(svc, GOOD)[0] == 429
    now[0] += 299
    assert post(svc, GOOD)[0] == 429
    now[0] += 2
    assert post(svc, GOOD)[0] == 200 and svc.failed == []


# ---- the wrapper: what it will run ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body, why", [
    ({"channel": "youtube", "action": "search", "values": {"query": "x"}}, "channel must be one of"),
    ({"channel": "instagram", "action": "delete", "values": {}}, "has no 'delete' action"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "x", "cmd": "rm"}}, "may hold only the inputs"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "--exec"}}, "may not start with '-'"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "a\nb"}}, "not usable"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "a" * 301}}, "not usable"),
    ({"channel": "instagram", "action": "profile", "values": {"username": ["a"]}}, "not usable"),
    ({"channel": "instagram", "action": "search", "values": {"query": "x", "limit": "ten"}}, "limit must be a whole number"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "x"}, "timeout": 1}, "timeout must be"),
    ({"channel": "instagram", "action": "profile", "values": {"username": "x"}, "timeout": True}, "timeout must be"),
    ({"channel": "instagram", "action": "profile", "values": "x"}, "may hold only the inputs"),
    (["not", "an", "object"], "must be a JSON object"),
])
def test_a_request_for_anything_but_a_declared_action_with_declared_inputs_is_refused_before_anything_runs(wrapper, body, why):
    server, _ = wrapper
    runner = Runner()
    status, out = post(service(server, runner), body)
    assert status == 400 and out["error"]["code"] == "BAD_INPUT" and why in out["error"]["message"] and runner.calls == []


def test_a_request_that_is_not_json_or_is_too_large_is_refused(wrapper):
    server, _ = wrapper
    runner = Runner()
    svc = service(server, runner)
    assert post(svc, b"{not json")[0] == 400 and post(svc, b"\xff\xfe")[0] == 400 and post(svc, b"x" * 9000)[0] == 413 and runner.calls == []


def test_the_command_is_built_exactly_as_the_platforms_own_runner_builds_it_for_every_declared_action(wrapper):
    server, _ = wrapper
    ext = ARX.ExternalAgentReach(None, binary="agent-reach", environ={}, timeout=90)
    checked = 0
    for channel in SEVEN:
        for action, declared in CAT.DECLARED[channel].items():
            if action not in CAT.CHANNELS[channel]["actions"]:
                continue
            values = {name: ("7" if name == "limit" else f"v-{name}") for name in declared}
            assert server.build_command("agent-reach", channel, action, values, 90) == ext.command(channel, action, values), (channel, action)
            checked += 1
    assert checked >= 14


def test_the_command_gets_a_minimal_environment_and_never_the_token_or_any_other_secret(wrapper):
    server, _ = wrapper
    runner = Runner()
    status, out = post(service(server, runner), GOOD)
    cmd, kw = runner.calls[0]
    assert status == 200 and out == {"ok": True, "items": [{"title": "x"}], "error": None}
    assert cmd == ["agent-reach", "instagram", "profile", "swoveralls_", "--timeout", "90"] and kw["timeout"] == 120
    assert set(kw["env"]) <= set(server.ENV_KEEP) | {"PYTHONDONTWRITEBYTECODE"} and "AGENT_REACH_TOKEN" not in kw["env"] and "ANTHROPIC_API_KEY" not in kw["env"]
    assert TOKEN not in " ".join(cmd) and "sk-secret-key-123" not in json.dumps(kw["env"])


def test_what_agent_reach_answers_is_passed_on_capped_and_every_failure_has_its_own_code(wrapper):
    server, _ = wrapper
    many = Runner(Done(json.dumps({"ok": True, "items": [{"n": i} for i in range(500)]})))
    assert len(post(service(server, many), GOOD)[1]["items"]) == 200
    err = Runner(Done("log line\n" + json.dumps({"ok": False, "error": {"code": "NEED_SETUP", "message": "not logged in"}})))
    assert post(service(server, err), GOOD)[1] == {"ok": False, "items": [], "error": {"code": "NEED_SETUP", "message": "not logged in"}}
    leaky = Runner(Done("", f"Traceback ... token {TOKEN} ..."))
    out = post(service(server, leaky), GOOD)[1]
    assert out["error"]["code"] == "UPSTREAM_BROKEN" and TOKEN not in json.dumps(out) and "[hidden]" in out["error"]["message"]
    assert post(service(server, Runner(FileNotFoundError())), GOOD)[1]["error"]["code"] == "NEED_SETUP"
    import subprocess
    assert post(service(server, Runner(subprocess.TimeoutExpired("c", 1))), GOOD)[1]["error"]["code"] == "TIMEOUT"
    assert post(service(server, Runner(Done("[1, 2]"))), GOOD)[1]["error"]["code"] == "UPSTREAM_BROKEN"
    assert post(service(server, Runner(Done(json.dumps({"ok": False, "error": {"message": f"x {TOKEN}"}})))), GOOD)[1]["error"]["message"] == "x [hidden]"


# ---- the wrapper: one person, one account --------------------------------------------------------------------------------------------------------------

def test_calls_run_one_at_a_time_with_a_gap_that_varies_and_a_longer_break_every_few_runs(wrapper):
    server, _ = wrapper
    import human
    runner = Runner()
    slept = []
    person = human.Human({"break_every_pages": [3, 3], "break_seconds": [30, 90]}, rng=__import__("random").Random(9), sleep=slept.append)
    svc = service(server, runner, human=person, sleep=slept.append)
    svc.sleeps = slept
    for _ in range(7):
        assert post(svc, GOOD)[0] == 200
    gaps = svc.sleeps
    assert len(gaps) == 6 + 2                                                                           # no gap before the first run; a gap before each other run; two breaks (after runs 3 and 6)
    breaks = [g for g in gaps if g >= 30]
    assert len(breaks) == 2 and all(30 <= b <= 90 for b in breaks)
    ordinary = [g for g in gaps if g < 30]
    assert all(3 <= g <= 25 for g in ordinary) and len({round(g, 3) for g in ordinary}) >= 4


def test_two_calls_at_once_are_made_to_wait_for_each_other(wrapper):
    server, _ = wrapper
    inside, worst, lock = [0], [0], threading.Lock()

    def slow(cmd, **kw):
        with lock:
            inside[0] += 1
            worst[0] = max(worst[0], inside[0])
        threading.Event().wait(0.05)
        with lock:
            inside[0] -= 1
        return Done(json.dumps({"ok": True, "items": []}))
    svc = service(server, slow)
    threads = [threading.Thread(target=post, args=(svc, GOOD)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert worst[0] == 1


def test_over_real_http_the_service_answers_and_the_token_is_never_echoed_or_logged(wrapper, capsys):
    server, _ = wrapper
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(service(server)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        health = json.loads(urllib.request.urlopen(base + "/healthz", timeout=10).read())
        assert health["ok"] and health["channels"] == list(SEVEN)
        req = urllib.request.Request(base + "/run", data=json.dumps(GOOD).encode(), headers={"X-Agent-Reach-Token": TOKEN, "Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())["items"] == [{"title": "x"}]
        bad = urllib.request.Request(base + "/run", data=json.dumps(GOOD).encode(), headers={"X-Agent-Reach-Token": "wrong" * 8})
        with pytest.raises(urllib.error.HTTPError) as seen:
            urllib.request.urlopen(bad, timeout=10)
        assert seen.value.code == 401
    finally:
        httpd.shutdown()
    err = capsys.readouterr().err
    assert TOKEN not in err and "wrong" * 8 not in err and "POST /run" in err


# ---- the cookies from Secret Manager --------------------------------------------------------------------------------------------------------------------

EXPORT = [{"name": "sessionid", "value": "SECRET-SESSION-VALUE", "domain": ".instagram.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "no_restriction", "expirationDate": 1893456000.5},
          {"name": "csrftoken", "value": "SECRET-CSRF", "domain": ".instagram.com"}, {"name": "nodomain", "value": "x"}, "junk"]


def test_a_browser_export_or_a_storage_state_becomes_the_cookie_list_the_browser_wants(wrapper):
    _, cookies = wrapper
    out = cookies.parse(json.dumps(EXPORT))
    assert [c["name"] for c in out] == ["sessionid", "csrftoken"]
    assert out[0] == {"name": "sessionid", "value": "SECRET-SESSION-VALUE", "path": "/", "domain": ".instagram.com", "expires": 1893456000.5, "httpOnly": True, "secure": True, "sameSite": "None"}
    assert cookies.parse(json.dumps({"cookies": EXPORT[:1]}))[0]["name"] == "sessionid" and cookies.parse(json.dumps([{"name": "a", "value": "b", "url": "https://x.example/"}]))[0]["url"] == "https://x.example/"
    for bad, why in (("not json", "not JSON"), ("[]", "no cookies"), ("{}", "no cookies"), (json.dumps([{"name": "a"}]), "no cookie in the secret")):
        with pytest.raises(ValueError, match=why):
            cookies.parse(bad)


class FakePW:
    def __init__(self, fail=False):
        self.fail, self.added, self.closed, self.profile = fail, [], False, None
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def launch_persistent_context(self, profile, **kw):
        if self.fail:
            raise RuntimeError("chromium missing at /secret/path")
        self.profile = profile
        return self

    def add_cookies(self, cookie_list):
        self.added.append(cookie_list)

    def close(self):
        self.closed = True


def test_cookies_are_seeded_for_the_channels_that_have_a_secret_and_the_state_holds_no_value(wrapper, tmp_path):
    _, cookies = wrapper
    secrets = tmp_path / "secrets"
    (secrets / "instagram").mkdir(parents=True)
    (secrets / "instagram" / "cookies").write_text(json.dumps(EXPORT), encoding="utf-8")
    (secrets / "reddit").mkdir()
    (secrets / "reddit" / "cookies").write_text("garbage", encoding="utf-8")
    (secrets / "twitter").mkdir()
    (secrets / "twitter" / "cookies").write_text(" " * 300_000, encoding="utf-8")
    pw = FakePW()
    state = cookies.seed_all(str(secrets), str(tmp_path / "profile"), SEVEN, playwright_factory=lambda: pw)
    assert state == {"boss": "missing", "facebook": "missing", "instagram": "seeded", "linkedin": "missing", "reddit": "failed: the secret is not JSON", "twitter": "failed: the secret is too large", "xiaohongshu": "missing"}
    assert len(pw.added) == 1 and [c["name"] for c in pw.added[0]] == ["sessionid", "csrftoken"] and pw.closed and pw.profile == str(tmp_path / "profile")
    assert "SECRET-SESSION-VALUE" not in json.dumps(state)


def test_a_browser_that_will_not_start_is_reported_by_kind_only_and_without_any_secret_text(wrapper, tmp_path):
    _, cookies = wrapper
    (tmp_path / "s" / "instagram").mkdir(parents=True)
    (tmp_path / "s" / "instagram" / "cookies").write_text(json.dumps(EXPORT), encoding="utf-8")
    state = cookies.seed_all(str(tmp_path / "s"), str(tmp_path / "p"), SEVEN, playwright_factory=lambda: FakePW(fail=True))
    assert state["instagram"] == "failed: the browser could not be started (RuntimeError)" and "/secret/path" not in json.dumps(state)
    assert cookies.seed_all(str(tmp_path / "none"), str(tmp_path / "p2"), SEVEN, playwright_factory=lambda: pytest.fail("no browser is started when there is nothing to seed")) == {c: "missing" for c in SEVEN}


# ---- the client inside the platform ----------------------------------------------------------------------------------------------------------------------

class Http:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error, self.calls = status, body if body is not None else {"ok": True, "items": [{"a": 1}], "error": None}, error, []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if self.error:
            raise self.error
        return self.status, self.body, {}


def remote(http=None, **kw):
    return REMOTE.RemoteAgentReach(None, url=kw.pop("url", "https://agent-reach-abc.a.run.app/"), token=kw.pop("token", TOKEN), http=http or Http(), **kw)


def test_the_remote_channels_are_exactly_the_seven_opencli_ones():
    assert REMOTE.OPENCLI_CHANNELS == SEVEN


def test_a_remote_call_sends_only_the_declared_inputs_and_returns_the_items():
    http = Http()
    rows = remote(http, id_token="gid-token").call_action("instagram", "search", {"query": "q" * 400, "limit": 5, "username": "ignored", "lang": ""})
    method, url, headers, body, timeout = http.calls[0]
    assert rows == [{"a": 1}] and method == "POST" and url == "https://agent-reach-abc.a.run.app/run" and timeout == 150
    assert body == {"channel": "instagram", "action": "search", "timeout": 90, "values": {"query": "q" * 300, "limit": "5"}}              # undeclared inputs are never sent; a long value is cut
    assert headers["X-Agent-Reach-Token"] == TOKEN and headers["Authorization"] == "Bearer gid-token" and "Authorization" not in remote(Http()).headers()


def test_the_address_must_be_https_and_the_token_long_and_only_opencli_channels_are_called():
    for bad in ("", "http://example.com", "ftp://x", "https://", "agent-reach.example"):
        with pytest.raises(K.Failure) as seen:
            REMOTE.check_url(bad)
        assert seen.value.code == K.NEED_SETUP
    assert REMOTE.check_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    with pytest.raises(K.Failure) as seen:
        remote(token="short")
    assert seen.value.code == K.NEED_SETUP
    for channel, action in (("youtube", "search"), ("github", "search"), ("instagram", "delete")):
        with pytest.raises(K.Failure) as seen:
            remote().call_action(channel, action, {"query": "x"})
        assert seen.value.code == K.BAD_INPUT


@pytest.mark.parametrize("http, code", [
    (Http(status=401, body={}), K.NEED_SETUP), (Http(status=403, body={}), K.NEED_SETUP), (Http(status=429, body={}), K.RATE_LIMITED), (Http(status=500, body={}), K.UPSTREAM_BROKEN),
    (Http(status=200, body="text"), K.UPSTREAM_BROKEN), (Http(status=200, body={"ok": False, "error": {"code": "NEED_SETUP", "message": "not logged in"}}), K.NEED_SETUP),
    (Http(status=200, body={"ok": False, "error": {"code": "MADE_UP", "message": "x"}}), K.UPSTREAM_BROKEN), (Http(error=TimeoutError("timed out")), K.TIMEOUT),
    (Http(error=ConnectionError(f"failed to reach https://x/?token={TOKEN}")), K.UPSTREAM_BROKEN)])
def test_every_way_the_service_can_fail_is_the_platforms_own_failure_with_the_right_code_and_no_secret(http, code):
    with pytest.raises(K.Failure) as seen:
        remote(http).call_action("reddit", "search", {"query": "x"})
    assert seen.value.code == code and TOKEN not in str(seen.value) and "run.app" not in str(seen.value)


def test_the_service_is_used_only_when_both_secrets_are_set():
    assert REMOTE.configured({"AGENT_REACH_URL": "https://x.run.app", "AGENT_REACH_TOKEN": TOKEN}) == ("https://x.run.app", TOKEN)
    for env in ({}, {"AGENT_REACH_URL": "https://x.run.app"}, {"AGENT_REACH_TOKEN": TOKEN}, {"AGENT_REACH_URL": " ", "AGENT_REACH_TOKEN": TOKEN}, None):
        assert REMOTE.configured(env) == ("", "")


# ---- how a call is routed ------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return S.load_settings(None, tmp_path / "data")


ENV = {"AGENT_REACH_URL": "https://agent-reach-abc.a.run.app", "AGENT_REACH_TOKEN": TOKEN}


def client(settings, channel, environ, **kw):
    return S.agent_reach_client(settings, channel, "social_discovery", environ=environ, **kw)


def test_the_seven_opencli_channels_go_to_the_service_when_it_is_set_up_and_nothing_else_changes(settings):
    for channel in SEVEN:
        got = client(settings, channel, ENV)
        assert isinstance(got, ARX.RoutedAgentReach) and isinstance(got.external, REMOTE.RemoteAgentReach), channel
    for channel in ("youtube", "github"):
        assert not isinstance(getattr(client(settings, channel, ENV), "external", None), REMOTE.RemoteAgentReach), channel         # the channels that need no browser stay where they were
    assert not isinstance(getattr(client(settings, "instagram", {}), "external", None), REMOTE.RemoteAgentReach)
    assert not isinstance(getattr(client(settings, "instagram", {"AGENT_REACH_URL": ENV["AGENT_REACH_URL"]}), "external", None), REMOTE.RemoteAgentReach)


def test_the_owners_switches_still_decide(settings, tmp_path):
    off = S.load_settings(None, tmp_path / "d2")
    off["services"]["connections"]["channels"]["agent_reach"] = False
    assert not isinstance(getattr(client(off, "instagram", ENV), "external", None), REMOTE.RemoteAgentReach)                             # the connection is switched off
    builtin = S.load_settings(None, tmp_path / "d3")
    builtin["services"]["connection_routes"]["routes"]["agent_reach"] = "builtin_only"
    assert not isinstance(getattr(client(builtin, "instagram", ENV), "external", None), REMOTE.RemoteAgentReach)                         # the owner chose the platform's own adapter only
    ok, _ = S.agent_reach_allowed(settings, "instagram", "social_discovery")
    assert ok is True
    gone = S.load_settings(None, tmp_path / "d4")
    gone["services"]["agent_reach"]["channels"]["instagram"] = False
    assert S.agent_reach_allowed(gone, "instagram", "social_discovery")[0] is False                                                      # the per-channel switch is checked by the callers, as before


def test_a_service_that_is_down_falls_back_to_the_platforms_own_adapter_and_the_secret_never_reaches_the_local_cli(settings):
    got = client(settings, "instagram", ENV)

    class Builtin:
        needs_browser = True

        def call(self, args):
            return [{"from": "builtin"}]
    got.builtin = Builtin()
    got.external.http = Http(error=ConnectionError("down"))
    assert got.call_action("instagram", "profile", {"username": "x"}, argv=["profile", "x"]) == [{"from": "builtin"}] and got.last_route == "builtin"
    got.external.http = Http()
    assert got.call_action("instagram", "profile", {"username": "x"}, argv=["profile", "x"]) == [{"a": 1}] and got.last_route == "external"
    assert "AGENT_REACH_TOKEN" not in CONN.CONNECTIONS["agent_reach"]["secrets"] and "AGENT_REACH_URL" not in CONN.CONNECTIONS["agent_reach"]["secrets"]


def test_the_workflows_that_use_agent_reach_hand_it_the_two_secrets():
    for name in ("research", "request", "observatory", "acceptance"):
        text = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8")
        assert "AGENT_REACH_URL: ${{ secrets.AGENT_REACH_URL }}" in text and "AGENT_REACH_TOKEN: ${{ secrets.AGENT_REACH_TOKEN }}" in text, name


# ---- the image and the deploy workflow ---------------------------------------------------------------------------------------------------------------

DOCKERFILE = (APP / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_is_pinned_runs_as_a_normal_user_and_holds_no_secret():
    assert re.search(r"^FROM node:22-bookworm-slim$", DOCKERFILE, re.M)
    assert re.search(r"^ARG AGENT_REACH_REF=[0-9a-f]{40}$", DOCKERFILE, re.M) and re.search(r"^ARG PLAYWRIGHT_VERSION=\d+\.\d+\.\d+$", DOCKERFILE, re.M)
    assert "git+https://github.com/Panniantong/Agent-Reach@${AGENT_REACH_REF}" in DOCKERFILE and '"playwright==${PLAYWRIGHT_VERSION}"' in DOCKERFILE
    assert DOCKERFILE.index("USER reach") < DOCKERFILE.index("RUN /app/bootstrap.sh") < DOCKERFILE.index("CMD [")
    assert re.search(r"^USER reach$", DOCKERFILE, re.M) and "useradd --create-home --uid 10001 reach" in DOCKERFILE and 'CMD ["python3", "/app/server.py"]' in DOCKERFILE and "EXPOSE 8080" in DOCKERFILE
    for line in DOCKERFILE.splitlines():
        if re.match(r"^(ENV|ARG)\b", line):
            assert not re.search(r"TOKEN|SECRET|COOKIE|PASSWORD|KEY", line.split("=")[0], re.I), line
    assert not re.search(r"(?i)latest", " ".join(l for l in DOCKERFILE.splitlines() if l.startswith(("FROM", "ARG"))))


def test_the_image_copies_exactly_the_files_the_wrapper_imports_and_the_bootstrap_covers_the_seven_channels():
    copied = set(re.findall(r"^COPY (.+) /app/$", DOCKERFILE, re.M))
    files = {Path.split("/")[-1] for line in copied for Path in line.split()}
    assert files == {"server.py", "cookies.py", "bootstrap.sh", "agent_reach_catalog.py", "human.py"}
    for line in copied:
        for src in line.split():
            assert (ROOT / src).is_file(), src
    boot = (APP / "bootstrap.sh").read_text(encoding="utf-8")
    assert re.search(r"for channel in ([a-z ]+); do", boot).group(1).split() == ["instagram", "reddit", "twitter", "facebook", "linkedin", "xiaohongshu", "boss"] and sorted(re.search(r"for channel in ([a-z ]+); do", boot).group(1).split()) == list(SEVEN)
    assert "agent-reach channels" in boot and "exit 1" in boot and "bootstrap.json" in boot


def test_cloud_build_builds_from_the_repository_root():
    cfg = yaml.safe_load((APP / "cloudbuild.yaml").read_text(encoding="utf-8"))
    assert cfg["steps"][0]["args"] == ["build", "-f", "cloudrun/agent-reach/Dockerfile", "-t", "${_IMAGE}", "."] and cfg["images"] == ["${_IMAGE}"]


WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "agent-reach-cloudrun.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)


def test_the_deploy_workflow_is_manual_one_at_a_time_and_keeps_the_service_small_and_serial():
    inputs = WORKFLOW[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"action", "region", "require_google_sign_in"} and inputs["action"]["default"] == "status" and inputs["require_google_sign_in"]["default"] is False
    assert WORKFLOW["concurrency"] == {"group": "cloudrun-agent-reach", "cancel-in-progress": False} and WORKFLOW["permissions"] == {"contents": "read"} and set(WORKFLOW[True]) == {"workflow_dispatch"}
    for flag in (r"--concurrency 1(?!\d)", r"--max-instances 1(?!\d)", r"--min-instances 0(?!\d)", r"--execution-environment gen2", r"--no-allow-unauthenticated", r"--allow-unauthenticated"):
        assert re.search(flag, WORKFLOW_TEXT), flag                                                         # "1" and not "100": the whole number is checked
    assert WORKFLOW["env"]["CHANNELS"].split() == ["instagram", "reddit", "twitter", "facebook", "linkedin", "xiaohongshu", "boss"]


def test_the_deploy_workflow_keeps_every_secret_in_secret_manager_and_never_prints_one():
    assert 'mounts="AGENT_REACH_TOKEN=agent-reach-token:latest"' in WORKFLOW_TEXT and "/secrets/$channel/cookies=agent-reach-cookies-$channel:latest" in WORKFLOW_TEXT
    assert "gcloud secrets describe \"agent-reach-cookies-$channel\"" in WORKFLOW_TEXT                              # a channel without a secret is not mounted, so the deploy does not fail
    assert "--set-secrets \"$mounts\"" in WORKFLOW_TEXT and "cookies_json" not in WORKFLOW_TEXT.lower()
    for line in WORKFLOW_TEXT.splitlines():
        if re.search(r"echo|tee|printf", line) and "$GITHUB_STEP_SUMMARY" in line or re.search(r"\becho\b", line):
            assert "AGENT_REACH_TOKEN\"" not in line and "$GCP_SA_KEY" not in line and "$current" not in line, line
    secrets_used = set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW_TEXT))
    assert secrets_used == {"GCP_SA_KEY", "GCP_PROJECT_ID", "AGENT_REACH_TOKEN"}


def test_the_documents_say_it_is_built_not_used_and_who_reminds_whom():
    doc = (ROOT / "docs" / "platform" / "agent-reach-cloud-run.md").read_text(encoding="utf-8")
    for phrase in ("built but not used yet", "own Google Cloud Run", "Google Secret Manager in our own account", "Google data-centre addresses", "docs/deferred-live-checks.md", "require_google_sign_in", "AGENT_REACH_URL"):
        assert phrase in doc, phrase
    deferred = (ROOT / "docs" / "deferred-live-checks.md").read_text(encoding="utf-8")
    assert "not dropped" in deferred and "Do you want to run it now?" in deferred and "bootstrap" in deferred
    assert "Deferred is not dropped" in (ROOT / "docs" / "standing-rules.md").read_text(encoding="utf-8")
