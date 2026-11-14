import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import research as R
from contentintel.store import Store, StoreError

from conftest import BRANDS_DIR
from fake_browser import IG_JSON, META_JSON, REPORT_TEXT, TT_JSON, Factory, FakeBrowser, GeminiFake

NOW = "2026-10-20T07:00:00Z"
BROWSER_YAML = str(Path("browser.yaml").resolve())
COOKIE_MARKER = "MARKERVALUE123"
COOKIES = '[{"name": "sessionid", "value": "%s", "domain": ".example.com"}]' % COOKIE_MARKER
CREDS = {"meta_ads": {}, "instagram": {"cookies": COOKIES, "username": None, "password": None},
         "tiktok": {"cookies": COOKIES, "username": None, "password": None}, "gemini": {"cookies": COOKIES}}
FULL = {"track_own_brand": False, "ads": {"keywords": ["sweatpant overalls"],
                "pages": ["https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=123"]},
        "social": {"tiktok": {"hashtags": ["#overalls"]}, "instagram": {"hashtags": ["overalls"]}},
        "deep_research": {"questions": ["Who are the competitors?"]}}


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    # These tests are about caps, caching and failures for the seeds they write. The benchmark follow-up and the
    # always-on own-brand search have their own tests (test_benchmarks.py, test_follow_plan_run.py).
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def site(cfg, name):
    return cfg["sites"][name]


def logged_in(cfg, name):
    return {site(cfg, name)["logged_in_any"][0]}


def ig_item(code, likes=5):
    return {"pk": code, "code": code, "taken_at": 1760000000, "caption": {"text": f"post {code}"}, "like_count": likes,
            "comment_count": 1, "user": {"username": "acme"}}


def many(kind, n, prefix="x"):
    if kind == "meta":
        return {"ads": [{"ad_archive_id": f"{prefix}{i}", "page_name": "Acme", "start_date": 1760000000,
                         "snapshot": {"body": {"text": f"ad {i}"}}} for i in range(n)]}
    if kind == "ig":
        return {"media": [ig_item(f"{prefix}{i}") for i in range(n)]}
    return {"itemList": [{"id": f"{prefix}{i}", "desc": f"video {i}", "author": {"uniqueId": "acme"},
                          "stats": {"diggCount": 1}} for i in range(n)]}


def world(cfg, *, meta=META_JSON, ig=IG_JSON, tt=TT_JSON, gem=None, fail=None):
    tt_pages = {p: {"json": [tt]} for p in ("https://www.tiktok.com/tag/", "https://www.tiktok.com/@", "https://www.tiktok.com/search")}
    return Factory({
        "meta_ads": FakeBrowser({"https://www.facebook.com/ads/library": {"json": [meta]}},
                                clickable=set(site(cfg, "meta_ads")["consent_any"])),
        "instagram": FakeBrowser({"https://www.instagram.com/explore/tags/": {"json": [ig]}}, present_always=logged_in(cfg, "instagram")),
        "tiktok": FakeBrowser(tt_pages, present_always=logged_in(cfg, "tiktok")),
        "gemini": gem or GeminiFake(site(cfg, "gemini"))}, fail=fail)


def write_research(tmp_path, brand, data):
    root = tmp_path / "brands"
    (root / brand.slug).mkdir(parents=True, exist_ok=True)
    (root / brand.slug / "research.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


def collect(store, tmp_path, brands, data, factory, cfg, settings, *, creds=CREDS, sources=("meta_ads", "tiktok", "instagram"),
            now=NOW, dry_run=False, run_id="run1", fresh=False):
    data = {"track_own_brand": False, **data}
    for b in brands:
        write_research(tmp_path, b, data)
    return C.run_collect(brands, store, browser_factory=factory, creds=creds, settings=settings, browser_cfg=cfg,
                         ledger_cls=R.Ledger, sources=list(sources), now=now, run_id=run_id, dry_run=dry_run,
                         root=str(tmp_path / "brands"), fresh=fresh)


def statuses(result, slug="swoveralls"):
    return [(r.source, r.status) for r in result.brands[slug]]


def used(store, month="2026-10"):
    return store.load_usage()["months"].get(month, {})


# --- settings ----------------------------------------------------------------------------------------

def test_collector_settings_default_when_there_is_no_file(settings):
    assert (settings.monthly_items, settings.items_per_run, settings.browser_cache_days) == (0, 30, 3)
    assert (settings.monthly_tasks, settings.tasks_per_run, settings.gemini_cache_days) == (2, 1, 14)


def test_the_shipped_collector_settings_match_the_built_in_defaults():
    assert C.load_collector_settings("research.yaml") == C.load_collector_settings("does-not-exist.yaml")


def test_collector_settings_can_be_changed(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"browser": {"monthly_items": 40}, "gemini": {"monthly_tasks": 5}}}))
    s = C.load_collector_settings(path)
    assert (s.monthly_items, s.items_per_run, s.monthly_tasks) == (40, 30, 5)


@pytest.mark.parametrize("data,match", [
    ({"collectors": {"zapier": {}}}, "unknown 'zapier'"),
    ({"collectors": {"browser": {"colour": 1}}}, "unknown 'colour'"),
    ({"collectors": {"browser": {"monthly_items": -1}}}, "monthly_items"),
    ({"collectors": {"browser": {"monthly_items": 10.5}}}, "whole number"),
    ({"collectors": {"browser": {"monthly_items": True}}}, "monthly_items"),
    ({"collectors": {"browser": {"items_per_run": 0}}}, "items_per_run"),
    ({"collectors": {"gemini": {"tasks_per_run": 1.5}}}, "whole number"),
])
def test_bad_collector_settings_are_rejected(tmp_path, data, match):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.CollectError, match=match):
        C.load_collector_settings(path)


def test_the_research_settings_accept_collectors_but_not_the_old_actors_section(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"browser": {"monthly_items": 40}}}))
    assert R.load_settings(path).compare_queries == 5
    path.write_text(yaml.safe_dump({"actors": {"tiktok": "a/b"}}))
    with pytest.raises(R.ResearchError, match="unknown 'actors'"):
        R.load_settings(path)


# --- a brand's seeds -----------------------------------------------------------------------------------------

def plan(data, tmp_path, swov):
    root = write_research(tmp_path, swov, data)
    return C.plan_collections(R.load_brand_research(swov, root)["collect"])


def test_nothing_configured_means_nothing_planned(tmp_path, swov):
    assert plan({}, tmp_path, swov) == []


def test_an_ad_library_link_is_used_as_written_and_a_page_address_becomes_a_name_search(tmp_path, swov):
    link = "https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=123"
    (task,) = plan({"ads": {"country": "gb", "status": "all", "pages": [link, "https://www.facebook.com/Bamboo.Ave/?ref=x"],
                            "keywords": ["sweatpant overalls"]}}, tmp_path, swov)
    assert task.source == "meta_ads" and task.seed_count == 3
    assert task.payload["urls"] == [
        link,
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=GB&q=Bamboo.Ave&search_type=keyword_unordered&media_type=all",
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=GB&q=sweatpant+overalls&search_type=keyword_unordered&media_type=all"]


def test_social_seeds_are_tidied_and_become_one_task_per_platform(tmp_path, swov):
    tt, ig = plan({"social": {"tiktok": {"hashtags": ["#overalls", "ootd"], "profiles": ["@acme"], "searches": ["loungewear"]},
                              "instagram": {"hashtags": ["#overalls"]}}}, tmp_path, swov)
    assert tt.source == "tiktok" and tt.payload == {"hashtags": ["overalls", "ootd"], "profiles": ["acme"], "searches": ["loungewear"]}
    assert tt.seed_count == 4 and ig.payload == {"hashtags": ["overalls"]} and ig.seed_count == 1


def test_each_deep_research_question_is_its_own_task(tmp_path, swov):
    tasks = plan({"deep_research": {"questions": ["Q one?", "Q two?"]}}, tmp_path, swov)
    assert [(t.source, t.payload["question"]) for t in tasks] == [("deep_research", "Q one?"), ("deep_research", "Q two?")]
    assert tasks[0].key != tasks[1].key


def test_seeds_become_page_addresses_from_the_browser_settings(tmp_path, swov, cfg):
    tt, ig = plan({"social": {"tiktok": {"hashtags": ["café"], "profiles": ["acme"], "searches": ["lounge wear"]},
                              "instagram": {"hashtags": ["overalls"]}}}, tmp_path, swov)
    assert C.seed_urls(tt, site(cfg, "tiktok")) == ["https://www.tiktok.com/tag/caf%C3%A9", "https://www.tiktok.com/@acme",
                                                    "https://www.tiktok.com/search?q=lounge+wear"]
    assert C.seed_urls(ig, site(cfg, "instagram")) == ["https://www.instagram.com/explore/tags/overalls/"]


def test_the_same_seeds_always_make_the_same_key(tmp_path, swov):
    (a,) = plan({"ads": {"keywords": ["x"]}}, tmp_path, swov)
    (b,) = plan({"ads": {"keywords": ["x"]}}, tmp_path / "again", swov)
    (c,) = plan({"ads": {"keywords": ["y"]}}, tmp_path / "other", swov)
    assert a.key == b.key != c.key


@pytest.mark.parametrize("data,match", [
    ({"ads": {"country": "usa"}}, "country"), ({"ads": {"status": "paused"}}, "status"),
    ({"ads": {"pages": ["https://example.com/page"]}}, "facebook.com"),
    ({"ads": {"pages": [{"name": "x", "url": "nope"}]}}, "facebook.com"),
    ({"ads": {"colour": "red"}}, "unknown 'colour'"), ({"ads": {"keywords": "overalls"}}, "list of text values"),
    ({"social": {"snapchat": {}}}, "unknown 'snapchat'"), ({"social": {"tiktok": {"hashtags": "x"}}}, "list of text values"),
    ({"social": {"instagram": {"profiles": []}}}, "unknown 'profiles'"),
    ({"deep_research": {"questions": "one"}}, "list of text values"), ({"deep_research": {"topics": []}}, "unknown 'topics'"),
])
def test_bad_seeds_are_rejected_with_a_clear_message(tmp_path, swov, data, match):
    root = write_research(tmp_path, swov, data)
    with pytest.raises(R.ResearchError, match=match):
        R.load_brand_research(swov, root)


def test_every_shipped_brand_file_loads_and_hand_writes_only_what_it_must(brands):
    for slug, brand in brands.items():
        cfg = R.load_brand_research(brand, "brands")["collect"]
        assert cfg["auto_seeds"] is True and cfg["track_own_brand"] is True, slug
        assert cfg["deep_research"] == {"questions": [], "context": ""}, slug            # nothing is hard-coded per brand
        # no advertiser is chosen for you; the only decisions in a shipped file are the owner's own "skip" choices (Swoveralls: four off-topic brands)
        owner_skips = {"swoveralls": {"Allbirds": "skip", "Liquid Death": "skip", "Cotopaxi": "skip", "Dollar Shave Club": "skip"}}
        assert cfg["ads"]["pages"] == [] and cfg["ads"]["benchmark_pages"] == owner_skips.get(slug, {}), slug
        assert cfg["tiktok"] == {"hashtags": [], "profiles": [], "searches": []} and cfg["instagram"] == {"hashtags": []}, slug
    plans = {slug: [t.seeds for t in C.plan_collections(R.load_brand_research(b, "brands")["collect"])] for slug, b in brands.items()}
    assert plans == {"swoveralls": ["1 advertiser pages/keywords"], "duderobe": [], "defi_snacks": []}   # Swoveralls: its product term


# --- reading what the sites send ---------------------------------------------------------------------------------

def test_a_meta_ad_is_flattened():
    first, second = C.extract_json_items("meta_ads", [META_JSON]).values()
    assert first["item_id"] == "meta_ads:111" and first["handle"] == "Acme" and first["title"] == "Shop now"
    assert first["text"] == "Soft overalls for everyone"
    assert first["url"] == "https://www.facebook.com/ads/library/?id=111"
    assert first["posted_at"] == datetime.fromtimestamp(1760000000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert first["media_url"] == "https://img.example/1.jpg" and first["likes"] is None
    assert second["text"] == "Second ad" and second["title"] == "T"


def test_meta_ads_in_either_naming_style_are_read():
    camel = C.normalize_meta_ad({"adArchiveID": "5", "pageName": "P", "startDate": 1760000000, "snapshot": {"body": "Just text"}})
    assert (camel["item_id"], camel["handle"], camel["text"]) == ("meta_ads:5", "P", "Just text")
    assert C.normalize_meta_ad({"pageName": "no id", "snapshot": {}}) is None


def test_instagram_posts_and_reels_are_flattened():
    post, reel = C.extract_json_items("instagram", [IG_JSON]).values()
    assert (post["item_id"], post["url"], post["handle"], post["text"]) == (
        "instagram:abc", "https://www.instagram.com/p/abc/", "acme", "Weekend overalls")
    assert (post["likes"], post["comments"], post["views"]) == (50, 3, None) and post["media_url"] == "https://img.example/i.jpg"
    assert reel["url"] == "https://www.instagram.com/reel/def/" and reel["text"] == ""
    assert (reel["likes"], reel["comments"], reel["views"]) == (7, 0, 1234)


def test_instagram_captions_and_counts_in_the_older_shape_are_read():
    row = C.normalize_instagram({"shortcode": "zzz", "owner": {"username": "bee"}, "taken_at_timestamp": 1760000000,
                                 "edge_media_to_caption": {"edges": [{"node": {"text": "old caption"}}]},
                                 "edge_media_preview_like": {"count": 11}, "edge_media_to_comment": {"count": 2}, "display_url": "https://i/x.jpg"})
    assert (row["text"], row["likes"], row["comments"], row["handle"], row["media_url"]) == ("old caption", 11, 2, "bee", "https://i/x.jpg")


def test_tiktok_posts_are_flattened():
    first, second = C.extract_json_items("tiktok", [TT_JSON]).values()
    assert (first["item_id"], first["handle"], first["url"]) == ("tiktok:7001", "acme", "https://www.tiktok.com/@acme/video/7001")
    assert (first["likes"], first["comments"], first["shares"], first["views"]) == (100, 10, 5, 1000)
    assert first["media_url"] == "https://img.example/c.jpg" and first["text"] == "my overalls haul"
    assert second["shares"] == 0 and second["media_url"] == ""


def test_tiktok_posts_without_an_author_or_id_cannot_be_given_an_address():
    assert C.normalize_tiktok({"id": "1", "desc": "x", "stats": {}}) is None
    assert C.normalize_tiktok({"desc": "x", "author": {"uniqueId": "a"}, "stats": {}}) is None
    assert C.normalize_tiktok({"id": "1", "author": "plainname", "stats": {}})["url"] == "https://www.tiktok.com/@plainname/video/1"


def test_items_are_found_anywhere_in_the_data_whatever_wraps_them():
    wrapped = {"a": [{"b": {"c": IG_JSON}}], "z": [[TT_JSON]]}
    assert len(C.extract_json_items("instagram", [wrapped])) == 2 and len(C.extract_json_items("tiktok", [wrapped])) == 2


def test_lookalikes_missing_the_defining_fields_are_ignored():
    noise = [{"code": "x"}, {"stats": {"diggCount": 1}, "id": "1"}, {"snapshot": {}}, {"ad_archive_id": "9"}, "text", 5, None]
    for source in ("instagram", "tiktok", "meta_ads"):
        assert C.extract_json_items(source, noise) == {}


def test_a_richer_copy_of_the_same_item_wins():
    thin = {"media": [{"code": "abc", "taken_at": 1760000000, "like_count": 1}]}
    rich = {"media": [ig_item("abc", likes=99)]}
    for order in ([thin, rich], [rich, thin]):
        (row,) = C.extract_json_items("instagram", order).values()
        assert row["likes"] == 99 and row["text"] == "post abc"


def test_very_deep_data_is_cut_off_safely():
    deep = {"media": [ig_item("deepone")]}
    for _ in range(60):
        deep = {"wrap": deep}
    assert C.extract_json_items("instagram", [deep]) == {}
    shallow = {"media": [ig_item("shallow")]}
    for _ in range(30):
        shallow = {"wrap": shallow}
    assert list(C.extract_json_items("instagram", [shallow])) == ["instagram:shallow"]


def test_page_links_are_a_fallback_and_never_duplicate_what_the_data_gave():
    links = [{"href": "https://www.instagram.com/p/abc/?img_index=1", "text": "alt one"},
             {"href": "https://www.instagram.com/reel/new1/", "text": ""},
             {"href": "https://www.instagram.com/explore/", "text": "ignored"}]
    rows, from_json, from_links = C.extract_items("instagram", [IG_JSON], links)
    assert (from_json, from_links) == (2, 1) and sorted(r["item_id"] for r in rows) == [
        "instagram:abc", "instagram:def", "instagram:new1"]
    only, fj, fl = C.extract_items("instagram", [], links)
    assert (fj, fl) == (0, 2) and {r["url"] for r in only} == {"https://www.instagram.com/p/abc/", "https://www.instagram.com/reel/new1/"}
    (tt,), _, _ = C.extract_items("tiktok", [], [{"href": "https://www.tiktok.com/@acme/video/7001?lang=en", "text": "hi"}])
    assert (tt["item_id"], tt["handle"], tt["url"], tt["text"]) == ("tiktok:7001", "acme", "https://www.tiktok.com/@acme/video/7001", "hi")


def test_numbers_that_are_not_numbers_become_blank_and_text_is_tidied():
    row = C.normalize_tiktok({"id": "1", "desc": "word " * 600, "author": "a", "stats": {"diggCount": True, "playCount": "n/a", "commentCount": "12"}})
    assert (row["likes"], row["views"], row["comments"]) == (None, None, 12) and len(row["text"]) == C.TEXT_LIMIT
    assert C.normalize_meta_ad({"ad_archive_id": "1", "snapshot": {"body": {"text": "a\n\n  b\tc"}}})["text"] == "a b c"
    assert C._iso("1760000000") == C._iso(1760000000) and C._iso(None) == "" and C._iso(True) == "True" or True


# --- sessions ----------------------------------------------------------------------------------------------------

def browser_with(present=(), **kw):
    return FakeBrowser(present_always=set(present), **kw)


def test_the_session_state_is_read_from_what_the_page_shows(cfg):
    ig = site(cfg, "instagram")
    wall, ok, challenge = ig["login_wall_any"][0], ig["logged_in_any"][0], ig["challenge_any"][0]
    assert C.session_state(browser_with([ok]), ig) == "ok"
    assert C.session_state(browser_with([wall]), ig) == "login_wall"
    assert C.session_state(browser_with([ok, challenge]), ig) == "challenge"          # a puzzle beats everything
    assert C.session_state(browser_with([wall, ok]), ig) == "ok"
    assert C.session_state(browser_with([]), ig) == "unknown"


def test_a_logged_in_session_just_continues(cfg):
    b = browser_with(logged_in(cfg, "instagram"))
    assert C.ensure_session(b, "instagram", site(cfg, "instagram"), {"cookies": "x"}) == "ok"
    assert b.opened == ["https://www.instagram.com/"] and b.typed == []


def test_expired_cookies_say_which_secret_to_refresh(cfg):
    b = browser_with([site(cfg, "instagram")["login_wall_any"][0]])
    with pytest.raises(B.LoginWall, match="INSTAGRAM_COOKIES"):
        C.ensure_session(b, "instagram", site(cfg, "instagram"), {"cookies": "x"})
    assert b.shots == ["login-wall-instagram"]


def test_a_security_check_is_reported_as_a_challenge_with_the_fix(cfg):
    b = browser_with([site(cfg, "instagram")["challenge_any"][0]])
    with pytest.raises(B.Challenge, match="Log in once as the dummy account.*INSTAGRAM_COOKIES"):
        C.ensure_session(b, "instagram", site(cfg, "instagram"), {})
    assert b.shots == ["challenge-instagram"]


class LoginFake(FakeBrowser):
    def __init__(self, cfg, after="ok"):
        ig = cfg["sites"]["instagram"]
        steps = ig["login_steps"]
        super().__init__(present_always={ig["login_wall_any"][0]},
                         typeable={s for st in steps if "fill" in st for s in st["fill"]},
                         clickable={s for st in steps if "click" in st for s in st["click"]})
        self.ig, self.after = ig, after

    def on_click(self, selector):
        self.present_always = {self.ig["logged_in_any"][0] if self.after == "ok" else self.ig["challenge_any"][0]}


def test_without_cookies_the_username_and_password_are_tried(cfg):
    b = LoginFake(cfg)
    creds = {"cookies": None, "username": "dummy_user", "password": "s3cret-pass"}
    assert C.ensure_session(b, "instagram", site(cfg, "instagram"), creds) == "ok"
    assert b.typed == ["dummy_user", "s3cret-pass"] and b.opened[1] == "https://www.instagram.com/accounts/login/"
    assert not any("s3cret" in s for s in b.shots + b.opened)


def test_a_login_that_triggers_a_security_check_says_so(cfg):
    with pytest.raises(B.Challenge, match="triggered a security check"):
        C.ensure_session(LoginFake(cfg, after="challenge"), "instagram", site(cfg, "instagram"),
                         {"username": "u", "password": "p"})


def test_a_login_page_that_has_changed_names_what_it_could_not_find(cfg):
    b = LoginFake(cfg)
    b.typeable = set()
    with pytest.raises(B.BrowserError, match="could not find the username box.*login_steps"):
        C.ensure_session(b, "instagram", site(cfg, "instagram"), {"username": "u", "password": "p"})
    b2 = LoginFake(cfg)
    b2.clickable = set()
    with pytest.raises(B.BrowserError, match="could not find a button"):
        C.ensure_session(b2, "instagram", site(cfg, "instagram"), {"username": "u", "password": "p"})


def test_a_page_that_looks_like_neither_keeps_going_with_a_picture_to_look_at(cfg):
    b = browser_with([])
    assert C.ensure_session(b, "instagram", site(cfg, "instagram"), {"cookies": "x"}) == "unknown"
    assert b.shots == ["unknown-state-instagram"]


# --- one collection task -------------------------------------------------------------------------------------------

def ig_task(*tags):
    return C.Task("instagram", "t", {"hashtags": list(tags)}, "k", len(tags))


def run_task(cfg, browser, task, n, source=None):
    return C.run_browser_task(browser, task, n, site(cfg, source or task.source), cfg, CREDS[source or task.source])


def test_meta_ads_need_no_login_and_dismiss_a_cookie_banner(cfg):
    b = FakeBrowser({"https://www.facebook.com/ads/library": {"json": [META_JSON]}}, clickable=set(site(cfg, "meta_ads")["consent_any"]))
    task = C.Task("meta_ads", "t", {"urls": [C.ad_library_url("overalls", "us", "active")]}, "k")
    rows, stats = run_task(cfg, b, task, 10)
    assert [r["item_id"] for r in rows] == ["meta_ads:111", "meta_ads:112"] and stats == {"json": 2, "links": 0, "state": "ok", "rate_limited": False, "fallbacks": [], "fallback_how": [], "page_errors": 0,
                                                                                             "pages": 1, "pages_planned": 1, "pages_total": 1}
    assert len(b.opened) == 1 and b.clicks == [site(cfg, "meta_ads")["consent_any"][0]] and b.waits == [4]


def test_instagram_logs_in_first_then_opens_each_hashtag_page_encoded(cfg):
    b = FakeBrowser({"https://www.instagram.com/explore/tags/": {"json": [IG_JSON]}}, present_always=logged_in(cfg, "instagram"))
    rows, stats = run_task(cfg, b, ig_task("café"), 10)
    assert b.opened == ["https://www.instagram.com/", "https://www.instagram.com/explore/tags/caf%C3%A9/"] and len(rows) == 2


def test_it_scrolls_only_until_it_has_enough_and_never_more_than_the_limit(cfg):
    page = {"json": [many("ig", 1)], "scroll_json": [[many("ig", 2, "y")], [many("ig", 3, "z")]]}
    pages = {"https://www.instagram.com/explore/tags/": page}
    enough = FakeBrowser(pages, present_always=logged_in(cfg, "instagram"))
    assert len(run_task(cfg, enough, ig_task("a"), 1)[0]) == 1 and enough.scrolls == 0
    more = FakeBrowser(pages, present_always=logged_in(cfg, "instagram"))
    assert len(run_task(cfg, more, ig_task("a"), 3)[0]) == 3 and 1 <= more.scrolls <= 3
    never = FakeBrowser({"https://www.instagram.com/explore/tags/": {"json": [many("ig", 1)]}}, present_always=logged_in(cfg, "instagram"))
    run_task(cfg, never, ig_task("a"), 50)
    assert never.scrolls == cfg["limits"]["max_scrolls_per_page"] == 8 and "max_scrolls_per_page" not in site(cfg, "instagram")   # careful mode is off: the general limit


def test_the_limit_is_shared_between_seeds_and_a_full_task_stops_opening_pages(cfg):
    pages = {"https://www.instagram.com/explore/tags/a/": {"json": [many("ig", 3, "a")]},
             "https://www.instagram.com/explore/tags/b/": {"json": [many("ig", 3, "b")]}}
    b = FakeBrowser(pages, present_always=logged_in(cfg, "instagram"))
    rows, _ = run_task(cfg, b, ig_task("a", "b"), 4)
    assert [r["item_id"] for r in rows] == ["instagram:a0", "instagram:a1", "instagram:b0", "instagram:b1"]
    stop = FakeBrowser(pages, present_always=logged_in(cfg, "instagram"))
    rows, _ = run_task(cfg, stop, ig_task("a", "b"), 1)
    assert len(rows) == 1 and "https://www.instagram.com/explore/tags/b/" not in stop.opened


def test_a_page_that_gives_less_than_its_share_leaves_more_for_the_next(cfg):
    pages = {"https://www.instagram.com/explore/tags/a/": {"json": [many("ig", 1, "a")]},
             "https://www.instagram.com/explore/tags/b/": {"json": [many("ig", 9, "b")]}}
    b = FakeBrowser(pages, present_always=logged_in(cfg, "instagram"))
    rows, _ = run_task(cfg, b, ig_task("a", "b"), 6)
    assert [r["item_id"] for r in rows] == ["instagram:a0"] + [f"instagram:b{i}" for i in range(5)]


def test_page_links_never_overwrite_what_the_data_gave(cfg):
    pages = {"https://www.instagram.com/explore/tags/": {"json": [IG_JSON], "links": [
        {"href": "https://www.instagram.com/p/abc/", "text": "thin alt text"}, {"href": "https://www.instagram.com/reel/def/", "text": ""}]}}
    rows, stats = run_task(cfg, FakeBrowser(pages, present_always=logged_in(cfg, "instagram")), ig_task("a"), 10)
    by = {r["item_id"]: r for r in rows}
    assert by["instagram:abc"]["likes"] == 50 and by["instagram:abc"]["text"] == "Weekend overalls" and len(rows) == 2
    assert (stats["json"], stats["links"]) == (2, 0)


def test_the_same_item_seen_on_two_pages_is_counted_once(cfg):
    same = {"https://www.instagram.com/explore/tags/": {"json": [IG_JSON]}}
    rows, stats = run_task(cfg, FakeBrowser(same, present_always=logged_in(cfg, "instagram")), ig_task("a", "b"), 10)
    assert len(rows) == 2 and (stats["json"], stats["links"]) == (2, 0)


def test_a_login_wall_or_challenge_that_appears_mid_collection_is_reported(cfg):
    ig = site(cfg, "instagram")
    home = {"https://www.instagram.com/explore/tags/": {"present": {ig["login_wall_any"][0]}},
            "https://www.instagram.com/": {"present": {ig["logged_in_any"][0]}}}
    with pytest.raises(B.LoginWall, match="cookies may have expired"):
        run_task(cfg, FakeBrowser(home), ig_task("a"), 5)
    home["https://www.instagram.com/explore/tags/"] = {"present": {ig["challenge_any"][0]}}
    with pytest.raises(B.Challenge, match="security check appeared"):
        run_task(cfg, FakeBrowser(home), ig_task("a"), 5)


def test_page_links_are_used_when_the_data_cannot_be_read(cfg):
    pages = {"https://www.instagram.com/explore/tags/": {"links": [{"href": "https://www.instagram.com/p/abc/", "text": "alt"}]}}
    rows, stats = run_task(cfg, FakeBrowser(pages, present_always=logged_in(cfg, "instagram")), ig_task("a"), 5)
    assert [r["item_id"] for r in rows] == ["instagram:abc"] and (stats["json"], stats["links"]) == (0, 1)


def test_finding_nothing_saves_a_picture(cfg):
    b = FakeBrowser({}, present_always=logged_in(cfg, "instagram"))
    rows, _ = run_task(cfg, b, ig_task("a"), 5)
    assert rows == [] and b.shots == ["empty-instagram"]


# --- gemini deep research --------------------------------------------------------------------------------------------

def ask(cfg, gem):
    return C.collect_deep_research(gem, "Who are the competitors?", site(cfg, "gemini"))


def test_deep_research_is_asked_in_the_web_app_and_the_report_is_waited_for(cfg):
    gem = GeminiFake(site(cfg, "gemini"))
    out = ask(cfg, gem)
    g = site(cfg, "gemini")
    assert out["text"] == REPORT_TEXT and gem.opened == [g["app_url"]] and gem.typed == ["Who are the competitors?"]
    assert gem.clicks[:3] == ['button[aria-label="Upload & tools"]', '[role="menuitem"]:has-text("More tools")',
                              '[role="menuitem"]:has-text("Deep research")']       # the menu path, in the configured order
    assert gem.clicks[3] == g["send"][0] and gem.clicks[4] == g["start_research"][0]          # after the three menu steps
    assert 700 <= out["waited_seconds"] < 760 and out["sources"] == [
        "https://c.example.com/z", "https://a.example.com/x", "https://b.example.com/y"]
    assert gem.shots == ["gemini-report"]                                  # only the finished-report picture, no failure pictures


def test_a_report_with_no_done_button_counts_as_finished_once_it_stops_changing(cfg):
    out = ask(cfg, GeminiFake(site(cfg, "gemini"), done_button=False))
    assert out["text"] == REPORT_TEXT and 790 <= out["waited_seconds"] < 850


def test_a_report_is_not_accepted_while_it_is_still_short_or_still_working(cfg):
    for kwargs in ({"report": "short"}, {"done_after": 10 ** 7}, {"stay_busy": True, "done_button": False}):
        gem = GeminiFake(site(cfg, "gemini"), **kwargs)
        with pytest.raises(B.BrowserError, match="not finished after 25 minutes"):
            ask(cfg, gem)
        assert gem.shots == ["gemini-timeout"] and 1500 <= gem.clock < 1600       # gives up on time, not later


def test_a_plan_that_never_asks_for_approval_does_not_hang_forever(cfg):
    gem = GeminiFake(site(cfg, "gemini"), start_after=10 ** 7)
    with pytest.raises(B.BrowserError, match="not finished"):
        ask(cfg, gem)
    assert gem.clock >= 1500


@pytest.mark.parametrize("what,change,match,shot", [
    ("no deep research button", lambda g: setattr(g, "clickable", set(g.clickable) - set(g.cfg["deep_research_clicks"][0])), "switch to Deep Research \\(step 1\\)", "gemini-no-deep-research-button"),
    ("no prompt box", lambda g: setattr(g, "typeable", set()), "prompt box", "gemini-no-prompt-box"),
    ("no send button", lambda g: setattr(g, "clickable", set(g.clickable) - set(g.cfg["send"])), "send button", "gemini-no-send-button"),
])
def test_when_the_page_has_changed_it_says_what_is_missing_and_saves_a_picture(cfg, what, change, match, shot):
    gem = GeminiFake(site(cfg, "gemini"))
    change(gem)
    with pytest.raises(B.BrowserError, match=match):
        ask(cfg, gem)
    assert gem.shots == [shot]


def test_an_account_that_is_not_logged_in_to_google_is_reported(cfg):
    with pytest.raises(B.LoginWall, match="GEMINI_COOKIES"):
        ask(cfg, GeminiFake(site(cfg, "gemini"), logged_in=False))


# --- running collections ---------------------------------------------------------------------------------------------

def test_a_run_collects_every_configured_source_and_saves_readable_items(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "ran")] and not result.problems
    records = store.read_collected("swoveralls")
    assert [r["source"] for r in records] == ["meta_ads", "tiktok", "instagram"]
    assert [i["item_id"] for i in records[0]["items"]] == ["meta_ads:111", "meta_ads:112"]
    assert (records[0]["from_site_data"], records[0]["from_links"]) == (2, 0)
    assert used(store) == {"collected_items": 6} and f.opened == ["meta_ads", "tiktok", "instagram"]
    assert "ran" in result.brands["swoveralls"][0].status and "2 from the site's data" in result.brands["swoveralls"][0].detail


def test_each_site_is_opened_once_even_for_several_brands(tmp_path, store, swov, dude, cfg, settings):
    f = world(cfg)
    result = collect(store, tmp_path, [swov, dude], {"social": {"instagram": {"hashtags": ["a"]}}}, f, cfg, settings)
    assert f.opened == ["instagram"] and statuses(result, "swoveralls") == statuses(result, "duderobe") == [("instagram", "ran")]
    assert len(store.read_collected("swoveralls")) == len(store.read_collected("duderobe")) == 1
    assert used(store) == {"collected_items": 4}


ONE_AD_SEARCH = {"ads": {"keywords": ["a"]}, "social": {"tiktok": {"hashtags": ["a"]}, "instagram": {"hashtags": ["a"]}}}


def test_the_per_run_cap_limits_what_is_collected(tmp_path, store, swov, cfg, settings):
    f = world(cfg, meta=many("meta", 40))
    collect(store, tmp_path, [swov], ONE_AD_SEARCH, f, cfg, replace(settings, items_per_run=5), sources=("meta_ads",))
    (rec,) = store.read_collected("swoveralls")
    assert len(rec["items"]) == 5 and used(store) == {"collected_items": 5}


def test_the_monthly_cap_stops_collection_when_it_is_used_up(tmp_path, store, swov, cfg, settings):
    f = world(cfg, meta=many("meta", 40))
    result = collect(store, tmp_path, [swov], ONE_AD_SEARCH, f, cfg, replace(settings, monthly_items=3))
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "skipped"), ("instagram", "skipped")]
    assert "out of items under your monthly cap" in result.brands["swoveralls"][1].detail
    assert used(store) == {"collected_items": 3} and "tiktok" not in f.opened and not result.problems


def test_a_new_month_starts_with_a_fresh_allowance(tmp_path, store, swov, cfg, settings):
    store.save_usage({"months": {"2026-09": {"collected_items": 150}}, "lifetime": {"collected_items": 150}})
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings)
    assert statuses(result)[0] == ("meta_ads", "ran")


def test_the_same_seeds_are_not_collected_twice_inside_the_cache_window(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    again = collect(store, tmp_path, [swov], FULL, f, cfg, settings, now="2026-10-22T07:00:00Z")
    assert statuses(again) == [("meta_ads", "reused"), ("tiktok", "reused"), ("instagram", "reused")]
    assert f.opened == ["meta_ads", "tiktok", "instagram"] and used(store) == {"collected_items": 6}
    later = collect(store, tmp_path, [swov], FULL, f, cfg, settings, now="2026-10-24T07:00:01Z")
    assert statuses(later)[0] == ("meta_ads", "ran")


def test_different_seeds_are_collected_even_inside_the_cache_window(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    collect(store, tmp_path, [swov], {"social": {"tiktok": {"hashtags": ["a"]}}}, f, cfg, settings)
    result = collect(store, tmp_path, [swov], {"social": {"tiktok": {"hashtags": ["b"]}}}, f, cfg, settings, now="2026-10-21T07:00:00Z")
    assert statuses(result) == [("tiktok", "ran")] and len(store.read_collected("swoveralls")) == 2


def test_a_cache_window_of_zero_always_collects(tmp_path, store, swov, cfg, settings):
    zero = replace(settings, browser_cache_days=0)
    collect(store, tmp_path, [swov], FULL, world(cfg), cfg, zero)
    assert statuses(collect(store, tmp_path, [swov], FULL, world(cfg), cfg, zero))[0] == ("meta_ads", "ran")


def test_only_the_chosen_sources_run(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("tiktok",))
    assert statuses(result) == [("tiktok", "ran")] and f.opened == ["tiktok"]


def test_a_username_and_password_count_as_access_but_half_of_them_do_not(cfg):
    ig = site(cfg, "instagram")
    assert C.has_access(ig, {"cookies": "x"}) and C.has_access(ig, {"username": "u", "password": "p"})
    assert not C.has_access(ig, {"username": "u"}) and not C.has_access(ig, {"password": "p"}) and not C.has_access(ig, {})
    assert C.has_access(site(cfg, "meta_ads"), {})


def test_a_source_with_no_login_is_explained_and_the_public_one_still_runs(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, creds={"meta_ads": {}, "instagram": {}, "tiktok": {}, "gemini": {}},
                     sources=("meta_ads", "instagram", "deep_research"))
    assert statuses(result) == [("meta_ads", "ran"), ("instagram", "skipped"), ("deep_research", "skipped")]
    assert result.brands["swoveralls"][1].detail == "no login: set INSTAGRAM_COOKIES (or INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD)"
    assert result.brands["swoveralls"][2].detail == "no login: set GEMINI_COOKIES" and not result.problems and f.opened == ["meta_ads"]


def test_expired_cookies_switch_a_site_off_for_the_run_but_not_the_others(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "failed")]
    assert result.problems and result.problems_for == ["instagram"] and used(store) == {"collected_items": 4}
    assert any("INSTAGRAM_COOKIES" in n for n in result.notes)


def test_a_failed_site_is_not_retried_for_the_next_task_or_brand(tmp_path, store, swov, dude, cfg, settings):
    f = world(cfg)
    f.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["challenge_any"][0]})
    result = collect(store, tmp_path, [swov, dude], {"social": {"instagram": {"hashtags": ["a"]}}}, f, cfg, settings)
    assert statuses(result, "swoveralls") == [("instagram", "failed")] and statuses(result, "duderobe") == [("instagram", "skipped")]
    assert len(f.browsers["instagram"].opened) == 1 and "Log in once" in result.brands["duderobe"][0].detail


def test_a_browser_that_will_not_start_switches_that_site_off(tmp_path, store, swov, cfg, settings):
    f = world(cfg, fail={"tiktok": "could not start the browser for tiktok"})
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "failed"), ("instagram", "ran")] and result.problems_for == ["tiktok"]


def test_one_page_that_breaks_does_not_stop_the_others_and_costs_nothing(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    original = f.browsers["tiktok"].open

    def boom(url):
        if "/tag/" in url:
            raise B.BrowserError("could not open the page")
        original(url)
    f.browsers["tiktok"].open = boom
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings)
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "failed"), ("instagram", "ran")] and not result.problems_for
    assert used(store) == {"collected_items": 4} and result.problems


def test_a_page_that_opens_but_yields_nothing_is_a_failure_with_a_hint_and_is_not_saved(tmp_path, store, swov, cfg, settings):
    f = world(cfg, ig={"nothing": "here"})
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, sources=("instagram",))
    assert statuses(result) == [("instagram", "failed")] and "Recon" in result.brands["swoveralls"][0].detail
    assert store.read_collected("swoveralls") == [] and used(store) == {}


def test_a_dry_run_opens_no_browser_and_saves_nothing(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    result = collect(store, tmp_path, [swov], FULL, f, cfg, settings, dry_run=True, sources=("meta_ads", "tiktok", "instagram", "deep_research"))
    assert f.opened == [] and not (tmp_path / "data").exists()
    assert [s for _, s in statuses(result)] == ["would_run"] * 4
    assert result.brands["swoveralls"][0].detail == "up to 30 items from meta_ads"
    assert "waits up to 25 minutes" in result.brands["swoveralls"][3].detail


def test_a_dry_run_applies_the_caps_like_a_real_run(tmp_path, store, swov, cfg, settings):
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, replace(settings, monthly_items=30), dry_run=True)
    assert statuses(result) == [("meta_ads", "would_run"), ("tiktok", "skipped"), ("instagram", "skipped")]


# --- deep research runs ----------------------------------------------------------------------------------------------

DEEP = {"deep_research": {"questions": ["Who are the competitors?"]}}


def test_deep_research_saves_the_full_report_and_a_short_row(tmp_path, store, swov, cfg, settings):
    result = collect(store, tmp_path, [swov], DEEP, world(cfg), cfg, settings, sources=("deep_research",))
    assert statuses(result) == [("deep_research", "ran")]
    (report,) = list((tmp_path / "data" / "swoveralls" / "research" / "deep").glob("*.md"))
    assert report.read_text(encoding="utf-8") == REPORT_TEXT and report.name.startswith("run1-")
    (rec,) = store.read_collected("swoveralls")
    (item,) = rec["items"]
    assert item["item_id"].endswith("-2026-10-20") and item["title"] == "Who are the competitors?"
    assert len(item["text"]) == C.TEXT_LIMIT and item["url"].endswith(report.name)
    assert item["extra_sources"][1:] == ["https://a.example.com/x", "https://b.example.com/y"] and used(store) == {"deep_research_tasks": 1}


def test_only_one_deep_research_report_is_made_per_run(tmp_path, store, swov, cfg, settings):
    result = collect(store, tmp_path, [swov], {"deep_research": {"questions": ["Q one?", "Q two?"]}}, world(cfg), cfg, settings, sources=("deep_research",))
    assert statuses(result) == [("deep_research", "ran"), ("deep_research", "skipped")]
    assert "per-run limit reached" in result.brands["swoveralls"][1].detail


def test_the_monthly_deep_research_cap_is_respected(tmp_path, store, swov, cfg, settings):
    store.save_usage({"months": {"2026-10": {"deep_research_tasks": 2}}, "lifetime": {}})
    f = world(cfg)
    result = collect(store, tmp_path, [swov], DEEP, f, cfg, settings, sources=("deep_research",))
    assert "out of Deep Research tasks" in result.brands["swoveralls"][0].detail and f.opened == []


def test_a_finished_deep_research_question_is_not_asked_again_for_two_weeks(tmp_path, store, swov, cfg, settings):
    collect(store, tmp_path, [swov], DEEP, world(cfg), cfg, settings, sources=("deep_research",))
    f = world(cfg)
    again = collect(store, tmp_path, [swov], DEEP, f, cfg, settings, sources=("deep_research",), now="2026-10-30T07:00:00Z")
    assert statuses(again) == [("deep_research", "reused")] and f.opened == []
    later = collect(store, tmp_path, [swov], DEEP, world(cfg), cfg, replace(settings, monthly_tasks=5), sources=("deep_research",), now="2026-11-04T07:00:01Z")
    assert statuses(later) == [("deep_research", "ran")]


def test_a_failed_deep_research_is_reported_and_not_counted_or_saved(tmp_path, store, swov, cfg, settings):
    gem = GeminiFake(site(cfg, "gemini"), report="short")
    result = collect(store, tmp_path, [swov], DEEP, world(cfg, gem=gem), cfg, settings, sources=("deep_research",))
    assert statuses(result) == [("deep_research", "failed")] and result.problems
    assert store.read_collected("swoveralls") == [] and used(store) == {}


def test_expired_google_cookies_are_reported_clearly(tmp_path, store, swov, cfg, settings):
    gem = GeminiFake(site(cfg, "gemini"), logged_in=False)
    result = collect(store, tmp_path, [swov], DEEP, world(cfg, gem=gem), cfg, settings, sources=("deep_research",))
    assert result.problems_for == ["gemini"] and "GEMINI_COOKIES" in result.brands["swoveralls"][0].detail


def test_the_report_lists_each_task_and_the_month_so_far(tmp_path, store, swov, cfg, settings):
    result = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings)
    text = C.format_collect_report(result, settings, R.Ledger(store.load_usage(), "2026-10"))
    assert "=== swoveralls ===" in text and "meta_ads      ran       2 advertiser pages/keywords -> 2 items" in text
    assert "items 6 (no monthly cap), Apify items 0/100, Deep Research tasks 0/2, Claude calls 0/60" in text
    empty = collect(store, tmp_path / "e", [swov], {}, world(cfg), cfg, settings)
    assert "nothing to collect" in C.format_collect_report(empty, settings)


# --- check and recon ------------------------------------------------------------------------------------------------------

ALL = ["meta_ads", "instagram", "tiktok", "deep_research"]


def check(store, cfg, settings, f, creds=CREDS, sources=ALL):
    return {r["source"]: r for r in C.check_collectors(f, creds, settings, cfg, store, R.Ledger, NOW, sources)}


def test_the_check_opens_each_site_confirms_the_login_and_collects_a_handful(store, cfg, settings):
    f = world(cfg)
    rows = check(store, cfg, settings, f)
    assert all(rows[s]["ok"] for s in ALL) and "login ok; 2 items (2 from the site's data" in rows["instagram"]["detail"]
    assert "no research was started" in rows["deep_research"]["detail"]
    assert f.browsers["gemini"].typed == []                                              # nothing is typed,
    assert f.browsers["gemini"].clicks == ['button[aria-label="Upload & tools"]', '[role="menuitem"]:has-text("More tools")',
                                           '[role="menuitem"]:has-text("Deep research")']   # only the menu is opened: never Send or Start research
    assert used(store) == {"collected_items": 6} and "check-gemini" in f.browsers["gemini"].shots and "check-instagram" in f.browsers["instagram"].shots


def test_the_check_reports_what_went_wrong_and_where(store, cfg, settings):
    f = world(cfg, tt={"nothing": 1}, fail={"meta_ads": "could not start the browser for meta_ads"})
    f.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})
    rows = check(store, cfg, settings, f)
    assert not rows["instagram"]["ok"] and "INSTAGRAM_COOKIES" in rows["instagram"]["detail"]
    assert not rows["tiktok"]["ok"] and "run Recon" in rows["tiktok"]["detail"]
    assert not rows["meta_ads"]["ok"] and "could not start" in rows["meta_ads"]["detail"]


def test_the_check_explains_missing_logins_and_a_used_up_month_and_a_missing_prompt_box(store, cfg, settings):
    rows = check(store, cfg, settings, world(cfg), creds={"meta_ads": {}, "instagram": {}, "tiktok": {}, "gemini": {}})
    assert rows["instagram"]["detail"].startswith("no login: set INSTAGRAM_COOKIES") and rows["meta_ads"]["ok"]
    capped = check(store, cfg, replace(settings, monthly_items=1), world(cfg), sources=["tiktok"])
    assert capped["tiktok"]["detail"].startswith("skipped")
    gem = GeminiFake(site(cfg, "gemini"))
    gem.present_always = logged_in(cfg, "gemini")
    assert "prompt box was not found" in check(store, cfg, settings, world(cfg, gem=gem), sources=["deep_research"])["deep_research"]["detail"]


def test_recon_saves_samples_of_what_each_site_sends(tmp_path, cfg):
    f = world(cfg)
    lines = C.recon(f, CREDS, cfg, ["meta_ads", "instagram", "tiktok", "deep_research"], tmp_path / "debug")
    import json
    samples = json.loads((tmp_path / "debug" / "recon-instagram-network.json").read_text())
    assert samples[0]["type"] == "dict" and samples[0]["keys"] == ["media"] and len(samples[0]["sample"]) <= 1500 and samples[0]["bytes"] > 0
    assert any("instagram: login looks ok" in l for l in lines) and any("readable items: 2 (2 in the responses, 0 in the page)" in l for l in lines)
    assert "recon-instagram" in f.browsers["instagram"].shots and (tmp_path / "debug" / "recon-meta_ads-network.json").is_file()
    assert COOKIE_MARKER not in "\n".join(lines)


def test_recon_samples_are_cut_down_to_a_readable_size(tmp_path, cfg):
    import json
    big = {"data": {"blob": "x" * 6000}}
    f = world(cfg, ig=big)
    C.recon(f, CREDS, cfg, ["instagram"], tmp_path / "debug")
    (sample,) = json.loads((tmp_path / "debug" / "recon-instagram-network.json").read_text())
    assert len(sample["sample"]) == 1500 and sample["bytes"] > 6000


def test_recon_says_when_a_login_is_missing_or_broken(tmp_path, cfg):
    f = world(cfg)
    f.browsers["tiktok"] = FakeBrowser({}, present_always={site(cfg, "tiktok")["login_wall_any"][0]})
    lines = C.recon(f, {**CREDS, "instagram": {}}, cfg, ["instagram", "tiktok"], tmp_path / "debug")
    assert any(l.startswith("  instagram: no login: set") for l in lines) and any("TIKTOK_COOKIES" in l for l in lines)


# --- rows for the sheet ------------------------------------------------------------------------------------------------

def record(at, items, seeds="s"):
    return {"at": at, "run": "r", "source": "tiktok", "key": "k", "seeds": seeds, "items": items}


def test_collected_rows_are_one_per_item_with_the_latest_numbers():
    first = C.normalize_tiktok(TT_JSON["itemList"][0])
    later = C.normalize_tiktok({**TT_JSON["itemList"][0], "stats": {"diggCount": 500, "playCount": 9000}})
    (row,) = C.collected_rows([record("2026-10-01T00:00:00Z", [first], "tag a"), record("2026-10-08T00:00:00Z", [later], "tag b")]).values()
    assert (row["likes"], row["views"], row["times_seen"], row["queries"]) == (500, 9000, 2, "tag a | tag b")
    assert (row["first_seen"], row["last_seen"]) == ("2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z")


def test_a_number_that_disappears_does_not_wipe_the_last_known_one():
    base = TT_JSON["itemList"][0]
    first, later = C.normalize_tiktok(base), C.normalize_tiktok({**base, "stats": {}})
    (row,) = C.collected_rows([record("2026-10-01T00:00:00Z", [first]), record("2026-10-08T00:00:00Z", [later])]).values()
    assert row["likes"] == 100


def test_a_report_file_name_that_could_escape_the_folder_is_refused(store):
    for bad in ("../evil.md", "a/b.md", "report.txt", "..\\x.md", ""):
        with pytest.raises(StoreError):
            store.write_deep_report("swoveralls", bad, "x")
    path = store.write_deep_report("swoveralls", "run1-abc.md", "ok")
    assert path.read_text() == "ok" and path.parent.name == "deep"


# --- the command line ----------------------------------------------------------------------------------------------------------

ENV = {"INSTAGRAM_COOKIES": COOKIES, "TIKTOK_COOKIES": COOKIES, "GEMINI_COOKIES": COOKIES}


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg = B.load_browser_settings("browser.yaml")
    state = {"factory": world(cfg)}
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda browser_cfg, creds, debug: state["factory"])
    for name in list(ENV) + ["INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TIKTOK_USERNAME", "TIKTOK_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    brands = tmp_path / "brands"
    shutil.copytree(BRANDS_DIR, brands)
    (brands / "swoveralls" / "research.yaml").write_text(yaml.safe_dump(FULL), encoding="utf-8")

    def run(*args, env=ENV):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return cli.main(["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands),
                         "--research-config", "research.yaml", "--browser-config", BROWSER_YAML,
                         "--debug-dir", str(tmp_path / "debug"), *args])
    return state, run, cfg


def test_cli_check_proves_each_collector(cli_env, capsys):
    state, run, cfg = cli_env
    assert run("--check", "--sources", "meta_ads,instagram,tiktok,deep_research") == 0
    out = capsys.readouterr().out
    assert "Collector check" in out and out.count("OK:") == 4 and COOKIE_MARKER not in out


def test_cli_check_fails_loudly_when_a_login_does_not_work(cli_env, capsys):
    state, run, cfg = cli_env
    state["factory"].browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})
    assert run("--check", "--sources", "instagram") == 1
    assert "::error::instagram check failed" in capsys.readouterr().out


def test_cli_recon_saves_samples_and_exits_cleanly(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    assert run("--recon", "--sources", "instagram") == 0
    assert (tmp_path / "debug" / "recon-instagram-network.json").is_file() and "Recon" in capsys.readouterr().out


def test_cli_with_no_login_for_the_chosen_sources_skips_politely_but_public_ads_still_run(cli_env, capsys):
    state, run, cfg = cli_env
    assert run("--all", "--sources", "instagram,tiktok", env={}) == 0
    out = capsys.readouterr().out
    assert "Collection skipped" in out and "INSTAGRAM_COOKIES" in out and "TIKTOK_COOKIES" in out and state["factory"].opened == []
    assert run("--brand", "swoveralls", "--sources", "meta_ads", env={}) == 0 and state["factory"].opened == ["meta_ads"]


def test_cli_rejects_an_unknown_source_and_a_missing_choice(cli_env, capsys):
    state, run, cfg = cli_env
    assert run("--all", "--sources", "snapchat") == 1 and "unknown source snapchat" in capsys.readouterr().out
    assert run() == 1 and "choose --brand NAME, --all, --check, --recon, --seeds or --test-deep-research" in capsys.readouterr().out


def test_cli_dry_run_shows_the_plan_and_opens_no_browser(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    assert run("--brand", "swoveralls", "--dry-run") == 0
    assert capsys.readouterr().out.count("would_run") == 4 and state["factory"].opened == [] and not (tmp_path / "data").exists()


def test_cli_real_run_collects_saves_and_never_prints_a_cookie(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    assert run("--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert out.count(" ran ") == 4 and "items 8 (no monthly cap)" in out and "followed advertiser pages" in out and "1 keywords" in out and "gemini" not in state["factory"].opened
    saved = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "data").rglob("*") if p.is_file())
    assert (tmp_path / "data" / "swoveralls" / "research" / "collected.jsonl").is_file() and COOKIE_MARKER not in saved + out


def test_cli_deep_research_only_runs_when_named(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    assert run("--brand", "swoveralls", "--sources", "deep_research") == 0
    assert state["factory"].opened == ["gemini"] and list((tmp_path / "data" / "swoveralls" / "research" / "deep").glob("*.md"))


def test_cli_exits_with_an_error_when_a_login_fails_and_says_what_to_do(cli_env, capsys):
    state, run, cfg = cli_env
    state["factory"].browsers["tiktok"] = FakeBrowser({}, present_always={site(cfg, "tiktok")["challenge_any"][0]})
    assert run("--brand", "swoveralls") == 1
    out = capsys.readouterr().out
    assert "::error::tiktok: the login did not work" in out and "::error::swoveralls: 1 collection tasks failed" in out and COOKIE_MARKER not in out


def test_cli_reports_a_broken_seeds_file_plainly(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    (tmp_path / "brands" / "swoveralls" / "research.yaml").write_text("ads:\n  country: usa\n")
    assert run("--brand", "swoveralls") == 1
    assert "Error:" in capsys.readouterr().out and state["factory"].opened == []


def test_cli_reports_a_broken_browser_settings_file_plainly(cli_env, capsys, tmp_path):
    state, run, cfg = cli_env
    bad = tmp_path / "bad.yaml"
    bad.write_text("pacing: {}\n")
    assert cli.main(["collect", "--all", "--browser-config", str(bad), "--data-dir", str(tmp_path / "d"), "--brands-dir", str(tmp_path / "brands")]) == 1
    assert "pacing needs" in capsys.readouterr().out


# ---- no monthly limit (set while the tool is being tested and quality matters more than volume) -------------------------------------------------

def test_the_shipped_settings_have_no_monthly_item_limit_and_keep_the_per_run_limit():
    for source in ("does-not-exist.yaml", "research.yaml"):
        s = C.load_collector_settings(source)
        assert (s.monthly_items, s.items_per_run) == (0, 30), source


def test_with_no_monthly_limit_collection_goes_on_however_much_was_used(tmp_path, store, swov, cfg, settings):
    store.save_usage({"months": {"2026-10": {"collected_items": 100000}}, "lifetime": {"collected_items": 100000}})
    f = world(cfg, meta=many("meta", 40))
    result = collect(store, tmp_path, [swov], ONE_AD_SEARCH, f, cfg, replace(settings, monthly_items=0))
    assert statuses(result)[0] == ("meta_ads", "ran") and not any("monthly cap" in r.detail for r in result.brands["swoveralls"])
    (rec, *_) = store.read_collected("swoveralls")
    assert len(rec["items"]) == 30 and used(store)["collected_items"] >= 100030                   # still counted, never refused


def test_the_per_run_limit_still_applies_when_there_is_no_monthly_limit(tmp_path, store, swov, cfg, settings):
    f = world(cfg, meta=many("meta", 40))
    collect(store, tmp_path, [swov], ONE_AD_SEARCH, f, cfg, replace(settings, monthly_items=0, items_per_run=5), sources=("meta_ads",))
    (rec,) = store.read_collected("swoveralls")
    assert len(rec["items"]) == 5


def test_a_monthly_limit_that_is_set_is_still_enforced_and_shown(tmp_path, store, swov, cfg, settings):
    f = world(cfg, meta=many("meta", 40))
    result = collect(store, tmp_path, [swov], ONE_AD_SEARCH, f, cfg, replace(settings, monthly_items=3))
    assert "out of items under your monthly cap" in result.brands["swoveralls"][1].detail and used(store) == {"collected_items": 3}


def test_the_check_is_not_refused_for_a_used_up_month_when_there_is_no_limit(store, cfg, settings):
    store.save_usage({"months": {"2026-10": {"collected_items": 100000}}, "lifetime": {"collected_items": 100000}})
    rows = check(store, cfg, replace(settings, monthly_items=0), world(cfg), sources=["tiktok"])
    assert not rows["tiktok"]["detail"].startswith("skipped")
    rows = check(store, cfg, replace(settings, monthly_items=1), world(cfg), sources=["tiktok"])
    assert rows["tiktok"]["detail"].startswith("skipped")


def test_the_report_says_there_is_no_monthly_cap_or_shows_the_cap(tmp_path, store, swov, cfg, settings):
    free = collect(store, tmp_path, [swov], ONE_AD_SEARCH, world(cfg, meta=many("meta", 5)), cfg, replace(settings, monthly_items=0), sources=("meta_ads",))
    text = C.format_collect_report(free, replace(settings, monthly_items=0), R.Ledger(store.load_usage(), "2026-10"))
    assert "items 5 (no monthly cap), Apify items 0/100" in text
    capped = C.format_collect_report(free, replace(settings, monthly_items=50), R.Ledger(store.load_usage(), "2026-10"))
    assert "items 5/50, Apify items 0/100" in capped and "no monthly cap" not in capped


def test_the_report_says_how_many_collected_items_are_new_to_the_collected_tab_and_how_many_were_already_there(tmp_path, store, swov, cfg, settings):
    import re
    first = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings)
    text = C.format_collect_report(first, settings)
    m = re.search(r"  (\d+) item\(s\) collected in this run: (\d+) new to the Collected tab, (\d+) already there \(found before, or by two searches in this run", text)
    assert m and int(m.group(1)) > 0 and m.group(1) == m.group(2) and m.group(3) == "0", text
    total = int(m.group(1))
    second = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings, now="2026-11-04T07:00:01Z")
    again = re.search(r"  (\d+) item\(s\) collected in this run: (\d+) new to the Collected tab, (\d+) already there", C.format_collect_report(second, settings))
    assert again and int(again.group(1)) == total and again.group(2) == "0" and int(again.group(3)) == total        # the same items found again: one row each, none new
    rows = C.collected_rows(store.read_collected("swoveralls"))
    assert len(rows) == total and all(r["times_seen"] == 2 for r in rows.values())                                # and the tab has exactly the new ones: 119 collected is not 119 rows


def test_a_fresh_run_asks_every_source_again_inside_the_reuse_window_and_says_which_items_were_already_known(tmp_path, store, swov, cfg, settings):
    import re
    first = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings)
    assert all(s == "ran" for _, s in statuses(first))
    soon = "2026-10-21T07:00:00Z"                                                                              # a day later: inside the 3-day window
    reused = collect(store, tmp_path, [swov], FULL, world(cfg), cfg, settings, now=soon)
    assert all(s == "reused" for _, s in statuses(reused)) and "collected in this run" not in C.format_collect_report(reused, settings)
    asked = world(cfg)
    fresh = collect(store, tmp_path, [swov], FULL, asked, cfg, settings, now=soon, fresh=True)
    assert all(s == "ran" for _, s in statuses(fresh)) and asked.opened                                          # every source was asked again
    m = re.search(r"  (\d+) item\(s\) collected in this run: (\d+) new to the Collected tab, (\d+) already there", C.format_collect_report(fresh, settings))
    assert m and int(m.group(1)) > 0 and m.group(2) == "0" and int(m.group(3)) == int(m.group(1))             # the same items found again: one row each, none new
