"""Snapshots, retention and change detection with planted changes."""
import copy
import gzip
import json

import pytest

from contentintel.observatory import changes as CH
from contentintel.observatory import htmlx as H
from contentintel.observatory import shopify as S
from contentintel.observatory import snapshots as SN
from observatory_helpers import product, settings

CFG = settings()
BASE = "https://shop.example"
T1, T2, T3, T4 = "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z", "2026-11-03T10:00:00Z", "2026-11-04T10:00:00Z"


def snap(products, collections=None, *, at=T1, complete=True, best=None, best_ok=False):
    return {"kind": "shopify_catalogue", "schema": 1, "domain": "shop.example", "captured_at": at, "complete": complete, "master_source": "products.json", "meta": {}, "notes": [],
            "products": [S.normalize_product(p, BASE) for p in products],
            "collections": [{"id": n, "handle": h, "title": h.title(), "products_count": len(ids), "updated_at": "", "members": ids} for n, (h, ids) in enumerate((collections or {}).items())],
            "best_selling": {"honored": best_ok, "top": best or [], "note": ""}, "stats": {}}


def run(old, new, state=None, at=T2, cfg=CFG):
    """One diff. The state is passed through JSON first, exactly as a saved run would be, so integer ids come back as text."""
    events, out = CH.diff_catalogue("shop-example", old, new, cfg, json.loads(json.dumps(state or {})), at)
    return events, json.loads(json.dumps(out))


def types(events):
    return [e["type"] for e in events]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


P = lambda: [product(1, "Dark Bar", "24.00"), product(2, "Milk Bar", "20.00"), product(3, "Salt Bar", "22.00")]


# ---- quiet when nothing happened ---------------------------------------------------------------------------------------------------------------

def test_two_identical_readings_make_no_events_and_no_state_noise():
    events, state = run(snap(P(), {"all": [1, 2, 3]}), snap(P(), {"all": [1, 2, 3]}, at=T2))
    assert events == [] and state["missing"] == {} and state["removed"] == {}


def test_event_ids_are_stable_unique_and_json_safe():
    a, _ = run(snap(P()), snap(P() + [product(4, "New", "5.00")], at=T2))
    b, _ = run(snap(P()), snap(P() + [product(4, "New", "5.00")], at=T2))
    assert [e["event_id"] for e in a] == [e["event_id"] for e in b] and len({e["event_id"] for e in a}) == len(a)
    json.dumps(a)


# ---- new products and drops -----------------------------------------------------------------------------------------------------------------

def test_a_new_product_is_reported_with_its_collections_and_a_fresh_one_is_major():
    new = snap(P() + [product(4, "Fresh Bar", "6.00", created="2026-10-30T00:00:00Z"), product(5, "Old Stock Bar", "6.00", created="2025-01-01T00:00:00Z")],
               {"new": [4], "all": [1, 2, 3, 4, 5]}, at=T2)
    events, _ = run(snap(P(), {"all": [1, 2, 3]}), new)
    fresh, old = [e for e in of(events, "product_new")]
    assert fresh["severity"] == "major" and old["severity"] == "notable" and "Fresh Bar" in fresh["summary"] and "$6.00" in fresh["summary"]
    assert fresh["after"]["collections"] == ["new", "all"] and fresh["subject"]["handle"] == "fresh-bar"


def test_three_new_products_together_are_a_drop_and_two_are_not():
    base = snap(P(), {"all": [1, 2, 3]})
    three = snap(P() + [product(n, f"Drop {n}", "9.00") for n in (4, 5, 6)], {"all": [1, 2, 3, 4, 5, 6]}, at=T2)
    events, _ = run(base, three)
    drop = of(events, "new_drop")
    assert len(drop) == 1 and drop[0]["severity"] == "major" and drop[0]["details"]["count"] == 3 and len(of(events, "product_new")) == 3
    two = snap(P() + [product(n, f"Drop {n}", "9.00") for n in (4, 5)], {"all": [1, 2, 3, 4, 5]}, at=T2)
    assert of(run(base, two)[0], "new_drop") == []


def test_an_incomplete_baseline_cannot_claim_a_product_is_new():
    events, _ = run(snap(P()[:2], complete=False), snap(P(), at=T2))
    assert of(events, "product_new") == []


# ---- removals need confirmation ---------------------------------------------------------------------------------------------------------------

def test_a_product_is_removed_only_after_it_has_been_missing_for_two_runs():
    full, less = snap(P()), snap(P()[:2], at=T2)
    ev1, st1 = run(full, less, at=T2)
    assert of(ev1, "product_removed") == [] and st1["missing"]["3"]["count"] == 1
    ev2, st2 = run(less, snap(P()[:2], at=T3), st1, at=T3)
    removed = of(ev2, "product_removed")
    assert len(removed) == 1 and "Salt Bar" in removed[0]["summary"] and "since 2026-11-02" in removed[0]["summary"] and st2["missing"] == {}
    ev3, _ = run(snap(P()[:2], at=T3), snap(P()[:2], at=T4), st2, at=T4)
    assert of(ev3, "product_removed") == []                                           # reported once, not every run


def test_a_product_that_vanishes_for_one_run_and_returns_was_never_removed_or_new():
    _, st = run(snap(P()), snap(P()[:2], at=T2), at=T2)
    ev, st2 = run(snap(P()[:2], at=T2), snap(P(), at=T3), st, at=T3)
    assert ev == [] and st2["missing"] == {}


def test_removal_confirmation_of_one_run_reports_immediately():
    cfg = settings(changes__removal_confirm_runs=1)
    ev, _ = run(snap(P()), snap(P()[:2], at=T2), cfg=cfg)
    assert len(of(ev, "product_removed")) == 1


def test_an_incomplete_new_reading_never_counts_as_a_removal_or_advances_the_clock():
    ev, st = run(snap(P()), snap(P()[:1], at=T2, complete=False))
    assert of(ev, "product_removed") == [] and st["missing"] == {}


def test_a_confirmed_removal_that_comes_back_is_a_relisting_not_a_new_product():
    _, st = run(snap(P()), snap(P()[:2], at=T2))
    _, st = run(snap(P()[:2], at=T2), snap(P()[:2], at=T3), st, at=T3)
    ev, st2 = run(snap(P()[:2], at=T3), snap(P(), at=T4), st, at=T4)
    assert types(ev) == ["product_relisted"] and st2["removed"] == {}


# ---- prices, sales, stock ---------------------------------------------------------------------------------------------------------------------

def test_price_changes_below_the_noise_floor_are_ignored_and_big_ones_are_notable():
    base = snap(P())
    small = snap([product(1, "Dark Bar", "24.05"), *P()[1:]], at=T2)                                  # +0.2%
    assert of(run(base, small)[0], "price_change") == []
    big = snap([product(1, "Dark Bar", "20.00"), *P()[1:]], at=T2)
    (e,) = of(run(base, big)[0], "price_change")
    assert e["severity"] == "notable" and e["details"]["pct"] == -16.7 and e["before"]["price_min"] == 24.0 and e["after"]["price_min"] == 20.0 and "-16.7%" in e["summary"]
    mid = snap([product(1, "Dark Bar", "23.00"), *P()[1:]], at=T2)
    assert of(run(base, mid)[0], "price_change")[0]["severity"] == "info"


def test_only_the_top_variant_price_changing_is_still_noticed():
    old = snap([product(1, "Variety", "10.00", extra_variants=[{"price": "30.00"}])])
    new = snap([product(1, "Variety", "10.00", extra_variants=[{"price": "36.00"}])], at=T2)
    (e,) = of(run(old, new)[0], "price_change")
    assert "top price" in e["summary"] and e["after"] == {"price_max": 36.0}


def test_sales_start_end_and_change_depth():
    base = snap(P())
    on = snap([product(1, "Dark Bar", "16.00", "24.00"), *P()[1:]], at=T2)                             # 33% off
    (e,) = of(run(base, on)[0], "sale_started")
    assert e["severity"] == "major" and e["after"]["depth_pct"] == 33.3 and "33% off" in e["summary"]
    mild = snap([product(1, "Dark Bar", "22.00", "24.00"), *P()[1:]], at=T2)
    assert of(run(base, mild)[0], "sale_started")[0]["severity"] == "notable"
    ev, _ = run(on, base.__class__(snap(P(), at=T3)), at=T3)
    assert types(of(ev, "sale_ended")) == ["sale_ended"]
    deeper = snap([product(1, "Dark Bar", "12.00", "24.00"), *P()[1:]], at=T3)
    (d,) = of(run(on, deeper, at=T3)[0], "discount_depth_change")
    assert d["before"]["depth_pct"] == 33.3 and d["after"]["depth_pct"] == 50.0


def test_stock_outs_and_returns_and_a_best_sellers_stock_out_is_major():
    old = snap(P(), best=[1, 2, 3], best_ok=True)
    out = snap([product(1, "Dark Bar", "24.00", available=False), *P()[1:]], at=T2, best=[1, 2, 3], best_ok=True)
    (e,) = of(run(old, out)[0], "stock_out")
    assert e["severity"] == "major" and e["details"]["best_seller"] is True
    plain_old = snap(P())
    plain_new = snap([product(1, "Dark Bar", "24.00", available=False), *P()[1:]], at=T2)
    assert of(run(plain_old, plain_new)[0], "stock_out")[0]["severity"] == "notable"
    (back,) = of(run(plain_new, snap(P(), at=T3), at=T3)[0], "back_in_stock")
    assert "back in stock" in back["summary"]


def test_small_edits_are_info_level_with_before_and_after():
    new = snap([product(1, "Dark Chocolate Bar", "24.00", tags=["new", "vegan"], images=2, body="<p>Rewritten copy</p>", extra_variants=[{"title": "Large"}]), *P()[1:]], at=T2)
    new["products"][0]["image"] = "https://cdn.example/other.jpg"
    ev, _ = run(snap(P()), new)
    got = {e["type"]: e for e in ev}
    assert set(got) == {"title_change", "image_change", "description_change", "tags_change", "variants_changed"}
    assert all(e["severity"] == "info" for e in ev) and got["title_change"]["before"] == {"title": "Dark Bar"} and got["tags_change"]["details"]["added"] == ["new", "vegan"]
    assert got["variants_changed"]["details"]["added"] == ["Large"]


def test_the_event_cap_keeps_the_most_important_and_says_what_it_dropped():
    cfg = settings(changes__max_events_per_competitor=10)
    old = snap([product(n, f"P{n}", "10.00") for n in range(1, 31)])
    new = snap([product(n, f"P{n}", "10.00", available=(n > 20), tags=["x"]) for n in range(1, 31)] + [product(99, "Launch", "5.00", created="2026-11-01T00:00:00Z")], at=T2)
    ev, _ = run(old, new, cfg=cfg)
    assert len(ev) == 10 and ev[-1]["type"] == "events_truncated" and ev[-1]["details"]["dropped"] > 0
    assert any(e["type"] == "product_new" for e in ev) and CH.severity_rank(ev[0]["severity"]) <= CH.severity_rank(ev[-2]["severity"])


# ---- collections ------------------------------------------------------------------------------------------------------------------------------

def test_new_and_removed_collections_and_memberships():
    old = snap(P(), {"all": [1, 2, 3], "old-line": [1]})
    new = snap(P(), {"all": [1, 2, 3], "best-sellers": [2, 1], "summer-sale": [3]}, at=T2)
    ev, _ = run(old, new)
    assert sorted(e["subject"]["handle"] for e in of(ev, "collection_new")) == ["best-sellers", "summer-sale"] and all(e["severity"] == "notable" for e in of(ev, "collection_new"))
    assert [e["subject"]["handle"] for e in of(ev, "collection_removed")] == ["old-line"]
    adds = of(ev, "collection_add")
    assert adds == [] or all(a["subject"]["id"] in (1, 2, 3) for a in adds)
    old2 = snap(P(), {"best-sellers": [1, 2]})
    new2 = snap(P(), {"best-sellers": [1, 2, 3]}, at=T2)
    (a,) = of(run(old2, new2)[0], "collection_add")
    assert a["severity"] == "notable" and a["after"] == {"collection": "best-sellers", "position": 3}
    (r,) = of(run(new2, old2, at=T3)[0], "collection_remove")
    assert r["subject"]["title"] == "Salt Bar"
    assert of(run(new2, snap(P(), {}, complete=False), at=T3)[0], "collection_removed") == []        # an incomplete reading removes nothing


def test_inserting_one_product_at_the_top_is_not_reported_as_everything_moving():
    prods = [product(n, f"P{n}", "10.00") for n in range(1, 21)]
    old = snap(prods, {"all": list(range(1, 21))})
    new = snap(prods + [product(99, "Top", "5.00")], {"all": [99] + list(range(1, 21))}, at=T2)
    ev, _ = run(old, new)
    assert of(ev, "collection_reorder") == [] and len(of(ev, "product_new")) == 1


def test_a_real_reorder_is_reported_once_with_who_entered_the_top():
    prods = [product(n, f"P{n}", "10.00") for n in range(1, 31)]
    order = list(range(1, 31))
    moved = [30] + [i for i in order if i != 30]                                       # P30 jumps from last to first
    ev, _ = run(snap(prods, {"best-sellers": order}), snap(prods, {"best-sellers": moved}, at=T2))
    (e,) = of(ev, "collection_reorder")
    assert e["severity"] == "notable" and e["details"]["entered_top"] == ["P30"] and "now in the first 12" in e["summary"] and e["details"]["moved"][0] == {"title": "P30", "by": 29}
    quiet = [i for i in order]
    quiet[4], quiet[5] = quiet[5], quiet[4]                                           # a swap of neighbours is below the move threshold
    assert of(run(snap(prods, {"c": order}), snap(prods, {"c": quiet}, at=T2))[0], "collection_reorder") == []


def test_best_seller_entries_are_reported_only_when_the_sort_was_honoured_both_times():
    old = snap(P(), best=[1, 2], best_ok=True)
    new = snap(P(), at=T2, best=[3, 1, 2], best_ok=True)
    assert [e["subject"]["id"] for e in of(run(old, new)[0], "best_seller_entry")] == [3]
    ignored = snap(P(), at=T2, best=[3, 1, 2], best_ok=False)
    assert of(run(old, ignored)[0], "best_seller_entry") == []


# ---- sitemap, feeds, pages --------------------------------------------------------------------------------------------------------------------

def sm(urls, truncated=False):
    return {"kind": "sitemap", "urls": {u: {"lastmod": lm, "cat": cat} for u, (lm, cat) in urls.items()}, "truncated": truncated}


def test_sitemap_changes_new_pages_posts_removals_and_updates():
    old = sm({f"{BASE}/products/a": ("2026-10-01", "product"), f"{BASE}/pages/about": ("2026-09-01", "page"), f"{BASE}/collections/old": ("", "collection")})
    new = sm({f"{BASE}/products/a": ("2026-11-01", "product"), f"{BASE}/products/b": ("", "product"), f"{BASE}/pages/about": ("2026-11-01", "page"),
              f"{BASE}/pages/sale": ("2026-11-02", "page"), f"{BASE}/blogs/news/launch": ("2026-11-02", "article")})
    ev = CH.diff_sitemap("shop-example", old, new, CFG, T2)
    got = {e["type"]: e for e in ev}
    assert got["sitemap_new_products"]["details"]["count"] == 1 and got["blog_post_new"]["subject"]["url"].endswith("/blogs/news/launch")
    assert got["new_landing_page"]["subject"]["url"].endswith("/pages/sale") and got["landing_page_removed"]["subject"]["url"].endswith("/collections/old")
    assert got["page_updated"]["before"] == {"lastmod": "2026-09-01"} and got["sitemap_products_updated"]["details"]["count"] == 1


def test_a_truncated_sitemap_never_reports_removals_or_new_pages_against_a_truncated_baseline():
    big = sm({f"{BASE}/products/{n}": ("", "product") for n in range(10)})
    small = sm({f"{BASE}/products/0": ("", "product")}, truncated=True)
    assert [e for e in CH.diff_sitemap("s", big, small, CFG, T2) if "removed" in e["type"]] == []
    assert CH.diff_sitemap("s", small, big, CFG, T2) == []                                      # the baseline was cut short, so 'new' means nothing


def feed(items, fmt="rss"):
    return {"kind": "feed", "format": fmt, "items": items}


def test_feed_changes_blog_posts_and_product_feed_prices_and_stock():
    posts = CH.diff_feed("s", feed([{"id": "1", "title": "Old", "link": "", "published": ""}]), feed([{"id": "1", "title": "Old", "link": ""}, {"id": "2", "title": "Launch", "link": BASE + "/p", "published": "x", "summary": "s"}]), CFG, T2)
    assert types(posts) == ["blog_post_new"] and posts[0]["subject"]["title"] == "Launch"
    old = feed([{"id": "A", "title": "Bar", "link": "", "price": 24.0, "sale_price": None, "availability": "in_stock"}], "merchant")
    new = feed([{"id": "A", "title": "Bar", "link": "", "price": 24.0, "sale_price": 18.0, "availability": "out_of_stock"},
                {"id": "B", "title": "Fresh", "link": "", "price": 5.0, "sale_price": None, "availability": "in_stock"}], "merchant")
    got = {e["type"]: e for e in CH.diff_feed("s", old, new, CFG, T2)}
    assert got["feed_price_change"]["details"]["pct"] == -25.0 and got["feed_availability_change"]["after"] is None and got["feed_product_new"]["subject"]["title"] == "Fresh"


def page(html, url=BASE + "/"):
    p = H.parse_page(html, url)
    import hashlib
    return {"kind": "page", "url": url, "title": p["title"], "h1": p["h1"], "announcements": p["announcements"], "offers": H.extract_offers(p["text"], " ".join(p["announcements"])),
            "text": p["text"], "text_hash": hashlib.sha1(p["text"].encode()).hexdigest()[:16]}


def test_page_changes_headline_announcement_and_offers_with_big_offers_major():
    old = page("<title>Shop</title><div class='announcement-bar'>Free shipping on orders over $50</div><h1>Real chocolate</h1><p>We make bars from cacao beans in small batches.</p>")
    new = page("<title>Shop</title><div class='announcement-bar'>BLACK FRIDAY: 30% off sitewide with code BF30</div><h1>Black Friday is here</h1><p>We make bars from cacao beans in small batches.</p>")
    got = {e["type"]: e for e in CH.diff_page("s", old, new, CFG, T2)}
    assert got["headline_change"]["before"] == {"h1": ["Real chocolate"]} and "Black Friday is here" in got["headline_change"]["summary"]
    assert got["announcement_change"]["severity"] == "notable" and "BLACK FRIDAY" in got["announcement_change"]["summary"]
    assert got["offer_change"]["severity"] == "major" and "30% off" in got["offer_change"]["details"]["added"] and "free shipping on orders over $50" in got["offer_change"]["details"]["ended"]


def test_page_text_changes_below_the_threshold_or_identical_pages_are_quiet():
    a = page("<h1>Hi</h1><p>Our story began in a kitchen.</p>")
    assert CH.diff_page("s", a, copy.deepcopy(a), CFG, T2) == []
    b = page("<h1>Hi</h1><p>Our story began in a kitchen!</p>")
    assert CH.diff_page("s", a, b, CFG, T2) == [] or CH.diff_page("s", a, b, settings(changes__page_change_min_chars=500), T2) == []
    c = page("<h1>Hi</h1><p>Our story began in a kitchen.</p><p>We now ship to Canada, the UK and Australia every week.</p>")
    (e,) = CH.diff_page("s", a, c, CFG, T2)
    assert e["type"] == "page_changed" and e["details"]["added"] == ["We now ship to Canada, the UK and Australia every week."] and e["details"]["removed"] == []


# ---- snapshot store ---------------------------------------------------------------------------------------------------------------------------

def test_a_snapshot_is_stored_compressed_and_read_back_identically(tmp_path):
    store = SN.SnapshotStore(tmp_path)
    s = snap(P(), {"all": [1, 2, 3]})
    entry, written = store.save("shop-example", "catalogue", s, T1)
    assert written and entry["count"] == 3 and entry["complete"] is True
    raw = (tmp_path / "_observatory" / "sites" / "shop-example" / "catalogue" / entry["file"]).read_bytes()
    assert raw[:2] == b"\x1f\x8b" and json.loads(gzip.decompress(raw)) == s
    got_entry, got = store.latest("shop-example", "catalogue")
    assert got == s and got_entry["hash"] == entry["hash"] and store.kinds("shop-example") == ["catalogue"] and store.sites() == ["shop-example"]


def test_an_unchanged_reading_is_only_confirmed_not_stored_again(tmp_path):
    store = SN.SnapshotStore(tmp_path)
    store.save("s", "catalogue", snap(P(), at=T1), T1)
    entry, written = store.save("s", "catalogue", snap(P(), at=T2), T2)
    assert written is False and entry["confirmed_at"] == T2 and entry["captured_at"] == T1
    assert len(list((tmp_path / "_observatory" / "sites" / "s" / "catalogue").glob("*.json.gz"))) == 1
    changed, written = store.save("s", "catalogue", snap(P()[:2], at=T3), T3)
    assert written and len(store.entries("s", "catalogue")) == 2
    partial, written = store.save("s", "catalogue", snap(P()[:2], at=T4, complete=False), T4)
    assert written and partial["complete"] is False                                      # same content but now flagged incomplete is a different fact
    assert store.latest("s", "catalogue", complete_only=True)[0]["captured_at"] == T3


def test_retention_keeps_recent_then_weekly_then_monthly_and_never_the_newest(tmp_path):
    store = SN.SnapshotStore(tmp_path)
    now = "2026-11-03T12:00:00Z"
    days = [0, 1, 2, 3, 5, 8, 9, 12, 20, 30, 33, 40, 61, 100, 130, 200, 400]
    from datetime import datetime, timedelta
    stamps = [(datetime(2026, 11, 3, 10) - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ") for d in days]
    for n, ts in enumerate(sorted(stamps)):
        store.save("s", "catalogue", snap([product(1, f"V{n}", "1.00")], at=ts), ts)
    before = len(store.entries("s", "catalogue"))
    removed = store.prune("s", "catalogue", CFG["retention"], now)
    left = store.entries("s", "catalogue")
    assert len(left) + len(removed) == before and len(left) < before
    assert left[-1]["captured_at"] == max(stamps) and {e["captured_at"] for e in left} >= {s for s in stamps if (datetime(2026, 11, 3, 10) - datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")).days <= 7}
    assert all((tmp_path / "_observatory" / "sites" / "s" / "catalogue" / e["file"]).exists() for e in left)
    assert not any((tmp_path / "_observatory" / "sites" / "s" / "catalogue" / f).exists() for f in removed)
    assert min(e["captured_at"] for e in left) >= "2025-11-01"                                  # the 400-day-old one is gone
    assert store.prune("s", "catalogue", CFG["retention"], now) == []


def test_a_damaged_index_is_reported_and_nothing_is_overwritten(tmp_path):
    store = SN.SnapshotStore(tmp_path)
    store.save("s", "catalogue", snap(P()), T1)
    idx = tmp_path / "_observatory" / "sites" / "s" / "catalogue" / "index.json"
    idx.write_text("{broken")
    with pytest.raises(SN.SnapshotError):
        store.save("s", "catalogue", snap(P()[:2], at=T2), T2)
    assert idx.read_text() == "{broken" and len(list(idx.parent.glob("*.json.gz"))) == 1


@pytest.mark.parametrize("bad", ["../x", "A b", "", "x/y", "-lead"])
def test_site_and_kind_names_cannot_escape_the_folder(tmp_path, bad):
    store = SN.SnapshotStore(tmp_path)
    with pytest.raises(SN.SnapshotError):
        store.site_dir(bad)
    with pytest.raises(SN.SnapshotError):
        store.entries("ok", bad)


def test_events_are_appended_once_survive_a_torn_line_and_state_round_trips(tmp_path):
    store = SN.SnapshotStore(tmp_path)
    ev, _ = run(snap(P()), snap(P() + [product(4, "N", "1.00")], at=T2))
    assert store.append_events("s", ev) == len(ev) and store.append_events("s", ev) == 0
    path = tmp_path / "_observatory" / "sites" / "s" / "events.jsonl"
    path.write_text(path.read_text() + '{"event_id": "half')
    assert [e["event_id"] for e in store.read_events("s")] == [e["event_id"] for e in ev]
    store.save_state("s", {"missing": {"3": {"count": 1}}})
    assert store.load_state("s") == {"missing": {"3": {"count": 1}}} and store.load_state("other") == {}
