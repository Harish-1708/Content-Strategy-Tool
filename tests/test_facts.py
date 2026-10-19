import pytest

from contentintel.facts import (compute_readiness, extract_facts, extract_materials,
                                extract_pocket_claims, html_to_lines, list_items,
                                merge_manual_facts)


class B:
    """Minimal stand-in for a Brand when only the fact lists matter."""
    def __init__(self, required, expected=()):
        self.required_facts = list(required)
        self.expected_facts = list(expected)


def test_materials_percent_before_the_fibre():
    items = extract_materials("Super Soft Blend of cotton (60%), poly (36%), and spandex (4%)")
    assert {(i["material"], i["percent"]) for i in items} == {("cotton", 60), ("poly", 36), ("spandex", 4)}


def test_materials_percent_after_the_fibre_is_not_double_counted():
    text = ("Details 95% GOTS-Certified Organic Cotton, 5% Spandex "
            "Materials & Care 95% Organic Cotton, 5% Recycled Spandex")
    items = extract_materials(text)
    assert {(i["material"], i["percent"]) for i in items} == {("cotton", 95), ("spandex", 5)}


def test_materials_total_and_consistency_flag():
    facts = extract_facts("<p>65% cotton, 31% recycled polyester, 4% spandex</p>", "apparel")
    value = facts["materials"]["value"]
    assert value["total"] == 100 and value["consistent"] is True
    partial = extract_facts("<p>5% Spandex</p>", "apparel")["materials"]["value"]
    assert partial["total"] == 5 and partial["consistent"] is False


def test_unknown_words_after_a_percent_are_not_invented_as_fibres():
    assert extract_materials("30% off this week, 100% satisfaction") == []


@pytest.mark.parametrize("text,counts", [
    ("they feature six pockets (including a hidden zip one)", [6]),
    ("7 pockets, including a secret invisible one", [7]),
    ("Six pockets and jogger cuffs included", [6]),
    ("SWEATPANTS, BUT WITH 6 DEEP POCKETS AND STRAPS", [6]),
    ("Deep front bib and side pockets", []),
    ("Each one holds a little pocket of warm air against you", []),
    ("Inside-bib, secret front pocket with zipper closure", []),
])
def test_pocket_claims_capture_stated_counts_only(text, counts):
    assert [c["count"] for c in extract_pocket_claims(text)] == counts


def test_apparel_schema_does_not_extract_food_facts_and_vice_versa():
    html = "<p>INGREDIENTS: Cocoa, Sugar. Contains Milk. 6 pockets, 60% cotton</p>"
    apparel = extract_facts(html, "apparel")
    food = extract_facts(html, "food")
    assert "ingredients" not in apparel and "allergens" not in apparel
    assert "materials" not in food and "pocket_claims" not in food


def test_food_ingredients_and_allergens():
    html = ("<p>Peanut Crunch Bite INGREDIENTS: Milk Chocolate (cane sugar, cacao butter), "
            "Dry Roasted Peanuts, Sea Salt. Contains Milk and Peanuts.</p>")
    facts = extract_facts(html, "food")
    assert facts["ingredients"]["value"] == [
        "Milk Chocolate (cane sugar, cacao butter), Dry Roasted Peanuts, Sea Salt"]
    assert facts["allergens"]["value"] == ["Milk and Peanuts"]


def test_variety_pack_keeps_one_ingredient_list_per_flavour():
    html = ("<p>A INGREDIENTS: Dark Chocolate, Buckwheat. Contains Milk. "
            "B INGREDIENTS: Milk Chocolate, Peanuts. Contains Milk and Peanuts.</p>")
    facts = extract_facts(html, "food")
    assert len(facts["ingredients"]["value"]) == 2
    assert facts["allergens"]["value"] == ["Milk", "Milk and Peanuts"]


def test_nutrition_facts_only_when_stated():
    assert "nutrition_facts" not in extract_facts("<p>Whey Crisps (whey protein, tapioca)</p>", "food")
    assert "nutrition_facts" in extract_facts("<p>Nutrition Facts per bite</p>", "food")
    assert "nutrition_facts" in extract_facts("<p>protein: 26g per serving</p>", "food")


def test_html_to_lines_cleans_entities_and_invisible_characters():
    lines = html_to_lines("<p>Soft&nbsp;fabric\u200b</p><ul><li>One\u2060 item</li><li>Two</li></ul>")
    assert lines == ["Soft fabric", "One item", "Two"]


def test_list_items():
    assert list_items("<ul><li><em>A</em> b</li><li>C</li></ul>") == ["A b", "C"]
    assert list_items("<p>none</p>") == []


def test_empty_description_means_no_description_fact():
    assert "description" not in extract_facts("", "apparel")
    assert "description" not in extract_facts(None, "apparel")
    assert "description" in extract_facts("<p>Hello there</p>", "apparel")


def test_images_fact_follows_the_image_count():
    assert "images" not in extract_facts("<p>x</p>", "apparel", image_count=0)
    assert extract_facts("<p>x</p>", "apparel", image_count=3)["images"]["value"] == {"count": 3}


def test_manual_facts_outrank_the_catalogue_and_report_conflicts():
    facts = extract_facts("<p>6 pockets</p>", "apparel")
    merged, conflicts, family = merge_manual_facts(
        facts, {"pocket_claims": [{"count": 7}], "audience": "women", "family": "Robes"})
    assert merged["pocket_claims"]["source"] == "manual"
    assert merged["pocket_claims"]["status"] == "stated_by_user"
    assert merged["audience"]["value"] == "women"
    assert conflicts == [{"fact": "pocket_claims", "catalogue": facts["pocket_claims"]["value"],
                          "manual": [{"count": 7}]}]
    assert family == "Robes"
    assert "audience" not in facts            # input is not modified


def test_manual_fact_with_the_same_value_is_not_a_conflict():
    facts = extract_facts("<p>6 pockets</p>", "apparel")
    _, conflicts, _ = merge_manual_facts(facts, {"pocket_claims": facts["pocket_claims"]["value"]})
    assert conflicts == []


def test_a_manual_description_satisfies_the_required_description():
    facts, _, _ = merge_manual_facts(extract_facts("", "apparel"), {"description": "A robe for women"})
    assert compute_readiness(facts, B(["description"]), "active", False)["state"] == "ready"


def test_readiness_priority_excluded_then_inactive_then_needs_facts_then_ready():
    brand = B(["description"], ["images", "materials"])
    facts = {}
    assert compute_readiness(facts, brand, "active", True)["state"] == "excluded"
    assert compute_readiness(facts, brand, "inactive", False)["state"] == "inactive"
    out = compute_readiness(facts, brand, "active", False)
    assert out["state"] == "needs_facts" and out["missing_required"] == ["description"]
    assert out["missing_expected"] == ["images", "materials"]
    facts = extract_facts("<p>hello</p>", "apparel", image_count=1)
    assert compute_readiness(facts, brand, "coming_soon", False) == {
        "state": "ready", "missing_required": [], "missing_expected": ["materials"]}
