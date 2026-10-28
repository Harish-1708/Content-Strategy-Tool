"""Asking for the kinds of content a brand actually makes: UGC, AI content, influencer posts, static posts, carousels, video, memes.

The first real request (2026-10-28, "Father's Day gifting ideas for dads") returned mostly gift-guide articles, kept TikTok searches after
TikTok had been dropped, and showed the tools that were allowed rather than the ones in the plan. A request can now list the formats it
wants; the plan goes looking for them, each item is labelled with its format, and an item in a wanted format scores a little higher."""
import pytest

from contentintel import ideabank as IB
from contentintel import planner as PL
from contentintel import sheets
from contentintel.store import Store

from fake_browser import FakeClaude
from test_collectors import NOW
from test_concept_scoring import entry
from test_ideabank import S, bank_with
from test_planner import ALL, clean, make, raw_plan


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


# ---- understanding what a person types -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("ugc", ["ugc"]), ("UGC", ["ugc"]), ("  Ugc  ", ["ugc"]), ("UGC, AI content", ["ugc", "ai"]), ("ugc and ai content and posts", ["ugc", "ai", "static"]),
    ("UGC & AI & Reels", ["ugc", "ai", "video"]), ("ugc/ai/memes", ["ugc", "ai", "meme"]), ("ugc; carousels", ["ugc", "carousel"]),
    ("user-generated, creators", ["ugc", "influencer"]), ("User Generated Content", ["ugc"]), ("AI-generated", ["ai"]), ("ai generated ads", ["ai"]),
    ("posts, images, photos", ["static"]), ("reels, videos, short videos", ["video"]), ("humour, humor, trends", ["meme"]),
    ("testimonials, reviews, hauls", ["ugc"]), ("influencers", ["influencer"]), ("slides", ["carousel"]), ("static posts", ["static"]),
    ("video, ugc, video", ["video", "ugc"]), ("ugc\nai", ["ugc", "ai"]), ("", []), (None, []), ("  ,  ,", [])])
def test_what_a_person_types_is_understood(text, expected):
    assert PL.parse_formats(text) == expected


@pytest.mark.parametrize("text,expected", [("AI generated ads", ["ai"]), ("ai generated video content", ["ai"]), ("UGC style ads", ["ugc"]), ("funny memes", ["meme"]),
                                           ("instagram reels", ["video"]), ("fashion influencers", ["influencer"]), ("photo carousels", ["carousel"])])
def test_a_phrase_with_one_format_word_means_that_format(text, expected):
    assert PL.parse_formats(text) == expected


def test_pieces_with_only_punctuation_are_ignored_and_a_phrase_with_two_formats_is_not_guessed():
    assert PL.parse_formats("ai, ??, ugc, !!") == ["ai", "ugc"]
    with pytest.raises(PL.PlanError):
        PL.parse_formats("ugc ai thing")


def test_a_list_is_understood_too():
    assert PL.parse_formats(["UGC", "AI content", "posts"]) == ["ugc", "ai", "static"] and PL.parse_formats(("meme",)) == ["meme"] and PL.parse_formats([]) == []


@pytest.mark.parametrize("text", ["blogs", "ugc, podcasts", "nonsense words", "tiktok", "ai influencer ugc", "ai ugc ads"])
def test_an_unknown_word_is_an_error_that_says_what_is_understood(text):
    with pytest.raises(PL.PlanError, match="is not a content format I know. Use any of: ugc \\(UGC: real customers") as err:
        PL.parse_formats(text)
    for name in PL.FORMATS:
        assert name in str(err.value)


def test_the_tables_agree_with_each_other():
    assert set(PL.FORMAT_WORDS.values()) == set(PL.FORMATS) == set(PL.FORMAT_QUERIES)
    for name in PL.FORMATS:
        assert PL.parse_formats(name) == [name]                                  # every format can be typed as its own name
    for name, queries in PL.FORMAT_QUERIES.items():
        assert len(queries) == 2 and len(set(queries)) == 2 and all(q.strip() and "site:" not in q for q in queries), name
    assert all(word == word.lower() and word == " ".join(word.split()) for word in PL.FORMAT_WORDS)


def test_every_scored_format_a_request_can_ask_for_is_one_an_item_can_have():
    assert set(PL.FORMATS) <= set(IB.ITEM_FORMATS) and {"article", "other"} <= set(IB.ITEM_FORMATS)


# ---- searches that go looking for a format ------------------------------------------------------------------------------------------------------

def test_one_search_per_wanted_format_then_a_second_for_each():
    assert PL.format_queries(["ugc", "ai"], 10) == [PL.FORMAT_QUERIES["ugc"][0], PL.FORMAT_QUERIES["ai"][0], PL.FORMAT_QUERIES["ugc"][1], PL.FORMAT_QUERIES["ai"][1]]
    assert PL.format_queries(["ugc", "ai"], 3) == [PL.FORMAT_QUERIES["ugc"][0], PL.FORMAT_QUERIES["ai"][0], PL.FORMAT_QUERIES["ugc"][1]]
    assert PL.format_queries(["ugc"], 1) == [PL.FORMAT_QUERIES["ugc"][0]]


def test_nothing_to_search_for_gives_nothing():
    assert PL.format_queries([], 5) == [] and PL.format_queries(["ugc"], 0) == [] and PL.format_queries(["nope"], 5) == []


def test_creator_searches_cover_only_the_platforms_given():
    assert PL.creator_queries(["a", "b"], 10, ("tiktok.com",)) == ["a site:tiktok.com", "b site:tiktok.com"]
    assert PL.creator_queries(["a"], 10, ("instagram.com",)) == ["a site:instagram.com"]
    assert PL.creator_queries(["a"], 10) == ["a site:tiktok.com", "a site:instagram.com"]


def test_with_no_platform_each_creator_angle_is_searched_on_its_own():
    assert PL.creator_queries(["a", "b"], 10, ()) == ["a", "b"] and PL.creator_queries(["a", "b"], 1, ()) == ["a"] and PL.creator_queries(["a"], 0, ()) == []


# ---- a plan that follows the tools it keeps ------------------------------------------------------------------------------------------------------

def web(plan):
    return plan["tools"]["web_search"]["queries"]


def test_a_plan_without_tiktok_never_searches_tiktok_pages():
    raw = raw_plan()
    del raw["tools"]["tiktok"]
    queries = web(clean(raw)[0])
    assert not any("site:tiktok.com" in q for q in queries) and any("site:instagram.com" in q for q in queries)


def test_a_plan_without_instagram_never_searches_instagram_pages():
    raw = raw_plan()
    del raw["tools"]["instagram"]
    queries = web(clean(raw)[0])
    assert not any("site:instagram.com" in q for q in queries) and any("site:tiktok.com" in q for q in queries)


def test_a_plan_with_both_searches_both_and_one_with_neither_searches_the_angles_plainly():
    both = web(clean()[0])
    assert any("site:tiktok.com" in q for q in both) and any("site:instagram.com" in q for q in both)
    raw = raw_plan()
    del raw["tools"]["tiktok"], raw["tools"]["instagram"]
    plain = web(clean(raw)[0])
    assert "what I bought my dad haul" in plain and not any("site:" in q for q in plain)


def test_a_tool_that_was_not_allowed_does_not_bring_its_site_searches():
    plan, _ = clean(allowed=[t for t in ALL if t != "tiktok"])
    assert not any("site:tiktok.com" in q for q in web(plan))


def test_wanted_formats_add_their_searches_after_the_creator_ones_and_before_claudes():
    plan, _ = PL.clean_plan(raw_plan(), ALL, S, "x", ["ugc", "ai"])
    queries = web(plan)
    creators = [q for q in queries if "site:" in q]
    assert queries[:len(creators)] == creators
    assert queries[len(creators):len(creators) + 2] == [PL.FORMAT_QUERIES["ugc"][0], PL.FORMAT_QUERIES["ai"][0]] and plan["formats"] == ["ugc", "ai"]
    assert len(queries) <= S.plan_max_queries and len(set(queries)) == len(queries)
    seeds = [q for q in queries if any(q in pool for pool in PL.FORMAT_QUERIES.values())]
    assert seeds == [PL.FORMAT_QUERIES["ugc"][0], PL.FORMAT_QUERIES["ai"][0]]                  # one search for each wanted format, no more


def test_a_plan_with_formats_still_keeps_one_of_claudes_own_searches():
    plan, _ = PL.clean_plan(raw_plan(), ALL, S, "x", list(PL.FORMATS))
    queries = web(plan)
    assert any(q in queries for q in ("best fathers day gifts 2026", "comfortable loungewear gift guide")) and len(queries) <= S.plan_max_queries


def test_a_plan_without_formats_is_as_before_and_says_so():
    plan, _ = clean()
    assert plan["formats"] == [] and not any(q in web(plan) for qs in PL.FORMAT_QUERIES.values() for q in qs)


def test_formats_do_not_make_a_web_search_the_person_did_not_allow():
    plan, _ = PL.clean_plan(raw_plan(), ["meta_ads"], S, "x", ["ugc"])
    assert "web_search" not in plan["tools"]


# ---- requests carry their formats --------------------------------------------------------------------------------------------------------------

def test_a_request_stores_its_formats_and_tells_claude_what_they_mean(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude, formats="UGC, AI content and posts")
    assert req["formats"] == ["ugc", "ai", "static"] and req["plan"]["formats"] == ["ugc", "ai", "static"] and store.read_request("swoveralls", "RQ-0001")["formats"] == ["ugc", "ai", "static"]
    asked = claude.json_calls[0][1]
    assert "Formats wanted: ugc = UGC: real customers" in asked and "ai = AI-made or AI-assisted content" in asked and "static = static posts" in asked
    assert "If the request lists formats wanted" in claude.json_calls[0][0] and "for UGC, hauls, try-ons" in claude.json_calls[0][0]


def test_a_request_with_no_formats_stores_an_empty_list_and_does_not_mention_formats(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude)
    assert req["formats"] == [] and "Formats wanted" not in claude.json_calls[0][1]


def test_an_unknown_format_is_refused_before_claude_is_asked_and_before_a_call_is_spent(store, swov):
    claude = FakeClaude()
    with pytest.raises(PL.PlanError, match="'blogs' is not a content format I know"):
        make(store, swov, claude, formats="ugc, blogs")
    assert claude.json_calls == [] and store.list_requests("swoveralls") == []
    assert store.read_claude_usage() == {}


def test_the_plan_shows_the_formats_wanted_in_the_log_and_the_summary(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude, formats="ugc, ai")
    assert "  Formats wanted: ugc, ai" in PL.render_plan(req)
    assert PL.plan_summary(req["plan"]).startswith("formats: ugc, ai | ads: ")
    plain = {**req, "formats": []}
    assert not any("Formats wanted" in line for line in PL.render_plan(plain)) and not PL.plan_summary({**req["plan"], "formats": []}).startswith("formats")


def test_changing_a_plan_keeps_its_formats_and_their_searches(store, swov):
    claude = FakeClaude()
    revised = raw_plan()
    del revised["tools"]["tiktok"]
    claude.json_answers = [raw_plan(), revised]
    req, budget = make(store, swov, claude, formats="ugc")
    req = PL.revise_request(store, swov, req, claude, budget, S, changes="drop TikTok", now=NOW, context="x")
    assert req["formats"] == ["ugc"] and req["plan"]["formats"] == ["ugc"] and "Formats wanted: ugc" in claude.json_calls[1][1]
    assert PL.FORMAT_QUERIES["ugc"][0] in web(req["plan"]) and not any("site:tiktok.com" in q for q in web(req["plan"]))


# ---- labelling and valuing what is found --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("given,kept", [("ugc", "ugc"), ("UGC", "ugc"), (" Video ", "video"), ("ai", "ai"), ("article", "article"), ("other", "other"),
                                         ("podcast", ""), ("", ""), (None, ""), (5, "")])
def test_an_items_format_is_read_strictly(given, kept):
    assert IB.clean_scores({"items": [entry(format=given)]}, {"IB-0001"})["IB-0001"]["format"] == kept


def test_an_answer_without_a_format_is_still_usable():
    got = entry()
    got.pop("format", None)
    assert IB.clean_scores({"items": [got]}, {"IB-0001"})["IB-0001"]["format"] == ""


def scored(request, *answers):
    bank = bank_with("A", "B", "C")
    claude = FakeClaude()
    claude.json_answers = [{"items": list(answers)}]
    return bank, claude, request


def run(bank, claude, request, store, swov):
    return IB.score_items(bank, list(bank["items"]), claude, swov, store, S, IB.ClaudeBudget(store, NOW, 50), NOW, request)


def test_a_scored_item_keeps_its_format_and_the_instructions_define_every_format(store, swov):
    bank, claude, request = scored(None, entry("IB-0001", format="ugc"))
    run(bank, claude, request, store, swov)
    assert bank["items"]["IB-0001"]["format"] == "ugc"
    for name in IB.ITEM_FORMATS:
        assert f"\\\"{name}\\\"" in repr(IB.SCORE_SYSTEM) or f"'{name}'" in IB.SCORE_SYSTEM or name in IB.SCORE_SYSTEM, name
    assert "never stretch an item into a format it is not" in IB.SCORE_SYSTEM and "'ugc' made by real customers" in IB.SCORE_SYSTEM


def test_an_item_in_a_wanted_format_scores_a_little_higher(store, swov):
    request = {"id": "RQ-0001", "text": "x", "formats": ["ugc", "ai"]}
    bank, claude, _ = scored(request, entry("IB-0001", fit=70, idea=70, format="ugc"), entry("IB-0002", fit=70, idea=70, format="static"),
                             entry("IB-0003", fit=70, idea=70, format="ai"))
    run(bank, claude, request, store, swov)
    scores = {i: it["score"] for i, it in bank["items"].items()}
    assert scores == {"IB-0001": 70 + IB.FORMAT_BOOST, "IB-0002": 70, "IB-0003": 70 + IB.FORMAT_BOOST}
    assert bank["items"]["IB-0001"]["relevance"] == 78


def test_the_boost_never_goes_past_100(store, swov):
    request = {"id": "RQ-0001", "text": "x", "formats": ["ugc"]}
    bank, claude, _ = scored(request, entry("IB-0001", fit=100, idea=98, format="ugc"))
    run(bank, claude, request, store, swov)
    assert bank["items"]["IB-0001"]["score"] == 100


def test_there_is_no_boost_without_a_request_or_without_wanted_formats_or_when_the_format_is_unknown(store, swov):
    for request in (None, {"id": "RQ-0001", "text": "x", "formats": []}, {"id": "RQ-0001", "text": "x"}):
        bank, claude, _ = scored(request, entry("IB-0001", fit=70, idea=70, format="ugc"))
        run(bank, claude, request, store, swov)
        assert bank["items"]["IB-0001"]["score"] == 70
    request = {"id": "RQ-0001", "text": "x", "formats": ["ugc"]}
    bank, claude, _ = scored(request, entry("IB-0001", fit=70, idea=70, format="podcast"))
    run(bank, claude, request, store, swov)
    assert bank["items"]["IB-0001"]["score"] == 70


def test_a_generic_or_unrelated_item_stays_below_the_minimum_even_in_a_wanted_format(store, swov):
    request = {"id": "RQ-0001", "text": "x", "formats": ["ugc"]}
    bank, claude, _ = scored(request, entry("IB-0001", fit=100, idea=100, generic=True, format="ugc"), entry("IB-0002", fit=100, idea=100, category="other", format="ugc"))
    run(bank, claude, request, store, swov)
    assert max(it["score"] for it in list(bank["items"].values())[:2]) < S.min_relevance


def test_the_request_and_its_formats_are_put_to_claude_when_scoring(store, swov):
    request = {"id": "RQ-0001", "text": "Father's Day", "formats": ["ugc", "ai"], "occasion": "Father's Day"}
    bank, claude, _ = scored(request, entry("IB-0001"))
    run(bank, claude, request, store, swov)
    assert "Request: Father's Day (occasion: Father's Day)\nFormats wanted: ugc, ai" in claude.json_calls[0][1]


def test_how_an_ad_was_made_is_read_from_the_details_and_shown_to_claude(store, swov):
    assert IB._media_format("Facebook/Instagram | CTA: Shop now | format: IMAGE | active | ends 2026-10-27") == "IMAGE"
    assert IB._media_format("format: video") == "VIDEO" and IB._media_format("format: CAROUSEL | x") == "CAROUSEL"
    assert IB._media_format("no label here") == "" and IB._media_format(None) == ""
    bank = {"next": 1, "items": {}}
    from test_ideabank import record, row
    IB.intake_collected(bank, [record([row("meta_ads", "9", handle="Acme", title="t", text="long enough text for an ad to be compared " * 2,
                                           details="CTA: Shop now | format: VIDEO | active")])], S, NOW)
    item = bank["items"]["IB-0001"]
    assert item["media_format"] == "VIDEO" and "ad format VIDEO" in IB._item_line(item)
    assert "ad format" not in IB._item_line({**item, "media_format": ""})


def test_the_sheet_shows_each_items_format():
    from test_ideabank import kept_bank
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["format"] = "ugc"
    assert IB.sheet_rows(bank)["IB-0001"]["format"] == "ugc" and IB.sheet_rows(kept_bank("B"))["IB-0001"]["format"] == ""
    assert IB.IDEABANK_COLUMNS.index("format") == IB.IDEABANK_COLUMNS.index("idea") + 1


def test_an_old_bank_gets_the_new_fields():
    from test_ideabank import kept_bank
    bank = kept_bank("A")
    for key in ("format", "media_format"):
        bank["items"]["IB-0001"].pop(key, None)
    IB.normalise_bank(bank)
    assert bank["items"]["IB-0001"]["format"] == "" and bank["items"]["IB-0001"]["media_format"] == ""


# ---- the Requests tab ------------------------------------------------------------------------------------------------------------------------------

def test_the_requests_tab_shows_the_formats_and_the_tools_actually_in_the_plan():
    request = {"id": "RQ-0001", "created": NOW, "status": "done", "text": "t", "region": "worldwide", "formats": ["ugc", "ai"],
               "allowed": ["meta_ads", "instagram", "tiktok", "web_search", "deep_research"],
               "plan": {"tools": {"meta_ads": {"keywords": ["x"]}, "instagram": {"hashtags": ["y"]}, "web_search": {"queries": ["q"]}}, "formats": ["ugc", "ai"]},
               "run": {"items_added": ["IB-0001"], "notes": ["n"]}}
    row = sheets.request_sheet_rows([request])["RQ-0001"]
    assert row["formats"] == "ugc, ai" and row["tools"] == "meta_ads, instagram, web_search"            # TikTok was dropped from the plan
    assert "formats" in sheets.REQUEST_COLUMNS and sheets.REQUEST_COLUMNS.index("formats") == sheets.REQUEST_COLUMNS.index("region") + 1


def test_a_request_with_no_plan_falls_back_to_the_tools_allowed_and_has_no_formats():
    row = sheets.request_sheet_rows([{"id": "RQ-0002", "allowed": ["web_search"], "plan": {}}])["RQ-0002"]
    assert row["tools"] == "web_search" and row["formats"] == ""


# ---- the workflow and the README ----------------------------------------------------------------------------------------------------------------------

def test_the_request_workflow_takes_formats_and_passes_them_safely():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/request.yml", encoding="utf-8"))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["formats"]["default"] == "" and inputs["formats"]["required"] is False and "ugc" in inputs["formats"]["description"] and "ai" in inputs["formats"]["description"]
    step = next(s for s in wf["jobs"]["request"]["steps"] if s.get("name") == "Plan or run the request")
    assert step["env"]["FORMATS"] == "${{ inputs.formats }}" and "${{ inputs." not in step["run"]
    assert 'if [ -n "$FORMATS" ]; then args+=(--formats "$FORMATS"); fi' in step["run"]


def test_the_readme_explains_formats():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("Asking for the kinds of content", "UGC", "AI content", "formats", "scores 8 points higher", "label"):
        assert needle in text, needle
