"""The shared evidence record: built from what was collected, honest about what it is, and checked."""
import copy
import json

import jsonschema
import pytest

from contentintel import evidence as E
from contentintel.store import Store

from conftest import ROOT
from stage1_helpers import NOW, at, shipped

REG = shipped()
CAPTURED = at(NOW, days=-3)


def item(**kw):
    base = {"item_id": "tiktok:1", "source": "tiktok", "handle": "@acme", "title": "", "text": "Nobody tells you that sweatpants overalls are this comfy",
            "url": "https://www.tiktok.com/@acme/video/1?utm_source=share", "posted_at": "2026-10-01", "likes": 120, "views": 5000, "comments": 7,
            "media_url": "https://cdn.example.com/c.jpg"}
    base.update(kw)
    return base


def batch(items, source="tiktok", at_=CAPTURED, **kw):
    rec = {"at": at_, "run": "r1", "source": source, "key": "k", "seeds": "#overalls", "via": "browser", "from_site_data": len(items), "from_links": 0, "items": items}
    rec.update(kw)
    return rec


def one(records, source=None):
    out = E.evidence_from_collected(records, REG, NOW)
    return out[0] if source is None else next(r for r in out if r["origin"]["source"] == source)


def valid(rec):
    return E.validate_evidence(rec, REG, NOW)


# ---- building ---------------------------------------------------------------------------------------------------------------

def test_a_collected_post_becomes_a_complete_valid_record():
    rec = one([batch([item()])])
    assert valid(rec) == []
    o, i, c, q, g = rec["origin"], rec["identity"], rec["content"], rec["quality"], rec["governance"]
    assert (o["source"], o["platform"], o["route"], o["access_tier"], o["access_type"]) == ("tiktok", "tiktok", "browser", "session_based", "site_data")
    assert o["canonical_url"] == "https://www.tiktok.com/@acme/video/1" and o["url"].endswith("utm_source=share")      # the tracking code is dropped from the identity only
    assert o["captured_at"] == CAPTURED and o["account"] == "@acme" and o["collector"] == "tiktok.browser" and o["found_via"] == "#overalls"
    assert i["item_id"] == "tiktok:1" and i["creator"] == "@acme" and i["first_seen"] == i["last_seen"] == CAPTURED and i["times_seen"] == 1
    assert rec["evidence_id"].startswith("ev1-") and len(i["content_hash"]) == 20 and len(i["url_hash"]) == 16 and i["duplicate_cluster"] == ""
    assert c["media"] == [{"kind": "image", "url": "https://cdn.example.com/c.jpg", "status": "linked"}] and c["media_status"] == "linked"
    assert q["completeness"] == {"status": "observed", "observed": ["text", "url", "posted_at", "media_url", "likes", "views"], "missing": []}
    assert q["stale"] is False and q["age_days"] == 3.0 and q["parse_confidence"] == "high"
    assert g["data_class"] == "public" and g["review_status"] == "unreviewed" and "never reproduced" in g["licence_note"] and g["provenance"] == "tiktok via browser"
    assert rec["analysis"]["formula"] == "" and rec["analysis"]["human_decision"] == "" and rec["content"]["transcript"] == ""


def test_the_record_meets_the_published_schema_and_the_schema_is_the_one_in_the_code():
    schema = json.loads((ROOT / "contracts" / "evidence.v1.schema.json").read_text(encoding="utf-8"))
    assert schema == json.loads(json.dumps(E.EVIDENCE_SCHEMA))
    jsonschema.validate(one([batch([item()])]), schema)
    broken = one([batch([item()])])
    broken["origin"]["access_tier"] = "magic"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)


def test_metrics_say_what_they_measure_and_which_are_only_proxies():
    ad = item(item_id="meta_ads:9", source="meta_ads", url="https://www.facebook.com/ads/library/?id=9", days_running=40, variants=6, page_likes=900,
              shares=None, saves=0)
    rec = one([batch([ad], source="meta_ads")])
    by = {m["name"]: m for m in rec["metrics"]}
    assert set(by) == {"likes", "views", "comments", "days_running", "variant_count", "page_followers", "saves"}      # None is not a metric; zero is
    assert by["days_running"]["kind"] == "proxy" and by["days_running"]["unit"] == "days" and by["variant_count"]["kind"] == "proxy"
    assert by["likes"]["kind"] == "measured" and by["likes"]["value"] == 120 and by["saves"]["value"] == 0
    for m in rec["metrics"]:
        assert m["denominator"] is None and m["window"] == "cumulative at capture" and m["platform"] == "meta" and m["source"] == "meta_ads"
        assert m["captured_at"] == CAPTURED
    assert valid(rec) == []


def test_an_item_known_only_from_a_link_is_labelled_links_only():
    rec = one([batch([{"item_id": "tiktok:2", "source": "tiktok", "url": "https://www.tiktok.com/@a/video/2"}])])
    assert rec["origin"]["access_type"] == "page_links" and rec["quality"]["completeness"]["status"] == "links_only"
    assert rec["quality"]["parse_confidence"] == "low" and valid(rec) == []


def test_an_item_with_some_fields_missing_is_partial_not_observed():
    rec = one([batch([item(likes=None, views=None, media_url="")])])
    assert rec["quality"]["completeness"]["status"] == "partial"
    assert rec["quality"]["completeness"]["missing"] == ["media_url", "likes", "views"] and valid(rec) == []


def test_a_deep_research_report_is_report_text_even_when_it_has_only_text():
    rec = one([batch([{"item_id": "deep_research:1", "source": "deep_research", "text": "A long report " * 20, "title": "Who are the competitors?",
                       "url": "data/swoveralls/research/deep/r.md"}], source="deep_research")])
    assert rec["origin"]["access_type"] == "report_text" and rec["origin"]["access_tier"] == "session_based" and valid(rec) == []


def test_a_search_result_is_always_a_snippet_and_never_the_page():
    searches = [{"at": CAPTURED, "provider": "tavily", "query": "sweatpants overalls", "kind": "web",
                 "results": [{"rank": 1, "title": "Best overalls", "url": "https://blog.example.com/overalls/?utm_campaign=x#top", "snippet": "A short extract", "date": "2026-09-30"}]}]
    rec = E.evidence_from_searches(searches, REG, NOW)[0]
    assert rec["origin"]["access_type"] == "search_snippet" and rec["quality"]["completeness"]["status"] == "snippet_only"
    assert rec["quality"]["completeness"]["missing"] == ["page_text"] and rec["origin"]["route"] == "tavily" and rec["origin"]["found_via"] == "sweatpants overalls"
    assert rec["origin"]["canonical_url"] == "https://blog.example.com/overalls" and rec["origin"]["published"] == "2026-09-30"
    assert rec["origin"]["access_tier"] == "third_party_service" and valid(rec) == []


def test_the_same_page_found_by_two_searches_is_one_record_with_both_sightings():
    r = lambda t, p, rank: {"at": t, "provider": p, "query": "q", "kind": "web", "results": [{"rank": rank, "title": "T", "url": "https://x.example.com/a", "snippet": "s"}]}
    out = E.evidence_from_searches([r(at(NOW, days=-2), "tavily", 1), r(at(NOW, days=-1), "serper_dev", 3)], REG, NOW)
    assert len(out) == 1 and out[0]["identity"]["times_seen"] == 2
    assert out[0]["identity"]["first_seen"] == at(NOW, days=-2) and out[0]["identity"]["last_seen"] == at(NOW, days=-1) and out[0]["origin"]["route"] == "serper_dev"


def test_records_saved_before_provenance_existed_still_work_and_new_provenance_is_preferred():
    legacy = one([batch([item()])])
    assert legacy["origin"]["access_tier"] == "session_based"                                    # read from sources.yaml for the route
    prov = E.provenance(REG, "tiktok", "apify", CAPTURED, run="r9", health_status="healthy")
    rec = one([batch([item()], via="apify", provenance=prov)])
    assert rec["origin"]["route"] == "apify" and rec["origin"]["access_tier"] == "third_party_service" and rec["quality"]["source_health"] == "healthy"
    assert prov == {"schema": 1, "route": "apify", "access_tier": "third_party_service", "access_type": "site_data", "collector": "tiktok.apify",
                    "captured_at": CAPTURED, "run": "r9", "source_health": "healthy"}


def test_what_was_recorded_at_collection_beats_what_is_declared_today():
    prov = E.provenance(REG, "tiktok", "browser", CAPTURED)
    prov["access_tier"] = "licensed_vendor"                          # the route was reached differently when this batch was collected
    rec = one([batch([item()], provenance=prov)])
    assert rec["origin"]["access_tier"] == "licensed_vendor" and rec["origin"]["access_type"] == "site_data"
    assert one([batch([item()])])["origin"]["access_tier"] == "session_based"


def test_provenance_of_two_merged_routes_and_of_an_undeclared_one():
    merged = E.provenance(REG, "meta_ads", "browser+apify", NOW)
    assert merged["route"] == "browser+apify" and merged["access_tier"] == "public_polite"
    odd = E.provenance(REG, "myspace", "carrier", NOW)
    assert odd["access_tier"] == "unknown"                                                       # never raises: collection must not stop for a label
    rec = one([batch([item()], provenance=odd)])
    assert any("access_tier" in p for p in valid(rec))                                           # but validation says it is not declared


def test_merged_routes_and_partial_runs_are_named_as_limitations():
    rec = one([batch([item()], via="browser+apify", partial=True)])
    assert "merged from two routes" in rec["quality"]["limitations"] and "the run stopped before every page was read" in rec["quality"]["limitations"]
    assert "a dummy account can be warned" in rec["quality"]["limitations"]


# ---- sightings --------------------------------------------------------------------------------------------------------------

def test_repeated_sightings_keep_first_and_last_and_the_newest_numbers():
    first = batch([item(likes=10, text="Original wording of the ad that goes on a bit")], at_=at(NOW, days=-10))
    second = batch([item(likes=99, views=None, text="")], at_=at(NOW, days=-1))
    rec = one([second, first])                                                                   # order of the file does not matter
    assert rec["identity"]["first_seen"] == at(NOW, days=-10) and rec["identity"]["last_seen"] == at(NOW, days=-1) and rec["identity"]["times_seen"] == 2
    by = {m["name"]: m["value"] for m in rec["metrics"]}
    assert by["likes"] == 99 and by["views"] == 5000                                             # a blank later reading never erases an earlier one
    assert rec["content"]["text"].startswith("Original wording") and rec["origin"]["captured_at"] == at(NOW, days=-1)


# ---- duplicates -------------------------------------------------------------------------------------------------------------

def test_the_same_address_or_the_same_wording_forms_a_duplicate_cluster():
    a = item(item_id="tiktok:1", url="https://www.tiktok.com/@a/video/1", text="Totally different words about overalls number one here")
    b = item(item_id="instagram:5", source="instagram", url="https://www.tiktok.com/@a/video/1?utm_source=x", text="Another wording entirely, about socks and shoes")
    c = item(item_id="tiktok:3", url="https://www.tiktok.com/@c/video/3", text="Same long wording that two different posts happen to share")
    d = item(item_id="tiktok:4", url="https://www.tiktok.com/@d/video/4", text="Same long wording that two different posts happen to share")
    e = item(item_id="tiktok:5", url="https://www.tiktok.com/@e/video/5", text="A completely unique caption that nobody else has written")
    out = {r["identity"]["item_id"]: r for r in E.assign_clusters(E.evidence_from_collected([batch([a, c, d, e]), batch([b], source="instagram")], REG, NOW))}
    cl = lambda k: out[k]["identity"]["duplicate_cluster"]
    assert cl("tiktok:1") == cl("instagram:5") != "" and cl("tiktok:3") == cl("tiktok:4") != "" and cl("tiktok:1") != cl("tiktok:3")
    assert cl("tiktok:5") == ""
    assert cl("tiktok:1") == min(out["tiktok:1"]["evidence_id"], out["instagram:5"]["evidence_id"])         # the smallest id names the cluster


def test_short_wording_is_never_used_to_call_two_things_duplicates():
    out = E.assign_clusters(E.evidence_from_collected([batch([item(item_id="tiktok:1", url="https://t.example.com/1", text="New drop", likes=1),
                                                              item(item_id="tiktok:2", url="https://t.example.com/2", text="New drop", likes=2)])], REG, NOW))
    assert [r["identity"]["duplicate_cluster"] for r in out] == ["", ""]


def test_invisible_characters_do_not_make_copies_look_different_or_stay_in_the_record():
    clean = item(item_id="tiktok:1", url="https://t.example.com/1", text="Exactly the same sentence in two posts, one with hidden characters")
    sneaky = item(item_id="tiktok:2", url="https://t.example.com/2", text="Exactly the same sentence\u200b in two posts,\u200d one with hidden\ufeff characters")
    out = E.assign_clusters(E.evidence_from_collected([batch([clean, sneaky])], REG, NOW))
    assert out[0]["identity"]["content_hash"] == out[1]["identity"]["content_hash"] and out[0]["identity"]["duplicate_cluster"] != ""
    assert "\u200b" not in out[1]["content"]["text"] and "\ufeff" not in out[1]["content"]["text"]


# ---- age --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("days, stale", [(13, False), (14, False), (15, True), (60, True)])
def test_old_data_is_labelled_stale_with_its_age(days, stale):
    rec = one([batch([item()], at_=at(NOW, days=-days))])
    assert rec["quality"]["stale"] is stale and rec["quality"]["age_days"] == float(days) and valid(rec) == []


def test_each_source_has_its_own_freshness_limit():
    searches = [{"at": at(NOW, days=-10), "provider": "tavily", "query": "q", "kind": "web", "results": [{"rank": 1, "title": "T", "url": "https://a.example.com/x", "snippet": "s"}]}]
    assert E.evidence_from_searches(searches, REG, NOW)[0]["quality"]["stale"] is False
    reg = copy.deepcopy(REG)
    object.__setattr__(reg.sources["web_search"], "stale_after_days", 7)
    assert E.evidence_from_searches(searches, reg, NOW)[0]["quality"]["stale"] is True


# ---- the honesty rules are checked ------------------------------------------------------------------------------------------

def good():
    return one([batch([item(days_running=5)])])


@pytest.mark.parametrize("edit, fragment", [
    (lambda r: r.update(schema_version=2), "schema_version"),
    (lambda r: r.update(evidence_id="ev1-xyz"), "evidence_id"),
    (lambda r: r.pop("metrics"), "missing section 'metrics'"),
    (lambda r: r["origin"].update(access_tier="magic"), "not a known tier"),
    (lambda r: r["origin"].update(access_type="vibes"), "not a known access type"),
    (lambda r: r["origin"].update(route=""), "origin.route is empty"),
    (lambda r: r["identity"].update(first_seen=at(NOW, days=1), last_seen=NOW), "first_seen is after last_seen"),
    (lambda r: r["quality"]["completeness"].update(status="complete"), "is not allowed"),
    (lambda r: r["origin"].update(access_type="search_snippet"), "snippet must be labelled snippet_only"),
    (lambda r: r["origin"].update(access_type="page_links"), "known only from links must be labelled links_only"),
    (lambda r: r["content"]["media"][0].update(status="cover_only", kind="video"), "cover picture must be an image"),
    (lambda r: r["content"]["media"][0].update(status="saved", kind="unknown"), "must say whether it is a picture or a video"),
    (lambda r: r["content"]["media"][0].update(status="vanished"), "a media entry has status"),
    (lambda r: r["content"].update(media_status="vanished"), "media_status"),
    (lambda r: r["quality"].update(parse_confidence="certain"), "parse_confidence"),
    (lambda r: r["metrics"][0].pop("denominator"), "lacks a denominator"),
    (lambda r: r["metrics"][0].update(value="12"), "no numeric value"),
    (lambda r: r["metrics"][0].update(value=True), "no numeric value"),
    (lambda r: r["metrics"][0].update(unit=""), "lacks unit"),
    (lambda r: r["metrics"][0].update(kind="guess"), "measured or proxy"),
    (lambda r: next(m for m in r["metrics"] if m["name"] == "days_running").update(kind="measured"), "is a proxy and must be labelled one"),
    (lambda r: r["metrics"][0].update(unit="percent"), "needs its denominator"),
    (lambda r: r["governance"].update(review_status="maybe"), "review_status"),
    (lambda r: r["quality"].update(stale=True), "does not match the age"),
])
def test_each_way_to_break_the_contract_or_its_honesty_rules_is_caught(edit, fragment):
    rec = copy.deepcopy(good())
    assert valid(rec) == []
    edit(rec)
    assert any(fragment in p for p in valid(rec)), valid(rec)


def test_a_non_object_and_a_non_list_of_metrics_are_refused():
    assert E.validate_evidence("x") == ["the record is not an object"]
    rec = copy.deepcopy(good())
    rec["metrics"] = {}
    assert "metrics must be a list" in valid(rec)


# ---- a whole brand ----------------------------------------------------------------------------------------------------------

def test_a_brands_saved_data_becomes_one_valid_set_with_a_summary_and_an_export(tmp_path):
    store = Store(tmp_path / "data")
    store.append_collected("swoveralls", batch([item(), item(item_id="tiktok:2", url="https://www.tiktok.com/@a/video/2", text="Second post, quite different words entirely here")]))
    store.append_collected("swoveralls", batch([{"item_id": "meta_ads:7", "source": "meta_ads", "url": "https://www.facebook.com/ads/library/?id=7", "title": "Ad", "text": "Ad text for a sale on overalls today",
                                                 "days_running": 12, "posted_at": "2026-09-01", "media_url": "https://cdn.example.com/a.jpg"}], source="meta_ads"))
    store.append_searches("swoveralls", [{"at": CAPTURED, "provider": "tavily", "query": "q", "kind": "web",
                                          "results": [{"rank": 1, "title": "T", "url": "https://blog.example.com/p", "snippet": "snippet text here and more"}]}])
    records = E.build_all(store, "swoveralls", REG, NOW)
    assert len(records) == 4 and all(valid(r) == [] for r in records)
    stats = E.summary(records)
    assert stats["total"] == 4 and stats["by_source"] == {"meta_ads": 1, "tiktok": 2, "web_search": 1}
    assert stats["by_access_type"] == {"search_snippet": 1, "site_data": 3} and stats["by_completeness"]["snippet_only"] == 1 and stats["stale"] == 0
    lines = E.export_jsonl(records).splitlines()
    assert len(lines) == 4 and all(json.loads(line)["schema_version"] == 1 for line in lines) and E.export_jsonl([]) == ""
    assert E.build_all(store, "duderobe", REG, NOW) == []                                         # a brand with nothing saved has no records, and no folder is made for it
    assert not (tmp_path / "data" / "duderobe").exists()


def test_ids_are_stable_across_runs_and_differ_between_items():
    a1, a2 = one([batch([item()])]), one([batch([item()], at_=at(NOW, days=-1))])
    assert a1["evidence_id"] == a2["evidence_id"]                                                 # the same post is the same evidence on any day
    assert one([batch([item(url="https://www.tiktok.com/@acme/video/2", item_id="tiktok:2")])])["evidence_id"] != a1["evidence_id"]
    assert E.evidence_id("tiktok", "x") != E.evidence_id("instagram", "x")


def test_the_content_hash_ignores_hidden_characters_and_case_but_not_words():
    plain = E.content_hash("Big sale", "Exactly the same sentence about overalls")
    assert plain and plain == E.content_hash("Big\u200b sale", "Exactly the same\ufeff sentence about OVERALLS")
    assert plain != E.content_hash("Big sale", "A different sentence about overalls entirely")
    assert E.content_hash("", "too short") == ""


def test_the_summary_counts_stale_records_and_duplicates():
    fresh = one([batch([item(item_id="tiktok:1")])])
    old = one([batch([item(item_id="tiktok:2", url="https://www.tiktok.com/@a/video/2", text="Another long enough wording about socks and shoes")], at_=at(NOW, days=-40))])
    a, b = fresh, copy.deepcopy(fresh)
    b["evidence_id"] = "ev1-" + "0" * 16
    E.assign_clusters([a, b, old])
    stats = E.summary([a, b, old])
    assert stats["stale"] == 1 and stats["in_duplicate_clusters"] == 2 and stats["total"] == 3
