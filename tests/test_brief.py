"""Briefs from approved Idea Bank items only: selection, Claude's structured output, the exact rules checked by code, one repair
pass, the optional bake-off, and the Word file (opened and read back for real)."""
import io
import base64
from dataclasses import replace

import pytest
from docx import Document

from contentintel import brief as BR
from contentintel import ideabank as IB
from contentintel import rules as RU
from contentintel.drive import DriveClient
from contentintel.providers import ProviderError
from contentintel.store import Store

from conftest import BRIEFS_DIR
from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from test_collectors import NOW
from test_ideabank import bank_with
from test_sheets import baselined

S = IB.IdeaBankSettings()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
HANDLES = {"swoveralls-midnight-black", "swoveralls-navy", "swoveralls-dark-athletic-grey"}


@pytest.fixture
def ruleset():
    return RU.load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")


@pytest.fixture
def store(tmp_path, swov):
    s = Store(tmp_path / "data")
    baselined(swov, s, "swoveralls_full_72.json", 72)
    return s


def concept(**over):
    base = {"name": "Comfort switch", "format": "30 second vertical video", "hook": "Sweatpants that secretly zip up into overalls", "script": "Show the zip, then the pockets.",
            "visual_direction": "Natural light, one take.", "cta": "Try the comfort switch", "product": "swoveralls-navy", "references": ["IB-0001"]}
    base.update(over)
    return base


def good_brief(**over):
    base = {"title": "Father's Day comfort", "objective": "Win gift orders", "audience": "adults buying for dad", "key_message": "Comfort he will wear",
            "tone": "warm", "concepts": [concept(), concept(name="Dad reacts", hook="Dad opens his sweatpants overalls", references=["IB-0002"])],
            "dos": ["Show the pockets"], "donts": ["Do not show denim"], "deliverables": ["2 videos"], "notes_for_creators": "Keep it real"}
    base.update(over)
    return base


def banked(store, ids=("A", "B", "C")):
    bank = bank_with(*ids)
    for item in bank["items"].values():
        item.update({"summary": f"summary of {item['title']}", "how_to_use": "use it our way", "topic": f"topic {item['id']}", "risk": "none"})
    store.write_ideabank("swoveralls", bank)
    return bank


def decisions(**kw):
    return {i: {"decision": d, "notes": ""} for i, d in kw.items()}


# ---- who is approved -------------------------------------------------------------------------------------------------------------

def test_only_items_marked_use_are_approved_oldest_id_first(store):
    bank = banked(store)
    got = BR.approved_items(bank, decisions(**{"IB-0003": "Use", "IB-0001": "yes", "IB-0002": "skip"}))
    assert [i["id"] for i in got] == ["IB-0001", "IB-0003"]


def test_maybe_is_used_only_when_asked(store):
    bank = banked(store)
    d = decisions(**{"IB-0001": "use", "IB-0002": "maybe", "IB-0003": ""})
    assert [i["id"] for i in BR.approved_items(bank, d)] == ["IB-0001"]
    assert [i["id"] for i in BR.approved_items(bank, d, include_maybe=True)] == ["IB-0001", "IB-0002"]


def test_an_item_with_no_decision_or_an_unknown_one_is_never_used(store):
    bank = banked(store)
    assert BR.approved_items(bank, {}) == [] and BR.approved_items(bank, decisions(**{"IB-0001": "later", "IB-9999": "use"})) == []


def test_a_request_limits_the_items_to_the_ones_it_found(store):
    bank = banked(store)
    bank["items"]["IB-0002"]["request_id"] = "RQ-0001"
    d = decisions(**{"IB-0001": "use", "IB-0002": "use"})
    assert [i["id"] for i in BR.approved_items(bank, d, request_id="RQ-0001")] == ["IB-0002"] and len(BR.approved_items(bank, d)) == 2


def test_the_products_a_brief_may_use_are_the_approved_ones_that_exist(store, swov, ruleset):
    assert set(BR.product_table(store, "swoveralls", ruleset)) == HANDLES


def test_without_an_approved_list_every_catalogue_product_is_allowed(store, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: b\nbrand: swoveralls\nversion: 1\ntext_rules: {}\nproduct_rules: {}\n")
    assert len(BR.product_table(store, "swoveralls", RU.load_ruleset(path))) == 72


def test_an_approved_product_missing_from_the_catalogue_is_not_offered(store, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: b\nbrand: swoveralls\nversion: 1\nproduct_rules:\n  approved_products: [swoveralls-navy, no-such-product]\n")
    assert set(BR.product_table(store, "swoveralls", RU.load_ruleset(path))) == {"swoveralls-navy"}


# ---- what Claude is asked --------------------------------------------------------------------------------------------------------

def test_the_prompt_carries_the_brand_the_ask_the_items_the_products_and_the_rules(store, swov, ruleset):
    bank = banked(store)
    items = BR.approved_items(bank, decisions(**{"IB-0001": "use", "IB-0002": "use"}))
    prompt = BR.build_prompt(swov, "Father's Day gifting", items, BR.product_table(store, "swoveralls", ruleset), ruleset, 6)
    for needle in ("Brand: Swoveralls (swoveralls.com).", "Request: Father's Day gifting", "Write between 3 and 6 concepts",
                   'IB-0001 (social post, instagram) "post A": summary of post A How we could use it: use it our way. Risk: none.',
                   "- swoveralls-navy: ", "(", "Never use em dash", "Never use exclamation point", "'sweatpants' must come before 'overalls' in hook, whenever both appear.",
                   "Guidance: Never show the classic overalls look"):
        assert needle in prompt, needle
    assert "$" in [line for line in prompt.splitlines() if line.startswith("- swoveralls-navy")][0]


def test_the_prompt_with_no_extra_rules_says_so(store, swov, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: b\nbrand: swoveralls\nversion: 1\n")
    assert "- (no extra rules)" in BR.build_prompt(swov, "x", [], {}, RU.load_ruleset(path), 4) and "- (none)" in BR.build_prompt(swov, "x", [], {}, RU.load_ruleset(path), 4)


def test_the_instructions_demand_the_things_that_make_a_brief_usable_and_safe():
    for needle in ("using ONLY the approved reference ideas", "Each concept must be inspired by at least one reference idea", "never a copy of someone else's content",
                   "Obey every rule exactly, including the banned characters and words", "Use only product handles from the list", "Do not invent prices, offers, claims, results or quotes"):
        assert needle in BR.BRIEF_SYSTEM, needle


# ---- cleaning Claude's brief --------------------------------------------------------------------------------------------------------

def clean(raw, ids=("IB-0001", "IB-0002"), handles=tuple(HANDLES), cap=8):
    return BR.clean_brief(raw, set(ids), set(handles), cap)


def test_a_good_brief_is_kept_whole():
    brief, notes = clean(good_brief())
    assert notes == [] and brief["title"] == "Father's Day comfort" and len(brief["concepts"]) == 2 and brief["concepts"][0]["references"] == ["IB-0001"]
    assert (brief["dos"], brief["donts"], brief["deliverables"], brief["notes_for_creators"]) == (["Show the pockets"], ["Do not show denim"], ["2 videos"], "Keep it real")


def test_a_reference_to_an_unapproved_idea_is_dropped_with_a_note():
    brief, notes = clean(good_brief(concepts=[concept(references=["IB-0001", "IB-0009", "IB-0001"])]))
    assert brief["concepts"][0]["references"] == ["IB-0001"] and "dropped reference(s) to unapproved or unknown idea(s) IB-0009" in notes[0]


def test_a_product_that_is_not_allowed_is_removed_with_a_note():
    brief, notes = clean(good_brief(concepts=[concept(product="swoveralls-garden-party")]))
    assert brief["concepts"][0]["product"] == "" and "product 'swoveralls-garden-party' is not an allowed product and was removed" in notes[0]


def test_a_concept_without_a_hook_is_dropped_and_none_left_is_an_error():
    brief, _ = clean(good_brief(concepts=[concept(hook=""), concept(hook="  "), concept(), "text", None]))
    assert len(brief["concepts"]) == 1
    with pytest.raises(BR.BriefError, match="no usable concepts"):
        clean(good_brief(concepts=[concept(hook="")]))
    with pytest.raises(BR.BriefError, match="no usable concepts"):
        clean(good_brief(concepts="x"))


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_a_brief_in_the_wrong_shape_is_refused(raw):
    with pytest.raises(BR.BriefError, match="not in the expected shape"):
        clean(raw)


def test_more_concepts_than_allowed_are_cut_with_a_note():
    brief, notes = clean(good_brief(concepts=[concept(name=f"c{n}") for n in range(6)]), cap=3)
    assert [c["name"] for c in brief["concepts"]] == ["c0", "c1", "c2"] and "kept the first 3 of 6 concepts" in notes


def test_missing_titles_and_names_get_defaults_and_long_texts_are_cut():
    brief, _ = clean(good_brief(title="", concepts=[concept(name="", script="s" * 3000, hook="h" * 900)], dos=["d" * 500] * 15))
    assert brief["title"] == "Creative brief" and brief["concepts"][0]["name"] == "Concept 1"
    assert len(brief["concepts"][0]["script"]) == 1500 and len(brief["concepts"][0]["hook"]) == 400 and len(brief["dos"]) == 10 and len(brief["dos"][0]) == 240


# ---- the exact rules, by code ---------------------------------------------------------------------------------------------------------

def products_for(store, ruleset):
    return BR.product_table(store, "swoveralls", ruleset)


def checked(store, ruleset, **over):
    brief, _ = clean(good_brief(**over))
    return BR.check_brief(brief, ruleset, products_for(store, ruleset))


def test_a_brief_that_obeys_every_rule_has_no_violations(store, ruleset):
    assert checked(store, ruleset) == []


def test_a_banned_character_in_a_hook_is_found_and_located(store, ruleset):
    found = checked(store, ruleset, concepts=[concept(hook="Sweatpants — then overalls!")])
    assert {(v["where"], v["rule"]) for v in found} == {("concept 1 (Comfort switch) hook", "banned_char")} and len(found) == 2
    assert all(v["severity"] == "error" for v in found)


def test_the_word_order_rule_applies_to_hooks_only(store, ruleset):
    bad_hook = checked(store, ruleset, concepts=[concept(hook="Overalls that feel like sweatpants")])
    assert [(v["where"], v["rule"]) for v in bad_hook] == [("concept 1 (Comfort switch) hook", "ordered_terms")]
    in_script = checked(store, ruleset, concepts=[concept(script="Overalls first, then sweatpants")])
    assert in_script == []


def test_every_text_field_is_checked(store, ruleset):
    found = checked(store, ruleset, title="Big!", objective="a — b", audience="x!", key_message="y!", tone="z!", notes_for_creators="n!",
                    concepts=[concept(name="N!", script="s!", visual_direction="v!", cta="c!")], dos=["d!"], donts=["e!"], deliverables=["f!"])
    wheres = {v["where"] for v in found}
    assert wheres >= {"title", "objective", "audience", "key_message", "tone", "notes_for_creators", "dos", "donts", "deliverables",
                      "concept 1 (N!) name", "concept 1 (N!) script", "concept 1 (N!) visual direction", "concept 1 (N!) cta"}


def test_a_blocked_or_unavailable_product_is_a_violation(store, ruleset):
    brief, _ = clean(good_brief())
    products = dict(products_for(store, ruleset))
    products["swoveralls-navy"] = {**products["swoveralls-navy"], "flags": ["limited_edition"], "status": "sold_out"}
    found = BR.check_brief(brief, ruleset, products)
    assert {v["rule"] for v in found} == {"blocked_flag", "status_not_allowed"} and all(v["where"].endswith("product") for v in found)


def test_a_product_missing_from_the_table_is_a_violation(store, ruleset):
    brief, _ = clean(good_brief())
    found = BR.check_brief(brief, ruleset, {})
    assert [v["rule"] for v in found] == ["unknown_product", "unknown_product"] and "'swoveralls-navy' is not in the catalogue" in found[0]["message"]


def test_only_error_violations_count_as_errors():
    assert BR.errors_of([{"severity": "error"}, {"severity": "warn"}]) == [{"severity": "error"}] and BR.errors_of([]) == []


# ---- writing one brief ----------------------------------------------------------------------------------------------------------------

def items_of(store):
    return BR.approved_items(banked(store), decisions(**{"IB-0001": "use", "IB-0002": "use"}))


def write(store, swov, ruleset, claude, cap=100, settings=S):
    budget = IB.ClaudeBudget(store, NOW, cap)
    return BR.write_brief(claude, swov, "Father's Day", items_of(store), products_for(store, ruleset), ruleset, settings, budget), budget


def claude_with(*answers):
    c = FakeClaude()
    c.json_answers = list(answers)
    return c


BAD = good_brief(concepts=[concept(hook="Sweatpants — wow!")])


def test_a_brief_that_passes_is_one_call_and_no_repair(store, swov, ruleset):
    claude = claude_with(good_brief())
    (brief, violations, notes, calls), budget = write(store, swov, ruleset, claude)
    assert violations == [] and notes == [] and calls == 1 and budget.used == 1 and len(claude.json_calls) == 1 and claude.json_calls[0][2] == 6000


def test_rule_breaking_is_sent_back_once_and_the_fixed_brief_is_used(store, swov, ruleset):
    claude = claude_with(BAD, good_brief())
    (brief, violations, notes, calls), budget = write(store, swov, ruleset, claude)
    assert violations == [] and calls == 2 and notes == ["one repair pass: 0 rule problem(s) left"] and brief["concepts"][0]["hook"] == "Sweatpants that secretly zip up into overalls"
    system, user, _ = claude.json_calls[1]
    assert "Problems to fix:\n- concept 1 (Comfort switch) hook: Contains em dash (1x)" in user and "exclamation point" in user and "Rules:" in user and "Brief:" in user and "keeping everything else word for word" in system


def test_a_repair_that_makes_things_worse_is_not_used(store, swov, ruleset):
    worse = good_brief(concepts=[concept(hook="Sweatpants — wow!", script="a!"), concept(hook="b!")])
    (brief, violations, notes, calls), _ = write(store, swov, ruleset, claude_with(BAD, worse))
    assert brief["concepts"][0]["hook"] == "Sweatpants — wow!" and len(brief["concepts"]) == 1 and "the repair made the brief worse and was not used" in notes and len(violations) == 2


def test_a_failed_repair_keeps_the_first_version_and_says_so(store, swov, ruleset):
    (brief, violations, notes, calls), _ = write(store, swov, ruleset, claude_with(BAD, ProviderError("Claude: overloaded")))
    assert len(violations) == 2 and notes == ["the repair pass failed (Claude: overloaded); the first version is kept"] and calls == 2


def test_an_unusable_repair_keeps_the_first_version(store, swov, ruleset):
    (brief, violations, notes, calls), _ = write(store, swov, ruleset, claude_with(BAD, {"concepts": []}))
    assert len(violations) == 2 and "the repair pass failed (the brief has no usable concepts)" in notes[0]


def test_with_no_call_left_for_a_repair_the_first_version_stands(store, swov, ruleset):
    (brief, violations, notes, calls), _ = write(store, swov, ruleset, claude_with(BAD), settings=replace(S, brief_max_calls=1))
    assert len(violations) == 2 and notes == ["no Claude call was left for a repair pass"] and calls == 1


def test_when_the_months_last_call_writes_the_brief_there_is_none_for_a_repair(store, swov, ruleset):
    (brief, violations, notes, calls), budget = write(store, swov, ruleset, claude_with(BAD), cap=1)
    assert notes == ["no Claude call was left for a repair pass"] and budget.used == 1 and len(violations) == 2


def test_warnings_alone_never_trigger_a_repair(store, swov, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: b\nbrand: swoveralls\nversion: 1\ntext_rules:\n  banned_phrases:\n    - {phrase: wow, severity: warn}\n")
    rs = RU.load_ruleset(path)
    claude = claude_with(good_brief(title="wow"))
    (brief, violations, notes, calls), _ = write(store, swov, rs, claude)
    assert calls == 1 and [v["severity"] for v in violations] == ["warn"]


def test_no_month_left_means_no_brief(store, swov, ruleset):
    with pytest.raises(BR.BriefError, match="monthly Claude limit .0 calls. is reached"):
        write(store, swov, ruleset, claude_with(), cap=0)


def test_a_claude_failure_while_writing_is_a_brief_error(store, swov, ruleset):
    with pytest.raises(BR.BriefError, match="Claude could not write the brief: Claude: down"):
        write(store, swov, ruleset, claude_with(ProviderError("Claude: down")))


# ---- choosing between models --------------------------------------------------------------------------------------------------------

def cand(model, errors=0):
    return {"model": model, "brief": good_brief(), "violations": [{"severity": "error", "where": "x", "rule": "r", "message": "m"}] * errors, "notes": []}


def judged(store, scores=None, reason="B is clearer", fail=None):
    judge = FakeClaude()
    judge.json_answers = [fail] if fail else [{"scores": scores or {}, "reason": reason}]
    return judge, IB.ClaudeBudget(store, NOW, 10)


def test_one_candidate_needs_no_judge(store):
    assert BR.pick_best([cand("m1")], None, IB.ClaudeBudget(store, NOW, 1), {"m1"}) == (0, [])


def test_the_judge_picks_the_higher_score(store):
    judge, budget = judged(store, {"m1": 6, "m2": 8})
    best, notes = BR.pick_best([cand("m1"), cand("m2")], judge, budget, {"m1", "m2"})
    assert best == 1 and notes[0] == "judge: B is clearer" and notes[1] == "bake-off: m1 6.0, m2 8.0; kept m2" and budget.used == 1


def test_rule_problems_count_against_a_version(store):
    judge, budget = judged(store, {"m1": 9, "m2": 8})
    assert BR.pick_best([cand("m1", errors=2), cand("m2")], judge, budget, {"m1", "m2"})[0] == 1       # 9 - 3 < 8


def test_a_failed_judge_falls_back_to_rule_problems(store):
    judge, budget = judged(store, fail=ProviderError("Claude: down"))
    best, notes = BR.pick_best([cand("m1", errors=1), cand("m2")], judge, budget, {"m1", "m2"})
    assert best == 1 and notes[0] == "the judge failed (Claude: down); versions were ranked by rule problems alone"


def test_no_judge_or_no_budget_falls_back_too(store):
    best, notes = BR.pick_best([cand("m1", errors=1), cand("m2")], None, IB.ClaudeBudget(store, NOW, 10), {"m1", "m2"})
    assert best == 1 and notes[0] == "no judge call was possible; versions were ranked by rule problems alone"
    judge = FakeClaude()
    assert BR.pick_best([cand("m1"), cand("m2")], judge, IB.ClaudeBudget(store, NOW, 0), {"m1", "m2"})[0] == 0 and judge.json_calls == []


def test_scores_out_of_range_or_for_unknown_models_are_ignored_and_ties_keep_the_first(store):
    judge, budget = judged(store, {"m1": 99, "m2": True, "ghost": 9, "m3": 0})
    best, notes = BR.pick_best([cand("m1"), cand("m2")], judge, budget, {"m1", "m2"})
    assert best == 0 and "m1 5.0, m2 5.0" in notes[-1]


# ---- the Word file --------------------------------------------------------------------------------------------------------------------

def read_docx(data):
    doc = Document(io.BytesIO(data))
    return doc, "\n".join(p.text for p in doc.paragraphs), "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)


def build(store, swov, ruleset, images=None, violations=(), notes=(), brief=None):
    items = items_of(store)
    items[0]["drive_folder_url"] = "https://drive.google.com/drive/folders/FOLDER1"
    meta = {"id": "BR-0001", "brief": "swoveralls-new-audience", "ask": "Father's Day gifting", "created": NOW}
    return BR.build_docx(clean(brief or good_brief())[0], swov, meta, items, images or {}, list(violations), ruleset, list(notes))


def test_the_file_is_a_real_word_document_with_every_section(store, swov, ruleset):
    doc, text, tables = read_docx(build(store, swov, ruleset))
    for needle in ("Father's Day comfort", "Swoveralls  |  swoveralls-new-audience  |  BR-0001  |  2026-10-20", "Request: Father's Day gifting", "Objective", "Win gift orders",
                   "Audience", "Key message", "Tone", "Concepts", "1. Comfort switch", "2. Dad reacts", "Do: Show the pockets", "Don't: Do not show denim", "Deliverables", "2 videos",
                   "Notes for creators", "Keep it real", "Reference gallery"):
        assert needle in text, needle
    assert "Sweatpants that secretly zip up into overalls" in tables and "swoveralls-navy" in tables and "IB-0001" in tables and "Natural light, one take." in tables


def test_the_gallery_lists_each_referenced_idea_with_its_links(store, swov, ruleset):
    doc, text, _ = read_docx(build(store, swov, ruleset))
    assert "IB-0001: topic IB-0001" in text and "IB-0002: topic IB-0002" in text and "What it is: summary of post A" in text and "How to use it: use it our way" in text
    assert "Original: https://www.instagram.com/p/A/" in text and "Saved files: https://drive.google.com/drive/folders/FOLDER1" in text and "IB-0003" not in text


def test_a_saved_picture_is_placed_in_the_file(store, swov, ruleset):
    doc, _, _ = read_docx(build(store, swov, ruleset, images={"IB-0001": PNG}))
    assert len(doc.inline_shapes) == 1


def test_a_picture_word_cannot_read_does_not_lose_the_brief(store, swov, ruleset):
    doc, text, _ = read_docx(build(store, swov, ruleset, images={"IB-0001": b"not an image"}))
    assert len(doc.inline_shapes) == 0 and "the saved picture could not be placed in this file; open the folder link" in text and "Father's Day comfort" in text


def test_a_brief_that_cites_no_ideas_has_no_gallery(store, swov, ruleset):
    doc, text, _ = read_docx(build(store, swov, ruleset, brief=good_brief(concepts=[concept(references=[])])))
    assert "Reference gallery" not in text


def test_the_needs_review_section_lists_everything_a_person_must_still_decide(store, swov, ruleset):
    v = [{"severity": "error", "where": "concept 1 hook", "rule": "banned_char", "message": "Contains em dash (1x)"}]
    doc, text, _ = read_docx(build(store, swov, ruleset, violations=v, notes=["one repair pass: 1 rule problem(s) left"]))
    assert "Needs review" in text and "[error] concept 1 hook: Contains em dash (1x)" in text and "Note: one repair pass: 1 rule problem(s) left" in text
    assert "Open question: Concepts 1, 5 and 6 use 'overalls'" in text and "For a person to check: Never show the classic overalls look" in text


# ---- pictures from Drive --------------------------------------------------------------------------------------------------------------------

def saved_items(store, kinds=("image",)):
    fake = FakeDrive()
    client = DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)
    bank = banked(store)
    for idea_id in ("IB-0001",):
        item = bank["items"][idea_id]
        folder = client.item_folder(client.brand_folder("Swoveralls"), idea_id, "topic")
        for n, kind in enumerate(kinds):
            fid = client.upload(folder, f"{idea_id}-{n}.bin", PNG, "image/png")
            item["assets"].append({"name": f"{idea_id}-{n}", "drive_id": fid, "kind": kind, "bytes": len(PNG)})
    return client, fake, [bank["items"]["IB-0001"]]


def test_the_first_saved_picture_of_each_idea_is_downloaded(store):
    client, fake, items = saved_items(store, kinds=("video", "image", "image"))
    images, notes = BR.saved_pictures(items, client, 1)
    assert images == {"IB-0001": PNG} and notes == [] and sum(1 for c in fake.calls if c["params"].get("alt") == "media") == 1


def test_videos_are_never_placed_as_pictures(store):
    client, _, items = saved_items(store, kinds=("video",))
    assert BR.saved_pictures(items, client, 1) == ({}, [])


def test_no_drive_or_no_pictures_wanted_means_none(store):
    client, _, items = saved_items(store)
    assert BR.saved_pictures(items, None, 1) == ({}, []) and BR.saved_pictures(items, client, 0) == ({}, [])


def test_a_picture_that_cannot_be_read_is_a_note_and_the_next_one_is_tried(store):
    client, fake, items = saved_items(store, kinds=("image", "image"))
    items[0]["assets"][0]["drive_id"] = "GONE"
    images, notes = BR.saved_pictures(items, client, 2)
    assert images == {"IB-0001": PNG} and len(notes) == 1 and notes[0].startswith("IB-0001: the saved picture could not be read from Drive")


# ---- one brief, start to finish --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def usable(monkeypatch):
    monkeypatch.setattr(BR, "catalogue_state", lambda slug, store, now: {"usable": True, "reasons": []})


def make(store, swov, ruleset, claudes, drive=None, ask="Father's Day gifting", settings=S, d=None, prepare=None, **kw):
    bank = banked(store)
    if prepare:
        prepare(bank)
        store.write_ideabank("swoveralls", bank)
    return BR.make_brief(swov, store, brief_name="swoveralls-new-audience", ask=ask, ruleset=ruleset,
                         decisions=d or decisions(**{"IB-0001": "use", "IB-0002": "use"}), claude_for=lambda m: claudes[m], settings=settings,
                         budget=IB.ClaudeBudget(store, NOW, 100), now=NOW, drive=drive, **kw)


def test_a_catalogue_that_cannot_be_used_stops_the_brief(tmp_path, swov, ruleset):
    empty = Store(tmp_path / "empty")
    with pytest.raises(BR.BriefError, match="the catalogue cannot be used right now: "):
        make(empty, swov, ruleset, {})


def test_a_brief_needs_to_know_what_it_is_for(store, swov, ruleset, usable):
    with pytest.raises(BR.BriefError, match="say what the brief is for"):
        make(store, swov, ruleset, {}, ask="  ")


def test_a_brief_needs_approved_items_and_says_how_to_approve_them(store, swov, ruleset, usable):
    with pytest.raises(BR.BriefError, match="mark rows Use in the Decision column of the Idea Bank tab, then run the sheet update"):
        make(store, swov, ruleset, {}, d=decisions(**{"IB-0001": "skip"}))
    with pytest.raises(BR.BriefError, match=r"\(for request RQ-0009\)"):
        make(store, swov, ruleset, {}, request_id="RQ-0009")


def test_a_brief_needs_at_least_one_allowed_product(store, swov, usable, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: b\nbrand: swoveralls\nversion: 1\nproduct_rules:\n  approved_products: [no-such-product]\n")
    with pytest.raises(BR.BriefError, match="no product in the catalogue is allowed by this brief's rules"):
        make(store, swov, RU.load_ruleset(path), {})


def test_a_brief_is_written_checked_and_saved_as_a_record_and_a_word_file(store, swov, ruleset, usable, tmp_path):
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    assert record["id"] == "BR-0001" and record["status"] == "ready" and record["violations"] == [] and record["items"] == ["IB-0001", "IB-0002"]
    assert record["used_items"] == ["IB-0001", "IB-0002"] and record["models"] == ["claude-sonnet-5-5"] and record["title"] == "Father's Day comfort"
    assert (record["brief"], record["ask"], record["created"], record["drive_link"], record["rules_digest"]) == ("swoveralls-new-audience", "Father's Day gifting", NOW, "", ruleset.digest)
    assert store.list_briefs("swoveralls") == [record]
    doc, text, _ = read_docx((tmp_path / "data" / "swoveralls" / "briefs" / "BR-0001.docx").read_bytes())
    assert "Father's Day comfort" in text


def test_a_brief_that_still_breaks_a_rule_is_marked_needs_review_and_says_what(store, swov, ruleset, usable, tmp_path):
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(BAD, BAD)})
    assert record["status"] == "needs review" and len(record["violations"]) == 2
    _, text, _ = read_docx((tmp_path / "data" / "swoveralls" / "briefs" / "BR-0001.docx").read_bytes())
    assert "[error] concept 1 (Comfort switch) hook: Contains em dash" in text


def test_the_file_goes_to_the_brands_briefs_folder_in_drive_and_the_link_is_kept(store, swov, ruleset, usable):
    fake = FakeDrive()
    client = DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())}, drive=client)
    assert fake.tree() == ["Swoveralls/", "  Briefs/", "    BR-0001 - Father's Day comfort.docx"] and record["drive_link"].startswith("https://drive.google.com/file/d/")
    assert store.list_briefs("swoveralls")[0]["drive_link"] == record["drive_link"]


def test_the_referenced_ideas_pictures_are_placed_from_drive(store, swov, ruleset, usable, tmp_path):
    fake = FakeDrive()
    client = DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)

    def with_picture(bank):
        folder = client.item_folder(client.brand_folder("Swoveralls"), "IB-0001", "topic")
        fid = client.upload(folder, "IB-0001-image.png", PNG, "image/png")
        bank["items"]["IB-0001"]["assets"].append({"name": "IB-0001-image.png", "drive_id": fid, "kind": "image", "bytes": len(PNG)})
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())}, drive=client, prepare=with_picture)
    doc, _, _ = read_docx((tmp_path / "data" / "swoveralls" / "briefs" / "BR-0001.docx").read_bytes())
    assert len(doc.inline_shapes) == 1 and not any("picture" in n for n in record["notes"])


def test_a_drive_failure_keeps_the_repository_copy_and_says_so(store, swov, ruleset, usable):
    fake = FakeDrive(script=[(403, {"error": {"message": "no"}})])
    client = DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())}, drive=client)
    assert record["drive_link"] == "" and any("the file was not saved in Drive" in n and "the copy in the repository is the only one" in n for n in record["notes"])
    assert store.list_briefs("swoveralls")


def test_a_second_brief_gets_the_next_number(store, swov, ruleset, usable):
    first = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    second = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    assert (first["id"], second["id"]) == ("BR-0001", "BR-0002")


def test_with_two_models_each_writes_a_brief_and_the_judged_best_is_kept(store, swov, ruleset, usable):
    one, two = claude_with(good_brief(title="First model")), claude_with(good_brief(title="Second model"))
    one.json_answers.append({"scores": {"claude-sonnet-5-5": 5, "claude-opus-5-5": 9}, "reason": "second is sharper"})
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": one, "claude-opus-5-5": two}, settings=replace(S, brief_models=("claude-sonnet-5-5", "claude-opus-5-5")))
    assert record["title"] == "Second model" and record["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert any(n.startswith("bake-off: ") and n.endswith("kept claude-opus-5-5") for n in record["notes"]) and "judge: second is sharper" in record["notes"]


def test_a_model_that_fails_is_noted_and_the_other_is_used(store, swov, ruleset, usable):
    bad, ok = claude_with(ProviderError("Claude: down")), claude_with(good_brief(title="Survivor"))
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": bad, "claude-opus-5-5": ok}, settings=replace(S, brief_models=("claude-sonnet-5-5", "claude-opus-5-5")))
    assert record["title"] == "Survivor" and any("claude-sonnet-5-5: Claude could not write the brief" in n for n in record["notes"])


def test_when_no_model_can_write_the_brief_it_is_an_error_naming_each(store, swov, ruleset, usable):
    with pytest.raises(BR.BriefError, match="no model produced a usable brief: claude-sonnet-5-5: Claude could not write the brief: Claude: down"):
        make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(ProviderError("Claude: down"))})


def test_only_the_requests_items_are_used_when_a_request_is_named(store, swov, ruleset, usable):
    def tag(bank):
        bank["items"]["IB-0002"]["request_id"] = "RQ-0001"
    claude = claude_with(good_brief(concepts=[concept(references=["IB-0002"])]))
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude}, prepare=tag, request_id="RQ-0001")
    prompt = claude.json_calls[0][1]
    assert record["items"] == ["IB-0002"] and record["request_id"] == "RQ-0001" and "IB-0002 (" in prompt and "IB-0001 (" not in prompt


def test_rows_marked_maybe_are_used_only_when_asked(store, swov, ruleset, usable):
    d = decisions(**{"IB-0001": "use", "IB-0002": "maybe"})
    plain = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(concepts=[concept()]))}, d=d)
    maybe = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(concepts=[concept()]))}, d=d, include_maybe=True)
    assert plain["items"] == ["IB-0001"] and maybe["items"] == ["IB-0001", "IB-0002"]


def test_only_the_ideas_the_brief_actually_cites_count_as_used(store, swov, ruleset, usable):
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(concepts=[concept(references=["IB-0001"])]))})
    assert record["items"] == ["IB-0001", "IB-0002"] and record["used_items"] == ["IB-0001"]


def test_a_picture_is_taken_once_per_idea_even_when_more_are_wanted(store):
    client, fake, items = saved_items(store, kinds=("image", "image", "image"))
    images, notes = BR.saved_pictures(items, client, 3)
    assert images == {"IB-0001": PNG} and sum(1 for c in fake.calls if c["params"].get("alt") == "media") == 1
