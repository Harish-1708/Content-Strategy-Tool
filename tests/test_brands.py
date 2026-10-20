import pytest
import yaml

from contentintel.brands import (BrandError, brand_data_dir, list_brand_slugs, load_all,
                                 load_brand, validate_slug)


def write_brand(tmp_path, slug, **overrides):
    data = {"slug": slug, "name": "T", "platform": "shopify", "domain": "t.example",
            "category_schema": "apparel"}
    data.update(overrides)
    folder = tmp_path / slug
    folder.mkdir()
    (folder / "brand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_all_three_brands_load(brands):
    assert sorted(brands) == ["defi_snacks", "duderobe", "swoveralls"]
    assert brands["swoveralls"].category_schema == "apparel"
    assert brands["defi_snacks"].category_schema == "food"


def test_brands_carry_their_own_fact_requirements(brands):
    assert brands["defi_snacks"].required_facts == ["description", "ingredients"]
    assert brands["swoveralls"].required_facts == ["description"]
    assert "nutrition_facts" in brands["defi_snacks"].expected_facts
    assert "nutrition_facts" not in brands["swoveralls"].expected_facts


def test_manual_facts_and_claims_are_loaded_only_for_their_brand(brands):
    assert brands["duderobe"].manual_facts["the-sherobe"] == {"audience": "women"}
    assert brands["swoveralls"].manual_facts == {}
    claim = brands["duderobe"].claims[0]
    assert claim["id"] == "shark-tank" and claim["guidelines"] == "pending"
    assert brands["defi_snacks"].claims == []


def test_gate_defaults_are_filled_in_and_overridable(tmp_path):
    root = write_brand(tmp_path, "tiny", gate={"fail_drop": 0.5})
    brand = load_brand("tiny", root)
    assert brand.gate["fail_drop"] == 0.5
    assert brand.gate["warn_change"] == 0.10


@pytest.mark.parametrize("slug", ["../x", "a/b", "A", "", "x" * 60, ".hidden", "a b"])
def test_unsafe_slugs_are_rejected(slug):
    with pytest.raises(BrandError):
        validate_slug(slug)


def test_data_dir_stays_inside_the_data_folder(tmp_path):
    path = brand_data_dir(tmp_path, "swoveralls")
    assert path.parent == tmp_path.resolve()
    with pytest.raises(BrandError):
        brand_data_dir(tmp_path, "../escape")


def test_two_brands_never_share_a_data_folder(tmp_path):
    assert brand_data_dir(tmp_path, "swoveralls") != brand_data_dir(tmp_path, "duderobe")


def test_slug_must_match_its_folder(tmp_path):
    root = write_brand(tmp_path, "alpha")
    (root / "alpha" / "brand.yaml").write_text(yaml.safe_dump(
        {"slug": "beta", "name": "T", "platform": "shopify", "domain": "t.example",
         "category_schema": "apparel"}), encoding="utf-8")
    with pytest.raises(BrandError, match="slug"):
        load_brand("alpha", root)


@pytest.mark.parametrize("bad", [
    {"surprise": 1},
    {"gate": {"nonsense": 1}},
    {"platform": "woocommerce"},
    {"catalogue": {"unknown_key": 1}},
    {"catalogue": {"exclude": {"colours": []}}},
    {"catalogue": {"status_rules": {"retired": {}}}},
    {"catalogue": {"exclude": {"title_patterns": ["("]}}},
    {"domain": ""},
])
def test_invalid_profiles_are_rejected(tmp_path, bad):
    root = write_brand(tmp_path, "bad", **bad)
    with pytest.raises(BrandError):
        load_brand("bad", root)


def test_missing_profile_raises(tmp_path):
    with pytest.raises(BrandError, match="No brand profile"):
        load_brand("ghost", tmp_path)


def test_listing_ignores_folders_without_a_profile(tmp_path):
    write_brand(tmp_path, "real")
    (tmp_path / "empty").mkdir()
    assert list_brand_slugs(tmp_path) == ["real"]
    assert list(load_all(tmp_path)) == ["real"]
    assert list_brand_slugs(tmp_path / "missing") == []


def write_manual(tmp_path, slug, products):
    root = write_brand(tmp_path, slug)
    (root / slug / "manual_facts.yaml").write_text(yaml.safe_dump({"products": products}), encoding="utf-8")
    return root


@pytest.mark.parametrize("status", ["active", "inactive", "coming_soon", "pre_order"])
def test_valid_manual_statuses_are_accepted(tmp_path, status):
    root = write_manual(tmp_path, "okay", {"a-product": {"status": status, "audience": "all"}})
    assert load_brand("okay", root).manual_facts["a-product"]["status"] == status


@pytest.mark.parametrize("bad", ["live", "Active", "", None, 3])
def test_unknown_manual_statuses_are_rejected(tmp_path, bad):
    root = write_manual(tmp_path, "bad", {"a-product": {"status": bad}})
    with pytest.raises(BrandError, match="status"):
        load_brand("bad", root)


def test_manual_facts_must_be_name_value_pairs(tmp_path):
    root = write_manual(tmp_path, "bad", {"a-product": "active"})
    with pytest.raises(BrandError, match="name: value"):
        load_brand("bad", root)


def test_loading_a_brand_with_manual_facts_keeps_its_required_facts(tmp_path):
    root = write_manual(tmp_path, "okay", {"a-product": {"audience": "all"}})
    brand = write_and_load_with_facts(root)
    assert brand.required_facts == ["description"] and brand.expected_facts == ["images"]


def write_and_load_with_facts(root):
    path = root / "okay" / "brand.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["facts"] = {"required": ["description"], "expected": ["images"]}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_brand("okay", root)
