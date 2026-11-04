"""Reading a public page through Jina Reader (https://r.jina.ai), a free third-party service that turns a page into clean Markdown.

It is useful for public research and content extraction. Three things to know, and the code follows them:

  * The service sees the address (and so the page we are interested in). Only public pages are sent: an address that carries something
    looking like a credential, or points inside a private network, is refused before anything leaves.
  * robots.txt is a crawler *preference*, not an access control. For this route the policy is configurable (crawl_policy.reader_robots: obey or
    advisory, with per-host overrides that need a recorded basis). Under 'advisory' the page is read and the report says robots.txt asked
    otherwise. A real access restriction (a login page, a bot check, a refusal, a rate limit) is never worked around, under any policy.
  * The free service allows 20 requests a minute without a key (500 with a free key); this client keeps below it.
"""
import os
import re
import time
from urllib.parse import urlsplit

from .. import contract as K
from .. import safety
from ..safety import clean_text
from . import polite

JINA = "https://r.jina.ai/"
_SECRET_QUERY = re.compile(r"(?:^|[?&])(?:access[_-]?token|auth|token|api[_-]?key|key|password|secret|signature|sig|session|sid|cookie|credential)=", re.I)
_WARNING = re.compile(r"Warning:\s*Target URL returned error (\d{3})", re.I)
_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare", "access denied", "verify you are human", "security check", "are you a robot")
_LOGIN = re.compile(r"^\s*(log\s*in|sign\s*in|login|sign up)\b", re.I)


def looks_blocked(body_text):
    """High-confidence challenge pages in what the reader returned (the Agent-Reach test): recognised so the run stops, never worked around."""
    head = body_text[:4096].lower()
    if "warning:" in head and "requiring captcha" in head:
        return True
    title = re.search(r"^title:\s*(.*)$", head, re.M)
    if title and any(title.group(1).strip().startswith(t) for t in _CHALLENGE_TITLES) and len(body_text) < 6000:
        return True
    return "performing security verification" in head or "/cdn-cgi/challenge-platform/" in head


class JinaReader:
    def __init__(self, settings, polite_client, *, api_key=None, http=polite.default_http, clock=time.monotonic, sleep=time.sleep, environ=None):
        self.cfg = settings["readers"]
        self.policy = settings["crawl_policy"]
        self.polite = polite_client
        self.api_key = (api_key if api_key is not None else (environ or os.environ).get("JINA_API_KEY", "")).strip()
        self.http, self.clock, self.sleep = http, clock, sleep
        self.calls = []                                              # times of recent calls, for the per-minute limit

    def mode_for(self, host):
        """obey or advisory (or ignore, from a recorded override) for reading this host through the reader, and the basis."""
        host = (host or "").lower().removeprefix("www.")
        while host:
            for entry in self.policy.get("overrides", []):
                if entry["host"] == host:
                    return entry["robots"], f"override for {entry['host']}: {entry['basis']}, approved by {entry['approved_by']} on {entry['date']}"
            host = host.partition(".")[2] if "." in host else ""
        return self.policy.get("reader_robots", "advisory"), self.policy.get("basis", "")

    def _wait(self):
        window = 60.0
        limit = self.cfg["jina_per_minute"]
        now = self.clock()
        self.calls = [t for t in self.calls if now - t < window]
        if len(self.calls) >= limit:
            gap = window - (now - self.calls[0])
            if gap > 0:
                self.sleep(gap)
        self.calls.append(self.clock())

    def read(self, url):
        """{'url','title','text','published','reader','robots_note','access_type','completeness_verified','third_party'} or a Failure."""
        if not self.cfg["jina"]:
            raise K.Failure(K.NEED_SETUP, "the Jina reader is switched off (readers.jina in observatory.yaml)")
        try:
            clean = safety.normalize_public_url(url)
        except safety.UnsafeURL:
            raise K.Failure(K.BAD_INPUT, "only public http(s) addresses are sent to the reader") from None
        parts = urlsplit(clean)
        if _SECRET_QUERY.search("?" + parts.query) or parts.fragment and _SECRET_QUERY.search("?" + parts.fragment):
            raise K.Failure(K.BAD_INPUT, "the address carries something that looks like a credential, so it is not sent to a third-party reader")
        host = parts.hostname
        mode, basis = self.mode_for(host)
        note = ""
        try:
            allowed = self.polite.robots_allows(clean)
        except K.Failure as exc:
            if exc.code in (K.BAD_INPUT,):
                raise
            allowed = False if exc.code == K.PLATFORM_BLOCKED else True      # a site we cannot ask about is neither forbidden nor permitted: the page may still be public
        if not allowed:
            if mode == "obey":
                failure = K.Failure(K.PLATFORM_BLOCKED, f"{clean} was not read: its robots.txt asks crawlers to stay away and the reader policy is 'obey'")
                failure.robots_path = True
                raise failure
            note = "robots.txt asks crawlers not to read this address; read through the reader under policy '" + mode + "'" + (f" ({basis})" if basis else "")
        self._wait()
        headers = {"User-Agent": self.polite.user_agent, "Accept": "text/plain"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            status, got, body = self.http("GET", JINA + clean, headers, 40, self.cfg["max_chars"] * 8)
        except TimeoutError:
            raise K.Failure(K.TIMEOUT, "the reader did not answer in time") from None
        except OSError:
            raise K.Failure(K.UPSTREAM_BROKEN, "the reader could not be reached") from None
        text = body.decode("utf-8", errors="replace")
        if status == 429:
            raise K.Failure(K.RATE_LIMITED, "the reader's free limit was reached; it rests before the next use", retry_after=polite.parse_retry_after(got.get("retry-after")))
        if status in (401, 402, 403) and self.api_key:
            raise K.Failure(K.NEED_SETUP, f"the reader rejected the API key ({status})")
        if status in (401, 402, 403):
            raise K.Failure(K.RATE_LIMITED if status == 402 else K.PLATFORM_BLOCKED, f"the reader refused the request ({status})")
        if status >= 500:
            raise K.Failure(K.UPSTREAM_BROKEN, f"the reader answered {status}")
        if status != 200:
            raise K.Failure(K.UPSTREAM_BROKEN, f"the reader answered {status}")
        if looks_blocked(text):
            raise K.Failure(K.PLATFORM_BLOCKED, f"{host} showed a bot-check or challenge page to the reader; it was not worked around")
        warn = _WARNING.search(text[:3000])
        if warn:
            code = int(warn.group(1))
            if code in (404, 410):
                raise K.Failure(K.NOT_FOUND, f"{clean} does not exist ({code})")
            if code in (401, 403, 451):
                raise K.Failure(K.PLATFORM_BLOCKED, f"{host} refused the reader ({code})")
            if code == 429:
                raise K.Failure(K.RATE_LIMITED, f"{host} asked the reader to slow down")
            if code >= 500:
                raise K.Failure(K.UPSTREAM_BROKEN, f"{host} answered {code}")
        title = re.search(r"^Title:\s*(.*)$", text, re.M)
        published = re.search(r"^Published Time:\s*(.*)$", text, re.M)
        content = text.split("Markdown Content:", 1)[1] if "Markdown Content:" in text else text
        content = " \n".join(l.rstrip() for l in clean_text(content).strip().splitlines())[:self.cfg["max_chars"]]
        title_text = " ".join(clean_text(title.group(1)).split())[:200] if title else ""
        if len(content) < 80 and (_LOGIN.match(title_text) or not content.strip() or content.strip().casefold() == title_text.casefold()):
            raise K.Failure(K.UPSTREAM_BROKEN, f"{clean} gave no readable content (empty, or only a login page)")
        return {"url": clean, "title": title_text, "text": content, "published": published.group(1).strip() if published else "", "reader": "jina_reader",
                "robots_note": note, "access_type": "page_text", "completeness_verified": False, "third_party": True}
