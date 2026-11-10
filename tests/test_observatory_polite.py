"""The polite fetcher, tested offline with scripted responses."""
import pytest

from contentintel import contract as K
from contentintel.observatory import polite as P
from contentintel.observatory.pauses import PauseLedger
from observatory_helpers import CONTACT, NOW, PRIVATE, Web, client, gz, reply, settings

SHOP = "https://shop.example"
OPEN = "User-agent: *\nDisallow: /cart\nAllow: /\n"


def fails(web_or_client, url, code, **kw):
    c = web_or_client if isinstance(web_or_client, P.PoliteClient) else client(web_or_client, **kw)
    with pytest.raises(K.Failure) as seen:
        c.get(url)
    assert seen.value.code == code, (seen.value.code, str(seen.value))
    return seen.value, c


# ---- identity ---------------------------------------------------------------------------------------------------------------------------------

def test_a_contact_address_is_required_and_goes_into_an_honest_user_agent():
    with pytest.raises(K.Failure) as seen:
        P.PoliteClient(settings(), contact="", http=Web())
    assert seen.value.code == K.NEED_SETUP and "OBSERVATORY_CONTACT" in str(seen.value)
    c = client(Web())
    assert c.user_agent == f"KelsonResearchBot/1.0 (research; contact: {CONTACT})"
    assert "Mozilla" not in c.user_agent and "Chrome" not in c.user_agent


@pytest.mark.parametrize("bad", ["a@b.example\r\nX-Evil: 1", "a (b)", "<x>"])
def test_a_contact_that_could_break_the_header_is_refused(bad):
    with pytest.raises(K.Failure) as seen:
        P.PoliteClient(settings(), contact=bad, http=Web())
    assert seen.value.code == K.BAD_INPUT


def test_the_request_carries_the_identity_and_no_disguise():
    seen = []

    def spy(method, url, headers, timeout, max_bytes):
        seen.append(headers)
        return reply(200, "x") if url.endswith("/a") else reply(404, "")
    client(spy).get(SHOP + "/a")
    assert all(h["User-Agent"].startswith("KelsonResearchBot/1.0") for h in seen)
    assert not any(k.lower() in ("referer", "sec-ch-ua", "sec-fetch-site", "dnt") for h in seen for k in h)


# ---- robots.txt -------------------------------------------------------------------------------------------------------------------------------

def test_robots_is_read_before_any_page_and_a_disallowed_page_is_never_requested():
    web = Web({SHOP + "/cart": reply(200, "cart"), SHOP + "/a": reply(200, "ok")}, robots=OPEN)
    failure, c = fails(web, SHOP + "/cart", K.PLATFORM_BLOCKED)
    assert "robots.txt does not allow" in str(failure) and web.log == [SHOP + "/robots.txt"]
    assert c.get(SHOP + "/a").status == 200 and web.log[0] == SHOP + "/robots.txt"


def test_robots_wildcards_allow_overrides_and_the_most_specific_group_are_understood():
    text = "User-agent: *\nDisallow: /*?sort=\nDisallow: /private/\nAllow: /private/public/\n\nUser-agent: KelsonResearchBot\nDisallow: /only-us\n"
    web = Web({SHOP + "/*": reply(200, "ok")}, robots=text)
    c = client(web)
    assert c.robots_allows(SHOP + "/products") and not c.robots_allows(SHOP + "/only-us")
    assert c.robots_allows(SHOP + "/private/x") is True           # our own group has no rule for it, so the * group no longer applies
    web2 = Web(robots="User-agent: *\nDisallow: /*?sort=\nDisallow: /private/\nAllow: /private/public/\n")
    c2 = client(web2)
    assert not c2.robots_allows(SHOP + "/c?sort=price") and not c2.robots_allows(SHOP + "/private/x") and c2.robots_allows(SHOP + "/private/public/y")


def test_a_missing_robots_file_means_open_but_a_broken_one_means_closed():
    open_web = Web({SHOP + "/a": reply(200, "ok")})            # robots.txt not scripted: 404
    assert client(open_web).get(SHOP + "/a").status == 200
    for broken in (reply(500, "oops"), reply(503, ""), TimeoutError("slow"), OSError("down")):
        web = Web({SHOP + "/a": reply(200, "ok")})
        web.on(SHOP + "/robots.txt", broken)
        failure, c = fails(web, SHOP + "/a", K.PLATFORM_BLOCKED)
        assert "closed" in str(failure) and SHOP + "/a" not in web.log


def test_robots_is_fetched_once_per_site_and_refetched_after_it_expires():
    web = Web({SHOP + "/a": reply(200, "1"), SHOP + "/b": reply(200, "2")}, robots=OPEN)
    c = client(web)
    c.get(SHOP + "/a")
    c.get(SHOP + "/b")
    assert web.count(SHOP + "/robots.txt") == 1
    c.test_clock.t += 25 * 3600
    c.get(SHOP + "/a", use_cache=False)
    assert web.count(SHOP + "/robots.txt") == 2


def test_crawl_delay_and_request_rate_set_the_pause_up_to_the_cap():
    web = Web({SHOP + "/*": reply(200, "x")}, robots="User-agent: *\nCrawl-delay: 20\n")
    c = client(web)
    c.get(SHOP + "/a")
    c.get(SHOP + "/b")
    assert c.test_clock.sleeps and max(c.test_clock.sleeps) == pytest.approx(20)
    web = Web({SHOP + "/*": reply(200, "x")}, robots="User-agent: *\nCrawl-delay: 9999\n")
    c = client(web)
    c.get(SHOP + "/a")
    c.get(SHOP + "/b")
    assert max(c.test_clock.sleeps) == pytest.approx(60)


def test_sitemaps_a_site_lists_in_robots_are_found_and_unsafe_ones_dropped():
    web = Web(robots="User-agent: *\nSitemap: https://shop.example/sitemap.xml\nSitemap: http://127.0.0.1/evil.xml\n")
    assert client(web).sitemaps_from_robots(SHOP + "/") == ["https://shop.example/sitemap.xml"]


# ---- the throttle -----------------------------------------------------------------------------------------------------------------------------

def test_the_pause_between_requests_is_the_minimum_and_nothing_waits_before_the_first():
    web = Web({SHOP + "/a": reply(200, "1"), SHOP + "/b": reply(200, "2")}, robots=OPEN)
    c = client(web)
    c.get(SHOP + "/a")
    assert c.test_clock.sleeps == [5.0]                                     # robots.txt, then a 5 second gap before the page
    c.get(SHOP + "/b")
    assert c.test_clock.sleeps == [5.0, 5.0]


def test_a_429_doubles_the_pause_honours_retry_after_stops_the_site_and_rests_it(tmp_path):
    web = Web({SHOP + "/a": reply(429, "slow down", {"Retry-After": "45"}), SHOP + "/b": reply(200, "ok")}, robots=OPEN)
    ledger = PauseLedger(tmp_path)
    failure, c = fails(web, SHOP + "/a", K.RATE_LIMITED, ledger=ledger)
    assert failure.retry_after == 45 and c.delay_for("shop.example") == 45          # max(2 x 5, 45)
    assert ledger.paused("shop.example", NOW)[2] == K.RATE_LIMITED
    before = len(web.log)
    fails(c, SHOP + "/b", K.RATE_LIMITED)                                           # resting: nothing is sent
    assert len(web.log) == before and any("resting" in e for e in c.events)


def test_the_pause_never_exceeds_the_cap_and_doubles_on_each_block():
    web = Web({SHOP + "/*": reply(503, "busy")}, robots=OPEN)
    c = client(web, settings(politeness__retries=0, politeness__max_delay_seconds=30))
    delays = []
    for n in range(5):
        with pytest.raises(K.Failure):
            c.get(SHOP + f"/{n}")
        delays.append(c.delay_for("shop.example"))
    assert delays == [10, 20, 30, 30, 30]


def test_the_pause_halves_again_after_five_good_answers():
    web = Web({SHOP + "/bad": reply(429, "x"), SHOP + "/*": reply(200, "ok")}, robots=OPEN)
    c = client(web, settings(politeness__max_delay_seconds=40))
    with pytest.raises(K.Failure):
        c.get(SHOP + "/bad")
    assert c.delay_for("shop.example") == 10
    for n in range(5):
        c.get(SHOP + f"/ok{n}", use_cache=False)
    assert c.delay_for("shop.example") == 5


def test_a_learned_delay_is_remembered_and_the_next_run_starts_at_half_of_it(tmp_path):
    ledger = PauseLedger(tmp_path)
    web = Web({SHOP + "/a": reply(429, "x")}, robots=OPEN)
    fails(web, SHOP + "/a", K.RATE_LIMITED, ledger=ledger)
    ledger.save(NOW)
    again = client(Web(robots=OPEN), ledger=PauseLedger(tmp_path))
    assert again.delay_for("shop.example") == 5                                    # half of the remembered 10, but never below the minimum
    ledger.data["shop.example"]["delay"] = 40
    assert client(Web(robots=OPEN), ledger=ledger).delay_for("shop.example") == 20


def test_a_rest_survives_to_a_new_run_and_ends_on_time(tmp_path):
    ledger = PauseLedger(tmp_path)
    ledger.pause("shop.example", 3600, "asked too often", NOW)
    ledger.save(NOW)
    reloaded = PauseLedger(tmp_path)
    web = Web({SHOP + "/a": reply(200, "ok")}, robots=OPEN)
    fails(client(web, ledger=reloaded), SHOP + "/a", K.RATE_LIMITED)
    assert web.log == []
    later = lambda: "2026-11-03T13:00:01Z"
    c = client(web, ledger=reloaded)
    c.now_fn = later
    assert c.get(SHOP + "/a").status == 200


# ---- blocks are recognised, never worked around -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_is_final_and_is_not_retried(status):
    web = Web({SHOP + "/a": reply(status, "no")}, robots=OPEN)
    failure, c = fails(web, SHOP + "/a", K.PLATFORM_BLOCKED)
    assert web.count(SHOP + "/a") == 1 and failure.code in K.FINAL_CODES


@pytest.mark.parametrize("status, body, headers", [
    (503, "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script></html>", {}),
    (403, "<html><title>Attention Required! | Cloudflare</title></html>", {}),
    (200, "<html><title>Verify you are human</title></html>", {}),
    (200, "ok", {"cf-mitigated": "challenge"}),
    (403, "<html>px-captcha</html>", {}),
])
def test_a_challenge_page_is_recognised_reported_and_the_site_rests(tmp_path, status, body, headers):
    ledger = PauseLedger(tmp_path)
    web = Web({SHOP + "/a": reply(status, body, headers)}, robots=OPEN)
    failure, c = fails(web, SHOP + "/a", K.PLATFORM_BLOCKED, ledger=ledger)
    assert "challenge" in str(failure) and web.count(SHOP + "/a") == 1 and ledger.paused("shop.example", NOW)[2] == K.PLATFORM_BLOCKED


def test_an_ordinary_page_that_mentions_a_captcha_or_a_check_is_not_taken_for_a_challenge():
    page = "<html><title>Contact us</title><p>Our form uses a captcha to stop spam. Security check passed.</p>" + "<p>text</p>" * 200 + "</html>"
    web = Web({SHOP + "/a": reply(200, page)}, robots=OPEN)
    assert client(web).get(SHOP + "/a").status == 200


def test_login_wall_detection_needs_a_login_title_and_almost_no_content():
    assert P.looks_like_login_wall(b"<html><title>Log in</title><form>user pass</form></html>", "Log in")
    assert not P.looks_like_login_wall(b"<html><title>Log in</title>" + b"<p>word </p>" * 300 + b"</html>", "Log in")
    assert not P.looks_like_login_wall(b"<html>tiny</html>", "Our shop")


def test_not_found_is_not_found_and_does_not_slow_the_site_down():
    web = Web({SHOP + "/gone": reply(404, "x")}, robots=OPEN)
    failure, c = fails(web, SHOP + "/gone", K.NOT_FOUND)
    assert c.delay_for("shop.example") == 5


# ---- server errors, silence, retries ---------------------------------------------------------------------------------------------------------

def test_a_server_error_is_retried_then_the_site_rests_briefly(tmp_path):
    ledger = PauseLedger(tmp_path)
    web = Web({SHOP + "/a": reply(502, "bad")}, robots=OPEN)
    failure, c = fails(web, SHOP + "/a", K.UPSTREAM_BROKEN, ledger=ledger)
    assert web.count(SHOP + "/a") == 3                                            # the first try and two retries (settings: retries)
    assert ledger.paused("shop.example", NOW)[2] == K.UPSTREAM_BROKEN
    fails(c, SHOP + "/b", K.UPSTREAM_BROKEN)
    assert SHOP + "/b" not in web.log


def test_a_server_error_that_clears_up_succeeds_on_the_retry():
    web = Web({SHOP + "/a": [reply(500, "x"), reply(200, "fine")]}, robots=OPEN)
    c = client(web)
    assert c.get(SHOP + "/a").body == b"fine" and web.count(SHOP + "/a") == 2


def test_a_silent_server_is_a_timeout_and_a_dead_one_is_upstream_broken():
    web = Web({SHOP + "/a": TimeoutError("t")}, robots=OPEN)
    assert fails(web, SHOP + "/a", K.TIMEOUT)[0].code == K.TIMEOUT
    web = Web({SHOP + "/a": OSError("ConnectionError")}, robots=OPEN)
    failure, _ = fails(web, SHOP + "/a", K.UPSTREAM_BROKEN)
    assert "could not be reached" in str(failure)


# ---- addresses --------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "https://localhost/a", "http://169.254.169.254/latest", "ftp://shop.example/a",
                                 "https://user:pw@shop.example/a", "https://shop.example\\evil", "file:///etc/passwd", "http://[::1]/"])
def test_only_public_web_addresses_are_ever_fetched(url):
    web = Web()
    fails(web, url, K.BAD_INPUT)
    assert web.log == []


def test_a_public_looking_name_that_points_inside_the_network_is_refused():
    web = Web({SHOP + "/a": reply(200, "x")}, robots=OPEN)
    failure, _ = fails(web, SHOP + "/a", K.BAD_INPUT, resolver=PRIVATE)
    assert "public address" in str(failure) and web.log == []


def test_a_redirect_is_followed_but_every_hop_is_checked_and_robots_of_the_new_site_applies():
    web = Web({SHOP + "/old": reply(301, "", {"Location": "/new"}), SHOP + "/new": reply(200, "arrived")}, robots=OPEN)
    c = client(web)
    got = c.get(SHOP + "/old")
    assert got.body == b"arrived" and got.final_url == SHOP + "/new"
    web = Web({SHOP + "/go": reply(302, "", {"Location": "http://169.254.169.254/latest/meta-data"})}, robots=OPEN)
    fails(web, SHOP + "/go", K.BAD_INPUT)
    assert not any("169.254" in u for u in web.log)
    web = Web({SHOP + "/go": reply(302, "", {"Location": "https://other.example/x"}), "https://other.example/x": reply(200, "x")}, robots=OPEN)
    web.on_robots("https://other.example", "User-agent: *\nDisallow: /\n")
    failure, _ = fails(web, SHOP + "/go", K.PLATFORM_BLOCKED)
    assert "https://other.example/x" not in web.log


def test_redirect_loops_and_missing_destinations_stop():
    web = Web({SHOP + "/a": reply(302, "", {"Location": "/a"})}, robots=OPEN)
    fails(web, SHOP + "/a", K.UPSTREAM_BROKEN)
    assert web.count(SHOP + "/a") == 4                                             # the first request and three hops (settings: max_redirects)
    web = Web({SHOP + "/b": reply(302, "")}, robots=OPEN)
    fails(web, SHOP + "/b", K.UPSTREAM_BROKEN)


# ---- size, cache, budget ----------------------------------------------------------------------------------------------------------------------

def test_an_answer_larger_than_the_limit_is_refused_unread():
    web = Web({SHOP + "/big": reply(200, "x" * 5000)}, robots=OPEN)
    c = client(web)
    with pytest.raises(K.Failure) as seen:
        c.get(SHOP + "/big", max_bytes=1000)
    assert seen.value.code == K.UPSTREAM_BROKEN and "larger" in str(seen.value)


def test_the_same_page_within_the_cache_time_is_answered_from_memory_and_expires():
    web = Web({SHOP + "/a": reply(200, "v1")}, robots=OPEN)
    c = client(web)
    first = c.get(SHOP + "/a")
    second = c.get(SHOP + "/a")
    assert not first.from_cache and second.from_cache and web.count(SHOP + "/a") == 1
    assert not c.get(SHOP + "/a", use_cache=False).from_cache
    c.test_clock.t += 361 * 60
    assert not c.get(SHOP + "/a").from_cache
    assert web.count(SHOP + "/a") == 3


def test_the_per_site_request_ceiling_stops_a_run_without_blaming_the_site():
    web = Web({SHOP + "/*": reply(200, "x")}, robots=OPEN)
    c = client(web, settings(politeness__max_requests_per_domain=3))
    c.get(SHOP + "/1")
    c.get(SHOP + "/2")
    failure, _ = fails(c, SHOP + "/3", K.BAD_INPUT)
    assert "request limit" in str(failure)


# ---- helpers ----------------------------------------------------------------------------------------------------------------------------------

def test_retry_after_numbers_and_dates_are_read_and_junk_is_ignored():
    assert P.parse_retry_after("30") == 30 and P.parse_retry_after("-5") == 0 and P.parse_retry_after("") is None and P.parse_retry_after("soon") is None
    assert P.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now_ts=1445412480 - 80) == pytest.approx(80, abs=1)


def test_compressed_bodies_are_unpacked_within_a_limit():
    assert P.decompress(gz("hello")) == b"hello" and P.decompress(b"plain") == b"plain"
    with pytest.raises(K.Failure):
        P.decompress(gz("x" * 100_000), limit=1000)
    with pytest.raises(K.Failure):
        P.decompress(b"\x1f\x8bnot really gzip")
    with pytest.raises(K.Failure):
        P.decompress(b"y" * 2000, limit=1000)


def test_text_and_json_decode_by_the_declared_charset_with_a_bom_tolerated():
    f = P.Fetched("u", "u", 200, {"content-type": "text/html; charset=iso-8859-1"}, "café".encode("latin-1"))
    assert f.text() == "café"
    j = P.Fetched("u", "u", 200, {}, b"\xef\xbb\xbf" + b'{"a": 1}')
    assert j.json() == {"a": 1}


# ---- guards: nothing blocks unless the owner turned the guard ON ---------------------------------------------------------------------------------

def _guards(**on):
    from contentintel import guards as G
    G.configure({g: on.get(g, False) for g in G.GUARDS})


def test_with_the_robots_guard_off_a_disallowed_page_is_fetched_and_robots_txt_is_never_asked_for():
    web = Web({SHOP + "/cart": reply(200, "cart")}, robots=OPEN)
    _guards()
    assert client(web).get(SHOP + "/cart").status == 200 and web.log == [SHOP + "/cart"]
    web = Web({SHOP + "/cart": reply(200, "cart")}, robots=OPEN)
    _guards(robots_txt=True)
    fails(web, SHOP + "/cart", K.PLATFORM_BLOCKED)


def test_with_the_private_address_guard_off_any_address_is_fetched_and_with_it_on_it_is_refused():
    web = Web({SHOP + "/a": reply(200, "ok")}, robots=OPEN)
    _guards()
    assert client(web, resolver=PRIVATE).get(SHOP + "/a").status == 200
    _guards(private_address_block=True)
    fails(Web({SHOP + "/a": reply(200, "ok")}, robots=OPEN), SHOP + "/a", K.BAD_INPUT, resolver=PRIVATE)


def test_with_the_rest_guard_off_a_refusing_site_is_tried_a_few_times_then_marked_failed_with_no_rest_and_the_run_moves_on(tmp_path):
    web = Web({SHOP + "/a": reply(429, "slow down"), SHOP + "/b": reply(200, "ok")}, robots=OPEN)
    _guards()
    led = PauseLedger(tmp_path)
    failure, c = fails(web, SHOP + "/a", K.RATE_LIMITED, ledger=led)
    assert web.log.count(SHOP + "/a") == 3 and "still refused after 3 attempts" in str(failure) and "moved on" in str(failure)
    assert led.paused("shop.example", NOW) is None and c.get(SHOP + "/b").status == 200                 # nothing rests, and the next page is read
    cfg = settings(retry__attempts=5)
    web = Web({SHOP + "/a": reply(403, "no")}, robots=OPEN)
    fails(web, SHOP + "/a", K.PLATFORM_BLOCKED, cfg=cfg)
    assert web.log.count(SHOP + "/a") == 5                                                                 # the owner's own number of tries


def test_with_the_rest_guard_on_a_refusal_is_not_retried_and_the_site_rests(tmp_path):
    web = Web({SHOP + "/a": reply(429, "slow down")}, robots=OPEN)
    _guards(rest_after_refusal=True)
    led = PauseLedger(tmp_path)
    fails(web, SHOP + "/a", K.RATE_LIMITED, ledger=led)
    assert web.log.count(SHOP + "/a") == 1 and led.paused("shop.example", NOW)
