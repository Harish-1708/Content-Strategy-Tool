"""The provider connection test (ai connect) and the OmniRoute provider mappings."""
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from contentintel import cli
from contentintel.ai import commands as aic
from contentintel.ai import connect as cn
from contentintel.ai import lanes as L
from contentintel.ai import omniroute as om
from contentintel.ai import registry as R

AT = "2026-11-17T12:00:00Z"
OMNI = {"sambanova": "sambanova", "nara": "nara", "xkiro": "xkiro", "llm7": "llm7", "pollinations": "pollinations", "airforce": "api-airforce", "bluesminds": "bluesminds", "bazaarlink": "bazaarlink"}


class FakeRouter:
    def __init__(self, behaviour):
        self.pool = [{"provider": "zeta", "model": "z1"}]
        self.behaviour, self.calls = behaviour, []

    def run_route(self, lane, route, *, instructions, now, ignore_rests=False, system="", context=()):
        self.calls.append((route["provider"], route["model"]))
        return self.behaviour(route)


def lane(name, chain):
    return SimpleNamespace(name=name, chain=tuple(chain))


def test_the_report_says_who_connected_who_failed_and_why_and_who_was_not_tried_and_never_tries_claude():
    def behaviour(route):
        p, m = route["provider"], route["model"]
        if p == "alpha":
            return {"status": "ok", "completion": SimpleNamespace(latency_ms=300 if m == "a1" else 500)}
        if p == "beta":
            return {"status": "failed", "reason": "HTTP 401: invalid key"}
        if p == "gamma":
            return {"status": "ok", "completion": SimpleNamespace(latency_ms=100)} if m == "g1" else {"status": "failed", "reason": "model not found"}
        return {"status": "skipped", "reason": "no credential in the environment"}
    lanes = {"one": lane("one", [{"provider": "alpha", "model": "a1"}, {"provider": "alpha", "model": "a2"}, {"provider": "beta", "model": "b1"}, {"provider": "claude", "model": "c"},
                                  {"provider": "gamma", "model": "g1"}, {"provider": "gamma", "model": "g2"}]),
             "canary": lane("canary", [{"provider": "alpha", "model": "never"}])}
    registry = SimpleNamespace(providers={p: SimpleNamespace(label=p.title()) for p in ("claude", "alpha", "beta", "gamma", "zeta", "delta")})
    router = FakeRouter(behaviour)
    report = cn.run_connect(router, registry, lanes, ["SKIPPED: sambanova: no credential in the environment (SAMBANOVA_API_KEY), so it is not tried; add the secret to use it"], AT)
    assert ("claude", "c") not in router.calls and ("alpha", "never") not in router.calls and len(router.calls) == len(set(router.calls)) == 6        # Claude and the canary lane are never tried; each route once
    text = cn.format_report(report)
    assert text.splitlines()[0] == "Provider connection test (the repository's Claude key is not used): 2 of 5 providers connected, 1 failed, 2 not tried"
    assert "CONNECTED  alpha: 2 of 2 route(s) answered, median 400 ms" in text and "CONNECTED  gamma: 1 of 2 route(s) answered, median 100 ms; failed: g2: model not found" in text
    assert "FAILED     beta: HTTP 401: invalid key (1 route(s) tried)" in text and "NOT TRIED  zeta: no credential in the environment" in text and "NOT TRIED  delta: no approved model in any lane or the shared pool" in text
    assert "SKIPPED: sambanova" in text


def test_a_provider_the_owner_has_not_switched_on_is_tried_in_the_test_but_never_claude_and_never_one_with_unchecked_terms():
    import dataclasses
    reg = R.load_registry("ai_providers.yaml")
    off = next(p for p in reg.providers.values() if p.id != "claude" and not p.enabled and p.terms.get("status") not in ("unverified", "declared_only") and not p.retired) if any(
        (not p.enabled and p.id != "claude" and p.terms.get("status") not in ("unverified", "declared_only") and not p.retired) for p in reg.providers.values()) else None
    reg.providers["claude"] = dataclasses.replace(reg.providers["claude"], enabled=False)
    unchecked = next(p for p in reg.providers.values() if p.terms.get("status") == "unverified" and not p.enabled)
    aic.all_providers_on(reg)
    assert reg.providers["claude"].enabled is False                                                           # Claude is never switched on by the test
    assert reg.providers[unchecked.id].enabled is False                                                       # terms nobody has checked: not even tried
    if off is not None:
        assert reg.providers[off.id].enabled is True


def test_the_connect_command_removes_the_repositorys_claude_key_before_anything_starts(tmp_path, monkeypatch):
    seen = {}

    def fake_live(registry, environ, gateway_dir, workdir, notes):
        seen["has_claude_key"] = "ANTHROPIC_API_KEY" in environ
        seen["other_keys_kept"] = environ.get("GROQ_API_KEY") == "g"
        return {}, None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-repo-key")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(aic, "live_clients", fake_live)
    code = cli.main(["ai", "connect", "--live", "--data-dir", str(tmp_path / "data"), "--workdir", str(tmp_path / "w")])
    assert code == 0 and seen == {"has_claude_key": False, "other_keys_kept": True}


def test_the_provider_test_workflow_never_receives_the_claude_key_and_runs_the_connection_test():
    text = Path(".github/workflows/provider-test.yml").read_text(encoding="utf-8")
    assert "ANTHROPIC" not in text and "anthropic" not in text.lower()
    wf = yaml.safe_load(text)
    assert list(wf[True]) == ["workflow_dispatch"] and wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["providers"]
    assert any(s.get("uses") == "./.github/actions/ai-pool" for s in job["steps"]) and "python -m contentintel ai connect --live" in next(s["run"] for s in job["steps"] if s.get("name") == "Try every provider")
    for key in ("FREEAPI_ENCRYPTION_KEY", "GROQ_API_KEY", "SAMBANOVA_API_KEY", "NARA_API_KEY", "XKIRO_API_KEY", "LLM7_API_KEY", "POLLINATIONS_API_KEY", "SILICONFLOW_API_KEY"):
        assert job["env"][key] == "${{ secrets." + key + " }}"
    reg = R.load_registry("ai_providers.yaml")
    wanted = {n for p in reg.providers.values() if p.id != "claude" and p.enabled for n in p.credential_env}
    assert wanted <= set(job["env"]) | {"ANTHROPIC_API_KEY"} and "ANTHROPIC_API_KEY" not in job["env"]         # every provider's key reaches the test


def test_the_eight_omniroute_providers_are_mapped_with_the_providers_own_ids_recorded_terms_and_prefixed_models_and_siliconflow_is_not():
    reg = R.load_registry("ai_providers.yaml")
    lanes = L.load_lanes("ai_lanes.yaml", reg)
    pool = L.load_pool("ai_lanes.yaml", reg)
    assert lanes
    for pid in ("sambanova", "nara"):                                                                        # mapped, recorded and switched OFF on 2026-11-18 (402 and a rejected key)
        assert reg.providers[pid].omniroute == {"provider": OMNI[pid]} and reg.providers[pid].enabled is False
    for pid, omni_id in {k: v for k, v in OMNI.items() if k not in ("sambanova", "nara")}.items():
        p = reg.providers[pid]
        approved = [m.id for m in p.models if m.approved]
        assert p.omniroute == {"provider": omni_id} and p.enabled and p.terms["status"] == "upstream_documented" and p.terms["date"] == "2026-11-17" and "not verified" in p.terms["trains_on_data"]
        assert approved and all(m.startswith(omni_id + "/") for m in approved) and p.credential_env and set(approved) <= {r["model"] for r in pool if r["provider"] == pid}, pid
        assert set(p.approved_classes) == {"public", "internal"} and p.override["approved_by"] == "Harish"                                 # under the owner's standing decision of 2026-11-03
    sf = reg.providers["siliconflow"]
    assert sf.omniroute == {} and sf.enabled is False                                                          # not in OmniRoute's catalogue: it stays off
    assert not any(p.omniroute for pid, p in reg.providers.items() if pid not in OMNI)                           # nothing else was mapped


def test_one_provider_that_omniroute_will_not_accept_does_not_take_the_others_down(tmp_path, monkeypatch):
    registry = SimpleNamespace(providers={"a": SimpleNamespace(id="a", enabled=True, omniroute={"provider": "a"}, credential_env=("A_KEY",)),
                                          "b": SimpleNamespace(id="b", enabled=True, omniroute={"provider": "bogus-id"}, credential_env=("B_KEY",))})
    stopped = []

    class Side:
        def __init__(self, d):
            self.base_url = "http://127.0.0.1:20999"

        def start(self, *a):
            return self

        def wait_ready(self):
            return self

        def bootstrap(self):
            return "sk"

        def add_provider(self, pid, name, key):
            if pid == "bogus-id":
                raise om.OmniRouteError("did not accept the bogus-id connection (HTTP 400)")

        def stop(self):
            stopped.append(1)
    monkeypatch.setattr(om, "OmniRouteSidecar", Side)
    monkeypatch.setattr(om, "entry_point", lambda d: Path(d, "x"))
    clients, notes = {}, []
    side = aic._start_omniroute(registry, {"A_KEY": "1", "B_KEY": "2", "CONTENTINTEL_OMNIROUTE_DIR": "/o"}, clients, tmp_path, notes)
    assert isinstance(side, Side) and set(clients) == {"a"} and not stopped
    assert any(n.startswith("APPLICATION: OmniRoute did not accept b (provider id bogus-id): did not accept the bogus-id connection") for n in notes)
    clients2, notes2 = {}, []
    only_bad = SimpleNamespace(providers={"b": registry.providers["b"]})
    assert aic._start_omniroute(only_bad, {"B_KEY": "2", "CONTENTINTEL_OMNIROUTE_DIR": "/o"}, clients2, tmp_path, notes2) is None and stopped == [1] and clients2 == {}          # none accepted: it is stopped, not left running


def test_the_connection_test_report_is_json_safe_for_the_summary(tmp_path):
    report = cn.run_connect(FakeRouter(lambda r: {"status": "ok", "completion": SimpleNamespace(latency_ms=5)}), SimpleNamespace(providers={"x": SimpleNamespace(label="X")}),
                            {"one": lane("one", [{"provider": "x", "model": "m"}])}, [], AT)
    assert json.loads(json.dumps(report))["rows"][0]["state"] == "connected"
