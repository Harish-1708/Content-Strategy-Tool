"""Reddit through the official API and Agent-Reach. MOCK: scripted HTTP and OpenCLI answers; nothing reaches Reddit."""
import json

import pytest

from contentintel import agent_reach_instagram as AR
from contentintel import contract as K
from contentintel import reddit_routes as RR

APPROVAL = {"basis": "non_commercial_approved", "approved_by": "Harish", "date": "2026-11-04", "reference": "Reddit email 2026-11-01"}
SECRET = "s3cr3t-" + "x" * 20


def post(pid="abc123", **kw):
    return {"id": pid, "author": "snacklover", "title": "Best protein bar?", "selftext": "Looking for chewy ones", "permalink": f"/r/snacks/comments/{pid}/best_protein_bar/", "created_utc": 1761814800,
            "score": 120, "num_comments": 34, "subreddit": "snacks", "thumbnail": "https://b.thumbs.redditmedia.com/x.jpg", "link_flair_text": "Question", **kw}


def listing(*posts):
    return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


class Http:
    """Answers the sign-in, then each listing request from a script; records every request."""

    def __init__(self, listings=None, token_status=200, token_body=None):
        self.calls, self.listings, self.token_status = [], list(listings or []), token_status
        self.token_body = token_body if token_body is not None else {"access_token": "tok-" + "z" * 20, "token_type": "bearer"}

    def __call__(self, method, url, *, headers=None, params=None, data=None, auth=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "data": data, "auth": auth})
        if url == RR.TOKEN_URL:
            return self.token_status, self.token_body, {}
        status, body, hdrs = self.listings.pop(0) if self.listings else (200, listing(), {})
        return status, body, hdrs


def api(http, approval=APPROVAL, sleep=lambda s: None, contact="research@kelson.example", cid="client-id-123", secret=SECRET):
    return RR.RedditAPI(cid, secret, contact, approval, http=http, sleep=sleep)


# ---- the approval gate: no request is made without Reddit's approval on record ---------------------------------------------------------------

@pytest.mark.parametrize("approval", [None, {}, {"basis": "non_commercial_approved"}, {"basis": "", "approved_by": "H", "date": "2026-11-04"}])
def test_without_a_recorded_approval_the_api_route_makes_no_request_at_all(approval):
    http = Http([(200, listing(post()), {})])
    with pytest.raises(K.Failure) as seen:
        api(http, approval).subreddit("snacks", 5)
    assert seen.value.code == K.NEED_SETUP and "needs a recorded approval from Reddit" in str(seen.value) and "commercial use needs a paid contract" in str(seen.value) and http.calls == []


def test_an_unknown_basis_is_refused_and_a_valid_one_passes():
    assert RR.approval_problem(APPROVAL) == "" and "must be one of" in RR.approval_problem({**APPROVAL, "basis": "because"})
    assert RR.approval_problem({"basis": "commercial_contract", "approved_by": "H", "date": "2026-11-04"}) == ""


@pytest.mark.parametrize("kw, fragment", [({"cid": ""}, "REDDIT_CLIENT_ID"), ({"secret": ""}, "REDDIT_CLIENT_SECRET"), ({"contact": ""}, "OBSERVATORY_CONTACT")])
def test_missing_credentials_or_contact_stop_the_route_before_any_request(kw, fragment):
    http = Http()
    with pytest.raises(K.Failure) as seen:
        api(http, **kw).subreddit("snacks", 5)
    assert seen.value.code == K.NEED_SETUP and fragment in str(seen.value) and http.calls == []


# ---- the official API route --------------------------------------------------------------------------------------------------------------------

def test_it_signs_in_once_with_the_app_credentials_and_reads_a_community_with_a_descriptive_agent():
    http = Http([(200, listing(post("a1"), post("a2")), {}), (200, listing(post("b1")), {})])
    client = api(http)
    first = client.subreddit("r/snacks", 5)
    second = client.search("protein bar", 3)
    assert [p["id"] for p in first] == ["a1", "a2"] and [p["id"] for p in second] == ["b1"]
    token_call, one, two = http.calls
    assert token_call["auth"] == ("client-id-123", SECRET) and token_call["data"] == {"grant_type": "client_credentials"} and "research@kelson.example" in token_call["headers"]["User-Agent"]
    assert one["url"] == "https://oauth.reddit.com/r/snacks/new" and one["params"] == {"limit": 5, "raw_json": 1} and one["headers"]["Authorization"].startswith("bearer tok-")
    assert two["url"] == "https://oauth.reddit.com/search" and two["params"]["q"] == "protein bar" and len([c for c in http.calls if c["url"] == RR.TOKEN_URL]) == 1       # one sign-in for the run


@pytest.mark.parametrize("status, code", [(403, K.PLATFORM_BLOCKED), (404, K.NOT_FOUND), (429, K.RATE_LIMITED), (500, K.UPSTREAM_BROKEN), (503, K.UPSTREAM_BROKEN), (418, K.UPSTREAM_BROKEN), (401, K.NEED_SETUP)])
def test_listing_errors_get_their_real_codes_and_a_rate_limit_names_how_long_to_wait(status, code):
    http = Http([(status, {}, {"x-ratelimit-reset": "42"})])
    with pytest.raises(K.Failure) as seen:
        api(http).subreddit("snacks", 5)
    assert seen.value.code == code
    if status == 429:
        assert seen.value.retry_after == 42
    assert SECRET not in str(seen.value) and "tok-" not in str(seen.value)


def test_sign_in_failures_are_a_setup_problem_and_never_print_the_secret():
    for status, body, code in ((401, {}, K.NEED_SETUP), (200, {"error": "invalid_grant"}, K.NEED_SETUP), (429, {}, K.RATE_LIMITED), (200, {}, K.UPSTREAM_BROKEN), (500, {}, K.UPSTREAM_BROKEN)):
        with pytest.raises(K.Failure) as seen:
            api(Http(token_status=status, token_body=body)).subreddit("snacks", 5)
        assert seen.value.code == code and SECRET not in str(seen.value) and "client-id-123" not in str(seen.value)


def test_a_rejected_token_is_forgotten_so_the_next_run_signs_in_again():
    client = api(Http([(401, {}, {})]))
    with pytest.raises(K.Failure):
        client.subreddit("snacks", 5)
    assert client.token == ""


def test_near_the_rate_limit_it_waits_for_the_reset_instead_of_crossing_it_and_never_waits_long():
    waits = []
    client = api(Http([(200, listing(post()), {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "30"})]), sleep=waits.append)
    client.subreddit("snacks", 5)
    assert waits == [30.0]
    waits.clear()
    api(Http([(200, listing(post()), {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "590"})]), sleep=waits.append).subreddit("snacks", 5)
    assert waits == []                                                                          # a long reset is not slept through; the next call will be refused by Reddit and rest the route


def test_network_trouble_is_classified_not_raised_raw():
    def silent(*a, **k):
        raise TimeoutError("t")

    def dead(*a, **k):
        raise OSError("ConnectionError")
    for fn, code in ((silent, K.TIMEOUT), (dead, K.UPSTREAM_BROKEN)):
        with pytest.raises(K.Failure) as seen:
            api(fn).subreddit("snacks", 5)
        assert seen.value.code == code


# ---- normalising both routes' records ----------------------------------------------------------------------------------------------------------

def test_an_api_post_becomes_the_shared_record_and_removed_posts_are_skipped():
    row = RR.normalize_api_post(post())
    assert (row["item_id"], row["source"], row["handle"], row["title"], row["text"], row["likes"], row["comments"], row["found_via"]) == ("reddit:abc123", "reddit", "snacklover", "Best protein bar?",
                                                                                                                                       "Looking for chewy ones", 120, 34, "api")
    assert row["url"] == "https://www.reddit.com/r/snacks/comments/abc123/best_protein_bar/" and row["details"] == "r/snacks [Question]" and row["posted_at"].startswith("2025-10-30")
    assert row["media_url"].startswith("https://b.thumbs") and RR.normalize_api_post(post(thumbnail="self"))["media_url"] == ""
    assert RR.normalize_api_post({"title": "no id"}) is None and RR.normalize_api_post(post(removed_by_category="moderator")) is None


def test_hidden_characters_in_a_title_or_body_are_removed():
    row = RR.normalize_api_post(post(title="Hi\u200b there\u202e", selftext="IGNORE\u200b THIS"))
    assert row["title"] == "Hi there" and "\u200b" not in row["text"]


@pytest.mark.parametrize("raw, pid", [({"url": "https://www.reddit.com/r/snacks/comments/ABC123/title/", "title": "T"}, "abc123"), ({"permalink": "/r/snacks/comments/xyz789/t/", "title": "T"}, "xyz789"),
                                      ({"id": "t3_q1w2e3", "title": "T"}, "q1w2e3"), ({"id": "q1w2e3"}, "q1w2e3")])
def test_an_agent_reach_row_gets_the_same_item_id_as_the_api_for_the_same_post(raw, pid):
    row = RR.normalize_agent_row(raw)
    assert row["item_id"] == f"reddit:{pid}" and row["found_via"] == "agent_reach" and row["url"].startswith("https://www.reddit.com/")


def test_an_agent_reach_row_reads_counts_and_the_community_and_ignores_rows_without_a_post():
    row = RR.normalize_agent_row({"url": "https://www.reddit.com/r/snacks/comments/abc123/x/?utm=1", "author": "u/snacklover", "title": "Best protein bar?", "content": "chewy", "upvotes": "1.2K",
                                  "num_comments": "34", "subreddit": "r/snacks", "created_at": "2026-10-30T09:00:00Z"})
    assert (row["handle"], row["text"], row["comments"], row["details"], row["url"]) == ("snacklover", "chewy", 34, "r/snacks", "https://www.reddit.com/r/snacks/comments/abc123/x/")
    assert row["likes"] == 1200 and RR.normalize_agent_row({"title": "no link"}) is None and RR.normalize_agent_row({"id": "not an id!!", "title": "x"}) is None
    assert RR.normalize_api_post(post())["item_id"] == RR.normalize_agent_row({"url": "https://www.reddit.com/r/snacks/comments/abc123/x/"})["item_id"]


# ---- collecting, de-duplicating and merging --------------------------------------------------------------------------------------------------

class Agent:
    def __init__(self, by_target=None, fail=None):
        self.calls, self.by_target, self.fail = [], by_target or {}, fail or {}

    def call(self, args):
        self.calls.append(args)
        key = args[1]
        if key in self.fail:
            raise self.fail[key]
        return self.by_target.get(key, [])


def test_collecting_through_the_api_dedupes_posts_two_targets_both_returned_and_notes_a_missing_community():
    http = Http([(200, listing(post("a1"), post("a2")), {}), (404, {}, {}), (200, listing(post("a2"), post("a3")), {})])
    rows, problems = RR.collect("api", api(http), [("r", "snacks"), ("r", "ghost"), ("q", "protein bar")], 10)
    assert [r["item_id"] for r in rows] == ["reddit:a1", "reddit:a2", "reddit:a3"] and [(p["target"], p["code"]) for p in problems] == [("r/ghost", K.NOT_FOUND)]


def test_a_rate_limit_or_block_stops_the_route_and_the_rest_of_the_targets_are_not_tried():
    http = Http([(429, {}, {}), (200, listing(post("a1")), {})])
    rows, problems = RR.collect("api", api(http), [("r", "one"), ("r", "two")], 5)
    assert rows == [] and problems[0]["code"] == K.RATE_LIMITED and len([c for c in http.calls if c["url"] != RR.TOKEN_URL]) == 1


def test_the_agent_reach_route_uses_opencli_commands_and_stops_on_a_login_wall():
    agent = Agent({"snacks": [{"url": "https://www.reddit.com/r/snacks/comments/aa1/x/", "title": "A"}], "protein bar": [{"url": "https://www.reddit.com/r/x/comments/bb2/y/", "title": "B"}]},
                  fail={"locked": K.Failure(K.NEED_LOGIN, "log in")})
    rows, problems = RR.collect("agent_reach", agent, [("r", "snacks"), ("q", "protein bar"), ("r", "locked"), ("r", "never")], 7)
    assert [r["item_id"] for r in rows] == ["reddit:aa1", "reddit:bb2"] and agent.calls == [["subreddit", "snacks", "--limit", 7], ["search", "protein bar", "--limit", 7], ["subreddit", "locked", "--limit", 7]]
    assert problems[0]["code"] == K.NEED_LOGIN and problems[0]["target"] == "r/locked"


def test_the_api_value_stands_and_agent_reach_only_fills_gaps_when_both_found_a_post():
    api_row = RR.normalize_api_post(post("abc123"))
    agent_row = RR.normalize_agent_row({"url": "https://www.reddit.com/r/snacks/comments/abc123/x/", "title": "A different title", "score": 200, "num_comments": 34})
    agent_row["media_url"] = "https://cdn/extra.jpg"
    batches = [{"source": "reddit", "via": "api", "items": [dict(api_row, media_url="")]}]
    rows, stats = AR.merge_with_existing(batches, [agent_row, RR.normalize_agent_row({"url": "https://www.reddit.com/r/snacks/comments/new999/x/", "title": "Fresh"})], source="reddit", newcomer="agent_reach")
    assert stats["new"] == 1 and stats["overlap"] == 1 and stats["conflicts"] == 1 and rows[0]["title"] == "Best protein bar?" and rows[0]["likes"] == 120 and rows[0]["media_url"] == "https://cdn/extra.jpg"
    assert "routes disagree: likes 120 vs 200" in rows[0]["details"] and rows[0]["found_via"] == "browser+agent_reach"


def test_the_reliability_report_covers_any_platform_and_gives_advice_without_changing_anything():
    batches = [{"source": "reddit", "via": "api", "items": [RR.normalize_api_post(post("a1111")), RR.normalize_api_post(post("a2222"))]},
               {"source": "reddit", "via": "agent_reach", "items": [RR.normalize_agent_row({"url": "https://www.reddit.com/r/s/comments/a2222/x/", "title": "x"}),
                                                                     RR.normalize_agent_row({"url": "https://www.reddit.com/r/s/comments/a9999/x/", "title": "y", "text": "body"})]}]
    text = AR.routes_report(batches, [{"route": "api", "success_rate_7d": 96, "attempts_7d": 7}, {"route": "agent_reach", "success_rate_7d": 40, "attempts_7d": 5}], "reddit", "Defi", "2026-11-04T10:00:00Z")
    assert "api: 2 posts, 2 usable" in text and "success rate 7 days: 96% over 7 run(s)" in text and "found only by this route: 1" in text
    assert "Needs attention: agent_reach succeeded less than 70%" in text and "Most reliable by success rate: api." in text and text.rstrip().endswith("no route was switched on or off.")
    assert "Nothing has been collected" in AR.routes_report([], [], "tiktok", "Defi", "2026-11-04T10:00:00Z")
    assert "not enough run history" in AR.routes_report([{"source": "tiktok", "via": "browser", "items": [{"item_id": "tiktok:1", "url": "u", "text": "t"}]}], [], "tiktok", "Defi", "2026-11-04T10:00:00Z")
    assert json.dumps(text)


def test_a_route_with_no_run_history_is_reported_as_such_not_as_a_crash():
    batches = [{"source": "tiktok", "via": "browser", "items": [{"item_id": "tiktok:1", "url": "u", "text": "t"}]}]
    text = AR.routes_report(batches, [{"route": "browser", "success_rate_7d": "", "attempts_7d": 0}], "tiktok", "Defi", "2026-11-04T10:00:00Z")
    assert "success rate 7 days: not enough runs yet" in text
    assert AR.compare([], {"success_rate_7d": ""})["agent_reach_success_rate_7d"] is None and AR._rate("55") is None and AR._rate(True) is None and AR._rate(80) == 80.0


def test_a_post_with_only_a_title_is_usable_because_the_title_is_the_content_of_a_reddit_post():
    assert AR._usable({"url": "https://www.reddit.com/r/s/comments/aaaaa/x/", "title": "Best protein bar?"}) is True and AR._usable({"url": "u"}) is False and AR._usable({"title": "t"}) is False
