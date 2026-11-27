"""Briefs from approved Idea Bank items only: selection, Claude's structured output, the exact rules checked by code, one repair
pass, the optional bake-off, and the Word file (opened and read back for real)."""
import io
from types import SimpleNamespace
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
    base = {"name": "Comfort switch", "format": "30 second vertical video", "hook": "Sweatpants that secretly zip up into a full outfit", "script": "Show the zip, then the pockets.",
            "visual_direction": "Natural light, one take.", "cta": "Try the comfort switch", "product": "swoveralls-navy", "references": ["IB-0001"]}
    base.update(over)
    return base


def good_brief(**over):
    base = {"title": "Father's Day comfort", "objective": "Win gift orders", "audience": "adults buying for dad", "key_message": "Comfort he will wear",
            "tone": "warm", "concepts": [concept(), concept(name="Dad reacts", hook="Dad opens his sweatpants", references=["IB-0002"])],
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
    assert {(v["where"], v["rule"]) for v in found} == {("concept 1 (Comfort switch) hook", "banned_char"), ("concept 1 (Comfort switch) hook", "banned_phrase")} and len(found) == 3          # the em dash, the exclamation point, and 'overalls' in a hook (the Update 73 v5 rule)
    assert all(v["severity"] == "error" for v in found)


def test_the_word_order_rule_applies_to_hooks_only(store, ruleset):
    bad_hook = checked(store, ruleset, concepts=[concept(hook="Overalls that feel like sweatpants")])
    assert sorted((v["where"], v["rule"]) for v in bad_hook) == [("concept 1 (Comfort switch) hook", "banned_phrase"), ("concept 1 (Comfort switch) hook", "ordered_terms")]
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
    assert violations == [] and calls == 2 and notes == ["one repair pass: 0 rule problem(s) left"] and brief["concepts"][0]["hook"] == "Sweatpants that secretly zip up into a full outfit"
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
    assert "Sweatpants that secretly zip up into a full outfit" in tables and "swoveralls-navy" in tables and "IB-0001" in tables and "Natural light, one take." in tables


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


def test_the_evidence_for_the_concepts_is_sent_to_the_model_when_there_is_some_and_not_when_there_is_none(store, swov, ruleset, usable):
    from contentintel import voice as VOICE
    plain = claude_with(good_brief())
    make(store, swov, ruleset, {"claude-sonnet-5-5": plain})
    assert "Evidence for the concepts" not in plain.json_calls[0][1]
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", "straps slip all day", "https://r/1", "", {"score": 410}, "", "", "t", "research")], NOW)
    store.write_extra("swoveralls", "voice", voice)
    rich = claude_with(good_brief())
    make(store, swov, ruleset, {"claude-sonnet-5-5": rich})
    sent = rich.json_calls[0][1]
    assert "Evidence for the concepts (not facts about our products)" in sent and "What audiences are saying right now" in sent and "410 engagement" in sent
    assert sent.index("Facts you may state") < sent.index("Evidence for the concepts")                  # the evidence is kept apart from, and after, the facts the brief may state


# ---- stage 3: a brief is written only from approved canon and every claim in it goes through the gate ---------------------------------------------------------------------------------------------------
def _ledger(store):
    from pathlib import Path
    from contentintel import canon as C
    reg = C.Registry(store.data_dir, "swoveralls")
    C.apply_seed(reg, C.load_seed(Path("brands/swoveralls/claims_seed.yaml")), NOW)
    return reg


def test_a_brand_with_no_canon_and_no_claims_gets_its_brief_with_a_note_that_it_was_not_read_against_approved_canon(store, swov, ruleset, usable):
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    assert record["status"] == "ready" and any(n.startswith("no narrative canon and no claims ledger exist for this brand yet: the brief was NOT read against approved canon") for n in record["notes"])
    assert not any(v["rule"] == "claims-gate" for v in record["violations"])


def test_a_brand_whose_canon_has_proposals_but_none_approved_is_refused_with_the_fix(store, swov, ruleset, usable):
    reg = _ledger(store)
    reg.propose_canon("positioning", "Swoveralls makes soft overalls for grown-ups.", "launch/research/positioning-mapper", "a" * 64, NOW)
    with pytest.raises(BR.BriefError, match="canon has proposals but none is approved: approve the foundation sections in the Canon tab"):
        make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})


def test_the_approved_canon_and_the_do_not_state_list_reach_the_prompt_and_the_brief_is_read_against_the_ledger(store, swov, ruleset, usable):
    from contentintel import canon as C
    reg = _ledger(store)
    reg.propose_canon("positioning", "Swoveralls makes soft overalls for grown-ups who want comfort.", "launch/research/positioning-mapper", "a" * 64, NOW)
    reg.decide_canon(next(iter(reg.state()["canon"])), "approved", C.OWNER, NOW)
    claude = claude_with(good_brief())
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude})
    prompt = claude.json_calls[0][1]
    assert "APPROVED CANON" in prompt and "[positioning] Swoveralls makes soft overalls for grown-ups who want comfort." in prompt and "DO NOT STATE (the claims ledger blocks or disputes these)" in prompt and "- Awards (blocked)" in prompt
    assert not any(n.startswith("no narrative canon") for n in record["notes"])


def test_a_blocked_claim_in_the_brief_makes_it_need_review_and_a_checkable_statement_nothing_verifies_is_a_warning(store, swov, ruleset, usable):
    from contentintel import canon as C
    reg = _ledger(store)
    reg.propose_canon("positioning", "Swoveralls makes soft overalls for grown-ups.", "launch/research/positioning-mapper", "a" * 64, NOW)
    reg.decide_canon(next(iter(reg.state()["canon"])), "approved", C.OWNER, NOW)
    bad = good_brief(concepts=[concept(script="Swoveralls won several awards for its design. Show the zip, then the pockets.")])
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(bad)})
    gate = [v for v in record["violations"] if v["rule"] == "claims-gate"]
    assert record["status"] == "needs review" and any(v["severity"] == "error" and v["message"].startswith("BLOCKED:") and "script" in v["where"] for v in gate)
    viol = BR.gate_violations({"title": "T", "concepts": [{"name": "n", "hook": "Clinically proven to be comfy.", "script": "", "visual_direction": "", "cta": ""}]}, reg)
    assert viol and all(v["severity"] == "warning" and v["message"].startswith("NEEDS_REVIEW:") for v in viol)
    assert BR.gate_violations({"title": "Soft overalls", "concepts": []}, reg) == []


def test_a_validator_error_or_a_reviewer_that_says_revise_stops_a_brief_being_ready_and_the_findings_are_saved(store, swov, ruleset, usable):
    ok = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {"hook_confirms_the_ask": True}})})
    assert ok["status"] == "ready" and ok["review"]["verdict"] == "pass" and ok["rules_version"] == 1 and ok["content"]["concepts"][0]["name"] == "Comfort switch"
    invisible = good_brief(concepts=[concept(script="Show the zip,\u200b then the pockets.")])
    bad = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(invisible, {"verdict": "pass", "problems": [], "checklist": {}})})
    assert bad["violations"] == [] and bad["status"] == "needs review" and any(f["rule"] == "slop_invisible" and f["severity"] == "error" for f in bad["validator_findings"])      # only the validator bank objected
    revise = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(), {"verdict": "revise", "problems": [{"where": "concept 1", "issue": "no proof"}], "checklist": {"no_unverifiable_claim": False}})})
    assert revise["violations"] == [] and not [f for f in revise["validator_findings"] if f["severity"] == "error"] and revise["status"] == "needs review" and revise["review"]["verdict"] == "revise"
    assert any(n.startswith("independent review: revise") for n in revise["notes"]) and any(n.startswith("validator bank:") for n in bad["notes"])
    down = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    assert down["status"] == "ready" and down["review"]["verdict"] == "unavailable"                                                       # a reviewer that cannot answer never blocks a brief


def test_a_script_given_as_a_list_of_lines_stays_lines_and_is_never_shown_as_a_python_list():
    raw = good_brief(concepts=[concept(script=["Show the zip.", "  Then   the pockets. ", "", "Say the fit."]), concept(name="Plain", script="One line only")])
    brief, _notes = BR.clean_brief(raw, {"IB-0001", "IB-0002"}, {"swoveralls-navy"}, 7)
    assert brief["concepts"][0]["script"] == "Show the zip.\nThen the pockets.\nSay the fit." and "[" not in brief["concepts"][0]["script"] and brief["concepts"][1]["script"] == "One line only"
    assert "plain text with one spoken or shown line per line" in BR.BRIEF_SYSTEM and "normal apostrophes in contractions" in BR.BRIEF_SYSTEM


class RoutedFake(FakeClaude):
    """A routed client that says how its calls were answered (as services.RoutedClaude does) and why the pool did not."""
    def __init__(self, route, notes=(), answers=()):
        super().__init__()
        self.calls, self.notes, self.json_answers = [], [], list(answers)
        self.route, self.pending = route, list(notes)

    def json_task(self, system, user, max_tokens=None, **kw):
        self.calls.append(self.route)
        self.notes += self.pending                                                                                                              # the reason is written while the call is made, as the real client does
        self.pending = []
        return super().json_task(system, user, max_tokens)

    def for_job(self, job):
        return self


def test_the_record_says_who_wrote_the_brief_and_why_the_pool_did_not_when_claude_did(store, swov, ruleset, usable):
    pool = make(store, swov, ruleset, {"claude-sonnet-5-5": RoutedFake("gateway", answers=[good_brief(), {"verdict": "pass", "problems": [], "checklist": {}}])})
    assert pool["built_by"] == "the pool (lane brief_json)" and "writer: the pool (lane brief_json)" in pool["notes"] and pool["revision"] == 0 and pool["revision_of"] == ""
    claude = make(store, swov, ruleset, {"claude-sonnet-5-5": RoutedFake("claude", notes=["AI Gateway had no answer for brief (every route failed); Claude was used"], answers=[good_brief(), {"verdict": "pass", "problems": [], "checklist": {}}])})
    assert claude["built_by"] == "Claude (the pool had no usable answer)" and any("every route failed" in n for n in claude["notes"])
    plain = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief())})
    assert plain["built_by"] == "claude-sonnet-5-5"                                                                                            # an unrouted client: the configured model name, as before


def test_a_brief_that_needs_review_is_revised_from_its_own_problems_and_the_revision_says_which_brief_it_fixes(store, swov, ruleset, usable):
    first = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(concepts=[concept(script="Show the zip,\u200b then the pockets.")]),
                                                                          {"verdict": "revise", "problems": [{"where": "concept 1", "issue": "the hook never says the product"}], "checklist": {"hook_confirms_the_ask": False}})})
    assert first["status"] == "needs review" and first["content"]["concepts"][0]["script"].startswith("Show the zip")
    lines = BR.revision_problems(first)
    assert any("slop_invisible" in l or "invisible character" in l for l in lines) and "concept 1: the hook never says the product (independent reviewer)" in lines
    claude = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {"hook_confirms_the_ask": True}})
    second = make(store, swov, ruleset, {"claude-sonnet-5-5": claude}, revise_of=first)
    assert second["status"] == "ready" and second["revision"] == 1 and second["revision_of"] == first["id"] and second["id"] != first["id"] and second["review"]["verdict"] == "pass"
    system, user, _ = claude.json_calls[0]
    assert system.endswith(BR.REVISE_ADDENDUM) and "The brief to revise:" in user and "the hook never says the product (independent reviewer)" in user and "Show the zip,\u200b then the pockets." in user
    third = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})}, revise_of=second)
    assert third["revision"] == 2 and third["revision_of"] == second["id"]
    plain = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    make(store, swov, ruleset, {"claude-sonnet-5-5": plain})
    assert BR.REVISE_ADDENDUM not in plain.json_calls[0][0] and "The brief to revise:" not in plain.json_calls[0][1]                                # a new brief is written as before


def test_the_blocked_claim_is_named_to_the_revision_as_something_to_remove_never_to_restate():
    record = {"violations": [{"where": "concept 6 (POV) script", "rule": "claims-gate", "severity": "error", "message": "BLOCKED: the cotton claim"}, {"where": "c1 hook", "rule": "banned_char", "severity": "error", "message": "Contains em dash"}],
              "validator_findings": [{"where": "c1 hook", "rule": "hook_no_formula", "severity": "warning", "message": "no formula"}], "review": {"problems": []}}
    lines = BR.revision_problems(record)
    assert lines[0].endswith("(BLOCKED claim: remove it, never restate it)") and "Contains em dash" in lines[1] and lines[-1] == "c1 hook: no formula (warning)"


def test_a_phrase_can_be_banned_only_where_it_matters_and_the_swoveralls_overalls_rule_bans_it_in_the_hook_only():
    from contentintel import rules as R
    swov_rules = R.load_ruleset("briefs/swoveralls-new-audience/rules.yaml")
    assert [v.rule for v in R.check_text(swov_rules, "sweatpants overalls in three colors", "hook")] == ["banned_phrase"]               # BR-0001's hooks 1 and 2 said it
    assert R.check_text(swov_rules, "Free the waistband with Swoveralls", "hook") == [] and R.check_text(swov_rules, "Overalls with six pockets", "copy") == []     # the brand name and the body copy are fine
    ordered = [v for v in R.check_text(swov_rules, "overalls then sweatpants", "hook") if v.rule == "ordered_terms"]
    assert ordered                                                                                                                      # the older word-order rule is untouched
    assert next(item for item in swov_rules.rules["text_rules"]["banned_phrases"])["severity"] == "error"


# ---- the production-brief fields (Update 73 v6) ------------------------------------------------------------------------------------------------------------------------------------------------

def full_concept(**over):
    return concept(**{"alt_hook": "Sweatpants that become a whole outfit", "angle": "comfort without giving up style", "awareness": "cold", "tests": "whether the comfort angle beats the style angle",
                      "size": "9:16 1080x1920", "never": ["never say it replaces jeans", "never show a price"], **over})


def test_every_concept_carries_an_alt_hook_angle_awareness_what_it_tests_a_size_and_its_own_never_list_and_junk_is_dropped():
    raw = good_brief(concepts=[full_concept(), concept(name="Plain", awareness="Mild", never="one thing only")])
    brief, _notes = BR.clean_brief(raw, {"IB-0001", "IB-0002"}, {"swoveralls-navy"}, 7)
    a, b = brief["concepts"]
    assert a["alt_hook"] == "Sweatpants that become a whole outfit" and a["awareness"] == "Cold" and a["never"] == ["never say it replaces jeans", "never show a price"] and a["size"] == "9:16 1080x1920"
    assert b["awareness"] == "" and b["never"] == ["one thing only"] and b["alt_hook"] == "" and brief["held_back"] == []                                      # not one of the three levels: dropped, not guessed
    assert "alt_hook" in BR.BRIEF_SYSTEM and "Unaware, Cold or Warm" in BR.BRIEF_SYSTEM and "a different angle and a different hook shape" in BR.BRIEF_SYSTEM


def test_a_concept_with_no_hook_or_over_the_limit_is_held_back_with_its_reason_not_lost():
    raw = good_brief(concepts=[concept(name="Ok"), {"name": "No hook", "hook": "", "script": "x"}, concept(name="Third"), concept(name="Fourth")])
    brief, notes = BR.clean_brief(raw, {"IB-0001"}, {"swoveralls-navy"}, 2)
    assert [c["name"] for c in brief["concepts"]] == ["Ok", "Third"]
    assert brief["held_back"] == [{"name": "No hook", "reason": "no hook was written, so there is nothing to shoot"}, {"name": "Fourth", "reason": "over the limit of 2 concepts for one brief"}] and any("kept the first 2" in n for n in notes)


def test_the_alt_hook_obeys_the_hook_rules_and_the_claims_gate_sees_every_new_text(ruleset):
    brief, _ = BR.clean_brief(good_brief(concepts=[full_concept(alt_hook="Sweatpants overalls for dads", angle="a medical cure")]), {"IB-0001", "IB-0002"}, {"swoveralls-navy"}, 7)
    found = BR.check_brief(brief, ruleset, {"swoveralls-navy": {}}, ())
    assert any(v["where"].endswith("alt hook") and v["rule"] == "banned_phrase" for v in found)                                                  # the 'overalls' hook rule applies to the second hook too

    class Reg:
        def gate(self, text):
            return {"findings": [{"result": "BLOCKED", "sentence": text, "why": "x"}] if "medical" in text else []}
    gated = BR.gate_violations({**brief, "offer": "a medical offer"}, Reg())
    assert {v["where"] for v in gated} == {"concept 1 (Comfort switch) angle", "offer"}


def test_code_fills_in_the_kind_the_recipient_the_one_offer_the_sizes_the_reference_links_and_the_done_when_checklist(ruleset):
    brief, _ = BR.clean_brief(good_brief(concepts=[full_concept(size=""), concept(name="Two")]), {"IB-0001", "IB-0002"}, {"swoveralls-navy"}, 7)
    items = [{"id": "IB-0001", "url": "https://www.instagram.com/p/abc/"}, {"id": "IB-0002", "url": ""}]
    none = BR.finish_brief(brief, "static", " Sarah  Lopez ", "", items, ruleset)
    assert none == [] and brief["kind"] == "static" and brief["for"] == "Sarah Lopez" and brief["offer"] == "" and brief["offer_placement"] == ""
    assert [c["size"] for c in brief["concepts"]] == ["4:5 1080x1350", "4:5 1080x1350"] and brief["concepts"][0]["reference_links"] == [{"id": "IB-0001", "url": "https://www.instagram.com/p/abc/"}]
    assert brief["done_when"][0].startswith("All 2 concept(s) are made") and brief["done_when"][1] == "Every concept is delivered at its own size: 4:5 1080x1350" and brief["done_when"][-1].startswith("Each image keeps its layout")
    allowed = SimpleNamespace(rules={"claim_rules": {"allowed_offers": ["Buy 2+, 30% off. No code needed."]}})
    blank = BR.finish_brief(brief, "video", "", "", items, allowed)
    assert blank == [] and brief["offer"] == "" and brief["offer_placement"] == ""                                                              # the offer comes from the input each time: the rules' only allowed offer is NOT used by default
    one = BR.finish_brief(brief, "video", "", "buy 2+, 30% off. no code needed.", items, allowed)
    assert one == [] and brief["offer"] == "buy 2+, 30% off. no code needed." and brief["offer_placement"] == BR.OFFER_PLACEMENT["video"] and any("30% off" in d and "one place only" in d for d in brief["done_when"])          # checked against the guard list without regard to case
    bad = BR.finish_brief(brief, "video", "", "50% off everything", items, allowed)
    assert bad[0]["rule"] == "offer_not_allowed" and bad[0]["severity"] == "error" and "Buy 2+, 30% off. No code needed." in bad[0]["message"]
    nothing = BR.finish_brief(brief, "video", "", "free shipping", items, ruleset)
    assert "not one of this brief's allowed offers" in nothing[0]["message"] and "free shipping" in nothing[0]["message"]   # the Swoveralls rules now allow the example "30% off" offer, so a different offer is refused by name
    no_offer = replace(ruleset, rules={k: v for k, v in ruleset.rules.items() if k != "claim_rules"})
    nothing_else = BR.finish_brief(brief, "video", "", "free shipping", items, no_offer)
    assert "the rules allow no offer" in nothing_else[0]["message"]   # a brief whose rules allow no offer at all says so plainly
    assert BR.finish_brief(brief, "weird", "", "", items, ruleset) == [] and brief["kind"] == "video"


def test_the_word_document_has_the_recipient_the_offer_an_at_a_glance_table_the_new_rows_held_back_concepts_and_the_checklist(store, swov, ruleset, usable):
    import docx as docx_mod
    first = good_brief(concepts=[full_concept(), full_concept(name="Blocked one", hook="Sweatpants that fix everything")])
    claude = claude_with(first, {"verdict": "pass", "problems": [], "checklist": {}})
    record = make(store, swov, ruleset, {"claude-sonnet-5-5": claude}, for_person="Sarah")
    assert record["content"]["for"] == "Sarah" and record["content"]["kind"] == "video" and record["content"]["concepts"][0]["awareness"] == "Cold" and record["content"]["done_when"]
    blocked = [{"where": "concept 2 (Blocked one) script", "rule": "claims-gate", "severity": "error", "message": "BLOCKED: it fixes everything"}]
    meta = {"brief": "x", "id": "BR-0009", "created": "2026-11-26T00:00:00Z", "ask": "a test"}
    content = dict(record["content"], offer="Buy 2+, 30% off. No code needed.", offer_placement="a sticker on the opening shot", held_back=[{"name": "Third", "reason": "over the limit of 2 concepts for one brief"}])
    data = BR.build_docx(content, swov, meta, [], {}, blocked, ruleset, [])
    import io
    d = docx_mod.Document(io.BytesIO(data))
    text = "\n".join(p.text for p in d.paragraphs)
    cells = " | ".join(c.text for t in d.tables for r in t.rows for c in r.cells)
    assert "For: Sarah" in text and "Buy 2+, 30% off. No code needed.  (shown once, in this place: a sticker on the opening shot)" in text
    assert "At a glance" in text and "What it tests" in cells and "comfort without giving up style (Cold)" in cells and "Alt hook" in cells and "never say it replaces jeans; never show a price" in cells
    assert "Held back" in text and "Blocked one: the claims gate blocked a claim in it: BLOCKED: it fixes everything" in text and "Third: over the limit of 2 concepts for one brief" in text
    assert text.count("Concepts") >= 1 and "2. Blocked one" not in text and "1. Comfort switch" in text                                               # the blocked concept is not handed out to shoot
    assert "[ ] All 2 concept(s) are made" in text and "Done when" in text


def test_a_static_or_carousel_brief_changes_what_the_writer_is_asked_and_a_revision_keeps_its_kind(store, swov, ruleset, usable):
    claude = claude_with(good_brief(concepts=[full_concept(format="static image 4:5", size="")]), {"verdict": "pass", "problems": [], "checklist": {}})
    first = make(store, swov, ruleset, {"claude-sonnet-5-5": claude}, kind="static")
    assert first["content"]["kind"] == "static" and first["content"]["concepts"][0]["size"] == "4:5 1080x1350"
    system, user, _ = claude.json_calls[0]
    assert "STATIC images, not videos" in user and "exact on-image copy" in user and "carousel" not in user.lower().split("static")[1][:0]
    revised = make(store, swov, ruleset, {"claude-sonnet-5-5": claude_with(good_brief(concepts=[full_concept(format="static image 4:5")]), {"verdict": "pass", "problems": [], "checklist": {}})}, revise_of=first)
    assert revised["content"]["kind"] == "static" and revised["revision_of"] == first["id"]
    car = claude_with(good_brief(concepts=[full_concept(format="carousel 4:5", script="Slide 1: a\nSlide 2: b\nSlide 3: c")]), {"verdict": "pass", "problems": [], "checklist": {}})
    make(store, swov, ruleset, {"claude-sonnet-5-5": car}, kind="carousel")
    assert "CAROUSELS" in car.json_calls[0][1] and "numbered (Slide 1: ...)" in car.json_calls[0][1]
    video = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    make(store, swov, ruleset, {"claude-sonnet-5-5": video})
    assert "STATIC images" not in video.json_calls[0][1] and "CAROUSELS" not in video.json_calls[0][1]                                                  # the default is the video brief, unchanged


def test_the_validator_bank_warns_about_missing_production_fields_a_repeated_hook_shape_and_an_alt_hook_that_is_the_same():
    from contentintel import formulas as FM
    from contentintel import validators as VAL
    lib = FM.load_library()
    cost = "$18,000 is what one missing clause cost me."
    bare = {"kind": "video", "concepts": [concept(hook=cost), concept(name="B", hook=cost), concept(name="C", hook=cost)]}
    found = VAL.brief_bank(bare, lib)
    rules = {f["rule"] for f in found}
    assert {"missing_alt_hook", "missing_angle", "missing_awareness", "missing_tests", "same_hook_shape"} <= rules and all(f["severity"] == "warning" for f in found if f["rule"].startswith("missing") or f["rule"] == "same_hook_shape")
    same = VAL.brief_bank({"kind": "video", "concepts": [full_concept(hook=cost, alt_hook=cost)]}, lib)
    assert any(f["rule"] == "alt_hook_same" for f in same) and not any(f["rule"] == "same_hook_shape" for f in same)                                   # one concept cannot repeat itself across concepts
    legacy = VAL.brief_bank({"concepts": [concept(hook=cost)]}, lib)
    assert not any(f["rule"].startswith("missing") or f["rule"] == "alt_hook_same" for f in legacy)                                                    # a brief written before these fields is not nagged


def test_the_offer_is_only_what_the_brief_is_given_and_only_an_allowed_one_reaches_the_writer(store, swov, ruleset, usable):
    allowed = "Buy 2+, 30% off. No code needed."
    import dataclasses
    rules = dataclasses.replace(ruleset, rules={**ruleset.rules, "claim_rules": {**(ruleset.rules.get("claim_rules") or {}), "allowed_offers": [allowed]}})
    ok = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    rec = make(store, swov, rules, {"claude-sonnet-5-5": ok}, offer=allowed)
    assert rec["content"]["offer"] == allowed and f"placed once ({BR.OFFER_PLACEMENT['video']}): {allowed}" in ok.json_calls[0][1]
    none = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    rec2 = make(store, swov, rules, {"claude-sonnet-5-5": none})
    assert rec2["content"]["offer"] == "" and "The ONE offer for this brief" not in none.json_calls[0][1]                                      # one allowed offer is not used unless it is given
    bad = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    rec3 = make(store, swov, rules, {"claude-sonnet-5-5": bad}, offer="50% off everything")
    assert "The ONE offer for this brief" not in bad.json_calls[0][1] and any(v["rule"] == "offer_not_allowed" for v in rec3["violations"]) and rec3["status"] == "needs review"   # an offer outside the guard list never reaches the writer and the brief is flagged
    assert "50%" not in " ".join(allowed_offers for allowed_offers in BR.allowed_offers(rules))


from contentintel import reviews as R_                                                                                                              # noqa: E402


def reviews_for_brief(*rows):
    return [R_.make_review(body, rating=5, reviewer=who, date=date, handle=h, source="judge.me export") for body, who, date, h in rows]


def test_the_brief_offers_reviews_to_the_writer_attributes_a_valid_quote_and_flags_an_invented_one(store, swov, ruleset, usable):
    stored = reviews_for_brief(("So soft and the pockets are huge, I wear it every single day.", "Jess P.", "2026-09-01", "swoveralls-navy"))
    R_.merge(store, "swoveralls", stored, "judge.me export", "2026-11-26T08:00:00Z")
    rid = stored[0]["id"]
    good = claude_with(good_brief(concepts=[concept(quote={"review_id": rid, "text": "the pockets are huge"})]), {"verdict": "pass", "problems": [], "checklist": {}})
    rec = make(store, swov, ruleset, {"claude-sonnet-5-5": good})
    assert f"- {rid}: \"So soft and the pockets are huge, I wear it every single day.\"" in good.json_calls[0][1] and "Customer reviews you may quote." in good.json_calls[0][1]
    assert rec["content"]["concepts"][0]["quote"]["attribution"] == f"Jess P., 2026-09-01 (judge.me export, 5 stars, {rid})" and not [v for v in rec["violations"] if v["rule"].startswith("quote_")]
    import docx as docx_mod
    import io
    meta = {"brief": "x", "id": "BR-1", "created": "2026-11-26T08:00:00Z", "ask": "a"}
    cells = " | ".join(c.text for t in docx_mod.Document(io.BytesIO(BR.build_docx(rec["content"], swov, meta, [], {}, [], ruleset, []))).tables for r in t.rows for c in r.cells)
    assert "Customer quote" in cells and "\"the pockets are huge\"  Jess P., 2026-09-01" in cells
    bad = claude_with(good_brief(concepts=[concept(script='She says "It is the softest thing I have ever worn, truly".')]), {"verdict": "pass", "problems": [], "checklist": {}})
    rec2 = make(store, swov, ruleset, {"claude-sonnet-5-5": bad})
    assert any(v["rule"] == "quote_not_verbatim" and v["severity"] == "error" for v in rec2["violations"]) and rec2["status"] == "needs review"
    assert any("not word for word in any one stored review" in line for line in BR.revision_problems(rec2))                                          # the revision is told exactly what to fix
    empty_store = Store(store.data_dir.parent / "elsewhere")
    assert R_.load(empty_store, "swoveralls") == {"reviews": []}


# ---- cuts briefs: edits of the footage inventory --------------------------------------------------------------------------------------------------------------------------------------------

def stored_footage(store):
    from contentintel import footage as FT_
    files = [{"id": "f1", "name": "zip-demo.mp4", "path": "creators/Sarah", "mime": "video/mp4", "size": 20_000_000, "modified": "2026-11-20T10:00:00Z", "seconds": 20.0, "width": 1080, "height": 1920},
             {"id": "f2", "name": "unboxing.mov", "path": "", "mime": "video/quicktime", "size": 9_000_000, "modified": "2026-11-10T10:00:00Z", "seconds": 12.0, "width": 1080, "height": 1920}]
    clips, ignored = FT_.build_inventory(files)
    FT_.save(store, "swoveralls", clips, ignored, "FOLDER1", "2026-11-27T08:00:00Z")
    return clips


def test_a_cuts_brief_needs_the_footage_inventory_and_says_where_the_clips_go_when_there_is_none(store, swov, ruleset, usable):
    claude = claude_with(good_brief(), {"verdict": "pass", "problems": [], "checklist": {}})
    with pytest.raises(BR.BriefError) as e:
        make(store, swov, ruleset, {"claude-sonnet-5-5": claude}, kind="cuts")
    assert "a cuts brief needs the footage inventory" in str(e.value) and "<Idea Bank folder> / <brand> / Footage" in str(e.value) and "Footage > scan" in str(e.value) and claude.json_calls == []          # the writer is never asked


def test_a_cuts_brief_shows_the_writer_the_inventory_checks_every_clip_and_prints_the_shot_list(store, swov, ruleset, usable):
    import docx as docx_mod
    clips = stored_footage(store)
    by_name = {c["name"]: c["id"] for c in clips}
    a, b = by_name["zip-demo.mp4"], by_name["unboxing.mov"]
    good = claude_with(good_brief(concepts=[full_concept(format="15 second cut", clips=[{"clip": a, "start": 2, "end": 9}, {"clip": b, "start": 0, "end": 8}])]), {"verdict": "pass", "problems": [], "checklist": {}})
    rec = make(store, swov, ruleset, {"claude-sonnet-5-5": good}, kind="cuts")
    system, user, _ = good.json_calls[0]
    assert "CUTS" in user and "Footage you may cut from." in user and f"- {a}: creators/Sarah/zip-demo.mp4 (20 s, vertical" in user
    assert rec["content"]["kind"] == "cuts" and rec["content"]["concepts"][0]["clips"] == [{"clip": a, "start": 2.0, "end": 9.0}, {"clip": b, "start": 0.0, "end": 8.0}] and not [v for v in rec["violations"] if v["rule"].startswith("cuts_")]
    assert set(rec["content"]["footage_used"]) == {a, b} and rec["content"]["footage_used"][a]["name"] == "zip-demo.mp4"
    meta = {"brief": "x", "id": "BR-1", "created": "2026-11-27T08:00:00Z", "ask": "a"}
    cells = " | ".join(c.text for t in docx_mod.Document(io.BytesIO(BR.build_docx(rec["content"], swov, meta, [], {}, [], ruleset, []))).tables for r in t.rows for c in r.cells)
    assert "Cut from" in cells and f"{a}: zip-demo.mp4 (creators/Sarah), 2 to 9 s" in cells and f"{b}: unboxing.mov, 0 to 8 s" in cells
    bad = claude_with(good_brief(concepts=[full_concept(format="15 second cut", clips=[{"clip": a, "start": 15, "end": 25}, {"clip": "FT-nope00", "start": 0, "end": 3}])]), {"verdict": "pass", "problems": [], "checklist": {}})
    rec2 = make(store, swov, ruleset, {"claude-sonnet-5-5": bad}, kind="cuts")
    rules = {v["rule"] for v in rec2["violations"]}
    assert {"cuts_past_the_end", "cuts_unknown_clip"} <= rules and rec2["status"] == "needs review"
    none = claude_with(good_brief(concepts=[full_concept(format="15 second cut")]), {"verdict": "pass", "problems": [], "checklist": {}})
    assert any(v["rule"] == "cuts_no_clips" for v in make(store, swov, ruleset, {"claude-sonnet-5-5": none}, kind="cuts")["violations"])
    assert BR.DEFAULT_SIZE["cuts"] == "9:16 1080x1920" and rec["content"]["done_when"][-1].startswith("Each cut uses only the clips")
