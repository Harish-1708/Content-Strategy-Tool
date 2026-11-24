"""Apify async start + poll, the 408 "not finished yet" rule, split asks, and the client-side date filter.

The fake internet here is FakeHttp (routes matched by method + URL prefix), the same pattern
test_apify_backend.py uses. A big ask (more than SYNC_MAX_ITEMS items) starts async: POST
/v2/acts/{actor}/runs, then GET /v2/actor-runs/{runId} until SUCCEEDED, then the dataset items.
A small ask stays on the sync endpoint exactly as before.
"""
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import providers as P
from contentintel import research as R
from contentintel import voice as VOICE
from contentintel.apify import (
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    SYNC_MAX_ITEMS,
    Apify,
    ApifyPool,
    ApifyRunUnfinished,
    filter_by_create_time,
)
from contentintel.store import Store

from fake_http import FakeHttp
from test_apify_backend import AP_AD, apify_world, run as run_collect
from test_collectors import statuses, used, world, write_research

ACTOR = "clockworks/tiktok-scraper"
RUNS_URL = "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs"
SYNC_URL = "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items"
STATUS_URL = "https://api.apify.com/v2/actor-runs/run-tt"
DATASET_URL = "https://api.apify.com/v2/datasets/ds-tt/items"


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def _item(n, posted="2026-10-01T10:00:00.000Z"):
    return {"id": f"v{n}", "text": f"video {n} about overalls", "webVideoUrl": f"https://www.tiktok.com/@acme/video/{n}",
            "createTimeISO": posted, "authorMeta": {"name": "acme"}, "playCount": 10}


def async_world(items, *, status="SUCCEEDED"):
    """An Apify fake whose only route for this actor is async: start, one status read, dataset."""
    http = FakeHttp()
    http.on("POST", RUNS_URL, (201, {"data": {"id": "run-tt", "status": "RUNNING", "defaultDatasetId": "ds-tt"}}))
    http.on("GET", STATUS_URL, (200, {"data": {"id": "run-tt", "status": status, "defaultDatasetId": "ds-tt"}}))
    http.on("GET", DATASET_URL, (200, items))
    return http, Apify("tok", http=http, sleep=lambda s: None)


def _ledger(**used_items):
    return R.Ledger({"months": {"2026-11": dict(used_items)}, "lifetime": {}}, "2026-11")


def _norm(source, raw):
    """The real lane normalizes Apify rows first (Instagram rows carry `caption`, not `text`); the test fake does the same mapping, nothing more."""
    return [{**r, "text": r.get("text") or r.get("caption") or ""} for r in raw], 0


# --- the switch: sync only for a small ask ------------------------------------------------------------------------

def test_a_small_ask_stays_on_the_sync_endpoint_and_nothing_about_it_changes():
    http, apify = apify_world()
    items = apify.run(ACTOR, {"searchQueries": ["overalls"], "resultsPerPage": 5}, max_items=5, max_cost_usd=0.5)
    (call,) = http.calls
    assert call["method"] == "POST" and call["url"] == SYNC_URL
    assert call["params"] == {"timeout": 280, "maxItems": 5, "maxTotalChargeUsd": 0.5} and call["timeout"] == 310
    assert len(items) == 1


def test_a_big_ask_starts_async_and_is_polled_then_read_from_its_dataset():
    items = [_item(i) for i in range(1, 4)]
    http, apify = async_world(items)
    got = apify.run(ACTOR, {"searchQueries": ["overalls"], "resultsPerPage": 20}, max_items=20, max_cost_usd=0.5)
    assert got == items
    assert [c["url"] for c in http.calls] == [RUNS_URL, STATUS_URL, DATASET_URL]
    start = http.calls[0]
    assert start["params"] == {"timeout": DEFAULT_MAX_WAIT, "maxItems": 20, "maxTotalChargeUsd": 0.5}
    assert http.calls[2]["params"] == {"limit": 20, "offset": 0, "format": "json"}
    assert len(items) == 3


def test_the_switch_is_the_ask_size_and_each_way_can_be_forced():
    assert SYNC_MAX_ITEMS == 10
    http, apify = async_world([_item(1)])
    assert len(apify.run(ACTOR, {}, max_items=10, max_cost_usd=0.5, mode="async")) == 1           # small, forced async
    assert [c["url"] for c in http.calls] == [RUNS_URL, STATUS_URL, DATASET_URL]
    http2, apify2 = apify_world()
    assert len(apify2.run(ACTOR, {}, max_items=30, max_cost_usd=0.5, mode="sync")) == 1          # big, forced sync
    assert [c["url"] for c in http2.calls] == [SYNC_URL]
    with pytest.raises(ValueError, match="mode must be auto, sync or async"):
        apify.run(ACTOR, {}, max_items=1, max_cost_usd=0.5, mode="sometimes")


def test_the_poll_waits_between_reads_and_stops_at_the_first_terminal_status():
    waits = []
    http, apify = async_world([_item(1)])
    apify.sleep = waits.append
    http.routes = [r for r in http.routes if "actor-runs" not in r[1]]
    http.on("GET", STATUS_URL,
            (200, {"data": {"id": "run-tt", "status": "RUNNING", "defaultDatasetId": "ds-tt"}}),
            (200, {"data": {"id": "run-tt", "status": "SUCCEEDED", "defaultDatasetId": "ds-tt"}}))
    assert len(apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5, poll_interval=7)) == 1
    assert waits == [7] and DEFAULT_POLL_INTERVAL == 5


def test_an_async_start_is_never_retried_because_a_retry_would_start_a_second_billed_run():
    calls = []

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        calls.append(url)
        raise ConnectionError("dropped")

    apify = Apify("tok", http=http, sleep=lambda s: None)
    with pytest.raises(P.ProviderError, match="may have started server-side"):
        apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5)
    assert len(calls) == 1


# --- a 408 is "not finished yet" -------------------------------------------------------------------------------

def test_a_408_that_names_the_run_is_polled_and_its_dataset_fetched_not_started_again():
    http = FakeHttp()
    http.on("POST", SYNC_URL, (408, {"error": {"type": "run-timeout-exceeded",
                                               "message": "run did not finish (run ID: run-tt, status: RUNNING)"}}))
    http.on("GET", STATUS_URL, (200, {"data": {"id": "run-tt", "status": "SUCCEEDED", "defaultDatasetId": "ds-tt"}}))
    http.on("GET", DATASET_URL, (200, [_item(1), _item(2)]))
    apify = Apify("tok", http=http, sleep=lambda s: None)
    assert apify.run(ACTOR, {}, max_items=5, max_cost_usd=0.5) == [_item(1), _item(2)]
    assert [c["url"] for c in http.calls] == [SYNC_URL, STATUS_URL, DATASET_URL]          # one sync call, then the recovery: no second run


@pytest.mark.parametrize("body", [
    {"data": {"id": "run-tt", "status": "RUNNING"}},
    {"error": {"type": "run-timeout-exceeded", "message": "slow", "data": {"id": "run-tt"}}},
    {"error": {"type": "run-timeout-exceeded", "message": "Actor run did not succeed (run ID: run-tt, status: RUNNING)"}},
])
def test_a_408_run_id_is_found_in_each_shape_apify_uses(body):
    http = FakeHttp()
    http.on("POST", SYNC_URL, (408, body))
    http.on("GET", STATUS_URL, (200, {"data": {"id": "run-tt", "status": "SUCCEEDED", "defaultDatasetId": "ds-tt"}}))
    http.on("GET", DATASET_URL, (200, [_item(1)]))
    assert len(Apify("tok", http=http, sleep=lambda s: None).run(ACTOR, {}, max_items=5, max_cost_usd=0.5)) == 1


def test_a_408_with_no_run_id_is_a_clear_error_and_is_never_repeated():
    http = FakeHttp()
    http.on("POST", SYNC_URL, (408, {"error": {"type": "run-timeout-exceeded", "message": "t"}}))
    apify = Apify("tok", http=http, sleep=lambda s: None)
    with pytest.raises(ApifyRunUnfinished) as seen:
        apify.run(ACTOR, {}, max_items=5, max_cost_usd=0.5)
    assert len(http.calls) == 1 and "not repeated" in str(seen.value) and "HTTP 408" in str(seen.value)
    assert "may still be going server-side" in str(seen.value) and "429" not in str(seen.value)


def test_a_run_that_never_finishes_is_reported_unfinished_not_failed_and_starts_nothing_again():
    http, apify = async_world([_item(1)], status="RUNNING")
    with pytest.raises(ApifyRunUnfinished) as seen:
        apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5, max_wait=0.05)
    assert "still running" in str(seen.value) and "do not start the same ask again" in str(seen.value)
    assert [c["url"] for c in http.calls].count(RUNS_URL) == 1 and DATASET_URL not in [c["url"] for c in http.calls]


@pytest.mark.parametrize("status", ["FAILED", "ABORTED"])
def test_a_failed_or_aborted_run_is_a_failure_and_its_dataset_is_never_read(status):
    http, apify = async_world([_item(1)], status=status)
    with pytest.raises(P.ProviderError, match=f"ended {status}"):
        apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5)
    assert DATASET_URL not in [c["url"] for c in http.calls]


def test_a_timed_out_run_says_so_plainly_so_the_voice_lane_may_ask_again_for_fewer():
    http, apify = async_world([], status="TIMED-OUT")
    with pytest.raises(P.ProviderError) as seen:
        apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5)
    assert "timed-out" in str(seen.value).lower() and "run-tt" in str(seen.value)


# --- split large asks --------------------------------------------------------------------------------------------

def test_one_target_per_call_instead_of_one_big_call():
    seen = []

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        seen.append((json_body, params))
        tag = (json_body.get("hashtags") or json_body.get("searchQueries") or ["?"])[0]
        return 200, [{"id": tag, "text": f"video for {tag}"}], None

    apify = Apify("tok", http=http, sleep=lambda s: None)
    got = apify.run(ACTOR, {"hashtags": ["a", "b"], "searchQueries": ["x y"], "resultsPerPage": 5},
                    max_items=15, max_cost_usd=0.5, mode="sync")
    assert got == [{"id": "a", "text": "video for a"}, {"id": "b", "text": "video for b"}, {"id": "x y", "text": "video for x y"}]
    assert [b for b, _ in seen] == [{"hashtags": ["a"], "resultsPerPage": 5}, {"hashtags": ["b"], "resultsPerPage": 5},
                                    {"searchQueries": ["x y"], "resultsPerPage": 5}]
    assert [p["maxItems"] for _, p in seen] == [5, 5, 5]                     # each split call carries its own per-target count


def test_split_calls_stop_once_the_cap_is_reached_and_a_video_under_two_targets_is_kept_once():
    calls = []

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        calls.append(json_body)
        tag = json_body["hashtags"][0]
        return 200, [{"id": "same", "text": f"video for {tag}"}, {"id": tag, "text": f"second for {tag}"}], None

    apify = Apify("tok", http=http, sleep=lambda s: None)
    got = apify.run(ACTOR, {"hashtags": ["a", "b", "c"], "resultsPerPage": 2}, max_items=3, max_cost_usd=0.5, mode="sync")
    assert [g["id"] for g in got] == ["same", "a", "b"]                        # "same" once; the third call never happens
    assert len(calls) == 2


def test_meta_ads_start_urls_are_never_split():
    http = FakeHttp()
    http.on("POST", "https://api.apify.com/v2/acts/apify~facebook-ads-scraper/run-sync-get-dataset-items", (200, [AP_AD]))
    apify = Apify("tok", http=http, sleep=lambda s: None)
    assert len(apify.run("apify/facebook-ads-scraper", {"startUrls": [{"url": "u1"}, {"url": "u2"}], "resultsLimit": 5},
                      max_items=5, max_cost_usd=1.0)) == 1
    assert len(http.calls) == 1 and http.calls[0]["json"]["startUrls"] == [{"url": "u1"}, {"url": "u2"}]


# --- the date filter: the actor's own does not work on hashtags --------------------------------------------------

def test_the_booktok_case_old_videos_are_dropped_when_a_window_is_asked_for():
    rows = [_item(1, "2022-06-01T10:00:00.000Z"),                    # 3 years 8 months before the October 2026 test
            _item(2, "2026-10-01T10:00:00.000Z"), _item(3, "2026-09-15T10:00:00.000Z"),
            {"id": "nodate", "text": "a video with no date at all"}]
    assert [r["id"] for r in filter_by_create_time(rows, since="2026-09-01")] == ["v2", "v3", "nodate"]
    assert [r["id"] for r in filter_by_create_time(rows, since="2026-09-01", until="2026-09-30")] == ["v3", "nodate"]
    assert [r["id"] for r in filter_by_create_time(rows, until="2023-01-01")] == ["v1", "nodate"]
    assert filter_by_create_time(rows) == rows                                        # no window asked: nothing is dropped


def test_the_date_filter_reads_instagrams_timestamp_too_and_rejects_a_backwards_window():
    rows = [{"id": "a", "timestamp": "2026-10-01T10:00:00.000Z"}, {"id": "b", "timestamp": "2020-01-01T00:00:00.000Z"}]
    assert [r["id"] for r in filter_by_create_time(rows, since="2026-01-01")] == ["a"]
    with pytest.raises(ValueError, match="since .* is after until"):
        filter_by_create_time(rows, since="2026-10-02", until="2026-10-01")


def test_a_date_window_is_applied_to_a_real_run_end_to_end():
    http, apify = async_world([_item(1, "2022-06-01T10:00:00.000Z"), _item(2, "2026-10-01T10:00:00.000Z")])
    got = apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5, since="2026-09-01")
    assert [g["id"] for g in got] == ["v2"]


# --- the pool: an unfinished run drops no key ---------------------------------------------------------------------

def test_the_pool_never_fails_over_an_unfinished_run_to_another_key():
    class Key:
        def __init__(self, error=None):
            self.error = error
            self.calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            if self.error:
                raise self.error
            return [{"ok": 1}]

        def usage(self):
            return None

    pool = ApifyPool(["a", "b"])
    first, second = Key(ApifyRunUnfinished("Apify: HTTP 408: the run did not finish")), Key()
    pool.clients = [first, second]
    with pytest.raises(ApifyRunUnfinished):
        pool.run("x/y", {}, max_items=1, max_cost_usd=0.5)
    assert (first.calls, second.calls) == (1, 0) and pool.dropped == {} and pool.events == []


def test_the_pool_still_fails_over_a_429_from_an_async_start():
    class Key:
        def __init__(self, error=None):
            self.error = error

        def run(self, *args, **kwargs):
            if self.error:
                raise self.error
            return [{"ok": 1}]

        def usage(self):
            return None

    pool = ApifyPool(["a", "b"])
    pool.clients = [Key(P.ProviderError("Apify: HTTP 429: too many requests")), Key()]
    assert pool.run("x/y", {}, max_items=20, max_cost_usd=0.5) == [{"ok": 1}]
    assert pool.dropped == {0: "RateLimited"}


def test_the_pool_forwards_the_async_options_to_the_key_that_answers():
    seen = {}

    class Key:
        def run(self, actor, payload, *, max_items, max_cost_usd, **kwargs):
            seen.update(kwargs)
            return [{"ok": 1}]

        def usage(self):
            return None

    pool = ApifyPool(["a"])
    pool.clients = [Key()]
    pool.run("x/y", {}, max_items=20, max_cost_usd=0.5, mode="async", poll_interval=8, max_wait=60)
    assert seen == {"mode": "async", "poll_interval": 8, "max_wait": 60}


# --- the voice lane on the new client ----------------------------------------------------------------------------

def test_voice_tiktok_at_twenty_items_goes_async_the_way_voice_18_needed():
    http, _ = apify_world()
    apify = Apify("tok", http=http, sleep=lambda s: None)
    lane = VOICE.ApifyVoice(apify, _ledger(), per_run=40, monthly=60, shared_cap=300,
                            normalize=_norm)
    items, notes = lane.run("sweatpant overalls")
    urls = [c["url"] for c in http.calls]
    assert RUNS_URL in urls and SYNC_URL not in urls                                # 20 items is past the sync cap: started, not waited on sync
    assert len(items) == 2 and {i["source"] for i in items} == {"apify tiktok", "apify instagram"} and len(notes) == 2


def test_voice_never_retries_a_run_that_may_still_be_going_even_when_its_message_says_timed_out():
    class Unfinished:
        def __init__(self):
            self.calls = 0

        def run(self, actor, payload, *, max_items, max_cost_usd):
            self.calls += 1
            raise ApifyRunUnfinished("Apify: the actor run abc timed-out after waiting")    # the words say timed-out; the type says unfinished

    fake = Unfinished()
    lane = VOICE.ApifyVoice(fake, _ledger(), normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("sweatpant overalls")
    assert fake.calls == 2                                       # tiktok once, instagram once: neither is tried a second time
    assert items == [] and all("ApifyRunUnfinished" in n for n in notes)


def test_voice_still_retries_a_run_that_is_over_at_half_the_ask():
    calls = []

    class HalfAgain:
        def run(self, actor, payload, *, max_items, max_cost_usd):
            calls.append((actor, max_items))
            if len(calls) == 1:
                raise P.ProviderError('Apify: the actor run K4WGG ended TIMED-OUT')
            return [{"text": "a caption about overalls", "likes": 5, "url": "https://x/1"}] * max_items

    lane = VOICE.ApifyVoice(HalfAgain(), _ledger(), normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("sweatpant overalls")
    assert calls[0][1] == 20 and calls[1][1] == 10 and "timed out on Apify's side; asking once more for 10" in notes[0]
    assert len(calls) == 3 and len(items) == 40                              # the retry's 10 TikTok items, then Instagram's 30


# --- Collect's own Apify provider covers TikTok: a config flip, not a code change -------------------------------

def test_collects_own_apify_provider_already_covers_tiktok():
    assert C.DEFAULT_APIFY_ACTORS["tiktok"] == "clockworks/tiktok-scraper"
    assert C.apify_input(C.Task("tiktok", "t", {"hashtags": ["a"], "searches": ["x y"]}, "k", 2), 10) == {
        "hashtags": ["a"], "searchQueries": ["x y"], "resultsPerPage": 5}


def test_routing_tiktok_collect_through_apify_is_a_mode_flip_in_research_yaml(tmp_path, store, swov, cfg, settings):
    for mode, backend in (("fallback", "fallback"), ("always", "apify")):
        assert C.effective_backend("auto", replace(settings, apify_mode=mode)) == backend
    http, apify = apify_world()
    write_research(tmp_path, swov, {"track_own_brand": False,
                                    "social": {"tiktok": {"hashtags": ["a"]}}})
    result = run_collect(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="apify",
                         sources=("tiktok",), data={"track_own_brand": False, "social": {"tiktok": {"hashtags": ["a"]}}})
    assert statuses(result) == [("tiktok", "ran")]
    assert {r["via"] for r in store.read_collected("swoveralls")} == {"apify"} and used(store) == {"apify_items": 1}
    assert C.load_collector_settings("does-not-exist.yaml").apify_mode == "fallback" == C.load_collector_settings("research.yaml").apify_mode          # Harish flipped it on 2026-11-24: Apify takes over when the browser route fails


@pytest.mark.parametrize("status", [202, 204])
def test_only_200_and_201_count_as_a_started_run_so_a_202_or_204_is_never_taken_as_one(status):
    calls = []

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        calls.append(url)
        return status, {"data": {"id": "run-x", "status": "RUNNING", "defaultDatasetId": "ds-x"}}, {}

    apify = Apify("tok", http=http, sleep=lambda s: None)
    with pytest.raises(P.ProviderError):
        apify.run(ACTOR, {}, max_items=20, max_cost_usd=0.5)
    assert len(calls) == 1                                                                                          # and nothing is started a second time
