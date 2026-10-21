"""Kids products must be filed as kids even when Shopify's product type says otherwise.

Swoveralls files two Yo Gabba Gabba kids products (GooBle Search, Kammy Kubes) under the product type
"Swoveralls", so the type alone made them "core". The brand's own audience rules (a Kids tag, a badge_KIDS tag,
or a title starting with "Kids") say who a product is for, and that now decides the category.
Runs on the real 72-product catalogue and the real brand file.
"""
import copy

import pytest
import yaml

from contentintel.brands import BrandError, load_brand
from contentintel.classify import classify, product_category
from contentintel.diff import diff_snapshots
from contentintel.normalize import normalize_all

from conftest import BRANDS_DIR, raw_products

KIDS_TYPED_WRONG = {"kids-swoveralls-gooble-search", "kids-swoveralls-kammy-kubes"}


@pytest.fixture(scope="module")
def swoveralls():
    return load_brand("swoveralls", BRANDS_DIR)


@pytest.fixture(scope="module")
def records(swoveralls):
    recs, problems = normalize_all(raw_products("swoveralls_full_72.json"), swoveralls)
    assert problems == []
    return {r["handle"]: r for r in recs.values()}


def test_the_two_known_kids_products_are_kids(records):
    for handle in KIDS_TYPED_WRONG:
        assert records[handle]["category"] == "kids", handle
        assert records[handle]["audience_hints"] == ["kids"], handle


def test_the_trap_is_real_these_two_have_the_adult_product_type(records):
    assert {records[h]["product_type"] for h in KIDS_TYPED_WRONG} == {"Swoveralls"}


def test_every_kids_product_is_kids_and_nothing_else_is(records):
    kids = {h for h, r in records.items() if r["title"].startswith("Kids ")}
    assert len(kids) == 6
    assert {h for h, r in records.items() if r["category"] == "kids"} == kids


def test_adult_versions_of_the_same_prints_stay_core(records):
    for handle in ("swoveralls-gooble-search", "swoveralls-kammy-kubes"):
        assert records[handle]["category"] == "core", handle
        assert records[handle]["audience_hints"] == []


def test_other_categories_are_untouched(records):
    cats = {}
    for r in records.values():
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    assert cats == {"core": 42, "kids": 6, None: 3, "merch": 21}      # before the fix: core 44, kids 4


def test_price_band_of_core_no_longer_includes_kids_prices(swoveralls):
    from contentintel.autoseeds import brand_profile
    recs, _ = normalize_all(raw_products("swoveralls_full_72.json"), swoveralls)
    snapshot = {"products": recs}
    profile = brand_profile(swoveralls, snapshot)
    assert profile["categories"]["core"]["price_low"] >= 89           # was 43.99 with the two kids products in core
    assert profile["categories"]["kids"]["price_low"] == 43.99
    assert profile["categories"]["kids"]["products"] == 6


def test_the_change_shows_up_once_as_a_category_change_and_only_for_those_two(swoveralls):
    old_cfg = copy.deepcopy(swoveralls.catalogue)
    old_cfg["category_by_audience"] = {}
    raw = raw_products("swoveralls_full_72.json")
    before = {r["product_id"]: r for r in normalize_all(raw, type(swoveralls)(**{**swoveralls.__dict__, "catalogue": old_cfg}))[0].values()}
    after = {r["product_id"]: r for r in normalize_all(raw, swoveralls)[0].values()}
    events, _ = diff_snapshots(before, after, {pid: {"handle": r["handle"], "title": r["title"], "first_seen": "x",
                                                      "presence": "present"} for pid, r in before.items()}, "now")
    assert {e["handle"] for e in events} == KIDS_TYPED_WRONG
    for e in events:                                # one ordinary "changed" entry per product: category core -> kids
        assert e["type"] == "changed", e["type"]
        assert e["details"]["category"] == {"old": "core", "new": "kids"}
        assert set(e["details"]) == {"category"}


# ---- the rule itself, on small made-up configs ------------------------------------------

def cfg(**kw):
    base = {"exclude": {"product_types": [], "title_patterns": [], "tags": []}, "active_tag": None, "status_rules": {},
            "flags": {}, "category_by_type": {"Swoveralls": "core", "Youth": "kids"},
            "category_by_audience": {"kids": "kids"}, "audience": {"tags": {"Kids": "kids"}, "title_patterns": {}},
            "family": {}}
    base.update(kw)
    return base


def test_audience_beats_product_type():
    assert classify("Thing", "Swoveralls", ["Kids"], [], cfg())["category"] == "kids"


def test_without_an_audience_hint_the_product_type_decides():
    assert classify("Thing", "Swoveralls", [], [], cfg())["category"] == "core"


def test_a_missing_setting_changes_nothing():
    c = cfg()
    del c["category_by_audience"]
    assert classify("Thing", "Swoveralls", ["Kids"], [], c)["category"] == "core"


def test_an_audience_with_no_category_rule_is_ignored():
    c = cfg(audience={"tags": {"Kids": "kids", "Mens": "adult"}, "title_patterns": {}})
    assert classify("Thing", "Swoveralls", ["Mens"], [], c)["category"] == "core"


def test_first_matching_audience_in_rule_order_wins():
    c = cfg(category_by_audience={"kids": "kids", "adult": "adult"},
            audience={"tags": {"Mens": "adult", "Kids": "kids"}, "title_patterns": {}})
    assert product_category("", ["adult", "kids"], c) == "adult"
    assert product_category("", ["kids", "adult"], c) == "kids"


def test_blank_product_type_with_a_kids_tag_is_kids():
    assert classify("Thing", "", ["Kids"], [], cfg())["category"] == "kids"


# ---- the brand file is checked when it is loaded ----------------------------------------

def write_brand(tmp_path, **catalogue):
    folder = tmp_path / "t"
    folder.mkdir()
    (folder / "brand.yaml").write_text(yaml.safe_dump({
        "slug": "t", "name": "T", "platform": "shopify", "domain": "t.example", "category_schema": "apparel",
        "catalogue": catalogue}), encoding="utf-8")
    return tmp_path


def test_a_rule_naming_an_audience_nothing_produces_is_rejected(tmp_path):
    root = write_brand(tmp_path, category_by_audience={"kids": "kids"})
    with pytest.raises(BrandError, match="no audience rule produces"):
        load_brand("t", root)


def test_a_rule_with_a_blank_category_is_rejected(tmp_path):
    root = write_brand(tmp_path, category_by_audience={"kids": ""}, audience={"tags": {"Kids": "kids"}})
    with pytest.raises(BrandError, match="category_by_audience"):
        load_brand("t", root)


def test_a_valid_rule_loads(tmp_path):
    root = write_brand(tmp_path, category_by_audience={"kids": "kids"}, audience={"tags": {"Kids": "kids"}})
    assert load_brand("t", root).catalogue["category_by_audience"] == {"kids": "kids"}


def test_the_other_two_brands_are_unaffected(brands):
    assert brands["duderobe"].catalogue["category_by_audience"] == {}
    assert brands["defi_snacks"].catalogue["category_by_audience"] == {}
