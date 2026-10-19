"""Each classification rule on its own, so no rule hides behind another one."""
import pytest

from contentintel.classify import (audience_hints, classify, family_key, is_excluded,
                                   lifecycle_status, product_flags)


def cfg(**kw):
    base = {
        "exclude": {"product_types": [], "title_patterns": [], "tags": []},
        "active_tag": None,
        "status_rules": {},
        "flags": {},
        "category_by_type": {},
        "audience": {"tags": {}, "title_patterns": {}},
        "family": {},
    }
    base.update(kw)
    return base


def excl(**kw):
    return {"product_types": [], "title_patterns": [], "tags": [], **kw}


def v(price, compare=None):
    return {"price": price, "compare_at_price": compare}


# --- exclusions -------------------------------------------------------------------------

def test_exclusion_by_product_type_alone():
    c = cfg(exclude=excl(product_types=["Gift Card"]))
    assert is_excluded("Anything", "Gift Card", [], c) == "product type 'Gift Card'"
    assert is_excluded("Anything", "gift card", [], c) is not None          # case-insensitive
    assert is_excluded("Anything", "Hat", [], c) is None
    assert is_excluded("Anything", "", [], c) is None


def test_exclusion_by_title_alone():
    c = cfg(exclude=excl(title_patterns=["^gift card$"]))
    assert is_excluded("Gift Card", "", [], c) is not None
    assert is_excluded("Gift Cards for dads", "", [], c) is None


def test_exclusion_by_tag_alone():
    c = cfg(exclude=excl(tags=["hidden"]))
    assert is_excluded("Thing", "", ["Hidden"], c) is not None
    assert is_excluded("Thing", "", ["visible"], c) is None


def test_excluded_products_get_the_excluded_status():
    c = cfg(exclude=excl(product_types=["Gift Card"]), active_tag="Active")
    out = classify("Card", "Gift Card", ["Active"], [v(10)], c)
    assert out["excluded"] is True and out["status"] == "excluded"
    assert out["excluded_reason"]


# --- lifecycle --------------------------------------------------------------------------

def test_coming_soon_by_tag_alone():
    c = cfg(status_rules={"coming_soon": {"tags": ["badge_Coming Soon"], "title_patterns": []}})
    assert lifecycle_status("Plain title", ["badge_Coming Soon"], c) == "coming_soon"
    assert lifecycle_status("Plain title", [], c) == "active"


def test_coming_soon_by_title_alone():
    c = cfg(status_rules={"coming_soon": {"tags": ["badge_Coming Soon"], "title_patterns": ["coming soon"]}})
    assert lifecycle_status("Swoveralls - Dino Navy - Coming Soon!", [], c) == "coming_soon"
    assert lifecycle_status("Swoveralls - Dino Navy", [], c) == "active"


def test_pre_order_by_tag():
    c = cfg(status_rules={"pre_order": {"tags": ["badge_Pre-Order"], "title_patterns": []}})
    assert lifecycle_status("Thing", ["badge_Pre-Order"], c) == "pre_order"


def test_coming_soon_outranks_pre_order_and_inactive():
    c = cfg(active_tag="Active", status_rules={
        "coming_soon": {"tags": ["soon"], "title_patterns": []},
        "pre_order": {"tags": ["pre"], "title_patterns": []}})
    assert lifecycle_status("Thing", ["soon", "pre"], c) == "coming_soon"
    assert lifecycle_status("Thing", ["pre"], c) == "pre_order"


def test_a_missing_active_tag_means_inactive_only_when_the_brand_uses_one():
    assert lifecycle_status("Thing", ["Other"], cfg(active_tag="Active")) == "inactive"
    assert lifecycle_status("Thing", ["active"], cfg(active_tag="Active")) == "active"   # case-insensitive
    assert lifecycle_status("Thing", [], cfg()) == "active"


# --- flags ------------------------------------------------------------------------------

def test_flag_by_tag_alone_and_by_title_alone():
    c = cfg(flags={"limited_edition": {"tags": ["LimitedSW"], "title_patterns": ["limited edition"]}})
    assert product_flags("Plain", ["LimitedSW"], [v(1)], c) == ["limited_edition"]
    assert product_flags("IndoorCamo Limited Edition Robe", [], [v(1)], c) == ["limited_edition"]
    assert product_flags("Plain", [], [v(1)], c) == []


def test_sale_flag_needs_a_compare_price_above_the_price():
    c = cfg()
    assert product_flags("T", [], [v(50, 80)], c) == ["on_sale"]
    assert product_flags("T", [], [v(50, 50)], c) == []
    assert product_flags("T", [], [v(50, None)], c) == []
    assert product_flags("T", [], [v(50), v(50, 80)], c) == ["on_sale"]


def test_flags_are_sorted_and_unique():
    c = cfg(flags={"new": {"tags": ["New", "badge_NEW"], "title_patterns": []},
                   "limited_edition": {"tags": ["LE"], "title_patterns": []}})
    assert product_flags("T", ["New", "badge_NEW", "LE"], [v(1, 2)], c) == [
        "limited_edition", "new", "on_sale"]


# --- audience, category, family ----------------------------------------------------------

def test_audience_hints_from_tags_and_titles_without_duplicates():
    c = cfg(audience={"tags": {"Kids": "kids", "badge_KIDS": "kids", "igg_her": "her"},
                      "title_patterns": {"^Kids\\b": "kids"}})
    assert audience_hints("Kids Swoveralls - Navy", ["Kids", "badge_KIDS", "igg_her"], c) == ["kids", "her"]
    assert audience_hints("Kids Swoveralls - Navy", [], c) == ["kids"]
    assert audience_hints("Swoveralls", [], c) == []


def test_category_comes_from_the_product_type_and_is_none_when_blank():
    c = cfg(category_by_type={"Hat": "merch"})
    assert classify("A", "Hat", [], [v(1)], c)["category"] == "merch"
    assert classify("A", "", [], [v(1)], c)["category"] is None
    assert classify("A", "Unlisted", [], [v(1)], c)["category"] is None


@pytest.mark.parametrize("title,family", [
    ("UFC DudeRobe - Navy", "UFC DudeRobe"),
    ("Swoveralls - Garden Party - Coming Soon!", "Swoveralls"),
    ("Swoveralls -  Stripes Red/White", "Swoveralls"),
    ("Sidekick Tee - Good at Naps - Salt Navy", "Sidekick Tee - Good at Naps"),
    ("The BroThrow", "The BroThrow"),
    ("  Spaced   Title  ", "Spaced Title"),
])
def test_family_key(title, family):
    assert family_key(title, cfg()) == family


def test_family_suffix_patterns_are_configurable():
    c = cfg(family={"strip_suffix_patterns": [r"\s*\(preorder\)$"]})
    assert family_key("Robe - Navy (preorder)", c) == "Robe"
