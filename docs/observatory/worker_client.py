"""Running the isolated Scrapling worker from the main tool.

The worker lives in its own Python environment (see docs/observatory/README.md). It is started as a separate process, given a job file and nothing
else from this environment: no API keys, no cookies, only the named secrets the job needs (a proxy address or an account's cookies, for a host that is
listed under authorised_access). It writes a result file and exits. The core checks the authorised-access list before building the job, and the worker
checks it again for every request.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .. import contract as K
from . import access
from .config import USER_AGENT_TOKEN

EXPECTED_VERSION = "0.5"
_PASS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "PLAYWRIGHT_BROWSERS_PATH")
_CODES = {"NEED_SETUP": K.NEED_SETUP, "NEED_LOGIN": K.NEED_LOGIN, "UPSTREAM_BROKEN": K.UPSTREAM_BROKEN, "TIMEOUT": K.TIMEOUT, "RATE_LIMITED": K.RATE_LIMITED,
          "PLATFORM_BLOCKED": K.PLATFORM_BLOCKED, "NOT_FOUND": K.NOT_FOUND, "BAD_INPUT": K.BAD_INPUT}


def proxy_env_names(settings):
    return [f"OBSERVATORY_PROXY_{p.upper()}" for p in settings["advanced"]["proxies"]]


def advanced_for(comp, settings, gate):
    """The advanced options for a competitor's requests, or {}. Refuses (BAD_INPUT) when the site is not authorised for what is asked: it never quietly does less or more."""
    adv = dict(comp.get("advanced") or {})
    if not adv:
        return {}
    options = {}
    for key in ("stealth", "solve_cloudflare", "impersonate", "fingerprint", "headless", "network_idle"):
        if adv.get(key):
            options[key] = True
    if adv.get("headless") is False:
        options["headless"] = False
    if adv.get("wait_selector"):
        options["wait_selector"] = adv["wait_selector"]
    if adv.get("proxy"):
        names = proxy_env_names(settings)
        if not names:
            raise K.Failure(K.NEED_SETUP, "advanced.proxy is on but no proxy names are listed under advanced.proxies in observatory.yaml")
        options["proxy_envs"] = names
        default = settings["advanced"]["rotate"] if settings["advanced"]["rotate"] != "none" else "sequential"
        options["rotate"] = adv.get("rotate") or default
    if adv.get("cookies_env"):
        options["cookies_env"] = adv["cookies_env"]
    from .scrapling_worker import required_methods
    need = required_methods(options)
    if need:
        gate.require(comp["domain"], *need)
    return options


def build_job(items, settings, contact, *, selectors=None, adaptive_db=""):
    pol = settings["crawl_policy"]
    return {"expected_version": EXPECTED_VERSION, "user_agent": f"{USER_AGENT_TOKEN}/1.0 (research; contact: {contact})", "delay": settings["politeness"]["min_delay_seconds"],
            "max_delay": settings["politeness"]["max_delay_seconds"], "timeout": settings["politeness"]["timeout_seconds"], "max_chars": settings["pages"]["max_text_chars"],
            "robots": {"mode": pol["robots"], "basis": pol["basis"], "overrides": pol["overrides"]}, "authorisations": settings["authorised_access"]["hosts"], "items": items,
            "selectors": selectors or {}, "adaptive_db": adaptive_db}


def worker_environment(job, environ=None):
    """The environment the worker gets: the basics, and only the secrets named in the job (proxy addresses, an account's cookies)."""
    environ = environ if environ is not None else os.environ
    names = set(_PASS)
    for item in job["items"]:
        adv = item.get("advanced") or {}
        names.update(adv.get("proxy_envs") or [])
        if adv.get("cookies_env"):
            names.add(adv["cookies_env"])
    env = {k: environ[k] for k in names if k in environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_worker(job, python, *, timeout=600, environ=None, runner=subprocess.run):
    """The worker's result dict. Raises Failure(NEED_SETUP) when the environment is missing or broken, Failure(TIMEOUT) when it does not finish."""
    if not python or not Path(python).exists():
        raise K.Failure(K.NEED_SETUP, "the Scrapling environment is not set up: set SCRAPLING_PYTHON to the Python of the environment that has the pinned Scrapling "
                        "(the observatory workflow creates it), or use engine.pages: core")
    with tempfile.TemporaryDirectory(prefix="observatory-") as tmp:
        job_path, out_path = Path(tmp) / "job.json", Path(tmp) / "result.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        job_path.chmod(0o600)
        try:
            done = runner([str(python), "-m", "contentintel.observatory.scrapling_worker", str(job_path), str(out_path)], env=worker_environment(job, environ),
                          capture_output=True, text=True, timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise K.Failure(K.TIMEOUT, f"the Scrapling worker did not finish within {timeout} seconds") from None
        except OSError as exc:
            raise K.Failure(K.NEED_SETUP, f"the Scrapling worker could not be started ({exc.__class__.__name__})") from None
        if done.returncode != 0 or not out_path.is_file():
            tail = " ".join((done.stderr or "").split())[-240:]
            raise K.Failure(K.UPSTREAM_BROKEN, f"the Scrapling worker failed ({done.returncode}): {tail}")
        try:
            result = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            raise K.Failure(K.UPSTREAM_BROKEN, "the Scrapling worker wrote an unreadable result") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        raise K.Failure(_CODES.get(err.get("code"), K.UPSTREAM_BROKEN), err.get("message", "the Scrapling worker reported a problem"))
    return result


def failure_from(result):
    err = result.get("error") or {}
    failure = K.Failure(_CODES.get(err.get("code"), K.UPSTREAM_BROKEN), err.get("message", "the worker could not read the page"))
    failure.robots_path = bool(err.get("robots_path"))
    return failure


def page_snapshot(result, settings, comp):
    """A page snapshot (the same shape the built-in reader makes) from a worker result, so diffs and reports do not care which engine read it."""
    keep = settings["pages"]["keep_text_chars"]
    text = (result.get("markdown") or result.get("text") or "")[:keep]
    adaptive = result.get("adaptive") or {}
    h1 = [adaptive["headline"]] if adaptive.get("headline") else result.get("h1", [])
    announcements = [adaptive["announcement"]] if adaptive.get("announcement") else result.get("announcements", [])
    return {"kind": "page", "schema": 1, "url": result["url"], "final_url": result.get("final_url", result["url"]), "captured_at": "", "complete": True, "status": result.get("status", 200),
            "title": result.get("title", ""), "description": result.get("description", ""), "canonical": "", "h1": h1, "headings": result.get("headings", []), "announcements": announcements,
            "offers": result.get("offers", []), "products": result.get("products", []), "alternates": result.get("alternates", []), "text": text,
            "text_hash": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16], "text_truncated": len(result.get("markdown") or "") > keep, "chars": result.get("chars", len(text)),
            "engine": "scrapling", "advanced_used": result.get("advanced_used", []), "authorised_by": result.get("authorised_by", ""), "robots_note": result.get("robots_note", "")}


class WorkerPages:
    """Reads a competitor's pages through the worker in one batch the first time one is asked for, then hands each back as a snapshot or its failure."""

    def __init__(self, comp, urls, settings, contact, python, snaps, *, environ=None, runner=subprocess.run, job_extra=None):
        self.comp, self.urls, self.settings, self.job_extra = comp, list(urls), settings, job_extra or {}
        self.contact, self.python, self.environ, self.runner = contact, python, environ, runner
        self.adaptive_db = str(snaps.site_dir(comp["id"]) / "adaptive.db")
        self.results = None

    def _load(self):
        gate = access.Gate(self.settings)
        adv = advanced_for(self.comp, self.settings, gate)
        items = [{"url": u, "advanced": adv} for u in self.urls]
        Path(self.adaptive_db).parent.mkdir(parents=True, exist_ok=True)
        job = build_job(items, self.settings, self.contact, selectors=self.comp.get("selectors"), adaptive_db=self.adaptive_db)
        job.update(self.job_extra)
        got = run_worker(job, self.python, environ=self.environ, runner=self.runner)
        self.results = {r["url"]: r for r in got["results"]}

    def get(self, url):
        if self.results is None:
            self._load()
        result = self.results.get(url)
        if result is None:
            raise K.Failure(K.UPSTREAM_BROKEN, "the worker returned nothing for this page")
        if not result.get("ok"):
            raise failure_from(result)
        return page_snapshot(result, self.settings, self.comp)

