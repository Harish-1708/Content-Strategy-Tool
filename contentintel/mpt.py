"""Batch 3: MoneyPrinterTurbo (MPT), the open-source short-video generator, driven through its HTTP API from briefs. BUILT FOR A HOST AND SWITCHED OFF.
MPT is a web application: it has to be RUNNING somewhere (Cloud Run is the planned host, see docs/platform/mpt-cloud-run.md). A GitHub job starts, runs and stops, so it cannot host it, and cookies cannot help (there is
no site to log into: it is software you install). So nothing here runs until the owner says a host is ready: `mpt.yaml` has `enabled: false`, and even then MPT_URL must name the host.
What it does when on: (1) DRAFTS: one draft video per concept of a brief; (2) EXECUTABLE BRIEFS: only a brief that passed the claims gate and the brief rules is turned into jobs (a brief that needs review is refused with the reason); (3) HOOK A/B RENDERS:
the same concept rendered with several different opening lines, labelled A, B, C, so the hooks can be compared. PUBLISHING IS OFF AND CANNOT BE SWITCHED ON HERE: this module has no publish, upload or post function at all;
a draft is a file for the owner to look at. Pexels (free, no card) supplies the stock footage MPT uses: PEXELS_API_KEY goes into MPT's own configuration on the host, not into this tool."""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

CONFIG = "mpt.yaml"
DEFAULTS = {"enabled": False, "stock_source": "auto", "aspect": "9:16", "voice": "en-US-JennyNeural-Female", "clip_seconds": 4, "max_hooks": 3, "timeout_seconds": 900, "poll_seconds": 10}


class MptOff(Exception):
    pass


class MptError(Exception):
    pass


def load_config(path=CONFIG):
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    unknown = set(raw) - set(DEFAULTS) - {"publishing"}
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "publishing"}}                       # `publishing` is read and ignored: there is nothing to switch on
    cfg["enabled"] = cfg["enabled"] is True
    cfg["max_hooks"] = max(1, min(5, int(cfg["max_hooks"])))
    return cfg


def stock_source(cfg, environ):
    """Where MoneyPrinterTurbo takes its stock footage from: 'pexels' or 'pixabay' (both free, no card). `auto` uses Pexels when a Pexels key exists and otherwise PIXABAY: Pexels has paused new key issuance (checked 2026-11-23),
    so Pixabay is the default until a Pexels key can be created. The keys themselves live in MoneyPrinterTurbo's own configuration on the host, not in this tool."""
    choice = cfg.get("stock_source", "auto")
    if choice in ("pexels", "pixabay"):
        return choice
    return "pexels" if (environ.get("PEXELS_API_KEY") or "").strip() else "pixabay"


def ensure_on(cfg, environ):
    """The base URL of the host, or MptOff with the reason. Both the owner's switch and a host address are needed."""
    if not cfg["enabled"]:
        raise MptOff("MoneyPrinterTurbo is OFF (mpt.yaml: enabled: false): it stays off until the owner says a host is ready")
    url = (environ.get("MPT_URL") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        raise MptOff("MPT_URL is not set to the https address of the MoneyPrinterTurbo host: nothing can be rendered without a running instance")
    return url


def default_http(method, url, body=None, timeout=60, token=""):
    """One JSON call to the host. `token` is the Google identity token a private Cloud Run service asks for (MPT_TOKEN): the service is deployed WITHOUT public access, so nobody else can use it."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "KelsonResearchBot/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                                  # noqa: S310 - the owner's own https host
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return exc.code, {}


class Client:
    def __init__(self, base_url, http=default_http, sleep=time.sleep, clock=time.monotonic):
        self.base, self.http, self.sleep, self.clock = base_url.rstrip("/"), http, sleep, clock

    def create(self, params):
        status, body = self.http("POST", self.base + "/api/v1/videos", params, 60)
        task = ((body or {}).get("data") or {}).get("task_id")
        if status != 200 or not task:
            raise MptError(f"MPT did not accept the job (HTTP {status}): {str(body)[:160]}")
        return str(task)

    def wait(self, task_id, timeout=900, poll=10):
        """The finished task: {state, progress, videos}. state 1 is complete, -1 failed (MPT's own codes); a job not finished in `timeout` seconds raises."""
        t0 = self.clock()
        while True:
            status, body = self.http("GET", f"{self.base}/api/v1/tasks/{task_id}", None, 30)
            data = (body or {}).get("data") or {}
            if status == 200 and data.get("state") == 1:
                return {"state": 1, "progress": data.get("progress", 100), "videos": [str(v) for v in (data.get("videos") or [])]}
            if status != 200 or data.get("state") == -1:
                raise MptError(f"MPT failed the job (HTTP {status}, state {data.get('state')}): {str(body)[:160]}")
            if self.clock() - t0 > timeout:
                raise MptError(f"MPT did not finish within {timeout} seconds (progress {data.get('progress')})")
            self.sleep(poll)


def terms_from(text, limit=6):
    stop = {"the", "and", "with", "that", "this", "show", "shot", "then", "into", "from", "your", "for", "are", "one", "his", "her"}
    words = [w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in stop]
    return ", ".join(list(dict.fromkeys(words))[:limit])


def params_for(concept, cfg, *, hook=None, brand="", source="pixabay"):
    """The MPT request for one concept (a draft). `hook` replaces the opening line of the script (an A/B variant). Nothing in it asks MPT to publish: it has no such field."""
    script = " ".join(str(concept.get("script") or "").split())
    if hook:
        rest = script.split(". ", 1)[1] if ". " in script else ""
        script = (hook.strip().rstrip(".") + ". " + rest).strip()
    if not script:
        raise MptError(f"the concept {concept.get('name', '')!r} has no script")
    return {"video_subject": str(concept.get("name") or brand or "draft")[:100], "video_script": script, "video_terms": terms_from(concept.get("visual_direction") or script),
            "video_aspect": cfg["aspect"], "video_clip_duration": cfg["clip_seconds"], "voice_name": cfg["voice"], "video_count": 1, "subtitle_enabled": True, "video_source": source}


def jobs_from_brief(brief, cfg, *, status="ready", violations=(), brand="", source="pixabay"):
    """EXECUTABLE BRIEFS: [(label, params)] for a brief that is fit to execute. A brief that needs review, or has any error violation (a blocked claim, a broken rule), is refused with the reason: a draft video
    is never made from a brief a person has not cleared."""
    errors = [v for v in violations if v.get("severity") == "error"]
    if status != "ready" or errors:
        why = "; ".join(f"{v.get('where', '')}: {v.get('message', '')}"[:100] for v in errors[:3]) or f"its status is {status!r}, not 'ready'"
        raise MptError("the brief is not executable: " + why)
    return [(f"concept {n}", params_for(c, cfg, brand=brand, source=source)) for n, c in enumerate(brief.get("concepts", []), 1) if c.get("script")]


def hook_variants(concept, hooks, cfg, brand="", source="pixabay"):
    """HOOK A/B RENDERS: [(label, params)], the same concept with each hook as its opening line: A is the concept's own, then B, C... (at most max_hooks in all)."""
    out = [("A (the concept's own hook)", params_for(concept, cfg, brand=brand, source=source))]
    for i, hook in enumerate([h for h in hooks if str(h).strip()][: cfg["max_hooks"] - 1], 1):
        out.append((f"{chr(65 + i)} ({str(hook).strip()[:40]})", params_for(concept, cfg, hook=str(hook), brand=brand, source=source)))
    return out


def render(client, jobs, cfg, *, manifest=None, outdir=None, fetch=None, probe=None, gate=None):
    """Make each job and keep the result: [{label, task_id, videos, path, measured, passed, failures}]. With a manifest each step is recorded; with `fetch`, `probe` and `gate` the video is downloaded, measured and checked."""
    from . import openmontage as OM
    results = []
    for label, params in jobs:
        step = manifest.start(label, "moneyprinterturbo", params) if manifest else None
        entry = {"label": label}
        try:
            task = client.create(params)
            done = client.wait(task, cfg["timeout_seconds"], cfg["poll_seconds"])
            entry.update(task_id=task, videos=done["videos"])
            if done["videos"] and outdir and fetch:
                path = Path(outdir) / f"{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}-{task[:8]}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(fetch(done["videos"][0]))
                entry["path"] = str(path)
                if probe and gate is not None:
                    entry["measured"] = probe(path)
                    entry["passed"], entry["failures"] = OM.quality_gate(entry["measured"], gate)
            ok = entry.get("passed", True)
            if manifest:
                manifest.finish(step, output=entry.get("path", ", ".join(done["videos"])), gate=entry.get("failures"), ok=ok, note="" if ok else "quality gate failed")
        except (MptError, OSError) as exc:
            entry.update(error=str(exc)[:200])
            if manifest:
                manifest.finish(step, ok=False, note=str(exc)[:200])
        results.append(entry)
    return results
