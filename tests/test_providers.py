import pytest

from contentintel import providers as P

from fake_http import FakeHttp, serpapi_news, serpapi_web, serper_news, serper_web, tavily_resp


@pytest.fixture
def sleeps():
    return []


def make(cls, http, sleeps, key="KEY-123"):
    return cls(key, http=http, sleep=sleeps.append)


# --- Serper.dev -----------------------------------------------------------------------------

def test_serper_dev_web_request_and_results(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, serper_web(3)))
    results = make(P.SerperDev, http, sleeps).search("loungewear", country="US", recency="week", num=25)
    (call,) = http.calls
    assert call["url"] == "https://google.serper.dev/search" and call["method"] == "POST"
    assert call["headers"]["X-API-KEY"] == "KEY-123"
    assert call["json"] == {"q": "loungewear", "num": 10, "gl": "us", "tbs": "qdr:w"}
    assert [r.title for r in results] == ["a result 1", "a result 2", "a result 3"]
    assert results[0].url == "https://a1.example.com/page" and results[0].snippet == "a snippet 1"
    assert [r.rank for r in results] == [1, 2, 3]


def test_serper_dev_news_uses_the_news_endpoint(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/news", (200, serper_news(2)))
    results = make(P.SerperDev, http, sleeps).search("robes", kind="news")
    assert http.calls[0]["url"].endswith("/news") and "gl" not in http.calls[0]["json"]
    assert results[0].date == "2 days ago" and len(results) == 2


def test_serper_dev_skips_entries_without_a_link_and_survives_an_empty_answer(sleeps):
    body = {"organic": [{"title": "no link"}, {"title": "ok", "link": "https://ok.example.com"}]}
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, body))
    assert [r.title for r in make(P.SerperDev, http, sleeps).search("x")] == ["ok"]
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, {}))
    assert make(P.SerperDev, http, sleeps).search("x") == []


def test_serper_dev_costs_one_credit_and_reports_no_balance(sleeps):
    provider = make(P.SerperDev, FakeHttp(), sleeps)
    assert provider.cost("web") == 1 and provider.cost("news") == 1 and provider.remaining() is None


# --- Tavily -----------------------------------------------------------------------------------

def test_tavily_request_and_results(sleeps):
    http = FakeHttp().on("POST", "https://api.tavily.com/search", (200, tavily_resp(2)))
    results = make(P.Tavily, http, sleeps).search("robes", kind="news", recency="month", num=50,
                                                  depth="advanced")
    (call,) = http.calls
    assert call["headers"]["Authorization"] == "Bearer KEY-123"
    assert call["json"] == {"query": "robes", "max_results": 20, "search_depth": "advanced",
                            "topic": "news", "time_range": "month"}
    assert results[0].snippet == "t content 1" and results[0].date == "2026-10-18"


def test_tavily_web_search_defaults_to_basic_general(sleeps):
    http = FakeHttp().on("POST", "https://api.tavily.com/search", (200, tavily_resp(1)))
    make(P.Tavily, http, sleeps).search("robes")
    assert http.calls[0]["json"] == {"query": "robes", "max_results": 10, "search_depth": "basic",
                                     "topic": "general"}


def test_tavily_costs_two_credits_for_an_advanced_search(sleeps):
    provider = make(P.Tavily, FakeHttp(), sleeps)
    assert provider.cost("web", "basic") == 1 and provider.cost("web", "advanced") == 2


@pytest.mark.parametrize("body,expected", [
    ({"key": {"usage": 150, "limit": 1000}, "account": {}}, 850),
    ({"key": {"usage": 5, "limit": None}, "account": {"plan_usage": 100, "plan_limit": 1000}}, 900),
    ({"key": {}, "account": {}}, None),
    ({}, None),
])
def test_tavily_reports_the_credits_it_says_are_left(sleeps, body, expected):
    http = FakeHttp().on("GET", "https://api.tavily.com/usage", (200, body))
    assert make(P.Tavily, http, sleeps).remaining() == expected
    assert http.calls[0]["headers"]["Authorization"] == "Bearer KEY-123"


def test_tavily_plan_limit_status_is_an_out_of_credits_error(sleeps):
    http = FakeHttp().on("POST", "https://api.tavily.com/search", (432, {"detail": {"error": "x"}}))
    with pytest.raises(P.QuotaError):
        make(P.Tavily, http, sleeps).search("x")


# --- SerpApi ----------------------------------------------------------------------------------

def test_serpapi_request_and_results(sleeps):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, serpapi_web(2)))
    results = make(P.SerpApi, http, sleeps).search("robes", country="GB", recency="day", num=40)
    (call,) = http.calls
    assert call["params"] == {"engine": "google", "q": "robes", "num": 10, "api_key": "KEY-123",
                              "gl": "gb", "tbs": "qdr:d"}
    assert [r.title for r in results] == ["s hit 1", "s hit 2"]


def test_serpapi_news_uses_the_news_vertical(sleeps):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, serpapi_news(2)))
    results = make(P.SerpApi, http, sleeps).search("robes", kind="news")
    assert http.calls[0]["params"]["tbm"] == "nws"
    assert results[0].date == "1 day ago" and results[0].title == "sn news 1"


@pytest.mark.parametrize("message,error", [
    ("Your account has run out of searches.", P.QuotaError),
    ("Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key", P.AuthError),
    ("Something odd happened", P.ProviderError),
])
def test_serpapi_problems_reported_inside_a_200_answer(sleeps, message, error):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, {"error": message}))
    with pytest.raises(error):
        make(P.SerpApi, http, sleeps).search("x")


def test_serpapi_no_results_is_an_empty_list_not_an_error(sleeps):
    body = {"error": "Google hasn't returned any results for this query."}
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, body))
    assert make(P.SerpApi, http, sleeps).search("zzzz") == []


@pytest.mark.parametrize("body,expected", [
    ({"total_searches_left": 240, "plan_searches_left": 200}, 240),
    ({"plan_searches_left": 200}, 200),
    ({}, None),
])
def test_serpapi_reports_searches_left(sleeps, body, expected):
    http = FakeHttp().on("GET", "https://serpapi.com/account", (200, body))
    assert make(P.SerpApi, http, sleeps).remaining() == expected
    assert http.calls[0]["params"] == {"api_key": "KEY-123"}


def test_serpapi_balance_check_with_a_bad_key_is_an_auth_error(sleeps):
    http = FakeHttp().on("GET", "https://serpapi.com/account", (200, {"error": "Invalid API key."}))
    with pytest.raises(P.AuthError):
        make(P.SerpApi, http, sleeps).remaining()


# --- shared behaviour -------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_an_auth_error(sleeps, status):
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (status, {"message": "Unauthorized."}))
    with pytest.raises(P.AuthError, match="rejected the API key"):
        make(P.SerperDev, http, sleeps).search("x")
    assert len(http.calls) == 1 and sleeps == []


def test_not_enough_credits_is_a_quota_error_and_is_not_retried(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search",
                         (400, {"message": "Not enough credits", "statusCode": 400}))
    with pytest.raises(P.QuotaError):
        make(P.SerperDev, http, sleeps).search("x")
    assert len(http.calls) == 1 and sleeps == []


def test_a_server_error_is_retried_then_succeeds(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (500, "oops"), (200, serper_web(1)))
    assert len(make(P.SerperDev, http, sleeps).search("x")) == 1
    assert len(http.calls) == 2 and sleeps == [1]


def test_repeated_server_errors_give_up_after_three_tries(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (503, "down"))
    with pytest.raises(P.ProviderError, match="gave up after retries"):
        make(P.SerperDev, http, sleeps).search("x")
    assert len(http.calls) == 3 and sleeps == [1, 2, 4]


def test_a_rate_limit_waits_as_long_as_the_provider_asks(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search",
                         (429, "slow down", "7"), (200, serper_web(1)))
    make(P.SerperDev, http, sleeps).search("x")
    assert sleeps == [7]


def test_a_long_rate_limit_wait_is_capped(sleeps):
    http = FakeHttp().on("POST", "https://google.serper.dev/search",
                         (429, "slow down", "900"), (200, serper_web(1)))
    make(P.SerperDev, http, sleeps).search("x")
    assert sleeps == [30]


def test_a_network_failure_is_retried_and_never_shows_the_key(sleeps):
    def broken(*args, **kwargs):
        raise ConnectionError("could not reach https://serpapi.com/search.json?api_key=KEY-123")
    with pytest.raises(P.ProviderError) as info:
        make(P.SerpApi, broken, sleeps).search("x")
    assert "KEY-123" not in str(info.value) and "network error" in str(info.value)


def test_an_unexpected_answer_never_shows_the_key(sleeps):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (400, "bad request for key KEY-123"))
    with pytest.raises(P.ProviderError) as info:
        make(P.SerpApi, http, sleeps).search("x")
    assert "KEY-123" not in str(info.value) and "***" in str(info.value)


def test_serpapi_error_text_never_shows_the_key(sleeps):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, {"error": "weird KEY-123 problem"}))
    with pytest.raises(P.ProviderError) as info:
        make(P.SerpApi, http, sleeps).search("x")
    assert "KEY-123" not in str(info.value)


def test_only_providers_with_a_key_are_built():
    env = {"TAVILY_API_KEY": " tvly-1 ", "SERPER_API_KEY": "", "SERPAPI_API_KEY": "   "}
    found = P.build_providers(env)
    assert list(found) == ["tavily"] and found["tavily"].api_key == "tvly-1"
    both = P.build_providers({"SERPER_API_KEY": "a", "SERPAPI_API_KEY": "b"})
    assert sorted(both) == ["serpapi", "serper_dev"]
    assert P.build_providers({}) == {}


def test_each_provider_reads_its_own_secret_name():
    assert {c.name: c.env_var for c in P.PROVIDER_CLASSES} == {
        "serper_dev": "SERPER_API_KEY", "tavily": "TAVILY_API_KEY", "serpapi": "SERPAPI_API_KEY", "exa": "EXA_API_KEY", "exa_mcp": "EXA_MCP"}


def test_results_become_plain_dicts_for_saving():
    r = P.Result(rank=2, title="T", url="https://x.example.com", snippet="S", date=None)
    assert r.to_dict() == {"rank": 2, "title": "T", "url": "https://x.example.com",
                           "snippet": "S", "date": None}
