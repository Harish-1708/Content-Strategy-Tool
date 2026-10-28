"""Ideas that can be re-made for the brand, not generic ones.

The audit of the first real Idea Bank (26 rows, 2026-10-28) showed a score that did not separate strong ideas from generic ones: a 437-day
Madepants hook scored 68 and 451 days scored 82, while an Amazon affiliate post scored 72, and 17 of 26 rows sat at 60 to 68 (a sneaker
scarcity ad, two outdoor-gear ads, a baby-clothes bundle, a restock price ad). The score is now built from two judgements, how well the item
fits and how strong its reusable idea is, combined by code, with a generic item and an unrelated category held down."""
import pytest
import yaml

from contentintel import cli
from contentintel import ideabank as IB
from contentintel.drive import DriveClient
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from test_collectors import NOW
from test_ideabank import S, bank_with, full_run, getter, kept_bank, record, row


def entry(idea_id="IB-0001", fit=80, idea=80, **kw):
    base = {"id": idea_id, "fit": fit, "idea": idea, "generic": False, "category": "same", "mechanism": "compliment hook",
            "concept": "The wearer is stopped and asked where it is from.", "topic": "asked where it's from", "summary": "A hook that ran long.",
            "how_to_use": "Use it for our drop.", "risk": "none"}
    base.update(kw)
    return base


def clean(*entries, ids=("IB-0001",)):
    return IB.clean_scores({"items": list(entries)}, set(ids))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


# ---- the score is built by code from two judgements --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fit,idea,generic,category,score", [
    (80, 80, False, "same", 80), (100, 100, False, "same", 100), (0, 0, False, "same", 0),
    (100, 50, False, "same", 70), (50, 100, False, "same", 80),             # the idea counts for more than the fit
    (90, 40, False, "same", 60), (90, 39, False, "same", 59),
    (100, 100, True, "same", 45), (30, 30, True, "same", 30),               # a generic item cannot reach the usual minimum
    (100, 100, False, "other", 45), (40, 40, False, "other", 40),           # nor can an unrelated product category
    (100, 100, True, "other", 45), (100, 100, False, "adjacent", 100)])
def test_the_score_is_a_weighted_blend_held_down_for_generic_and_unrelated(fit, idea, generic, category, score):
    assert IB.combined_score(fit, idea, generic, category) == score


def test_a_generic_item_can_never_reach_the_default_minimum_and_a_strong_one_can():
    assert IB.combined_score(100, 100, True, "same") < IB.IdeaBankSettings().min_relevance <= IB.combined_score(75, 75, False, "same")


# ---- reading Claude's answer ---------------------------------------------------------------------------------------------------------------

def test_a_full_answer_is_combined_and_every_field_is_kept():
    got = clean(entry(fit=90, idea=70))["IB-0001"]
    assert got["relevance"] == 78 and (got["fit"], got["idea"]) == (90, 70) and got["generic"] is False and got["category"] == "same"
    assert got["mechanism"] == "compliment hook" and got["concept"] == "The wearer is stopped and asked where it is from."
    assert got["topic"] == "asked where it's from" and got["summary"] and got["how_to_use"] and got["risk"] == "none"


def test_the_score_comes_from_the_two_judgements_not_from_a_number_claude_adds():
    assert clean({**entry(fit=60, idea=60), "relevance": 99})["IB-0001"]["relevance"] == 60


@pytest.mark.parametrize("generic,expected", [(True, True), ("true", True), ("TRUE", True), ("yes", True), (False, False), ("false", False), ("no", False),
                                              (None, False), ("", False), (1, False), ("maybe", False)])
def test_generic_is_read_strictly(generic, expected):
    assert clean(entry(generic=generic))["IB-0001"]["generic"] is expected


@pytest.mark.parametrize("category,expected", [("same", "same"), ("Adjacent", "adjacent"), (" OTHER ", "other"), ("unrelated", "adjacent"), ("", "adjacent"),
                                               (None, "adjacent"), (5, "adjacent")])
def test_an_unknown_category_is_treated_as_adjacent_not_as_a_free_pass_or_a_punishment(category, expected):
    assert clean(entry(category=category))["IB-0001"]["category"] == expected


def test_a_generic_answer_is_held_down_by_code_even_if_claude_rates_it_highly():
    assert clean(entry(fit=95, idea=95, generic=True))["IB-0001"]["relevance"] == 45
    assert clean(entry(fit=95, idea=95, category="other"))["IB-0001"]["relevance"] == 45


@pytest.mark.parametrize("bad", [{"fit": None}, {"idea": None}, {"fit": "80"}, {"idea": True}, {"fit": 101}, {"idea": -1}, {"fit": float("nan")}])
def test_a_missing_or_impossible_judgement_makes_the_answer_unusable_unless_a_plain_relevance_is_given(bad):
    assert clean({**entry(), **bad}) == {}
    assert clean({**entry(), **bad, "relevance": 70})["IB-0001"]["relevance"] == 70 and clean({**entry(), **bad, "relevance": 70})["IB-0001"]["fit"] is None


def test_an_older_style_answer_with_only_a_relevance_is_still_accepted_as_it_stands():
    old = {"id": "IB-0001", "relevance": 77, "topic": "t", "summary": "s", "how_to_use": "h", "risk": "none"}
    got = clean(old)["IB-0001"]
    assert got["relevance"] == 77 and got["fit"] is None and got["idea"] is None and got["generic"] is False and got["category"] == ""


def test_a_whole_number_is_not_needed_and_scores_are_rounded():
    got = clean(entry(fit=79.5, idea=70.4))["IB-0001"]
    assert (got["fit"], got["idea"]) == (80, 70) and got["relevance"] == round(0.4 * 79.5 + 0.6 * 70.4)


def test_texts_are_cut_to_a_sensible_length_and_a_missing_summary_makes_the_answer_unusable():
    got = clean(entry(concept="c" * 500, mechanism="m" * 200))["IB-0001"]
    assert len(got["concept"]) <= 240 and len(got["mechanism"]) <= 60
    assert clean(entry(summary="  ")) == {} and clean(entry(summary=None)) == {}


def test_only_the_items_asked_about_are_taken():
    assert clean(entry("IB-0009"), ids=("IB-0001",)) == {} and clean("text", None, 5) == {}


# ---- the instructions to Claude ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "judge TWO things separately", "'fit' (0-100)", "'idea' (0-100)", "a discount or promo code, a price, a restock or 'new arrivals' notice",
    "Mark those 'generic': true", "an ad that has run 60 or more days", "150 or more days is very strong", "a big brand's generic ad is still generic",
    "'same' (our own kind of product)", "'adjacent'", "'other' (unrelated products such as shoes, baby goods", "outdoor gear",
    "Be strict: most items are ordinary, so most scores should be under 60", "Never invent numbers, quotes or results", "the reusable idea with the product taken out"])
def test_the_instructions_say_what_makes_an_idea_worth_keeping(phrase):
    assert phrase in IB.SCORE_SYSTEM


def test_the_instructions_ask_for_every_field_the_code_reads():
    for field in ("fit", "idea", "generic", "category", "mechanism", "concept", "topic", "summary", "how_to_use", "risk"):
        assert f'\\"{field}\\"' in repr(IB.SCORE_SYSTEM).replace("'", "'") or f'"{field}"' in IB.SCORE_SYSTEM, field


def test_claude_is_shown_how_long_an_ad_has_run_so_longevity_can_count(store, swov):
    bank = bank_with("A")
    bank["items"]["IB-0001"]["metrics"] = {"days_running": 437, "variants": 3}
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry()]}]
    IB.score_items(bank, ["IB-0001"], claude, swov, store, S, IB.ClaudeBudget(store, NOW, 10), NOW)
    assert "days_running 437" in claude.json_calls[0][1] and "variants 3" in claude.json_calls[0][1] and claude.json_calls[0][0] == IB.SCORE_SYSTEM


# ---- scoring stores everything and scores old scores again, once --------------------------------------------------------------------------

def run_scoring(store, swov, bank, claude, ids=None):
    return IB.score_items(bank, ids or list(bank["items"]), claude, swov, store, S, IB.ClaudeBudget(store, NOW, 50), NOW)


def test_a_scored_item_keeps_the_two_judgements_the_idea_and_the_version(store, swov):
    bank = bank_with("A")
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry(fit=70, idea=90, category="adjacent")]}]
    assert run_scoring(store, swov, bank, claude)[0] == 1
    item = bank["items"]["IB-0001"]
    assert (item["score"], item["fit"], item["idea"], item["category"], item["generic"]) == (82, 70, 90, "adjacent", False)
    assert item["mechanism"] == "compliment hook" and item["concept"].startswith("The wearer is stopped") and item["scoring_version"] == IB.SCORING_VERSION


def test_needs_scoring_covers_new_old_current_and_removed_items():
    item = lambda **kw: {"state": "kept", "score": 80, "scoring_version": IB.SCORING_VERSION, **kw}      # noqa: E731
    assert IB.needs_scoring(item(score=None, state="new", scoring_version=0)) and IB.needs_scoring(item(scoring_version=1))
    assert not IB.needs_scoring(item()) and not IB.needs_scoring(item(state="removed", scoring_version=1)) and not IB.needs_scoring(item(state="removed", score=None))
    assert IB.needs_scoring({"state": "kept", "score": 70})                                               # no version at all means an old score


def test_an_item_scored_under_the_old_rubric_is_scored_again_and_the_new_judgement_replaces_it(store, swov):
    bank = kept_bank("A")
    old = bank["items"]["IB-0001"]
    old.update({"score": 72, "scoring_version": 1, "summary": "old summary", "fit": None, "idea": None})
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry(fit=60, idea=30, generic=True, summary="new summary")]}]
    assert run_scoring(store, swov, bank, claude)[0] == 1
    assert (old["score"], old["summary"], old["generic"], old["scoring_version"]) == (42, "new summary", True, IB.SCORING_VERSION)      # 0.4*60 + 0.6*30


def test_an_item_scored_under_the_current_rubric_is_left_alone(store, swov):
    bank = kept_bank("A")
    claude = FakeClaude()
    assert run_scoring(store, swov, bank, claude) == (0, []) and claude.json_calls == []


def test_an_answer_in_the_old_shape_still_counts_as_scored_so_it_is_not_asked_for_again_every_run(store, swov):
    bank = bank_with("A")
    claude = FakeClaude()
    claude.json_answers = [{"items": [{"id": "IB-0001", "relevance": 70, "topic": "t", "summary": "s", "how_to_use": "h", "risk": "none"}]}]
    run_scoring(store, swov, bank, claude)
    assert bank["items"]["IB-0001"]["score"] == 70 and not IB.needs_scoring(bank["items"]["IB-0001"])


def test_a_bank_from_before_gets_the_new_fields_and_its_scores_count_as_old(store):
    bank = kept_bank("A", "B")
    for item in bank["items"].values():
        for key in ("fit", "idea", "mechanism", "concept", "generic", "category", "scoring_version"):
            item.pop(key, None)
    bank["items"]["IB-0002"]["score"] = None
    IB.normalise_bank(bank)
    one, two = bank["items"]["IB-0001"], bank["items"]["IB-0002"]
    assert (one["scoring_version"], two["scoring_version"]) == (1, 0) and one["fit"] is None and one["generic"] is False and one["concept"] == "" and one["category"] == ""


# ---- what a run now does with the audit's examples ---------------------------------------------------------------------------------------------

def audit_bank():
    """Rows like the audit's: a proven hook, a generic promo, an unrelated category, and a moderate fit with a strong idea."""
    bank = {"next": 1, "items": {}}
    recs = [row("meta_ads", "1", handle="Madepants", title="Asked where it's from", text="Get stopped in the street and asked where your pants are from.", days_running=437, media_url="https://cdn.example/1.jpg"),
            row("meta_ads", "2", handle="Foreverfrenchbaby", title="Build your 3 for $99 Bundle and SAVE", text="Pick any three for $99 with code SAVE.", days_running=18, media_url="https://cdn.example/2.jpg"),
            row("meta_ads", "3", handle="Asian Sports", title="Limited Drop", text="Once they're gone, they're gone. Limited edition sneakers.", days_running=105, media_url="https://cdn.example/3.jpg"),
            row("meta_ads", "4", handle="Oodie Co", title="Indoor energy. Outdoor attitude", text="Comfort is not just for home. Wear it out.", days_running=37, media_url="https://cdn.example/4.jpg")]
    IB.intake_collected(bank, [record(recs)], S, NOW)
    return bank


def test_a_run_keeps_the_strong_ideas_and_removes_generic_and_unrelated_ones(store, swov, drive):
    client, fake = drive
    store.write_ideabank("swoveralls", audit_bank())
    claude = FakeClaude()
    claude.json_answers = [{"items": [
        entry("IB-0001", fit=75, idea=88, mechanism="compliment hook", topic="asked where it's from"),
        entry("IB-0002", fit=70, idea=30, generic=True, category="adjacent", mechanism="bundle promo", topic="3 for 99 bundle"),
        entry("IB-0003", fit=40, idea=70, category="other", mechanism="scarcity", topic="limited sneaker drop"),
        entry("IB-0004", fit=70, idea=80, category="adjacent", mechanism="occasion reframe", topic="indoor to outdoor")]}]
    new, lines = full_run(store, swov, (client, client.session), claude)
    items = store.read_ideabank("swoveralls")["items"]
    assert {i: it["state"] for i, it in items.items()} == {"IB-0001": "kept", "IB-0002": "removed", "IB-0003": "removed", "IB-0004": "kept"}
    assert items["IB-0001"]["score"] == 83 and items["IB-0004"]["score"] == 76 and items["IB-0002"]["score"] == 45 and items["IB-0003"]["score"] == 45
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - asked where it's from/", "  IB-0004 - indoor to outdoor/"]
    assert any("2 item(s) were not relevant enough" in line for line in lines)


def test_a_run_judges_the_old_scored_rows_again_and_drops_the_ones_that_were_generic(store, swov, drive):
    client, fake = drive
    bank = kept_bank("A", "B", "C")
    for item in bank["items"].values():
        item.update({"score": 66, "scoring_version": 1, "fit": None, "idea": None})
    store.write_ideabank("swoveralls", bank)
    first = IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(), S)
    store.write_ideabank("swoveralls", bank)
    assert first[0] == 3
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry("IB-0001", fit=80, idea=85), entry("IB-0002", fit=70, idea=20, generic=True), entry("IB-0003", fit=50, idea=40)]}]
    new, lines = full_run(store, swov, (client, client.session), claude)
    items = store.read_ideabank("swoveralls")["items"]
    assert {i: it["state"] for i, it in items.items()} == {"IB-0001": "kept", "IB-0002": "removed", "IB-0003": "removed"}      # 83, 40, 44
    assert len([n for n in fake.tree() if n.startswith("  IB-")]) == 1 and "2 folder(s) of removed items moved to the Drive trash" in " ".join(lines)
    assert "3 item(s) scored" in " ".join(lines)


def test_the_old_scored_rows_are_judged_again_only_once(store, swov, drive):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"].update({"scoring_version": 1})
    store.write_ideabank("swoveralls", bank)
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry("IB-0001", fit=80, idea=80)]}]
    full_run(store, swov, (client, client.session), claude)
    again = FakeClaude()
    full_run(store, swov, (client, client.session), again)
    assert len(claude.json_calls) == 1 and again.json_calls == []


# ---- what the sheet shows ---------------------------------------------------------------------------------------------------------------------

def test_the_sheet_shows_the_idea_to_copy_and_whether_there_is_a_file():
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"].update({"fit": 75, "idea": 88, "mechanism": "compliment hook", "concept": "Asked where it is from."})
    bank["items"]["IB-0002"]["assets"] = [{"name": "x"}]
    rows = IB.sheet_rows(bank)
    one, two = rows["IB-0001"], rows["IB-0002"]
    assert (one["fit"], one["idea"], one["mechanism"], one["concept"]) == (75, 88, "compliment hook", "Asked where it is from.") and one["preview"] == "original link only"
    assert two["preview"] == "saved in Drive" and two["fit"] == "" and two["idea"] == "" and two["concept"] == ""
    assert list(one) == IB.IDEABANK_COLUMNS


def test_the_new_columns_sit_next_to_the_score_in_a_new_tab():
    cols = IB.IDEABANK_COLUMNS
    assert cols[cols.index("score"):cols.index("score") + 6] == ["score", "fit", "idea", "format", "mechanism", "concept"] and "preview" in cols


def test_an_existing_idea_bank_tab_gains_the_new_columns_and_keeps_every_decision(tmp_path, swov):
    from fake_sheets import FakeClient
    from test_sheets import baselined, one
    from test_idea_commands import cells
    store = Store(tmp_path / "data")
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = kept_bank("A", "B")
    store.write_ideabank("swoveralls", bank)
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    ws, header, _ = cells(sp, "Idea Bank")
    ws._put(2, header.index("Decision") + 1, "Use")
    for item in bank["items"].values():
        item.update({"fit": 70, "idea": 90, "mechanism": "problem-first hook", "concept": "Lead with the problem."})
    store.write_ideabank("swoveralls", bank)
    report, error = one(swov, store, client)
    _, header, rows = cells(sp, "Idea Bank")
    assert error is None and rows[0]["Decision"] == "Use" and rows[0]["concept"] == "Lead with the problem." and rows[0]["fit"] == "70" and rows[0]["idea"] == "90"
    assert rows[0]["preview"] == "original link only"
    ws, header, _ = cells(sp, "Idea Bank")
    for column in ("fit", "idea"):
        value = ws.cells[(2, header.index(column) + 1)]
        assert isinstance(value, int) and not isinstance(value, bool), column                  # written as numbers, so the sheet can sort them


# ---- brands you chose to skip -------------------------------------------------------------------------------------------------------------------

def handled(*handles):
    bank = kept_bank(*[f"S{n}" for n in range(len(handles))])
    for item, handle in zip(bank["items"].values(), handles):
        item["handle"] = handle
    IB.normalise_bank(bank)
    return bank


def test_items_from_a_skipped_brand_are_removed_whatever_their_score():
    bank = handled("Cotopaxi", "Madepants", "COTOPAXI", "Cotopaxi Official Store")
    assert IB.tidy_skipped_brands(bank, ["Cotopaxi"]) == 2
    states = [it["state"] for it in bank["items"].values()]
    assert states == ["removed", "kept", "removed", "kept"]
    assert bank["items"]["IB-0001"]["removed_reason"] == "from Cotopaxi, a brand you chose to skip"


def test_a_skipped_brand_matches_however_it_is_spelled():
    assert IB.tidy_skipped_brands(handled("liquid-death", "Liquid Death", "Dollar Shave Club", "dollar shave club"), ["Liquid Death", "Dollar Shave Club"]) == 4


def test_nothing_is_removed_when_no_brand_is_skipped_or_the_handle_is_blank():
    bank = handled("Cotopaxi", "")
    assert IB.tidy_skipped_brands(bank, []) == 0 and IB.tidy_skipped_brands(bank, ["", None, "  "]) == 0 and {it["state"] for it in bank["items"].values()} == {"kept"}


def test_an_item_already_removed_is_not_counted_again():
    bank = handled("Cotopaxi")
    bank["items"]["IB-0001"].update({"state": "removed", "removed_reason": "a copy of IB-0009"})
    assert IB.tidy_skipped_brands(bank, ["Cotopaxi"]) == 0 and bank["items"]["IB-0001"]["removed_reason"] == "a copy of IB-0009"


def test_a_run_removes_skipped_brands_before_scoring_so_no_call_is_spent_on_them(store, swov, drive):
    client, fake = drive
    bank = bank_with("A", "B")
    bank["items"]["IB-0001"]["handle"] = "Cotopaxi"
    store.write_ideabank("swoveralls", bank)
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry("IB-0002")]}]
    new, lines = full_run(store, swov, (client, client.session), claude, skip_brands=["Cotopaxi"])
    prompt = claude.json_calls[0][1]
    items = store.read_ideabank("swoveralls")["items"]
    assert "IB-0001" not in prompt and "IB-0002" in prompt and items["IB-0001"]["state"] == "removed" and items["IB-0002"]["state"] == "kept"
    assert "swoveralls: 1 item(s) removed (0 the brand's own, 1 from brands you skip, 0 cop(ies), 0 not relevant enough); 1 kept" in lines


def test_a_skipped_brands_folder_is_moved_to_the_trash(store, swov, drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"]["handle"] = "Cotopaxi"
    IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(), S)
    store.write_ideabank("swoveralls", bank)
    new, lines = full_run(store, swov, (client, client.session), skip_brands=["Cotopaxi"])
    assert len([n for n in fake.tree() if n.startswith("  IB-")]) == 1 and "1 folder(s) of removed items moved to the Drive trash" in " ".join(lines)


def test_the_skipped_brand_names_come_from_the_brands_research_file(swov):
    assert sorted(cli._skipped_brands(swov, str(BRANDS_DIR))) == ["Allbirds", "Cotopaxi", "Dollar Shave Club", "Liquid Death"]


def test_only_skip_entries_count_not_page_ids_or_links(tmp_path, swov):
    root = tmp_path / "brands" / "swoveralls"
    root.mkdir(parents=True)
    (root / "research.yaml").write_text(yaml.safe_dump({"ads": {"keywords": ["x"], "benchmark_pages": {"Gymshark": "123456789012345", "Oodie": "skip",
                                                                                                      "Other": "https://www.facebook.com/ads/library/?view_all_page_id=123456789012345"}}}))
    assert cli._skipped_brands(swov, str(tmp_path / "brands")) == ["Oodie"]
    assert cli._skipped_brands(swov, str(tmp_path / "nowhere")) == []


def test_the_readme_explains_the_idea_first_scoring_and_the_new_columns():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("Ideas, not generic content", "fit", "idea strength", "concept", "original link only", "scored again once", "brands you skip"):
        assert needle in text, needle


# ---- the minimum is 50 and what is held down stays under it ------------------------------------------------------------------------------------------

def test_the_minimum_is_50_and_the_caps_on_generic_and_unrelated_items_sit_below_it():
    assert IB.IdeaBankSettings().min_relevance == 50 == IB.load_settings("research.yaml").min_relevance
    assert IB.GENERIC_CAP < IB.IdeaBankSettings().min_relevance and IB.OTHER_CAP < IB.IdeaBankSettings().min_relevance


def test_an_item_that_is_generic_or_unrelated_is_never_kept_at_the_default_minimum_whatever_else_is_true():
    for fit in (0, 50, 100):
        for idea in (0, 50, 100):
            assert IB.combined_score(fit, idea, True, "same") < 50 and IB.combined_score(fit, idea, False, "other") < 50
            assert IB.combined_score(fit, idea, True, "other") < 50


def test_a_wanted_format_never_lifts_a_generic_or_unrelated_item_but_does_lift_a_real_idea(store, swov):
    request = {"id": "RQ-0001", "text": "x", "formats": ["ugc"]}
    bank = bank_with("A", "B", "C")
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry("IB-0001", fit=100, idea=100, generic=True, format="ugc"), entry("IB-0002", fit=100, idea=100, category="other", format="ugc"),
                                       entry("IB-0003", fit=60, idea=60, format="ugc")]}]
    IB.score_items(bank, list(bank["items"]), claude, swov, store, S, IB.ClaudeBudget(store, NOW, 50), NOW, request)
    assert [it["score"] for it in bank["items"].values()] == [45, 45, 60 + IB.FORMAT_BOOST]


def test_an_item_between_the_old_and_new_minimum_is_kept_now():
    bank = bank_with("A")
    bank["items"]["IB-0001"].update({"score": 55, "scoring_version": IB.SCORING_VERSION})
    assert IB.apply_relevance(bank, S) == 0 and bank["items"]["IB-0001"]["state"] == "kept"


def test_items_brought_back_by_a_lowered_minimum_are_judged_again_before_they_count(store, swov, drive):
    client, fake = drive
    bank = bank_with("A", "B")
    for item, score in zip(bank["items"].values(), (55, 52)):
        item.update({"score": score, "scoring_version": 1, "state": "removed", "removed_reason": f"not relevant enough (scored {score}, the minimum is 60)"})
    store.write_ideabank("swoveralls", bank)
    claude = FakeClaude()
    claude.json_answers = [{"items": [entry("IB-0001", fit=70, idea=80), entry("IB-0002", fit=60, idea=20, generic=True)]}]
    new, lines = full_run(store, swov, (client, client.session), claude)
    prompt = claude.json_calls[0][1]
    items = store.read_ideabank("swoveralls")["items"]
    assert "IB-0001" in prompt and "IB-0002" in prompt                                    # both were revived and judged under the new rubric
    assert (items["IB-0001"]["state"], items["IB-0001"]["score"]) == ("kept", 76) and (items["IB-0002"]["state"], items["IB-0002"]["score"]) == ("removed", 36)
    assert items["IB-0002"]["scoring_version"] == IB.SCORING_VERSION
    text = " ".join(lines)
    assert "1 item(s) removed earlier for a low score reach the minimum now and were brought back" in text and "2 item(s) removed earlier" not in text
