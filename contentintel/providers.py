"""Search providers behind one small interface: Serper.dev, Tavily and SerpApi.

Each adapter turns a query into a list of Result objects and knows what a search costs in
credits. Network access goes through an injectable `http` function so tests never touch the
internet. API keys are never written into errors or logs.
"""
import json
import time
from dataclasses import dataclass

import requests

RECENCY = ("day", "week", "month", "year")
_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
_QUOTA_WORDS = ("not enough credits", "run out of searches", "out of searches", "usage limit",
                "quota", "exceeds your plan", "out of credits", "plan's set")


class ProviderError(Exception):
    """A search failed; another provider may still answer."""


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


class Provider:
    name = ""
    label = ""
    env_var = ""
    kinds = ("web", "news")

    def __init__(self, api_key, http=None, sleep=None):
        self.api_key = api_key
        self.http = http or default_http
        self.sleep = sleep or time.sleep

    # -- shared plumbing ---------------------------------------------------------------
    def _clean(self, text):
        text = str(text)
        return text.replace(self.api_key, "***") if self.api_key else text

    def _call(self, method, url, *, params=None, json_body=None, headers=None):
        last = "no response"
        for attempt in range(3):
            try:
                status, body, retry_after = self.http(
                    method, url, params=params, json_body=json_body, headers=headers)
            except Exception as exc:                      # network problem
                last = f"network error ({exc.__class__.__name__})"
                self.sleep(2 ** attempt)
                continue
            if status == 200:
                return body
            text = body if isinstance(body, str) else json.dumps(body)
            low = text.lower()
            if status in (401, 403):
                raise AuthError(f"{self.label} rejected the API key (HTTP {status})")
            if status in (402, 432, 433) or any(w in low for w in _QUOTA_WORDS):
                raise QuotaError(f"{self.label} is out of credits (HTTP {status})")
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
        if country:
            body["gl"] = country.lower()
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
                return {}
            raise ProviderError(f"{self.label}: {self._clean(message)[:200]}")
        return data if isinstance(data, dict) else {}

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        params = {"engine": "google", "q": query, "num": min(num, 10), "api_key": self.api_key}
        if kind == "news":
            params["tbm"] = "nws"
        if country:
            params["gl"] = country.lower()
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


PROVIDER_CLASSES = (SerperDev, Tavily, SerpApi)
PROVIDER_NAMES = tuple(c.name for c in PROVIDER_CLASSES)


def build_providers(env, http=None, sleep=None):
    """Providers whose API key is present in `env` (a mapping such as os.environ)."""
    found = {}
    for cls in PROVIDER_CLASSES:
        key = (env.get(cls.env_var) or "").strip()
        if key:
            found[cls.name] = cls(key, http=http, sleep=sleep)
    return found
