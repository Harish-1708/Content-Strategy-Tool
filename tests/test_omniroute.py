"""OmniRoute as the second gateway: the sidecar, the client, the registry field and the live-client builder. The real OmniRoute is used when CONTENTINTEL_OMNIROUTE_DIR points at an `npm install omniroute` folder."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentintel.ai import clients as cl
from contentintel.ai import commands as aic
from contentintel.ai import omniroute as om
from contentintel.ai import registry as R


class FakeOmniRoute(BaseHTTPRequestHandler):
    """Enough of OmniRoute's local API for the sidecar: health, login (sets a cookie), keys, providers, nodes and chat; it records what it was asked."""
    seen = []
    no_key = False

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeOmniRoute.seen.append(("GET", self.path))
        self._send(200, {"status": "ok"}) if self.path == "/api/health" else self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        FakeOmniRoute.seen.append(("POST", self.path, body, self.headers.get("cookie"), self.headers.get("authorization")))
        if self.path == "/api/auth/login":
            ok = body.get("password") == FakeOmniRoute.password
            return self._send(200 if ok else 401, {"success": ok}, {"set-cookie": "session=abc; Path=/"} if ok else {})
        if "session=abc" not in (self.headers.get("cookie") or "") and self.path.startswith("/api/"):
            return self._send(401, {"error": "login first"})
        if self.path == "/api/keys":
            if FakeOmniRoute.no_key:
                return self._send(201, {"id": "k1"})                                                         # created, but no key came back
            return self._send(201, {"key": "sk-fake-client-key", "id": "k1"})
        if self.path == "/api/providers":
            return self._send(201, {"connection": {"id": "conn-" + body["provider"], "provider": body["provider"]}})
        if self.path == "/api/provider-nodes":
            return self._send(201, {"node": {"id": "openai-compatible-chat-" + body["prefix"]}})
        if self.path == "/v1/chat/completions":
            if self.headers.get("authorization") != "Bearer sk-fake-client-key":
                return self._send(401, {"error": {"message": "bad key"}})
            return self._send(200, {"model": body["model"], "choices": [{"message": {"content": "FROM-" + body["model"]}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        self._send(404, {})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        FakeOmniRoute.seen.append(("PUT", self.path, body, self.headers.get("cookie"), None))
        self._send(200, {"connection": {"id": self.path.rsplit("/", 1)[-1], "isActive": bool(body.get("isActive"))}})


@pytest.fixture
def fake_server():
    FakeOmniRoute.seen, FakeOmniRoute.no_key = [], False
    server = HTTPServer(("127.0.0.1", 0), FakeOmniRoute)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def sidecar_on(server, tmp_path):
    side = om.OmniRouteSidecar(str(tmp_path))
    side.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    FakeOmniRoute.password = side.password
    return side


def test_the_sidecar_logs_in_with_its_own_password_creates_one_key_and_registers_each_providers_key(fake_server, tmp_path):
    side = sidecar_on(fake_server, tmp_path)
    side.wait_ready(timeout=5)
    key = side.bootstrap()
    assert key == "sk-fake-client-key"
    assert side.add_provider("sambanova", "sambanova", "sk-real-provider-key") == "conn-sambanova"
    posted = [s for s in FakeOmniRoute.seen if s[0] == "POST"]
    assert [p[1] for p in posted] == ["/api/auth/login", "/api/keys", "/api/providers"]
    put = [s for s in FakeOmniRoute.seen if s[0] == "PUT"]
    assert len(put) == 1 and put[0][1] == "/api/providers/conn-sambanova" and put[0][2] == {"isActive": True}          # a new connection starts inactive: it is switched on
    assert posted[1][3] and "session=abc" in posted[1][3] and posted[2][2] == {"provider": "sambanova", "name": "sambanova", "apiKey": "sk-real-provider-key", "isActive": True}      # the login cookie is carried
    assert len(side.password) >= 30                                                                          # made up per run, long, never stored


def test_a_wrong_password_a_missing_install_and_a_server_that_never_starts_are_said_in_words(fake_server, tmp_path):
    side = sidecar_on(fake_server, tmp_path)
    side.password = "not-the-right-one"
    with pytest.raises(om.OmniRouteError, match="refused the login"):
        side.bootstrap()
    with pytest.raises(om.OmniRouteError, match="is not installed at"):
        om.OmniRouteSidecar(str(tmp_path / "nowhere")).start(20199, tmp_path / "d", tmp_path / "l.log")
    dead = om.OmniRouteSidecar(str(tmp_path))
    dead.base_url = "http://127.0.0.1:9"
    ticks = iter(range(100))
    dead.clock, dead.sleep = lambda: next(ticks), lambda s: None
    with pytest.raises(om.OmniRouteError, match="did not answer"):
        dead.wait_ready(timeout=3)
    stopped = om.OmniRouteSidecar(str(tmp_path))
    stopped.base_url, stopped.proc = "http://127.0.0.1:9", SimpleNamespace(poll=lambda: 1, returncode=1)
    with pytest.raises(om.OmniRouteError, match="stopped while starting"):
        stopped.wait_ready(timeout=3)


def test_a_key_that_never_comes_back_is_a_start_failure_and_a_started_failure_is_reported_as_one_not_swallowed(fake_server, tmp_path):
    side = sidecar_on(fake_server, tmp_path)
    FakeOmniRoute.no_key = True
    with pytest.raises(om.OmniRouteError, match="did not create a client key"):
        side.bootstrap()


def test_the_client_sends_the_routes_own_model_id_with_the_clients_key_and_only_to_localhost(fake_server, tmp_path):
    side = sidecar_on(fake_server, tmp_path)
    client = om.OmniRouteClient(side.base_url, side.bootstrap())
    done = client.complete({"model": "sambanova/Llama-3.3-70B", "gateway_model": "ignored"}, "sys", "hello", 100)
    assert done.text == "FROM-sambanova/Llama-3.3-70B" and (done.tokens_in, done.tokens_out) == (3, 2)
    with pytest.raises(ValueError):
        om.OmniRouteClient("https://example.com", "k")                                                       # never a public address
    bad = om.OmniRouteClient(side.base_url, "wrong")
    with pytest.raises(cl.Failure):
        bad.complete({"model": "x/y"}, "", "p", 10)


def test_omniroute_providers_load_from_the_registry_and_a_malformed_omniroute_entry_is_refused(tmp_path):
    import copy
    import yaml
    base = yaml.safe_load(Path("ai_providers.yaml").read_text(encoding="utf-8"))
    base["providers"]["sambanova"]["omniroute"] = {"provider": "sambanova"}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    reg = R.load_registry(str(path))
    assert reg.providers["sambanova"].omniroute == {"provider": "sambanova"} and reg.providers["claude"].omniroute == {}
    for bad, match in (({"provider": ""}, "needs the OmniRoute provider id"), ({"provider": "sambanova", "extra": 1}, None)):
        broken = copy.deepcopy(base)
        broken["providers"]["sambanova"]["omniroute"] = bad
        path.write_text(yaml.safe_dump(broken), encoding="utf-8")
        with pytest.raises(R.RegistryError, match=match):
            R.load_registry(str(path))
    both = copy.deepcopy(base)
    both["providers"]["groq"]["omniroute"] = {"provider": "groq"}                                         # groq is direct or gateway-held in the shipped file: a direct one cannot also be OmniRoute's
    both["providers"]["openrouter"]["omniroute"] = {"provider": "openrouter"}
    path.write_text(yaml.safe_dump(both), encoding="utf-8")
    if both["providers"]["openrouter"].get("direct"):
        with pytest.raises(R.RegistryError, match="not both"):
            R.load_registry(str(path))


def provider(pid, env, enabled=True):
    return SimpleNamespace(id=pid, enabled=enabled, omniroute={"provider": pid}, credential_env=(env,))


def test_the_live_builder_starts_omniroute_only_for_enabled_providers_with_a_credential_and_says_why_it_does_not(monkeypatch, tmp_path):
    registry = SimpleNamespace(providers={"sambanova": provider("sambanova", "SAMBANOVA_API_KEY"), "llm7": provider("llm7", "LLM7_API_KEY"), "off": provider("off", "OFF_KEY", enabled=False)})
    clients, notes = {}, []
    assert aic._start_omniroute(registry, {}, clients, tmp_path, notes) is None and clients == {}
    assert sum(n.startswith("SKIPPED: ") for n in notes) == 2 and not any("off" in n for n in notes)       # a disabled one is not mentioned; the two without keys are
    notes.clear()
    assert aic._start_omniroute(registry, {"SAMBANOVA_API_KEY": "k"}, clients, tmp_path, notes) is None
    assert any(n.startswith("APPLICATION: OmniRoute is not installed at (CONTENTINTEL_OMNIROUTE_DIR is not set)") and "sambanova" in n for n in notes)          # an APPLICATION problem, not a quiet note

    class FakeSide:
        added, stopped = [], False

        def __init__(self, d):
            self.base_url = "http://127.0.0.1:20999"

        def start(self, *a):
            return self

        def wait_ready(self):
            return self

        def bootstrap(self):
            return "sk-client"

        def add_provider(self, pid, name, key):
            FakeSide.added.append((pid, name, key))

        def stop(self):
            FakeSide.stopped = True
    monkeypatch.setattr(om, "OmniRouteSidecar", FakeSide)
    monkeypatch.setattr(om, "entry_point", lambda d: Path(d, "x"))
    notes.clear()
    side = aic._start_omniroute(registry, {"SAMBANOVA_API_KEY": "key-a", "CONTENTINTEL_OMNIROUTE_DIR": "/opt/omni"}, clients, tmp_path, notes)
    assert isinstance(side, FakeSide) and FakeSide.added == [("sambanova", "sambanova", "key-a")] and set(clients) == {"sambanova"} and isinstance(clients["sambanova"], om.OmniRouteClient)
    assert any(n.startswith("SKIPPED: llm7") for n in notes)                                                  # the one without a key is named

    class Broken(FakeSide):
        def wait_ready(self):
            raise om.OmniRouteError("did not answer on http://127.0.0.1:20999 within 90 seconds")
    monkeypatch.setattr(om, "OmniRouteSidecar", Broken)
    FakeSide.stopped, notes_b, clients_b = False, [], {}
    assert aic._start_omniroute(registry, {"SAMBANOVA_API_KEY": "k", "CONTENTINTEL_OMNIROUTE_DIR": "/opt/omni"}, clients_b, tmp_path, notes_b) is None and FakeSide.stopped and clients_b == {}
    assert any("OmniRoute could not be started: did not answer" in n for n in notes_b)                        # a failed start is a note, the run goes on without it


def test_several_gateways_are_stopped_together_and_one_that_will_not_stop_does_not_keep_the_others_running():
    stopped = []

    class A:
        def stop(self):
            stopped.append("a")
            raise RuntimeError("stuck")

    class B:
        def stop(self):
            stopped.append("b")
    both = om.Sidecars(A(), None, B())
    both.stop()
    assert stopped == ["a", "b"] and both.sides[0].__class__ is A


@pytest.mark.skipif(not om.entry_point(os.environ.get("CONTENTINTEL_OMNIROUTE_DIR", "")), reason="needs a real `npm install omniroute` folder in CONTENTINTEL_OMNIROUTE_DIR (the live proof of the wiring)")
def test_real_omniroute_boots_headless_hands_out_a_key_and_routes_a_chat_to_an_upstream_with_that_upstreams_own_key(tmp_path):
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            seen.append((self.path, body.get("model"), self.headers.get("authorization")))
            raw = json.dumps({"model": body.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": "UPSTREAM-OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    upstream = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    side = om.OmniRouteSidecar(os.environ["CONTENTINTEL_OMNIROUTE_DIR"])
    try:
        side.start(aic.gw.free_port(), tmp_path / "data", tmp_path / "omniroute.log").wait_ready()
        key = side.bootstrap()
        node = side.add_custom_node("standin", "standin", f"http://127.0.0.1:{upstream.server_address[1]}/v1")
        side.add_provider(node, "standin", "sk-upstream-own-key")
        done = om.OmniRouteClient(side.base_url, key).complete({"model": "standin/stand-in-model"}, "system", "hello", 50)
    finally:
        side.stop()
        upstream.shutdown()
        upstream.server_close()
    assert done.text == "UPSTREAM-OK" and seen == [("/v1/chat/completions", "stand-in-model", "Bearer sk-upstream-own-key")]          # the upstream got ITS key, not ours


def test_omniroute_is_installed_at_one_exact_pinned_version_by_the_shared_action():
    import re
    text = Path(".github/actions/ai-pool/action.yml").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+", om.PINNED_VERSION) and 'npm install "omniroute@${version}"' in text and "PINNED_VERSION" in text          # an exact version, read from the code: never `latest`, never a range
    assert "omniroute-${{ runner.os }}-${{ hashFiles('contentintel/ai/omniroute.py') }}" in text                                          # the cache is keyed on the pin: changing it reinstalls


# ---- the 401 that recurred in three GitHub runs: the login is asked again, the start is repeated on fresh folders, and the log is shown ---------------------------------------------------------------------
def _sidecar(tmp_path, requests, popen=None):
    sleeps = []
    popens = []

    def fake_popen(cmd, **kw):
        popens.append((cmd, kw))
        return type("P", (), {"poll": lambda self: None, "terminate": lambda self: None, "wait": lambda self, timeout=None: 0, "kill": lambda self: None, "returncode": 0})()
    side = om.OmniRouteSidecar(str(tmp_path / "src"), popen=popen or fake_popen, sleep=sleeps.append)
    seen = []

    def fake_request(method, path, body=None, headers=None, timeout=20):
        seen.append((method, path, body))
        return requests.pop(0)
    side._request = fake_request
    return side, sleeps, popens, seen


def test_a_refused_login_is_asked_again_and_the_third_answer_is_taken(tmp_path):
    side, sleeps, _p, seen = _sidecar(tmp_path, [(401, {"error": "no"}), (401, {"error": "no"}), (200, {"success": True}), (201, {"key": "sk-omni-1"})])
    assert side.bootstrap() == "sk-omni-1" and sleeps == [om.LOGIN_WAIT, om.LOGIN_WAIT] and [s[1] for s in seen] == ["/api/auth/login"] * 3 + ["/api/keys"]


def test_a_login_that_stays_refused_says_what_omniroute_said_how_often_it_was_tried_and_never_the_password(tmp_path):
    side, sleeps, _p, _s = _sidecar(tmp_path, [(401, {"error": f"bad password {'x' * 4}"})] * om.LOGIN_ATTEMPTS)
    side.password = "PW-SECRET-123"
    with pytest.raises(om.OmniRouteError) as err:
        side.bootstrap()
    text = str(err.value)
    assert f"HTTP 401, {om.LOGIN_ATTEMPTS} attempts" in text and "bad password" in text and "PW-SECRET-123" not in text and len(sleeps) == om.LOGIN_ATTEMPTS - 1


def test_the_start_gets_its_own_home_and_working_folder_and_a_restart_gets_a_new_password_and_new_folders(tmp_path):
    side, _sl, popens, _seen = _sidecar(tmp_path, [(200, {"status": "ok"})] * 3)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "node_modules" / "omniroute" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "node_modules" / "omniroute" / "bin" / "omniroute.mjs").write_text("", encoding="utf-8")
    side.start(20123, tmp_path / "w" / "omniroute-data", tmp_path / "w" / "omniroute.log")
    first = popens[0][1]
    assert first["cwd"] == str(tmp_path / "w" / "omniroute-data-cwd") and first["env"]["HOME"] == str(tmp_path / "w" / "omniroute-data-home") and first["env"]["DATA_DIR"] == str(tmp_path / "w" / "omniroute-data")
    assert Path(first["cwd"]).is_dir() and Path(first["env"]["HOME"]).is_dir()
    old_password = side.password
    side.restart_fresh()
    second = popens[1][1]
    assert side.password != old_password and second["env"]["INITIAL_PASSWORD"] == side.password and second["env"]["DATA_DIR"].endswith("omniroute-data-retry1") and second["env"]["DATA_DIR"] != first["env"]["DATA_DIR"]


def test_the_log_tail_hides_the_password_and_key_looking_text(tmp_path):
    side, _s, _p, _q = _sidecar(tmp_path, [])
    side.password = "PW-SECRET-123"
    side.log_path = tmp_path / "o.log"
    side.log_path.write_text("starting\nlogin attempt password=PW-SECRET-123 failed\nthe value PW-SECRET-123 was refused by the form\napiKey: abcdef123456 rejected\nready on 20123\n", encoding="utf-8")
    tail = side.log_tail()
    assert "PW-SECRET-123" not in tail and "abcdef123456" not in tail and "starting" in tail and "ready on 20123" in tail


def test_a_refused_login_makes_the_tool_start_omniroute_again_once_and_a_second_refusal_prints_the_log(tmp_path, monkeypatch):
    from contentintel.ai import commands as aic
    made = []

    class Side:
        def __init__(self, d):
            made.append(self)
            self.base_url, self.restarts, self.logins = "http://127.0.0.1:20999", 0, 0

        def start(self, *a):
            return self

        def wait_ready(self):
            return self

        def bootstrap(self):
            self.logins += 1
            if self.logins <= made_refusals[0]:
                raise om.OmniRouteError("OmniRoute refused the login it was started with (HTTP 401, 4 attempts)")
            return "sk"

        def restart_fresh(self):
            self.restarts += 1
            return self

        def add_provider(self, *a):
            return None

        def log_tail(self):
            return "boot ok | login failed"

        def stop(self):
            pass
    monkeypatch.setattr(om, "OmniRouteSidecar", Side)
    monkeypatch.setattr(om, "entry_point", lambda d: Path(d, "x"))
    registry = type("R", (), {"providers": {"a": type("P", (), {"id": "a", "enabled": True, "omniroute": {"provider": "a"}, "credential_env": ("A_KEY",)})()}})()
    made_refusals = [1]
    clients, notes = {}, []
    assert isinstance(aic._start_omniroute(registry, {"A_KEY": "1", "CONTENTINTEL_OMNIROUTE_DIR": "/o"}, clients, tmp_path, notes), Side) and made[0].restarts == 1 and set(clients) == {"a"}
    assert any(n.startswith("NOTE: OmniRoute refused the login") and "again with a new password" in n for n in notes)
    made_refusals[0] = 5
    clients2, notes2 = {}, []
    assert aic._start_omniroute(registry, {"A_KEY": "1", "CONTENTINTEL_OMNIROUTE_DIR": "/o"}, clients2, tmp_path, notes2) is None
    assert any("could not be started" in n and "its log: boot ok | login failed" in n for n in notes2) and clients2 == {}


def test_the_action_clears_any_database_a_cached_install_carries():
    import yaml as _y
    steps = _y.safe_load(Path(".github/actions/ai-pool/action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
    clear = next(s for s in steps if "Clear any state" in s.get("name", ""))
    assert "cache-hit == 'true'" in clear["if"] and "storage.sqlite" in clear["run"] and "omniroute-src/data" in clear["run"]
    names = [s.get("name", "") for s in steps]
    assert names.index(clear["name"]) > names.index("Restore OmniRoute") and clear.get("continue-on-error") is True
