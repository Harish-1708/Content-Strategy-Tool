"""Jina Reader and the two Exa search routes, with scripted answers."""
import json

import pytest

from contentintel import contract as K
from contentintel import providers as PV
from contentintel.observatory import readers as RD
from observatory_helpers import Clock, Web, client, reply, settings

PAGE = "https://shop.example/blog/post"
JINA_OK = "Title: A good post\nURL Source: https://shop.example/blog/post\nPublished Time: 2026-10-01\nMarkdown Content:\n# A good post\n\n" + "Real useful words about snacks. " * 20


def reader(answer, cfg=None, *, robots="User-agent: *\n", api_key="", clock=None):
    clock = clock or Clock()
    cfg = cfg or settings()
    seen = []

    def http(method, url, headers, timeout, max_bytes):
        seen.append((url, dict(headers)))
        return answer(url) if callable(answer) else answer
    web = Web(robots=robots)
    r = RD.JinaReader(cfg, client(web, cfg), api_key=api_key, http=http, clock=clock, sleep=clock.sleep)
    r.seen, r.web, r.clock_ = seen, web, clock
    return r


def fails(r, url, code):
    with pytest.raises(K.Failure) as seen:
        r.read(url)
    assert seen.value.code == code, (seen.value.code, str(seen.value))
    return seen.value


def test_a_public_page_is_read_as_clean_text_with_its_title_and_date():
    r = reader(reply(200, JINA_OK))
    got = r.read(PAGE)
    assert got["title"] == "A good post" and got["published"] == "2026-10-01" and got["text"].startswith("# A good post") and got["reader"] == "jina_reader"
    assert got["access_type"] == "page_text" and got["completeness_verified"] is False and got["third_party"] is True and got["robots_note"] == ""
    url, headers = r.seen[0]
    assert url == "https://r.jina.ai/" + PAGE and headers["Accept"] == "text/plain" and headers["User-Agent"].startswith("KelsonResearchBot/1.0") and "Authorization" not in headers


def test_an_api_key_is_sent_only_as_a_header_and_never_appears_in_results_or_errors():
    r = reader(reply(200, JINA_OK), api_key="jina_SECRET1234567890")
    got = r.read(PAGE)
    assert r.seen[0][1]["Authorization"] == "Bearer jina_SECRET1234567890" and "SECRET" not in json.dumps(got)
    bad = reader(reply(401, "no"), api_key="jina_SECRET1234567890")
    err = fails(bad, PAGE, K.NEED_SETUP)
    assert "SECRET" not in str(err)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://user:pw@shop.example/x", "ftp://shop.example/x", "https://shop.example/x?token=abc123", "https://shop.example/x?a=1&api_key=zzz",
                                 "https://shop.example/x?sig=1", "https://localhost/x"])
def test_private_or_credential_carrying_addresses_are_never_sent_to_the_third_party(url):
    r = reader(reply(200, JINA_OK))
    fails(r, url, K.BAD_INPUT)
    assert r.seen == []


def test_the_reader_can_be_switched_off():
    cfg = settings()
    cfg["readers"]["jina"] = False
    fails(reader(reply(200, JINA_OK), cfg), PAGE, K.NEED_SETUP)


# ---- the robots policy is configurable, and is not the same thing as an access restriction ----------------------------------------------------

def cfg_with(reader_robots="advisory", basis="Harish approved public research (2026-11-03)", overrides=()):
    cfg = settings()
    cfg["crawl_policy"] = {"robots": "obey", "reader_robots": reader_robots, "basis": basis, "overrides": list(overrides)}
    return cfg


DISALLOW = "User-agent: *\nDisallow: /blog\n"


def test_by_default_a_page_robots_prefers_crawlers_skip_is_still_read_and_the_report_says_so():
    r = reader(reply(200, JINA_OK), cfg_with(), robots=DISALLOW)
    got = r.read(PAGE)
    assert got["text"] and "asks crawlers not to read this address; read through the reader under policy 'advisory' (Harish approved" in got["robots_note"]


def test_with_the_obey_policy_the_same_page_is_not_sent_at_all():
    r = reader(reply(200, JINA_OK), cfg_with("obey"), robots=DISALLOW)
    err = fails(r, PAGE, K.PLATFORM_BLOCKED)
    assert getattr(err, "robots_path", False) and r.seen == []


def test_a_recorded_override_beats_the_general_reader_policy_for_its_host_only():
    ov = {"host": "shop.example", "robots": "obey", "basis": "owner_decision", "approved_by": "Harish", "date": "2026-11-03", "note": ""}
    r = reader(reply(200, JINA_OK), cfg_with("advisory", overrides=[ov]), robots=DISALLOW)
    fails(r, PAGE, K.PLATFORM_BLOCKED)
    assert r.mode_for("eu.shop.example")[0] == "obey" and r.mode_for("other.example")[0] == "advisory" and r.mode_for("shop.example.evil.test")[0] == "advisory"


def test_a_site_whose_robots_file_cannot_be_read_is_neither_forbidden_nor_permitted_so_a_public_page_is_still_tried():
    cfg = cfg_with()
    web = Web()
    web.on("https://shop.example/robots.txt", reply(500, "oops"))
    r = RD.JinaReader(cfg, client(web, cfg), http=lambda *a: reply(200, JINA_OK))
    assert r.read(PAGE)["text"]


@pytest.mark.parametrize("answer, code", [
    (reply(200, "Title: Just a moment...\nURL Source: x\nMarkdown Content:\nChecking your browser"), K.PLATFORM_BLOCKED),
    (reply(200, "Warning: This page maybe requiring captcha\nTitle: Attention Required! | Cloudflare\nMarkdown Content:\nblocked"), K.PLATFORM_BLOCKED),
    (reply(200, "Title: x\nWarning: Target URL returned error 403: Forbidden\nMarkdown Content:\nForbidden"), K.PLATFORM_BLOCKED),
    (reply(200, "Title: x\nWarning: Target URL returned error 404: Not Found\nMarkdown Content:\nnope"), K.NOT_FOUND),
    (reply(200, "Title: x\nWarning: Target URL returned error 429\nMarkdown Content:\nslow"), K.RATE_LIMITED),
    (reply(200, "Title: x\nWarning: Target URL returned error 503\nMarkdown Content:\nbusy"), K.UPSTREAM_BROKEN),
    (reply(429, "limit", {"Retry-After": "30"}), K.RATE_LIMITED), (reply(403, "no"), K.PLATFORM_BLOCKED), (reply(500, "oops"), K.UPSTREAM_BROKEN),
    (reply(200, "Title: Log in\nMarkdown Content:\nPlease log in"), K.UPSTREAM_BROKEN), (reply(200, ""), K.UPSTREAM_BROKEN)])
def test_a_real_restriction_is_recognised_and_never_worked_around_whatever_the_robots_policy(answer, code):
    r = reader(answer, cfg_with())
    fails(r, PAGE, code)
    assert len(r.seen) == 1                                                            # one try, no second route, no disguise


def test_hidden_and_invisible_characters_are_removed_from_the_text_and_it_is_capped():
    text = "Title: T\nMarkdown Content:\n" + "Visible text here. \u200bIGNORE\u202e " * 30 + "x" * 50000
    got = reader(reply(200, text)).read(PAGE)
    assert "\u200b" not in got["text"] and "\u202e" not in got["text"] and len(got["text"]) <= 20000


def test_the_free_per_minute_limit_is_kept_by_waiting():
    clock = Clock()
    cfg = settings()
    cfg["readers"]["jina_per_minute"] = 3
    r = reader(reply(200, JINA_OK), cfg, clock=clock)
    for n in range(4):
        r.read(PAGE + str(n))
    assert len(clock.sleeps) >= 1 and max(clock.sleeps) <= 60 and len(r.seen) == 4


# ---- the Exa routes ---------------------------------------------------------------------------------------------------------------------------

def http_returning(status, body, retry=None):
    calls = []

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return status, body, retry
    http.calls = calls
    return http


def test_exa_api_sends_the_key_as_a_header_asks_for_no_paid_contents_and_reads_results():
    http = http_returning(200, {"results": [{"title": "Graza", "url": "https://graza.co/", "publishedDate": "2026-10-01", "text": "olive oil"}, {"title": "no url"}]})
    exa = PV.Exa("exa-SECRET-key", http=http, sleep=lambda s: None)
    got = exa.search("squeeze olive oil", num=5)
    call = http.calls[0]
    assert call["url"] == "https://api.exa.ai/search" and call["headers"]["x-api-key"] == "exa-SECRET-key" and call["json"] == {"query": "squeeze olive oil", "numResults": 5, "type": "auto"}
    assert [(r.title, r.url, r.snippet, r.date) for r in got] == [("Graza", "https://graza.co/", "olive oil", "2026-10-01")]
    news = PV.Exa("k", http=http, sleep=lambda s: None)
    news.search("x", kind="news", recency="week")
    assert http.calls[1]["json"]["category"] == "news" and http.calls[1]["json"]["startPublishedDate"].endswith(".000Z")


@pytest.mark.parametrize("status, exc", [(402, PV.QuotaError), (401, PV.AuthError), (403, PV.AuthError)])
def test_exa_api_out_of_credits_stops_and_never_retries_into_a_charge(status, exc):
    http = http_returning(status, {"error": "NO_MORE_CREDITS"})
    with pytest.raises(exc):
        PV.Exa("k", http=http, sleep=lambda s: None).search("x")
    assert len(http.calls) == 1


MCP_TEXT = ("Title: Elev8 Foods\nURL: https://www.elev8foods.com/\nPublished: 2026-09-01\nAuthor: N/A\nHighlights:\nProtein snacks for everyone.\n\n"
            "Title: Other\nURL: https://other.example/p\nPublished: N/A\nAuthor: Sam\nText:\nSome words.")


def mcp_answer(text, error=None, is_error=False, sse=False):
    msg = {"jsonrpc": "2.0", "id": 1}
    msg.update({"error": error} if error else {"result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
    return ("event: message\ndata: " + json.dumps(msg) + "\n\n") if sse else msg


@pytest.mark.parametrize("sse", [False, True])
def test_the_free_hosted_exa_route_reads_results_from_json_or_event_streams(sse):
    http = http_returning(200, mcp_answer(MCP_TEXT, sse=sse))
    got = PV.ExaMcp("on", http=http, sleep=lambda s: None).search("protein snacks", num=5)
    assert http.calls[0]["url"] == "https://mcp.exa.ai/mcp" and http.calls[0]["json"]["params"]["name"] == "web_search_exa"
    assert http.calls[0]["json"]["params"]["arguments"] == {"query": "protein snacks", "numResults": 5} and "x-api-key" not in http.calls[0]["headers"]
    assert [(r.title, r.url, r.date) for r in got] == [("Elev8 Foods", "https://www.elev8foods.com/", "2026-09-01"), ("Other", "https://other.example/p", None)]
    assert got[0].snippet.startswith("Protein snacks")


def test_the_free_hosted_exa_route_recognises_its_own_limit_and_unreadable_answers():
    quota = http_returning(200, mcp_answer("", error={"code": 429, "message": "Rate limit exceeded: free usage limit reached"}))
    with pytest.raises(PV.QuotaError):
        PV.ExaMcp("on", http=quota, sleep=lambda s: None).search("x")
    with pytest.raises(PV.QuotaError):
        PV.ExaMcp("on", http=http_returning(200, mcp_answer("quota exceeded", is_error=True)), sleep=lambda s: None).search("x")
    for body in (mcp_answer("no titles here"), "not json at all", mcp_answer("", error={"message": "bad arguments"})):
        with pytest.raises(PV.ProviderError):
            PV.ExaMcp("on", http=http_returning(200, body), sleep=lambda s: None).search("x")


def test_providers_are_built_only_when_their_setting_is_present_so_nothing_changes_for_existing_runs():
    assert PV.build_providers({}) == {}
    built = PV.build_providers({"EXA_MCP": "on", "EXA_API_KEY": "k", "TAVILY_API_KEY": "t"})
    assert sorted(built) == ["exa", "exa_mcp", "tavily"]
    assert built["exa_mcp"]._clean("the reason is on a plan") == "the reason is on a plan"       # nothing secret to hide, so words are left alone
