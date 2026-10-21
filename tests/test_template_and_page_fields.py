"""Template tokens in catalogue ads, the advertiser page on every ad, the cautious signal, and the new settings.

The ads are shaped like the real ones in tests/fixtures/meta_ad_library_sample.html (SHEIN, SHEIN CURVE, Madepants and
Sarah Randall), where one ad's whole text really is the token {{product.brand}}.
"""
from dataclasses import replace

import pytest
import yaml

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.store import Store

from test_meta_real_page import HTML


# ---- which advertiser name may fill {{product.brand}} ---------------------------------------------------

@pytest.mark.parametrize("name,landing,expected", [
    ("Rhoback", "https://rhoback.com/products/mens-hoodie", "Rhoback"),
    ("Rhoback", "https://www.rhoback.com/products/x?utm=1", "Rhoback"),
    ("Chic Me", "https://www.chicme.com/collections/best-sellers", "Chic Me"),
    ("ChicMe", "https://chicme.com/products/x", "ChicMe"),
    ("Superdw-DE", "https://www.superdw.com/collections/x", "Superdw-DE"),
    ("SHEIN", "https://us.shein.com/ark/default?scene=1", "SHEIN"),
    ("Shop Example Ltd", "https://shop.example.co.uk/p/1", "Shop Example Ltd"),
    ("Sarah Randall", "https://amzlink.to/az0VNvq2JXL5L", ""),                       # an affiliate: the product's brand is unknown
    ("Chris and Melis - Designer Decor Deals", "https://amzn.markable.ai/11QlB0pZem", ""),
    ("Kortney and Karlee", "https://amzn.markable.ai/11QlP76vIM", ""),
    ("Some Boutique", "https://some-boutique.myshopify.com/products/x", ""),         # a shared host proves nothing
    ("Walmart Finds", "https://www.walmart.com/ip/123", ""),
    ("Acme", "https://acme.com/", "Acme"), ("Ab", "https://ab.com/", ""), ("Ab", "https://abcd.com/", ""), ("Fun Co", "https://fun.com/", ""),   # too short to trust
    ("Rhoback", "", ""), ("", "https://rhoback.com/", ""), ("Rhoback", "not a url", ""), ("Rhoback", None, "")])
def test_the_brand_is_filled_in_only_when_the_ads_own_landing_site_agrees(name, landing, expected):
    assert C.verified_brand(name, landing) == expected


@pytest.mark.parametrize("url,label", [
    ("https://rhoback.com/x", "rhoback"), ("https://www.rhoback.com/x", "rhoback"), ("https://shop.chicme.co.uk/x", "chicme"),
    ("https://a.b.example.com/x", "example"), ("http://localhost/x", "localhost"), ("", ""), (None, ""), ("nonsense", "")])
def test_the_name_part_of_a_web_address_is_found(url, label):
    assert C._site_label(url) == label


# ---- cleaning ad text -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text,page,landing,title,expected", [
    ("{{product.brand}}", "Rhoback", "https://rhoback.com/x", "Boston College Hoodie", "Rhoback"),
    ("{{product.brand}}", "Sarah Randall", "https://amzlink.to/x", "my go-to overalls!", ""),
    ("Shop {{product.brand}} today", "Rhoback", "https://rhoback.com/x", "", "Shop Rhoback today"),
    ("Shop {{product.brand}} today", "Sarah Randall", "https://amzlink.to/x", "", "Shop today"),
    ("{{product.name}} - new in", "SHEIN", "https://us.shein.com/x", "Reflora Plus Size Top", "Reflora Plus Size Top - new in"),
    ("{{ product.name }}", "SHEIN", "https://us.shein.com/x", "{{product.name}}", ""),
    ("Only {{product.price}}!", "SHEIN", "https://us.shein.com/x", "", "Only !"),
    ("{{product.brand}} {{product.brand}}", "Rhoback", "https://rhoback.com/x", "", "Rhoback Rhoback"),
    ("broken {{product.brand", "Rhoback", "https://rhoback.com/x", "", "broken"),
    ("", "Rhoback", "https://rhoback.com/x", "t", ""), (None, "Rhoback", "https://rhoback.com/x", "t", None)])
def test_template_tokens_are_filled_only_with_verified_facts_and_dropped_otherwise(text, page, landing, title, expected):
    assert C.clean_template_text(text, page, landing, title) == expected


@pytest.mark.parametrize("text", ["Sweatpants + Overalls = Swoveralls.", "20% off — code: FALL20 ", "  spaced   out  ", "curly {braces} stay"])
def test_text_without_tokens_is_returned_exactly_as_it_came(text):
    assert C.clean_template_text(text, "Rhoback", "https://rhoback.com/x", "t") == text


def real_ads():
    return list(C.extract_json_items("meta_ads", B.parse_embedded_json(HTML)).values())


def test_real_ads_never_keep_a_token_in_text_title_or_details():
    for r in real_ads():
        assert "{{" not in r["text"] + r["title"] + r["details"] + r["handle"]


def test_the_real_ad_whose_whole_text_was_a_token_ends_up_blank_and_the_others_are_untouched():
    shein, curve, madepants, sarah = real_ads()
    assert sarah["text"] == "" and sarah["handle"] == "Sarah Randall"
    assert shein["text"] == "Fashion for All: Discover quality clothing for every occasion."
    assert madepants["text"].startswith("😻Duck Bib Overalls")


# ---- the advertiser page on every ad --------------------------------------------------------------------

def test_every_real_ad_carries_its_advertiser_page_id_and_follower_count():
    shein, curve, madepants, sarah = real_ads()
    assert [(r["page_id"], r["page_likes"]) for r in (shein, curve, madepants, sarah)] == [
        ("380039845369159", 39244835), ("1894913033945980", 8413014), ("108271304233408", 63224), ("103943851045230", 2290)]


def test_the_page_id_is_in_the_details_line_before_the_landing_page():
    details = real_ads()[2]["details"]
    assert "| page: 108271304233408 | landing: https://www.madepants.com/" in details


@pytest.mark.parametrize("value,expected", [("108271304233408", "108271304233408"), (108271304233408, "108271304233408"),
                                            ("500", ""), ("abc", ""), ("", ""), (None, ""), ("1" * 21, ""), (True, "")])
def test_only_a_real_looking_page_id_is_kept(value, expected):
    ad = {"ad_archive_id": "1", "page_id": value, "page_name": "X", "snapshot": {"body": {"text": "t"}, "page_like_count": 5}}
    assert C.normalize_meta_ad(ad)["page_id"] == expected


@pytest.mark.parametrize("likes,expected", [(63224, 63224), ("63224", 63224), (None, None), ("many", None), (True, None)])
def test_follower_counts_are_numbers_or_nothing(likes, expected):
    ad = {"ad_archive_id": "1", "page_id": "108271304233408", "snapshot": {"body": {"text": "t"}, "page_like_count": likes}}
    assert C.normalize_meta_ad(ad)["page_likes"] == expected


def test_an_ad_with_no_page_information_still_reads_and_says_so_by_leaving_it_blank():
    r = C.normalize_meta_ad({"ad_archive_id": "7", "snapshot": {"body": {"text": "t"}}})
    assert (r["page_id"], r["page_likes"]) == ("", None) and "page:" not in r["details"]


# ---- the cautious signal --------------------------------------------------------------------------------

@pytest.mark.parametrize("row,expected", [
    ({"source": "meta_ads", "days_running": 440, "variants": 4}, "worth studying: ran 440 days, 4 copies"),
    ({"source": "meta_ads", "days_running": 60}, "worth studying: ran 60 days"),
    ({"source": "meta_ads", "days_running": 59}, ""), ({"source": "meta_ads", "variants": 3}, "worth studying: 3 copies"),
    ({"source": "meta_ads", "variants": 2}, ""), ({"source": "meta_ads"}, ""),
    ({"source": "meta_ads", "days_running": None, "variants": None}, ""), ({"source": "meta_ads", "days_running": 0}, ""),
    ({"source": "meta_ads", "days_running": "440"}, ""), ({"source": "meta_ads", "days_running": True}, ""),
    ({"source": "tiktok", "days_running": 440, "variants": 9}, ""), ({"days_running": 440}, "")])
def test_the_signal_points_at_ads_worth_studying_and_never_calls_anything_a_winner(row, expected):
    assert C.ad_signal(row) == expected and "winner" not in C.ad_signal(row).lower() and "profit" not in C.ad_signal(row).lower()


def record(at, *items):
    return {"at": at, "run": "r", "source": "meta_ads", "key": "k", "seeds": "s", "items": list(items)}


def test_a_blank_number_on_a_later_sighting_never_erases_a_known_one_or_becomes_zero():
    first = C._row("meta_ads", "1", handle="Madepants", text="t", days_running=440, variants=4)
    again = C._row("meta_ads", "1", handle="Madepants", text="t")                    # seen again, numbers not shown this time
    never = C._row("meta_ads", "2", handle="Other", text="t")
    rows = C.collected_rows([record("2026-10-01T00:00:00Z", first, never), record("2026-10-08T00:00:00Z", again)])
    assert (rows["meta_ads:1"]["days_running"], rows["meta_ads:1"]["variants"], rows["meta_ads:1"]["times_seen"]) == (440, 4, 2)
    assert rows["meta_ads:2"]["days_running"] is None and rows["meta_ads:2"]["signal"] == "" and rows["meta_ads:2"]["engagement_pct"] is None


def test_a_newer_number_replaces_an_older_one():
    a = C._row("meta_ads", "1", text="t", days_running=10)
    b = C._row("meta_ads", "1", text="t", days_running=17)
    rows = C.collected_rows([record("2026-10-01T00:00:00Z", a), record("2026-10-08T00:00:00Z", b)])
    assert rows["meta_ads:1"]["days_running"] == 17


# ---- the new settings and research.yaml keys ------------------------------------------------------------

def parse(data):
    return C.parse_collect_config(data, "swoveralls/research.yaml")


def test_the_new_settings_have_safe_defaults():
    s = C.load_collector_settings("does-not-exist.yaml")
    assert (s.benchmarks_enabled, s.benchmark_max_pages, s.benchmark_discovery_per_run, s.benchmark_items_per_name,
            s.benchmark_items_per_page, s.benchmark_retry_days, s.auto_max_benchmarks) == (True, 6, 3, 10, 8, 30, 8)


def test_the_settings_can_be_changed_in_research_yaml(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"benchmarks": {"max_pages": 2, "retry_days": 7}, "auto_seeds": {"max_benchmarks": 3}}}))
    s = C.load_collector_settings(path)
    assert (s.benchmark_max_pages, s.benchmark_retry_days, s.auto_max_benchmarks, s.benchmark_items_per_page) == (2, 7, 3, 8)


@pytest.mark.parametrize("section,bad,match", [
    ("benchmarks", {"enabled": "yes"}, "benchmarks.enabled must be true or false"), ("benchmarks", {"max_pages": -1}, "max_pages"),
    ("benchmarks", {"items_per_name": 0}, "items_per_name"), ("benchmarks", {"items_per_page": 0}, "items_per_page"),
    ("benchmarks", {"retry_days": 0}, "retry_days"), ("benchmarks", {"discovery_per_run": 1.5}, "discovery_per_run"),
    ("benchmarks", {"colour": 1}, "unknown 'colour'"), ("auto_seeds", {"max_benchmarks": -2}, "max_benchmarks")])
def test_a_bad_setting_is_refused_by_name(tmp_path, section, bad, match):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {section: bad}}))
    with pytest.raises(C.CollectError, match=match):
        C.load_collector_settings(path)


def test_the_new_brand_keys_are_read_and_validated():
    cfg = parse({"track_own_brand": False, "ads": {"benchmark_pages": {"Gymshark": 108271304233408, "Skip Me": "skip"}}})
    assert cfg["track_own_brand"] is False and cfg["ads"]["benchmark_pages"] == {"Gymshark": "108271304233408", "Skip Me": "skip"}
    default = parse({})
    assert default["track_own_brand"] is True and default["ads"]["benchmark_pages"] == {}


@pytest.mark.parametrize("data,match", [
    ({"track_own_brand": "no"}, "track_own_brand must be true or false"), ({"track_own_brand": 0}, "track_own_brand must be true or false"),
    ({"ads": {"benchmark_pages": {"Gymshark": "nope"}}}, "ads.benchmark_pages"), ({"ads": {"benchmark_pages": ["Gymshark"]}}, "ads.benchmark_pages"),
    ({"ads": {"benchmark": {}}}, "unknown 'benchmark'")])
def test_bad_brand_keys_are_refused_naming_the_file(data, match):
    with pytest.raises(C.CollectError, match=match) as err:
        parse(data)
    assert "swoveralls/research.yaml" in str(err.value)


def test_a_brand_file_with_the_new_keys_loads_through_the_real_loader(tmp_path, swov):
    from contentintel import research as R
    root = tmp_path / "brands" / "swoveralls"
    root.mkdir(parents=True)
    (root / "research.yaml").write_text(yaml.safe_dump({"track_own_brand": False, "ads": {"benchmark_pages": {"Oodie": "skip"}}}))
    cfg = R.load_brand_research(swov, str(tmp_path / "brands"))["collect"]
    assert cfg["track_own_brand"] is False and cfg["ads"]["benchmark_pages"] == {"Oodie": "skip"}


# ---- addresses ------------------------------------------------------------------------------------------

def test_a_followed_page_address_is_the_ad_librarys_own_page_link():
    url = C.ad_library_page_url("108271304233408", "us", "active")
    assert url == ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=us&view_all_page_id=108271304233408"
                   "&media_type=all")
    from contentintel.benchmarks import page_id_from
    assert page_id_from(url) == "108271304233408" and C.seed_label(url) == "page:108271304233408"


# ---- the own brand --------------------------------------------------------------------------------------

def test_the_own_brand_goes_first_and_is_not_repeated(swov):
    cfg = parse({"ads": {"keywords": ["sweatpant overalls", "SWOVERALLS"]}, "social": {"tiktok": {"hashtags": ["overalls"]}}})
    out = C.with_own_brand(cfg, swov)
    assert out["ads"]["keywords"] == ["Swoveralls", "sweatpant overalls"]
    assert out["tiktok"]["hashtags"] == ["swoveralls", "overalls"] and out["instagram"]["hashtags"] == ["swoveralls"]
    assert cfg["ads"]["keywords"] == ["sweatpant overalls", "SWOVERALLS"]                 # the original is not changed


def test_the_own_brand_is_left_out_when_the_brand_says_so(swov):
    cfg = parse({"track_own_brand": False, "ads": {"keywords": ["x"]}})
    assert C.with_own_brand(cfg, swov) is cfg


def test_a_brand_name_with_only_symbols_adds_no_hashtag(swov):
    out = C.with_own_brand(parse({}), replace(swov, name="!!!"))
    assert out["tiktok"]["hashtags"] == [] and out["instagram"]["hashtags"] == []


# ---- the store ------------------------------------------------------------------------------------------

def test_reading_benchmarks_creates_nothing_and_a_round_trip_keeps_everything(tmp_path):
    store = Store(tmp_path / "data")
    assert store.read_benchmarks("swoveralls") is None and not (tmp_path / "data").exists()
    state = {"version": 1, "items": {"gymshark": {"name": "Gymshark", "status": "resolved", "page_id": "100000000000001"}}}
    store.write_benchmarks("swoveralls", state)
    assert store.read_benchmarks("swoveralls") == state
    path = store._research_file("swoveralls", "benchmarks.json")
    assert path.name == "benchmarks.json" and path.parent.name == "research"


def test_a_damaged_benchmark_file_reads_as_nothing_decided(tmp_path):
    store = Store(tmp_path / "data")
    path = store._research_file("swoveralls", "benchmarks.json")
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.read_benchmarks("swoveralls") is None


def test_writing_the_same_decisions_twice_changes_nothing_on_disk(tmp_path):
    store = Store(tmp_path / "data")
    state = {"version": 1, "items": {}}
    store.write_benchmarks("swoveralls", state)
    path = store._research_file("swoveralls", "benchmarks.json")
    before = path.stat().st_mtime_ns
    store.write_benchmarks("swoveralls", state)
    assert path.stat().st_mtime_ns == before


def test_an_old_row_whose_title_is_a_token_is_repaired_without_inventing_a_name():
    old = C._row("meta_ads", "9", handle="Rhoback", title="{{product.name}}", text="Shop now",
                 details="active | landing: https://rhoback.com/products/x")
    rows = C.collected_rows([record("2026-10-01T00:00:00Z", old)])
    assert rows["meta_ads:9"]["title"] == "" and rows["meta_ads:9"]["text"] == "Shop now"
    brand_title = C._row("meta_ads", "10", handle="Rhoback", title="{{product.brand}} Hoodie", text="t",
                         details="active | landing: https://rhoback.com/products/x")
    assert C.collected_rows([record("2026-10-01T00:00:00Z", brand_title)])["meta_ads:10"]["title"] == "Rhoback Hoodie"


def test_the_landing_page_is_read_from_the_details_line():
    assert C._landing("active | page: 1 | landing: https://rhoback.com/x?a=1 | more") == "https://rhoback.com/x?a=1"
    assert C._landing("active") == "" and C._landing(None) == ""


def test_benchmarks_can_be_switched_off_in_research_yaml(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"benchmarks": {"enabled": False}}}))
    assert C.load_collector_settings(path).benchmarks_enabled is False
    assert C.load_collector_settings("does-not-exist.yaml").benchmarks_enabled is True
