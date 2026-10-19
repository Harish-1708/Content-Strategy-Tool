import copy

from contentintel.normalize import (content_hash, image_key, normalize_all,
                                    normalize_product)

from conftest import by_handle, by_title, raw_products


def records_for(brand, fixture):
    records, problems = normalize_all(raw_products(fixture), brand)
    assert problems == []
    return records


def test_catalogue_sizes_match_what_was_checked_by_hand(swov, dude, defi):
    sw = records_for(swov, "swoveralls_full_72.json")
    dr = records_for(dude, "duderobe_fetch_a.json")
    df = records_for(defi, "defi_snacks.json")
    assert (len(sw), len(dr), len(df)) == (72, 25, 7)
    assert sum(len(r["variants"]) for r in sw.values()) == 462
    assert sum(len(r["variants"]) for r in dr.values()) == 176
    assert sum(len(r["variants"]) for r in df.values()) == 21


def test_every_record_belongs_to_its_brand(swov, dude):
    assert {r["brand"] for r in records_for(swov, "swoveralls_full_72.json").values()} == {"swoveralls"}
    assert {r["brand"] for r in records_for(dude, "duderobe_fetch_a.json").values()} == {"duderobe"}


def test_swoveralls_lifecycle_and_flags(swov_records):
    classic = by_title(swov_records, "Swoveralls - Midnight Black")
    assert classic["status"] == "active" and classic["flags"] == []
    corduroy = by_title(swov_records, "Corduroy Swoveralls - Bordeaux")
    assert corduroy["status"] == "active"
    assert {"limited_edition", "new"} <= set(corduroy["flags"])
    soon = by_title(swov_records, "Swoveralls - Garden Party - Coming Soon!")
    assert soon["status"] == "coming_soon"
    assert by_title(swov_records, "Swoveralls - Duck Camo")["status"] == "inactive"   # no Active tag
    assert by_title(swov_records, "Swoveralls Gift Card")["excluded"] is True


def test_swoveralls_status_totals(swov_records):
    counts = {}
    for r in swov_records.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    assert counts == {"active": 54, "inactive": 8, "coming_soon": 6, "pre_order": 1, "excluded": 3}
    assert sum("limited_edition" in r["flags"] for r in swov_records.values()) == 29


def test_every_classic_core_product_is_unflagged_and_active(swov_records):
    core = [r for r in swov_records.values() if "Core" in r["tags"] and r["product_type"] == "Swoveralls"]
    assert len(core) == 5
    assert all(r["status"] == "active" and "limited_edition" not in r["flags"] for r in core)


def test_kids_products_filed_under_swoveralls_are_still_recognised(swov_records):
    kammy = by_title(swov_records, "Kids Swoveralls - Kammy Kubes")
    assert kammy["product_type"] == "Swoveralls" and kammy["audience_hints"] == ["kids"]
    assert by_title(swov_records, "Kids Swoveralls - Navy")["category"] == "kids"


def test_pocket_claims_differ_by_product(swov_records):
    assert [c["count"] for c in by_title(swov_records, "Swoveralls - Midnight Black")["facts"]["pocket_claims"]["value"]] == [6]
    assert [c["count"] for c in by_title(swov_records, "Comfyalls - Navy")["facts"]["pocket_claims"]["value"]] == [7]
    assert "pocket_claims" not in by_title(swov_records, "Corduroy Swoveralls - Bordeaux")["facts"]


def test_every_extracted_blend_adds_up_to_one_hundred(swov_records):
    blends = [r["facts"]["materials"]["value"] for r in swov_records.values() if "materials" in r["facts"]]
    assert len(blends) == 49
    assert all(b["total"] == 100 and b["consistent"] for b in blends)
    corduroy = by_title(swov_records, "Corduroy Swoveralls - Bordeaux")["facts"]["materials"]["value"]
    assert {(i["material"], i["percent"]) for i in corduroy["items"]} == {
        ("cotton", 60), ("poly", 36), ("spandex", 4)}


def test_sale_flag_follows_compare_at_prices(swov_records):
    on_sale = [r for r in swov_records.values() if "on_sale" in r["flags"]]
    assert len(on_sale) == 7
    for r in on_sale:
        assert any(v["compare_at_price"] and v["compare_at_price"] > v["price"] for v in r["variants"])


def test_families_group_products_sold_per_colour(dude, swov_records):
    dr = records_for(dude, "duderobe_fetch_a.json")
    ufc = [r for r in dr.values() if r["title"].startswith("UFC DudeRobe")]
    assert len(ufc) == 3 and {r["family"] for r in ufc} == {"UFC DudeRobe"}
    assert by_title(swov_records, "Swoveralls - Garden Party - Coming Soon!")["family"] == "Swoveralls"
    assert by_title(swov_records, "Corduroy Swoveralls - Bordeaux")["family"] == "Corduroy Swoveralls"


def test_variants_map_option_names_and_images_drop_the_cdn_version(swov_records):
    rec = by_title(swov_records, "Swoveralls - Midnight Black")
    assert rec["variants"][0]["options"] == {"Size": "XXS", "Color": "Midnight Black"}
    assert rec["variants"][0]["price"] == 99.0
    assert all("?" not in img["key"] for img in rec["images"])
    assert image_key("https://cdn.x/y.jpg?v=123") == "https://cdn.x/y.jpg"


def test_duderobe_products_without_a_description_need_facts(dude):
    dr = records_for(dude, "duderobe_fetch_a.json")
    for title in ("The SheRobe", "The BroThrow", "The Couple Bundle"):
        rec = by_title(dr, title)
        assert rec["readiness"]["state"] == "needs_facts", title
        assert rec["readiness"]["missing_required"] == ["description"]
    assert by_title(dr, "The DudeRobe")["readiness"]["state"] == "ready"


def test_manual_audiences_are_attached_with_their_source(dude):
    dr = records_for(dude, "duderobe_fetch_a.json")
    expected = {"The SheRobe": "women", "The BroThrow": "men", "The Couple Bundle": "couples"}
    for title, audience in expected.items():
        fact = by_title(dr, title)["facts"]["audience"]
        assert (fact["value"], fact["source"], fact["status"]) == (audience, "manual", "stated_by_user")
    assert "audience" not in by_title(dr, "The DudeRobe")["facts"]


def test_duderobe_licensed_and_limited_flags_and_exclusions(dude):
    dr = records_for(dude, "duderobe_fetch_a.json")
    licensed = sorted(r["title"] for r in dr.values() if "licensed" in r["flags"])
    assert licensed == ["UFC DudeRobe - Black", "UFC DudeRobe - Grey", "UFC DudeRobe - Navy",
                        "University of Michigan DudeRobe"]
    assert by_title(dr, "IndoorCamo\u2122 Limited Edition DudeRobe")["flags"] == ["limited_edition"]
    assert by_title(dr, "Gift Card")["excluded"] and by_title(dr, "Personalization")["excluded"]
    assert sum(r["excluded"] for r in dr.values()) == 2


def test_duderobe_audience_tags_are_hints_not_facts(dude):
    dr = records_for(dude, "duderobe_fetch_a.json")
    sherobe = by_title(dr, "The SheRobe")
    assert sherobe["audience_hints"] == ["her"]
    assert "audience_hints" not in sherobe["facts"]


def test_food_products_get_ingredients_allergens_and_flag_missing_nutrition(defi):
    df = records_for(defi, "defi_snacks.json")
    assert by_title(df, "Melting Insurance")["excluded"] is True
    peanut = by_title(df, "Milk Chocolate Peanut Butter Crunch Crispy Superfood Bite")
    assert peanut["facts"]["allergens"]["value"] == ["Milk and Peanuts"]
    live = [r for r in df.values() if not r["excluded"]]
    assert len(live) == 6 and all(r["readiness"]["state"] == "ready" for r in live)
    assert all("nutrition_facts" in r["readiness"]["missing_expected"] for r in live)
    assert all("materials" not in r["facts"] for r in df.values())


def test_hash_is_identical_across_two_real_fetches_that_differ_only_in_updated_at(dude):
    a = records_for(dude, "duderobe_fetch_a.json")
    b = records_for(dude, "duderobe_fetch_b.json")
    assert a.keys() == b.keys()
    assert any(a[i]["updated_at"] != b[i]["updated_at"] for i in a)     # the fixtures really do differ
    assert all(a[i]["content_hash"] == b[i]["content_hash"] for i in a)


def make(brand, mutate, handle="the-sherobe"):
    raw = raw_products("duderobe_fetch_a.json")
    target = next(p for p in raw if p["handle"] == handle)
    mutate(target)
    return normalize_product(target, brand), normalize_product(
        next(p for p in raw_products("duderobe_fetch_a.json") if p["handle"] == handle), brand)


def test_content_changes_alter_the_hash_but_timestamps_and_cdn_versions_do_not(dude):
    def bump(p):
        p["updated_at"] = "2030-01-01T00:00:00-00:00"
        p["variants"][0]["updated_at"] = "2030-01-01T00:00:00-00:00"
        p["images"][0]["src"] += "?v=999999"
        p["images"][0]["updated_at"] = "2030-01-01T00:00:00-00:00"
    changed, original = make(dude, bump)
    assert changed["content_hash"] == original["content_hash"]

    mutations = {
        "title": lambda p: p.update(title=p["title"] + " v2"),
        "body": lambda p: p.update(body_html="<p>Now with a description</p>"),
        "tags": lambda p: p["tags"].append("sale"),
        "price": lambda p: p["variants"][0].update(price="1.00"),
        "compare_at": lambda p: p["variants"][0].update(compare_at_price="999.00"),
        "sku": lambda p: p["variants"][0].update(sku="NEW-SKU"),
        "variant_removed": lambda p: p["variants"].pop(),
        "image_replaced": lambda p: p["images"][0].update(id=1, src="https://cdn.x/new.jpg"),
        "product_type": lambda p: p.update(product_type="Tops"),
    }
    for name, mutate in mutations.items():
        changed, original = make(dude, mutate)
        assert changed["content_hash"] != original["content_hash"], name


def test_stock_flips_do_not_change_the_content_hash(dude):
    changed, original = make(dude, lambda p: [v.update(available=not v["available"]) for v in p["variants"]])
    assert changed["content_hash"] == original["content_hash"]
    assert changed["availability"] != original["availability"]


def test_manual_fact_changes_do_change_the_hash(dude):
    from dataclasses import replace
    base = by_title(records_for(dude, "duderobe_fetch_a.json"), "The SheRobe")
    changed_brand = replace(dude, manual_facts={"the-sherobe": {"audience": "women and girls"}})
    other = by_title(records_for(changed_brand, "duderobe_fetch_a.json"), "The SheRobe")
    assert other["content_hash"] != base["content_hash"]


def test_malformed_and_duplicate_products_are_reported(swov):
    raw = raw_products("defi_snacks.json")
    raw.append(copy.deepcopy(raw[0]))
    raw.append({"id": 99, "handle": "x"})
    raw.append("junk")
    del raw[1]["variants"]
    records, problems = normalize_all(raw, swov)
    kinds = sorted(p["problem"] for p in problems)
    assert len(records) == 6 and len(problems) == 4
    assert "duplicate id" in kinds and any("title" in k for k in kinds)


def test_tags_given_as_a_comma_string_are_understood(swov):
    raw = raw_products("swoveralls_full_72.json")[0]
    raw["tags"] = "Active, New , LimitedSW"
    rec = normalize_product(raw, swov)
    assert rec["tags"] == ["Active", "New", "LimitedSW"] and "limited_edition" in rec["flags"]


def test_hash_function_ignores_dict_ordering(swov_records):
    rec = copy.deepcopy(next(iter(swov_records.values())))
    original = content_hash(rec)
    rec["facts"] = dict(reversed(list(rec["facts"].items())))
    rec["tags"] = list(reversed(rec["tags"]))
    assert content_hash(rec) == original


def test_by_handle_helper_finds_the_classic_products(swov_records):
    for handle in ("swoveralls-midnight-black", "swoveralls-navy", "swoveralls-dark-athletic-grey"):
        assert by_handle(swov_records, handle)["status"] == "active"
