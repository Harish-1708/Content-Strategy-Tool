"""The YouTube corpus, the outlier method and the corpus search."""
import json

import pytest

from contentintel import contract as K
from contentintel import evidence as E
from contentintel import sources as SRC
from contentintel.observatory import corpus as CP
from contentintel.observatory import snapshots as SN
from contentintel.store import Store
from conftest import ROOT
from observatory_helpers import settings

NOW = "2026-11-03T12:00:00Z"
FORMULAS = CP.load_formulas()
CFG = settings()["youtube"]


# ---- formulas and outliers --------------------------------------------------------------------------------------------------------------------

def test_the_21_formulas_load_with_working_patterns_and_the_licence_travels_with_the_data():
    assert len(FORMULAS) == 21 and len({f["id"] for f in FORMULAS}) == 21 and all(f["_rx"] for f in FORMULAS)
    licence = (CP.HOOKS_PATH.parent / "youtube_agent_skill_LICENSE.txt").read_text()
    assert "MIT License" in licence and "Schincariol" in licence


@pytest.mark.parametrize("title, expected", [
    ("97% of people who start a channel quit before video 30", "the-statistic"), ("I tried posting every day for 30 days", "i-tried-it"),
    ("Why does nobody talk about this?", "the-question"), ("a quiet morning vlog", ""), ("", "")])
def test_a_title_is_matched_to_the_formula_it_uses_or_to_none(title, expected):
    fid, name = CP.classify(title, FORMULAS)
    assert fid == expected and (name == "Unclassified") == (expected == "")


def vid(n, channel, views, title="t", **kw):
    return {"id": f"v{n}", "channel_id": channel, "channel_title": channel.title(), "title": title, "views": views, "url": f"https://youtu.be/v{n}", "shorts_class": "short", **kw}


def test_outliers_are_measured_against_each_channels_own_median_never_raw_views():
    big = [vid(n, "big", v) for n, v in enumerate([1_000_000, 1_100_000, 900_000, 1_050_000, 950_000], 1)]
    small = [vid(10 + n, "small", v, title="97% of people quit") for n, v in enumerate([10_000, 12_000, 9_000, 11_000, 60_000], 1)]
    res = CP.outliers(big + small, FORMULAS)
    assert [r["video_id"] for r in res["outliers"]] == ["v15"]                       # 60k on a small channel beats 1.1M on a huge one
    top = res["outliers"][0]
    assert top["multiple"] == 5.45 and top["median"] == 11000 and top["formula_id"] == "the-statistic" and top["channel_videos"] == 5
    assert res["channels"]["small"]["median"] == 11000 and res["formulas_among_outliers"] == {"The Statistic": 1}


def test_a_channel_with_fewer_than_four_readable_videos_is_listed_not_ranked():
    few = [vid(1, "tiny", 100), vid(2, "tiny", 90_000), vid(3, "tiny", 120), vid(4, "tiny", None)]
    many = [vid(10 + n, "ok", v) for n, v in enumerate([100, 110, 90, 105], 1)]
    res = CP.outliers(few + many, FORMULAS)
    assert res["thin_channels"] == [{"channel": "Tiny", "videos": 3}] and not [r for r in res["outliers"] if r["channel"] == "Tiny"]
    assert CP.outliers(few, FORMULAS, min_videos=3)["outliers"][0]["video_id"] == "v2"


def test_hidden_counts_zero_medians_thresholds_and_ties_are_handled():
    hidden = [vid(n, "c", v) for n, v in enumerate([100, 100, 100, 100, None, 300], 1)]
    res = CP.outliers(hidden, FORMULAS)
    assert [r["video_id"] for r in res["outliers"]] == ["v6"] and res["outliers"][0]["multiple"] == 3.0
    zero = [vid(n, "z", 0) for n in range(1, 6)] + [vid(9, "z", 50)]
    assert CP.outliers(zero, FORMULAS)["outliers"] == []                               # a median of zero says nothing
    even = [vid(n, "e", v) for n, v in enumerate([100, 100, 100, 100, 250, 250], 1)]
    assert [r["video_id"] for r in CP.outliers(even, FORMULAS)["outliers"]] == ["v5", "v6"]
    assert CP.outliers(even, FORMULAS, min_multiple=3)["outliers"] == []


@pytest.mark.parametrize("iso, secs, cls", [("PT45S", 45, "short"), ("PT1M", 60, "short"), ("PT1M1S", 61, "possible_short"), ("PT3M", 180, "possible_short"), ("PT3M1S", 181, "long"),
                                            ("PT1H2M3S", 3723, "long"), ("P0D", 0, "short"), ("garbage", None, ""), (None, None, "")])
def test_durations_are_read_and_shorts_are_a_guess_that_says_so(iso, secs, cls):
    assert CP.duration_seconds(iso) == secs and CP.shorts_class(secs) == cls


# ---- the API ----------------------------------------------------------------------------------------------------------------------------------

def item(n, channel="UCabcdefghijklmnopqrstu", views="1000", title="A title", dur="PT45S", caption="false", **extra):
    return {"id": f"vid{n}", "snippet": {"channelId": channel, "channelTitle": "Chan", "title": title, "publishedAt": "2026-10-01T00:00:00Z", "description": "First line\nsecond line",
                                         "tags": ["a"], "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/x/hq.jpg"}}},
            "contentDetails": {"duration": dur, "caption": caption}, "statistics": {"viewCount": views, "likeCount": "10", **extra}}


class Yt:
    """A scripted Data API. Each endpoint's answer is a function of its parameters."""

    def __init__(self, uploads=("vid1", "vid2"), error=None, views="1000"):
        self.calls, self.uploads, self.error, self.views = [], list(uploads), error, views

    def __call__(self, url, params, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        if self.error:
            return self.error
        end = url.rsplit("/", 1)[-1]
        if end == "channels":
            return 200, {"items": [{"id": "UCabcdefghijklmnopqrstu", "snippet": {"title": "Chan", "customUrl": "@chan"}, "statistics": {"subscriberCount": "5000", "videoCount": "40"},
                                    "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}}}]}
        if end == "playlistItems":
            start = int(params.get("pageToken") or 0)
            page = self.uploads[start:start + params["maxResults"]]
            more = start + len(page) < len(self.uploads)
            return 200, {"items": [{"contentDetails": {"videoId": v}} for v in page], **({"nextPageToken": str(start + len(page))} if more else {})}
        if end == "videos":
            return 200, {"items": [item(int(i[3:]), views=self.views) for i in params["id"].split(",")]}
        if end == "search":
            return 200, {"items": [{"snippet": {"channelId": "UCfound1", "channelTitle": "Found"}}, {"snippet": {"channelId": "UCfound1", "channelTitle": "Found"}},
                                   {"snippet": {"channelId": "UCfound2", "channelTitle": "Two"}}]}
        return 404, {}


def api(tmp_path, yt=None, cap=3000, key="AIza-SECRET-KEY-0123456789"):
    q = CP.Quota(tmp_path, cap, NOW)
    return CP.YouTubeAPI(key, q, get=yt or Yt()), q


def test_a_channel_is_found_by_handle_or_id_and_its_uploads_and_details_are_read(tmp_path):
    a, q = api(tmp_path)
    ch = a.channel("chan")
    assert ch == {"id": "UCabcdefghijklmnopqrstu", "title": "Chan", "handle": "@chan", "subscribers": 5000, "video_count": 40, "uploads": "UUabc"}
    assert a.get.calls[0][1]["forHandle"] == "@chan" and "id" not in a.get.calls[0][1]
    a.channel("UCabcdefghijklmnopqrstu")
    assert a.get.calls[1][1]["id"] == "UCabcdefghijklmnopqrstu" and a.get.calls[1][1].get("forHandle") is None
    ids = a.upload_ids("UUabc", 10)
    assert ids == ["vid1", "vid2"] and [i["id"] for i in a.videos(ids)] == ["vid1", "vid2"] and q.used == 4


def test_long_playlists_are_paged_and_videos_are_looked_up_fifty_at_a_time(tmp_path):
    yt = Yt(uploads=[f"vid{n}" for n in range(1, 121)])
    a, q = api(tmp_path, yt)
    ids = a.upload_ids("UUabc", 120)
    assert len(ids) == 120 and [c[1]["maxResults"] for c in yt.calls] == [50, 50, 20]
    a.videos(ids)
    assert [len(c[1]["id"].split(",")) for c in yt.calls if c[0] == "videos"] == [50, 50, 20] and q.used == 6
    assert len(a.upload_ids("UUabc", 7)) == 7


def test_each_call_spends_its_documented_units_and_a_search_costs_a_hundred(tmp_path):
    a, q = api(tmp_path)
    a.search("protein snacks")
    assert q.used == 100 and CP.COST == {"channels": 1, "playlistItems": 1, "videos": 1, "search": 100}
    found = CP.discover_channels(a, ["q1", "q2", "q3"], 2)
    assert found == [("UCfound1", "Found", "q1"), ("UCfound2", "Two", "q1")] and q.used == 300           # only two searches were allowed, and duplicates collapse


def test_the_tools_own_daily_cap_stops_a_call_before_it_is_made_and_does_not_blame_youtube(tmp_path):
    a, q = api(tmp_path, cap=120)
    a.search("one")
    with pytest.raises(K.Failure) as seen:
        a.search("two")
    assert seen.value.code == K.BAD_INPUT and seen.value.budget is True and len(a.get.calls) == 1 and q.used == 100
    q.save()
    again = CP.Quota(tmp_path, 120, NOW)
    assert again.used == 100
    assert CP.Quota(tmp_path, 120, "2026-11-04T20:00:00Z").used == 0                                    # a new Pacific day starts again from zero


def test_the_quota_day_follows_pacific_time():
    assert CP.pacific_day("2026-11-03T07:59:00Z") == "2026-11-02" and CP.pacific_day("2026-11-03T08:01:00Z") == "2026-11-03"
    assert CP.pacific_day("2026-07-03T06:59:00Z") == "2026-07-02" and CP.pacific_day("2026-07-03T07:01:00Z") == "2026-07-03"     # daylight saving


@pytest.mark.parametrize("answer, code", [
    ((403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}), K.RATE_LIMITED), ((400, {"error": {"message": "API key not valid. Please pass a valid API key.", "errors": [{"reason": "badRequest"}]}}), K.NEED_SETUP),
    ((403, {"error": {"errors": [{"reason": "accessNotConfigured"}], "message": "YouTube Data API has not been used"}}), K.NEED_SETUP), ((404, {}), K.NOT_FOUND), ((503, {}), K.UPSTREAM_BROKEN),
    ((418, {"error": {"message": "teapot"}}), K.UPSTREAM_BROKEN)])
def test_api_failures_get_their_real_codes_and_the_key_never_appears_in_a_message(tmp_path, answer, code):
    a, _ = api(tmp_path, Yt(error=answer))
    with pytest.raises(K.Failure) as seen:
        a.videos(["x"])
    assert seen.value.code == code and "SECRET" not in str(seen.value) and "AIza" not in str(seen.value)


def test_silence_and_network_errors_are_timeout_and_upstream_broken(tmp_path):
    def silent(url, params, timeout):
        raise TimeoutError("t")

    def dead(url, params, timeout):
        raise OSError("ConnectionError")
    with pytest.raises(K.Failure) as seen:
        api(tmp_path, silent)[0].videos(["x"])
    assert seen.value.code == K.TIMEOUT
    with pytest.raises(K.Failure) as seen:
        api(tmp_path, dead)[0].videos(["x"])
    assert seen.value.code == K.UPSTREAM_BROKEN


def test_a_channel_that_does_not_exist_is_not_found(tmp_path):
    class NoItems(Yt):
        def __call__(self, url, params, timeout):
            return (200, {"items": []}) if url.endswith("/channels") else super().__call__(url, params, timeout)
    with pytest.raises(K.Failure) as seen:
        api(tmp_path, NoItems())[0].channel("@ghost")
    assert seen.value.code == K.NOT_FOUND


# ---- records and the stored corpus ------------------------------------------------------------------------------------------------------------

def test_a_video_record_is_cleaned_classified_and_says_the_spoken_hook_is_unavailable():
    rec = CP.video_record(item(1, title="Hello\u200b world", dur="PT1M30S", caption="true", commentCount="7"), NOW)
    assert rec["title"] == "Hello world" and rec["shorts_class"] == "possible_short" and rec["views"] == 1000 and rec["likes"] == 10 and rec["comments"] == 7
    assert rec["has_captions"] is True and rec["description_line"] == "First line" and rec["url"] == "https://www.youtube.com/watch?v=vid1"
    assert rec["spoken_hook"] is None and "not available through the YouTube Data API" in rec["spoken_hook_note"]
    hidden = CP.video_record({"id": "x", "snippet": {}, "statistics": {}, "contentDetails": {}}, NOW)
    assert hidden["views"] is None and hidden["likes"] is None and hidden["shorts_class"] == ""


def test_the_corpus_keeps_a_history_of_counts_that_is_capped_and_only_grows_when_something_changed():
    corpus = {"channels": {}, "videos": {}}
    rec = CP.video_record(item(1), NOW)
    assert CP.merge_video(corpus, rec, NOW) == "new"
    assert CP.merge_video(corpus, rec, "2026-11-03T18:00:00Z") == "updated" and len(corpus["videos"]["vid1"]["history"]) == 1          # nothing changed within a day
    assert CP.merge_video(corpus, {**rec, "views": 2000}, "2026-11-03T20:00:00Z") == "updated" and corpus["videos"]["vid1"]["history"][-1]["views"] == 2000
    for day in range(4, 30):
        CP.merge_video(corpus, {**rec, "views": 2000}, f"2026-11-{day:02d}T12:00:00Z")
    v = corpus["videos"]["vid1"]
    assert len(v["history"]) == 12 and v["first_seen"] == NOW and v["last_seen"] == "2026-11-29T12:00:00Z"


def test_the_corpus_saves_and_reloads_and_a_damaged_file_is_an_empty_corpus(tmp_path):
    store = Store(tmp_path)
    assert CP.load_corpus(store, "b") == {"version": 1, "channels": {}, "videos": {}}
    corpus = CP.load_corpus(store, "b")
    CP.merge_video(corpus, CP.video_record(item(1), NOW), NOW)
    CP.save_corpus(store, "b", corpus)
    assert "vid1" in CP.load_corpus(store, "b")["videos"]
    CP.corpus_path(store, "b").write_text("{broken")
    assert CP.load_corpus(store, "b")["videos"] == {}


def test_collecting_channels_reads_uploads_stores_videos_and_survives_one_bad_channel(tmp_path):
    corpus = {"channels": {}, "videos": {}}
    a, q = api(tmp_path, Yt(uploads=["vid1", "vid2", "vid3"]))
    bad_get = a.get

    def flaky(url, params, timeout):
        if url.endswith("/channels") and params.get("forHandle") == "@ghost":
            return 404, {}
        return bad_get(url, params, timeout)
    a.get = flaky
    got = CP.collect_channels(a, corpus, ["@ghost", "@chan"], CFG, NOW)
    assert got["channels"] == 1 and got["videos"] == 3 and got["new"] == 3 and got["errors"][0]["code"] == K.NOT_FOUND and sorted(corpus["videos"]) == ["vid1", "vid2", "vid3"]
    assert corpus["channels"]["UCabcdefghijklmnopqrstu"]["subscribers"] == 5000


def test_a_bad_key_or_a_used_up_quota_stops_collection_and_keeps_what_was_read(tmp_path):
    corpus = {"channels": {}, "videos": {}}
    a, _ = api(tmp_path, Yt(error=(403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}})))
    with pytest.raises(CP._Stopped) as seen:
        CP.collect_channels(a, corpus, ["@a", "@b"], CFG, NOW)
    assert seen.value.failure.code == K.RATE_LIMITED and len(a.get.calls) == 1 and seen.value.partial["errors"][0]["channel"] == "@a"


# ---- evidence ---------------------------------------------------------------------------------------------------------------------------------

def test_a_video_becomes_a_valid_evidence_record_with_honest_labels():
    registry = SRC.load_sources(ROOT / "sources.yaml")
    corpus = {"channels": {}, "videos": {}}
    for n, v in enumerate(["100", "110", "90", "105", "900"], 1):
        CP.merge_video(corpus, CP.video_record(item(n, views=v, title="97% of people quit"), NOW), NOW)
    res = CP.outliers(list(corpus["videos"].values()), FORMULAS)
    top = res["outliers"][0]
    rec = CP.evidence_for_video(corpus["videos"]["vid5"], registry, NOW, multiple=top, formulas=FORMULAS)
    assert E.validate_evidence(rec, registry, NOW) == []
    assert rec["origin"]["access_tier"] == "official_api" and rec["quality"]["completeness"]["status"] == "partial" and "transcript" in rec["quality"]["completeness"]["missing"]
    assert rec["analysis"]["formula"] == "the-statistic" and rec["analysis"]["confidence"] == "low" and rec["content"]["format"] == "short"
    mult = next(m for m in rec["metrics"] if m["name"] == "outlier_multiple")
    assert mult["kind"] == "proxy" and mult["denominator"] == 105 and mult["value"] == 8.57 and any("not available" in x for x in rec["quality"]["limitations"])
    plain = CP.evidence_for_video(corpus["videos"]["vid1"], registry, NOW)
    assert E.validate_evidence(plain, registry, NOW) == [] and plain["analysis"]["formula"] == ""


def test_the_outlier_report_is_plain_and_states_its_limits():
    corpus = {"channels": {}, "videos": {}}
    for n, v in enumerate(["100", "110", "90", "105", "900"], 1):
        CP.merge_video(corpus, CP.video_record(item(n, views=v), NOW), NOW)
    text = CP.report_text(CP.outliers(list(corpus["videos"].values()), FORMULAS), "Defi", NOW)
    assert "8.57x" in text and "does not say why the video worked" in text and "Spoken hooks are not available" in text
    assert "Nothing cleared the threshold" in CP.report_text(CP.outliers([], FORMULAS), "Defi", NOW)


# ---- one search across the corpus -------------------------------------------------------------------------------------------------------------

DOCS = [{"id": "a", "source": "meta_ads", "kind": "ad", "title": "Protein bar launch", "text": "A chewy protein bar made with oats", "url": "u1", "when": NOW},
        {"id": "b", "source": "web_search", "kind": "snippet", "title": "Gym shoes", "text": "protein is mentioned once here among many other words about shoes and laces", "url": "u2", "when": NOW},
        {"id": "c", "source": "youtube_public", "kind": "short", "title": "Oats for breakfast", "text": "Chan", "url": "u3", "when": NOW}]


def test_search_ranks_by_relevance_weights_titles_and_returns_nothing_for_no_match():
    got = CP.search(DOCS, "protein bar")
    assert [r["id"] for r in got] == ["a", "b"] and got[0]["score"] > got[1]["score"] and "protein bar" in got[0]["snippet"].lower()
    assert [r["id"] for r in CP.search(DOCS, "oats")] == ["c", "a"]                       # a title match outranks a mention in the text
    assert CP.search(DOCS, "zebra") == [] and CP.search(DOCS, "") == [] and CP.search([], "protein") == []
    assert len(CP.search(DOCS, "protein oats", limit=1)) == 1


def test_a_title_match_outranks_one_passing_mention_when_nothing_else_differs():
    """With the title counted once the two documents are identical in length and frequency, a dead tie that the id would settle in favour of 'a-mention'.
    Only the extra weight on titles puts 'z-title' first."""
    words = [f"w{n}" for n in range(10)]
    docs = [{"id": "a-mention", "source": "x", "kind": "", "title": "bars", "text": " ".join(["oats"] + words[:9]), "url": "", "when": NOW},
            {"id": "z-title", "source": "x", "kind": "", "title": "oats", "text": " ".join(words), "url": "", "when": NOW}]
    got = CP.search(docs, "oats")
    assert [r["id"] for r in got] == ["z-title", "a-mention"] and got[0]["score"] > got[1]["score"]


def test_documents_are_gathered_from_collected_data_videos_and_competitor_pages(tmp_path):
    store, snaps = Store(tmp_path), SN.SnapshotStore(tmp_path)
    registry = SRC.load_sources(ROOT / "sources.yaml")
    store.append_collected("b", {"at": NOW, "source": "meta_ads", "via": "browser", "items": [{"source": "meta_ads", "handle": "Elev8", "title": "Protein ad", "text": "oats",
                                                                                          "url": "https://facebook.com/ads/library/?id=1", "item_id": "1"}]})
    corpus = CP.load_corpus(store, "b")
    CP.merge_video(corpus, CP.video_record(item(1, title="Protein breakfast"), NOW), NOW)
    CP.save_corpus(store, "b", corpus)
    snaps.save("shop-example", "page-aaaa", {"kind": "page", "url": "https://shop.example/", "title": "Home", "text": "Free shipping on protein bars", "captured_at": NOW}, NOW)
    snaps.save("shop-example", "feed-bbbb", {"kind": "feed", "captured_at": NOW, "items": [{"id": "1", "title": "Launch post", "summary": "our new protein flavour", "link": "https://shop.example/p"}]}, NOW)
    docs = CP.build_documents(store, snaps, "b", registry, NOW, ["shop-example"])
    assert {d["source"] for d in docs} >= {"youtube_public", "competitor_pages"} and len(docs) >= 4
    hits = CP.search(docs, "protein")
    assert {"youtube_public", "competitor_pages"} <= {h["source"] for h in hits} and json.dumps(hits)


# ---- trend speed: how fast a video gains views against its own channel's usual pace --------------------------------------------------------------

def stored(vid_id, channel, points, published="2026-10-01T00:00:00Z", views=None):
    """A stored video with its view history [(time, views), ...]."""
    return {"id": vid_id, "channel_id": channel, "channel_title": channel.title(), "title": f"Title {vid_id}", "views": views if views is not None else points[-1][1], "published_at": published,
            "url": f"https://youtu.be/{vid_id}", "shorts_class": "short", "history": [{"at": t, "views": v, "likes": None, "comments": None} for t, v in points]}


D1, D2, D3 = "2026-11-01T12:00:00Z", "2026-11-02T12:00:00Z", "2026-11-03T12:00:00Z"
NOW3 = "2026-11-03T12:00:00Z"


def test_speed_is_views_gained_per_day_between_readings_and_a_riser_beats_its_channels_usual_pace():
    vids = [stored("a", "chan", [(D1, 1000), (D3, 1200)]), stored("b", "chan", [(D1, 500), (D3, 700)]), stored("c", "chan", [(D1, 800), (D3, 1000)]),
            stored("hot", "chan", [(D1, 900), (D3, 2900)])]
    res = CP.velocity(vids, NOW3)
    assert [r["video_id"] for r in res["trending"]] == ["hot"]
    hot = res["trending"][0]
    assert hot["per_day"] == 1000.0 and hot["baseline_per_day"] == 100.0 and hot["multiple"] == 10.0 and hot["basis"] == "2 readings over 2.0 days" and res["evaluated"] == 4


def test_a_video_read_only_once_is_estimated_from_its_age_but_only_if_it_is_recent():
    fresh = stored("fresh", "chan", [(D3, 4000)], published="2026-11-01T12:00:00Z")           # 2 days old: 2,000 a day
    old = stored("old", "chan", [(D3, 900000)], published="2025-01-01T00:00:00Z")             # a lifetime average says nothing about now
    peers = [stored(f"p{n}", "chan", [(D3, 400)], published="2026-11-01T12:00:00Z") for n in range(3)]
    res = CP.velocity([fresh, old] + peers, NOW3)
    assert res["evaluated"] == 4 and [r["video_id"] for r in res["trending"]] == ["fresh"]
    assert res["trending"][0]["basis"].startswith("estimated from age (2.0 days, one reading)") and res["trending"][0]["per_day"] == 2000.0


def test_readings_too_close_together_falling_counts_and_old_readings_give_no_speed():
    close = stored("close", "chan", [("2026-11-03T06:00:00Z", 100), ("2026-11-03T10:00:00Z", 900)])          # only 4 hours apart
    fell = stored("fell", "chan", [(D1, 900), (D3, 800)])                                                    # YouTube corrected the count downwards
    stale = stored("stale", "chan", [("2026-10-01T00:00:00Z", 10), ("2026-10-05T00:00:00Z", 9999)])        # readings outside the 7-day window
    for v in (close, fell, stale):
        v["published_at"] = "2020-01-01T00:00:00Z"
    assert CP.velocity([close, fell, stale], NOW3)["evaluated"] == 0
    assert CP.velocity([close], NOW3, min_hours=2)["evaluated"] == 1


def test_a_channel_with_fewer_than_three_speeds_has_no_usual_pace_so_nothing_is_ranked():
    pair = [stored("a", "tiny", [(D1, 100), (D3, 5000)]), stored("b", "tiny", [(D1, 100), (D3, 150)])]
    res = CP.velocity(pair, NOW3)
    assert res["trending"] == [] and res["thin_channels"] == [{"channel": "Tiny", "videos": 2}]


def test_a_zero_baseline_threshold_and_ordering_are_handled():
    flat = [stored(n, "chan", [(D1, 100), (D3, 100)]) for n in "abc"] + [stored("up", "chan", [(D1, 100), (D3, 300)])]
    assert CP.velocity(flat, NOW3)["trending"] == []                                          # a median speed of zero gives no multiple to rank by
    vids = [stored("x", "c", [(D1, 0), (D3, 200)]), stored("y", "c", [(D1, 0), (D3, 400)]), stored("z", "c", [(D1, 0), (D3, 1200)]), stored("w", "c", [(D1, 0), (D3, 800)])]
    ids = [r["video_id"] for r in CP.velocity(vids, NOW3, min_multiple=1.2)["trending"]]
    assert ids == ["z", "w"] and CP.velocity(vids, NOW3, min_multiple=5)["trending"] == []


def test_the_trending_report_is_plain_marks_estimates_and_says_what_is_missing():
    vids = [stored("a", "chan", [(D1, 1000), (D3, 1200)]), stored("b", "chan", [(D1, 500), (D3, 700)]), stored("c", "chan", [(D1, 800), (D3, 1000)]), stored("hot", "chan", [(D1, 900), (D3, 2900)])]
    text = CP.trending_report_text(CP.velocity(vids, NOW3), "Defi", NOW3)
    assert "10.00x" in text and "1,000/day vs      100 usual" in text and "2 readings over 2.0 days" in text and "Speed is views gained per day" in text
    assert "Nothing is gaining faster" in CP.trending_report_text(CP.velocity([], NOW3), "Defi", NOW3)


def test_trend_metrics_join_the_evidence_record_and_it_still_validates():
    registry = SRC.load_sources(ROOT / "sources.yaml")
    vids = [stored("a", "chan", [(D1, 1000), (D3, 1200)]), stored("b", "chan", [(D1, 500), (D3, 700)]), stored("c", "chan", [(D1, 800), (D3, 1000)]), stored("hot", "chan", [(D1, 900), (D3, 2900)])]
    for v in vids:
        v.update(captured_at=NOW3, first_seen=D1, last_seen=NOW3, description_line="d", thumbnail="")
    trend = CP.velocity(vids, NOW3)["trending"][0]
    rec = CP.evidence_for_video(vids[3], registry, NOW3, trend=trend)
    assert E.validate_evidence(rec, registry, NOW3) == []
    by = {m["name"]: m for m in rec["metrics"]}
    assert by["views_per_day"]["value"] == 1000.0 and by["views_per_day"]["kind"] == "proxy" and by["trend_multiple"]["denominator"] == 100.0 and by["trend_multiple"]["unit"] == "ratio"
    assert "views_per_day" not in {m["name"] for m in CP.evidence_for_video(vids[0], registry, NOW3)["metrics"]}
