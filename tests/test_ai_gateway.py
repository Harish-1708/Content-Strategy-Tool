"""The clients and the gateway launcher, and the provider inventory. MOCK: fake HTTP and a fake process. The one real-gateway test runs only when FREEAPI_GATEWAY_DIR points at a built gateway."""
import json
import os

import pytest

from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import gateway as G
from contentintel.ai import registry as R
from contentintel.contract import Failure

from ai_helpers import PROVIDERS, edited, write
from conftest import ROOT

LOCK = G.load_lock(ROOT / "ai" / "gateway.lock.json")
INVENTORY = json.loads((ROOT / "docs" / "ai" / "provider_inventory.json").read_text(encoding="utf-8"))
ROUTE = {"provider": "alpha", "model": "a-1", "gateway_model": "gw-a-1"}


class Http:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a


def ok_body(platform="alpha", text="hello", model="a-1", usage=(11, 3)):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "model": model, "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}, "_routed_via": {"platform": platform}}


# ---- the gateway client ----------------------------------------------------------------------------------------------------------

def test_a_gateway_call_has_the_right_shape_and_reads_the_real_token_counts():
    http = Http((200, ok_body(), {}))
    done = C.GatewayClient("http://127.0.0.1:3001/", "freellmapi-key", http).complete(ROUTE, "be brief", "hi", 50, expect_platform="alpha")
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"] == "http://127.0.0.1:3001/v1/chat/completions" and call["headers"]["Authorization"] == "Bearer freellmapi-key"
    assert call["body"] == {"model": "gw-a-1", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], "max_tokens": 50, "temperature": 0}
    assert (done.text, done.tokens_in, done.tokens_out, done.model, done.estimated) == ("hello", 11, 3, "a-1", False)
    C.GatewayClient("http://localhost:1", "k", http).complete(ROUTE, "", "hi", 5)
    assert [m["role"] for m in http.calls[-1]["body"]["messages"]] == ["user"]                    # no empty system message is sent


def test_an_answer_from_a_provider_other_than_the_approved_one_is_refused():
    with pytest.raises(Failure, match="routed through 'beta', not the approved 'alpha'") as seen:
        C.GatewayClient("http://127.0.0.1:1", "k", Http((200, ok_body(platform="beta"), {}))).complete(ROUTE, "", "hi", 5, expect_platform="alpha")
    assert seen.value.code == K.BAD_INPUT
    with pytest.raises(Failure, match="None"):
        C.GatewayClient("http://127.0.0.1:1", "k", Http((200, {"choices": [{"message": {"content": "x"}}]}, {}))).complete(ROUTE, "", "hi", 5, expect_platform="alpha")


@pytest.mark.parametrize("status, headers, code", [(401, {}, K.NEED_SETUP), (403, {}, K.NEED_SETUP), (402, {}, K.RATE_LIMITED), (429, {"Retry-After": "30"}, K.RATE_LIMITED),
                                                  (400, {}, K.BAD_INPUT), (500, {}, K.UPSTREAM_BROKEN), (502, {}, K.UPSTREAM_BROKEN), (204, {}, K.UPSTREAM_BROKEN)])
def test_each_gateway_status_gets_the_right_code(status, headers, code):
    with pytest.raises(Failure) as seen:
        C.GatewayClient("http://127.0.0.1:1", "k", Http((status, {"error": {"message": "why"}}, headers))).complete(ROUTE, "", "hi", 5)
    assert seen.value.code == code
    if status == 429:
        assert seen.value.retry_after == 30


@pytest.mark.parametrize("boom, code", [(ConnectionError("refused"), K.UPSTREAM_BROKEN), (TimeoutError("slow"), K.TIMEOUT)])
def test_a_network_failure_is_classified_never_raised_raw(boom, code):
    with pytest.raises(Failure) as seen:
        C.GatewayClient("http://127.0.0.1:1", "k", Http(boom)).complete(ROUTE, "", "hi", 5)
    assert seen.value.code == code and "could not be reached" in str(seen.value)


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"nomessage": 1}]}])
def test_an_answer_with_no_message_is_a_broken_answer(body):
    with pytest.raises(Failure) as seen:
        C.GatewayClient("http://127.0.0.1:1", "k", Http((200, body, {}))).complete(ROUTE, "", "hi", 5)
    assert seen.value.code == K.UPSTREAM_BROKEN


def test_the_gateway_is_only_ever_reached_on_localhost():
    for url in ("https://api.example.com", "http://10.0.0.5:3001", "http://example.com", "http://127.0.0.1.evil.test"):
        if url != "http://127.0.0.1.evil.test":
            with pytest.raises(ValueError, match="localhost"):
                C.GatewayClient(url, "k")
    C.GatewayClient("http://localhost:3001", "k")
    C.GatewayClient("http://[::1]:3001", "k")


# ---- the Anthropic client --------------------------------------------------------------------------------------------------------

def anth_body(text="hi there", usage=(20, 5)):
    return {"content": [{"type": "text", "text": text}, {"type": "tool_use"}], "model": "claude-haiku-5-5", "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}


def test_an_anthropic_call_has_the_right_shape_and_reports_real_tokens():
    http = Http((200, anth_body(), {}))
    done = C.AnthropicClient("sk-ant-test", http).complete({"provider": "claude", "model": "claude-haiku-5-5", "gateway_model": "claude-haiku-5-5"}, "system words", "user words", 100)
    call = http.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages" and call["headers"]["x-api-key"] == "sk-ant-test" and call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["body"] == {"model": "claude-haiku-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "user words"}], "system": "system words"}
    assert (done.text, done.tokens_in, done.tokens_out, done.estimated) == ("hi there", 20, 5, False)
    C.AnthropicClient("k", http).complete({"provider": "claude", "model": "m", "gateway_model": "m"}, "", "u", 5)
    assert "system" not in http.calls[-1]["body"]


@pytest.mark.parametrize("status, data, code", [
    (401, {"error": {"message": "invalid x-api-key"}}, K.NEED_SETUP), (403, {}, K.NEED_SETUP), (402, {}, K.RATE_LIMITED),
    (400, {"error": {"message": "Your credit balance is too low to access the Anthropic API"}}, K.RATE_LIMITED), (400, {"error": {"message": "bad param"}}, K.BAD_INPUT),
    (429, {"error": {"message": "rate"}}, K.RATE_LIMITED), (529, {"error": {"message": "overloaded"}}, K.RATE_LIMITED), (500, {}, K.UPSTREAM_BROKEN)])
def test_each_anthropic_status_gets_the_right_code(status, data, code):
    with pytest.raises(Failure) as seen:
        C.AnthropicClient("k", Http((status, data, {"retry-after": "7"}))).complete({"provider": "claude", "model": "m", "gateway_model": "m"}, "", "u", 5)
    assert seen.value.code == code


def test_anthropic_network_failures_and_empty_answers_are_classified():
    route = {"provider": "claude", "model": "m", "gateway_model": "m"}
    with pytest.raises(Failure) as a:
        C.AnthropicClient("k", Http(TimeoutError("slow"))).complete(route, "", "u", 5)
    with pytest.raises(Failure) as b:
        C.AnthropicClient("k", Http((200, {"nocontent": 1}, {}))).complete(route, "", "u", 5)
    assert (a.value.code, b.value.code) == (K.TIMEOUT, K.UPSTREAM_BROKEN)


def test_the_existing_claude_client_can_stand_in_with_estimated_tokens():
    class Old:
        model = "claude-sonnet-5-5"

        def complete(self, system, user, max_tokens):
            return "an answer"
    done = C.ClaudeAdapter(Old()).complete({"provider": "claude", "model": "m", "gateway_model": "m"}, "sys", "usr", 10)
    assert done.text == "an answer" and done.estimated is True and done.model == "claude-sonnet-5-5" and done.tokens_in >= 1


def test_the_mock_client_records_calls_and_can_script_each_kind_of_answer():
    m = C.MockClient(["one", C.Completion(text="two", tokens_in=1, tokens_out=2), ValueError("boom"), lambda route, prompt, lane: f"{lane}:{prompt}"])
    r = {"provider": "p", "model": "m", "gateway_model": "m"}
    assert m.complete(r, "", "x", 5).text == "one" and m.complete(r, "", "x", 5).tokens_out == 2
    with pytest.raises(ValueError):
        m.complete(r, "", "x", 5)
    assert m.complete(r, "", "x", 5, lane="L").text == "L:x" and len(m.calls) == 4


# ---- the launcher ----------------------------------------------------------------------------------------------------------------

def registry_for(tmp_path, fn=None):
    p, _ = write(tmp_path, providers=edited(PROVIDERS, fn) if fn else None)
    return R.load_registry(p)


ENV = {"FREEAPI_ADMIN_PASSWORD": "long-enough-password", "FREEAPI_ENCRYPTION_KEY": "ab" * 32, "ALPHA_KEY": "alpha-secret", "PATH": "/usr/bin"}
BASELINE = {"alpha": ["a-1", "a-2"], "delta": ["d-1"], "gamma": ["g-1"]}


def test_the_shipped_pin_is_a_full_commit_and_matches_the_inventory():
    assert LOCK["gateway"]["tag"] == "v0.16.17" and LOCK["gateway"]["commit"] == "741d03094398facceb1b6019a838f806e4d9f7f9" == INVENTORY["pinned_gateway"]["commit"]
    assert LOCK["required_env"] == {"HOST": "127.0.0.1", "CATALOG_SYNC_DISABLED": "1", "NODE_ENV": "production"}
    assert set(LOCK["must_stay_unset"]) == {"MODEL_PROBE_ENABLED", "BUDGET_ALERT_WEBHOOK_URL", "ALLOWED_HOSTS"} and LOCK["node"] == "22"


@pytest.mark.parametrize("commit", ["v0.16.17", "741d030", "741d03094398facceb1b6019a838f806e4d9f7fZ", "", "741D03094398FACCEB1B6019A838F806E4D9F7F9"])
def test_a_pin_that_is_not_a_full_lowercase_commit_hash_is_refused(tmp_path, commit):
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"gateway": {"commit": commit}}), encoding="utf-8")
    with pytest.raises(G.GatewayError, match="40-character"):
        G.load_lock(p)


def test_only_enabled_providers_with_credentials_get_a_key_and_only_allowlisted_models_are_on(tmp_path):
    reg = registry_for(tmp_path)
    config, report = G.build_config(reg, ENV, BASELINE)
    assert config["admin"] == {"email": G.ADMIN_EMAIL, "password": "long-enough-password"}
    assert config["keys"] == [{"platform": "alpha", "key": "alpha-secret", "label": "actions", "enabled": True}]
    on = {(m["platform"], m["modelId"]): m["enabled"] for m in config["models"]}
    assert on == {("alpha", "a-1"): True, ("alpha", "a-2"): False, ("delta", "d-1"): False, ("gamma", "g-1"): True}
    assert all(m["fallbackEnabled"] == m["enabled"] for m in config["models"])
    assert report["keys"] == ["alpha"] and report["models_on"] == 2 and report["models_off"] == 2
    assert [s["provider"] for s in report["skipped"]] == ["beta"] and "BETA_KEY" in report["skipped"][0]["reason"]
    assert "claude" not in json.dumps(config) and "license" not in config and "alpha-secret" not in json.dumps(config["models"])


def test_a_model_that_is_not_approved_is_off_even_when_its_provider_is_on(tmp_path):
    reg = registry_for(tmp_path)
    config, _ = G.build_config(reg, ENV, {"alpha": ["a-2", "never-listed"]})
    assert all(m["enabled"] is False for m in config["models"])


def test_cloudflare_style_two_part_credentials_are_joined(tmp_path):
    def add(d):
        d["providers"]["cf"] = {"label": "CF", "enabled": True, "ceiling": "public", "approved_classes": ["public"], "gateway_platform": "cloudflare",
                                "credentials": {"env": ["CF_ACCOUNT", "CF_TOKEN"]}, "terms": {"status": "verified"}, "models": [{"id": "m1", "approved": True}]}
    reg = registry_for(tmp_path, add)
    config, report = G.build_config(reg, {**ENV, "CF_ACCOUNT": "acct", "CF_TOKEN": "tok"}, {"cloudflare": ["m1"]})
    assert {"platform": "cloudflare", "key": "acct:tok", "label": "actions", "enabled": True} in config["keys"]
    config, report = G.build_config(reg, {**ENV, "CF_ACCOUNT": "acct"}, {"cloudflare": ["m1"]})
    assert "cloudflare" not in report["keys"] and any(s["provider"] == "cf" for s in report["skipped"])


@pytest.mark.parametrize("password", ["", "short", None])
def test_without_an_admin_password_the_gateway_is_not_configured(tmp_path, password):
    env = {k: v for k, v in ENV.items() if k != "FREEAPI_ADMIN_PASSWORD"}
    if password is not None:
        env["FREEAPI_ADMIN_PASSWORD"] = password
    with pytest.raises(G.GatewayError, match="FREEAPI_ADMIN_PASSWORD"):
        G.build_config(registry_for(tmp_path), env, BASELINE)


def test_the_gateway_environment_is_localhost_only_with_forbidden_settings_removed(tmp_path):
    env = G.gateway_env(LOCK, {**ENV, "MODEL_PROBE_ENABLED": "1", "BUDGET_ALERT_WEBHOOK_URL": "https://x", "ALLOWED_HOSTS": "*"}, port=4455, db_path=tmp_path / "g.db", config={"a": 1})
    assert env["HOST"] == "127.0.0.1" and env["CATALOG_SYNC_DISABLED"] == "1" and env["NODE_ENV"] == "production" and env["PORT"] == "4455"
    assert env["ENCRYPTION_KEY"] == "ab" * 32 and env["FREEAPI_DB_PATH"] == str(tmp_path / "g.db") and json.loads(env["FREEAPI_CONFIG_JSON"]) == {"a": 1}
    for name in ("MODEL_PROBE_ENABLED", "BUDGET_ALERT_WEBHOOK_URL", "ALLOWED_HOSTS"):
        assert name not in env
    for bad in ("", "zz" * 32, "ab" * 31, "ab" * 33):
        with pytest.raises(G.GatewayError, match="64 hex"):
            G.gateway_env(LOCK, {**ENV, "FREEAPI_ENCRYPTION_KEY": bad}, port=1, db_path="x", config={})


def test_the_setup_code_the_unified_key_and_secrets_are_hidden_from_logs():
    log = "First-run setup code: Y88LWNPLQT\nkey freellmapi-9f8e7d6c5b4a39281706 used\nANTHROPIC_API_KEY=sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV\nok line"
    out = G.redact_log(log)
    for secret in ("Y88LWNPLQT", "freellmapi-9f8e7d6c5b4a", "sk-ant-api03"):
        assert secret not in out
    assert "ok line" in out and "setup code" in out.lower()


class Proc:
    def __init__(self, exit_code=None):
        self.exit_code, self.terminated, self.killed = exit_code, False, False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return self.t


def sidecar(tmp_path, http, proc=None):
    made = {}

    def popen(cmd, **kw):
        made.update(cmd=cmd, **kw)
        return proc or Proc()
    s = G.Sidecar("/gw", LOCK, popen=popen, http=http, sleep=lambda s: None, clock=Clock())
    s.start({"HOST": "127.0.0.1"}, 4455, tmp_path / "g.log")
    return s, made


def test_the_sidecar_starts_the_built_server_waits_for_it_and_stops_it(tmp_path):
    s, made = sidecar(tmp_path, Http(ConnectionError("not yet"), (401, {}, {})))
    assert made["cmd"] == ["node", "server/dist/index.js"] and made["cwd"] == "/gw" and made["env"] == {"HOST": "127.0.0.1"} and s.base_url == "http://127.0.0.1:4455"
    s.wait_ready(timeout=50)
    s.stop()
    assert s.proc.terminated and not s.proc.killed


def test_a_gateway_that_exits_or_never_answers_is_reported_with_a_masked_log(tmp_path):
    s, _ = sidecar(tmp_path, Http(ConnectionError("no")), proc=Proc(exit_code=1))
    (tmp_path / "g.log").write_text("boot\nFirst-run setup code: ABCDEF123456\ncrashed\n", encoding="utf-8")
    with pytest.raises(G.GatewayError, match="exited while starting") as seen:
        s.wait_ready()
    assert "ABCDEF123456" not in str(seen.value) and "crashed" in str(seen.value)
    s2, _ = sidecar(tmp_path, Http(ConnectionError("no")))
    with pytest.raises(G.GatewayError, match="did not become ready"):
        s2.wait_ready(timeout=3)


def test_bootstrap_logs_in_and_reads_the_key_and_fails_clearly(tmp_path):
    http = Http((200, {"token": "tok"}, {}), (200, {"apiKey": "freellmapi-abc"}, {}))
    s, _ = sidecar(tmp_path, http)
    assert s.bootstrap("pw-long-enough") == "freellmapi-abc"
    assert http.calls[0]["url"].endswith("/api/auth/login") and http.calls[0]["body"] == {"email": G.ADMIN_EMAIL, "password": "pw-long-enough"}
    assert http.calls[1]["url"].endswith("/api/settings/api-key") and http.calls[1]["headers"] == {"Authorization": "Bearer tok"}
    with pytest.raises(G.GatewayError, match="admin login failed"):
        sidecar(tmp_path, Http((401, {"error": 1}, {})))[0].bootstrap("x")
    with pytest.raises(G.GatewayError, match="did not give its key"):
        sidecar(tmp_path, Http((200, {"token": "t"}, {}), (500, {}, {})))[0].bootstrap("x")


def test_a_stubborn_gateway_is_killed_and_a_stopped_one_is_left_alone(tmp_path):
    class Stubborn(Proc):
        def wait(self, timeout=None):
            raise TimeoutError("still running")

        def terminate(self):
            self.terminated = True
    s, _ = sidecar(tmp_path, Http((200, {}, {})), proc=Stubborn())
    s.stop()
    assert s.proc.terminated and s.proc.killed
    done, _ = sidecar(tmp_path, Http((200, {}, {})), proc=Proc(exit_code=0))
    done.stop()
    assert not done.proc.terminated


def test_the_admin_model_list_is_read_with_the_login_token(tmp_path):
    http = Http((200, {"token": "t"}, {}), (200, [{"platform": "alpha", "modelId": "a-1", "enabled": True}], {}))
    s, _ = sidecar(tmp_path, http)
    assert s.admin_models("pw") == [{"platform": "alpha", "modelId": "a-1", "enabled": True}] and http.calls[1]["headers"] == {"Authorization": "Bearer t"}


def test_the_baseline_models_come_from_the_inventory_of_the_pinned_gateway():
    base = G.baseline_from_inventory(ROOT / "docs" / "ai" / "provider_inventory.json")
    assert len(base) == 15 and sum(len(v) for v in base.values()) == 108 and "llama-3.3-70b-versatile" in base["groq"]


def test_env_ready_needs_both_secrets():
    assert G.env_ready({"FREEAPI_ENCRYPTION_KEY": "a", "FREEAPI_ADMIN_PASSWORD": "b"}) and not G.env_ready({"FREEAPI_ENCRYPTION_KEY": "a"}) and not G.env_ready({})
    assert isinstance(G.free_port(), int)


# ---- the inventory --------------------------------------------------------------------------------------------------------------

def test_the_inventory_covers_every_platform_of_the_pinned_version_honestly():
    plats = INVENTORY["platforms"]
    assert len(plats) == 84 and INVENTORY["counts"] == {"platform_ids": 84, "with_bundled_models": 15, "bundled_models": 108, "verified": 1, "verified_secondary": 3}
    by = {p["id"]: p for p in plats}
    assert sum(1 for p in plats if "chat" in p["categories"]) == 43 and by["novai"]["in_versions"] == {"v0.13.0": False, "v0.13.10": False, "v0.16.15": False, "v0.16.17": True}
    assert "github" not in by                                                                      # GitHub Models was removed upstream before 0.16
    assert by["mistral"]["verification"]["status"] == "verified" and "help.mistral.ai" in by["mistral"]["verification"]["sources"][0]
    assert "console.groq.com/docs/your-data" in by["groq"]["verification"]["retention"] and "not retained by default" in by["groq"]["verification"]["retention"]
    for pid in ("mistral", "google", "groq", "openrouter"):
        assert by[pid]["verification"]["status"] in ("verified", "verified_secondary") and by[pid]["verification"]["allowed_data_classes"] == ["public"] and by[pid]["verification"]["sources"]
    assert [by[p]["verification"]["status"] for p in ("google", "groq", "openrouter")] == ["verified_secondary"] * 3
    for pid in ("kilo", "ovh", "aihorde"):
        assert by[pid]["verification"]["experimental"] is True and by[pid]["verification"]["anonymous"] is True
    for pid in ("cerebras", "cloudflare"):
        assert by[pid]["verification"]["status"] == "declared_only"
    assert all(p["verification"]["allowed_data_classes"] in (["public"], []) for p in plats)               # nothing in the inventory approves more than Public
    assert by["mistral"]["verification"]["trains_on_data"].startswith("yes") and by["google"]["verification"]["trains_on_data"].startswith("yes")


@pytest.mark.skipif(not os.environ.get("FREEAPI_GATEWAY_DIR"), reason="needs the pinned gateway built (the AI gateway workflow does this)")
def test_the_real_pinned_gateway_passes_the_mock_provider_smoke_test(tmp_path):
    from contentintel.ai import smoke
    reg = R.load_registry(ROOT / "ai_providers.yaml")
    results, report = smoke.run_smoke(os.environ["FREEAPI_GATEWAY_DIR"], reg, LOCK, tmp_path, G.baseline_from_inventory(ROOT / "docs" / "ai" / "provider_inventory.json"))
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, failed
    assert report["models_on"] == 0 and report["models_off"] == 108 and len(results) == 8


@pytest.mark.parametrize("status, body, message", [(204, {"error": {"message": "why"}}, "why"), (204, {}, "the gateway answered 204"), (302, {}, "the gateway answered 302"),
                                                   (500, {"error": {"message": "why"}}, "why"), (503, {}, "the gateway answered 503")])
def test_any_status_other_than_ok_is_a_broken_answer_with_its_own_message(status, body, message):
    with pytest.raises(Failure) as seen:
        C.GatewayClient("http://127.0.0.1:1", "k", Http((status, body, {}))).complete(ROUTE, "", "hi", 5)
    assert seen.value.code == K.UPSTREAM_BROKEN and str(seen.value) == message
