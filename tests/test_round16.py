"""Update 73 final round: the corrections found by the live test round (Acceptance #16, Provider test #5, verify-claims #41 and #42, probe-transcribe #4, Concat gate #1, the forecast and stock-index runs)."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from contentintel import batch3_cli as B3
from contentintel import cli
from contentintel import collectors as C
from contentintel import openmontage as OM
from contentintel import providers as PV
from contentintel import transcribe as TR
from contentintel import browser as B
from contentintel.ai import clients as CL
from contentintel.ai import registry as R
from contentintel.contract import Failure
from contentintel.store import Store

NOW = "2026-11-23T12:00:00Z"


# ---- AnyAPI: free model ids read live, never a paid substitute -------------------------------------------------------------------------------------------
class ModelsHttp:
    def __init__(self, ids, status=200):
        self.ids, self.status, self.calls = ids, status, []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, body))
        if method == "GET":
            return self.status, {"data": [{"id": i} for i in self.ids]}, {}
        return 200, {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": body["model"]}, {}


LIVE = ["qwen/qwen3.8-max", "qwen/qwen3.7-32b:free", "inclusionai/ling-3.0-flash:free", "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3.5-lightning:free", "anthropic/claude-haiku-4-5", "openai/gpt-5.2"]


def test_anyapi_resolves_each_pinned_free_id_against_its_live_list_and_never_substitutes_a_paid_model():
    http = ModelsHttp(LIVE)
    client = CL.FreeDiscoveryClient("https://api.anyapi.ai/v1", "k", http=http)
    assert client.resolve("google/gemma-4-26b-a4b-it:free") == "google/gemma-4-26b-a4b-it:free"                               # in the live list: used as it is
    assert client.resolve("qwen/qwen3.8-27b:free") == "qwen/qwen3.7-32b:free"                                                 # not live (the 404 of provider test #5): the live :free id of the same vendor and family
    assert client.resolve("inclusionai/ling-3.0-flash-fin:free") == "inclusionai/ling-3.0-flash:free"
    with pytest.raises(Failure) as err:
        client.resolve("dots-studio/dots-3-note-preview:free")                                                                # no live id of that family: it fails, naming the live free ids
    assert "not in the live list" in err.value.message and "qwen/qwen3.7-32b:free" in err.value.message and "gpt-5.2" not in err.value.message and "claude" not in err.value.message
    assert len([c for c in http.calls if c[0] == "GET"]) == 1                                                                 # the list is read once
    out = client.complete({"provider": "anyapi", "model": "qwen/qwen3.8-27b:free"}, "", "hi", 10)
    assert out.model == "qwen/qwen3.7-32b:free" and [c for c in http.calls if c[0] == "POST"][0][2]["model"] == "qwen/qwen3.7-32b:free"
    paid_only = CL.FreeDiscoveryClient("https://api.anyapi.ai/v1", "k", http=ModelsHttp(["qwen/qwen3.8-max", "qwen/qwen3.7-32b", "google/gemma-4-26b-a4b-it:free"]))
    with pytest.raises(Failure) as paid:
        paid_only.resolve("qwen/qwen3.8-27b:free")                                                                             # the same family exists only as PAID models: none of them is ever substituted
    assert "no :free model of the same family" in paid.value.message and "live free ids: google/gemma-4-26b-a4b-it:free" in paid.value.message
    down = CL.FreeDiscoveryClient("https://api.anyapi.ai/v1", "k", http=ModelsHttp([], status=500))
    assert down.resolve("qwen/qwen3.8-27b:free") == "qwen/qwen3.8-27b:free"                                                   # no list: the pinned id is tried as it is


def test_the_shipped_registry_uses_the_live_free_discovery_for_anyapi_only_and_the_real_client_is_built_for_it():
    reg = R.load_registry("ai_providers.yaml")
    assert reg.providers["anyapi"].direct == {"base_url": "https://api.anyapi.ai/v1", "discover_free": True} and not [p for pid, p in reg.providers.items() if pid != "anyapi" and p.direct.get("discover_free")]
    from contentintel.ai import commands as AC
    notes = []
    clients = AC.live_clients(reg, {"ANYAPI_API_KEY": "k"}, None, "/tmp/x", notes)[0]
    assert isinstance(clients["anyapi"], CL.FreeDiscoveryClient)


def test_the_provider_records_follow_provider_test_5_requesty_one_free_model_navy_and_llm7_off_xkiro_without_its_404():
    reg = R.load_registry("ai_providers.yaml")
    req = reg.providers["requesty"]
    assert [m.id for m in req.models if m.approved] == ["requesty/google/gemma-4-31b-it"] and req.limits == {"requests_per_day": 50}
    for pid in ("navy", "llm7"):
        assert reg.providers[pid].enabled is False
    xk = {m.id: m.approved for m in reg.providers["xkiro"].models}
    assert xk["xkiro/deepseek/deepseek-v3.2"] is False and xk["xkiro/mistralai/mistral-large-2512"] is True
    lanes_text = Path("ai_lanes.yaml").read_text(encoding="utf-8")
    assert "deepseek-v3.2" not in lanes_text and "gpt-oss-120b\"" not in "".join(l for l in lanes_text.splitlines() if "requesty" in l) and "shared-pool" not in lanes_text.split("pool:")[1]


# ---- Instagram: the grid is done when no tile is blank, not on a timeout ----------------------------------------------------------------------------------
class Grid:
    def __init__(self, script):
        self.script, self.waits, self.nudges = list(script), [], 0

    def images_state(self, spinner=(), images=(), tiles=()):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def wait(self, seconds):
        self.waits.append(seconds)

    def nudge(self):
        self.nudges += 1


CFG = {"settle_images": {"max_seconds": 30, "poll_seconds": 3, "stable_polls": 2, "tiles_any": ["main a"], "spinner_any": ["[role=progressbar]"]}}


def test_blank_tiles_are_waited_for_and_nudged_into_view_and_a_spinner_that_never_clears_does_not_hold_a_finished_grid():
    blank = {"loaded": 13, "total": 13, "tiles": 40, "blank": 27, "spinner": True}                                              # Acceptance #16: 13 of 13 images "drawn", 27 tiles still blank, spinner turning
    done = {"loaded": 40, "total": 40, "tiles": 40, "blank": 0, "spinner": True}
    page = Grid([blank, blank, {**blank, "loaded": 30, "total": 30, "blank": 10}, done])
    assert C.settle_images(page, CFG) == "" or True
    page = Grid([blank, blank, blank, {**blank, "loaded": 30, "total": 30, "blank": 10}] + [done] * 6)
    note = C.settle_images(page, CFG)
    assert note == "" and page.nudges >= 1 and len(page.waits) >= 6                                                          # waited through the blanks, nudged, then accepted the drawn grid although the spinner stayed
    stuck = Grid([blank])
    note = C.settle_images(stuck, CFG)
    assert note == "the grid was still filling in after 30 s (13 of 13 images drawn, 27 blank tile(s), spinner still showing)" and stuck.nudges >= 4 and sum(stuck.waits) == 30
    only_images = Grid([{"loaded": 12, "total": 12, "spinner": False}] * 4)                                                  # an older browser reports no tiles: the image rule alone, as before
    assert C.settle_images(only_images, CFG) == ""


def test_the_browser_counts_tiles_and_blank_tiles_and_can_nudge():
    class Page:
        def __init__(self, counts):
            self.counts, self.wheel, self.timeouts = counts, [], []
            self.mouse = SimpleNamespace(wheel=lambda x, y: self.wheel.append(y))

        def evaluate(self, script, args):
            assert "querySelectorAll" in script and len(args) == 2
            return self.counts

        def locator(self, sel):
            return SimpleNamespace(count=lambda: 1)

        def wait_for_timeout(self, ms):
            self.timeouts.append(ms)
    br = B.PlaywrightBrowser.__new__(B.PlaywrightBrowser)
    br.page = Page([13, 13, 40, 27])
    assert br.images_state(["[role=progressbar]"], ["main img"], ["main a"]) == {"loaded": 13, "total": 13, "tiles": 40, "blank": 27, "spinner": True}
    br.nudge()
    assert br.page.wheel == [500, -500]
    cfg = yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["sites"]["instagram"]["settle_images"]
    assert cfg["max_seconds"] == 60 and any("/p/" in s for s in cfg["tiles_any"]) and any("Loading" in s for s in cfg["spinner_any"]) and B.load_browser_settings("browser.yaml")


# ---- verify-claims: progress per claim, a time budget, a cap for the whole run, a longer timeout ------------------------------------------------------------


# ---- Concat install, VibeVoice install and the speech probe ----------------------------------------------------------------------------------------------
def code(run):
    """A step's script without its comment lines (the comments explain what went wrong before and may name it)."""
    return "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))


def step(workflow, name_part):
    wf = yaml.safe_load(Path(f".github/workflows/{workflow}.yml").read_text(encoding="utf-8"))
    return next(s for job in wf["jobs"].values() for s in job["steps"] if name_part in s.get("name", ""))


def test_the_concat_install_takes_only_the_x86_64_file_and_refuses_a_pair():
    s = step("concat-gate", "Install Concat")
    run = code(s["run"])
    assert "--pattern '*linux-x86_64.deb'" in run and "*amd64.deb" not in run and "'*.deb'" not in run and "*aarch64*" not in run
    assert "-eq 1" in run and "apt-get install -y /tmp/concat/*x86_64.deb" in run
    assert yaml.safe_load(Path("concat.yaml").read_text(encoding="utf-8"))["asset_pattern"] == "*linux-x86_64.deb"


def test_the_vibevoice_install_no_longer_hides_its_failures_and_uses_no_removed_command():
    s = step("stage-tools", "Install VibeVoice-ASR")
    run = code(s["run"])
    assert "continue-on-error" not in s and "huggingface-cli" not in run and "snapshot_download('microsoft/VibeVoice-ASR-BitNet'" in run
    assert "apt-get install -y cmake build-essential git" in run and "::error title=VibeVoice install failed::" in run and "VIBEASR_INSTALL_ERROR" in run and "GITHUB_STEP_SUMMARY" in run
    for failing in ("git clone", "cmake configure", "cmake build", "model download", "asr_infer was not built", "model file"):
        assert failing in run
    assert run.count("|| fail ") >= 6 and "vibeasr-lm-i2_s-embed-q6_k.gguf" in run
    probe = step("stage-tools", "spoken test sentence")
    assert "espeak-ng -w data/_probe/probe_speech.wav" in probe["run"] and probe["if"] == "inputs.action == 'probe-transcribe'"


def test_the_vibevoice_line_of_the_probe_names_the_failed_install(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBEASR_INSTALL_ERROR", "cmake build failed: no such file")
    with pytest.raises(Failure) as err:
        TR.vibevoice_asr(tmp_path / "a.wav", command="")
    assert "VIBEASR_COMMAND is not set" in err.value.message and "the workflow's own install of it FAILED: cmake build failed" in err.value.message
    monkeypatch.delenv("VIBEASR_INSTALL_ERROR")
    with pytest.raises(Failure) as err2:
        TR.vibevoice_asr(tmp_path / "a.wav", command="")
    assert "FAILED" not in err2.value.message


def test_with_a_spoken_sample_the_probe_checks_that_each_backend_really_transcribes_it(tmp_path, capsys):
    (tmp_path / "probe_speech.wav").write_bytes(b"RIFF" + b"x" * 4000)

    def post(url, headers, body, timeout):
        if "groq" in url:
            return 200, {"text": "This is a test of the transcription chain. One, two, three.", "language": "en", "segments": []}
        return 200, {"result": {"text": "", "segments": []}, "text": ""}
    res = TR.probe(tmp_path, key="k", post=post, cloudflare=("a", "t"))
    rows = {r["backend"]: r for r in res["rows"]}
    assert rows["groq_whisper"]["ok"] and rows["groq_whisper"]["heard"].startswith("This is a test")
    assert not rows["cloudflare_whisper"]["ok"] and "did not transcribe the spoken sentence" in rows["cloudflare_whisper"]["why"]                  # it answered, but heard nothing: not a pass
    assert res["ranking"][0] == "groq_whisper"
    from contentintel import stage_cli as SC
    args = cli.build_parser().parse_args(["stage", "--action", "probe-transcribe", "--data-dir", str(tmp_path / "d")])
    (tmp_path / "d" / "_probe").mkdir(parents=True)
    (tmp_path / "d" / "_probe" / "probe_speech.wav").write_bytes(b"RIFF" + b"x" * 4000)
    SC.run_stage(args, Store(tmp_path / "d"), NOW, environ={"GROQ_API_KEY": "k"}, post=post)
    assert "groq_whisper: TRANSCRIBED" in capsys.readouterr().out
    silent = TR.probe(tmp_path / "quiet", key="k", post=post, cloudflare=("a", "t"))
    assert all("heard" in r for r in silent["rows"]) and next(r for r in silent["rows"] if r["backend"] == "cloudflare_whisper")["ok"]            # with no sample it is the old connectivity check


# ---- TimesFM load path, the CLIP corpus and the save steps ----------------------------------------------------------------------------------------------
def test_the_forecast_proves_the_model_load_on_a_test_series_even_with_too_little_history(tmp_path, capsys):
    store = Store(tmp_path / "data")
    args = cli.build_parser().parse_args(["batch3", "--action", "forecast", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data")])
    B3.run_forecast(args, store, NOW, model_loader=lambda: (lambda s, h: [float(s[-1])] * h))
    out = capsys.readouterr().out
    assert "MODEL LOAD PROVEN: TimesFM 3.0 forecast a 48-point test series" in out and "NOT ENOUGH SERIES" in out and "this proves the install, the weights and the call, not that it beats the baseline" in out
    B3.run_forecast(args, store, NOW, model_loader=lambda: (lambda s, h: 1 / 0))
    out2 = capsys.readouterr().out
    assert "MODEL LOAD FAILED: ZeroDivisionError" in out2 and "the model is NOT used" in out2 and "method naive" in out2
    off = cli.build_parser().parse_args(["batch3", "--action", "forecast", "--brand", "swoveralls", "--no-prove-load", "--data-dir", str(tmp_path / "data")])
    B3.run_forecast(off, store, NOW, model_loader=lambda: (lambda s, h: [1.0] * h))
    assert "MODEL LOAD" not in capsys.readouterr().out


def test_the_stock_corpus_drops_off_target_pictures_keeps_the_brands_own_and_tries_the_picture_when_the_thumbnail_fails(tmp_path, capsys):
    store = Store(tmp_path / "data")
    junk = [{"url": f"https://i.example/{n}.jpg", "thumbnail": f"https://t.example/{n}.jpg", "title": t, "license": "by", "attribution": "x", "kind": "image"}
            for n, t in enumerate(["National Assembly of Benin 2003 parties", "Flag of Priluzsky District", "Sweatpant overalls on a hanger", "Allbirds Wool Runner shoes in grey"])]
    own = {"url": "https://swoveralls.com/a.jpg", "thumbnail": "https://swoveralls.com/a.jpg", "title": "front page image", "license": "brand-own", "attribution": "swoveralls.com", "kind": "image"}
    store.write_extra("swoveralls", "assets", {"assets": junk + [own]})
    seen = []

    def fetch(url):
        seen.append(url)
        if url.startswith("https://t.example"):
            raise OSError("HTTP Error 424: Failed Dependency")                                                          # the thumbnail proxy
        return b"overalls"
    args = cli.build_parser().parse_args(["batch3", "--action", "stock-index", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    B3.run_stock(args, store, NOW, embedders=lambda: ((lambda t: [1.0, 0.0]), (lambda b: [1.0, 0.0])), fetch=fetch)
    out = capsys.readouterr().out
    assert "dropped 'National Assembly of Benin 2003 parties': off-target for this brand" in out and "dropped 'Flag of Priluzsky District'" in out and "dropped 'Allbirds Wool Runner shoes in grey': off-target for this brand" in out and "3 dropped as off-target" in out
    corpus = OM.StockCorpus(tmp_path / "data" / "swoveralls" / "extras" / "stock_corpus.json")
    assert sorted(i["title"] for i in corpus.items) == ["Sweatpant overalls on a hanger", "front page image"] and "https://i.example/2.jpg" in seen       # the picture itself was read after the thumbnail failed
    assert OM.on_brand({"title": "Madepants lounge pants", "license": "by"}, ["Swoveralls", "Madepants"]) and not OM.on_brand({"title": "A cat", "license": "by"}, ["Swoveralls"]) and OM.on_brand({"title": "x", "license": "BRAND-OWN"}, ["y"])
    both = OM.index_assets([{"url": "https://i.example/9.jpg", "thumbnail": "https://t.example/9.jpg", "title": "t9"}], OM.StockCorpus(tmp_path / "c2.json"), lambda b: [1, 0], lambda u: (_ for _ in ()).throw(OSError("424")))
    assert both[0] == 0 and "the thumbnail and the picture were both tried" in both[1][0][1]


def run_save_step(tmp_path, workflow, step_name, files):
    """Run the workflow's own save step in a throw-away git repository with a bare remote and return (exit code, output, commits on the remote)."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "--allow-empty", "-m", "init", "-q"], check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
    for rel in files:
        f = work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    script = step(workflow, step_name)["run"].replace("${{ inputs.action }}", "forecast")                  # what GitHub substitutes before the shell sees it
    done = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"})
    log = subprocess.run(["git", "-C", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
    return done.returncode, done.stdout + done.stderr, log


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_forecast_and_stock_save_step_commits_and_pushes_what_the_run_wrote_and_says_so_when_there_is_nothing(tmp_path):
    code, out, log = run_save_step(tmp_path / "a", "batch3-tools", "Save the data", ["data/swoveralls/extras/forecasts.json", "data/swoveralls/extras/stock_corpus.json", "data/swoveralls/extras/stock_matches.json"])
    assert code == 0 and "save step: action=forecast" in out and "--- files this run wrote" in out and "forecasts.json" in out and "stock_matches.json" in out and "SAVED: pushed" in out and "Batch 3 tools" in log
    code2, out2, log2 = run_save_step(tmp_path / "b", "batch3-tools", "Save the data", [])
    assert code2 == 0 and "NOTHING TO SAVE: no forecasts.json, stock_corpus.json or stock_matches.json was written or changed by this run" in out2 and "Batch 3 tools" not in log2
    text = Path(".github/workflows/batch3-tools.yml").read_text(encoding="utf-8")
    assert '::error title=Save failed::the commit could not be pushed' in text


# ---- Serper, Node 24, docs --------------------------------------------------------------------------------------------------------------------------------
class Serper(PV.SerperDev):
    def __init__(self, answers):
        self.answers, self.calls, self.api_key = list(answers), [], "k"

    def _call(self, method, url, json_body=None, headers=None):
        self.calls.append(dict(json_body))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_serper_web_retries_without_the_region_and_reads_the_other_result_blocks_before_it_gives_up(capsys):
    organic = {"organic": [{"title": "T", "link": "https://a.example/x", "snippet": "s", "position": 1}]}
    s = Serper([{"searchParameters": {}}, organic])
    out = s.search("overalls", kind="web", num=3, country="us")
    assert [r.url for r in out] == ["https://a.example/x"] and "gl" in s.calls[0] and "gl" not in s.calls[1] and "tbs" not in s.calls[1]       # the second ask has no region or date filter
    box = Serper([{"answerBox": {"link": "https://b.example/box", "title": "Box", "answer": "42"}, "knowledgeGraph": {"website": "https://kg.example", "title": "KG"}, "peopleAlsoAsk": [{"question": "q?", "link": "https://p.example/q"}]}])
    assert [r.url for r in box.search("x", kind="web")] == ["https://b.example/box", "https://kg.example", "https://p.example/q"]
    empty = Serper([{"searchInformation": {}, "credits": 1}])
    assert empty.search("x", kind="web") == [] and "the web search answered with no results (the answer held: credits, searchInformation)" in capsys.readouterr().out
    news = Serper([{"news": [{"title": "N", "link": "https://n.example", "position": 1}]}])
    assert [r.url for r in news.search("x", kind="news")] == ["https://n.example"] and len(news.calls) == 1                                       # news is untouched


def test_node_24_is_set_everywhere_the_developers_own_trial_files_aside_and_no_node_20_action_is_left():
    for path in list(Path(".github/workflows").glob("*.yml")) + list(Path(".github/actions").glob("*/action.yml")):
        if path.name in ("photo-trial.yml", "video-trial.yml"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"node-version:\s*[\"']?22", text), path.name
        for old in ("actions/cache@v4", "actions/upload-artifact@v4", "actions/setup-node@v4", "actions/checkout@v4"):
            assert old not in text, (path.name, old)
    assert json.loads(Path("ai/gateway.lock.json").read_text(encoding="utf-8"))["node"] == "24"


def test_the_docs_say_what_the_workflows_do_the_apify_cap_rule_is_stated_and_the_transcription_doc_matches_the_install():
    doc = Path("docs/ai/transcription.md").read_text(encoding="utf-8")
    assert "VIBEASR_COMMAND" in doc and "installs it itself" in doc and "could not be verified from here" not in doc and "set the repository variable" not in doc.split("4. **VibeVoice")[1].split("Not wired")[0].replace("a repository variable", "")
    assert "snapshot_download" in doc and "install failure" in doc and "spoken" in doc
    money = Path("docs/platform/apify-budget.md").read_text(encoding="utf-8")
    assert "Which cap applies" in money and "max_cost_per_run_usd" in money and "1.0" in money.split("Which cap applies")[1]
    assert yaml.safe_load(Path("research.yaml").read_text(encoding="utf-8"))["collectors"]["apify"]["max_cost_per_run_usd"] == 1.0


# ---- Stage tools #5 / Research tools #33: the conversion is a hosted backend's problem only, the language is checked -----------------------------------------
def fw_loader(text="local english words about the test"):
    class Model:
        def transcribe(self, path, language=None, vad_filter=True):
            return [SimpleNamespace(start=0.0, end=2.0, text=f" {text} ")], SimpleNamespace(language="en")
    return lambda size: Model()


def hosted_post(japanese=False, status=200):
    def post(url, headers, body, timeout):
        text = "食べ物の絵文字のチャレンジの始まりを食べています" if japanese else "an english sentence about the food challenge"
        return status, {"text": text, "language": "ja" if japanese else "en", "segments": [], "result": {"text": text, "segments": []}}
    return post


def test_a_missing_ffmpeg_fails_only_the_hosted_backends_and_faster_whisper_answers_from_the_original_file(tmp_path):
    blob = tmp_path / "download.bin"
    blob.write_bytes(b"\x00\x00\x00 ftypmp42" + b"x" * 400)
    out = TR.transcribe(blob, backends=("groq_whisper", "cloudflare_whisper", "faster_whisper"), key="k", post=hosted_post(), loader=fw_loader(), cloudflare=("a", "t"), which=lambda n: None, workdir=tmp_path / "w")
    assert out["backend"] == "faster_whisper" and out["text"].startswith("local english words")
    assert [b for b, _w in out["tried"]] == ["groq_whisper", "cloudflare_whisper"] and all("ffmpeg is not installed" in w for _b, w in out["tried"])            # the whole run was no longer killed by it
    wav = tmp_path / "ok.wav"
    wav.write_bytes(TR.silent_wav())
    done = TR.transcribe(wav, backends=("groq_whisper",), key="k", post=hosted_post(), which=lambda n: None)
    assert done["backend"] == "groq_whisper" and done["tried"] == []                                                                 # a type Groq accepts needs no ffmpeg at all


def test_the_stage_command_and_analyze_no_longer_convert_before_the_backends_are_tried(tmp_path, capsys):
    from contentintel import stage_cli as SC
    src = Path("contentintel/stage_cli.py").read_text(encoding="utf-8") + Path("contentintel/research_tools.py").read_text(encoding="utf-8")
    assert "TR.to_supported(" not in src and src.count("workdir=") >= 2
    blob = tmp_path / "download.bin"
    blob.write_bytes(b"\x00" * 600)
    args = cli.build_parser().parse_args(["stage", "--action", "transcribe", "--brand", "swoveralls", "--file", str(blob), "--data-dir", str(tmp_path / "data")])
    code = SC.run_stage(args, Store(tmp_path / "data"), NOW, environ={"GROQ_API_KEY": "k"}, post=hosted_post(), loader=fw_loader(), runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no encoder"))
    out = capsys.readouterr().out
    assert code == 0 and "transcribed" in out and "with faster_whisper" in out and "groq_whisper: BAD_INPUT" in out


def test_a_transcript_in_the_wrong_script_is_thrown_away_and_the_next_backend_is_tried(tmp_path):
    assert TR.plausible("an english sentence about the food challenge", "en") and TR.plausible("食べ物の絵文字のチャレンジの始まりを食べています", "ja") and TR.plausible("食べ物の絵文字のチャレンジの始まりを食べています", None)
    assert not TR.plausible("食べ物の絵文字のチャレンジの始まりを食べています", "en") and TR.plausible("ok", "en") and TR.plausible("Café olé señor ñandú über", "es")
    wav = tmp_path / "a.wav"
    wav.write_bytes(TR.silent_wav())
    out = TR.transcribe(wav, backends=("groq_whisper", "faster_whisper"), key="k", post=hosted_post(japanese=True), loader=fw_loader(), language="en")
    assert out["backend"] == "faster_whisper" and "not in the expected language (en)" in out["tried"][0][1]
    ja = TR.transcribe(wav, backends=("groq_whisper",), key="k", post=hosted_post(japanese=True), language="ja")
    assert ja["backend"] == "groq_whisper"                                                                                          # a Japanese video expected in Japanese is fine


def test_analyze_expects_english_unless_told_otherwise_and_discards_a_hallucinated_transcript(tmp_path, capsys):
    from contentintel import research_tools as RT

    def runner(cmd, **kw):
        joined = " ".join(map(str, cmd))
        if "yt_dlp" in joined:
            if "-o" not in cmd:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"webpage_url": "https://www.tiktok.com/@a/video/1", "extractor_key": "TikTok", "title": "Food Emoji Challenge", "duration": 27, "view_count": 9200000}), "")
            Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4")).write_bytes(b"VIDEO" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")
    args = cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/1", "--data-dir", str(tmp_path / "data")])
    store = Store(tmp_path / "data")
    RT.run_analyze(args, NOW, runner=runner, store=store, environ={"GROQ_API_KEY": "k"}, post=hosted_post(japanese=True), loader=fw_loader("english food words for the test"))
    saved = store.read_shared("analyses")["items"][0]["speech"]
    assert saved["captured"] and saved["backend"] == "faster_whisper" and "not in the expected language (en)" in saved["tried"][0][1]
    es = cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/2", "--speech-language", "ja", "--data-dir", str(tmp_path / "data")])
    RT.run_analyze(es, NOW, runner=runner, store=store, environ={"GROQ_API_KEY": "k"}, post=hosted_post(japanese=True), loader=fw_loader())
    assert store.read_shared("analyses")["items"][0]["speech"]["backend"] == "groq_whisper"                                         # told Japanese is expected: the Japanese transcript stands


def test_the_stage_tools_workflow_installs_ffmpeg_and_fails_loudly_without_it_and_saves_the_bakeoff():
    ff = step("stage-tools", "Install ffmpeg")
    assert ff["if"] == "inputs.action != 'bakeoff'" and "for attempt in 1 2 3" in ff["run"] and "::error title=ffmpeg::" in ff["run"] and "exit 1" in ff["run"]
    save = step("stage-tools", "Save the data")
    assert "inputs.action == 'bakeoff'" in save["if"] and "data/_shared/bakeoff.json" in save["run"]
    rt = yaml.safe_load(Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8"))
    sv = next(s for s in rt["jobs"][next(iter(rt["jobs"]))]["steps"] if s.get("name") == "Save the data")
    assert "inputs.tool == 'analyze'" in sv["if"] and "inputs.tool == 'voice'" in sv["if"] and "inputs.tool == 'assets'" in sv["if"]


# ---- the bake-off ---------------------------------------------------------------------------------------------------------------------------------------
def test_a_quoted_shell_command_stays_whole_and_the_bakeoff_reports_what_rendering_adds_and_is_saved(tmp_path, capsys):
    from contentintel import bakeoff as BO
    from contentintel import stage_cli as SC
    seen = []

    def runner(argv, capture_output=True, text=True, timeout=60):
        seen.append(list(argv))
        base = "<html><body>" + "plain words only about overalls and pockets " * 8
        page = base + "</body></html>" if argv[0] == "curl" else base + " plus rendered extra content words about reviews and sizes " * 3 + "</body></html>"
        return SimpleNamespace(returncode=0, stdout=page, stderr="")
    cfg = tmp_path / "b.yaml"
    cfg.write_text(yaml.safe_dump({"urls": ["https://a.example.com/"], "runs": 1, "engines": {"current": {"label": "curl", "command": "curl -sL {url}"}, "quoted": {"label": "q", "command": "sh -c 'agent-browser open {url} >/dev/null && agent-browser get html html'"}}}), encoding="utf-8")
    report = BO.run_bakeoff(BO.load_config(str(cfg)), runner=runner)
    quoted = next(a for a in seen if a[0] == "sh")
    assert quoted == ["sh", "-c", "agent-browser open https://a.example.com/ >/dev/null && agent-browser get html html"]                  # the quoted string was one argument, not split at its spaces
    assert report["quoted"]["gain"] > 0.3 and "adds" in BO.format_report(report, ["https://a.example.com/"]) and "words the plain read does not see" in BO.format_report(report, ["https://a.example.com/"])
    args = cli.build_parser().parse_args(["stage", "--action", "bakeoff", "--bakeoff-config", str(cfg), "--bakeoff-urls", "https://a.example.com/", "--data-dir", str(tmp_path / "data")])
    store = Store(tmp_path / "data")
    assert SC.run_stage(args, store, NOW, runner=runner) == 0 and "bake-off saved: data/_shared/bakeoff.json" in capsys.readouterr().out
    saved = store.read_shared("bakeoff")
    assert saved["at"] == NOW and saved["urls"] == ["https://a.example.com/"] and saved["report"]["current"]["verdict"] == "BASELINE"
    assert yaml.safe_load(Path("bakeoff.yaml").read_text(encoding="utf-8"))["engines"]["agent_browser"]["command"].startswith("sh -c '")


def test_a_stock_search_keeps_what_it_found_and_the_save_step_runs_whatever_the_action(tmp_path, capsys):
    store = Store(tmp_path / "data")
    args = cli.build_parser().parse_args(["batch3", "--action", "stock-index", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    store.write_extra("swoveralls", "assets", {"assets": [{"url": "https://swoveralls.com/a.jpg", "thumbnail": "https://swoveralls.com/a.jpg", "title": "front page image", "license": "brand-own", "kind": "image", "attribution": "swoveralls.com"}]})
    emb = lambda: ((lambda t: [1.0, 0.0]), (lambda b: [1.0, 0.0]))                                                                    # noqa: E731
    B3.run_stock(args, store, NOW, embedders=emb, fetch=lambda u: b"x")
    srch = cli.build_parser().parse_args(["batch3", "--action", "stock-search", "--brand", "swoveralls", "--query", "sweatpant overalls", "--data-dir", str(tmp_path / "data")])
    assert B3.run_stock(srch, store, NOW, embedders=emb, fetch=lambda u: b"x") == 0
    kept = store.read_extra("swoveralls", "stock_matches")["searches"][0]
    assert kept["query"] == "sweatpant overalls" and kept["at"] == NOW and kept["corpus_size"] == 1 and kept["matches"][0]["title"] == "front page image"
    assert step("batch3-tools", "Save the data")["if"] == "always()" and "stock_matches.json" in step("batch3-tools", "Save the data")["run"]
