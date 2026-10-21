"""A scripted stand-in for the internet. Nothing in the tests reaches a real search provider."""


class FakeHttp:
    """Routes are matched by (method, url prefix). A route with several responses plays them
    in order and then repeats the last one."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, url_prefix, *responses):
        self.routes.append([method, url_prefix, list(responses)])
        return self

    def __call__(self, method, url, *, params=None, json_body=None, headers=None, timeout=30):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json_body, "headers": headers, "timeout": timeout})
        for route_method, prefix, responses in self.routes:
            if route_method == method and url.startswith(prefix):
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if callable(response):               # an answer that depends on the request
                    response = response(params=params, json_body=json_body)
                return response[0], response[1], (response[2] if len(response) > 2 else None)
        raise AssertionError(f"unexpected request: {method} {url}")


def serper_web(n=3, tag="a"):
    return {"organic": [{"title": f"{tag} result {i}", "link": f"https://{tag}{i}.example.com/page",
                         "snippet": f"{tag} snippet {i}", "position": i} for i in range(1, n + 1)]}


def serper_news(n=3, tag="n"):
    return {"news": [{"title": f"{tag} story {i}", "link": f"https://{tag}{i}.example.com/story",
                      "snippet": f"{tag} summary {i}", "date": "2 days ago", "source": "Example"}
                     for i in range(1, n + 1)]}


def tavily_resp(n=3, tag="t"):
    return {"query": "q", "response_time": 0.4,
            "results": [{"title": f"{tag} page {i}", "url": f"https://{tag}{i}.example.com/page",
                         "content": f"{tag} content {i}", "score": 0.9,
                         "published_date": "2026-10-18"} for i in range(1, n + 1)]}


def serpapi_web(n=3, tag="s"):
    return {"organic_results": [{"position": i, "title": f"{tag} hit {i}",
                                 "link": f"https://{tag}{i}.example.com/hit",
                                 "snippet": f"{tag} text {i}"} for i in range(1, n + 1)]}


def serpapi_news(n=3, tag="sn"):
    return {"news_results": [{"position": i, "title": f"{tag} news {i}",
                              "link": f"https://{tag}{i}.example.com/news",
                              "snippet": f"{tag} line {i}", "date": "1 day ago"}
                             for i in range(1, n + 1)]}


USAGE_URLS = ("https://api.tavily.com/usage", "https://serpapi.com/account")


def standard_routes(http, *, tavily_used=0, tavily_limit=1000, serpapi_left=250):
    http.on("POST", "https://google.serper.dev/search", (200, serper_web(3)))
    http.on("POST", "https://google.serper.dev/news", (200, serper_news(3)))
    http.on("POST", "https://api.tavily.com/search", (200, tavily_resp(3)))
    http.on("GET", "https://api.tavily.com/usage",
            (200, {"key": {"usage": tavily_used, "limit": tavily_limit}}))
    http.on("GET", "https://serpapi.com/search.json",
            lambda params, json_body: (200, serpapi_news(3) if params.get("tbm") == "nws" else serpapi_web(3)))
    http.on("GET", "https://serpapi.com/account", (200, {"total_searches_left": serpapi_left}))
    return http


def searches_only(http):
    """The calls that cost credits (not the free balance checks)."""
    return [c for c in http.calls if c["url"] not in USAGE_URLS]
