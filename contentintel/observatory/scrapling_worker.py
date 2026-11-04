"""The isolated Scrapling worker: run it with the Python of a separate environment that has the pinned Scrapling installed.

    <venv>/bin/python -m contentintel.observatory.scrapling_worker JOB.json RESULT.json      (with the repository root on PYTHONPATH)

It imports only the standard library, Scrapling, Protego and three small pure modules of this repository, so the main tool's dependencies and
Scrapling's never meet. Scrapling is BSD 3-Clause licensed (https://github.com/D4Vinci/Scrapling, copyright 2024 Karim Shoair).

By default it is a polite reader: Scrapling's HTTP fetcher with an honest identity, robots.txt handled by Protego under the job's crawl policy, a pause
between requests that doubles after a block, Markdown with hidden text removed, and adaptive selectors that find an element again after a redesign.

Scrapling also carries advanced methods: browser impersonation, a stealth browser, challenge handling, proxies and rotation, and cookies from a logged-in
account. Each one is available only for a host listed in the job's `authorisations` (hosts we own or are authorised to use), and only the methods listed for it.
The worker checks the list itself on every request, so a job that asks for a method on any other host is refused whoever wrote it.
"""
import json
import os
import random
import sys
import time
from urllib.parse import urljoin, urlsplit

from contentintel import safety
from contentintel.observatory import gatecheck, htmlx, signals

PINNED = "0.5"
AGENT = "KelsonResearchBot"


def _scrub(text):
    return safety.scrub(str(text), 300)


class Fetchers:
    """The real Scrapling calls, kept in one small class so tests can stand in for the browser (which cannot be installed everywhere)."""

    def http_get(self, url, **kw):
        from scrapling.fetchers import Fetcher
        return Fetcher.get(url, **kw)

    def stealth_get(self, url, **kw):
        from scrapling.fetchers import StealthyFetcher
        return StealthyFetcher.fetch(url, **kw)

    def is_proxy_error(self, exc):
        from scrapling.engines.toolbelt.proxy_rotation import is_proxy_error
        return is_proxy_error(exc)

    def selector(self, html, url, db):
        from scrapling.parser import Selector
        return Selector(html, url=url, adaptive=True, storage_args={"storage_file": db, "url": url})


def stealth_kwargs(adv, honest_agent, timeout):
    """The arguments for the stealth browser from the requested options. The Google referer is never set; fingerprint changes only when 'fingerprint' was asked for."""
    kw = {"headless": adv.get("headless", True), "google_search": False, "timeout": int(timeout * 1000), "network_idle": bool(adv.get("network_idle", False))}
    if adv.get("solve_cloudflare"):
        kw["solve_cloudflare"] = True
    if adv.get("fingerprint"):
        kw.update(hide_canvas=bool(adv.get("hide_canvas", True)), block_webrtc=bool(adv.get("block_webrtc", True)))
    else:
        kw["useragent"] = honest_agent
    if adv.get("wait_selector"):
        kw["wait_selector"] = adv["wait_selector"]
    return kw


def required_methods(adv):
    """The advanced methods a request asks for, by name."""
    need = []
    if adv.get("stealth"):
        need.append("stealth")
    if adv.get("solve_cloudflare"):
        need.append("challenge")
    if adv.get("impersonate") or adv.get("fingerprint"):
        need.append("fingerprint")
    if adv.get("proxy_envs"):
        need.append("proxy")
    if adv.get("cookies_env"):
        need.append("logged_in_browser")
    return list(dict.fromkeys(need))


class Worker:
    def __init__(self, job, fetchers=None, sleep=time.sleep, clock=time.monotonic, environ=None):
        self.job, self.f = job, fetchers or Fetchers()
        self.sleep, self.clock, self.env = sleep, clock, environ if environ is not None else os.environ
        self.agent = job["user_agent"]
        self.delay0, self.max_delay = float(job.get("delay", 5)), float(job.get("max_delay", 60))
        self.sites, self.robots, self.rotor = {}, {}, 0
        self.auth = job.get("authorisations", [])
        self.policy = job.get("robots", {"mode": "obey", "overrides": []})
        self.private_ok = bool(job.get("allow_private_for_tests"))

    # ---- helpers ---------------------------------------------------------------------------------------------------------------------------

    def _site(self, host):
        return self.sites.setdefault(host, {"delay": self.delay0, "last": None, "n": 0, "rest": ""})

    def _wait(self, host):
        site = self._site(host)
        if site["last"] is not None:
            gap = site["delay"] - (self.clock() - site["last"])
            if gap > 0:
                self.sleep(gap)

    def _done(self, host, ok, retry_after=None):
        site = self._site(host)
        site["last"], site["n"] = self.clock(), site["n"] + 1
        if not ok:
            site["delay"] = min(max(site["delay"] * 2, retry_after or 0), self.max_delay)

    def _guard(self, url):
        try:
            clean = safety.normalize_public_url(url) if not self.private_ok else url
        except safety.UnsafeURL:
            raise _Refused("BAD_INPUT", "only public http(s) web addresses are fetched") from None
        host = urlsplit(clean).hostname or ""
        if not self.private_ok and not safety.resolves_public(host):
            raise _Refused("BAD_INPUT", f"{host} does not point to a public address")
        return clean, host

    def _robots_mode(self, host):
        h = gatecheck.host_only(host)
        while h:
            for o in self.policy.get("overrides", []):
                if gatecheck.host_only(o["host"]) == h:
                    return o["robots"], f"override for {o['host']}: {o['basis']}, approved by {o['approved_by']} on {o['date']}"
            h = h.partition(".")[2] if "." in h else ""
        return self.policy.get("mode", "obey"), self.policy.get("basis", "")

    def _honest(self, url, **extra):
        return self.f.http_get(url, stealthy_headers=False, impersonate=None, headers={"User-Agent": self.agent}, timeout=self.job.get("timeout", 20), follow_redirects=False, **extra)

    def _proxy_for(self, adv):
        """The proxy address for this request (from the named secret), or None. One pick per address, used for robots.txt and the page alike."""
        names = adv.get("proxy_envs")
        if not names:
            return None
        pick = names[self.rotor % len(names)] if adv.get("rotate", "sequential") != "random" else random.choice(names)
        self.rotor += 1
        proxy = self.env.get(pick, "")
        if not proxy:
            raise _Refused("NEED_SETUP", f"{pick} is not set")
        return proxy

    def _robots_for(self, url, proxy=None):
        from protego import Protego
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self.robots:
            return self.robots[origin]
        closed, note, parser = False, "", None
        try:
            self._wait(parts.netloc)
            r = self._honest(origin + "/robots.txt", **({"proxy": proxy} if proxy else {}))      # through the same proxy as the page, never around it
            self._done(parts.netloc, ok=r.status < 500)
            if r.status == 200:
                parser = Protego.parse(r.body.decode("utf-8", errors="replace").removeprefix("\ufeff"))
            elif 400 <= r.status < 500:
                parser, note = Protego.parse(""), "no robots.txt"
            else:
                closed, note = True, f"robots.txt answered {r.status}; treated as closed for this run"
        except Exception as exc:
            closed, note = True, f"robots.txt could not be read ({exc.__class__.__name__}); treated as closed for this run"
        self.robots[origin] = (parser or Protego.parse("User-agent: *\nDisallow: /"), closed, note)
        return self.robots[origin]

    # ---- one address -----------------------------------------------------------------------------------------------------------------------

    def one(self, item):
        url, adv = item["url"], item.get("advanced") or {}
        out = {"url": url, "engine": "scrapling", "ok": False, "status": 0, "advanced_used": [], "robots_note": ""}
        try:
            clean, host = self._guard(url)
            need = required_methods(adv)
            if need:
                try:
                    out["authorised_by"] = gatecheck.require(self.auth, host, *need)
                except ValueError as exc:
                    raise _Refused("BAD_INPUT", str(exc)) from None
                out["advanced_used"] = need
            site = self._site(host)
            if site["rest"]:
                raise _Refused("RATE_LIMITED", f"{host} is resting this run ({site['rest']})")
            proxy = self._proxy_for(adv)
            parser, closed, note = self._robots_for(clean, proxy)
            mode, basis = self._robots_mode(host)
            if (closed or not parser.can_fetch(clean, AGENT)):
                if mode == "obey":
                    raise _Refused("PLATFORM_BLOCKED", f"{clean} was not fetched: " + (note if closed else "robots.txt does not allow this address"), robots_path=not closed)
                out["robots_note"] = (note if closed else "robots.txt does not allow this address") + f"; read under crawl policy '{mode}'" + (f" ({basis})" if basis else "")
            crawl = parser.crawl_delay(AGENT) if parser else None
            if crawl:
                site["delay"] = max(site["delay"], min(float(crawl), self.max_delay))
            resp = self._request(clean, host, adv, out, proxy)
        except _Refused as exc:
            out["error"] = {"code": exc.code, "message": _scrub(exc), "robots_path": exc.robots_path}
            return out
        except Exception as exc:
            out["error"] = {"code": "UPSTREAM_BROKEN", "message": _scrub(f"{exc.__class__.__name__}: {exc}"), "robots_path": False}
            return out
        return self._read(resp, clean, out, adv)

    def _request(self, url, host, adv, out, proxy=None):
        hops, current = 0, url
        while True:
            current, host = self._guard(current)
            self._wait(host)
            kw = {}
            if adv.get("stealth"):
                kw = stealth_kwargs(adv, self.agent, self.job.get("timeout", 20))
                resp = self.f.stealth_get(current, **kw)
            else:
                kw = {"timeout": self.job.get("timeout", 20), "follow_redirects": False, "stealthy_headers": bool(adv.get("fingerprint")), "impersonate": "chrome" if adv.get("impersonate") else None}
                headers = {} if adv.get("impersonate") else {"User-Agent": self.agent}
                if adv.get("cookies_env"):
                    raw = self.env.get(adv["cookies_env"], "")
                    try:
                        kw["cookies"] = json.loads(raw) if raw else None
                    except ValueError:
                        raise _Refused("NEED_SETUP", "the cookies secret is not valid JSON") from None
                    if not kw["cookies"]:
                        raise _Refused("NEED_SETUP", f"{adv['cookies_env']} is not set")
                if proxy:
                    kw["proxy"] = proxy
                try:
                    resp = self.f.http_get(current, headers=headers, **kw)
                except Exception as exc:
                    if kw.get("proxy") and self.f.is_proxy_error(exc):
                        self._done(host, ok=False)
                        raise _Refused("UPSTREAM_BROKEN", "the proxy failed: " + _scrub(str(exc).replace(kw["proxy"], "***"))) from None
                    raise
            status = int(resp.status)
            heads = {str(k).lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
            retry = signals.parse_retry_after(heads.get("retry-after"))
            ok = status < 400 and not signals.looks_like_challenge(status, heads, bytes(resp.body or b""))
            self._done(host, ok=ok or status in (301, 302, 303, 307, 308), retry_after=retry)
            if status in (301, 302, 303, 307, 308):
                hops += 1
                target = heads.get("location", "")
                if not target or hops > 3:
                    raise _Refused("UPSTREAM_BROKEN", "too many redirects, or one without a destination")
                current = urljoin(current, target)
                continue
            if signals.looks_like_challenge(status, heads, bytes(resp.body or b"")):
                self._site(host)["rest"] = "a bot-check or challenge page was shown"
                raise _Refused("PLATFORM_BLOCKED", f"{host} showed a challenge or bot-check page" + ("" if adv.get("solve_cloudflare") else "; it was not worked around"))
            if status == 429:
                self._site(host)["rest"] = "asked us to slow down"
                raise _Refused("RATE_LIMITED", f"{host} asked us to slow down (429)")
            if status in (401, 403):
                raise _Refused("PLATFORM_BLOCKED", f"{host} refused the request ({status})")
            if status in (404, 410):
                raise _Refused("NOT_FOUND", f"{current} does not exist ({status})")
            if status >= 400:
                raise _Refused("UPSTREAM_BROKEN", f"{host} answered {status}")
            out["final_url"] = current
            return resp

    def _read(self, resp, url, out, adv):
        body = bytes(resp.body or b"")
        html = body.decode("utf-8", errors="replace")
        page = htmlx.parse_page(html, out.get("final_url", url), self.job.get("max_chars", 20000))
        if signals.looks_like_login_wall(body, page["title"]):
            out["error"] = {"code": "UPSTREAM_BROKEN", "message": f"{url} shows only a login page", "robots_path": False}
            return out
        try:
            markdown = resp.markdown()
        except Exception as exc:
            markdown, out["markdown_note"] = page["text"], f"Scrapling's Markdown was not available ({exc.__class__.__name__}); the built-in text is used"
        out.update(ok=True, status=int(resp.status), title=page["title"], description=page["description"], h1=page["h1"], headings=page["headings"][:25], announcements=page["announcements"],
                   offers=htmlx.extract_offers(page["text"], " ".join(page["announcements"])), alternates=page["alternates"][:8], products=htmlx.jsonld_products(page["jsonld"]),
                   markdown=safety.clean_text(markdown)[:self.job.get("max_chars", 20000)], text=page["text"], chars=page["chars"])
        selectors = self.job.get("selectors") or {}
        if selectors and self.job.get("adaptive_db"):
            try:
                sel = self.f.selector(html, out.get("final_url", url), self.job["adaptive_db"])
                found = {}
                for name, css in selectors.items():
                    hit = sel.css(css, auto_save=True, identifier=name, adaptive=True)
                    found[name] = " ".join(hit[0].get_all_text().split())[:300] if hit else None
                out["adaptive"] = found
            except Exception as exc:
                out["adaptive_error"] = f"{exc.__class__.__name__}"
        return out


class _Refused(Exception):
    def __init__(self, code, message, robots_path=False):
        super().__init__(message)
        self.code, self.robots_path = code, robots_path


def run(job, fetchers=None, **kw):
    try:
        import scrapling
        version = getattr(scrapling, "__version__", PINNED)
    except ImportError:
        return {"ok": False, "error": {"code": "NEED_SETUP", "message": "Scrapling is not installed in this environment"}, "results": []}
    if job.get("expected_version") and str(version) != str(job["expected_version"]):
        return {"ok": False, "error": {"code": "NEED_SETUP", "message": f"Scrapling {version} is installed but {job['expected_version']} is pinned; update the pin only after the contract tests pass"}, "results": []}
    worker = Worker(job, fetchers=fetchers, **kw)
    results = [worker.one(item) for item in job.get("items", [])]
    return {"ok": True, "scrapling_version": str(version), "results": results, "hosts": {h: {"requests": s["n"], "delay": round(s["delay"], 1), "rest": s["rest"]} for h, s in worker.sites.items()}}


def main(argv):
    if len(argv) != 3:
        print("usage: scrapling_worker JOB.json RESULT.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        job = json.load(fh)
    result = run(job)
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
