from pathlib import Path

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import sheets
from contentintel.store import Store

from fake_browser import TT_JSON, FakeBrowser
from test_collectors import logged_in, site
from test_sheets import FakeClient, baselined, one, read, tabs

HTML = Path("tests/fixtures/meta_ad_library_sample.html").read_text(encoding="utf-8")
START, END = 1784962800, 1792479600                      # taken from a real ad: 2026-07-25 to 2026-10-20


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


def ad(**over):
    return {"ad_archive_id": "1", "start_date": START, "end_date": END, "snapshot": {"body": {"text": "t"}}, **over}


def signals(**over):
    row = C.normalize_meta_ad(ad(**over))
    return row["days_running"], row["variants"], row["reach"]


# --- what Meta publishes about an ad ---------------------------------------------------------------------------------------

def test_days_running_counts_from_the_first_day_to_the_last_day_inclusive():
    assert signals()[0] == 88 and signals(start_date=START, end_date=START)[0] == 1 and signals(start_date=START, end_date=START + 86399)[0] == 1
    assert signals(startDate=START, endDate=START + 86400 * 4, start_date=None, end_date=None)[0] == 5          # either naming style


def test_the_site_s_own_active_time_wins_when_it_is_given():
    assert signals(total_active_time=3 * 86400)[0] == 3 and signals(total_active_time=1)[0] == 1
    assert signals(total_active_time=0)[0] == 88 and signals(total_active_time=-5)[0] == 88


@pytest.mark.parametrize("start,end", [(END, START), (0, END), (None, END), (START, None), (True, END), ("x", "y"), (START, True)])
def test_days_running_is_left_blank_when_the_dates_cannot_be_trusted(start, end):
    assert signals(start_date=start, end_date=end)[0] is None


@pytest.mark.parametrize("count,expected", [(2, 2), ("3", 3), (1, None), (0, None), (None, None), ("many", None), (True, None)])
def test_variants_only_counts_when_the_ad_really_has_copies(count, expected):
    assert signals(collation_count=count)[1] == expected


def test_reach_is_shown_where_the_site_gives_it_and_blank_where_it_does_not():
    assert signals(reach_estimate=1500)[2] == 1500
    assert signals(impressions_with_index={"impressions_text": "1K-5K", "impressions_index": 2})[2] == "1K-5K"
    assert signals(impressions_with_index={"impressions_text": None, "impressions_index": -1})[2] is None
    assert signals()[2] is None and signals(impressions_with_index="odd")[2] is None


def test_the_real_ads_have_days_running_and_no_reach_as_meta_does_not_publish_it_for_us_ads():
    rows = list(C.extract_json_items("meta_ads", B.parse_embedded_json(HTML)).values())
    assert len(rows) == 4 and all(isinstance(r["days_running"], int) and r["days_running"] >= 1 for r in rows)
    assert all(r["reach"] is None for r in rows) and all(r["variants"] in (None, *range(2, 100)) for r in rows)
    assert all(r["likes"] is None and r["views"] is None and r["saves"] is None for r in rows)


# --- copies of the same creative -------------------------------------------------------------------------------------------------

def rows_of(*specs):
    return [C._row("meta_ads", i, handle=h, title=t, text=x, variants=v) for i, (h, t, x, v) in enumerate(specs)]


def test_ads_with_the_same_advertiser_title_and_text_are_counted_as_copies_of_one_creative():
    rows = C.add_variant_counts(rows_of(("A", "Offer", "20% off", None), ("A", "Offer", "20% off", None), ("A", "Offer", "20% off", None),
                                        ("A", "Other", "20% off", None), ("B", "Offer", "20% off", None)))
    assert [r["variants"] for r in rows] == [3, 3, 3, None, None]


def test_the_sites_own_copy_count_is_kept_when_it_is_larger():
    rows = C.add_variant_counts(rows_of(("A", "x", "y", 5), ("A", "x", "y", None), ("B", "x", "y", 2), ("C", "", "", None), ("C", "", "", None)))
    assert [r["variants"] for r in rows] == [5, 5, 2, None, None]            # empty ads are not mistaken for copies


# --- posts: saves and engagement ----------------------------------------------------------------------------------------------------

def test_tiktok_saves_are_read_where_tiktok_shows_them():
    item = {**TT_JSON["itemList"][0], "stats": {**TT_JSON["itemList"][0]["stats"], "collectCount": 20}}
    row = C.normalize_tiktok(item)
    assert (row["likes"], row["comments"], row["shares"], row["views"], row["saves"]) == (100, 10, 5, 1000, 20)
    assert C.normalize_tiktok(TT_JSON["itemList"][0])["saves"] is None and C.normalize_instagram({"code": "a", "like_count": 1})["saves"] is None
    assert C.normalize_tiktok({"id": "1", "author": "a", "desc": "x", "stats": {}, "collectCount": 7})["saves"] == 7


@pytest.mark.parametrize("row,expected", [
    ({"views": 1000, "likes": 100, "comments": 10, "shares": 5, "saves": 20}, 13.5),
    ({"views": 1000, "likes": 100}, 10.0), ({"views": 3, "likes": 1}, 33.33), ({"views": 200, "likes": 0, "comments": 0}, 0.0),
    ({"views": 0, "likes": 5}, None), ({"views": None, "likes": 5}, None), ({"views": 100}, None), ({"likes": 5}, None), ({"views": "n/a", "likes": 5}, None)])
def test_engagement_is_everything_known_as_a_share_of_views(row, expected):
    assert C._engagement(row) == expected


def record(at, items, seeds="s"):
    return {"at": at, "run": "r", "source": "tiktok", "key": "k", "seeds": seeds, "items": items}


def test_the_collected_rows_carry_the_signals_latest_values_and_the_engagement_rate():
    first = C._row("tiktok", "1", views=1000, likes=100, saves=20, days_running=None)
    later = C._row("tiktok", "1", views=2000, likes=300, saves=40, comments=10, shares=10)
    meta1, meta2 = C._row("meta_ads", "m", days_running=10, variants=2), C._row("meta_ads", "m", days_running=12)
    rows = C.collected_rows([record("2026-10-01T00:00:00Z", [first, meta1]), record("2026-10-08T00:00:00Z", [later, meta2])])
    t, m = rows["tiktok:1"], rows["meta_ads:m"]
    assert (t["views"], t["likes"], t["saves"], t["engagement_pct"]) == (2000, 300, 40, 18.0)
    assert (m["days_running"], m["variants"], m["engagement_pct"]) == (12, 2, None)      # a later blank never wipes a known number


# --- which seed found each item -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,label", [
    (C.ad_library_url("sweatpant overalls", "us", "active"), "sweatpant overalls"),
    (C.ad_library_url("café au lait", "us", "all"), "café au lait"),
    ("https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=123", "page:123"),       # a followed advertiser page
    ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all", "https://www.facebook.com/ads/library/?active_status=all&ad_type=all"),
    ("https://www.tiktok.com/tag/caf%C3%A9", "#café"), ("https://www.instagram.com/explore/tags/overalls/", "#overalls"),
    ("https://www.tiktok.com/@acme", "@acme"), ("https://www.tiktok.com/search?q=lounge+wear", "lounge wear"),
    ("https://example.com/somewhere", "https://example.com/somewhere")])
def test_each_seed_has_a_short_readable_name(url, label):
    assert C.seed_label(url) == label


def meta_json(*ids):
    return {"ads": [{"ad_archive_id": i, "page_name": "Acme", "start_date": START, "end_date": END, "snapshot": {"body": {"text": f"ad {i}"}}} for i in ids]}


def test_an_item_remembers_every_seed_that_found_it(cfg):
    a, b, c = (C.ad_library_url(w, "us", "active") for w in ("alpha", "beta", "gamma"))
    browser = FakeBrowser({a: {"json": [meta_json("x1", "x2")]}, b: {"json": [meta_json("x2", "x3")]}, c: {"json": [meta_json("x4")]}})
    task = C.Task("meta_ads", "t", {"urls": [a, b, c]}, "k", 3)
    rows, _ = C.run_browser_task(browser, task, 10, site(cfg, "meta_ads"), cfg, {})
    assert {r["item_id"].split(":")[1]: r["found_via"] for r in rows} == {"x1": "alpha", "x2": "alpha | beta", "x3": "beta", "x4": "gamma"}


def test_identical_ads_in_a_collection_show_how_many_copies_there_are(cfg):
    url = C.ad_library_url("alpha", "us", "active")
    same = {"ads": [{"ad_archive_id": str(i), "page_name": "Acme", "start_date": START, "end_date": END,
                     "snapshot": {"body": {"text": "20% off"}, "title": "Sale"}} for i in range(3)]}
    rows, _ = C.run_browser_task(FakeBrowser({url: {"json": [same, meta_json("z")]}}), C.Task("meta_ads", "t", {"urls": [url]}, "k"), 10,
                                 site(cfg, "meta_ads"), cfg, {})
    assert sorted((r["variants"] or 0) for r in rows) == [0, 3, 3, 3]


def test_social_items_also_say_which_hashtag_found_them(cfg):
    ig = {"https://www.instagram.com/explore/tags/overalls/": {"json": [{"media": [{"code": "abc", "like_count": 1, "caption": "x"}]}]}}
    browser = FakeBrowser(ig, present_always=logged_in(cfg, "instagram"))
    rows, _ = C.run_browser_task(browser, C.Task("instagram", "t", {"hashtags": ["overalls"]}, "k", 1), 5, site(cfg, "instagram"), cfg, {"cookies": "x"})
    assert [r["found_via"] for r in rows] == ["#overalls"]


def test_the_queries_column_shows_the_seeds_and_falls_back_to_the_task_description():
    with_label = C._row("meta_ads", "1", found_via="alpha | beta")
    without = C._row("meta_ads", "2")
    rows = C.collected_rows([record("2026-10-01T00:00:00Z", [with_label, without], "5 advertiser pages/keywords"),
                             record("2026-10-08T00:00:00Z", [C._row("meta_ads", "1", found_via="gamma")], "other")])
    assert rows["meta_ads:1"]["queries"] == "alpha | beta | gamma" and rows["meta_ads:2"]["queries"] == "5 advertiser pages/keywords"


# --- the sheet ----------------------------------------------------------------------------------------------------------------------------

def test_the_collected_columns_keep_the_numbers_together_in_a_useful_order():
    cols = sheets.COLLECTED_COLUMNS
    assert cols[cols.index("views"):cols.index("views") + 6] == ["views", "saves", "reach", "days_running", "variants", "engagement_pct"]
    assert cols[-1] == "queries" and len(cols) == len(set(cols))


def test_the_signals_reach_the_sheet_as_numbers_where_they_are_numbers(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    tiktok = C._row("tiktok", "1", handle="acme", text="hi", url="https://t/1", views=1000, likes=100, comments=10, shares=5, saves=20, found_via="#overalls")
    meta = C._row("meta_ads", "m", handle="Rival", text="ad", url="https://f/m", days_running=88, variants=3, reach="1K-5K", found_via="sweatpant overalls")
    store.append_collected("swoveralls", {"at": "2026-10-20T07:00:00Z", "run": "r", "source": "tiktok", "key": "k", "seeds": "s", "items": [tiktok, meta]})
    client = FakeClient()
    report, error = one(swov, store, client)
    header, rows = read(tabs(client.created[0])["Collected"])
    by = {r["item_id"]: r for r in rows}
    assert error is None and header[:len(sheets.COLLECTED_COLUMNS)] == sheets.COLLECTED_COLUMNS
    t, m = by["tiktok:1"], by["meta_ads:m"]
    assert (t["saves"], t["engagement_pct"], t["queries"]) == (20, 13.5, "#overalls") and isinstance(t["saves"], int) and isinstance(t["engagement_pct"], float)
    assert (m["days_running"], m["variants"], m["reach"], m["engagement_pct"]) == (88, 3, "1K-5K", "") and isinstance(m["days_running"], int)
