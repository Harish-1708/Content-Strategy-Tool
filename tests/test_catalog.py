"""`ai catalog`: each provider's own model list read and compared with ours (read-only), so a withdrawn model, a candidate and a model the pinned gateway cannot route are told apart without guessing."""
import yaml

from contentintel.ai import catalog as CAT
from contentintel.ai import gateway as G
from contentintel.ai import registry as R

from conftest import ROOT

REG = R.load_registry(ROOT / "ai_providers.yaml")
INVENTORY = G.baseline_from_inventory(ROOT / "docs" / "ai" / "provider_inventory.json")
DEAD_KILO = "nvidia/nemotron-3-super-120b-a12b:free"


class Http:
    """A scripted stand-in for the network: {address fragment: (status, body)}; records every call."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers)))
        for fragment, got in self.answers.items():
            if fragment in url:
                if isinstance(got, Exception):
                    raise got
                return got[0], got[1], {}
        return 404, {}, {}


def test_ids_are_compared_without_prefixes_or_cloud_suffixes_and_without_losing_real_tags():
    assert CAT.normalise("models/gemini-3.5-flash") == "gemini-3.5-flash" and CAT.normalise("glm-5.3-flash:cloud") == "glm-5.3-flash"
    assert CAT.normalise("GPT-OSS:120b-cloud") == "gpt-oss:120b" and CAT.normalise("gpt-oss:120b") == "gpt-oss:120b" and CAT.normalise("poolside/laguna-m.1:free") == "poolside/laguna-m.1:free"
    assert CAT.normalise(None) == "" and CAT.normalise("  X  ") == "x"


def test_every_shape_of_model_list_is_read_and_anything_else_is_an_empty_list():
    assert CAT.parse_models({"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}]}) == ["a", "b"]
    assert CAT.parse_models({"models": [{"name": "x"}, {"model": "y"}, {"modelId": "z"}, "w", 5, {}]}) == ["x", "y", "z", "w"]
    assert CAT.parse_models(["p", "q"]) == ["p", "q"]
    for nothing in ({"data": "nope"}, None, "text", 3, {}, {"models": None}):
        assert CAT.parse_models(nothing) == []


def provider(pid):
    return REG.provider(pid)


def test_a_list_is_fetched_with_the_key_in_the_right_place_and_the_key_is_never_returned():
    http = Http({"integrate.api.nvidia.com": (200, {"data": [{"id": "google/gemma-4-31b-it"}]})})
    ids, note = CAT.fetch(provider("nvidia"), {"NVIDIA_API_KEY": "SECRET-1"}, http)
    assert ids == ["google/gemma-4-31b-it"] and note == "" and http.calls[0][2]["Authorization"] == "Bearer SECRET-1" and "SECRET-1" not in http.calls[0][1]
    http = Http({"generativelanguage.googleapis.com": (200, {"models": [{"name": "models/gemini-3.5-flash"}]})})
    ids, note = CAT.fetch(provider("google"), {"GEMINI_API_KEY": "SECRET-2"}, http)
    assert ids == ["models/gemini-3.5-flash"] and "key=SECRET-2" in http.calls[0][1] and "Authorization" not in http.calls[0][2]       # Google takes it in the address: it is never printed
    http = Http({"api.kilo.ai": (200, {"data": [{"id": "poolside/laguna-m.1:free"}]})})
    assert CAT.fetch(provider("kilo"), {}, http)[0] == ["poolside/laguna-m.1:free"] and "Authorization" not in http.calls[0][2]       # a public list needs no key


def test_a_list_that_cannot_be_read_says_why_and_never_shows_the_key():
    assert CAT.fetch(provider("nvidia"), {}, Http({}))[1].startswith("no key in this run (NVIDIA_API_KEY)")
    assert CAT.fetch(provider("nvidia"), {"NVIDIA_API_KEY": "  "}, Http({}))[0] == []
    assert CAT.fetch(provider("nvidia"), {"NVIDIA_API_KEY": "S"}, Http({"nvidia": (401, {})})) == ([], "the list answered HTTP 401")
    assert CAT.fetch(provider("nvidia"), {"NVIDIA_API_KEY": "S"}, Http({"nvidia": (200, {"x": 1})})) == ([], "the answer was not a list of models")
    ids, note = CAT.fetch(provider("google"), {"GEMINI_API_KEY": "SECRET-3"}, Http({"google": ConnectionError("failed for https://x/?key=SECRET-3")}))
    assert ids == [] and note == "the list could not be fetched (ConnectionError)" and "SECRET-3" not in note
    assert CAT.fetch(provider("cloudflare"), {}, Http({})) == ([], "no model-list address is known for this provider")


def test_the_comparison_names_withdrawn_models_candidates_and_models_the_pinned_gateway_cannot_route():
    http = Http({"api.kilo.ai": (200, {"data": [{"id": "poolside/laguna-m.1:free"}, {"id": "stepfun/step-3.7-flash:free"}, {"id": DEAD_KILO}, {"id": "brand-new/model:free"}]}),
                 "ollama.com": (200, {"models": [{"name": "gpt-oss:120b-cloud"}, {"name": "glm-5.3-flash:cloud"}]})})
    rows = {r["id"]: r for r in CAT.run(REG, {"OLLAMA_API_KEY": "k"}, INVENTORY, http)}
    kilo = rows["kilo"]
    assert kilo["ok"] and kilo["listed"] == 4
    assert kilo["approved_missing"] == ["poolside/laguna-xs.2:free"]                                             # approved by us, not in Kilo's own list
    assert kilo["candidates"] == [DEAD_KILO]                                                                      # in the pinned catalogue, listed, not approved (here only because the stand-in lists it)
    assert kilo["unroutable"] == ["brand-new/model:free"]                                                         # listed, but the pinned gateway does not know it
    ollama = rows["ollama"]
    assert ollama["ok"] and "gpt-oss:120b" not in ollama["approved_missing"] and "glm-4.7" in ollama["approved_missing"] and ollama["unroutable"] == ["glm-5.3-flash:cloud"]      # a ':cloud' name matches its plain name
    assert rows["nvidia"]["ok"] is False and rows["nvidia"]["approved_missing"] == []                              # an unread list makes no claim about our models
    assert "claude" not in rows and "github" not in rows


def test_an_empty_list_is_not_a_claim_that_every_model_is_gone():
    rows = {r["id"]: r for r in CAT.run(REG, {}, INVENTORY, Http({"api.kilo.ai": (200, {"data": []})}))}
    assert rows["kilo"]["ok"] is False and rows["kilo"]["approved_missing"] == [] and rows["kilo"]["note"] == "the answer was not a list of models"


def test_the_report_is_plain_truncated_and_never_contains_a_key():
    many = [{"id": f"vendor/model-{i}"} for i in range(20)]
    http = Http({"integrate.api.nvidia.com": (200, {"data": many}), "api.kilo.ai": (200, {"data": [{"id": "poolside/laguna-m.1:free"}]})})
    text = CAT.format_report(CAT.run(REG, {"NVIDIA_API_KEY": "SECRET-9", "GEMINI_API_KEY": "SECRET-8"}, INVENTORY, http))
    assert text.startswith("Provider model lists (READ-ONLY: nothing is changed")
    assert "nvidia      20 listed" in text and "(+12 more)" in text and "need a newer pin" in text and "probably withdrawn" in text and "candidates" in text
    assert "kilo        1 listed" in text and "google      NOT READ: the list answered HTTP 404" in text and "cohere      NOT READ: no key in this run (COHERE_API_KEY)" in text
    assert "SECRET-9" not in text and "SECRET-8" not in text and "2 of 10 provider list(s) read" in text


def test_nothing_is_ever_written_by_the_catalog(tmp_path, monkeypatch):
    before = {p: (ROOT / p).read_bytes() for p in ("ai_providers.yaml", "ai_lanes.yaml")}
    CAT.run(REG, {}, INVENTORY, Http({"api.kilo.ai": (200, {"data": [{"id": "x/y"}]})}))
    assert before == {p: (ROOT / p).read_bytes() for p in ("ai_providers.yaml", "ai_lanes.yaml")}


def test_the_command_prints_the_report_and_never_fails_the_run(monkeypatch, capsys, tmp_path):
    from contentintel import cli
    rows = [{"id": "kilo", "ok": True, "note": "", "listed": 2, "approved_missing": ["a/b"], "candidates": [], "unroutable": ["c/d"]}, {"id": "nvidia", "ok": False, "note": "no key in this run (NVIDIA_API_KEY), so its list was not read", "listed": 0, "approved_missing": [], "candidates": [], "unroutable": []}]
    monkeypatch.setattr(CAT, "run", lambda registry, environ, inventory, http=None: rows)
    (tmp_path / "s.yaml").write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    code = cli.main(["ai", "catalog", "--data-dir", str(tmp_path / "data"), "--sources-config", str(tmp_path / "s.yaml"), "--providers-config", str(ROOT / "ai_providers.yaml"),
                     "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"), "--browser-config", str(ROOT / "browser.yaml"), "--workdir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert code == 0 and "kilo        2 listed" in out and "a/b" in out and "c/d" in out and "nvidia      NOT READ" in out


def test_the_workflow_offers_the_catalog_as_a_read_only_step_with_no_claude_key():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ai-gateway.yml").read_text(encoding="utf-8"))
    assert wf[True]["workflow_dispatch"]["inputs"]["catalog"]["default"] is False
    job = wf["jobs"]["live"]
    assert job["if"] == "inputs.live == true || inputs.catalog == true"
    steps = {s["name"]: s for s in job["steps"] if "name" in s}
    step = steps["Provider model lists (read-only)"]
    assert step["if"] == "inputs.catalog == true" and "ai catalog" in step["run"] and "ANTHROPIC_API_KEY" not in step["env"]
    assert steps["Live check (LIVE result)"]["if"] == "inputs.live == true" and steps["Live evaluation (LIVE result)"]["if"] == "inputs.live == true && inputs.evaluate == true"
    assert {"GROQ_API_KEY", "NVIDIA_API_KEY", "OLLAMA_API_KEY", "COHERE_API_KEY", "ZHIPU_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY"} <= set(step["env"])
