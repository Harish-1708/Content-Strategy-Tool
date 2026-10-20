import csv
import io
import json

import pytest

from contentintel.export import (EXPORT_NAMES, PRODUCT_COLUMNS, VARIANT_COLUMNS,
                                 build_exports, write_exports)
from contentintel.normalize import normalize_all
from contentintel.store import Store, StoreError

from conftest import raw_products


def records(brand, fixture, mutate=None):
    raw = raw_products(fixture)
    if mutate:
        mutate(raw)
    recs, problems = normalize_all(raw, brand)
    assert problems == []
    return recs


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def swov_exports(swov, swov_records):
    return build_exports("swoveralls", swov_records)


def test_three_exports_are_produced(swov_exports):
    assert tuple(swov_exports) == EXPORT_NAMES


def test_row_counts_match_the_catalogue(swov_exports):
    assert len(rows(swov_exports["products.csv"])) == 72
    assert len(rows(swov_exports["variants.csv"])) == 462
    doc = json.loads(swov_exports["products.json"])
    assert doc["brand"] == "swoveralls" and doc["count"] == 72 and len(doc["products"]) == 72


def test_csv_headers_are_the_documented_columns(swov_exports):
    assert next(csv.reader(io.StringIO(swov_exports["products.csv"]))) == PRODUCT_COLUMNS
    assert next(csv.reader(io.StringIO(swov_exports["variants.csv"]))) == VARIANT_COLUMNS


def test_a_classic_product_row_carries_its_facts(swov_exports):
    row = next(r for r in rows(swov_exports["products.csv"]) if r["handle"] == "swoveralls-midnight-black")
    assert row["price_min"] == row["price_max"] == "99.00"
    assert row["pocket_claims"] == "6"
    assert row["materials"] == "cotton 65%; polyester 31%; spandex 4%"
    assert (row["status"], row["flags"], row["readiness"], row["category"]) == ("active", "", "ready", "core")
    assert row["variant_count"] == "9" and row["in_stock"] == "true" and row["on_sale"] == "false"
    assert row["url"] == "https://swoveralls.com/products/swoveralls-midnight-black"


def test_flags_and_status_are_exported(swov_exports):
    by_handle = {r["handle"]: r for r in rows(swov_exports["products.csv"])}
    cord = by_handle["corduroy-swoveralls-bordeaux"]
    assert "limited_edition" in cord["flags"].split("; ") and cord["pocket_claims"] == ""
    assert sum(r["status"] == "coming_soon" for r in by_handle.values()) == 6


def test_variant_rows_describe_options_price_and_stock(swov_exports):
    variant = next(r for r in rows(swov_exports["variants.csv"])
                   if r["product_title"] == "Swoveralls - Midnight Black" and r["variant_title"].startswith("XXS"))
    assert variant["options"] == "Size=XXS; Color=Midnight Black"
    assert variant["sku"] == "20-XXS-BLK" and variant["price"] == "99.00"
    assert variant["compare_at_price"] == "" and variant["available"] in ("true", "false")


def test_sale_prices_are_exported(swov_exports):
    sale = [r for r in rows(swov_exports["variants.csv"]) if r["compare_at_price"]]
    assert sale and all(float(r["compare_at_price"]) > float(r["price"]) for r in sale)


def test_json_export_has_description_facts_and_variants(swov_exports):
    doc = json.loads(swov_exports["products.json"])
    prod = next(p for p in doc["products"] if p["handle"] == "swoveralls-midnight-black")
    assert "comfiest overalls" in prod["description"]
    assert prod["facts"]["pocket_claims"]["value"][0]["count"] == 6
    assert len(prod["variants"]) == 9 and prod["images"]
    assert prod["flags"] == [] and prod["excluded"] is False and prod["price_min"] == 99.0


def test_products_are_sorted_by_title_for_stable_diffs(swov_exports):
    titles = [r["title"] for r in rows(swov_exports["products.csv"])]
    assert titles == sorted(titles, key=str.lower)


def test_tricky_titles_survive_a_csv_round_trip(dude):
    exports = build_exports("duderobe", records(dude, "duderobe_fetch_a.json"))
    titles = {r["title"] for r in rows(exports["products.csv"])}
    assert "The Hef \u2014 A \u201cSmoking Jacket\u201d Robe" in titles
    assert "DudeRobe Tee - Just Chillin' - Ash Black" in titles
    assert "IndoorCamo\u2122 Limited Edition DudeRobe" in titles


def test_commas_quotes_and_newlines_are_escaped(dude):
    def nasty(raw):
        raw[0]["title"] = 'Robe, "Deluxe"\nEdition'
    exports = build_exports("duderobe", records(dude, "duderobe_fetch_a.json", nasty))
    assert 'Robe, "Deluxe"\nEdition' in {r["title"] for r in rows(exports["products.csv"])}


def test_manual_audience_and_missing_facts_are_visible(dude):
    exports = build_exports("duderobe", records(dude, "duderobe_fetch_a.json"))
    sherobe = next(r for r in rows(exports["products.csv"]) if r["title"] == "The SheRobe")
    assert sherobe["audience_stated"] == "women" and sherobe["audience_hints"] == "her"
    assert sherobe["readiness"] == "needs_facts" and sherobe["missing_required"] == "description"
    doc = json.loads(exports["products.json"])
    prod = next(p for p in doc["products"] if p["title"] == "The SheRobe")
    assert prod["missing_required"] == ["description"] and prod["facts"]["audience"]["source"] == "manual"


def test_food_exports_carry_ingredients_and_allergens(defi):
    exports = build_exports("defi_snacks", records(defi, "defi_snacks.json"))
    by_title = {r["title"]: r for r in rows(exports["products.csv"])}
    pack = by_title["4-Flavor Variety Pack"]
    assert pack["ingredients"].count(" || ") == 3
    assert pack["allergens"] == "Milk; Milk and Peanuts"
    assert by_title["Melting Insurance"]["excluded"] == "true"
    assert all(r["materials"] == "" for r in by_title.values())


def test_exports_hold_no_timestamps_so_two_real_fetches_give_identical_files(dude):
    a = build_exports("duderobe", records(dude, "duderobe_fetch_a.json"))
    b = build_exports("duderobe", records(dude, "duderobe_fetch_b.json"))
    assert a == b


def test_writing_twice_changes_nothing_the_second_time(swov, swov_records, tmp_path):
    store = Store(tmp_path)
    assert write_exports(store, "swoveralls", swov_records) == list(EXPORT_NAMES)
    assert write_exports(store, "swoveralls", swov_records) == []
    assert sorted(p.name for p in (store.dir("swoveralls") / "catalogue").iterdir()) == sorted(EXPORT_NAMES)


def test_a_price_change_rewrites_exactly_the_files_that_show_price(swov, swov_records, tmp_path):
    store = Store(tmp_path)
    write_exports(store, "swoveralls", swov_records)
    def cheaper(raw):
        next(p for p in raw if p["handle"] == "swoveralls-navy")["variants"][0]["price"] = "89.00"
    changed = write_exports(store, "swoveralls", records(swov, "swoveralls_full_72.json", cheaper))
    assert sorted(changed) == sorted(EXPORT_NAMES)


def test_selling_out_one_variant_leaves_the_product_sheet_alone(dude, tmp_path):
    store = Store(tmp_path)
    write_exports(store, "duderobe", records(dude, "duderobe_fetch_a.json"))
    def sell_out(raw):
        next(p for p in raw if p["handle"] == "the-sherobe")["variants"][0]["available"] = False
    changed = write_exports(store, "duderobe", records(dude, "duderobe_fetch_a.json", sell_out))
    assert sorted(changed) == ["products.json", "variants.csv"]


def test_export_names_cannot_escape_the_catalogue_folder(tmp_path):
    store = Store(tmp_path)
    for bad in ("../x.csv", "a/b.csv", ".hidden", "a\\b.csv"):
        with pytest.raises(StoreError):
            store.write_export("swoveralls", bad, "x")


@pytest.mark.parametrize("brand_fixture,slug", [
    ("swoveralls_full_72.json", "swoveralls"),
    ("duderobe_fetch_a.json", "duderobe"),
    ("defi_snacks.json", "defi_snacks"),
])
def test_exports_are_identical_after_a_save_and_reload(brands, brand_fixture, slug):
    """Storage sorts keys. Exports must not depend on dict order, or a quiet day rewrites files."""
    live = records(brands[slug], brand_fixture)
    reloaded = json.loads(json.dumps(live, sort_keys=True))
    assert build_exports(slug, live) == build_exports(slug, reloaded)


def test_size_and_colour_options_keep_the_stores_order(swov_exports):
    variant = next(r for r in rows(swov_exports["variants.csv"]) if r["product_title"] == "Swoveralls - Navy")
    assert variant["options"].startswith("Size=") and "; Color=" in variant["options"]
    doc = json.loads(swov_exports["products.json"])
    prod = next(p for p in doc["products"] if p["handle"] == "swoveralls-navy")
    assert [o["name"] for o in prod["variants"][0]["options"]] == ["Size", "Color"]


def test_the_missing_expected_column_shows_what_a_product_still_lacks(defi, swov_records):
    food = build_exports("defi_snacks", records(defi, "defi_snacks.json"))
    by_title = {r["title"]: r for r in rows(food["products.csv"])}
    assert by_title["Dark Chocolate Crispy Superfood Bite"]["missing_expected"] == "nutrition_facts"
    assert by_title["Melting Insurance"]["missing_expected"] == "allergens; nutrition_facts"
    apparel = {r["handle"]: r for r in rows(build_exports("swoveralls", swov_records)["products.csv"])}
    assert apparel["swoveralls-midnight-black"]["missing_expected"] == ""
    assert apparel["swoveralls-nasa"]["missing_expected"] == "materials; pocket_claims"
    doc = json.loads(build_exports("defi_snacks", records(defi, "defi_snacks.json"))["products.json"])
    assert next(p for p in doc["products"] if p["handle"] == "shipping-protection")["missing_expected"] == [
        "allergens", "nutrition_facts"]
