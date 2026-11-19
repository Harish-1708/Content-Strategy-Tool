"""Stage 5: transcription. Groq's hosted Whisper first, Faster-Whisper as the local backup, a measured order, a probe, the stage command."""
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentintel import cli
from contentintel import contract
from contentintel import stage_cli as SC
from contentintel import transcribe as TR
from contentintel.ai.clients import Failure
from contentintel.store import Store

AT = "2026-11-18T10:00:00Z"


def ok_post(text="hello there", language="en"):
    calls = []

    def post(url, headers, body, timeout):
        calls.append((url, headers, body))
        return 200, {"text": f" {text} ", "language": language, "segments": [{"start": 0.0, "end": 1.5, "text": text}]}
    post.calls = calls
    return post


def fw_loader(text="local words"):
    class Model:
        def transcribe(self, path, language=None, vad_filter=True):
            return [SimpleNamespace(start=0.0, end=2.0, text=f" {text} ")], SimpleNamespace(language="en")
    return lambda size: Model()


def audio(tmp_path, name="clip.wav"):
    p = tmp_path / name
    p.write_bytes(TR.silent_wav())
    return p


def test_the_multipart_body_carries_the_fields_and_the_file_in_one_valid_form():
    ctype, body = TR.multipart({"model": "m", "temperature": "0"}, "a.wav", b"RIFFdata")
    boundary = ctype.split("boundary=")[1]
    assert ctype.startswith("multipart/form-data") and body.startswith(f"--{boundary}".encode()) and body.rstrip().endswith(f"--{boundary}--".encode())
    assert b'name="model"\r\n\r\nm' in body and b'name="file"; filename="a.wav"' in body and b"RIFFdata" in body


def test_groq_whisper_answers_first_and_faster_whisper_takes_over_when_it_fails(tmp_path):
    path = audio(tmp_path)
    post = ok_post()
    out = TR.transcribe(path, backends=("groq_whisper", "faster_whisper"), key="gsk_secret", post=post, loader=fw_loader())
    assert out["backend"] == "groq_whisper" and out["text"] == "hello there" and out["segments"][0]["end"] == 1.5 and out["tried"] == []
    url, headers, body = post.calls[0]
    assert url == TR.GROQ_URL and headers["Authorization"] == "Bearer gsk_secret" and TR.GROQ_MODEL.encode() in body and b"verbose_json" in body
    failing = lambda url, headers, body, timeout: (429, {"error": "rate limit"})                                     # noqa: E731
    out2 = TR.transcribe(path, backends=("groq_whisper", "faster_whisper"), key="gsk_secret", post=failing, loader=fw_loader("backup text"))
    assert out2["backend"] == "faster_whisper" and out2["text"] == "backup text" and out2["tried"] == [("groq_whisper", f"{contract.RATE_LIMITED}: Groq Whisper's free limit was reached")]
    with pytest.raises(Failure) as err:
        TR.transcribe(path, backends=("groq_whisper", "vibevoice", "mystery"), key="", post=ok_post())
    assert "GROQ_API_KEY is not set" in str(err.value) and "licence" in str(err.value) and "unknown backend" in str(err.value)


def test_each_groq_failure_is_named_and_the_key_never_appears_in_an_error():
    def code(status, body=None):
        return lambda url, headers, b, timeout: (status, body if body is not None else {})
    for status, expected in ((401, contract.NEED_SETUP), (403, contract.NEED_SETUP), (402, contract.RATE_LIMITED), (500, contract.UPSTREAM_BROKEN)):
        with pytest.raises(Failure) as err:
            TR.groq_whisper(b"x" * 10, "a.wav", key="gsk_secret", post=code(status, {"error": "boom"}))
        assert err.value.code == expected
    def boom(url, headers, body, timeout):
        raise ConnectionError("could not reach api.groq.com with Bearer gsk_secret")
    with pytest.raises(Failure) as err:
        TR.groq_whisper(b"x", "a.wav", key="gsk_secret", post=boom)
    assert "gsk_secret" not in str(err.value) and "***" in str(err.value)
    with pytest.raises(Failure) as err:
        TR.groq_whisper(b"x" * (TR.MAX_BYTES + 1), "big.wav", key="gsk_secret", post=ok_post())
    assert err.value.code == contract.BAD_INPUT and "24 MB" in err.value.message
    with pytest.raises(Failure) as err:
        TR.groq_whisper(b"x", "a.wav", key="", post=ok_post())
    assert err.value.code == contract.NEED_SETUP


def test_faster_whisper_reports_a_missing_library_and_a_failed_run_and_joins_segments(tmp_path, monkeypatch):
    out = TR.faster_whisper_local(audio(tmp_path), loader=fw_loader("one two"))
    assert out["text"] == "one two" and out["language"] == "en" and out["segments"] == [{"start": 0.0, "end": 2.0, "text": "one two"}]
    import sys
    monkeypatch.setitem(sys.modules, "faster_whisper", None)                                                          # not installed
    with pytest.raises(Failure) as err:
        TR.faster_whisper_local(audio(tmp_path))
    assert err.value.code == contract.NEED_SETUP and "pip" in err.value.message
    class Broken:
        def transcribe(self, *a, **k):
            raise RuntimeError("model file missing")
    with pytest.raises(Failure) as err:
        TR.faster_whisper_local(audio(tmp_path), loader=lambda size: Broken())
    assert err.value.code == contract.UPSTREAM_BROKEN and "model file missing" in err.value.message


def test_the_order_is_the_measured_one_with_the_failing_backend_last_and_the_seeded_one_until_a_probe_has_run(tmp_path):
    assert TR.order(tmp_path) == TR.SEEDED_ORDER == ("groq_whisper", "faster_whisper")
    (tmp_path / TR.RANKINGS_FILE).write_text(json.dumps({"ranking": ["faster_whisper"]}), encoding="utf-8")
    assert TR.order(tmp_path) == ("faster_whisper", "groq_whisper")
    (tmp_path / TR.RANKINGS_FILE).write_text("not json", encoding="utf-8")
    assert TR.order(tmp_path) == TR.SEEDED_ORDER
    (tmp_path / TR.RANKINGS_FILE).write_text(json.dumps({"ranking": ["vibevoice", "groq_whisper"]}), encoding="utf-8")
    assert TR.order(tmp_path) == ("groq_whisper", "faster_whisper") and "vibevoice" in TR.NOT_WIRED


def test_the_probe_asks_every_backend_with_a_second_of_silence_and_ranks_the_ones_that_answered_fastest_first(tmp_path):
    wav = TR.silent_wav(1, 16000)
    riff, size, wave = struct.unpack("<4sI4s", wav[:12])
    assert riff == b"RIFF" and wave == b"WAVE" and size == len(wav) - 8 and len(wav) == 44 + 32000
    tick = [0]
    slow_post = ok_post()
    def post(url, headers, body, timeout):
        tick[0] += 5                                                                                                    # groq took 5 s
        return slow_post(url, headers, body, timeout)
    inner = fw_loader()
    def loader(size):
        model = inner(size)
        original = model.transcribe
        def timed(*a, **k):
            tick[0] += 1                                                                                                # the backup took 1 s
            return original(*a, **k)
        model.transcribe = timed
        return model
    res = TR.probe(tmp_path, key="k", post=post, loader=loader, clock=lambda: tick[0])
    assert res["ranking"] == ["faster_whisper", "groq_whisper"] and all(r["ok"] for r in res["rows"]) and [r["seconds"] for r in res["rows"]] == [5, 1]
    bad = TR.probe(tmp_path, key="", post=ok_post(), loader=None, clock=lambda: 0)
    assert [r["ok"] for r in bad["rows"]] == [False, False] or bad["ranking"] in ([], ["faster_whisper"])


def test_a_transcript_is_saved_without_the_audio_newest_first_and_never_twice(tmp_path):
    store = Store(tmp_path / "data")
    TR.save_transcript(store, "swoveralls", {"sha": "a" * 64, "source": "https://x.test/a.mp3", "backend": "groq_whisper", "language": "en", "text": "first words", "seconds": 1.0}, AT)
    TR.save_transcript(store, "swoveralls", {"sha": "b" * 64, "source": "clip", "backend": "faster_whisper", "language": "en", "text": "second words", "seconds": 2.0}, AT)
    TR.save_transcript(store, "swoveralls", {"sha": "a" * 64, "source": "https://x.test/a.mp3", "backend": "groq_whisper", "language": "en", "text": "first words again", "seconds": 1.0}, AT)
    items = store.read_extra("swoveralls", "transcripts")["items"]
    assert [i["sha"][0] for i in items] == ["a", "b"] and items[0]["text"] == "first words again" and "audio" not in json.dumps(items)
    assert "first words again" in (tmp_path / "data" / "swoveralls" / "catalogue" / "transcripts.md").read_text(encoding="utf-8")


def args_for(tmp_path, *extra):
    return cli.build_parser().parse_args(["stage", "--data-dir", str(tmp_path / "data"), *extra])


def test_the_stage_command_transcribes_a_file_and_saves_the_transcript(tmp_path, capsys):
    path = audio(tmp_path)
    code = SC.run_stage(args_for(tmp_path, "--action", "transcribe", "--brand", "swoveralls", "--file", str(path), "--source", "a test clip"), Store(tmp_path / "data"), AT,
                        environ={"GROQ_API_KEY": "gsk_x"}, post=ok_post("spoken words"))
    out = capsys.readouterr().out
    assert code == 0 and "transcribed 12 characters with groq_whisper" in out
    assert Store(tmp_path / "data").read_extra("swoveralls", "transcripts")["items"][0]["source"] == "a test clip"
    for extra, said in ((("--action", "transcribe", "--brand", "nobody", "--file", str(path)), "needs --brand"), (("--action", "transcribe", "--brand", "swoveralls"), "exactly one of --file and --url"),
                        (("--action", "transcribe", "--brand", "swoveralls", "--url", "http://insecure.test/a.mp3"), "only https addresses are read"),
                        (("--action", "transcribe", "--brand", "swoveralls", "--file", str(tmp_path / "missing.wav")), "does not exist")):
        assert SC.run_stage(args_for(tmp_path, *extra), Store(tmp_path / "data"), AT, environ={}, post=ok_post()) == 1 and said in capsys.readouterr().out
    assert SC.run_stage(args_for(tmp_path, "--action", "transcribe", "--brand", "swoveralls", "--file", str(path)), Store(tmp_path / "data"), AT, environ={}, post=ok_post(), loader=lambda s: (_ for _ in ()).throw(RuntimeError("no model"))) == 1
    assert "transcription failed" in capsys.readouterr().out


def test_the_probe_command_writes_the_measured_order_where_the_transcriber_reads_it(tmp_path, capsys):
    code = SC.run_stage(args_for(tmp_path, "--action", "probe-transcribe"), Store(tmp_path / "data"), AT, environ={"GROQ_API_KEY": "k"}, post=ok_post(), loader=fw_loader())
    out = capsys.readouterr().out
    saved = json.loads((tmp_path / "data" / "_shared" / TR.RANKINGS_FILE).read_text(encoding="utf-8"))
    assert code == 0 and "groq_whisper: CONNECTED" in out and "faster_whisper: CONNECTED" in out and "VibeVoice is not wired" in out
    assert set(saved["ranking"]) == {"groq_whisper", "faster_whisper"} and TR.order(tmp_path / "data" / "_shared")[0] == saved["ranking"][0]


def test_the_stage_tools_workflow_runs_no_model_keeps_the_key_in_the_step_and_saves_with_autostash():
    import yaml
    text = Path(".github/workflows/stage-tools.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert list(wf[True]) == ["workflow_dispatch"] and "ANTHROPIC" not in text
    steps = wf["jobs"]["stage"]["steps"]
    run = next(s for s in steps if s.get("name") == "Run it")
    assert run["env"]["GROQ_API_KEY"] == "${{ secrets.GROQ_API_KEY }}" and "stage --action probe-transcribe" in run["run"] and "stage --action bakeoff" in run["run"]
    save = next(s for s in steps if s.get("name") == "Save the data")
    assert "git pull --rebase --autostash" in save["run"] and "git status --porcelain" in save["run"] and "transcribe_rankings.json" in save["run"]
    assert next(s for s in steps if "faster-whisper" in s.get("run", ""))["continue-on-error"] is True
