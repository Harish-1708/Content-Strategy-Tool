"""Request mode: the plan Claude proposes is cleaned by code, can be revised, and runs through the tools the system already has."""
import copy
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import ideabank as IB
from contentintel import planner as PL
from contentintel import research as R
from contentintel.providers import ProviderError
from contentintel.store import Store

from fake_browser import FakeClaude
from test_collectors import CREDS, NOW, statuses, world

S = IB.IdeaBankSettings()
ALL = list(PL.TOOLS)


def raw_plan(**over):
    plan = {"interpretation": "Gift ideas for dads who like comfortable clothes", "occasion": "Father's Day", "audience": "adult children buying for dad",
            "goal": "get gifting orders", "tools": {
                "meta_ads": {"why": "see who advertises gifting", "keywords": ["fathers day gift", "comfy gifts for dad"]},
                "instagram": {"why": "gift hauls", "hashtags": ["#FathersDayGift", "giftsfordad"]},
                "tiktok": {"why": "what I bought dad", "hashtags": ["fathersdayideas"]},
                "web_search": {"why": "gift guides", "queries": ["best fathers day gifts 2026", "comfortable loungewear gift guide"]},
                "deep_research": {"why": "the broad picture", "questions": ["How is Father's Day gifting marketed on social media?"]}},
            "creator_angles": ["what I bought my dad haul", "dad reacts to gift"], "notes": "Check the region"}
    plan.update(over)
    return plan


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def clean(raw=None, allowed=ALL, settings=S, text="Father's Day gifting ideas"):
    return PL.clean_plan(raw if raw is not None else raw_plan(), allowed, settings, text)


# ---- cleaning a plan ----------------------------------------------------------------------------------------------------------

def test_a_good_plan_is_kept_in_full():
    plan, notes = clean()
    assert notes == [] and set(plan["tools"]) == set(ALL)
    assert plan["tools"]["meta_ads"]["keywords"] == ["fathers day gift", "comfy gifts for dad"] and plan["tools"]["meta_ads"]["why"] == "see who advertises gifting"
    assert (plan["interpretation"], plan["occasion"], plan["audience"], plan["goal"], plan["notes"]) == (
        "Gift ideas for dads who like comfortable clothes", "Father's Day", "adult children buying for dad", "get gifting orders", "Check the region")
    assert plan["tools"]["deep_research"]["questions"] == ["How is Father's Day gifting marketed on social media?"]


def test_hashtags_are_cleaned_to_plain_lower_case_words_without_duplicates():
    plan, _ = clean(raw_plan(tools={"instagram": {"hashtags": ["#Fathers Day!", "fathersday", "#FATHERSDAY", "gift-guide", "", 5, "#"]}}))
    assert plan["tools"]["instagram"]["hashtags"] == ["fathersday", "giftguide"]


def test_a_tool_you_did_not_allow_is_left_out_and_said_so():
    plan, notes = clean(allowed=["meta_ads", "web_search"])
    assert set(plan["tools"]) == {"meta_ads", "web_search"}
    assert "left out instagram: you did not allow it" in notes and "left out tiktok: you did not allow it" in notes and "left out deep_research: you did not allow it" in notes
    assert plan["dropped"] == notes


def test_a_tool_the_system_does_not_have_is_left_out_and_said_so():
    raw = raw_plan()
    raw["tools"]["pinterest"] = {"why": "pins", "hashtags": ["x"]}
    assert "left out 'pinterest': not a tool this system has" in clean(raw)[1]


def test_the_limits_in_the_settings_trim_the_plan():
    raw = raw_plan()
    raw["tools"]["meta_ads"]["keywords"] = [f"kw {n}" for n in range(20)]
    raw["tools"]["instagram"]["hashtags"] = [f"tag{n}" for n in range(20)]
    raw["tools"]["deep_research"]["questions"] = [f"q{n}" for n in range(9)]
    plan, _ = clean(raw, settings=replace(S, plan_max_keywords=3, plan_max_hashtags=2, plan_max_questions=1))
    assert len(plan["tools"]["meta_ads"]["keywords"]) == 3 and len(plan["tools"]["instagram"]["hashtags"]) == 2 and len(plan["tools"]["deep_research"]["questions"]) == 1


def test_only_ad_library_addresses_survive_as_pages():
    raw = raw_plan()
    raw["tools"]["meta_ads"]["pages"] = ["https://www.facebook.com/ads/library/?view_all_page_id=1", "https://evil.example/x", "ftp://www.facebook.com/ads/library/", 5]
    assert clean(raw)[0]["tools"]["meta_ads"]["pages"] == ["https://www.facebook.com/ads/library/?view_all_page_id=1"]


def test_a_tool_with_nothing_to_search_is_dropped_quietly():
    raw = raw_plan()
    raw["tools"]["meta_ads"] = {"why": "x", "keywords": []}
    raw["tools"]["tiktok"] = {"why": "x"}
    raw["tools"]["deep_research"] = {"why": "x", "questions": ["", "  "]}
    assert set(clean(raw)[0]["tools"]) == {"instagram", "web_search"}


def test_creator_searches_are_built_from_the_angles_for_both_platforms():
    assert PL.creator_queries(["dad haul", "gift reaction"], 10) == ["dad haul site:tiktok.com", "dad haul site:instagram.com",
                                                                     "gift reaction site:tiktok.com", "gift reaction site:instagram.com"]
    assert PL.creator_queries(["a", "b"], 3) == ["a site:tiktok.com", "a site:instagram.com", "b site:tiktok.com"] and PL.creator_queries([], 5) == [] and PL.creator_queries(["a"], 0) == []


def test_the_web_searches_put_creator_angles_first_then_fill_with_claudes_own():
    plan, _ = clean(settings=replace(S, plan_max_queries=4))
    assert plan["tools"]["web_search"]["queries"] == ["what I bought my dad haul site:tiktok.com", "what I bought my dad haul site:instagram.com",
                                                      "best fathers day gifts 2026", "comfortable loungewear gift guide"]


def test_creator_angles_are_limited_and_cut():
    plan, _ = clean(raw_plan(creator_angles=[f"angle number {n} " + "x" * 200 for n in range(9)] + ["angle number 0 " + "x" * 200, 5, ""]))
    assert len(plan["creator_angles"]) == 4 and all(len(a) <= 80 for a in plan["creator_angles"])


def test_web_search_is_always_run_when_allowed_even_if_claude_forgot_it():
    raw = raw_plan()
    del raw["tools"]["web_search"]
    plan, notes = clean(raw, text="Father's Day gifting ideas")
    assert plan["tools"]["web_search"]["queries"][:2] == ["what I bought my dad haul site:tiktok.com", "what I bought my dad haul site:instagram.com"]
    raw["creator_angles"] = []
    plan, notes = clean(raw, text="Father's Day gifting ideas")
    assert plan["tools"]["web_search"]["queries"] == ["Father's Day gifting ideas"] and "added the request itself as a web search" in notes[0]


def test_web_search_is_not_added_when_it_is_not_allowed():
    raw = raw_plan()
    assert "web_search" not in clean(raw, allowed=["meta_ads", "instagram"])[0]["tools"]


@pytest.mark.parametrize("raw", [None, [], "x", {}, {"tools": []}, {"tools": "x"}])
def test_a_plan_in_the_wrong_shape_is_refused(raw):
    with pytest.raises(PL.PlanError, match="not in the expected shape"):
        PL.clean_plan(raw, ALL, S, "x")


def test_a_plan_with_nothing_usable_is_refused_with_advice():
    with pytest.raises(PL.PlanError, match="no usable tool: check which tools you allowed"):
        PL.clean_plan({"tools": {"meta_ads": {"keywords": []}}}, ["meta_ads"], S, "")


def test_long_texts_are_cut_and_tidied():
    plan, _ = clean(raw_plan(interpretation="  a   b\n" + "x" * 500, notes="n" * 900))
    assert plan["interpretation"].startswith("a b xxx") and len(plan["interpretation"]) == 300 and len(plan["notes"]) == 400


# ---- describing a plan ----------------------------------------------------------------------------------------------------------

def test_the_summary_is_one_line_of_what_will_be_searched():
    plan, _ = clean()
    assert PL.plan_summary(plan) == ("ads: fathers day gift, comfy gifts for dad | instagram: #fathersdaygift, #giftsfordad | tiktok: #fathersdayideas | "
                                     "web: 6 searches | deep research: 1")
    assert PL.plan_summary({}) == "" and PL.plan_summary(None) == ""


def test_the_estimate_counts_what_the_plan_would_use():
    plan, _ = clean()
    est = PL.estimate(plan, S)
    assert est == {"web_searches": 6, "browser_pages": 5, "deep_research_reports": 1, "claude_calls_at_most": 1 + 15 + 2, "new_items_at_most": 120}


def test_the_estimate_of_claude_calls_follows_the_scoring_limits():
    plan, _ = clean()
    assert PL.estimate(plan, replace(S, max_new_per_run=8, score_batch=8, max_scoring_calls=6))["claude_calls_at_most"] == 1 + 2 + 2


def request(plan=None, **kw):
    plan = plan or clean()[0]
    base = {"id": "RQ-0001", "brand": "swoveralls", "text": "Father's Day gifting ideas", "created": NOW, "status": "planned", "tools_mode": "auto",
            "allowed": ALL, "region": "worldwide", "apify_extra": False, "changes": [], "run": {}, "plan": plan, "occasion": plan["occasion"]}
    base.update(kw)
    return base


def test_the_plan_is_shown_in_plain_words_with_what_it_will_use():
    req = request()
    text = "\n".join(PL.render_plan(req, PL.estimate(req["plan"], S)))
    for needle in ("Request RQ-0001 (swoveralls), region: worldwide", "You asked: Father's Day gifting ideas", "Understood as: Gift ideas for dads",
                   "Occasion: Father's Day", "meta_ads: see who advertises gifting", "      fathers day gift", "      #fathersdaygift", "      best fathers day gifts 2026",
                   "Creator and influencer angles: what I bought my dad haul; dad reacts to gift", "Claude's note: Check the region",
                   "It will use at most: 6 web searches, 5 browser pages, 1 Deep Research report(s), about 18 Claude calls, and add up to 120 items to the Idea Bank."):
        assert needle in text, needle


def test_a_note_about_what_was_left_out_is_shown():
    plan, _ = clean(allowed=["meta_ads"])
    assert any(line.startswith("  Note: left out") for line in PL.render_plan(request(plan)))


# ---- choosing the tools -------------------------------------------------------------------------------------------------------------

def test_auto_allows_everything_and_manual_only_what_was_ticked():
    assert PL.resolve_allowed("auto", []) == ALL and PL.resolve_allowed("auto", ["tiktok"]) == ALL
    assert PL.resolve_allowed("manual", ["tiktok", "meta_ads", "tiktok", "bogus"]) == ["meta_ads", "tiktok"]


@pytest.mark.parametrize("mode,ticked,match", [("manual", [], "ticked none"), ("manual", ["bogus"], "ticked none"), ("sometimes", [], "must be auto or manual"), (None, [], "must be auto")])
def test_a_bad_choice_of_tools_is_refused(mode, ticked, match):
    with pytest.raises(PL.PlanError, match=match):
        PL.resolve_allowed(mode, ticked)


# ---- planning -----------------------------------------------------------------------------------------------------------------------

def make(store, swov, claude, **kw):
    budget = kw.pop("budget", IB.ClaudeBudget(store, NOW, 100))
    args = dict(text="Father's Day gifting ideas for dads", mode="auto", ticked=[], region="worldwide", now=NOW, context="Brand: Swoveralls.")
    args.update(kw)
    return PL.plan_request(store, swov, claude, budget, S, **args), budget


def test_a_request_is_planned_saved_and_numbered(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan(), raw_plan()]
    first, budget = make(store, swov, claude)
    second, _ = make(store, swov, claude, budget=budget)
    assert (first["id"], second["id"]) == ("RQ-0001", "RQ-0002") and first["status"] == "planned" and first["occasion"] == "Father's Day"
    assert store.read_request("swoveralls", "RQ-0001") == first and [r["id"] for r in store.list_requests("swoveralls")] == ["RQ-0001", "RQ-0002"]
    assert (first["brand"], first["tools_mode"], first["allowed"], first["region"], first["apify_extra"], first["created"], first["changes"], first["run"]) == (
        "swoveralls", "auto", ALL, "worldwide", False, NOW, [], {}) and budget.used == 2


def test_what_claude_is_asked_includes_the_request_the_tools_the_region_and_the_brand(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    make(store, swov, claude, mode="manual", ticked=["meta_ads", "tiktok"], region="gb", text="Father's Day gifting ideas for dads")
    system, user, _ = claude.json_calls[0]
    assert "Brand context: Brand: Swoveralls." in user and "Allowed tools: meta_ads, tiktok" in user and "Region: gb" in user and "Request: Father's Day gifting ideas for dads" in user
    assert "creators" in system and "never put a country code" in system and "Do not invent facts" in system


def test_a_manual_plan_cannot_use_a_tool_that_was_not_ticked_even_if_claude_proposes_it(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude, mode="manual", ticked=["meta_ads"])
    assert set(req["plan"]["tools"]) == {"meta_ads"} and req["allowed"] == ["meta_ads"]


def test_the_apify_choice_and_region_are_kept(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude, apify_extra=True, region="au")
    assert req["apify_extra"] is True and req["region"] == "au"


@pytest.mark.parametrize("text", ["", "  ", "short", None])
def test_a_request_needs_a_sentence(store, swov, text):
    with pytest.raises(PL.PlanError, match="describe the request in a sentence or two"):
        make(store, swov, FakeClaude(), text=text)


def test_planning_needs_claude_and_some_of_the_months_calls(store, swov):
    with pytest.raises(PL.PlanError, match="needs the ANTHROPIC_API_KEY secret"):
        make(store, swov, None)
    claude = FakeClaude()
    with pytest.raises(PL.PlanError, match="monthly Claude limit .0 calls. is reached"):
        make(store, swov, claude, budget=IB.ClaudeBudget(store, NOW, 0))
    assert claude.json_calls == [] and store.list_requests("swoveralls") == []


def test_a_claude_failure_is_a_plan_error_and_saves_nothing(store, swov):
    claude = FakeClaude()
    claude.json_answers = [ProviderError("Claude: overloaded")]
    with pytest.raises(PL.PlanError, match="Claude could not make a plan: Claude: overloaded"):
        make(store, swov, claude)
    assert store.list_requests("swoveralls") == []


def test_an_unusable_plan_saves_nothing(store, swov):
    claude = FakeClaude()
    claude.json_answers = [{"tools": {}}]
    with pytest.raises(PL.PlanError, match="no usable tool"):
        make(store, swov, claude, mode="manual", ticked=["meta_ads"])
    assert store.list_requests("swoveralls") == []


def test_an_empty_plan_with_web_search_allowed_still_searches_the_request_itself(store, swov):
    claude = FakeClaude()
    claude.json_answers = [{"tools": {}}]
    req, _ = make(store, swov, claude, text="Father's Day gifting ideas for dads")
    assert req["plan"]["tools"] == {"web_search": {"why": "", "queries": ["Father's Day gifting ideas for dads"]}}


# ---- changing a plan ----------------------------------------------------------------------------------------------------------------

def planned(store, swov, **kw):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, budget = make(store, swov, claude, **kw)
    return req, budget


def test_no_changes_means_no_call_and_no_difference(store, swov):
    req, budget = planned(store, swov)
    claude = FakeClaude()
    again = PL.revise_request(store, swov, req, claude, budget, S, changes="  ", now=NOW)
    assert again == req and claude.json_calls == [] and budget.used == 1


def test_changes_are_sent_with_the_current_plan_and_the_new_plan_replaces_it(store, swov):
    req, budget = planned(store, swov)
    claude = FakeClaude()
    revised = raw_plan()
    del revised["tools"]["tiktok"]
    revised["tools"]["web_search"]["queries"] = ["gift guide for dads"]
    claude.json_answers = [revised]
    out = PL.revise_request(store, swov, req, claude, budget, S, changes="drop TikTok and add a gift guide angle", now="2026-10-21T08:00:00Z")
    user = claude.json_calls[0][1]
    assert "Change it as the person asks, keeping what they did not mention: drop TikTok and add a gift guide angle" in user and '"fathersdayideas"' in user
    assert "tiktok" not in out["plan"]["tools"] and out["changes"] == [{"at": "2026-10-21T08:00:00Z", "text": "drop TikTok and add a gift guide angle"}]
    assert store.read_request("swoveralls", "RQ-0001")["plan"] == out["plan"] and budget.used == 2


def test_a_revision_cannot_bring_back_a_tool_that_was_not_allowed(store, swov):
    req, budget = planned(store, swov, mode="manual", ticked=["meta_ads"])
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    out = PL.revise_request(store, swov, req, claude, budget, S, changes="add tiktok", now=NOW)
    assert set(out["plan"]["tools"]) == {"meta_ads"}


@pytest.mark.parametrize("status", ["running", "done"])
def test_a_request_that_has_run_cannot_be_changed(store, swov, status):
    req, budget = planned(store, swov)
    req["status"] = status
    with pytest.raises(PL.PlanError, match=f"RQ-0001 is {status}; a request can only be changed before it runs"):
        PL.revise_request(store, swov, req, FakeClaude(), budget, S, changes="x", now=NOW)


@pytest.mark.parametrize("status", ["planned", "partial", "failed"])
def test_a_request_that_did_not_finish_can_still_be_changed(store, swov, status):
    req, budget = planned(store, swov)
    req["status"] = status
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    assert PL.revise_request(store, swov, req, claude, budget, S, changes="x", now=NOW)["changes"]


def test_changing_a_plan_needs_claude_and_calls(store, swov):
    req, budget = planned(store, swov)
    with pytest.raises(PL.PlanError, match="changing the plan needs Claude"):
        PL.revise_request(store, swov, req, None, budget, S, changes="x", now=NOW)
    with pytest.raises(PL.PlanError, match="changing the plan needs Claude"):
        PL.revise_request(store, swov, req, FakeClaude(), IB.ClaudeBudget(store, NOW, 1), S, changes="x", now=NOW)


def test_a_failed_revision_keeps_the_old_plan(store, swov):
    req, budget = planned(store, swov)
    before = copy.deepcopy(req["plan"])
    claude = FakeClaude()
    claude.json_answers = [ProviderError("Claude: down")]
    with pytest.raises(PL.PlanError, match="could not change the plan"):
        PL.revise_request(store, swov, req, claude, budget, S, changes="x", now=NOW)
    assert store.read_request("swoveralls", "RQ-0001")["plan"] == before and req["changes"] == []


# ---- what a plan turns into -------------------------------------------------------------------------------------------------------------

def test_the_collectors_get_only_what_the_plan_asked_for():
    cfg = PL.collect_config(request(region="gb"))
    assert cfg == {"track_own_brand": False,
                   "ads": {"keywords": ["fathers day gift", "comfy gifts for dad"], "pages": [], "country": "gb"},
                   "social": {"instagram": {"hashtags": ["fathersdaygift", "giftsfordad"]}, "tiktok": {"hashtags": ["fathersdayideas"]}},
                   "deep_research": {"questions": ["How is Father's Day gifting marketed on social media?"]}}
    assert PL.collect_sources(request()) == ["meta_ads", "instagram", "tiktok", "deep_research"] and PL.research_queries(request())[0].endswith("site:tiktok.com")


def test_a_plan_without_ads_or_deep_research_leaves_those_out_of_the_config():
    plan, _ = clean(allowed=["instagram", "web_search"])
    cfg = PL.collect_config(request(plan, allowed=["instagram", "web_search"]))
    assert set(cfg) == {"track_own_brand", "social"} and cfg["social"] == {"instagram": {"hashtags": ["fathersdaygift", "giftsfordad"]}}
    assert PL.collect_sources(request(plan)) == ["instagram"]


def test_the_plans_config_is_one_the_collectors_accept_and_they_search_only_the_plans_seeds(store, swov):
    cfg = B.load_browser_settings("browser.yaml")
    parsed = C.parse_collect_config(PL.collect_config(request(region="worldwide")), "swoveralls request RQ-0001")
    f = world(cfg)
    settings = replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False, auto_enabled=False)
    result = C.run_collect([swov], store, browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                           sources=["meta_ads", "instagram", "tiktok"], now=NOW, run_id="r", configs={"swoveralls": parsed}, root="brands")
    assert {s for s, st in statuses(result)} == {"meta_ads", "instagram", "tiktok"}
    ads = f.browsers["meta_ads"].opened
    assert len(ads) == 2 and all("country=ALL" in u for u in ads) and any("fathers+day+gift" in u for u in ads) and not any("Swoveralls" in u for u in ads)
    assert [u for u in f.browsers["instagram"].opened if "/explore/tags/" in u] == ["https://www.instagram.com/explore/tags/fathersdaygift/"] or \
        any("fathersdaygift" in u for u in f.browsers["instagram"].opened)
    assert not any("swoveralls" in u.lower() for u in f.browsers["tiktok"].opened)


def test_web_results_are_matched_to_the_requests_own_searches():
    sources = {"a": {"queries": "best gifts | other thing"}, "b": {"queries": "unrelated"}, "c": {"queries": "BEST   GIFTS"}, "d": {}}
    assert sorted(PL.web_sources_for(sources, ["Best  Gifts"])) == ["a", "c"] and PL.web_sources_for(sources, []) == {}


# ---- running a request ---------------------------------------------------------------------------------------------------------------

class Steps:
    def __init__(self, collect=None, research=None, bank=None):
        self.collect, self.research, self.bank, self.calls = collect, research, bank, []

    def collect_runner(self, cfg, sources):
        self.calls.append(("collect", cfg, sources))
        if isinstance(self.collect, Exception):
            raise self.collect
        return self.collect or ["meta_ads ran: 3 items"]

    def research_runner(self, queries):
        self.calls.append(("research", queries))
        if isinstance(self.research, Exception):
            raise self.research
        return self.research or ["8 searches planned, 0 failed"]

    def ideabank_runner(self, req, queries):
        self.calls.append(("bank", req["id"], queries))
        if isinstance(self.bank, Exception):
            raise self.bank
        return self.bank or (["IB-0001", "IB-0002"], ["2 new item(s)"])


def go(store, swov, steps, req=None, now="2026-10-20T09:00:00Z"):
    req = req or request()
    store.write_request("swoveralls", req)
    return PL.execute_request(store, swov, req, now=now, collect_runner=steps.collect_runner, research_runner=steps.research_runner,
                              ideabank_runner=steps.ideabank_runner)


def test_a_request_runs_every_step_in_order_and_records_what_happened(store, swov):
    steps = Steps()
    done = go(store, swov, steps)
    assert [c[0] for c in steps.calls] == ["collect", "research", "bank"] and done["status"] == "done"
    assert steps.calls[0][2] == ["meta_ads", "instagram", "tiktok", "deep_research"] and steps.calls[1][1] == PL.research_queries(done)
    assert done["run"]["items_added"] == ["IB-0001", "IB-0002"] and done["run"]["started"] == done["run"]["finished"] == "2026-10-20T09:00:00Z"
    assert done["run"]["notes"] == ["collect: meta_ads ran: 3 items", "web search: 8 searches planned, 0 failed", "idea bank: 2 new item(s)"]
    assert store.read_request("swoveralls", "RQ-0001") == done


def test_it_is_marked_running_before_the_work_starts(store, swov):
    seen = []

    class Watch(Steps):
        def collect_runner(self, cfg, sources):
            seen.append(store.read_request("swoveralls", "RQ-0001")["status"])
            return ["x"]
    go(store, swov, Watch())
    assert seen == ["running"]


def test_a_failing_collect_does_not_stop_the_web_search_or_the_bank(store, swov):
    steps = Steps(collect=RuntimeError("Meta said no"))
    done = go(store, swov, steps)
    assert done["status"] == "partial" and [c[0] for c in steps.calls] == ["collect", "research", "bank"]
    assert "collect failed: Meta said no" in done["run"]["notes"]


def test_a_failing_web_search_does_not_stop_the_collectors(store, swov):
    done = go(store, swov, Steps(research=RuntimeError("no keys")))
    assert done["status"] == "partial" and "web search failed: no keys" in done["run"]["notes"] and done["run"]["items_added"] == ["IB-0001", "IB-0002"]


def test_a_failing_bank_step_is_recorded(store, swov):
    done = go(store, swov, Steps(bank=RuntimeError("drive down")))
    assert done["status"] == "partial" and "idea bank failed: drive down" in done["run"]["notes"] and done["run"]["items_added"] == []


def test_when_every_step_fails_the_request_failed(store, swov):
    done = go(store, swov, Steps(collect=RuntimeError("a"), research=RuntimeError("b"), bank=RuntimeError("c")))
    assert done["status"] == "failed"


def test_an_error_with_no_message_still_says_what_failed(store, swov):
    done = go(store, swov, Steps(collect=KeyError()))
    assert "collect failed: KeyError" in done["run"]["notes"]


def test_a_plan_with_no_collectors_or_no_web_search_skips_those_steps(store, swov):
    plan, _ = clean(allowed=["web_search"])
    steps = Steps()
    go(store, swov, steps, request(plan, allowed=["web_search"]))
    assert [c[0] for c in steps.calls] == ["research", "bank"]
    plan, _ = clean(allowed=["tiktok"])
    steps = Steps()
    go(store, swov, steps, request(plan, allowed=["tiktok"]))
    assert [c[0] for c in steps.calls] == ["collect", "bank"]


def test_a_request_that_is_running_or_done_cannot_be_run_again(store, swov):
    for status, text in (("running", "already running"), ("done", "has already run")):
        with pytest.raises(PL.PlanError, match=text):
            PL.execute_request(store, swov, request(status=status), now=NOW, collect_runner=None, research_runner=None, ideabank_runner=None)


def test_a_request_that_failed_or_was_partial_can_be_run_again(store, swov):
    for status in ("partial", "failed"):
        assert go(store, swov, Steps(), request(status=status))["status"] == "done"


@pytest.mark.parametrize("tool", ALL)
def test_no_tool_appears_in_a_plan_unless_it_is_allowed(tool):
    plan, notes = clean(allowed=[t for t in ALL if t != tool])
    assert tool not in plan["tools"] and f"left out {tool}: you did not allow it" in notes


@pytest.mark.parametrize("unsafe", ["../x", "RQ-1", "rq-0001", "RQ-0001/../../x", "", None, "RQ-0001.json"])
def test_an_unsafe_request_id_reads_as_nothing(store, unsafe):
    assert store.read_request("swoveralls", unsafe) is None


def test_the_next_request_number_follows_the_highest_saved(store):
    store.write_request("swoveralls", {"id": "RQ-0004"})
    store.write_request("swoveralls", {"id": "RQ-0002"})
    assert store.next_request_id("swoveralls") == "RQ-0005" and store.next_request_id("duderobe") == "RQ-0001"


@pytest.mark.parametrize("region", ["", None])
def test_a_request_with_no_region_is_worldwide(store, swov, region):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    req, _ = make(store, swov, claude, region=region)
    assert req["region"] == "worldwide"


def test_an_unsafe_request_id_never_reaches_a_file_that_exists(store):
    outside = store._brand_file("swoveralls", "secret.json")
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text('{"id": "secret"}')
    store.write_request("swoveralls", {"id": "RQ-0001", "text": "ok"})                  # the requests folder must exist for "../" to lead out of it
    assert store.read_request("swoveralls", "../secret") is None
    assert store.read_request("swoveralls", "RQ-0001") == {"id": "RQ-0001", "text": "ok"}
