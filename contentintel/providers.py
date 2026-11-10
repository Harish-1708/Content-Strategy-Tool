"""Search providers behind one small interface: Serper.dev, Tavily and SerpApi.

Each adapter turns a query into a list of Result objects and knows what a search costs in
credits. Network access goes through an injectable `http` function so tests never touch the
internet. API keys are never written into errors or logs.
"""
import json
import re
import time
from dataclasses import dataclass

import requests

RECENCY = ("day", "week", "month", "year")
_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
_QUOTA_WORDS = ("not enough credits", "run out of searches", "out of searches", "usage limit",
                "quota", "exceeds your plan", "out of credits", "plan's set")


WORLDWIDE = ("worldwide", "all", "any", "global")


def search_country(country):
    """The two-letter code to send to a search service, or None for worldwide (no country is sent). Google itself has
    no 'worldwide' market: without a code its results lean towards its default market, so a truly wide search
    names several countries (one search each)."""
    if not country or str(country).strip().lower() in WORLDWIDE:
        return None
    return str(country).strip().lower()


class ProviderError(Exception):
    """A search failed; another provider may still answer."""


class ProviderConfigError(ProviderError):
    """Our own request cannot be accepted as it is configured. Nothing was run and nothing was spent; the provider is not at fault."""


class AuthError(ProviderError):
    """The key was rejected. Do not use this provider again in this run."""


class QuotaError(ProviderError):
    """The provider is out of credits. Do not use it again in this run."""


@dataclass(frozen=True)
class Result:
    rank: int
    title: str
    url: str
    snippet: str = ""
    date: str = None

    def to_dict(self):
        return {"rank": self.rank, "title": self.title, "url": self.url,
                "snippet": self.snippet, "date": self.date}


def default_http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
    resp = requests.request(method, url, params=params, json=json_body, headers=headers,
                            timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return resp.status_code, body, resp.headers.get("Retry-After")


def _shape(body):
    """What an answer contained, for diagnosing 'no results': key names with list lengths."""
    if not isinstance(body, dict):
        return type(body).__name__
    return ", ".join(f"{k} ({len(v)})" if isinstance(v, list) else k for k, v in body.items())


class Provider:
    name = ""
    label = ""
    env_var = ""
    kinds = ("web", "news")

    def __init__(self, api_key, http=None, sleep=None):
        self.api_key = api_key
        self.http = http or default_http
        self.sleep = sleep or time.sleep
        self.last_note = ""          # what the most recent answer contained (never the key)

    # -- shared plumbing ---------------------------------------------------------------
    def _clean(self, text):
        text = str(text)
        return text.replace(self.api_key, "***") if self.api_key else text

    def _call(self, method, url, *, params=None, json_body=None, headers=None, timeout=30, ok=(200,), stop=()):
        last = "no response"
        for attempt in range(3):
            try:
                status, body, retry_after = self.http(
                    method, url, params=params, json_body=json_body, headers=headers,
                    timeout=timeout)
            except Exception as exc:                      # network problem
                last = f"network error ({exc.__class__.__name__})"
                self.sleep(2 ** attempt)
                continue
            if status in ok:
                self.last_note = "response contained: " + _shape(body)
                return body
            text = body if isinstance(body, str) else json.dumps(body)
            low = text.lower()
            if status in (401, 403):
                raise AuthError(f"{self.label} rejected the API key (HTTP {status})")
            if status in (402, 432, 433) or any(w in low for w in _QUOTA_WORDS):
                raise QuotaError(f"{self.label} is out of credits (HTTP {status})")
            if status in stop:
                raise ProviderError(f"{self.label}: HTTP {status}: not repeated (repeating it could run, and charge for, the same job again)")
            if status == 429 or status >= 500:
                last = f"HTTP {status}"
                wait = int(retry_after) if str(retry_after or "").isdigit() else 2 ** attempt
                self.sleep(min(wait, 30))
                continue
            raise ProviderError(f"{self.label}: HTTP {status}: {self._clean(text)[:200]}")
        raise ProviderError(f"{self.label}: gave up after retries ({last})")

    # -- interface ---------------------------------------------------------------------
    def cost(self, kind="web", depth="basic"):
        return 1

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        raise NotImplementedError

    def remaining(self):
        """Credits the provider says are left, or None if it does not say."""
        return None


class SerperDev(Provider):
    name = "serper_dev"
    label = "Serper.dev"
    env_var = "SERPER_API_KEY"
    base = "https://google.serper.dev"

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        body = {"q": query, "num": min(num, 10)}
        if search_country(country):
            body["gl"] = search_country(country)
        if recency:
            body["tbs"] = _TBS[recency]
        path = "/news" if kind == "news" else "/search"
        data = self._call("POST", self.base + path, json_body=body,
                          headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"})
        items = data.get("news" if kind == "news" else "organic", []) if isinstance(data, dict) else []
        return [Result(rank=int(i.get("position") or n), title=i.get("title", ""),
                       url=i.get("link", ""), snippet=i.get("snippet", ""), date=i.get("date"))
                for n, i in enumerate(items, start=1) if i.get("link")]


class Tavily(Provider):
    name = "tavily"
    label = "Tavily"
    env_var = "TAVILY_API_KEY"
    base = "https://api.tavily.com"

    def cost(self, kind="web", depth="basic"):
        return 2 if depth == "advanced" else 1

    def _auth(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        body = {"query": query, "max_results": min(num, 20),
                "search_depth": "advanced" if depth == "advanced" else "basic",
                "topic": "news" if kind == "news" else "general"}
        if recency:
            body["time_range"] = recency
        data = self._call("POST", self.base + "/search", json_body=body, headers=self._auth())
        items = data.get("results", []) if isinstance(data, dict) else []
        return [Result(rank=n, title=i.get("title", ""), url=i.get("url", ""),
                       snippet=i.get("content", ""), date=i.get("published_date"))
                for n, i in enumerate(items, start=1) if i.get("url")]

    def remaining(self):
        data = self._call("GET", self.base + "/usage", headers=self._auth())
        if not isinstance(data, dict):
            return None
        key, acct = data.get("key") or {}, data.get("account") or {}
        if key.get("limit") is not None and key.get("usage") is not None:
            return key["limit"] - key["usage"]
        if acct.get("plan_limit") is not None and acct.get("plan_usage") is not None:
            return acct["plan_limit"] - acct["plan_usage"]
        return None


class SerpApi(Provider):
    name = "serpapi"
    label = "SerpApi"
    env_var = "SERPAPI_API_KEY"
    base = "https://serpapi.com"

    def _interpret(self, data):
        """SerpApi reports problems inside a 200 response."""
        if isinstance(data, dict) and data.get("error"):
            message = str(data["error"])
            low = message.lower()
            if "run out of searches" in low or "out of searches" in low:
                raise QuotaError(f"{self.label} is out of searches")
            if "invalid api key" in low:
                raise AuthError(f"{self.label} rejected the API key")
            if "hasn't returned any results" in low or "no results" in low:
                self.last_note = "SerpApi said: " + self._clean(message)[:90]
                return {}
            raise ProviderError(f"{self.label}: {self._clean(message)[:200]}")
        return data if isinstance(data, dict) else {}

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        params = {"engine": "google", "q": query, "num": min(num, 10), "api_key": self.api_key}
        if kind == "news":
            params["tbm"] = "nws"
        if search_country(country):
            params["gl"] = search_country(country)
        if recency:
            params["tbs"] = _TBS[recency]
        data = self._interpret(self._call("GET", self.base + "/search.json", params=params))
        items = data.get("news_results" if kind == "news" else "organic_results", [])
        return [Result(rank=int(i.get("position") or n), title=i.get("title", ""),
                       url=i.get("link", ""), snippet=i.get("snippet", ""), date=i.get("date"))
                for n, i in enumerate(items, start=1) if i.get("link")]

    def remaining(self):
        data = self._interpret(self._call("GET", self.base + "/account",
                                          params={"api_key": self.api_key}))
        for key in ("total_searches_left", "plan_searches_left"):
            if data.get(key) is not None:
                return data[key]
        return None


_EXA_LIMIT = re.compile(r"\b429\b|rate[ _-]?limit|too many requests|\bquota\b|(?:usage|request|daily|monthly|free)[ _-]+limit|(?:out of|insufficient|exhausted).{0,20}credits?", re.I)


def parse_exa_blocks(text):
    """Exa's hosted search answers in blocks: Title:, URL:, Published:, Author:, then the text. Returns [Result]."""
    results = []
    for block in re.split(r"\n(?=Title: )", "\n" + str(text or "").strip()):
        block = block.strip()
        if not block.startswith("Title:"):
            continue
        fields, body = {}, []
        for line in block.splitlines():
            m = re.match(r"^(Title|URL|Published|Author):\s*(.*)$", line)
            if m and not body:
                fields[m.group(1).lower()] = m.group(2).strip()
            elif line.strip() not in ("Highlights:", "Text:"):
                body.append(line)
        if fields.get("url"):
            published = fields.get("published")
            results.append(Result(rank=len(results) + 1, title=fields.get("title", ""), url=fields["url"], snippet=" ".join(" ".join(body).split())[:300],
                                  date=None if published in (None, "", "N/A") else published))
    return results


class Exa(Provider):
    """Exa's search API (an API key; new accounts get free credits and the free tier renews monthly; when credits end it answers 402 and stops)."""
    name = "exa"
    label = "Exa"
    env_var = "EXA_API_KEY"
    base = "https://api.exa.ai"

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        body = {"query": query, "numResults": min(num, 10), "type": "auto"}          # no page contents are requested: they are billed separately
        if kind == "news":
            body["category"] = "news"
        if recency:
            from datetime import datetime, timedelta, timezone
            days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
            body["startPublishedDate"] = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        data = self._call("POST", self.base + "/search", json_body=body, headers={"x-api-key": self.api_key, "Content-Type": "application/json"})
        items = data.get("results", []) if isinstance(data, dict) else []
        return [Result(rank=n, title=i.get("title") or "", url=i.get("url", ""), snippet=" ".join(str(i.get("text") or i.get("summary") or "").split())[:300],
                       date=i.get("publishedDate")) for n, i in enumerate(items, start=1) if i.get("url")]


class ExaMcp(Provider):
    """Exa's free hosted search (no key; third-party service, public queries only). Ideas and the answer format follow Agent-Reach's web channel."""
    name = "exa_mcp"
    label = "Exa (free hosted search)"
    env_var = "EXA_MCP"                 # no key: set EXA_MCP=on (the workflows do) to use it, like any other provider's secret being present
    base = "https://mcp.exa.ai/mcp"

    def _clean(self, text):
        return str(text)                # there is no secret to hide, and "on" must not be replaced inside ordinary words

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": min(num, 10)}}}
        raw = self._call("POST", self.base, json_body=body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        msg = raw
        if isinstance(raw, str):
            payloads = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")] or [raw]
            try:
                msg = json.loads(payloads[-1])
            except ValueError:
                raise ProviderError(f"{self.label}: the answer could not be read") from None
        if not isinstance(msg, dict):
            raise ProviderError(f"{self.label}: unexpected answer")
        if msg.get("error"):
            text = str((msg["error"] or {}).get("message", msg["error"]))
            if _EXA_LIMIT.search(text):
                raise QuotaError(f"{self.label}: its free limit was reached ({self._clean(text)[:120]})")
            raise ProviderError(f"{self.label}: {self._clean(text)[:200]}")
        result = msg.get("result") or {}
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            if _EXA_LIMIT.search(text):
                raise QuotaError(f"{self.label}: its free limit was reached")
            raise ProviderError(f"{self.label}: {self._clean(text)[:200]}")
        found = parse_exa_blocks(text)
        if not found:
            raise ProviderError(f"{self.label}: returned nothing that could be read as search results")
        return found[:num]


class AgentReachSearch(Provider):
    """Search on one Agent-Reach channel offered to general research like any other search provider. Reddit goes through OpenCLI and a browser a person has logged in to, on a machine the owner controls, so it
    is built only when AGENT_REACH_SEARCH is on and the run is not on a hosted runner. YouTube goes through yt-dlp, which needs no login, so it is built wherever AGENT_REACH_SEARCH is on (YouTube may bot-check a
    data-centre address; that is reported, not worked around). Keyless: there is no secret to hide."""
    channel = ""
    needs_browser = True                 # Reddit goes through OpenCLI and a logged-in browser; YouTube (yt-dlp) does not
    env_var = "AGENT_REACH_SEARCH"
    kinds = ("web",)

    def __init__(self, api_key="on", http=None, sleep=None, cli=None):
        super().__init__(api_key, http=http, sleep=sleep)
        self.cli = cli

    @classmethod
    def unavailable_reason(cls, env, which=None):
        """Why this channel cannot run on this machine, in plain words ('' when it can). The browser-based channel needs OpenCLI installed (and, if the owner turned that guard on, not a hosted runner)."""
        if not cls.needs_browser:
            return ""
        import shutil

        from . import agent_reach_instagram as AR
        if AR.hosted_runner(env):
            return f"cannot run on a GitHub-hosted runner: it needs OpenCLI and a browser logged in to {AR.site_name(cls.channel)}, which must stay on a machine you control"
        if not (which or shutil.which)(env.get("OPENCLI_BIN", "opencli")):
            return f"cannot run: OpenCLI is not installed on this machine (it needs a browser logged in to {AR.site_name(cls.channel)}, on a machine you control)"
        return ""

    def _clean(self, text):
        return str(text)

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        import os

        from . import agent_reach_instagram as AR
        from . import contract as K
        if self.cli is not None:
            cli = self.cli
        elif self.channel == "youtube":                                  # Agent-Reach's YouTube channel is yt-dlp
            from .ytdlp_youtube import YtDlpYouTube
            cli = YtDlpYouTube()
        else:
            cli = AR.OpenCLI(os.environ.get("OPENCLI_BIN", "opencli"), site=self.channel, environ=os.environ)
        try:
            rows = AR.rows_from(cli.call(["search", query, "--limit", num]))
        except K.Failure as exc:
            cls = AuthError if exc.code == K.NEED_LOGIN else ProviderError
            raise cls(f"{self.label}: {exc.code}: {exc}") from None
        out = []
        for raw in rows[:num]:
            url = str(AR._pick(raw, "url", "link", "webpage_url") or "")
            title = str(AR._pick(raw, "title", "name", "text") or "")
            if url.startswith("http") and (title or url):
                out.append(Result(rank=len(out) + 1, title=title[:200], url=url, snippet=str(AR._pick(raw, "description", "content", "selftext", "body", "text") or "")[:400]))
        self.last_note = f"{len(out)} result(s) through {self.channel}"
        return out


class AgentReachReddit(AgentReachSearch):
    name, label, channel = "agent_reach_reddit", "Reddit search (Agent-Reach)", "reddit"


class AgentReachYouTube(AgentReachSearch):
    name, label, channel = "agent_reach_youtube", "YouTube search (Agent-Reach, yt-dlp)", "youtube"
    needs_browser = False


PROVIDER_CLASSES = (SerperDev, Tavily, SerpApi, Exa, ExaMcp, AgentReachReddit, AgentReachYouTube)
PROVIDER_NAMES = tuple(c.name for c in PROVIDER_CLASSES)


def agent_reach_search_missing(env, built, which=None):
    """{('web_search', name): the true reason} for an Agent-Reach search provider the owner opted in to that cannot run here, so an acceptance run skips it with that reason instead of failing it."""
    out = {}
    for cls in PROVIDER_CLASSES:
        if issubclass(cls, AgentReachSearch) and cls.name not in built and (env.get(cls.env_var) or "").strip().lower() in ("on", "1", "true", "yes"):
            reason = cls.unavailable_reason(env, which)
            if reason:
                out[("web_search", cls.name)] = reason
    return out


def build_providers(env, http=None, sleep=None, which=None):
    """Providers whose API key is present in `env` (a mapping such as os.environ). An Agent-Reach channel is built only when the owner opted in AND it can run here (the browser-based one needs OpenCLI)."""
    found = {}
    for cls in PROVIDER_CLASSES:
        key = (env.get(cls.env_var) or "").strip()
        if issubclass(cls, AgentReachSearch):
            if key.lower() in ("on", "1", "true", "yes") and not cls.unavailable_reason(env, which):
                found[cls.name] = cls(key, http=http, sleep=sleep)
        elif key:
            found[cls.name] = cls(key, http=http, sleep=sleep)
    return found
