"""Keyless providers: OVH's anonymous free tier (an OpenAI-style endpoint called with NO key) and AI Horde (asynchronous, the public anonymous key)."""
import copy
from pathlib import Path

import pytest
import yaml

from contentintel import contract
from contentintel.ai import clients as cl
from contentintel.ai import commands as aic
from contentintel.ai import lanes as L
from contentintel.ai import registry as R

OVH = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
HORDE = "https://aihorde.net/api/v2"


class Http:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        status, answer = self.answers.pop(0)
        return status, answer, {}


def test_a_keyless_direct_client_sends_no_authorization_header_and_a_keyed_one_still_does():
    ok = (200, {"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}, "model": "Mistral-Small-3.2-24B-Instruct-2506"})
    http = Http([ok, ok])
    done = cl.DirectClient(OVH, "", http=http).complete({"model": "Mistral-Small-3.2-24B-Instruct-2506"}, "", "hi", 20)
    assert done.text == "OK" and "Authorization" not in http.calls[0][2] and http.calls[0][1] == OVH + "/chat/completions"
    cl.DirectClient(OVH, "secret-token", http=http).complete({"model": "m"}, "", "hi", 20)
    assert http.calls[1][2]["Authorization"] == "Bearer secret-token"


def test_the_keyless_providers_are_built_without_any_secret_and_never_reported_as_missing_a_credential():
    reg = R.load_registry("ai_providers.yaml")
    clients, notes = {}, []
    import os
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.endswith("_API_KEY")}
    try:
        built, _side = aic.live_clients(reg, {}, "", "work", notes)
    finally:
        os.environ.update(saved)
    assert isinstance(built["ovh"], cl.DirectClient) and not isinstance(built["ovh"], cl.HordeClient) and built["ovh"].token == ""
    assert isinstance(built["aihorde"], cl.HordeClient)
    assert not any(n.startswith("SKIPPED: ovh") or n.startswith("SKIPPED: aihorde") for n in notes) and clients == {}


def test_ai_horde_submits_polls_until_a_worker_has_answered_and_returns_the_text_with_an_estimated_count():
    http = Http([(202, {"id": "job-1"}), (200, {"done": False, "queue_position": 3}), (200, {"done": False, "processing": 1}),
                 (200, {"done": True, "generations": [{"text": "Hello, my friend!", "model": "koboldcpp/Rocinante-X-12B-v1"}]})])
    sleeps, ticks = [], iter(range(0, 1000, 2))
    client = cl.HordeClient(HORDE, http=http, clock=lambda: next(ticks), sleep=sleeps.append)
    done = client.complete({"provider": "aihorde", "model": "aihorde/any"}, "Be brief.", "Say hello", 100, timeout=60)
    assert done.text == "Hello, my friend!" and done.estimated and done.model == "koboldcpp/Rocinante-X-12B-v1" and sleeps == [3, 3]
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", HORDE + "/generate/text/async") and headers["apikey"] == "0000000000" and "Authorization" not in headers and "Client-Agent" in headers
    assert body["prompt"] == "Be brief.\n\nSay hello" and "models" not in body and body["params"]["max_length"] == 100        # aihorde/any: any worker may answer
    assert [c[0:2] for c in http.calls[1:]] == [("GET", HORDE + "/generate/text/status/job-1")] * 3
    named = Http([(202, {"id": "j"}), (200, {"done": True, "generations": [{"text": "x", "model": "m"}]})])
    cl.HordeClient(HORDE, http=named, clock=lambda: 0, sleep=lambda s: None).complete({"model": "aihorde/koboldcpp/Rocinante-X-12B-v1"}, "", "p", 20)
    assert named.calls[0][3]["models"] == ["koboldcpp/Rocinante-X-12B-v1"]                                        # a named model is asked for by name, without the prefix


def test_ai_horde_failures_are_each_named_and_a_job_that_never_finishes_is_cancelled_not_left_queued():
    def run(answers, timeout=30, ticks=None):
        http = Http(answers)
        it = iter(ticks or range(0, 10000, 5))
        client = cl.HordeClient(HORDE, http=http, clock=lambda: next(it), sleep=lambda s: None)
        with pytest.raises(cl.Failure) as err:
            client.complete({"model": "aihorde/any"}, "", "p", 20, timeout=timeout)
        return err.value, http
    e, _ = run([(429, {})])
    assert e.code == contract.RATE_LIMITED
    e, _ = run([(401, {})])
    assert e.code == contract.NEED_SETUP
    e, _ = run([(500, {"message": "boom"})])
    assert e.code == contract.UPSTREAM_BROKEN and "did not accept the job" in str(e)
    e, _ = run([(202, {"id": "j"}), (200, {"faulted": True})])
    assert e.code == contract.UPSTREAM_BROKEN and "faulted" in str(e)
    e, _ = run([(202, {"id": "j"}), (200, {"done": True, "generations": [{"text": "  "}]})])
    assert e.code == contract.UPSTREAM_BROKEN and "no text" in str(e)
    e, http = run([(202, {"id": "j"}), (200, {"done": False}), (200, {"done": False}), (200, {}), (200, {})], timeout=12, ticks=[0, 5, 10, 15, 20, 25, 30, 35, 40])
    assert "no worker answer within 12 seconds (the job was cancelled)" in str(e) and any(c[0] == "DELETE" and c[1].endswith("/generate/text/status/j") for c in http.calls)


def test_the_registry_accepts_only_known_direct_endpoints_kinds_and_a_boolean_keyless_flag(tmp_path):
    base = yaml.safe_load(Path("ai_providers.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "p.yaml"
    for mutate, match in ((lambda d: d["providers"]["ovh"]["direct"].update(base_url="https://evil.example/v1"), "must be one of the endpoints"),
                          (lambda d: d["providers"]["aihorde"]["direct"].update(kind="telepathy"), "kind must be openai or horde"),
                          (lambda d: d["providers"]["ovh"]["direct"].update(keyless="yes"), "keyless true or false"),
                          (lambda d: d["providers"]["ovh"]["direct"].update(extra=1), "")):
        broken = copy.deepcopy(base)
        mutate(broken)
        path.write_text(yaml.safe_dump(broken), encoding="utf-8")
        with pytest.raises(R.RegistryError, match=match or None):
            R.load_registry(str(path))


def test_ovh_and_ai_horde_are_switched_on_with_recorded_terms_approved_models_in_the_pool_and_uncloseai_is_not_wired():
    reg = R.load_registry("ai_providers.yaml")
    pool = L.load_pool("ai_lanes.yaml", reg)
    for pid, kind in (("ovh", "openai"), ("aihorde", "horde")):
        p = reg.providers[pid]
        assert p.enabled and p.direct["keyless"] is True and p.direct.get("kind", "openai") == kind and p.credential_env == () and p.terms["status"] == "upstream_documented" and p.terms["date"] == "2026-11-18"
        approved = {m.id for m in p.models if m.approved}
        assert approved and approved == {r["model"] for r in pool if r["provider"] == pid}, pid
    assert reg.providers["ovh"].limits["requests_per_minute"] == 2                                                # 2 requests a minute per IP per model
    assert "uncloseai" not in reg.providers or not reg.providers["uncloseai"].enabled                              # key registration is closed (probed 2026-11-18)
    data = yaml.safe_load(Path("ai/omniroute_free_providers.yaml").read_text(encoding="utf-8"))["provider_test_2026_11_18"]
    assert data["keyless_probed_live_2026_11_18"]["uncloseai"].startswith("CLOSED") and set(data["switched_off_records_kept"]) == {"sambanova", "ollama", "nara", "mistral"}
    for pid in data["switched_off_records_kept"]:
        assert reg.providers[pid].enabled is False


def test_a_keyless_provider_is_never_sent_a_key_even_when_one_is_lying_in_the_environment(tmp_path):
    from types import SimpleNamespace
    ovh = SimpleNamespace(id="ovh", enabled=True, direct={"base_url": OVH, "keyless": True}, credential_env=("OVH_KEY",), omniroute={})
    keyed = SimpleNamespace(id="keyed", enabled=True, direct={"base_url": "https://openrouter.ai/api/v1"}, credential_env=("OR_KEY",), omniroute={})
    claude = SimpleNamespace(id="claude", enabled=False, direct={}, credential_env=(), omniroute={})
    reg = SimpleNamespace(providers={"ovh": ovh, "keyed": keyed, "claude": claude})
    built, _ = aic.live_clients(reg, {"OVH_KEY": "should-never-be-sent", "OR_KEY": "or-token"}, "", str(tmp_path), [])
    assert built["ovh"].token == "" and built["keyed"].token == "or-token"                                      # the key is used where it is needed and never where it would bill
