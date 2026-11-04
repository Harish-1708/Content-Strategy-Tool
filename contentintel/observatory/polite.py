"""The polite fetcher: every page the observatory reads goes through here and nowhere else.

What it does, and what it deliberately never does:

  * robots.txt is always obeyed (the Protego parser, as Scrapling uses). If the file cannot be read because the site
    errors, the site is treated as closed for this run; a missing file (404) means open. Crawl-delay and Request-rate are honoured.
  * An honest identity: the user agent names this tool and carries a contact address. There is no browser impersonation,
    no made-up referer and no rotating identity or proxy.
  * One request at a time per site, with a pause between them that doubles after a block (up to a cap) and obeys Retry-After.
    A site that rate-limits us rests for an hour and the rest is remembered between runs.
  * Every address, and every redirect hop, is checked to be a public web address first; a name that points inside a private
    network is refused.
  * A challenge or login page is *recognised* and reported (PLATFORM_BLOCKED); it is never solved or worked around.

The transport is injectable, so every behaviour above is tested offline with scripted responses.
"""
import gzip
import io
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from .. import contract as K
from .. import safety
from .config import USER_AGENT_TOKEN
from . import signals

try:                                    # the robots parser Scrapling uses (BSD 3-Clause)
    from protego import Protego
except ImportError:                     # pragma: no cover - reported as NEED_SETUP when a client is made
    Protego = None

looks_like_challenge = signals.looks_like_challenge          # kept here too, where callers and tests have always found them
looks_like_login_wall = signals.looks_like_login_wall
parse_retry_after = signals.parse_retry_after


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    from_cache: bool = False
    waited: float = 0.0
    robots_note: str = ""            # set when the page was read although robots.txt asked crawlers not to: the policy that allowed it

    def text(self):
        match = re.search(r"charset=([\w\-]+)", self.headers.get("content-type", ""), re.I)
        for encoding in ([match.group(1)] if match else []) + ["utf-8"]:
            try:
                return self.body.decode(encoding, errors="replace")
            except LookupError:
                continue
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8-sig", errors="replace"))


def default_http(method, url, headers, timeout, max_bytes):
    """(status, lowercase headers, body). Never follows a redirect (the client checks every hop) and never reads past max_bytes."""
    import requests
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
    except requests.exceptions.Timeout as exc:
        raise TimeoutError(str(exc)) from None
    except requests.exceptions.RequestException as exc:
        raise OSError(exc.__class__.__name__) from None
    try:
        chunks, size = [], 0
        for chunk in resp.iter_content(65536):
            size += len(chunk)
            chunks.append(chunk)
            if size > max_bytes:
                break
        body = b"".join(chunks)[:max_bytes + 1]
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, body
    finally:
        resp.close()


@dataclass
class _Site:
    delay: float
    last: float = None
    ok_streak: int = 0
    requests: int = 0
    blocks: int = 0
    waited: float = 0.0
    public: bool = None


@dataclass
class _Robots:
    parser: object
    fetched: float
    note: str = ""
    closed: bool = False


class PoliteClient:
    def __init__(self, settings, *, contact=None, http=default_http, clock=time.monotonic, sleep=time.sleep, now_fn=utc_now, ledger=None,
                 resolver=None, cache=True):
        if Protego is None:
            raise K.Failure(K.NEED_SETUP, "the robots.txt reader is not installed (pip install protego)")
        self.cfg = settings["politeness"]
        self.policy = settings.get("crawl_policy") or {"robots": "obey", "basis": "", "overrides": []}
        contact = (contact or self.cfg.get("contact") or "").strip()
        if not contact:
            raise K.Failure(K.NEED_SETUP, "no contact address for the identifying user agent: set OBSERVATORY_CONTACT (an email address or web page "
                            "a site owner can write to) or politeness.contact in observatory.yaml")
        if any(c in contact for c in "\r\n()<>"):
            raise K.Failure(K.BAD_INPUT, "the contact address contains characters that are not allowed in a user agent")
        self.user_agent = f"{USER_AGENT_TOKEN}/1.0 (research; contact: {contact})"
        self.http, self.clock, self.sleep, self.now_fn, self.ledger = http, clock, sleep, now_fn, ledger
        self.resolver = resolver
        self._sites, self._robots, self._cache, self.use_cache = {}, {}, {}, cache
        self.events = []                        # what happened, in words, for the run report

    # ---- state -----------------------------------------------------------------------------------------------------------------------------

    def _site(self, host):
        site = self._sites.get(host)
        if site is None:
            hint = self.ledger.delay_hint(host) if self.ledger else None
            start = max(self.cfg["min_delay_seconds"], min((hint or 0) / 2, self.cfg["max_delay_seconds"]))    # a delay learned last time, halved
            site = self._sites[host] = _Site(delay=start)
        return site

    def delay_for(self, host):
        return self._site(host).delay

    def report(self):
        return {"sites": {h: {"requests": s.requests, "delay_seconds": round(s.delay, 1), "blocks": s.blocks, "waited_seconds": round(s.waited, 1)}
                          for h, s in sorted(self._sites.items())}, "events": list(self.events)}

    # ---- the guards ------------------------------------------------------------------------------------------------------------------------

    def _guard(self, url):
        try:
            clean = safety.normalize_public_url(url)
        except safety.UnsafeURL:
            raise K.Failure(K.BAD_INPUT, "only public http(s) web addresses are fetched") from None
        host = urlsplit(clean).hostname
        site = self._site(host)
        if site.public is None:
            site.public = safety.resolves_public(host, self.resolver) if self.resolver else safety.resolves_public(host)
        if not site.public:
            raise K.Failure(K.BAD_INPUT, f"{host} does not point to a public address")
        return clean, host

    def _check_pause(self, host):
        if self.ledger:
            rest = self.ledger.paused(host, self.now_fn())
            if rest:
                code = rest[2] if rest[2] in K.CODES else K.RATE_LIMITED
                raise K.Failure(code, f"{host} is resting until {rest[1]} ({rest[0]})", retry_after=None)

    def _robots_for(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        found = self._robots.get(origin)
        if found and self.clock() - found.fetched < self.cfg["robots_cache_minutes"] * 60:
            return found
        closed, note, parser = False, "", None
        try:
            got = self._request_with_retries(origin + "/robots.txt", robots=True, max_bytes=500_000)
            if got.status == 200:
                parser = Protego.parse(got.text().removeprefix("\ufeff"))
            elif 400 <= got.status < 500:
                parser, note = Protego.parse(""), f"no robots.txt ({got.status}); the site is open"
            else:
                closed, note = True, f"robots.txt answered {got.status}; treated as closed for this run"
        except K.Failure as exc:
            if exc.code in (K.NOT_FOUND,):
                parser, note = Protego.parse(""), "no robots.txt; the site is open"
            else:
                closed, note = True, f"robots.txt could not be read ({exc.code}); treated as closed for this run"
        except (TimeoutError, OSError) as exc:
            closed, note = True, f"robots.txt could not be read ({exc.__class__.__name__}); treated as closed for this run"
        found = self._robots[origin] = _Robots(parser or Protego.parse("User-agent: *\nDisallow: /"), self.clock(), note, closed)
        if note:
            self.events.append(f"{parts.netloc}: {note}")
        return found

    def robots_mode(self, host):
        """(mode, basis text) for a host: a recorded override for it, else the general crawl policy. obey / advisory / ignore."""
        host = (host or "").lower().removeprefix("www.")
        while host:
            for entry in self.policy.get("overrides", []):
                if entry["host"] == host:
                    return entry["robots"], f"override for {entry['host']}: {entry['basis']}, approved by {entry['approved_by']} on {entry['date']}"
            host = host.partition(".")[2] if "." in host else ""
        return self.policy.get("robots", "obey"), self.policy.get("basis", "")

    def robots_allows(self, url):
        clean, _ = self._guard(url)
        rb = self._robots_for(clean)
        return (not rb.closed) and bool(rb.parser.can_fetch(clean, USER_AGENT_TOKEN))

    def sitemaps_from_robots(self, url):
        """Sitemap addresses a site lists in its robots.txt."""
        clean, _ = self._guard(url)
        rb = self._robots_for(clean)
        return [s for s in (rb.parser.sitemaps or []) if safety.is_public_url(s)] if not rb.closed else []

    # ---- the throttle ----------------------------------------------------------------------------------------------------------------------

    def _wait_turn(self, host, parser=None):
        site = self._site(host)
        delay = site.delay
        if parser is not None:
            crawl = parser.crawl_delay(USER_AGENT_TOKEN)
            rate = parser.request_rate(USER_AGENT_TOKEN)
            if crawl:
                delay = max(delay, min(float(crawl), self.cfg["max_delay_seconds"]))
            elif rate and getattr(rate, "requests", 0):
                delay = max(delay, min(rate.seconds / rate.requests, self.cfg["max_delay_seconds"]))
        if site.last is not None:
            gap = delay - (self.clock() - site.last)
            if gap > 0:
                self.sleep(gap)
                site.waited += gap
        return site

    def _after(self, host, status, headers, ok):
        site = self._site(host)
        site.last = self.clock()
        site.requests += 1
        if ok:
            site.ok_streak += 1
            if site.ok_streak >= 5 and site.delay > self.cfg["min_delay_seconds"]:
                site.delay = max(self.cfg["min_delay_seconds"], site.delay / 2)
                site.ok_streak = 0
        else:
            site.ok_streak, site.blocks = 0, site.blocks + 1
            asked = parse_retry_after(headers.get("retry-after"))
            site.delay = min(max(site.delay * 2, asked or 0, self.cfg["min_delay_seconds"]), self.cfg["max_delay_seconds"])
            self.events.append(f"{host}: answered {status}; the pause between requests is now {site.delay:g} seconds")
        if self.ledger:
            self.ledger.remember_delay(host, site.delay, self.now_fn())
        return site

    # ---- one request, without robots (robots.txt itself) or with it -----------------------------------------------------------------------

    def _request(self, url, *, robots=False, max_bytes=None, accept="*/*", parser=None):
        clean, host = self._guard(url)
        self._check_pause(host)
        site = self._site(host)
        if site.requests >= self.cfg["max_requests_per_domain"]:
            failure = K.Failure(K.BAD_INPUT, f"the request limit for {host} in one run ({self.cfg['max_requests_per_domain']}) is used up; "
                                "the rest waits for the next run")
            failure.budget = True                       # our own ceiling, not the site's doing: a caller can stop gracefully and say what is left
            raise failure
        limit = max_bytes or self.cfg["max_bytes"]
        self._wait_turn(host, parser)
        headers = {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "gzip, deflate"}
        try:
            status, got_headers, body = self.http("GET", clean, headers, self.cfg["timeout_seconds"], limit)
        except TimeoutError:
            self._after(host, 0, {}, ok=False)
            raise _transient(K.TIMEOUT, f"{host} did not answer within {self.cfg['timeout_seconds']} seconds") from None
        except OSError as exc:
            self._after(host, 0, {}, ok=False)
            raise _transient(K.UPSTREAM_BROKEN, f"{host} could not be reached ({exc.__class__.__name__})") from None
        if len(body) > limit:
            self._after(host, status, got_headers, ok=False)
            raise K.Failure(K.UPSTREAM_BROKEN, f"the answer from {host} is larger than the allowed {limit} bytes and was not read")
        self._classify(host, clean, status, got_headers, body)
        return Fetched(clean, clean, status, got_headers, body)

    def _classify(self, host, url, status, headers, body):
        """Turn a response into success or the right failure, updating the throttle and the resting ledger."""
        if looks_like_challenge(status, headers, body):
            self._after(host, status, headers, ok=False)
            self._rest(host, self.cfg["rate_limit_pause_seconds"], "a bot-check or challenge page was shown", K.PLATFORM_BLOCKED)
            raise K.Failure(K.PLATFORM_BLOCKED, f"{host} showed a challenge or bot-check page; it was not worked around and the site now rests")
        if status in (301, 302, 303, 307, 308):
            self._after(host, status, headers, ok=True)
            return
        if status == 429:
            self._after(host, status, headers, ok=False)
            asked = parse_retry_after(headers.get("retry-after"))
            self._rest(host, max(self.cfg["rate_limit_pause_seconds"], asked or 0), "the site said it was being asked too often", K.RATE_LIMITED)
            raise K.Failure(K.RATE_LIMITED, f"{host} asked us to slow down (429); it rests for now", retry_after=asked)
        if status in (401, 403):
            self._after(host, status, headers, ok=False)
            raise K.Failure(K.PLATFORM_BLOCKED, f"{host} refused the request ({status}); that is left alone")
        if status in (404, 410):
            self._after(host, status, headers, ok=True)
            raise K.Failure(K.NOT_FOUND, f"{url} does not exist ({status})")
        if status >= 500:
            self._after(host, status, headers, ok=False)
            raise _transient(K.UPSTREAM_BROKEN, f"{host} answered {status}")
        if status >= 400 or status < 200:
            self._after(host, status, headers, ok=False)
            raise K.Failure(K.UPSTREAM_BROKEN, f"{host} answered {status}")
        self._after(host, status, headers, ok=True)

    def _rest(self, host, seconds, reason, code):
        if self.ledger:
            self.ledger.pause(host, seconds, reason, self.now_fn(), code)
        self.events.append(f"{host}: resting for {int(seconds)} seconds ({reason})")

    def _request_with_retries(self, url, **kw):
        """A server error or a silence is tried again (settings: retries), each time after the throttle's longer pause; then the site rests."""
        attempt = 0
        while True:
            try:
                return self._request(url, **kw)
            except K.Failure as exc:
                if not getattr(exc, "transient", False):
                    raise
                attempt += 1
                host = urlsplit(url).hostname
                if attempt > self.cfg["retries"]:
                    self._rest(host, self.cfg["error_pause_seconds"], f"it kept failing ({exc.code})", K.UPSTREAM_BROKEN)
                    raise

    # ---- the public call -------------------------------------------------------------------------------------------------------------------

    def get(self, url, *, accept="*/*", max_bytes=None, use_cache=True):
        """The page, or a Failure with the right code. robots.txt, the throttle and the address checks all apply, hop by hop."""
        clean, host = self._guard(url)
        key = (clean, accept)
        if self.use_cache and use_cache and key in self._cache:
            stamp, fetched = self._cache[key]
            if self.clock() - stamp < self.cfg["cache_minutes"] * 60:
                return Fetched(fetched.url, fetched.final_url, fetched.status, fetched.headers, fetched.body, from_cache=True)
        current, hops = clean, 0
        while True:
            current, host = self._guard(current)
            self._check_pause(host)
            rb = self._robots_for(current)
            robots_note = ""
            if rb.closed or not rb.parser.can_fetch(current, USER_AGENT_TOKEN):
                why = rb.note if rb.closed else "robots.txt does not allow this address"
                mode, basis = self.robots_mode(host)
                if mode == "obey":
                    failure = K.Failure(K.PLATFORM_BLOCKED, f"{current} was not fetched: {why}", hint="The site's own rules say no; the tool respects them (crawl_policy in observatory.yaml).")
                    failure.robots_path = not rb.closed      # one address the site's rules exclude (skip it), as opposed to a site we cannot read the rules of
                    raise failure
                # robots.txt is a crawler preference, not an access control: under an advisory policy or a recorded override the page is read and the fact is recorded
                robots_note = f"{why}; read under crawl policy '{mode}'" + (f" ({basis})" if basis else "")
                self.events.append(f"{host}: {robots_note}")
            fetched = self._request_with_retries(current, max_bytes=max_bytes, accept=accept, parser=rb.parser)
            if fetched.status in (301, 302, 303, 307, 308):
                target = fetched.headers.get("location", "")
                hops += 1
                if not target or hops > self.cfg["max_redirects"]:
                    raise K.Failure(K.UPSTREAM_BROKEN, f"{host} redirected too many times or without a destination")
                current = urljoin(current, target)
                continue
            result = Fetched(clean, current, fetched.status, fetched.headers, fetched.body, waited=self._site(host).waited, robots_note=robots_note)
            if self.use_cache:
                self._cache[key] = (self.clock(), result)
            return result


def is_fatal(exc):
    """A failure that ends the work on a whole site: blocked, rate-limited, login wanted. A missing page, one address robots.txt excludes, or a
    server hiccup only costs that one request."""
    return exc.code in K.FINAL_CODES and exc.code != K.NOT_FOUND and not getattr(exc, "robots_path", False) and not getattr(exc, "budget", False)


def _transient(code, message):
    """A failure worth another try: the server erred or did not answer, as opposed to saying no."""
    failure = K.Failure(code, message)
    failure.transient = True
    return failure


def decompress(body, headers=None, url="", limit=25_000_000):
    """The body, un-gzipped when it is (by header, by name or by its first bytes), never growing past `limit` bytes."""
    if body[:2] == b"\x1f\x8b":
        out = io.BytesIO()
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    if out.tell() > limit:
                        raise K.Failure(K.UPSTREAM_BROKEN, "a compressed file expands past the allowed size and was not read")
        except (OSError, EOFError):
            raise K.Failure(K.UPSTREAM_BROKEN, "a compressed file could not be unpacked") from None
        return out.getvalue()
    if len(body) > limit:
        raise K.Failure(K.UPSTREAM_BROKEN, "a file is larger than the allowed size and was not read")
    return body
