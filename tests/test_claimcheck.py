"""A brief must not claim anything the brand cannot back up.

Until now the only guard against an invented price, an offer nobody approved, "20g of protein" on a snack that does not say so, or "as seen on" a
programme the brand was never on, was one sentence in the prompt. The claim check turns it into code. These tests keep it honest in both directions:
every kind of unsupported claim is caught, and the same claim is accepted when the catalogue, the claims register or the brief's own allowed list backs it
(a check that cries wolf at real briefs would be switched off). The real hook lines from the Swoveralls and DudeRobe briefs are run through it too."""
import json
from dataclasses import replace

import pytest

from contentintel import brief as BR
from contentintel import claimcheck as CC
from contentintel import ideabank as IB
from contentintel import rules as RU
from contentintel.brands import load_brand
from contentintel.normalize import normalize_all
from contentintel.store import Store

from conftest import BRANDS_DIR, BRIEFS_DIR, FIXTURES
from test_brief import S, banked, claude_with, concept, decisions, good_brief, items_of, products_for
from test_collectors import NOW
from test_sheets import baselined


def ev(text="", prices=(), materials=(), pockets=(), has_materials=None, has_pockets=None):
    e = CC.Evidence()
    e.text = CC.norm(text)
    e.prices = {round(float(p), 2) for p in prices}
    e.materials = {(CC._fibre(f), int(p)) for f, p in materials}
    e.has_materials = bool(materials) if has_materials is None else has_materials
    e.pockets = set(pockets)
    e.has_pockets = bool(pockets) if has_pockets is None else has_pockets
    return e


def rules_of(text, evidence=None, claims=(), claim_rules=None):
    return [(v.rule, v.severity) for v in CC.check_text(text, evidence or ev(), claims, claim_rules)]


def real_records(slug, fixture):
    brand = load_brand(slug, BRANDS_DIR)
    raw = json.load(open(FIXTURES / fixture, encoding="utf-8"))
    recs = normalize_all(raw if isinstance(raw, list) else raw.get("products", raw), brand)[0]
    return brand, list(recs.values()) if isinstance(recs, dict) else recs


# ---- wording -------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [("Gluten-Free", "gluten free"), ("GLUTEN  FREE.", "gluten free"), (" gluten free ", "gluten free"), ("non\u2011GMO", "non gmo"), ("100% Cotton", "100% cotton"), ("It\u2019s", "it s"), ("Rated 4.8.", "rated 4.8")])
def test_wording_is_compared_without_case_dashes_or_spacing(a, b):
    assert CC.norm(a) == CC.norm(b)


# ---- prices ---------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,prices,flagged", [
    ("Only $19.99 today", [19.99], False), ("Only $20 today", [19.99], True), ("It costs 12 dollars", [12], False), ("It costs 12 dollars", [13], True),
    ("\u20b9499 only", [499], False), ("\u20b9499 only", [599], True), ("$1,299.00", [1299], False), ("$64", [64.17], True), ("No price here", [], False),
    ("only 5 bucks", [5], False), ("only 5 bucks", [6], True), ("USD 30", [30], False), ("\u00a329.50", [29.5], False), ("\u00a329.50", [30], True)])
def test_a_price_must_be_a_real_catalogue_price(text, prices, flagged):
    got = [r for r, s in rules_of(text, ev(prices=prices)) if r == "price_not_in_catalogue"]
    assert bool(got) is flagged


def test_the_price_message_lists_what_the_catalogue_says():
    (v,) = CC.check_text("Just $5", ev(prices=[64.17, 120]), (), None)
    assert v.rule == "price_not_in_catalogue" and v.severity == "error" and "$64.17, $120" in v.message and v.detail["match"].strip() == "$5"
    (v,) = CC.check_text("Just $5", ev(), (), None)
    assert "none known" in v.message


def test_a_discount_is_an_offer_not_also_a_wrong_price():
    assert rules_of("Take $10 off today") == [("offer_not_provided", "error")]
    assert rules_of("Take $10 off today", claim_rules={"allowed_offers": ["$10 off"]}) == []


# ---- offers ---------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Get 20% off", "save 15% now", "save up to $30", "Buy one get one free", "BOGO today", "with free shipping", "free delivery", "free returns",
                                  "use code FIRST15", "a promo code inside", "a discount code", "on sale now", "a flash sale", "limited-time offer", "limited time deal",
                                  "a money-back guarantee", "a free trial", "20% OFF the robe", "get two free"])
def test_an_offer_nobody_provided_is_an_error(text):
    assert ("offer_not_provided", "error") in rules_of(text)


def test_an_offer_the_brief_allows_is_fine_and_another_is_still_flagged():
    allowed = {"allowed_offers": ["30% off", "free shipping over $50"]}
    assert rules_of("30% OFF THE DUDEROBE.", claim_rules=allowed) == []
    assert rules_of("Free shipping over $50", ev(), claim_rules=allowed) == []
    assert ("offer_not_provided", "error") in rules_of("20% off", claim_rules=allowed)
    assert ("offer_not_provided", "error") in rules_of("free shipping on everything", claim_rules={"allowed_offers": ["30% off"]})
    assert rules_of("Use the code FIRST15", claim_rules={"allowed_offers": ["use code FIRST15"]}) == []


def test_an_amount_inside_an_allowed_offer_is_not_a_wrong_price():
    assert rules_of("Spend $50 and ship free", claim_rules={"allowed_offers": ["free shipping over $50"]}) == []


# ---- materials and counts -------------------------------------------------------------------------------------------------------------------------------

COTTON = ev(materials=[("cotton", 60), ("poly", 36), ("spandex", 4)])


@pytest.mark.parametrize("text,flagged", [("60% cotton", False), ("36% polyester", False), ("36% poly", False), ("4% spandex", False), ("100% cotton", True),
                                          ("50% cotton", True), ("80% bamboo", True), ("soft cotton", False), ("60% organic cotton", False), ("60% soft cotton", False)])
def test_a_material_percentage_must_match_the_catalogue(text, flagged):
    assert (("material_claim_unsupported", "error") in rules_of(text, COTTON)) is flagged


def test_the_material_message_says_what_the_catalogue_says_or_that_it_says_nothing():
    (v,) = CC.check_text("100% cotton", COTTON, (), None)
    assert "the catalogue says 60% cotton, 36% polyester, 4% spandex" in v.message
    (v,) = CC.check_text("100% cotton", ev(), (), None)
    assert "does not state this product's materials" in v.message


@pytest.mark.parametrize("text,flagged", [("five pockets", False), ("5 pockets", False), ("six pockets", True), ("6 deep pockets", True), ("two zip pockets", True), ("no pockets talk", False)])
def test_a_pocket_count_must_match_the_catalogue(text, flagged):
    assert (("count_claim_unsupported", "error") in rules_of(text, ev(pockets=[5]))) is flagged


def test_a_pocket_count_with_nothing_stated_says_so_and_the_description_can_support_it():
    (v,) = CC.check_text("seven pockets", ev(), (), None)
    assert "does not state the number of pockets" in v.message
    assert rules_of("seven pockets", ev(text="Seven roomy pockets and a hood")) == []


# ---- food ---------------------------------------------------------------------------------------------------------------------------------------------------

SNACK = ev(text="Dark chocolate crispy bite. 5g protein, 150 calories per serving. Gluten-free and vegan. Ingredients: cacao.")


@pytest.mark.parametrize("text,flagged", [("5g of protein", False), ("5 g protein", False), ("20g of protein", True), ("150 calories", False), ("200 calories", True),
                                          ("12g sugar", True), ("300mg sodium", True), ("protein 5g", False), ("9 grams of fibre", True), ("zero grams", False)])
def test_a_nutrition_figure_must_be_stated_in_the_catalogue(text, flagged):
    assert (("nutrition_claim_unsupported", "error") in rules_of(text, SNACK)) is flagged


@pytest.mark.parametrize("text,flagged", [("It is gluten free", False), ("Gluten-Free", False), ("totally vegan", False), ("so keto", True), ("it is organic", True), ("dairy free", True),
                                          ("non-GMO", True), ("kosher", True), ("sugar free", True), ("no added sugar", True), ("high protein", True), ("plant based", True),
                                          ("preservative free", True), ("nut free", True)])
def test_a_dietary_claim_must_be_stated_in_the_catalogue(text, flagged):
    assert (("dietary_claim_unsupported", "error") in rules_of(text, SNACK)) is flagged


def test_a_dietary_claim_is_one_violation_however_often_it_is_repeated():
    assert [r for r, s in rules_of("vegan vegan vegan", ev())] == ["dietary_claim_unsupported"]


@pytest.mark.parametrize("text", ["It cures acne", "boosts your immune system", "clinically proven", "a detox bite", "helps you lose weight", "doctor recommended", "anti-inflammatory",
                                  "reduces stress", "improves sleep", "FDA approved", "heals your gut", "burns fat", "dermatologist tested"])
def test_a_health_claim_is_an_error_unless_the_catalogue_makes_it(text):
    assert ("health_claim_prohibited", "error") in rules_of(text, ev())
    assert ("health_claim_prohibited", "error") not in rules_of(text, ev(text=text))


# ---- proof, results, endorsements, boasts ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,flagged", [("10,000 happy customers", True), ("over 500 reviews", True), ("rated 4.8", True), ("5 stars", True), ("4.9/5", True),
                                          ("thousands of happy customers", True), ("2 people can share", False), ("for 12 people", False), ("5 pockets", False),
                                          ("a 5 star feel", True), ("1,200 orders", True)])
def test_social_proof_nobody_verified_is_an_error(text, flagged):
    assert (("social_proof_unverified", "error") in rules_of(text)) is flagged


@pytest.mark.parametrize("text,flagged", [("results in 7 days", True), ("in 3 weeks", True), ("within a few days", True), ("in 2 months", True), ("results within a week", True),
                                          ("in 30 seconds", False), ("in 2 minutes", False), ("in the morning", False)])
def test_a_promised_result_or_time_is_an_error(text, flagged):
    assert (("result_claim_unproven", "error") in rules_of(text)) is flagged


def test_an_endorsement_must_be_in_the_register():
    assert ("unregistered_claim", "error") in rules_of("As seen on Shark Tank")
    assert ("unregistered_claim", "error") in rules_of("Featured in Vogue") and ("unregistered_claim", "error") in rules_of("recommended by dermatologists")
    assert ("unregistered_claim", "error") in rules_of("voted best loungewear")
    assert rules_of("As seen on Shark Tank", claims=[{"text": "As seen on Shark Tank", "status": "licensed", "guidelines": "received"}]) == []


def test_a_licensed_claim_whose_guidelines_are_missing_is_a_warning_not_an_error():
    register = [{"id": "shark-tank", "text": "As seen on Shark Tank", "status": "licensed", "guidelines": "pending"}]
    got = CC.check_text("Hook: as seen on Shark Tank", ev(), register, None)
    assert [(v.rule, v.severity) for v in got] == [("claim_status", "warn")] and "usage guidelines have not been received" in got[0].message and "shark-tank" in got[0].message


@pytest.mark.parametrize("status", ["prohibited", "expired", "withdrawn", "revoked", "denied"])
def test_a_claim_that_is_no_longer_allowed_is_an_error(status):
    register = [{"id": "x", "text": "Award winning fit", "status": status}]
    assert ("claim_status", "error") in rules_of("Our award winning fit", ev(), register)


def test_a_claim_in_the_register_with_all_in_order_raises_nothing():
    register = [{"id": "x", "text": "Award winning fit", "status": "licensed", "guidelines": "received"}]
    assert rules_of("Our award winning fit", ev(), register) == []


@pytest.mark.parametrize("text", ["best-selling robe", "#1 in comfort", "number one pick", "award winning", "the world's best", "most comfortable ever", "the best sweatpants ever"])
def test_a_boast_is_a_warning(text):
    assert rules_of(text) == [("superlative_unproven", "warn")]


def test_a_boast_the_catalogue_makes_is_not_flagged():
    assert rules_of("best selling robe", ev(text="Our best selling robe")) == []


# ---- the brief's own switches ------------------------------------------------------------------------------------------------------------------------

def test_a_rule_can_be_switched_off_by_the_brief_and_only_that_rule():
    text = "20% off, best-selling, vegan"
    assert {r for r, _ in rules_of(text)} == {"offer_not_provided", "superlative_unproven", "dietary_claim_unsupported"}
    assert {r for r, _ in rules_of(text, claim_rules={"skip": ["superlative_unproven"]})} == {"offer_not_provided", "dietary_claim_unsupported"}


def test_wording_the_brief_allows_is_not_flagged():
    assert rules_of("it is vegan", claim_rules={"allowed_claims": ["vegan"]}) == []
    assert rules_of("it is vegan and keto", claim_rules={"allowed_claims": ["vegan"]}) == [("dietary_claim_unsupported", "error")]


def test_an_empty_text_or_no_rules_is_fine():
    assert CC.check_text("", ev(), (), None) == [] and CC.check_text(None, ev(), (), {}) == []


# ---- what the catalogue supports, from real products ------------------------------------------------------------------------------------------------

def test_evidence_is_built_from_a_real_apparel_product_and_a_real_snack():
    _, apparel = real_records("swoveralls", "swoveralls_full_72.json")
    corduroy = next(r for r in apparel if r["handle"] == "corduroy-swoveralls-bordeaux" or r["handle"] == "corduroy-swoveralls-bordeaux")
    e = CC.evidence_for([corduroy])
    assert e.has_materials and ("cotton", 60) in e.materials and ("polyester", 36) in e.materials and ("spandex", 4) in e.materials and e.prices
    assert rules_of("60% cotton, 36% poly", e) == [] and ("material_claim_unsupported", "error") in rules_of("100% cotton", e)
    _, snacks = real_records("defi_snacks", "defi_snacks.json")
    s = CC.evidence_for([snacks[0]])
    assert 64.17 in s.prices and "himalayan" in s.text and not s.has_materials
    assert ("dietary_claim_unsupported", "error") in rules_of("so gluten free", s) and ("price_not_in_catalogue", "error") in rules_of("just $5", s)
    assert rules_of("only $64.17", s) == []


def test_without_a_product_a_claim_is_supported_if_any_allowed_product_supports_it():
    _, apparel = real_records("swoveralls", "swoveralls_full_72.json")
    allp = CC.evidence_for(apparel)
    assert 22 == len(allp.prices) and allp.has_pockets and {5, 6, 7} <= allp.pockets
    one = CC.evidence_for([apparel[0]])
    assert len(one.prices) < len(allp.prices)


def test_real_hook_lines_from_the_brands_own_briefs_raise_no_false_alarm():
    for slug, products, hooks in (("swoveralls", "swoveralls_full_72.json", "swoveralls_brief_hooks.json"), ("duderobe", "duderobe_fetch_a.json", "duderobe_brief_hooks.json")):
        brand, records = real_records(slug, products)
        everything = CC.evidence_for(records)
        entries = json.load(open(FIXTURES / hooks, encoding="utf-8"))
        entries = entries if isinstance(entries, list) else entries.get("hooks") or list(entries.values())
        rules = RU.load_ruleset(BRIEFS_DIR / {"swoveralls": "swoveralls-new-audience", "duderobe": "duderobe-swipe-to-robe"}[slug] / "rules.yaml").rules.get("claim_rules") or {}
        found = []
        for entry in entries:
            text = entry if isinstance(entry, str) else " ".join(str(v) for v in entry.values() if isinstance(v, str))
            found += [(v.rule, v.detail["match"]) for v in CC.check_text(text, everything, brand.claims, rules)]
        assert found == [], (slug, found)


def test_the_duderobe_offer_is_flagged_without_the_brief_allowing_it_and_allowed_with_it():
    ruleset = RU.load_ruleset(BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml")
    assert ruleset.rules["claim_rules"] == {"allowed_offers": ["30% off"]}
    assert ("offer_not_provided", "error") in rules_of("30% OFF THE DUDEROBE.", claim_rules=None)
    assert rules_of("30% OFF THE DUDEROBE.", claim_rules=ruleset.rules["claim_rules"]) == []


# ---- the rules file -----------------------------------------------------------------------------------------------------------------------------------

def write_rules(tmp_path, claim_block):
    path = tmp_path / "rules.yaml"
    path.write_text("brief: t\nbrand: b\nversion: 1\ntext_rules: {}\nproduct_rules: {}\n" + claim_block, encoding="utf-8")
    return path


def test_a_rules_file_may_carry_claim_rules(tmp_path):
    rs = RU.load_ruleset(write_rules(tmp_path, "claim_rules:\n  allowed_offers: ['20% off']\n  allowed_claims: [vegan]\n  skip: [superlative_unproven]\n"))
    assert rs.rules["claim_rules"] == {"allowed_offers": ["20% off"], "allowed_claims": ["vegan"], "skip": ["superlative_unproven"]}


@pytest.mark.parametrize("block,match", [("claim_rules:\n  allowed_offer: ['x']\n", "claim_rules may only hold"), ("claim_rules:\n  allowed_offers: x\n", "must be a list"),
                                         ("claim_rules:\n  allowed_offers: ['']\n", "must be a list"), ("claim_rules:\n  skip: [not_a_rule]\n", "unknown rules"),
                                         ("claim_rules: [1]\n", "claim_rules may only hold")])
def test_a_bad_claim_rules_section_is_refused_with_a_plain_message(tmp_path, block, match):
    with pytest.raises(RU.RuleError, match=match):
        RU.load_ruleset(write_rules(tmp_path, block))


def test_the_digest_of_an_older_rules_file_is_unchanged_and_a_claim_rule_changes_it(tmp_path):
    assert RU.load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml").digest == "4ac493be2891"
    plain = RU.load_ruleset(write_rules(tmp_path, ""))
    assert "claim_rules" not in plain.rules and RU.load_ruleset(write_rules(tmp_path, "claim_rules:\n  allowed_offers: ['20% off']\n")).digest != plain.digest


# ---- inside a brief --------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def ruleset():
    return RU.load_ruleset(BRIEFS_DIR / "swoveralls-new-audience" / "rules.yaml")


@pytest.fixture
def store(tmp_path, swov):
    s = Store(tmp_path / "data")
    baselined(swov, s, "swoveralls_full_72.json", 72)
    return s


def clean(brief, handles=("swoveralls-navy", "swoveralls-midnight-black", "swoveralls-dark-athletic-grey")):
    return BR.clean_brief(brief, {"IB-0001", "IB-0002", "IB-0003"}, set(handles), 8)[0]


def test_a_brief_that_invents_a_price_an_offer_and_a_claim_is_caught_where_it_says_them(store, swov, ruleset):
    products = products_for(store, ruleset)
    brief = clean(good_brief(key_message="Only $5 today", concepts=[concept(hook="Sweatpants with free shipping", script="It is 100% vegan and cures back pain.", cta="Use code FIRST15"),
                                                                    concept(name="Dad reacts", hook="Dad opens his sweatpants overalls", references=["IB-0002"])]))
    found = BR.check_brief(brief, ruleset, products, swov.claims)
    by_where = {(v["where"], v["rule"]) for v in found}
    assert ("key_message", "price_not_in_catalogue") in by_where and ("concept 1 (Comfort switch) hook", "offer_not_provided") in by_where
    assert ("concept 1 (Comfort switch) script", "dietary_claim_unsupported") in by_where and ("concept 1 (Comfort switch) script", "health_claim_prohibited") in by_where
    assert ("concept 1 (Comfort switch) cta", "offer_not_provided") in by_where and not any(w.startswith("concept 2") for w, _ in by_where)
    assert all(v["severity"] == "error" for v in found if v["rule"] != "superlative_unproven")


def test_what_a_creator_will_say_is_checked_but_instructions_to_the_team_are_not(store, swov, ruleset):
    products = products_for(store, ruleset)
    brief = clean(good_brief(donts=["Never promise 20% off or free shipping"], dos=["Do not say 100% vegan"], deliverables=["No code FIRST15 on screen"], notes_for_creators="No $5 claims"))
    assert BR.check_brief(brief, ruleset, products, swov.claims) == []
    spoken = clean(good_brief(concepts=[concept(hook="Take 20% off", references=["IB-0001"])]))
    assert [v["rule"] for v in BR.check_brief(spoken, ruleset, products, swov.claims)] == ["offer_not_provided"]


def test_a_claim_is_checked_against_the_concepts_own_product(store, swov, ruleset):
    products = products_for(store, ruleset)
    navy = products["swoveralls-navy"]
    price = sorted(CC.Evidence().absorb(navy).prices)[0]
    black_only = {p for p in CC.Evidence().absorb(products["swoveralls-midnight-black"]).prices} - CC.Evidence().absorb(navy).prices
    ok = clean(good_brief(concepts=[concept(hook=f"Sweatpants for ${price:g}")]))
    assert [v["rule"] for v in BR.check_brief(ok, ruleset, products, swov.claims) if v["rule"] == "price_not_in_catalogue"] == []
    if black_only:
        other = clean(good_brief(concepts=[concept(hook=f"Sweatpants for ${sorted(black_only)[0]:g}")]))
        assert "price_not_in_catalogue" in [v["rule"] for v in BR.check_brief(other, ruleset, products, swov.claims)]


def test_the_brand_register_is_used_inside_a_brief():
    brand = load_brand("duderobe", BRANDS_DIR)
    ruleset = RU.load_ruleset(BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml")
    brief = BR.clean_brief({"concepts": [{"hook": "As seen on Shark Tank: the DudeRobe", "references": ["IB-0001"]}, {"hook": "30% OFF THE DUDEROBE.", "references": ["IB-0001"]},
                                         {"hook": "Featured in Vogue", "references": ["IB-0001"]}]}, {"IB-0001"}, set(), 8)[0]
    found = [(v["where"], v["rule"], v["severity"]) for v in BR.check_brief(brief, ruleset, {}, brand.claims)]
    assert ("concept 1 (Concept 1) hook", "claim_status", "warn") in found and not any(w.startswith("concept 2") for w, _, _ in found)
    assert ("concept 3 (Concept 3) hook", "unregistered_claim", "error") in found and ("concept 1 (Concept 1) hook", "unregistered_claim", "error") not in found


def write(store, swov, ruleset, claude, cap=100):
    budget = IB.ClaudeBudget(store, NOW, cap)
    return BR.write_brief(claude, swov, "Father's Day", items_of(store), products_for(store, ruleset), ruleset, S, budget)


def test_a_repair_pass_is_asked_to_remove_the_invented_claims_and_the_final_brief_is_clean(store, swov, ruleset):
    banked(store)
    invented = good_brief(concepts=[concept(hook="Sweatpants for just $5", script="Say it is 100% vegan"), concept(name="Dad reacts", hook="Dad opens his sweatpants overalls", references=["IB-0002"])])
    claude = claude_with(invented, good_brief())
    brief, violations, notes, calls = write(store, swov, ruleset, claude)
    assert calls == 2 and violations == [] and "one repair pass: 0 rule problem(s) left" in notes
    system, user, _ = claude.json_calls[1]
    assert system == BR.REPAIR_SYSTEM and "price '$5' is not a catalogue price" in user and "(vegan) is not stated in the catalogue" in user and "Facts you may state" in user


def test_a_repair_that_makes_the_claims_worse_is_not_used(store, swov, ruleset):
    banked(store)
    first = good_brief(concepts=[concept(hook="Sweatpants for just $5")])
    worse = good_brief(concepts=[concept(hook="Sweatpants for just $5 with free shipping and 20% off")])
    brief, violations, notes, calls = write(store, swov, ruleset, claude_with(first, worse))
    assert "the repair made the brief worse and was not used" in notes and brief["concepts"][0]["hook"] == "Sweatpants for just $5"


def test_a_claim_error_is_penalised_in_the_bake_off_and_a_boast_is_not():
    err = {"model": "a", "brief": {}, "violations": [{"severity": "error", "rule": "price_not_in_catalogue", "where": "x", "message": "m"}]}
    boast = {"model": "b", "brief": {}, "violations": [{"severity": "warn", "rule": "superlative_unproven", "where": "x", "message": "m"}]}
    best, notes = BR.pick_best([err, boast], None, IB.ClaudeBudget.__new__(IB.ClaudeBudget), {"a", "b"})
    assert best == 1


# ---- what the model is told it may say ----------------------------------------------------------------------------------------------------------------

def test_the_prompt_lists_the_facts_the_offers_and_the_claims_the_model_may_use(store, swov, ruleset):
    products = products_for(store, ruleset)
    items = BR.approved_items(banked(store), decisions(**{"IB-0001": "use"}))
    prompt = BR.build_prompt(swov, "Father's Day", items, products, ruleset, 6)
    assert "Facts you may state (state NO other fact, price, number, offer, ingredient, dietary or health claim, endorsement or result):" in prompt
    assert "- swoveralls-navy: " in prompt and "Allowed offers: none: state no offer, discount, code, free shipping or guarantee" in prompt and "Approved claims you may use word for word: none" in prompt
    allowed = replace(ruleset, rules={**ruleset.rules, "claim_rules": {"allowed_offers": ["30% off", "free shipping over $50"], "allowed_claims": ["machine washable"]}})
    text = BR.claim_lines(swov, allowed, products)
    assert "Allowed offers: 30% off; free shipping over $50" in text and "Other allowed wording: machine washable" in text


def test_the_facts_line_states_only_what_the_catalogue_states():
    record = {"facts": {"materials": {"value": {"items": [{"material": "cotton", "percent": 60}, {"material": "poly", "percent": 40}]}},
                        "pocket_claims": {"value": [{"count": 5, "phrase": "five pockets"}]}, "ingredients": {"value": ["x"]}}}
    assert BR.facts_line("h", record) == "- h: materials 60% cotton, 40% polyester; pockets 5; the description states its ingredients"
    assert BR.facts_line("h", {"facts": {}}) == "- h: no materials, counts or nutrition are stated"
    assert "ingredients and allergens" in BR.facts_line("h", {"facts": {"ingredients": {}, "allergens": {}}})


def test_the_approved_claims_in_the_prompt_are_only_the_licensed_ones():
    brand = replace(load_brand("duderobe", BRANDS_DIR), claims=[{"id": "a", "text": "As seen on Shark Tank", "status": "licensed"}, {"id": "b", "text": "Voted best robe", "status": "prohibited"},
                                                                {"id": "c", "text": "Pending claim", "status": "pending"}])
    ruleset = RU.load_ruleset(BRIEFS_DIR / "duderobe-swipe-to-robe" / "rules.yaml")
    text = BR.claim_lines(brand, ruleset, {})
    assert "Approved claims you may use word for word: As seen on Shark Tank" in text and "Voted best robe" not in text and "Pending claim" not in text
    assert "Allowed offers: 30% off" in text and "- (none)" in text


# ---- documentation ---------------------------------------------------------------------------------------------------------------------------------------

def test_the_readme_explains_the_claim_check_and_how_to_allow_an_offer():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("claim check", "claim_rules", "allowed_offers", "allowed_claims", "price_not_in_catalogue", "offer_not_provided", "the approved-claims register", "never invented"):
        assert needle in text, needle


# ---- a few more places a mistake could hide ----------------------------------------------------------------------------------------------------------

def test_a_crossed_out_compare_at_price_is_a_real_catalogue_price_too():
    e = CC.Evidence().absorb({"variants": [{"price": 10.0, "compare_at_price": 15.0}, {"price": "12.50", "compare_at_price": None}, {"price": 0}, "junk"]})
    assert e.prices == {10.0, 15.0, 12.5} and rules_of("Was $15, now $10", e) == []
    assert ("price_not_in_catalogue", "error") in rules_of("Now $11", e)


def test_fibre_and_fiber_are_the_same_nutrient():
    e = ev(text="9g fiber per serving")
    assert rules_of("9g of fibre", e) == [] and rules_of("9g of fiber", e) == [] and ("nutrition_claim_unsupported", "error") in rules_of("10g of fibre", e)
    assert rules_of("12 grams carbs", ev(text="12g carbohydrates")) == []


def test_bad_facts_in_a_record_are_ignored_not_trusted():
    e = CC.Evidence().absorb({"facts": {"materials": {"value": {"items": [{"material": "cotton", "percent": None}, "junk"]}}, "pocket_claims": {"value": [{"count": "x"}, "junk"]}}})
    assert not e.has_materials and not e.has_pockets and e.materials == set() and e.pockets == set()
    assert CC.Evidence().absorb({}).text.strip() == "" and CC.Evidence().absorb({"facts": {"materials": "junk"}}).has_materials is False


def test_the_register_is_used_while_a_brief_is_written_and_repaired(store, swov, ruleset):
    banked(store)
    brand = replace(swov, claims=[{"id": "dad", "text": "Dad approved", "status": "prohibited"}])
    bad = good_brief(concepts=[concept(hook="Sweatpants that are Dad approved"), concept(name="Dad reacts", hook="Dad opens his sweatpants overalls", references=["IB-0002"])])
    budget = IB.ClaudeBudget(store, NOW, 100)
    brief, violations, notes, calls = BR.write_brief(claude_with(bad, good_brief()), brand, "Father's Day", items_of(store), products_for(store, ruleset), ruleset, S, budget)
    assert calls == 2 and violations == [] and "one repair pass: 0 rule problem(s) left" in notes
    budget = IB.ClaudeBudget(store, NOW, 100)
    brief, violations, notes, calls = BR.write_brief(claude_with(bad, bad), brand, "Father's Day", items_of(store), products_for(store, ruleset), ruleset, S, budget)
    assert [v["rule"] for v in violations] == ["claim_status"] and "one repair pass: 1 rule problem(s) left" in notes


def test_a_proof_a_result_or_an_endorsement_the_catalogue_itself_states_is_supported():
    assert rules_of("over 500 reviews", ev(text="Loved by over 500 reviews")) == [] and ("social_proof_unverified", "error") in rules_of("over 500 reviews", ev(text="Loved by many"))
    assert rules_of("see results in 7 days", ev(text="Visible results in 7 days")) == [] and ("result_claim_unproven", "error") in rules_of("see results in 7 days", ev(text="Soft fabric"))
    assert rules_of("as seen on Vogue", ev(text="As seen on Vogue")) == [] and ("unregistered_claim", "error") in rules_of("as seen on Vogue", ev(text="Soft fabric"))


def test_a_concepts_claim_is_judged_against_its_own_product_and_not_another(tmp_path):
    ruleset = RU.load_ruleset(write_rules(tmp_path, ""))
    products = {"a": {"handle": "a", "title": "A", "status": "active", "flags": [], "variants": [{"price": 10}], "facts": {}},
                "b": {"handle": "b", "title": "B", "status": "active", "flags": [], "variants": [{"price": 20}], "facts": {}}}
    for product, hook, flagged in (("a", "Only $10", False), ("a", "Only $20", True), ("b", "Only $20", False), ("b", "Only $10", True)):
        brief = BR.clean_brief({"concepts": [{"hook": hook, "product": product, "references": ["IB-0001"]}]}, {"IB-0001"}, {"a", "b"}, 8)[0]
        found = [v["rule"] for v in BR.check_brief(brief, ruleset, products, ())]
        assert ("price_not_in_catalogue" in found) is flagged, (product, hook)
    brief = BR.clean_brief({"concepts": [{"hook": "Only $20", "references": ["IB-0001"]}]}, {"IB-0001"}, {"a", "b"}, 8)[0]
    assert BR.check_brief(brief, ruleset, products, ()) == []                           # no product named: any allowed product's price will do
