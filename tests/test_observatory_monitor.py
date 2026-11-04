"""A whole monitoring run, end to end, against a scripted competitor site."""
import json
import types

import pytest
import yaml

from contentintel import health as HL
from contentintel import sources as SRC
from contentintel.observatory import monitor as MO
from contentintel.observatory import snapshots as SN
from contentintel.observatory.pauses import PauseLedger
from contentintel.store import Store
from observatory_helpers import Web, client, product, register_store, reply, settings
from conftest import ROOT

SITE, BASE = "shop.example", "https://shop.example"
T1, T2, T3 = "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z", "2026-11-03T10:00:00Z"
ROBOTS = "User-agent: *\nDisallow: /cart\nSitemap: https://shop.example/sitemap.xml\n"
COMP = {"id": "shop-example", "name": "Shop", "domain": SITE, "kind": "store", "pages": [BASE + "/pages/about"], "blog": "", "feeds": [], "youtube": "", "instagram": "",
        "tiktok": "", "monitor": {"shopify": "auto", "sitemap": True, "feeds": True, "pages": True}, "origin": "test"}


def homepage(headline="Real chocolate", banner="Free shipping on orders over $50"):
    return (f'<html><head><title>Shop</title><link rel="alternate" type="application/atom+xml" href="{BASE}/blogs/news.atom"></head><body>'
            f'<div class="announcement-bar">{banner}</div><h1>{headline}</h1><p>We make bars from cacao beans in small batches, by hand, every single week of the year.</p></body></html>')


def atom(titles):
    entries = "".join(f'<entry><id>tag:{n}</id><title>{t}</title><link rel="alternate" href="{BASE}/blogs/news/{n}"/><updated>2026-10-0{n}T00:00:00Z</updated></entry>' for n, t in enumerate(titles, 1))
    return f'<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>{entries}</feed>'


def sitemap(extra=()):
    urls = [f"{BASE}/", f"{BASE}/products/dark-bar", f"{BASE}/pages/about"] + list(extra)
    return '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{u}</loc><lastmod>2026-10-01</lastmod></url>" for u in urls) + "</urlset>"


def world(v=1):
    prods = [product(1, "Dark Bar", "24.00"), product(2, "Milk Bar", "20.00")]
    colls = {"all": [1, 2], "best-sellers": [1, 2]}
    if v >= 2:
        prods = [product(1, "Dark Bar", "16.00", "24.00", created="2026-10-01T00:00:00Z"), product(2, "Milk Bar", "20.00", available=False),
                 product(3, "Fresh Bar", "6.00", created="2026-11-01T00:00:00Z")]
        colls = {"all": [1, 2, 3], "best-sellers": [1, 2], "new": [3]}
    web = Web()
    register_store(web, SITE, prods, colls, robots=ROBOTS)
    web.on(BASE + "/", reply(200, homepage() if v < 2 else homepage("Black Friday is here", "BLACK FRIDAY: 30% off sitewide with code BF30"), {"content-type": "text/html"}))
    web.on(BASE + "/pages/about", reply(200, "<html><title>About</title><h1>About us</h1><p>We started in a kitchen with a simple idea: honest chocolate.</p></html>", {"content-type": "text/html"}))
    web.on(BASE + "/blogs/news.atom", reply(200, atom(["Hello"] if v < 2 else ["Hello", "Launch"]), {"content-type": "application/atom+xml"}))
    web.on(BASE + "/sitemap.xml", reply(200, sitemap() if v < 2 else sitemap([f"{BASE}/pages/black-friday", f"{BASE}/products/fresh-bar"]), {"content-type": "application/xml"}))
    return web


@pytest.fixture
def env(tmp_path):
    registry = SRC.load_sources(ROOT / "sources.yaml")
    store = Store(tmp_path)
    hl = HL.HealthLedger.load(store, registry)
    e = types.SimpleNamespace(tmp=tmp_path, snaps=SN.SnapshotStore(tmp_path), hl=hl, registry=registry, ledger=PauseLedger(tmp_path), cfg=settings())

    def go(web, at, comp=COMP, only=None, cfg=None):
        c = client(web, cfg or e.cfg, ledger=e.ledger)
        c.now_fn = lambda: at
        return MO.monitor_site(comp, client=c, snaps=e.snaps, settings=cfg or e.cfg, hl=e.hl, now=at, only=only)
    e.go = go
    return e


def ev(result, kind=None):
    return [e for e in result["events"] if kind is None or e["type"] == kind]


def part(result, name):
    return result["parts"][name]


def test_the_first_run_reads_every_part_stores_baselines_and_records_health(env):
    r = env.go(world(1), T1)
    assert r["stopped"] == "" and sorted(r["parts"]) == ["feeds", "pages", "shopify", "sitemap"]
    assert [e["type"] for e in r["events"]] == ["baseline_recorded"] * 5                  # two pages, one feed, the sitemap and the catalogue
    assert all(p["ok"] and p["baseline"] for plist in r["parts"].values() for p in plist)
    kinds = env.snaps.kinds("shop-example")
    assert "sitemap" in kinds and "catalogue" in kinds and sum(k.startswith("page-") for k in kinds) == 2 and sum(k.startswith("feed-") for k in kinds) == 1
    cat = part(r, "shopify")[0]
    assert cat["items"] == 2 and cat["complete"] is True and cat["best_selling"] is False
    rows = {(x["source"], x["route"]): x for x in env.hl.rows(T1)}
    for key in (("competitor_pages", "static_fetch"), ("competitor_pages", "feed"), ("competitor_stores", "sitemap"), ("competitor_stores", "public_json")):
        assert rows[key]["last_success"] == T1, key
    assert len(env.snaps.read_events("shop-example")) == 5


def test_planted_changes_are_all_found_on_the_second_run(env):
    env.go(world(1), T1)
    r = env.go(world(2), T2)
    types_ = {e["type"] for e in r["events"]}
    assert {"product_new", "sale_started", "stock_out", "collection_new", "headline_change", "announcement_change", "offer_change", "blog_post_new",
            "new_landing_page", "sitemap_new_products", "price_change"} <= types_
    assert "collection_add" not in types_                                              # the product added to 'all' is itself new, so that is part of its launch
    by = lambda t: [e for e in r["events"] if e["type"] == t]
    assert by("product_new")[0]["subject"]["title"] == "Fresh Bar" and by("product_new")[0]["severity"] == "major"
    assert by("sale_started")[0]["after"]["depth_pct"] == 33.3 and by("stock_out")[0]["subject"]["title"] == "Milk Bar"
    assert by("offer_change")[0]["severity"] == "major" and by("blog_post_new")[0]["subject"]["title"] == "Launch"
    assert any(e["subject"]["url"].endswith("/pages/black-friday") for e in by("new_landing_page"))
    assert not any(e["type"] == "baseline_recorded" for e in r["events"])
    assert all(e["site"] == "shop-example" and e["tier"] == "public_polite" for e in r["events"])
    assert len(env.snaps.read_events("shop-example")) == 5 + len(r["events"])


def test_a_third_run_with_nothing_new_changes_nothing_and_stores_no_new_files(env):
    env.go(world(2), T1)
    files = sorted(p.name for p in (env.tmp / "_observatory").rglob("*.json.gz"))
    r = env.go(world(2), T2)
    assert r["events"] == [] and all(not p["written"] for plist in r["parts"].values() for p in plist)
    assert sorted(p.name for p in (env.tmp / "_observatory").rglob("*.json.gz")) == files
    entry = env.snaps.entries("shop-example", "catalogue")[-1]
    assert entry["captured_at"] == T1 and entry["confirmed_at"] == T2                    # it held unchanged for a day


def test_a_site_that_blocks_the_home_page_stops_the_whole_site_and_is_recorded(env):
    env.go(world(1), T1)
    web = world(2)
    web.on(BASE + "/", reply(403, "Forbidden"))
    r = env.go(web, T2)
    assert "PLATFORM_BLOCKED" in r["stopped"] and r["events"] == []
    assert part(r, "pages")[0]["ok"] is False and "shopify" not in r["parts"] and "sitemap" not in r["parts"]
    assert not any(u.endswith("/products.json?limit=250&page=1") for u in web.log)
    assert env.hl.status("competitor_pages", "static_fetch", T2) == "paused"             # a refusal rests the route, as for every other source
    assert env.snaps.latest("shop-example", "catalogue")[1]["products"][0]["price_min"] == 24.0                      # the last good reading is still there


def test_a_rate_limit_stops_the_site_rests_it_and_the_next_run_sends_nothing(env):
    env.go(world(1), T1)
    web = world(2)
    web.on(BASE + "/sitemap.xml", reply(429, "slow down", {"Retry-After": "30"}))
    r = env.go(web, T2)
    assert "RATE_LIMITED" in r["stopped"] and "shopify" not in r["parts"]
    assert {e["type"] for e in r["events"]} >= {"headline_change", "blog_post_new"}                          # what was read before the limit is kept
    web2 = world(2)
    env2 = env.go(web2, "2026-11-02T10:05:00Z")
    assert web2.log == [] or all("robots" not in u for u in web2.log) and env2["stopped"]
    assert not any(u.startswith(BASE + "/products.json") for u in web2.log)


def test_a_site_that_is_not_a_shopify_store_is_noted_not_failed_and_is_not_probed_again(env):
    web = Web()
    web.on_robots(BASE, "User-agent: *\n")
    web.on(BASE + "/", reply(200, homepage(), {"content-type": "text/html"}))
    web.on(BASE + "/blogs/news.atom", reply(200, atom(["Hello"])))
    web.on(BASE + "/sitemap.xml", reply(200, sitemap()))
    web.on(BASE + "/pages/about", reply(200, "<title>About</title><p>About us and our long story in a kitchen.</p>", {"content-type": "text/html"}))
    r = env.go(web, T1)
    assert r["stopped"] == "" and "not a Shopify store" in part(r, "shopify")[0]["notes"][0] and "catalogue" not in env.snaps.kinds("shop-example")
    probes = [u for u in web.log if "products.json" in u or "meta.json" in u]
    assert probes
    web2 = Web(web.routes)
    web2.log = []
    env.go(web2, T2)
    assert [u for u in web2.log if "products.json" in u or "meta.json" in u] == []                            # remembered for 30 days
    rows = {(x["source"], x["route"]): x for x in env.hl.rows(T2)}
    assert not rows[("competitor_stores", "public_json")]["error_code"] and rows[("competitor_stores", "public_json")]["last_success"] == T1        # "not a store" is an answer, not a failure; run two did not probe again


def test_a_partial_reading_is_stored_but_never_becomes_the_baseline_and_never_causes_removals(env):
    env.go(world(1), T1)
    cfg = settings(politeness__max_requests_per_domain=6)
    r = env.go(world(2), T2, cfg=cfg, only=["shopify"])
    cat = part(r, "shopify")
    assert cat and cat[0]["ok"] and cat[0]["complete"] is False
    assert not ev(r, "product_removed") and not ev(r, "collection_removed")
    assert env.snaps.latest("shop-example", "catalogue")[0]["complete"] is False
    assert env.snaps.latest("shop-example", "catalogue", complete_only=True)[0]["captured_at"] == T1
    r3 = env.go(world(1), T3, only=["shopify"])
    assert ev(r3, "product_new") == [] and ev(r3, "stock_out") == []                                        # compared with the complete reading, not the partial one


def test_only_chosen_parts_run_and_a_part_switched_off_for_a_competitor_stays_off(env):
    r = env.go(world(1), T1, only=["sitemap"])
    assert list(r["parts"]) == ["sitemap"]
    off = {**COMP, "monitor": {"shopify": False, "sitemap": False, "feeds": True, "pages": False}}
    web = world(1)
    r = env.go(web, T2, comp=off)
    assert list(r["parts"]) == ["feeds"] and not any("products.json" in u or "sitemap" in u for u in web.log)


def test_a_feed_that_is_only_a_guess_is_not_a_failure_when_it_does_not_exist(env):
    web = world(1)
    web.routes.pop(BASE + "/blogs/news.atom")
    web.on(BASE + "/", reply(200, homepage().replace(f'<link rel="alternate" type="application/atom+xml" href="{BASE}/blogs/news.atom">', ""), {"content-type": "text/html"}))
    r = env.go(web, T1, only=["pages", "feeds"])
    assert part(r, "feeds")[-1]["ok"] is True and "no blog feed" in part(r, "feeds")[-1]["notes"][0]
    assert not [p for p in part(r, "feeds") if not p["ok"]]


# ---- who is watched ---------------------------------------------------------------------------------------------------------------------------

def brands_with(tmp_path, spec):
    root = tmp_path / "brands"
    out = []
    for slug, file in spec.items():
        (root / slug).mkdir(parents=True)
        (root / slug / "observatory.yaml").write_text(yaml.safe_dump(file), encoding="utf-8")
        out.append(types.SimpleNamespace(slug=slug))
    return out, str(root)


def test_two_brands_watching_one_competitor_read_it_once_and_exclusions_are_honoured(tmp_path):
    brands, root = brands_with(tmp_path, {
        "a": {"competitors": [{"name": "Shop", "domain": "shop.example", "pages": [BASE + "/pages/a"]}, {"name": "Other", "domain": "other.example"}], "exclude_domains": ["other.example"]},
        "b": {"competitors": [{"name": "Shop Inc", "domain": "www.shop.example/", "pages": [BASE + "/pages/b"], "monitor": {"sitemap": False}}, {"name": "Third", "domain": "third.example"}]}})
    sites = MO.plan_sites(brands, settings(), root=root)
    assert sorted(sites) == ["shop-example", "third-example"] or sorted(sites) == ["shop-example", "third-example", "www-shop-example"]
    shop = sites["shop-example"]
    assert shop["brands"] == ["a", "b"] and shop["comp"]["pages"] == [BASE + "/pages/a", BASE + "/pages/b"]
    assert shop["comp"]["monitor"]["sitemap"] is True                                  # one brand wants it, so it is read
    assert "other-example" not in sites


def test_a_brand_with_no_file_and_no_research_competitors_watches_nothing(tmp_path):
    (tmp_path / "brands" / "x").mkdir(parents=True)
    assert MO.plan_sites([types.SimpleNamespace(slug="x")], settings(), root=str(tmp_path / "brands")) == {}


def test_discovered_competitors_are_added_and_never_override_a_listed_one(tmp_path):
    brands, root = brands_with(tmp_path, {"a": {"competitors": [{"name": "Shop", "domain": "shop.example"}]}})
    found = {"a": [{**COMP, "id": "found-example", "domain": "found.example", "origin": "discovery"}, {**COMP, "name": "Dupe", "origin": "discovery"}]}
    sites = MO.plan_sites(brands, settings(), root=root, discovered=found)
    assert sorted(sites) == ["found-example", "shop-example"] and sites["shop-example"]["comp"]["name"] == "Shop"


def test_several_sites_run_together_in_order_and_one_failure_does_not_stop_the_rest(env):
    sites = {"shop-example": {"comp": COMP, "brands": ["a"]}, "bad-example": {"comp": {**COMP, "id": "bad-example", "domain": "bad.example"}, "brands": ["a"]}}

    def make():
        w = world(1)
        return client(w, env.cfg, ledger=env.ledger)
    original = MO.monitor_site

    def flaky(comp, **kw):
        if comp["id"] == "bad-example":
            raise RuntimeError("boom")
        return original(comp, **kw)
    MO.monitor_site, saved = flaky, MO.monitor_site
    try:
        results = MO.run_sites(sites, make_client=make, snaps=env.snaps, settings=env.cfg, hl=env.hl, now=T1, workers=2)
    finally:
        MO.monitor_site = saved
    assert [r["site"] for r in results] == ["bad-example", "shop-example"] and results[0]["stopped"] and results[0]["events"] == [] and results[1]["stopped"] == ""
    assert len(results[1]["events"]) == 5
    assert [r["site"] for r in MO.run_sites(sites, make_client=make, snaps=env.snaps, settings=env.cfg, hl=env.hl, now=T2, ids={"shop-example"}, workers=1)] == ["shop-example"]


def test_the_stale_report_says_how_old_each_reading_is(env):
    env.go(world(1), T1)
    rows = {r["kind"]: r for r in MO.stale_report(env.snaps, "shop-example", "2026-11-20T10:00:00Z", env.cfg)}
    assert rows["catalogue"]["age_days"] == 19.0 and rows["catalogue"]["stale"] is True
    fresh = {r["kind"]: r for r in MO.stale_report(env.snaps, "shop-example", "2026-11-03T10:00:00Z", env.cfg)}
    assert fresh["catalogue"]["stale"] is False and json.dumps(fresh)
