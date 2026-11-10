"""Running the isolated Scrapling worker from the main tool.

The worker lives in its own Python environment (see docs/observatory/README.md). It is started as a separate process, given a job file and nothing
else from this environment: no API keys, no cookies, only the named secrets the job needs (a proxy address or an account's cookies). It writes a result file and
exits. The core checks the owner's switches before building the job, and the worker checks them again for every request.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .. import guards, contract as K
from .config import USER_AGENT_TOKEN

EXPECTED_VERSION = "0.5"
_PASS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "PLAYWRIGHT_BROWSERS_PATH")
_CODES = {"NEED_SETUP": K.NEED_SETUP, "NEED_LOGIN": K.NEED_LOGIN, "UPSTREAM_BROKEN": K.UPSTREAM_BROKEN, "TIMEOUT": K.TIMEOUT, "RATE_LIMITED": K.RATE_LIMITED,
          "PLATFORM_BLOCKED": K.PLATFORM_BLOCKED, "NOT_FOUND": K.NOT_FOUND, "BAD_INPUT": K.BAD_INPUT}


def proxy_env_names(settings):
    return [f"OBSERVATORY_PROXY_{p.upper()}" for p in settings["advanced"]["proxies"]]


def advanced_for(comp, settings):
    """The request options for a competitor, or {}. Every option is the owner's to set on any host. The switches in access_controls can turn each kind off (the core refuses to build a job for
    something that is switched off, never quietly doing less), and the methods that exist to get past bot detection are included only with the competitor's own recorded decision."""
    adv = dict(comp.get("advanced") or {})
    if not adv:
        return {}
    controls = settings["access_controls"]
    for key, switch in (("identity", "custom_identity"), ("headers", "custom_identity"), ("accept_language", "custom_identity"), ("session", "session"), ("proxy", "proxy"), ("browser", "browser"),
                        ("stealth", "browser"), ("cookies_env", "account_cookies")):
        if adv.get(key) and not controls[switch]:
            raise K.Failure(K.BAD_INPUT, f"advanced.{key} is set for {comp['domain']} but access_controls.{switch} is switched off in observatory.yaml")
    options = {}
    if adv.get("browser") or adv.get("stealth"):
        options["stealth"] = True
    for key in ("headless", "network_idle", "session", "disable_resources"):
        if adv.get(key):
            options[key] = True
    for key in ("identity", "headers", "accept_language", "locale", "timezone", "wait_selector"):
        if adv.get(key):
            options[key] = adv[key]
    if adv.get("delay"):
        options["delay"] = max(float(adv["delay"]), float(settings["politeness"]["min_delay_seconds"]))          # only slower than the standing pause, never faster
    if adv.get("headless") is False:
        options["headless"] = False
    if adv.get("proxy"):
        names = proxy_env_names(settings)
        if not names:
            raise K.Failure(K.NEED_SETUP, "advanced.proxy is on but no proxy names are listed under advanced.proxies in observatory.yaml")
        options["proxy_envs"] = names
        default = settings["advanced"]["rotate"] if settings["advanced"]["rotate"] != "none" else "sequential"
        options["rotate"] = adv.get("rotate") or default
    if adv.get("cookies_env"):
        options["cookies_env"] = adv["cookies_env"]
    asked = [k for k in ("impersonate", "hide_canvas", "block_webrtc", "humanize", "solve_challenges") if adv.get(k)]
    if asked and not controls["evasion"]:
        raise K.Failure(K.BAD_INPUT, f"advanced.{asked[0]} is set for {comp['domain']} but access_controls.evasion is switched off in observatory.yaml")
    if adv.get("impersonate"):
        options["impersonate"] = True
    if adv.get("hide_canvas") or adv.get("block_webrtc"):
        options["fingerprint"] = True
        options["hide_canvas"], options["block_webrtc"] = bool(adv.get("hide_canvas")), bool(adv.get("block_webrtc"))
    if adv.get("solve_challenges"):
        options["solve_cloudflare"] = True
    if adv.get("humanize"):
        options["humanize"] = True
    return options


def build_job(items, settings, contact, *, selectors=None, adaptive_db=""):
    pol = settings["crawl_policy"]
    return {"expected_version": EXPECTED_VERSION if ((settings["services"].get("connection_versions") or {}).get("routes") or {}).get("scrapling", "latest") == "pinned" else "", "user_agent": f"{USER_AGENT_TOKEN}/1.0 (research; contact: {contact})", "contact": contact, "controls": settings["access_controls"], "delay": settings["politeness"]["min_delay_seconds"],
            "max_delay": settings["politeness"]["max_delay_seconds"], "timeout": settings["politeness"]["timeout_seconds"], "max_chars": settings["pages"]["max_text_chars"],
            "robots": {"mode": pol["robots"], "basis": pol["basis"], "overrides": pol["overrides"]}, "guards": guards.snapshot(), "retry": settings.get("retry") or {"attempts": 3, "backoff_seconds": 2}, "items": items,
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
            "offers": result.get("offers", []), "products": result.get("products", []), "alternates": result.get("alternates", []), "social": result.get("social", {}), "text": text,
            "text_hash": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16], "text_truncated": len(result.get("markdown") or "") > keep, "chars": result.get("chars", len(text)),
            "engine": "scrapling", "advanced_used": result.get("advanced_used", []), "authorised_by": result.get("authorised_by", ""), "robots_note": result.get("robots_note", "")}


def remember_rests(got, pauses, comp, settings, now):
    """What the worker met is remembered between runs, exactly as the built-in reader does: a host that refused rests, for at least the configured time (longer if the owner asked)."""
    if not pauses:
        return
    for host, info in (got.get("hosts") or {}).items():
        if info.get("rest"):
            code = K.RATE_LIMITED if "slow down" in info["rest"] else K.PLATFORM_BLOCKED
            longer = float((comp.get("advanced") or {}).get("rest_hours") or 0) * 3600           # the owner may make a host rest longer, never shorter
            pauses.pause(host, max(settings["politeness"]["rate_limit_pause_seconds"], longer), info["rest"], now, code)


def crawl_site(comp, url, settings, contact, python, *, max_pages, depth, environ=None, runner=subprocess.run, pauses=None, now="", sitemap=False, resume=None, extract=None):
    """A polite crawl of one site through the worker: {"pages": [...], "skipped": [...], "stopped": reason}. A resting site is not asked at all; a refusal during the crawl stops it and rests the site."""
    if pauses:
        rest = pauses.paused(comp["domain"], now)
        if rest:
            raise K.Failure(K.RATE_LIMITED if rest[2] != K.PLATFORM_BLOCKED else K.PLATFORM_BLOCKED, f"{comp['domain']} is resting until {rest[1][:16].replace('T', ' ')} UTC ({rest[0]})")
    max_pages, depth = max(1, min(int(max_pages), 200)), max(0, min(int(depth), 6))
    crawl = {"max_pages": max_pages, "depth": depth, "sitemap": bool(sitemap)}
    if resume:
        crawl["resume"] = resume
    job = build_job([{"url": url, "advanced": advanced_for(comp, settings), "crawl": crawl, "extract": list(extract or [])[:20]}], settings, contact)
    per_page = float(settings["politeness"]["timeout_seconds"]) + float(job.get("delay", 5))
    got = run_worker(job, python, timeout=max(600, int(max_pages * per_page * 2)), environ=environ, runner=runner)
    result = got["results"][0]
    remember_rests(got, pauses, comp, settings, now)
    if not result.get("ok"):
        raise failure_from(result)
    return {"pages": result["pages"], "skipped": result["skipped"], "stopped": result["stopped"], "scrapling_version": got.get("scrapling_version", ""), "state": result.get("state"),
            "sitemap": result.get("sitemap")}


def discover_feeds(comp, url, settings, contact, python, *, guesses=(), environ=None, runner=subprocess.run, pauses=None, now=""):
    """Feeds a site advertises (links in the page) and feeds at the usual addresses that really answer with a feed: [{"url", "how"}]. One page and a few guesses, through the same guarded fetch."""
    import re
    from urllib.parse import urljoin
    if pauses:
        rest = pauses.paused(comp["domain"], now)
        if rest:
            raise K.Failure(K.RATE_LIMITED if rest[2] != K.PLATFORM_BLOCKED else K.PLATFORM_BLOCKED, f"{comp['domain']} is resting until {rest[1][:16].replace('T', ' ')} UTC ({rest[0]})")
    adv = advanced_for(comp, settings)
    items = [{"url": url, "advanced": adv}] + [{"url": g, "advanced": adv, "raw": True} for g in list(guesses)[:8]]
    got = run_worker(build_job(items, settings, contact), python, timeout=max(600, 120 * len(items)), environ=environ, runner=runner)
    remember_rests(got, pauses, comp, settings, now)
    page, probes = got["results"][0], got["results"][1:]
    if not page.get("ok") and not any(p.get("ok") for p in probes):
        raise failure_from(page)
    found, seen = [], set()
    for link in page.get("alternates") or []:
        target = urljoin(page.get("final_url") or url, link.get("href", "")) if isinstance(link, dict) else ""
        if target and target not in seen:
            seen.add(target)
            found.append({"url": target, "how": "linked from the page"})
    for probe in probes:
        if probe.get("ok") and re.search(r"<(rss|feed|rdf:rdf)\b", (probe.get("raw") or "")[:3000], re.I) and probe["url"] not in seen:
            seen.add(probe["url"])
            found.append({"url": probe["url"], "how": "found at a usual address"})
    return found


class WorkerPages:
    """Reads a competitor's pages through the worker in one batch the first time one is asked for, then hands each back as a snapshot or its failure."""

    def __init__(self, comp, urls, settings, contact, python, snaps, *, environ=None, runner=subprocess.run, job_extra=None, pauses=None, now=""):
        self.comp, self.urls, self.settings, self.job_extra = comp, list(urls), settings, job_extra or {}
        self.pauses, self.now = pauses, now
        self.contact, self.python, self.environ, self.runner = contact, python, environ, runner
        self.adaptive_db = str(snaps.site_dir(comp["id"]) / "adaptive.db")
        self.results = None

    def _load(self):
        if self.pauses:                                   # a site that is resting (from this reader or the built-in one) is not asked at all
            rest = self.pauses.paused(self.comp["domain"], self.now)
            if rest:
                raise K.Failure(K.RATE_LIMITED if rest[2] != K.PLATFORM_BLOCKED else K.PLATFORM_BLOCKED, f"{self.comp['domain']} is resting until {rest[1][:16].replace('T', ' ')} UTC ({rest[0]})")
        adv = advanced_for(self.comp, self.settings)
        items = [{"url": u, "advanced": adv} for u in self.urls]
        Path(self.adaptive_db).parent.mkdir(parents=True, exist_ok=True)
        job = build_job(items, self.settings, self.contact, selectors=self.comp.get("selectors"), adaptive_db=self.adaptive_db)
        job.update(self.job_extra)
        got = run_worker(job, self.python, environ=self.environ, runner=self.runner)
        self.results = {r["url"]: r for r in got["results"]}
        remember_rests(got, self.pauses, self.comp, self.settings, self.now)

    def get(self, url):
        if self.results is None:
            self._load()
        result = self.results.get(url)
        if result is None:
            raise K.Failure(K.UPSTREAM_BROKEN, "the worker returned nothing for this page")
        if not result.get("ok"):
            raise failure_from(result)
        return page_snapshot(result, self.settings, self.comp)
