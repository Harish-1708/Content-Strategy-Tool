"""Stage 5: transcription. Groq's hosted Whisper first (free tier, one key), Cloudflare Workers AI's Whisper second (the same free Cloudflare account the pool already uses), Faster-Whisper as the local backup
(free, CPU, on the runner, with pinned compatible versions), VibeVoice last and NOT wired (see NOT_WIRED). The order is the job's own ranked list (data/_shared/transcribe_rankings.json, written by `stage --action probe-transcribe` and the pool retest); never random, never first-available.
Nothing here posts, sends or publishes: a transcript is saved as evidence next to the brand's data and the owner's research reads it."""
import hashlib
import json
import re
import os
import time
import uuid
from pathlib import Path

from .ai import clients as cl
from .ai.clients import Failure
from . import contract

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
MAX_BYTES = 24 * 1024 * 1024                                            # Groq's free tier takes files up to 25 MB; a larger one is refused with the reason, never cut
SEEDED_ORDER = ("groq_whisper", "cloudflare_whisper", "faster_whisper", "vibevoice_asr")
SPEECH_TEXT = "This is a test of the transcription chain. One, two, three."
SPEECH_WORDS = ("test", "transcription", "chain", "one", "two", "three")
SUPPORTED = (".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".opus", ".wav", ".webm")             # the file types Groq Whisper accepts; anything else is converted with ffmpeg first
CF_MODEL = "@cf/openai/whisper-large-v3-turbo"
CF_MAX_BYTES = 10 * 1024 * 1024
NOT_WIRED = {}                                                                          # every backend named in this file is wired; VibeVoice runs only once its command is set (see vibevoice_asr)
RANKINGS_FILE = "transcribe_rankings.json"


def multipart(fields, filename, content, mime="application/octet-stream"):
    """(content-type, body bytes) for a form with simple fields and one file."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def groq_whisper(content, filename, *, key, post=None, language=None):
    """One transcript from Groq's Whisper. `post(url, headers, body, timeout)` -> (status, json) is injected in tests. Returns {text, language, segments}."""
    if not key:
        raise Failure(contract.NEED_SETUP, "GROQ_API_KEY is not set, so Groq Whisper is not tried")
    if len(content) > MAX_BYTES:
        raise Failure(contract.BAD_INPUT, f"the audio is {len(content) // 1048576} MB; Groq's free tier takes up to {MAX_BYTES // 1048576} MB")
    fields = {"model": GROQ_MODEL, "response_format": "verbose_json", "temperature": "0"}
    if language:
        fields["language"] = language
    ctype, body = multipart(fields, filename, content)
    post = post or _post
    try:
        status, answer = post(GROQ_URL, {"Authorization": "Bearer " + key, "Content-Type": ctype, "User-Agent": cl.USER_AGENT}, body, 120)
    except Exception as exc:                                                                # noqa: BLE001
        raise Failure(contract.classify_exception(exc)["code"], f"Groq Whisper could not be reached: {str(exc).replace(key, '***')[:160]}") from None
    if status in (401, 403):
        raise Failure(contract.NEED_SETUP, "Groq rejected the key")
    if status in (402, 429):
        raise Failure(contract.RATE_LIMITED, "Groq Whisper's free limit was reached")
    if status != 200 or not isinstance(answer, dict) or "text" not in answer:
        raise Failure(contract.UPSTREAM_BROKEN, f"Groq Whisper answered HTTP {status}: {str((answer or {}).get('error', answer))[:160]}")
    segs = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text")} for s in (answer.get("segments") or []) if isinstance(s, dict)][:400]
    return {"text": str(answer["text"]).strip(), "language": str(answer.get("language") or ""), "segments": segs}


def _post(url, headers, body, timeout):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                          # noqa: S310 - the one fixed https address above
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return exc.code, {"error": "not JSON"}


def cloudflare_whisper(content, *, account, token, post=None):
    """One transcript from Cloudflare Workers AI's Whisper (free allowance on the account the pool already uses). The audio goes as base64 in a JSON body."""
    if not account or not token:
        raise Failure(contract.NEED_SETUP, "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are not both set, so Cloudflare Whisper is not tried")
    if len(content) > CF_MAX_BYTES:
        raise Failure(contract.BAD_INPUT, f"the audio is {len(content) // 1048576} MB; Cloudflare Whisper is given up to {CF_MAX_BYTES // 1048576} MB")
    import base64
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CF_MODEL}"
    body = json.dumps({"audio": base64.b64encode(content).decode("ascii")}).encode("utf-8")
    try:
        status, answer = (post or _post)(url, {"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": cl.USER_AGENT}, body, 120)
    except Exception as exc:                                                                # noqa: BLE001
        raise Failure(contract.classify_exception(exc)["code"], f"Cloudflare Whisper could not be reached: {str(exc).replace(token, '***')[:160]}") from None
    if status in (401, 403):
        raise Failure(contract.NEED_SETUP, "Cloudflare rejected the token")
    if status == 429:
        raise Failure(contract.RATE_LIMITED, "Cloudflare Whisper's free allowance was reached")
    result = (answer or {}).get("result") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(result, dict) or "text" not in result:
        raise Failure(contract.UPSTREAM_BROKEN, f"Cloudflare Whisper answered HTTP {status}: {str((answer or {}).get('errors', answer))[:160]}")
    segs = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text")} for s in (result.get("segments") or []) if isinstance(s, dict)][:400]
    return {"text": str(result["text"]).strip(), "language": str(((result.get("transcription_info") or {}).get("language")) or ""), "segments": segs}


def vibevoice_asr(path, *, command, runner=None):
    """The last backend: Microsoft's VibeVoice-ASR (MIT licence, settled 2026-11-19: the weights and the CPU-only BitNet variant are published under MIT). It runs as a command on the runner (the CPU build of
    microsoft/VibeASR.cpp) and prints the transcript on standard output: `{audio}` in the command is replaced by the file. With no command set it says so and the next backend is used."""
    import subprocess
    if not command or "{audio}" not in command:
        broke = os.environ.get("VIBEASR_INSTALL_ERROR", "").strip()
        raise Failure(contract.NEED_SETUP, "VIBEASR_COMMAND is not set (the command of the CPU build of VibeVoice-ASR, with {audio} for the file), so VibeVoice is not tried"
                      + (f"; the workflow's own install of it FAILED: {broke[:160]}" if broke else ""))
    argv = [part.replace("{audio}", str(path)) for part in command.split()]
    try:
        done = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=900)
    except Exception as exc:                                                                # noqa: BLE001
        raise Failure(contract.UPSTREAM_BROKEN, f"VibeVoice could not run: {type(exc).__name__}: {str(exc)[:120]}") from None
    text = (getattr(done, "stdout", "") or "").strip()
    if getattr(done, "returncode", 1) != 0 or not text:
        raise Failure(contract.UPSTREAM_BROKEN, f"VibeVoice exited {getattr(done, 'returncode', '?')} with {len(text)} characters: " + (getattr(done, "stderr", "") or "")[:120])
    return {"text": text, "language": "", "segments": []}


def to_supported(path, workdir, *, runner=None, which=None):
    """A file Groq will accept. A file with a supported extension and a modest size is used as it is; anything else (the unnamed download of a TikTok video, a .bin, a big video) is turned into a small mono MP3 with
    ffmpeg (a mono WAV if this ffmpeg has no MP3 encoder). Raises Failure with the reason when it cannot."""
    import shutil
    import subprocess
    path = Path(path)
    if path.suffix.lower() in SUPPORTED and path.stat().st_size <= 8 * 1024 * 1024:
        return path
    if not (which or shutil.which)("ffmpeg"):
        raise Failure(contract.NEED_SETUP, f"{path.name} is not a type Groq accepts and ffmpeg is not installed to convert it")
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    run = runner or subprocess.run
    problems = []
    for suffix, codec in ((".mp3", ["-c:a", "libmp3lame", "-b:a", "48k"]), (".wav", ["-c:a", "pcm_s16le"])):
        out = work / (path.stem + "-audio" + suffix)
        done = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", *codec, str(out)], capture_output=True, text=True, timeout=300)
        if getattr(done, "returncode", 1) == 0 and out.is_file() and out.stat().st_size > 0:
            return out
        problems.append((getattr(done, "stderr", "") or "")[:120])
    raise Failure(contract.BAD_INPUT, "ffmpeg could not turn the download into audio: " + " | ".join(problems))


def faster_whisper_local(path, *, model_size="small", loader=None, language=None):
    """The local backup: Faster-Whisper on CPU (int8). `loader(model_size)` returns an object with .transcribe(path, ...) (injected in tests). Raises Failure when the library is not installed."""
    if loader is None:
        try:
            from faster_whisper import WhisperModel                                          # noqa: PLC0415 - optional: installed by the workflow when this backend is wanted
        except ImportError:
            raise Failure(contract.NEED_SETUP, "faster-whisper is not installed here (the workflow installs it with pip when the backup is wanted)") from None

        def loader(size):
            return WhisperModel(size, device="cpu", compute_type="int8")
    try:
        segments, info = loader(model_size).transcribe(str(path), language=language, vad_filter=True)
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    except Exception as exc:                                                                # noqa: BLE001
        raise Failure(contract.UPSTREAM_BROKEN, f"Faster-Whisper failed: {str(exc)[:160]}") from None
    return {"text": " ".join(s["text"] for s in segs).strip(), "language": str(getattr(info, "language", "") or ""), "segments": segs[:400]}


def words_of(text):
    import re
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def word_error_rate(reference, heard):
    """The share of the reference's words the transcript got wrong (substitutions, drops and insertions over the reference length): 0.0 is perfect. Works for any sentence and any word: nothing is written for one word."""
    ref, hyp = words_of(reference), words_of(heard)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return round(prev[-1] / len(ref), 3)


MAX_PROBE_WER = 0.34                         # a backend that gets more than a third of a plain spoken sentence wrong is not used
CHECK_AGREEMENT = 0.80                       # two backends that agree on less than this share of the words: the transcript is flagged for a person to read
CROSS_CHECKERS = ("groq_whisper", "cloudflare_whisper")           # the quick hosted backends that give the second opinion


def agreement(first, second):
    """(share of words the two transcripts agree on, [(heard by the first, heard by the second)] for the first few differences)."""
    import difflib
    a, b = words_of(first), words_of(second)
    if not a and not b:
        return 1.0, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    same = sum(block.size for block in sm.get_matching_blocks())
    diffs = [(" ".join(a[i1:i2]), " ".join(b[j1:j2])) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"][:6]
    return round(same / max(len(a), len(b)), 3), diffs


def cross_check(path, primary, backends, **kw):
    """A second opinion on a transcript: the same audio through the next quick hosted backend (never the one that wrote it). Returns {'with', 'agreement', 'differences', 'flag'}, or {'with': '', 'why': ...} when none could be asked.
    A mishearing that one backend makes (VibeVoice heard 'subscription' for 'transcription') is caught here because the other backend disagrees on that word, whichever word it is."""
    last = ""
    for other in [b for b in backends if b in CROSS_CHECKERS and b != primary["backend"]]:
        try:
            second = transcribe(path, backends=(other,), **kw)
        except Failure as exc:
            last = f"{other}: {exc.message[:100]}"
            continue
        share, diffs = agreement(primary["text"], second["text"])
        return {"with": other, "agreement": share, "differences": diffs, "flag": share < CHECK_AGREEMENT}
    return {"with": "", "why": "no second hosted backend could be asked" + (f" ({last})" if last else "")}


def order(shared_dir):
    """The backends in their MEASURED order (the probe's result), the seeded order until a probe has run. A backend the probe found failing is left last, never dropped."""
    path = Path(shared_dir) / RANKINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ranked = [b for b in data.get("ranking", []) if b in SEEDED_ORDER]
        return tuple(ranked + [b for b in SEEDED_ORDER if b not in ranked])
    except (OSError, ValueError, AttributeError):
        return SEEDED_ORDER


LATIN_LANGUAGES = ("en", "es", "fr", "de", "it", "pt", "nl", "sv", "da", "no", "fi", "pl", "tr", "id")


def looping(text):
    """True when a transcript is a LOOP: a phrase of 2 to 8 words repeated four or more times in a row, or very little variety in a long text (fewer than one word in five is new). Whisper turns music and noise into the same
    phrase over and over ('a little bit of a drink' x 40, Research tools #35): right language, still a hallucination. Language-independent."""
    words = re.findall(r"\w+", str(text).lower())
    if len(words) >= 12:
        for n in range(2, 9):
            for i in range(0, len(words) - 4 * n + 1):
                if words[i:i + n] * 4 == words[i:i + 4 * n]:
                    return True
    return len(words) >= 30 and len(set(words)) / len(words) < 0.2


def plausible(text, language):
    """False when a transcript is in the wrong SCRIPT for the language expected: more than 30% of its letters are not Latin letters although the language is a Latin-script one (Whisper answered a music-and-noise English
    video with a Japanese sentence: a hallucination, not a transcript). An unknown language or a short text is not judged."""
    if not language or str(language).lower()[:2] not in LATIN_LANGUAGES:
        return True
    letters = [c for c in str(text) if c.isalpha()]
    if len(letters) < 8:
        return True
    latin = sum(1 for c in letters if ord(c) < 0x250 or 0x1E00 <= ord(c) <= 0x1EFF)
    return latin / len(letters) >= 0.7


def transcribe(path, *, backends, key="", post=None, loader=None, language=None, clock=time.monotonic, cloudflare=("", ""), vibevoice="", runner=None, workdir=None, which=None):
    """Try the backends in order; the first that answers wins, the rest are the failover. Returns {text, language, segments, backend, seconds, tried: [(backend, why)]} or raises Failure with every reason."""
    tried = []
    cache = {}

    def hosted_file():
        """The audio the HOSTED backends take (Groq, Cloudflare): the file itself when its type is one they accept, else an ffmpeg conversion, made once and only when a hosted backend is reached. A missing ffmpeg is that
        backend's failure, never the whole run's: Faster-Whisper reads the original file through its own decoder (the Stage tools #5 run died here before any backend was tried)."""
        if "path" not in cache and "failure" not in cache:
            try:
                import tempfile
                cache["path"] = to_supported(path, workdir or tempfile.mkdtemp(prefix="tr-"), runner=runner, which=which)
            except Failure as exc:
                cache["failure"] = exc
        if "failure" in cache:
            raise cache["failure"]
        return cache["path"]
    for backend in backends:
        t0 = clock()
        try:
            if backend == "groq_whisper":
                src = hosted_file()
                out = groq_whisper(Path(src).read_bytes(), Path(src).name, key=key, post=post, language=language)
            elif backend == "cloudflare_whisper":
                out = cloudflare_whisper(Path(hosted_file()).read_bytes(), account=cloudflare[0], token=cloudflare[1], post=post)
            elif backend == "faster_whisper":
                out = faster_whisper_local(path, loader=loader, language=language)
            elif backend == "vibevoice_asr":
                out = vibevoice_asr(path, command=vibevoice, runner=runner)
            else:
                tried.append((backend, NOT_WIRED.get(backend, "unknown backend")))
                continue
            if looping(out.get("text", "")):
                raise Failure(contract.UPSTREAM_BROKEN, "the transcript is a repetitive loop (the same phrase over and over): a hallucination on music or noise, not speech; the next backend is tried")
            if not plausible(out.get("text", ""), language):
                raise Failure(contract.UPSTREAM_BROKEN, f"the transcript is not in the expected language ({language}): most of its letters are not Latin, which is a hallucination on music or noise, not speech; the next backend is tried")
        except Failure as exc:
            tried.append((backend, f"{exc.code}: {exc.message}"[:200]))
            continue
        converted = Path(cache["path"]).name if "path" in cache and Path(cache["path"]) != Path(path) else ""
        return {**out, "backend": backend, "seconds": round(clock() - t0, 2), "tried": tried, "converted": converted}
    raise Failure(contract.UPSTREAM_BROKEN, "no backend could transcribe it: " + "; ".join(f"{b}: {w}" for b, w in tried))


def selfcheck(workdir):
    """(ok, line): proves the DISCARD path with no network and no key, in every probe run: a looping transcript and a wrong-script transcript from the first backend are thrown away and the next backend's answer is used, and a good
    transcript is kept. The stand-ins are scripted: this proves our code, which is what a real transcript can only exercise by luck."""
    path = Path(workdir) / "selfcheck.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(silent_wav())
    good = "this is a short and perfectly ordinary english sentence about the product"
    cases = (("a repetitive loop", "a little bit of a drink " * 30, "en"), ("a wrong-script transcript", "食べ物の絵文字のチャレンジの始まりを食べています", "en"), ("a good transcript", good, "en"))
    seen = []
    for label, first, lang in cases:
        def post(url, headers, body, timeout, _t=first):
            return 200, {"text": _t, "language": lang, "segments": [], "result": {"text": _t, "segments": []}}
        class Model:
            def transcribe(self, p, language=None, vad_filter=True):
                from types import SimpleNamespace
                return [SimpleNamespace(start=0.0, end=2.0, text=" " + good + " ")], SimpleNamespace(language="en")
        try:
            out = transcribe(path, backends=("groq_whisper", "faster_whisper"), key="selfcheck", post=post, loader=lambda size: Model(), language="en", workdir=workdir, which=lambda n: n, runner=lambda *a, **k: None)
        except Exception as exc:                                                                   # noqa: BLE001
            return False, f"discard-path self-check: FAILED on {label}: {exc}"
        expect = "groq_whisper" if label == "a good transcript" else "faster_whisper"
        if out["backend"] != expect or (label != "a good transcript" and not out["tried"]):
            return False, f"discard-path self-check: FAILED on {label}: answered by {out['backend']}, expected {expect}"
        seen.append(label)
    return True, "discard-path self-check: PASSED (" + "; ".join(seen[:2]) + " from the first backend were thrown away and the next backend answered; " + seen[2] + " was kept)"


def silent_wav(seconds=1, rate=16000):
    """A tiny valid WAV of silence for the probe: it proves a backend accepts and answers a request, not that it hears speech."""
    import struct
    n = rate * seconds
    data = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


def probe(workdir, *, key="", post=None, loader=None, clock=time.monotonic, cloudflare=("", ""), vibevoice="", runner=None):
    """Ask every backend once with a second of silence: connected or why not, and how long it took. Returns {ranking: [backends that answered, fastest first], rows: [...]}."""
    path = Path(workdir) / "probe.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    speech = Path(workdir) / "probe_speech.wav"                   # a spoken sentence the workflow makes with espeak-ng: with it the probe checks that each backend really TRANSCRIBES, not only that it answers
    spoken = speech.is_file() and speech.stat().st_size > 1000
    if spoken:
        path = speech
    else:
        path.write_bytes(silent_wav())
    rows = []
    for backend in SEEDED_ORDER:
        t0 = clock()
        try:
            out = transcribe(path, backends=(backend,), key=key, post=post, loader=loader, clock=clock, cloudflare=cloudflare, vibevoice=vibevoice, runner=runner)
            heard = " ".join(str(out.get("text", "")).split())[:120]
            wer = word_error_rate(SPEECH_TEXT, heard) if spoken else None
            if spoken and (not any(w in heard.lower() for w in SPEECH_WORDS) or wer > MAX_PROBE_WER):
                rows.append({"backend": backend, "ok": False, "seconds": round(clock() - t0, 2), "wer": wer, "why": f"it answered but got {int(round((wer or 1) * 100))}% of the spoken sentence's words wrong (more than {int(MAX_PROBE_WER * 100)}% is refused): heard {heard!r}", "heard": heard})
            else:
                rows.append({"backend": backend, "ok": True, "seconds": round(clock() - t0, 2), "wer": wer, "why": "", "heard": heard if spoken else ""})
        except Failure as exc:
            rows.append({"backend": backend, "ok": False, "seconds": round(clock() - t0, 2), "why": exc.message[:200], "heard": ""})
    return {"ranking": [r["backend"] for r in sorted((r for r in rows if r["ok"]), key=lambda r: ((r.get("wer") or 0.0), r["seconds"]))], "rows": rows}          # the most accurate first, the fastest among equals: a backend that mishears a word ranks below one that does not


def save_transcript(store, slug, record, now):
    """Keep the transcript (newest first, nothing replaced) in data/<brand>/extras/transcripts.json and a readable export. The audio itself is never stored."""
    kept = store.read_extra(slug, "transcripts") or {}
    items = [i for i in kept.get("items", []) if i.get("sha") != record["sha"]]
    items.insert(0, {**record, "at": now})
    store.write_extra(slug, "transcripts", {"note": "transcripts of audio the owner's research read; evidence only", "items": items[:200]})
    store.write_export(slug, "transcripts.md", "# Transcripts\n\n" + "\n\n".join(f"## {i.get('source', '')} ({i['at']}, {i['backend']})\n\n{i['text']}" for i in items[:50]))


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def env_cloudflare(environ=None):
    env = environ if environ is not None else os.environ
    return env.get("CLOUDFLARE_ACCOUNT_ID", "").strip(), env.get("CLOUDFLARE_API_TOKEN", "").strip()


def env_vibevoice(environ=None):
    return (environ if environ is not None else os.environ).get("VIBEASR_COMMAND", "").strip()


def env_key(environ=None):
    return (environ if environ is not None else os.environ).get("GROQ_API_KEY", "").strip()
