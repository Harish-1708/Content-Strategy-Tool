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
        return 200, {"text": f" {text} ", "language": language, "segments": [{"start": 0.0, "end": 1.5, "text": text}], "result": {"text": f" {text} ", "transcription_info": {"language": language}, "segments": [{"start": 0.0, "end": 1.5, "text": text}]}}
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
    assert "GROQ_API_KEY is not set" in str(err.value) and "unknown backend" in str(err.value) and "vibevoice" in str(err.value)


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
    assert TR.order(tmp_path) == TR.SEEDED_ORDER == ("groq_whisper", "cloudflare_whisper", "faster_whisper", "vibevoice_asr")
    (tmp_path / TR.RANKINGS_FILE).write_text(json.dumps({"ranking": ["faster_whisper"]}), encoding="utf-8")
    assert TR.order(tmp_path) == ("faster_whisper", "groq_whisper", "cloudflare_whisper", "vibevoice_asr")
    (tmp_path / TR.RANKINGS_FILE).write_text("not json", encoding="utf-8")
    assert TR.order(tmp_path) == TR.SEEDED_ORDER
    (tmp_path / TR.RANKINGS_FILE).write_text(json.dumps({"ranking": ["vibevoice", "groq_whisper"]}), encoding="utf-8")
    assert TR.order(tmp_path) == ("groq_whisper", "cloudflare_whisper", "faster_whisper", "vibevoice_asr")


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
    res = TR.probe(tmp_path, key="k", post=post, loader=loader, clock=lambda: tick[0], cloudflare=("acct", "tok"))
    assert res["ranking"] == ["faster_whisper", "groq_whisper", "cloudflare_whisper"] and all(r["ok"] for r in res["rows"][:3]) and [r["seconds"] for r in res["rows"][:3]] == [5, 5, 1] and res["rows"][3]["backend"] == "vibevoice_asr" and not res["rows"][3]["ok"]
    bad = TR.probe(tmp_path, key="", post=ok_post(), loader=None, clock=lambda: 0)
    assert [r["ok"] for r in bad["rows"]][:2] == [False, False] and bad["ranking"] in ([], ["faster_whisper"])


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
    assert code == 0 and "groq_whisper: CONNECTED" in out and "faster_whisper: CONNECTED" in out and "VibeVoice runs only when VIBEASR_COMMAND is set" in out
    assert set(saved["ranking"]) == {"groq_whisper", "faster_whisper"} and "cloudflare_whisper: FAILED" in out and TR.order(tmp_path / "data" / "_shared")[0] == saved["ranking"][0]


def test_the_stage_tools_workflow_runs_no_model_keeps_the_key_in_the_step_and_saves_with_autostash():
    import yaml
    text = Path(".github/workflows/stage-tools.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert list(wf[True]) == ["workflow_dispatch"] and "ANTHROPIC" not in text
    steps = wf["jobs"]["stage"]["steps"]
    run = next(s for s in steps if s.get("name") == "Run it")
    assert run["env"]["GROQ_API_KEY"] == "${{ secrets.GROQ_API_KEY }}" and "stage --action probe-transcribe" in run["run"] and "stage --action bakeoff" in run["run"]
    save = next(s for s in steps if s.get("name") == "Save the data")
    assert "bash .github/scripts/push-with-retry.sh" in save["run"] and "--autostash" in Path(".github/scripts/push-with-retry.sh").read_text(encoding="utf-8") and "git status --porcelain" in save["run"] and "transcribe_rankings.json" in save["run"]
    assert next(s for s in steps if "faster-whisper" in s.get("run", ""))["continue-on-error"] is True


def test_cloudflare_whisper_is_the_second_backend_and_each_of_its_failures_is_named_and_the_token_never_shown(tmp_path):
    path = audio(tmp_path)
    post = ok_post("from cloudflare")
    out = TR.transcribe(path, backends=("cloudflare_whisper",), cloudflare=("acct1", "cf-token"), post=post)
    url, headers, body = post.calls[0]
    assert out["backend"] == "cloudflare_whisper" and out["text"] == "from cloudflare" and out["language"] == "en" and url == "https://api.cloudflare.com/client/v4/accounts/acct1/ai/run/@cf/openai/whisper-large-v3-turbo"
    assert headers["Authorization"] == "Bearer cf-token" and json.loads(body)["audio"]                                           # the audio is sent as base64 in a JSON body
    out2 = TR.transcribe(path, backends=("groq_whisper", "cloudflare_whisper", "faster_whisper"), key="k", post=lambda u, h, b, t: (429, {}) if "groq" in u else ok_post("cf second")(u, h, b, t), cloudflare=("a", "t"), loader=fw_loader())
    assert out2["backend"] == "cloudflare_whisper" and out2["tried"][0][0] == "groq_whisper"                                      # the second backend answers when the first is limited
    def code(status, body=None):
        return lambda u, h, b, t: (status, body if body is not None else {})
    for status, expected in ((401, contract.NEED_SETUP), (403, contract.NEED_SETUP), (429, contract.RATE_LIMITED), (500, contract.UPSTREAM_BROKEN)):
        with pytest.raises(Failure) as err:
            TR.cloudflare_whisper(b"x", account="a", token="cf-token", post=code(status, {"errors": ["boom"]}))
        assert err.value.code == expected
    def boom(u, h, b, t):
        raise ConnectionError("could not reach it with Bearer cf-token")
    with pytest.raises(Failure) as err:
        TR.cloudflare_whisper(b"x", account="a", token="cf-token", post=boom)
    assert "cf-token" not in str(err.value) and "***" in str(err.value)
    with pytest.raises(Failure) as err:
        TR.cloudflare_whisper(b"x" * (TR.CF_MAX_BYTES + 1), account="a", token="t", post=ok_post())
    assert err.value.code == contract.BAD_INPUT
    with pytest.raises(Failure) as err:
        TR.cloudflare_whisper(b"x", account="", token="t", post=ok_post())
    assert err.value.code == contract.NEED_SETUP and TR.env_cloudflare({"CLOUDFLARE_ACCOUNT_ID": " a ", "CLOUDFLARE_API_TOKEN": "t"}) == ("a", "t")


class Ffmpeg:
    """A stand-in for the ffmpeg command: it writes the output file it is asked for, or fails for the extensions it is told to."""
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def __call__(self, argv, capture_output, text, timeout):
        self.calls.append(argv)
        out = Path(argv[-1])
        if out.suffix in self.fail:
            return SimpleNamespace(returncode=1, stderr="Unknown encoder 'libmp3lame'")
        out.write_bytes(b"AUDIO" * 100)
        return SimpleNamespace(returncode=0, stderr="")


def test_a_download_with_an_unsupported_name_or_a_big_video_is_converted_to_audio_a_supported_one_is_left_alone(tmp_path):
    ok = audio(tmp_path, "clip.wav")
    assert TR.to_supported(ok, tmp_path / "w", runner=Ffmpeg(), which=lambda n: n) == ok                                            # a small file of a known type is sent as it is
    blob = tmp_path / "download.bin"
    blob.write_bytes(b"\x00\x00\x00 ftypmp42" + b"x" * 500)
    ff = Ffmpeg()
    out = TR.to_supported(blob, tmp_path / "w", runner=ff, which=lambda n: n)
    assert out.suffix == ".mp3" and out.name == "download-audio.mp3" and ff.calls[0][:3] == ["ffmpeg", "-y", "-loglevel"] and "-vn" in ff.calls[0] and "libmp3lame" in ff.calls[0]
    wav = TR.to_supported(blob, tmp_path / "w2", runner=Ffmpeg(fail=(".mp3",)), which=lambda n: n)
    assert wav.suffix == ".wav"                                                                                                   # no MP3 encoder: a mono WAV instead
    big = tmp_path / "video.mp4"
    big.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    assert TR.to_supported(big, tmp_path / "w3", runner=Ffmpeg(), which=lambda n: n).suffix == ".mp3"                               # a big video is reduced to its audio
    with pytest.raises(Failure) as err:
        TR.to_supported(blob, tmp_path / "w4", runner=Ffmpeg(fail=(".mp3", ".wav")), which=lambda n: n)
    assert err.value.code == contract.BAD_INPUT and "ffmpeg could not turn the download into audio" in err.value.message
    with pytest.raises(Failure) as err:
        TR.to_supported(blob, tmp_path / "w5", runner=Ffmpeg(), which=lambda n: None)
    assert err.value.code == contract.NEED_SETUP and "ffmpeg is not installed" in err.value.message


def test_the_stage_command_converts_an_unsupported_download_before_it_is_sent_and_says_so(tmp_path, capsys):
    blob = tmp_path / "tiktok-download"
    blob.write_bytes(b"\x00\x00\x00 ftypmp42" + b"x" * 500)
    sent = []

    def post(url, headers, body, timeout):
        sent.append(body)
        return ok_post("converted words")(url, headers, body, timeout)
    code = SC.run_stage(args_for(tmp_path, "--action", "transcribe", "--brand", "swoveralls", "--file", str(blob)), Store(tmp_path / "data"), AT, environ={"GROQ_API_KEY": "g"}, post=post, runner=Ffmpeg())
    out = capsys.readouterr().out
    assert code == 0 and "was converted to tiktok-download-audio.mp3" in out and b'filename="tiktok-download-audio.mp3"' in sent[0]          # Groq is given a name and a type it accepts


def test_the_stage_tools_workflow_installs_a_pinned_compatible_faster_whisper_and_passes_the_cloudflare_secrets():
    text = Path(".github/workflows/stage-tools.yml").read_text(encoding="utf-8")
    assert 'pip install "faster-whisper==1.1.1" "av==13.1.0"' in text and "pip install faster-whisper\n" not in text
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}" in text and "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in text


def test_vibevoice_runs_as_a_command_only_when_one_is_set_and_otherwise_is_skipped_with_the_reason(tmp_path):
    path = audio(tmp_path)
    ran = []

    def runner(argv, capture_output, text, timeout):
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout=" vibe words \n", stderr="")
    out = TR.transcribe(path, backends=("vibevoice_asr",), vibevoice="vibeasr --cpu {audio}", runner=runner)
    assert out["backend"] == "vibevoice_asr" and out["text"] == "vibe words" and ran[0] == ["vibeasr", "--cpu", str(path)]
    with pytest.raises(Failure) as err:
        TR.transcribe(path, backends=("vibevoice_asr",))
    assert "VIBEASR_COMMAND is not set" in str(err.value)
    for bad in (SimpleNamespace(returncode=2, stdout="", stderr="no model"), SimpleNamespace(returncode=0, stdout="  ", stderr="")):
        with pytest.raises(Failure) as err:
            TR.vibevoice_asr(path, command="v {audio}", runner=lambda *a, **k: bad)
        assert err.value.code == contract.UPSTREAM_BROKEN
    called = []
    with pytest.raises(Failure) as err:
        TR.vibevoice_asr(path, command="no-placeholder-here", runner=lambda *a, **k: called.append(a))
    assert err.value.code == contract.NEED_SETUP and called == []                                                                   # a command with no {audio} is never run
    assert TR.env_vibevoice({"VIBEASR_COMMAND": " v {audio} "}) == "v {audio}" and "MIT" in Path("docs/ai/transcription.md").read_text(encoding="utf-8")
