"""OpenMontage patterns (Batch 3): the four ideas taken from OpenMontage, written for this tool and nothing else copied.
  1. SCORED PROVIDER CHOICE: when several providers could do a step, each is scored on measured quality, headroom (how much of its own allowance is left), speed and cost, and the best healthy one is chosen, with
     the breakdown kept so the choice can be audited. The per-job ranked lists are the quality input; the usage ledger is the headroom input.
  2. PIPELINE MANIFEST: every multi-step run (a draft video, a render, a transcription chain) writes a manifest: each step, its tool, what went in (a hash), what came out, which provider answered, how long it
     took, what it cost, and whether it passed its gate.
  3. QUALITY GATES: a step's output is checked by code before the next step starts (duration, size, resolution, audio, text); a failed gate stops the pipeline and says which check failed.
  4. A CLIP-INDEXED STOCK CORPUS: pictures the brand may use (the brand's own site media, the licence-safe Openverse and Commons finds) are indexed by what they SHOW, with a CLIP model, so a script line can be matched
     to a picture by meaning instead of by keyword. The embedder is injected: tests use a fake one, a runner installs sentence-transformers' clip-ViT-B-32."""
import hashlib
import json
import math
import time
from pathlib import Path

DIMENSIONS = ("quality", "headroom", "reliability", "speed", "cost", "capability", "terms")
WEIGHTS = {"quality": 0.35, "headroom": 0.20, "reliability": 0.15, "speed": 0.10, "cost": 0.08, "capability": 0.07, "terms": 0.05}          # the seven dimensions of the scored ranking; they add up to 1
SPEED_CEILING_S, COST_CEILING_USD = 60.0, 0.10


def parts(candidate):
    """The seven dimensions of one candidate, each 0 to 1 (1 is best):
      quality      how good it measured at this job (the retest's rank);
      headroom     how much of its own allowance is left today;
      reliability  how rarely it failed today (1 minus the failure rate);
      speed        how fast it answers (1 minus the average seconds over a minute);
      cost         1 for a free route, lower as its price per call rises;
      capability   whether its models can do what the job needs (reasoning, long context, JSON);
      terms        how settled its terms are (verified or documented 1, an owner decision 0.8, unverified 0.5)."""
    def clamp(x):
        return min(1.0, max(0.0, float(x)))
    return {"quality": clamp(candidate.get("quality", 0)), "headroom": clamp(candidate.get("headroom", 0)), "reliability": clamp(candidate.get("reliability", 1.0)),
            "speed": 1.0 - min(1.0, max(0.0, float(candidate.get("latency_s", 0))) / SPEED_CEILING_S), "cost": 1.0 - min(1.0, max(0.0, float(candidate.get("cost_usd", 0))) / COST_CEILING_USD),
            "capability": clamp(candidate.get("capability", 1.0)), "terms": clamp(candidate.get("terms", 1.0))}


def score(candidate, weights=None):
    """0 to 1 for one candidate: the weighted sum of its seven dimensions. An unhealthy one scores 0 whatever its numbers say."""
    w = weights or WEIGHTS
    if not candidate.get("healthy", True):
        return 0.0
    p = parts(candidate)
    return round(sum(w[d] * p[d] for d in DIMENSIONS), 4)


def choose(candidates, weights=None):
    """(best, ranking): the ranking is every candidate with its score and the four parts, best first; best is None when nobody healthy is left. Ties keep the order given (the measured order)."""
    ranking = []
    for i, c in enumerate(candidates):
        ranking.append({**c, "score": score(c, weights), "parts": parts(c), "order": i})
    ranking.sort(key=lambda r: (-r["score"], r["order"]))
    best = next((r for r in ranking if r["score"] > 0), None)
    return best, ranking


def candidates_for_job(router, lane_name, now, measured_quality=None, data_class="public"):
    """The providers that could take a job right now, as scored candidates: quality from the measured rank (first = 1.0, then falling), headroom from the usage ledger, healthy from the router's own eligibility."""
    lane = router.lanes[lane_name]
    rows = {}
    for rank, r in enumerate(router._routes(lane, now)):
        pid = r["provider"]
        if pid in rows:
            continue
        ok, why = router._eligible(lane, r, data_class, now)
        provider = router.registry.providers.get(pid)
        load = router.usage.load(provider, now) if provider is not None and pid != "claude" else 0.0
        q = (measured_quality or {}).get(pid, max(0.2, 1.0 - 0.08 * rank))
        rows[pid] = {"name": pid, "model": r["model"], "quality": q, "headroom": max(0.0, 1.0 - load), "latency_s": 0.0, "cost_usd": 0.0 if not getattr(provider, "paid", False) else 0.05, "healthy": ok, "why": why}
    return list(rows.values())


class Manifest:
    """The record of one multi-step run. Steps are appended as they start and closed as they finish; the manifest is written to disk after every change so a run that dies still leaves its trail."""
    def __init__(self, run, path=None, now=""):
        self.data = {"run": run, "started": now, "steps": [], "status": "running"}
        self.path = Path(path) if path else None
        self._save()

    def _save(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def start(self, step, tool, inputs=None, provider=""):
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16] if inputs is not None else ""
        entry = {"step": step, "tool": tool, "input_hash": digest, "provider": provider, "status": "running", "t0": time.monotonic()}
        self.data["steps"].append(entry)
        self._save()
        return entry

    def finish(self, entry, *, output="", gate=None, cost_usd=0.0, provider=None, ok=True, note=""):
        entry.update(status="ok" if ok else "failed", output=str(output), gate=gate, cost_usd=round(float(cost_usd), 6), seconds=round(time.monotonic() - entry.pop("t0", time.monotonic()), 3), note=note)
        if provider:
            entry["provider"] = provider
        if not ok:
            self.data["status"] = "stopped"
        self._save()
        return entry

    def close(self, now=""):
        if self.data["status"] == "running":
            self.data["status"] = "complete"
        self.data["ended"] = now
        self.data["cost_usd"] = round(sum(s.get("cost_usd", 0.0) for s in self.data["steps"]), 6)
        self._save()
        return self.data


def quality_gate(measured, checks):
    """(passed, failures). `measured` is what was found about the output ({duration_s, width, height, bytes, has_audio, text_chars}); `checks` maps a check name to its limit:
    min_duration_s, max_duration_s, width, height, min_bytes, max_bytes, has_audio, min_text_chars. A check whose value was not measured fails (never assumed)."""
    failures = []
    for name, limit in checks.items():
        key = {"min_duration_s": "duration_s", "max_duration_s": "duration_s", "width": "width", "height": "height", "min_bytes": "bytes", "max_bytes": "bytes", "has_audio": "has_audio", "min_text_chars": "text_chars"}.get(name)
        if key is None:
            failures.append(f"{name}: not a known check")
            continue
        got = measured.get(key)
        if got is None:
            failures.append(f"{name}: {key} was not measured")
        elif name.startswith("min_") and got < limit:
            failures.append(f"{name}: {got} is below {limit}")
        elif name.startswith("max_") and got > limit:
            failures.append(f"{name}: {got} is above {limit}")
        elif name in ("width", "height") and got != limit:
            failures.append(f"{name}: {got} is not {limit}")
        elif name == "has_audio" and bool(got) != bool(limit):
            failures.append(f"has_audio: expected {bool(limit)}")
    return not failures, failures


def probe_video(path, *, runner=None):
    """What ffprobe says about a video file: {duration_s, width, height, bytes, has_audio}. Raises RuntimeError with the reason when it cannot."""
    import subprocess
    run = runner or subprocess.run
    done = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration,size", "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if getattr(done, "returncode", 1) != 0:
        raise RuntimeError("ffprobe could not read it: " + (getattr(done, "stderr", "") or "")[:120])
    info = json.loads(done.stdout or "{}")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"duration_s": round(float((info.get("format") or {}).get("duration") or 0), 2), "width": video.get("width"), "height": video.get("height"),
            "bytes": int((info.get("format") or {}).get("size") or 0), "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


# ---- the CLIP-indexed stock corpus --------------------------------------------------------------------------------------------------------------------------------
def cosine(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


class StockCorpus:
    """pictures the brand may use, indexed by what they show. items: [{id, url, title, license, credit, vector}]. Saved as one JSON file (a few hundred pictures: small)."""
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except ValueError:
            data = {}
        self.items = data.get("items", []) if isinstance(data, dict) else []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"note": "CLIP-indexed stock corpus: the vector of each picture is what it shows", "model": "clip-ViT-B-32", "items": self.items}, indent=None), encoding="utf-8")

    def add(self, asset, vector):
        self.items = [i for i in self.items if i["id"] != asset["id"]]
        self.items.append({"id": asset["id"], "url": asset.get("url", ""), "title": asset.get("title", ""), "license": asset.get("license", ""), "credit": asset.get("attribution", ""), "vector": [round(float(x), 5) for x in vector]})

    def search(self, text_vector, k=5, floor=0.18):
        """The k pictures whose vector is closest to the text's, best first, each with its cosine score; pictures under `floor` are not matches."""
        scored = sorted(((cosine(text_vector, i["vector"]), i) for i in self.items), key=lambda p: -p[0])
        return [{**{k_: v for k_, v in i.items() if k_ != "vector"}, "score": round(s, 4)} for s, i in scored[:k] if s >= floor]


def index_assets(assets, corpus, embed_image, fetch):
    """Index the pictures in `assets` ([{url, thumbnail, title, ...}], as assets.json keeps them) that the corpus does not hold yet. `fetch(url)` returns the picture's bytes, `embed_image(bytes)` its vector. Returns
    (indexed, skipped_with_reasons); a picture that cannot be fetched or embedded is skipped and said so, never guessed."""
    done, skipped = 0, []
    for a in assets:
        url = a.get("thumbnail") or a.get("url")
        asset_id = hashlib.sha1((a.get("url") or url or "").encode("utf-8")).hexdigest()[:12]
        if not url or any(i["id"] == asset_id for i in corpus.items):
            continue
        try:
            corpus.add({**a, "id": asset_id}, embed_image(fetch(url)))
            done += 1
        except Exception as exc:                                                                    # noqa: BLE001
            skipped.append((a.get("title") or url, f"{type(exc).__name__}: {str(exc)[:80]}"))
    return done, skipped


def clip_embedders():
    """(embed_text, embed_image) from sentence-transformers' clip-ViT-B-32, loaded once. Raises ImportError with the install line when the library is not on this machine."""
    try:
        from sentence_transformers import SentenceTransformer
        from PIL import Image
        import io
    except ImportError:
        raise ImportError("pip install sentence-transformers pillow  (the CLIP model clip-ViT-B-32 downloads on first use)") from None
    model = SentenceTransformer("clip-ViT-B-32")
    return (lambda text: model.encode([text])[0].tolist()), (lambda data: model.encode([Image.open(io.BytesIO(data)).convert("RGB")])[0].tolist())
