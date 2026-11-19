"""Stage 5: transcription. Groq's hosted Whisper first (free tier, one key), Faster-Whisper as the local backup (free, CPU, on the runner), VibeVoice last and NOT wired (the licence of its published
weights is still unverified). The order is the job's own ranked list (data/_shared/transcribe_rankings.json, written by `stage --action probe-transcribe` and the pool retest); never random, never first-available.
Nothing here posts, sends or publishes: a transcript is saved as evidence next to the brand's data and the owner's research reads it."""
import hashlib
import json
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
SEEDED_ORDER = ("groq_whisper", "faster_whisper")
NOT_WIRED = {"vibevoice": "its published weights carry a licence nobody here has verified, so it is not used"}
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


def order(shared_dir):
    """The backends in their MEASURED order (the probe's result), the seeded order until a probe has run. A backend the probe found failing is left last, never dropped."""
    path = Path(shared_dir) / RANKINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ranked = [b for b in data.get("ranking", []) if b in SEEDED_ORDER]
        return tuple(ranked + [b for b in SEEDED_ORDER if b not in ranked])
    except (OSError, ValueError, AttributeError):
        return SEEDED_ORDER


def transcribe(path, *, backends, key="", post=None, loader=None, language=None, clock=time.monotonic):
    """Try the backends in order; the first that answers wins, the rest are the failover. Returns {text, language, segments, backend, seconds, tried: [(backend, why)]} or raises Failure with every reason."""
    content = Path(path).read_bytes()
    tried = []
    for backend in backends:
        t0 = clock()
        try:
            if backend == "groq_whisper":
                out = groq_whisper(content, Path(path).name, key=key, post=post, language=language)
            elif backend == "faster_whisper":
                out = faster_whisper_local(path, loader=loader, language=language)
            else:
                tried.append((backend, NOT_WIRED.get(backend, "unknown backend")))
                continue
        except Failure as exc:
            tried.append((backend, f"{exc.code}: {exc.message}"[:200]))
            continue
        return {**out, "backend": backend, "seconds": round(clock() - t0, 2), "tried": tried}
    raise Failure(contract.UPSTREAM_BROKEN, "no backend could transcribe it: " + "; ".join(f"{b}: {w}" for b, w in tried))


def silent_wav(seconds=1, rate=16000):
    """A tiny valid WAV of silence for the probe: it proves a backend accepts and answers a request, not that it hears speech."""
    import struct
    n = rate * seconds
    data = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


def probe(workdir, *, key="", post=None, loader=None, clock=time.monotonic):
    """Ask every backend once with a second of silence: connected or why not, and how long it took. Returns {ranking: [backends that answered, fastest first], rows: [...]}."""
    path = Path(workdir) / "probe.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(silent_wav())
    rows = []
    for backend in SEEDED_ORDER:
        t0 = clock()
        try:
            transcribe(path, backends=(backend,), key=key, post=post, loader=loader, clock=clock)
            rows.append({"backend": backend, "ok": True, "seconds": round(clock() - t0, 2), "why": ""})
        except Failure as exc:
            rows.append({"backend": backend, "ok": False, "seconds": round(clock() - t0, 2), "why": exc.message[:200]})
    return {"ranking": [r["backend"] for r in sorted((r for r in rows if r["ok"]), key=lambda r: r["seconds"])], "rows": rows}


def save_transcript(store, slug, record, now):
    """Keep the transcript (newest first, nothing replaced) in data/<brand>/extras/transcripts.json and a readable export. The audio itself is never stored."""
    kept = store.read_extra(slug, "transcripts") or {}
    items = [i for i in kept.get("items", []) if i.get("sha") != record["sha"]]
    items.insert(0, {**record, "at": now})
    store.write_extra(slug, "transcripts", {"note": "transcripts of audio the owner's research read; evidence only", "items": items[:200]})
    store.write_export(slug, "transcripts.md", "# Transcripts\n\n" + "\n\n".join(f"## {i.get('source', '')} ({i['at']}, {i['backend']})\n\n{i['text']}" for i in items[:50]))


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def env_key(environ=None):
    return (environ if environ is not None else os.environ).get("GROQ_API_KEY", "").strip()
