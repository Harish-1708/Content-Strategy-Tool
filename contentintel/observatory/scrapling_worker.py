"""The isolated Scrapling worker: run it with the Python of a separate environment that has the pinned Scrapling installed.

    <venv>/bin/python -m contentintel.observatory.scrapling_worker JOB.json RESULT.json      (with the repository root on PYTHONPATH)

It imports only the standard library, Scrapling, Protego and three small pure modules of this repository, so the main tool's dependencies and
Scrapling's never meet. Scrapling is BSD 3-Clause licensed (https://github.com/D4Vinci/Scrapling, copyright 2024 Karim Shoair).

By default it is a polite reader: Scrapling's HTTP fetcher with an honest identity, robots.txt handled by Protego under the job's crawl policy, a pause
between requests that doubles after a block, Markdown with hidden text removed, and adaptive selectors that find an element again after a redesign.

Controls available for ANY target host, decided by the person in the settings: a declared request identity and extra headers (a `From:` header naming us is always
sent), proxies with rotation, a per-run cookie session, a slower pace, and adaptive selectors. None of them can be used to try again after a refusal: a challenge, a
refusal or a rate limit rests that host for the run whatever the identity or proxy, and robots.txt is read for the identity actually used.

Also owner-controlled for any host: the stealth browser (rendering), its profile (locale, timezone), account cookies from a named secret, and how long a host rests.

Also owner-controlled, with their own switch (access_controls.evasion): TLS impersonation, canvas and WebRTC changes, simulated cursor movement and challenge solving. There is no host list and
no approval step. What nothing can do is retry past a refusal: a challenge, 401/403 or 429 rests the host for the run whatever options are set.
"""
import html as html_lib
import json
import os
import random
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit

from contentintel import guards, safety
from contentintel.observatory import gatecheck, htmlx, signals

PINNED = "0.5"


def _scrub(text):
    return safety.scrub(str(text), 300)


class Fetchers:
    """The real Scrapling calls, kept in one small class so tests can stand in for the browser (which cannot be installed everywhere)."""

    def __init__(self):
        self.sessions = {}

    def session_get(self, key, url, **kw):
        """A request that keeps cookies between requests to the same site for the length of the run."""
        from scrapling.fetchers import FetcherSession
        held = self.sessions.get(key)
        if held is None:
            manager = FetcherSession()
            held = self.sessions[key] = (manager, manager.__enter__())       # entering returns the working session; the manager is kept to close it
        return held[1].get(url, **kw)

    def close(self):
        for manager, _session in self.sessions.values():
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        self.sessions.clear()

    def http_get(self, url, **kw):
        from scrapling.fetchers import Fetcher
        return Fetcher.get(url, **kw)

    def stealth_get(self, url, **kw):
        from scrapling.fetchers import StealthyFetcher
        return StealthyFetcher.fetch(url, **kw)

    def is_proxy_error(self, exc):
        from scrapling.engines.toolbelt.proxy_rotation import is_proxy_error
        return is_proxy_error(exc)

    def links(self, resp, host):
        """The page's same-site links as Scrapling's own LinkExtractor finds them: absolute, canonical, de-duplicated, with files such as PDFs and images dropped."""
        from scrapling.spiders import LinkExtractor
        return LinkExtractor(allow_domains=(host,)).extract(resp)

    def selector(self, html, url, db):
        from scrapling.parser import Selector
        return Selector(html, url=url, adaptive=True, storage_args={"storage_file": db, "url": url})


def stealth_kwargs(adv, honest_agent, timeout):
    """The arguments for the stealth browser from the requested options. The Google referer is never set; fingerprint changes only when 'fingerprint' was asked for."""
    kw = {"headless": adv.get("headless", True), "google_search": False, "timeout": int(timeout * 1000), "network_idle": bool(adv.get("network_idle", False))}
    if adv.get("solve_cloudflare"):
        kw["solve_cloudflare"] = True
    if adv.get("humanize"):
        kw["humanize"] = True
    if adv.get("locale"):
        kw["locale"] = adv["locale"]
    if adv.get("timezone"):
        kw["timezone_id"] = adv["timezone"]
    if adv.get("disable_resources"):
        kw["disable_resources"] = True
    if adv.get("fingerprint"):
        kw.update(hide_canvas=bool(adv.get("hide_canvas", True)), block_webrtc=bool(adv.get("block_webrtc", True)))
    else:
        kw["useragent"] = honest_agent
    if adv.get("wait_selector"):
        kw["wait_selector"] = adv["wait_selector"]
    if adv.get("wait"):
        kw["wait"] = max(0, min(int(adv["wait"]), 15000))                                  # milliseconds to wait after the page has loaded, for a page that draws itself afterwards: at most 15 seconds
    return kw


def required_methods(adv):
    """The bot-detection options a request asks for (they fall under the owner's evasion switch). Identity, headers, proxy, session, cookies, the browser and pace are not among them."""
    return gatecheck.flags_requested(adv)


class Worker:
    def __init__(self, job, fetchers=None, sleep=time.sleep, clock=time.monotonic, environ=None):
        self.job, self.f = job, fetchers or Fetchers()
        self.sleep, self.clock, self.env = sleep, clock, environ if environ is not None else os.environ
        self.agent = job["user_agent"]
        self.delay0, self.max_delay = float(job.get("delay", 5)), float(job.get("max_delay", 60))
        self.sites, self.robots, self.rotor = {}, {}, 0
        self.policy = job.get("robots", {"mode": "obey", "overrides": []})
        self.private_ok = bool(job.get("allow_private_for_tests"))
        self.controls = {"custom_identity": True, "proxy": True, "session": True, "browser": True, "account_cookies": True, "evasion": True, **(job.get("controls") or {})}
        self.contact = job.get("contact", "")
        self._want_links = False
        if "guards" in job:                                                                    # the owner's guard switches, sent with the job (this is a separate process)
            guards.configure(job["guards"])
        self.retry = {"attempts": 3, "backoff_seconds": 2, **(job.get("retry") or {})}
        self.autothrottle = bool(job.get("autothrottle", True))          # pace follows how fast the site answers (a site that slows down is asked less often), never faster than the configured pace
        self._t0 = None
        self._extract = []

    # ---- helpers ---------------------------------------------------------------------------------------------------------------------------

    def _site(self, host):
        return self.sites.setdefault(host, {"delay": self.delay0, "last": None, "n": 0, "rest": "", "lat": None})

    def _wait(self, host):
        site = self._site(host)
        if site["last"] is not None:
            gap = max(site["delay"], self._latency_floor(site)) - (self.clock() - site["last"])
            if gap > 0:
                self.sleep(gap)

    def _latency_floor(self, site):
        return min(self.max_delay, 2.0 * site["lat"]) if self.autothrottle and site.get("lat") else 0.0

    def _done(self, host, ok, retry_after=None):
        site = self._site(host)
        if self.autothrottle and self._t0 is not None:
            took, self._t0 = max(0.0, self.clock() - self._t0), None
            site["lat"] = took if site.get("lat") is None else 0.7 * site["lat"] + 0.3 * took
        site["last"], site["n"] = self.clock(), site["n"] + 1
        if not ok:
            site["delay"] = min(max(site["delay"] * 2, retry_after or 0), self.max_delay)

    def _guard(self, url):
        try:
            clean = url if self.private_ok else (safety.normalize_public_url(url) if guards.on("private_address_block") else safety.normalize_fetch_url(url))
        except safety.UnsafeURL:
            raise _Refused("BAD_INPUT", "only public http(s) web addresses are fetched" if guards.on("private_address_block") else "only http(s) addresses are fetched") from None
        host = urlsplit(clean).hostname or ""
        if guards.on("private_address_block") and not self.private_ok and not safety.resolves_public(host):
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

    @staticmethod
    def _token(agent):
        """The product token robots.txt rules are written against: the first word of the identity, before any slash or space."""
        import re
        found = re.match(r"[A-Za-z0-9_.\-]+", str(agent))
        return found.group(0) if found else "*"

    def _agent_for(self, adv):
        """The identity string used for a request and for robots.txt: the honest one unless the person set a declared identity (never together with browser impersonation)."""
        return adv.get("identity") if adv.get("identity") and not adv.get("impersonate") else self.agent

    def _headers_for(self, adv):
        """Request headers: the identity, a From: header naming us (always, whatever the identity), the Accept-Language the person chose, and their extra headers."""
        if adv.get("impersonate"):
            return {}
        headers = {"User-Agent": self._agent_for(adv)}
        if self.contact:
            headers["From"] = self.contact
        if adv.get("accept_language"):
            headers["Accept-Language"] = adv["accept_language"]
        headers.update({k: v for k, v in (adv.get("headers") or {}).items()})
        return headers

    def _honest(self, url, agent=None, **extra):
        headers = {"User-Agent": agent or self.agent}
        if self.contact:
            headers["From"] = self.contact
        return self.f.http_get(url, stealthy_headers=False, impersonate=None, headers=headers, timeout=self.job.get("timeout", 20), follow_redirects=False, **extra)

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

    def _robots_for(self, url, proxy=None, agent=None):
        from protego import Protego
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        key = (origin, agent or self.agent)
        if key in self.robots:
            return self.robots[key]
        closed, note, parser = False, "", None
        try:
            self._wait(parts.netloc)
            r = self._honest(origin + "/robots.txt", agent=agent, **({"proxy": proxy} if proxy else {}))      # through the same proxy as the page, never around it
            self._done(parts.netloc, ok=r.status < 500)
            if r.status == 200:
                parser = Protego.parse(r.body.decode("utf-8", errors="replace").removeprefix("\ufeff"))
            elif 400 <= r.status < 500:
                parser, note = Protego.parse(""), "no robots.txt"
            else:
                closed, note = True, f"robots.txt answered {r.status}; treated as closed for this run"
        except Exception as exc:
            closed, note = True, f"robots.txt could not be read ({exc.__class__.__name__}); treated as closed for this run"
        self.robots[key] = (parser or Protego.parse("User-agent: *\nDisallow: /"), closed, note)
        return self.robots[key]

    # ---- one address -----------------------------------------------------------------------------------------------------------------------

    def one(self, item):
        url, adv = item["url"], item.get("advanced") or {}
        out = {"url": url, "engine": "scrapling", "ok": False, "status": 0, "advanced_used": [], "robots_note": "",
               "identity": "declared" if adv.get("identity") and not adv.get("impersonate") else ("browser" if adv.get("impersonate") else "honest")}
        try:
            clean, host = self._guard(url)
            for flag, control, what in (("identity", "custom_identity", "a declared identity"), ("headers", "custom_identity", "extra headers"), ("proxy_envs", "proxy", "a proxy"), ("session", "session", "a session"),
                                        ("stealth", "browser", "the browser"), ("cookies_env", "account_cookies", "account cookies")):
                if adv.get(flag) and not self.controls.get(control):
                    raise _Refused("BAD_INPUT", f"{what} was asked for but is switched off in access_controls")
            evasion = required_methods(adv)
            if evasion:
                if not self.controls.get("evasion"):
                    raise _Refused("BAD_INPUT", f"{', '.join(evasion)} was asked for but evasion is switched off in access_controls")
                out["advanced_used"] = evasion
            elif adv.get("cookies_env"):
                out["advanced_used"] = ["account_cookies"]
            site = self._site(host)
            if site["rest"]:
                raise _Refused("RATE_LIMITED", f"{host} is resting this run ({site['rest']})")
            proxy = self._proxy_for(adv)
            agent = self._agent_for(adv)
            if guards.on("robots_txt"):
                parser, closed, note = self._robots_for(clean, proxy, agent)
                mode, basis = self._robots_mode(host)
            else:                                                                                  # the owner has not asked for robots.txt to be consulted
                parser, closed, note, mode, basis = None, False, "", "ignore", ""
            if adv.get("delay"):
                site["delay"] = max(site["delay"], min(float(adv["delay"]), self.max_delay))      # a person may only slow the pace
            if parser is not None and (closed or not parser.can_fetch(clean, self._token(agent))):
                if mode == "obey":
                    raise _Refused("PLATFORM_BLOCKED", f"{clean} was not fetched: " + (note if closed else "robots.txt does not allow this address"), robots_path=not closed)
                out["robots_note"] = (note if closed else "robots.txt does not allow this address") + f"; read under crawl policy '{mode}'" + (f" ({basis})" if basis else "")
            crawl = parser.crawl_delay(self._token(agent)) if parser else None
            if crawl:
                site["delay"] = max(site["delay"], min(float(crawl), self.max_delay))
            resp = self._request(clean, host, adv, out, proxy)
        except _Refused as exc:
            out["error"] = {"code": exc.code, "message": _scrub(exc), "robots_path": exc.robots_path}
            return out
        except Exception as exc:
            out["error"] = {"code": "UPSTREAM_BROKEN", "message": _scrub(f"{exc.__class__.__name__}: {exc}"), "robots_path": False}
            return out
        if item.get("raw"):                                                                       # the body as it is (a sitemap, a feed): same guards, no page parsing
            out.update(ok=True, status=int(resp.status), raw=bytes(resp.body or b"")[:2_000_000].decode("utf-8", errors="replace"))
            return out
        self._extract = item.get("extract") or []
        return self._read(resp, clean, out, adv)

    def _extracted(self, resp):
        """Named values pulled out with Scrapling's parser tools: css, xpath, text (find_by_text), regex (find_by_regex) and similar (find_similar from the first css match)."""
        found = {}
        for spec in self._extract[:20]:
            name = str(spec.get("name") or "")[:60]
            try:
                if spec.get("css"):
                    hits = list(resp.css(spec["css"]))
                elif spec.get("xpath"):
                    hits = list(resp.xpath(spec["xpath"]))
                elif spec.get("text"):
                    hits = list(resp.find_by_text(spec["text"], first_match=False, partial=True))
                elif spec.get("regex"):
                    hits = list(resp.find_by_regex(spec["regex"], first_match=False))
                elif spec.get("similar"):
                    first = resp.css(spec["similar"])
                    hits = [first[0], *first[0].find_similar()] if first else []
                else:
                    continue
                found[name] = [" ".join(str(h.get_all_text() if hasattr(h, "get_all_text") else h).split())[:300] for h in hits[:20]]
            except Exception as exc:                                                              # one bad selector never loses the page
                found[name] = {"error": exc.__class__.__name__}
        return found

    def _sitemap_urls(self, clean, adv, root, limit):
        """Same-site page addresses from robots.txt's Sitemap lines and /sitemap.xml (index files followed to at most 5 children, 8 files in all, 2000 addresses). Each file is read through the same
        guarded fetch as a page, so robots.txt and the host's rest apply; compressed (.gz) files are skipped."""
        parts = urlsplit(clean)
        base = f"{parts.scheme}://{parts.netloc}"
        listed = []
        try:
            parser, _closed, _note = self._robots_for(clean, self._proxy_for(adv), self._agent_for(adv))
            listed = [str(u) for u in (list(getattr(parser, "sitemaps", None) or []) if parser else [])]
        except Exception:                                                                         # robots.txt unreadable: the usual place is still tried
            listed = []
        queue, maps, urls = deque(dict.fromkeys(listed + [base + "/sitemap.xml"])), set(), []
        while queue and len(maps) < 8 and len(urls) < 2000:
            address = queue.popleft()
            if address in maps or address.endswith(".gz") or not _same_site(address, root):
                continue
            maps.add(address)
            res = self.one({"url": address, "advanced": adv, "raw": True})
            if not res.get("ok"):
                if (res.get("error") or {}).get("code") in ("PLATFORM_BLOCKED", "RATE_LIMITED") and not (res.get("error") or {}).get("robots_path"):
                    break
                continue
            text = res.get("raw", "")
            locs = [html_lib.unescape(m.strip()) for m in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", text, re.S | re.I)]
            if "<sitemapindex" in text[:2000].lower():
                queue.extend(locs[:5])
            else:
                urls.extend(u for u in locs if urlsplit(u).scheme in ("http", "https") and _same_site(u, root))
        return urls[:limit]

    def _links_of(self, resp, page, base):
        try:
            found = list(self.f.links(resp, urlsplit(base).hostname or ""))
        except Exception:                                                                         # the library's extractor failed: fall back to the links the built-in parser saw
            found = [urljoin(base, link["href"]) for link in page.get("links", []) if link.get("href")]
        return [u for u in found if urlsplit(u).scheme in ("http", "https")][:400]

    def crawl(self, item):
        """Read a site's pages breadth-first from one start address, every page through the same guarded read as any other (public addresses only, robots.txt under the job's policy, pacing, a
        resting host). It stays on the start site, never reads more than max_pages (at most 200) or goes deeper than depth (at most 6), and stops the whole crawl at the first block, challenge or
        rate limit: nothing is retried after a refusal."""
        spec = item.get("crawl") or {}
        extract = item.get("extract") or []
        max_pages, depth = max(1, min(int(spec.get("max_pages", 25)), 200)), max(0, min(int(spec.get("depth", 2)), 6))
        start = item["url"]
        out = {"url": start, "engine": "scrapling", "mode": "crawl", "ok": False, "pages": [], "skipped": [], "stopped": ""}
        try:
            clean, host = self._guard(start)
        except _Refused as exc:
            out["error"] = {"code": exc.code, "message": _scrub(exc), "robots_path": False}
            return out
        root = _site_key(host)
        resume = spec.get("resume") or {}
        if resume.get("queue"):                                                                   # a paused crawl carries on from where it stopped: nothing already read is read again
            queue = deque((u, int(lv)) for u, lv in resume["queue"] if _same_site(u, root))
            seen = set(resume.get("seen") or []) | {_norm(u) for u, _ in queue}
        else:
            queue, seen = deque([(clean, 0)]), {_norm(clean)}
            if spec.get("sitemap"):
                found = self._sitemap_urls(clean, item.get("advanced") or {}, root, max_pages * 4)
                out["sitemap"] = {"addresses": len(found)}
                for u in found:
                    if _norm(u) not in seen and len(seen) < 5000:
                        seen.add(_norm(u))
                        queue.append((u, 0))
        failures = 0
        self._want_links = True
        try:
            while queue and len(out["pages"]) < max_pages:
                url, level = queue.popleft()
                res = self.one({"url": url, "advanced": item.get("advanced") or {}, "extract": extract})
                if res.get("ok") and not _same_site(res.get("final_url") or url, root):
                    res = {"ok": False, "error": {"code": "OFF_SITE", "message": f"{url} led to another site and was not read", "robots_path": False}}
                if not res.get("ok"):
                    err = res.get("error") or {}
                    out["skipped"].append({"url": url, "code": err.get("code", ""), "message": err.get("message", "")})
                    if err.get("code") in ("PLATFORM_BLOCKED", "RATE_LIMITED") and not err.get("robots_path"):
                        out["stopped"] = f"{err['code']}: {err.get('message', '')}"
                        break
                    failures += 1
                    if failures >= 5:
                        out["stopped"] = "five pages in a row could not be read"
                        break
                    continue
                failures = 0
                links = res.pop("links", [])
                out["pages"].append({k: res.get(k) for k in ("url", "final_url", "status", "title", "description", "h1", "markdown", "text", "chars", "robots_note", "extracted")})
                if level < depth:
                    for link in links:
                        key = _norm(link)
                        if key not in seen and len(seen) < 5000 and _same_site(link, root):
                            seen.add(key)
                            queue.append((link, level + 1))
        finally:
            self._want_links = False
        if not out["stopped"]:
            out["stopped"] = "page limit reached" if len(out["pages"]) >= max_pages and queue else "no more pages to follow"
        if queue:                                                                                 # what is left, so a later run can resume (the caller decides whether to keep it)
            out["state"] = {"queue": [[u, lv] for u, lv in list(queue)[:5000]], "seen": sorted(seen)[:20000]}
        out["ok"] = bool(out["pages"])
        if not out["ok"]:
            first = out["skipped"][0] if out["skipped"] else {"code": "UPSTREAM_BROKEN", "message": "nothing could be read"}
            out["error"] = {"code": first["code"] if first["code"] != "OFF_SITE" else "UPSTREAM_BROKEN", "message": first["message"], "robots_path": False}
        return out

    def _request(self, url, host, adv, out, proxy=None):
        hops, current, tries = 0, url, 0
        while True:
            current, host = self._guard(current)
            self._wait(host)
            self._t0 = self.clock()
            kw = {}
            if adv.get("stealth"):
                kw = stealth_kwargs(adv, self._agent_for(adv), self.job.get("timeout", 20))
                extra = {k: v for k, v in self._headers_for(adv).items() if k.lower() != "user-agent"}
                if extra:
                    kw["extra_headers"] = extra
                if proxy:
                    kw["proxy"] = proxy
                if adv.get("cookies_env"):
                    raw = self.env.get(adv["cookies_env"], "")
                    try:
                        jar = json.loads(raw) if raw else None
                    except ValueError:
                        raise _Refused("NEED_SETUP", "the cookies secret is not valid JSON") from None
                    if not jar:
                        raise _Refused("NEED_SETUP", f"{adv['cookies_env']} is not set")
                    kw["cookies"] = [{"name": str(k), "value": str(v), "url": current} for k, v in jar.items()]
                try:
                    resp = self.f.stealth_get(current, **kw)
                    out["fetched_with"] = "browser"
                except Exception as exc:
                    if _browser_missing(exc):                                                       # no browser installed here: read the page over plain HTTP instead, and say so
                        out["browser_note"] = "no browser is installed here, so the page was read over plain HTTP"
                        out["browser_error"] = _short_error(exc, [str(c["value"]) for c in kw.get("cookies", [])])       # what the browser library really said, so a wrong or missing browser is never hidden
                        out["fetched_with"] = "plain HTTP"
                        adv = {**adv, "stealth": False}
                        continue
                    raise
            else:
                kw = {"timeout": self.job.get("timeout", 20), "follow_redirects": False, "stealthy_headers": bool(adv.get("fingerprint")), "impersonate": "chrome" if adv.get("impersonate") else None}
                headers = self._headers_for(adv)
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
                    resp = (self.f.session_get(host, current, headers=headers, **kw) if adv.get("session") else self.f.http_get(current, headers=headers, **kw))
                except Exception as exc:
                    if kw.get("proxy") and self.f.is_proxy_error(exc):
                        self._done(host, ok=False)
                        raise _Refused("UPSTREAM_BROKEN", "the proxy failed: " + _scrub(str(exc).replace(kw["proxy"], "***"))) from None
                    raise
            status = int(resp.status)
            heads = {str(k).lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
            if self.job.get("return_html"):                                                  # a stored-session check judges the page, so it keeps the last one it saw, refused or not
                out["fetched_with"] = out.get("fetched_with") or "plain HTTP"
                out["status"], out["final_url"] = status, current
                _keep_raw(out, bytes(resp.body or b"").decode("utf-8", errors="replace"))
                out["cookie_names_back"] = _cookie_names(getattr(resp, "cookies", None))                                # names only, never a value
                try:                                                                              # the title and the visible words of the page, whatever happens to it next (a refusal, a login wall)
                    seen = htmlx.parse_page(out["html"], current, 4000)
                    out["title"], out["text"] = seen["title"], seen["text"][:3000]
                except Exception:
                    pass
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
            refused = ("PLATFORM_BLOCKED", "a bot-check or challenge page was shown", f"{host} showed a challenge or bot-check page" + ("" if adv.get("solve_cloudflare") else "; it was not worked around")) \
                if signals.looks_like_challenge(status, heads, bytes(resp.body or b"")) else \
                ("RATE_LIMITED", "asked us to slow down", f"{host} asked us to slow down (429)") if status == 429 else \
                ("PLATFORM_BLOCKED", f"refused the request ({status})", f"{host} refused the request ({status}); it rests and is not tried again with another identity or proxy") if status in (401, 403) else None
            if refused:
                code, why, message = refused
                if guards.on("rest_after_refusal"):                                          # the owner's rest guard: the host rests, nothing is retried
                    self._site(host)["rest"] = why
                    raise _Refused(code, message)
                tries += 1                                                                   # guard OFF: try again a few times, then mark it failed and move on to the next address
                if tries < int(self.retry["attempts"]):
                    self.sleep(float(self.retry["backoff_seconds"]) * tries)
                    continue
                raise _Refused(code, f"{host} still refused after {tries} attempt(s) ({why}); marked failed and moved on")
            if status in (404, 410):
                raise _Refused("NOT_FOUND", f"{current} does not exist ({status})")
            if status >= 400:
                raise _Refused("UPSTREAM_BROKEN", f"{host} answered {status}")
            out["final_url"] = current
            return resp

    def _read(self, resp, url, out, adv):
        body = bytes(resp.body or b"")
        html = body.decode("utf-8", errors="replace")
        out["status"] = int(resp.status)
        if self.job.get("return_html"):                                                    # only a stored-session check asks for the raw page: it judges logged-in markers that are not in the text
            _keep_raw(out, html)
        page = htmlx.parse_page(html, out.get("final_url", url), self.job.get("max_chars", 20000))
        if signals.looks_like_login_wall(body, page["title"]):
            out["error"] = {"code": "UPSTREAM_BROKEN", "message": f"{url} shows only a login page", "robots_path": False}
            return out
        try:
            markdown = resp.markdown()
        except Exception as exc:
            markdown, out["markdown_note"] = page["text"], f"Scrapling's Markdown was not available ({exc.__class__.__name__}); the built-in text is used"
        out.update(ok=True, status=int(resp.status), title=page["title"], description=page["description"], h1=page["h1"], headings=page["headings"][:25], announcements=page["announcements"],
                   offers=htmlx.extract_offers(page["text"], " ".join(page["announcements"])), alternates=page["alternates"][:8], products=htmlx.jsonld_products(page["jsonld"]), social=htmlx.social_links(page["links"], page["jsonld"]),
                   markdown=safety.clean_text(markdown)[:self.job.get("max_chars", 20000)], text=page["text"], chars=page["chars"])
        if self._extract:
            out["extracted"] = self._extracted(resp)
        if self._want_links:                                                                   # only while crawling: the links the crawl may follow
            out["links"] = self._links_of(resp, page, out.get("final_url", url))
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


RAW_PAGE_CAP = 2_000_000                                                                 # the most of a page's code a stored-session check keeps: logged-in markers can sit deep inside it


def _keep_raw(out, html):
    """The page's code for a stored-session check, bounded, with a note when it was cut."""
    out["html"] = html[:RAW_PAGE_CAP]
    out["html_truncated"] = len(html) > RAW_PAGE_CAP


def _cookie_names(cookies):
    """The NAMES of the cookies in a response, whatever shape the library gives them: a dict of name to value (plain HTTP), a tuple of cookie objects with a name key (a browser), pairs, or objects with
    a name. Never a value, and never an error: a shape it does not know gives no names, not a crash."""
    names = []
    try:
        if isinstance(cookies, dict):
            names = [str(k) for k in cookies]
        else:
            for item in cookies or ():
                if isinstance(item, dict):
                    name = item.get("name")
                elif isinstance(item, (tuple, list)):
                    name = item[0] if item else None
                else:
                    name = getattr(item, "name", None)
                if name:
                    names.append(str(name))
    except Exception:
        return []
    return sorted(set(names))[:40]


def _short_error(exc, secrets=()):
    """The first line of an error, 300 characters at most, with any cookie value hidden."""
    text = " ".join(str(exc).split())
    for value in secrets:
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text[:300]


def _browser_missing(exc):
    text = str(exc).lower()
    return ("executable doesn't exist" in text or "playwright install" in text or "patchright install" in text or "browser is not installed" in text
            or ("install" in text and "browser" in text and "download" in text))


def _site_key(host):
    return (host or "").lower().removeprefix("www.")


def _same_site(url, root):
    host = _site_key(urlsplit(url).hostname)
    return host == root or host.endswith("." + root)


def _norm(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), parts.path.rstrip("/") or "/", parts.query, ""))


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
    try:
        results = [worker.crawl(item) if item.get("crawl") else worker.one(item) for item in job.get("items", [])]
    finally:
        close = getattr(worker.f, "close", None)
        if close:
            close()
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
