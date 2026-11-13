"""Public-profile snapshots: what a competitor's public social profile shows (no login), kept as dated snapshots, compared with last time, shown in the sheet."""
import types

import pytest

from contentintel import contract as K
from contentintel import health as HL
from contentintel import session_live as SL
from contentintel import sheets
from contentintel import sources as SRC
from contentintel.observatory import acceptance_checks as AC
from contentintel.observatory import config as OC
from contentintel.observatory import htmlx
from contentintel.observatory import monitor as MO
from contentintel.observatory import report as REP
from contentintel.observatory import snapshots as SN
from contentintel.observatory import social_profiles as SP
from contentintel.store import Store
from conftest import ROOT
from fake_sheets import FakeClient, read
from observatory_helpers import settings

T1, T2, T3 = "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z", "2026-11-03T10:00:00Z"
TIKTOK_TEXT = ("# Swoveralls ## swoveralls ### 405Following 73.3KFollowers 1.8MLikes ## Sweatpants + Overalls = Swoveralls FALL LIMITED EDITION PRINT DROP NOW LIVE "
               "👇 linktr.ee/swoveralls Log in Log in")
LINKEDIN_TEXT = ("Skip to main content LinkedIn - Top Content - People - Learning - Jobs - Games Join now Sign in # Swoveralls ## Retail #### Creating the world comfiest products using "
                 "sustainable resources and ethical manufacturing methods. Follow - View all 2 employees - Report this company ## About us Swoveralls are the comfiest overalls ever made. ## Website swoveralls.com")
X_TEXT = ("SWOVERALLS 743 posts # SWOVERALLS @swoveralls Sweatpants + Overalls = Swoveralls™. The comfiest thing ya never knew you needed. New York, NY swoveralls.com Joined November 2017 "
          "122 Following 976 Followers Mention Follow")
IG_ROWS = [{"url": "https://www.instagram.com/swoveralls_/?hl=en", "title": "SWOVERALLS (@swoveralls_) • Instagram photos and ...", "text": "51K followers · 1.9K+ following · 1325 posts · @swoveralls_: “The Comfiest Overalls On Earth”"},
           {"url": "https://www.instagram.com/swoveralls/", "title": "Swoveralls (@swoveralls) • Instagram photos and videos", "text": "12.5K Followers, 300 Following, 410 Posts - Swoveralls (@swoveralls): “Comfy overalls.”"}]


def page(text, **kw):
    return {"ok": True, "status": 200, "final_url": "https://www.tiktok.com/@swoveralls?lang=en", "title": "Swoveralls (@swoveralls) | TikTok", "text": text, **kw}


# ---- reading the numbers and words -----------------------------------------------------------------------------------------------------------------

def test_counts_are_read_as_people_write_them():
    assert [SP.parse_count(*a) for a in (("73.3", "K"), ("1.8", "M"), ("1,234", ""), ("405", ""), ("1.2", "b"), ("1.9", "K"), ("12", None))] == [73300, 1800000, 1234, 405, 1200000000, 1900, 12]
    assert [SP.parse_count(*a) for a in (("", ""), ("abc", ""), ("1.2.3", ""), (None, ""), ("   ", "K"))] == [None] * 5
    assert SP.metric("405Following 73.3KFollowers 1.8MLikes", "following") == 405 and SP.metric("405Following 73.3KFollowers 1.8MLikes", "followers") == 73300
    assert SP.metric("51K followers · 1.9K+ following · 1325 posts", "following") == 1900 and SP.metric("122 Following 976 Followers", "followers") == 976
    assert SP.metric("View all 2 employees", "employees") == 2 and SP.metric("no numbers here", "followers") is None and SP.metric("abc1234 followers", "followers") is None


def test_each_platforms_real_public_page_gives_its_numbers_name_and_words():
    tt = SP.parse_profile("tiktok", TIKTOK_TEXT)
    assert tt["metrics"] == {"followers": 73300, "following": 405, "likes": 1800000} and tt["name"] == "Swoveralls" and tt["bio"] == "Sweatpants + Overalls = Swoveralls FALL LIMITED EDITION PRINT DROP NOW LIVE 👇 linktr.ee/swoveralls"
    li = SP.parse_profile("linkedin", LINKEDIN_TEXT)
    assert li["metrics"] == {"employees": 2} and li["name"] == "Swoveralls" and li["bio"].startswith("Creating the world comfiest products") and li["about"] == "Swoveralls are the comfiest overalls ever made."
    x = SP.parse_profile("x", X_TEXT)
    assert x["metrics"] == {"followers": 976, "following": 122, "posts": 743} and x["name"] == "SWOVERALLS" and x["joined"] == "November 2017" and x["bio"].endswith("New York, NY swoveralls.com") and "Joined" not in x["bio"]
    ig = SP.parse_profile("instagram", "51K followers · 1.9K+ following · 1325 posts · @swoveralls_: “Comfy”", "SWOVERALLS (@swoveralls_) • Instagram photos and ...")
    assert ig["metrics"] == {"followers": 51000, "following": 1900, "posts": 1325} and ig["name"] == "SWOVERALLS"
    assert SP.parse_profile("tiktok", "") == {"name": "", "bio": "", "joined": "", "about": "", "metrics": {}} and SP.parse_profile("linkedin", None)["metrics"] == {}
    assert SP.parse_profile("tiktok", "completely unrelated words " * 20)["metrics"] == {} and SP.parse_profile("facebook", "x", "Swoveralls | Facebook")["name"] == "Swoveralls"


def test_profile_addresses_and_which_handles_are_read(tmp_path):
    assert SP.profile_url("tiktok", "@swoveralls") == "https://www.tiktok.com/@swoveralls" and SP.profile_url("linkedin", "swoveralls") == "https://www.linkedin.com/company/swoveralls"
    assert SP.profile_url("instagram", "swove ralls/../x") == "https://www.instagram.com/swove%20ralls%2F..%2Fx/" and SP.profile_url("x", "swoveralls") == "https://x.com/swoveralls" and SP.profile_url("facebook", "swoveralls") == "https://www.facebook.com/swoveralls/"
    cfg = settings()
    assert cfg["social"] == {"max_per_competitor": 4, "tiktok": True, "linkedin": True, "instagram": True, "facebook": False, "x": False, "min_change_pct": 5.0, "max_discovery_per_run": 12}
    comp = {"tiktok": "@filetok", "instagram": "", "linkedin": "", "x": "filex", "facebook": ""}
    found = {"tiktok": "pagetok", "instagram": "pageig", "linkedin": "pageli", "facebook": "pagefb", "x": "pagex"}
    assert SP.handles_for(comp, found, cfg) == [("tiktok", "filetok"), ("linkedin", "pageli"), ("instagram", "pageig")]                       # the file wins; only platforms that are on; in the fixed order
    on = settings(social__facebook=True, social__x=True)
    assert [p for p, _ in SP.handles_for(comp, found, on)] == ["tiktok", "linkedin", "instagram", "facebook"]                                  # five are on, the limit is four: X, the last, is left out
    assert SP.handles_for({}, {}, cfg) == [] and SP.handles_for(comp, found, settings(social__max_per_competitor=0)) == [] and len(SP.handles_for(comp, found, settings(social__max_per_competitor=1))) == 1
    assert SP.handles_for({"tiktok": "  @x  "}, {}, cfg) == [("tiktok", "x")]


# ---- the browser read and the snapshot --------------------------------------------------------------------------------------------------------------

def test_the_browser_reader_reads_the_public_page_with_no_login_and_says_why_when_it_cannot(monkeypatch):
    seen = []
    monkeypatch.setattr(SL, "_worker_page", lambda *a, **k: seen.append((a, k)) or page(TIKTOK_TEXT))
    read_ = SP.make_reader(settings(), "me@example.com", "/py", {"X": "1"})
    got = read_("tiktok", "https://www.tiktok.com/@swoveralls")
    assert got == {"text": TIKTOK_TEXT, "title": "Swoveralls (@swoveralls) | TikTok", "landed": "https://www.tiktok.com/@swoveralls", "quality": "real text"}
    (args, kwargs), = seen
    assert args[:4] == ("tiktok", "https://www.tiktok.com/@swoveralls", settings(), "me@example.com") and args[4] == "/py" and kwargs == {"with_session": False, "reading": True}      # no session, and the page is waited for
    monkeypatch.setattr(SL, "_worker_page", lambda *a, **k: {"ok": False, "status": 403, "error": {"code": "PLATFORM_BLOCKED", "message": "tiktok.com still refused after 1 attempt(s)"}})
    with pytest.raises(K.Failure) as seen_exc:
        read_("tiktok", "https://www.tiktok.com/@x")
    assert seen_exc.value.code == K.PLATFORM_BLOCKED and "still refused" in str(seen_exc.value)
    monkeypatch.setattr(SL, "_worker_page", lambda *a, **k: page("Drag the puzzle piece into place Audio"))
    with pytest.raises(K.Failure) as seen_exc:
        read_("tiktok", "https://www.tiktok.com/@x")
    assert seen_exc.value.code == K.PLATFORM_BLOCKED and "a bot-check or puzzle page, not content" in str(seen_exc.value)
    monkeypatch.setattr(SL, "_worker_page", lambda *a, **k: page("InstagramDown chevron icon ![](data:image/png;base64," + "A" * 3000 + ")"))
    with pytest.raises(K.Failure) as seen_exc:
        read_("instagram", "https://www.instagram.com/x/")
    assert seen_exc.value.code == K.UPSTREAM_BROKEN and "script shell" in str(seen_exc.value) and "base64" not in str(seen_exc.value)


def test_a_profile_is_read_into_a_snapshot_and_a_missing_account_is_not_one():
    reader = lambda platform, url: {"text": TIKTOK_TEXT, "title": "T", "landed": "https://www.tiktok.com/@swoveralls", "quality": "real text"}
    snap = SP.snapshot_profile("tiktok", "@swoveralls", reader, T1)
    assert snap["platform"] == "tiktok" and snap["handle"] == "swoveralls" and snap["url"] == "https://www.tiktok.com/@swoveralls" and snap["metrics"]["followers"] == 73300 and snap["complete"] is True
    assert snap["name"] == "Swoveralls" and snap["quality"] == "real text" and snap["text"] == TIKTOK_TEXT and "via" not in snap
    no_numbers = SP.snapshot_profile("tiktok", "x", lambda p, u: {"text": "words about a thing " * 10, "title": "", "landed": "", "quality": "real text"})
    assert no_numbers["complete"] is False and no_numbers["metrics"] == {}
    gone = lambda p, u: {"text": "Couldn't find this account Looking for videos? Try browsing our trending creators", "title": "", "landed": "", "quality": "real text"}
    with pytest.raises(K.Failure) as seen:
        SP.snapshot_profile("tiktok", "nobody", gone)
    assert seen.value.code == K.NOT_FOUND and "no such account or page as 'nobody'" in str(seen.value)
    long_page = lambda p, u: {"text": "Page not found is a phrase in our blog post. " + "real words about our brand " * 60, "title": "", "landed": "", "quality": "real text"}
    assert SP.snapshot_profile("tiktok", "x", long_page)["complete"] is False                                              # a long real page that mentions the phrase is not a missing account
    assert len(SP.snapshot_profile("tiktok", "x", lambda p, u: {"text": "w " * 5000, "title": "", "landed": "", "quality": "real text"})["text"]) == SP.TEXT_KEPT


# ---- what changed ---------------------------------------------------------------------------------------------------------------------------------

def snap(platform="tiktok", bio="Comfy overalls", **metrics):
    return {"platform": platform, "handle": "swoveralls", "metrics": metrics, "bio": bio, "name": "Swoveralls"}


def diff(old, new, **cfg):
    return SP.diff_profile("shop-example", "Shop", old, new, settings(**cfg), T2)


def test_a_count_that_moved_enough_is_an_event_and_noise_is_not():
    assert diff(snap(followers=100), snap(followers=104)) == []                                                          # 4% is under the owner's 5%
    e, = diff(snap(followers=100), snap(followers=106))
    assert e["type"] == "social_metric_changed" and e["severity"] == "info" and e["summary"] == "Shop TikTok followers: 100 to 106 (+6.0%)" and e["before"] == 100 and e["after"] == 106
    assert e["details"] == {"platform": "tiktok", "metric": "followers", "change": 6, "pct": 6.0} and e["scope"] == "social" and e["subject"] == {"kind": "profile", "key": "tiktok-followers"} and e["tier"] == "public_polite"
    assert diff(snap(followers=73300), snap(followers=73400)) == [] and diff(snap(followers=20), snap(followers=21)) == []         # rounding noise, and a handful of people on a small account
    big, = diff(snap(followers=1000), snap(followers=1300))
    assert big["severity"] == "notable" and big["summary"].endswith("1,000 to 1,300 (+30.0%)")
    down, = diff(snap(followers=1000), snap(followers=900))
    assert down["summary"].endswith("1,000 to 900 (-10.0%)") and down["severity"] == "info" and down["details"]["change"] == -100
    assert [x["details"]["metric"] for x in diff(snap(followers=1000, likes=500, employees=2, posts=10, following=5), snap(followers=1100, likes=600, employees=3, posts=99, following=50))] == ["followers", "likes", "employees"]
    emp, = diff(snap("linkedin", employees=2), snap("linkedin", employees=3))
    assert emp["summary"] == "Shop LinkedIn employees: 2 to 3 (+50.0%)" and emp["severity"] == "notable"
    assert diff(snap(followers=100), snap(followers=100)) == [] and diff(snap(), snap(followers=100)) == [] and diff(snap(followers=100), snap()) == [] and diff(snap(followers=0), snap(followers=100)) == []
    assert len(diff(snap(followers=100), snap(followers=103), social__min_change_pct=0)) == 0 and len(diff(snap(followers=1000), snap(followers=1010), social__min_change_pct=0)) == 1       # the owner's threshold, and a floor for rounding
    assert len(diff(snap(followers=1000), snap(followers=1100), social__min_change_pct=20)) == 0


def test_a_changed_bio_is_an_event_and_only_when_there_are_words_on_both_sides():
    e, = diff(snap(bio="Comfy overalls"), snap(bio="Comfy overalls, now in corduroy"))
    assert e["type"] == "social_bio_changed" and e["summary"] == "Shop changed its TikTok bio" and e["before"] == "Comfy overalls" and e["after"] == "Comfy overalls, now in corduroy" and e["severity"] == "info"
    assert diff(snap(bio="Comfy   overalls"), snap(bio="comfy overalls")) == [] and diff(snap(bio=""), snap(bio="New")) == [] and diff(snap(bio="Old"), snap(bio="")) == [] and diff(snap(bio="Same"), snap(bio="Same")) == []


# ---- the search layer for platforms that block a browser ----------------------------------------------------------------------------------------------

@pytest.fixture
def search_rows(monkeypatch):
    calls = []

    def fake(url, environ, providers):
        calls.append(url)
        return list(rows_for[0])
    rows_for = [IG_ROWS]
    monkeypatch.setattr(SL, "discovery_rows", fake)
    return types.SimpleNamespace(calls=calls, rows=rows_for)


def test_the_search_layer_gives_the_exact_account_and_never_a_near_match(search_rows):
    ask = SP.make_discovery({}, [("serper_dev", None)], limit=3)
    got = ask("instagram", "swoveralls", T1)
    assert got["metrics"] == {"followers": 12500, "following": 300, "posts": 410} and got["handle"] == "swoveralls" and got["quality"] == "search snippet" and got["via"] == "search" and got["complete"] is True
    assert got["landed"] == "https://www.instagram.com/swoveralls/" and got["bio"] == "Comfy overalls." and got["name"] == "Swoveralls" and got["url"] == "https://www.instagram.com/swoveralls/" and search_rows.calls == ["https://www.instagram.com/swoveralls/"]
    near = ask("instagram", "swoveralls_", T1)
    assert near["metrics"]["followers"] == 51000 and near["bio"] == "The Comfiest Overalls On Earth"                      # the other account, asked for by its own handle
    search_rows.rows[0] = [IG_ROWS[0]]
    with pytest.raises(K.Failure) as seen:
        ask("instagram", "swoveralls", T1)
    assert seen.value.code == K.NOT_FOUND and "no result for exactly Instagram 'swoveralls'" in str(seen.value) and "it found swoveralls_: other accounts are never used" in str(seen.value)
    with pytest.raises(K.Failure) as seen:
        ask("instagram", "swoveralls_", T1)                                                                              # the fourth search: over the owner's limit of three
    assert seen.value.code == K.RATE_LIMITED and "limit of 3 profile searches" in str(seen.value) and len(search_rows.calls) == 3
    search_rows.rows[0] = []
    with pytest.raises(K.Failure) as seen:
        SP.make_discovery({}, [], limit=5)("instagram", "x")
    assert seen.value.code == K.NOT_FOUND and "it found" not in str(seen.value)
    search_rows.rows[0] = [{"url": "https://www.instagram.com/x/", "title": "x on Instagram", "text": "photos and videos"}]
    with pytest.raises(K.Failure) as seen:
        SP.make_discovery({}, [], limit=5)("instagram", "x")
    assert seen.value.code == K.UPSTREAM_BROKEN and "gave no follower, like or post count" in str(seen.value)
    search_rows.rows[0] = [{"url": "https://x.com/Swoveralls", "title": "SWOVERALLS (@swoveralls) / X", "text": "The latest posts from @swoveralls. 976 Followers 122 Following 743 posts"}]
    assert SP.make_discovery({}, [], limit=5)("x", "swoveralls")["metrics"] == {"followers": 976, "following": 122, "posts": 743}                 # the handle is matched without regard to case
    assert [SP._handle_of(p, u) for p, u in (("tiktok", "https://www.tiktok.com/@Swo/video/1"), ("linkedin", "https://www.linkedin.com/company/swo/"), ("facebook", "https://www.facebook.com/Swo?ref=1"),
                                             ("x", "https://twitter.com/Swo"), ("instagram", "https://example.com/swo"), ("instagram", ""))] == ["swo", "swo", "swo", "swo", "", ""]


def make_reader_pair(browser=None, discovery=None):
    return SP.ProfileReader(browser, discovery)


def test_the_profile_reader_tries_the_browser_for_tiktok_and_linkedin_then_the_search_and_goes_straight_to_the_search_for_the_rest():
    calls = []
    good = lambda platform, url: calls.append(("browser", platform)) or {"text": TIKTOK_TEXT, "title": "T", "landed": url, "quality": "real text"}
    found = lambda platform, handle, now="": calls.append(("search", platform)) or {"platform": platform, "handle": handle, "metrics": {"followers": 5}, "via": "search"}
    blocked = lambda platform, url: (_ for _ in ()).throw(K.Failure(K.PLATFORM_BLOCKED, "tiktok.com still refused"))
    assert make_reader_pair(good, found).snapshot("tiktok", "swoveralls", T1)["metrics"]["followers"] == 73300 and calls == [("browser", "tiktok")]
    calls.clear()
    assert make_reader_pair(blocked, found).snapshot("tiktok", "swoveralls", T1)["via"] == "search" and calls == [("search", "tiktok")]
    calls.clear()
    assert make_reader_pair(good, found).snapshot("instagram", "swoveralls", T1)["via"] == "search" and calls == [("search", "instagram")]                         # never the browser for Instagram
    calls.clear()
    assert make_reader_pair(None, found).snapshot("tiktok", "swoveralls", T1)["via"] == "search" and calls == [("search", "tiktok")]
    with pytest.raises(K.Failure) as seen:
        make_reader_pair(blocked, None).snapshot("tiktok", "swoveralls", T1)
    assert seen.value.code == K.PLATFORM_BLOCKED and str(seen.value) == "tiktok.com still refused"
    with pytest.raises(K.Failure) as seen:
        make_reader_pair(good, None).snapshot("instagram", "swoveralls", T1)
    assert seen.value.code == K.NEED_SETUP and "Instagram profiles need the browser" in str(seen.value)
    gone = lambda platform, url: (_ for _ in ()).throw(K.Failure(K.NOT_FOUND, "TikTok says there is no such account"))
    calls.clear()
    with pytest.raises(K.Failure) as seen:
        make_reader_pair(gone, found).snapshot("tiktok", "nobody", T1)
    assert seen.value.code == K.NOT_FOUND and calls == []                                                              # a missing account is not looked for again
    no_result = lambda platform, handle, now="": (_ for _ in ()).throw(K.Failure(K.NOT_FOUND, "the search found no result"))
    with pytest.raises(K.Failure) as seen:
        make_reader_pair(blocked, no_result).snapshot("tiktok", "swoveralls", T1)
    assert seen.value.code == K.PLATFORM_BLOCKED and str(seen.value) == "tiktok.com still refused; and the search fallback: the search found no result"
    with pytest.raises(K.Failure) as seen:
        make_reader_pair(None, no_result).snapshot("instagram", "swoveralls", T1)
    assert seen.value.code == K.NOT_FOUND and str(seen.value) == "the search found no result"


def test_the_run_builds_its_reader_from_what_is_available_and_what_the_owner_allows(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(SL, "discovery_providers", lambda env: [("serper_dev", None)] if env.get("SERPER_API_KEY") else [])
    reader, note = SP.build_reader(cfg, "me@example.com", {"SCRAPLING_PYTHON": "/py", "SERPER_API_KEY": "k"})
    assert isinstance(reader, SP.ProfileReader) and reader.browser is not None and reader.discovery is not None and note == "social profiles: read through the browser and the search layer"
    reader, note = SP.build_reader(cfg, "me@example.com", {"SCRAPLING_PYTHON": "/py"})
    assert reader.browser is not None and reader.discovery is None and note == "social profiles: read through the browser (no search service key: the platforms that block a browser are skipped)"
    reader, note = SP.build_reader(cfg, "me@example.com", {"SERPER_API_KEY": "k"})
    assert reader.browser is None and reader.discovery is not None and note == "social profiles: read through no browser and the search layer"
    assert SP.build_reader(cfg, "me@example.com", {"SCRAPLING_PYTHON": "/py", "SERPER_API_KEY": "k"}, browser_allowed=False)[0].browser is None
    none, note = SP.build_reader(cfg, "me@example.com", {})
    assert none is None and "neither is available here, so they were not read" in note
    assert SP.build_reader(settings(social__max_per_competitor=0), "me@example.com", {"SCRAPLING_PYTHON": "/py"}) == (None, "")
    assert SP.build_reader(settings(social__tiktok=False, social__linkedin=False, social__instagram=False), "me@example.com", {"SCRAPLING_PYTHON": "/py"}) == (None, "")
    assert SP.build_reader(settings(social__max_discovery_per_run=0), "me@example.com", {"SERPER_API_KEY": "k"})[0] is None
    assert SP.build_reader(settings(social__max_discovery_per_run=0), "me@example.com", {"SERPER_API_KEY": "k", "SCRAPLING_PYTHON": "/py"})[0].discovery is None


# ---- inside a monitoring run ----------------------------------------------------------------------------------------------------------------------------

COMP = {"id": "shop-example", "name": "Shop", "domain": "shop.example", "kind": "store", "pages": [], "blog": "", "feeds": [], "youtube": "", "instagram": "", "tiktok": "swoveralls",
        "monitor": {"shopify": "auto", "sitemap": True, "feeds": True, "pages": True}, "origin": "test"}


@pytest.fixture
def env(tmp_path):
    registry = SRC.load_sources(ROOT / "sources.yaml")
    e = types.SimpleNamespace(tmp=tmp_path, snaps=SN.SnapshotStore(tmp_path), hl=HL.HealthLedger.load(Store(tmp_path), registry), cfg=settings(), text=[TIKTOK_TEXT], fail=[None])

    def browser(platform, url):
        if e.fail[0]:
            raise e.fail[0]
        return {"text": e.text[0], "title": "T", "landed": url, "quality": "real text"}
    e.browser = browser

    def go(at, comp=COMP, only=("social",), cfg=None, reader="default"):
        rd = SP.ProfileReader(browser, None) if reader == "default" else reader
        return MO.monitor_site(comp, client=types.SimpleNamespace(report=lambda: {}), snaps=e.snaps, settings=cfg or e.cfg, hl=e.hl, now=at, only=list(only) if only else None, social_reader=rd)
    e.go = go
    return e


def test_the_first_reading_is_a_baseline_an_unchanged_one_is_quiet_and_a_change_is_an_event(env):
    r = env.go(T1)
    assert r["stopped"] == "" and list(r["parts"]) == ["social"] and [e["type"] for e in r["events"]] == ["baseline_recorded"]
    p, = r["parts"]["social"]
    assert p["ok"] and p["kind"] == "profile-tiktok" and p["baseline"] is True and p["written"] is True and "TikTok profile @swoveralls" in r["events"][0]["summary"]
    kept = env.snaps.latest("shop-example", "profile-tiktok")[1]
    assert kept["metrics"] == {"followers": 73300, "following": 405, "likes": 1800000} and kept["captured_at"] == T1 and "profile-tiktok" in env.snaps.kinds("shop-example")
    assert env.go(T2)["events"] == [] and env.snaps.history("shop-example", "profile-tiktok")[-1]["confirmed_at"] == T2 and len(env.snaps.history("shop-example", "profile-tiktok")) == 1
    env.text[0] = TIKTOK_TEXT.replace("73.3KFollowers", "80.1KFollowers").replace("FALL LIMITED EDITION", "WINTER")
    r = env.go(T3)
    assert sorted(e["type"] for e in r["events"]) == ["social_bio_changed", "social_metric_changed"]
    metric_event = next(e for e in r["events"] if e["type"] == "social_metric_changed")
    assert metric_event["summary"] == "Shop TikTok followers: 73,300 to 80,100 (+9.3%)" and len(env.snaps.history("shop-example", "profile-tiktok")) == 2
    assert [e["type"] for e in env.snaps.read_events("shop-example")] == ["baseline_recorded", "social_metric_changed", "social_bio_changed"] or len(env.snaps.read_events("shop-example")) == 3


def test_a_platform_that_refuses_the_runner_is_reported_and_never_stops_the_competitors_own_monitoring(env):
    env.fail[0] = K.Failure(K.PLATFORM_BLOCKED, "tiktok.com still refused after 1 attempt(s)")
    r = env.go(T1)
    assert r["stopped"] == "" and r["events"] == []
    p, = r["parts"]["social"]
    assert p["ok"] is False and p["code"] == K.PLATFORM_BLOCKED and "still refused" in p["message"] and p["route"] == "social_profile"
    rows = {(x["source"], x["route"]): x for x in env.hl.rows(T1)}
    assert ("competitor_pages", "social_profile") in rows and rows[("competitor_pages", "social_profile")]["last_success"] in ("", None)
    env.fail[0] = None
    r = env.go(T2)
    assert r["parts"]["social"][0]["ok"] and {(x["source"], x["route"]): x for x in env.hl.rows(T2)}[("competitor_pages", "social_profile")]["last_success"] == T2


def test_the_social_part_runs_only_when_asked_for_reader_present_and_switched_on(env):
    assert env.go(T1, only=("sitemap",))["parts"].get("social") is None                                                  # not in what was asked for
    assert env.go(T1, only=("social",), reader=None)["parts"] == {}                                                       # no reader: nothing is read, nothing fails
    off = dict(COMP, monitor={**COMP["monitor"], "social": False})
    assert env.go(T1, comp=off)["parts"] == {}                                                                            # this competitor's own switch
    assert env.go(T1, cfg=settings(social__max_per_competitor=0))["parts"] == {} and env.go(T1, cfg=settings(social__tiktok=False))["parts"] == {}
    nobody = dict(COMP, tiktok="")
    assert env.go(T1, comp=nobody)["parts"] == {}                                                                         # no handle known
    env.snaps.write_social("shop-example", {"tiktok": "foundtok", "linkedin": "foundli"}, T1)
    r = env.go(T2, comp=nobody)
    assert sorted(p["kind"] for p in r["parts"]["social"]) == ["profile-linkedin", "profile-tiktok"]                       # the accounts its own pages linked to


def test_the_other_parts_are_unchanged_when_no_social_reader_is_given(env):
    r = MO.monitor_site(dict(COMP, tiktok="x"), client=types.SimpleNamespace(report=lambda: {}), snaps=env.snaps, settings=env.cfg, hl=env.hl, now=T1, only=["sitemap"])
    assert "social" not in r["parts"] and MO.PARTS == ("pages", "feeds", "sitemap", "shopify", "social")
    both = env.go(T1, comp=dict(COMP, tiktok="swoveralls", instagram="swoveralls"), reader=SP.ProfileReader(env.browser, lambda p, h, now="": {"platform": p, "handle": h, "metrics": {"followers": 5}, "bio": "b", "complete": True, "via": "search"}))
    assert sorted(p["kind"] for p in both["parts"]["social"]) == ["profile-instagram", "profile-tiktok"]


# ---- where a competitor's own accounts come from ----------------------------------------------------------------------------------------------------------

def test_a_competitors_own_pages_name_its_linkedin_facebook_and_x_accounts_too_and_the_store_keeps_them(tmp_path):
    links = ["https://www.linkedin.com/company/Swoveralls/", "https://www.linkedin.com/company/swoveralls?trk=x", "https://www.facebook.com/Swoveralls", "https://www.facebook.com/sharer.php?u=1",
             "https://www.facebook.com/groups/x", "https://twitter.com/swoveralls", "https://x.com/swoveralls/", "https://x.com/intent/tweet", "https://x.com/share", "https://www.instagram.com/swoveralls/",
             "https://www.tiktok.com/@swoveralls", "https://www.facebook.com/abc", "https://example.com/swoveralls"]
    assert htmlx.social_links(links) == {"instagram": "swoveralls", "tiktok": "swoveralls", "linkedin": "swoveralls", "facebook": "swoveralls", "x": "swoveralls"}
    assert htmlx.social_links(["https://www.facebook.com/one.page", "https://www.facebook.com/one.page", "https://www.facebook.com/other.page"]) == {"facebook": "one.page"} and htmlx.social_links([]) == {}
    assert htmlx.social_links(["https://www.facebook.com/sharer.php"] * 3 + ["https://www.facebook.com/realpage"]) == {"facebook": "realpage"}                      # a share link that is more common than the account is still not an account
    assert htmlx.social_links(["https://x.com/share"] * 3 + ["https://twitter.com/intent"] * 3 + ["https://x.com/realacct"]) == {"x": "realacct"}
    assert htmlx.social_links(["https://www.facebook.com/groups"] * 3 + ["https://www.facebook.com/realpage"]) == {"facebook": "realpage"}
    snaps = SN.SnapshotStore(tmp_path)
    snaps.write_social("shop", {"instagram": "i", "tiktok": "t", "youtube": "@y", "linkedin": "l", "facebook": "f", "x": "xx", "myspace": "no", "pinterest": "no"}, T1)
    assert snaps.read_social("shop") == {"instagram": "i", "tiktok": "t", "youtube": "@y", "linkedin": "l", "facebook": "f", "x": "xx"} and SN.SOCIAL_KEYS == ("instagram", "tiktok", "youtube", "linkedin", "facebook", "x")


def test_the_owners_settings_for_profiles_are_checked(tmp_path):
    assert "social" in OC._MONITOR_KEYS and OC.DEFAULTS["social"]["facebook"] is False and OC.DEFAULTS["social"]["x"] is False and OC.DEFAULTS["social"]["instagram"] is True
    path = tmp_path / "observatory.yaml"
    path.write_text("social:\n  max_per_competitor: 2\n  facebook: true\n  min_change_pct: 10\n", encoding="utf-8")
    loaded = OC.load_settings(str(path))
    assert loaded["social"]["max_per_competitor"] == 2 and loaded["social"]["facebook"] is True and loaded["social"]["min_change_pct"] == 10 and loaded["social"]["tiktok"] is True
    for bad in ("max_per_competitor: 7", "max_per_competitor: -1", "min_change_pct: 101", "max_discovery_per_run: 61", "myspace: true"):
        path.write_text(f"social:\n  {bad}\n", encoding="utf-8")
        with pytest.raises(OC.ObservatoryConfigError):
            OC.load_settings(str(path))


# ---- the route is proved live, and shown in the sheet ----------------------------------------------------------------------------------------------------

def test_the_acceptance_probe_reads_a_major_public_profile_and_is_skipped_without_a_browser(monkeypatch):
    cfg = settings()
    checks = AC.make_checks("shop.example", cfg, "me@example.com", T1, client=types.SimpleNamespace())
    probe = checks[("competitor_pages", "social_profile")]
    monkeypatch.delenv("SCRAPLING_PYTHON", raising=False)
    from contentintel.acceptance import Skipped
    with pytest.raises(Skipped) as seen:
        probe()
    assert "browser (the Scrapling environment) is not installed here" in str(seen.value)
    monkeypatch.setenv("SCRAPLING_PYTHON", "/py")
    asked = []
    monkeypatch.setattr(SP, "make_reader", lambda *a, **k: asked.append(a) or "READER")
    monkeypatch.setattr(SP, "snapshot_profile", lambda platform, handle, reader, now: {"metrics": {"followers": 5, "likes": 7}} if (platform, handle, reader) == ("tiktok", "tiktok", "READER") else {})
    assert probe() == (2, "read the public TikTok profile of @tiktok without a login: followers 5, likes 7") and asked[0][1] == "me@example.com"
    monkeypatch.setattr(SP, "snapshot_profile", lambda *a: {"metrics": {}})
    with pytest.raises(K.Failure) as seen:
        probe()
    assert seen.value.code == K.UPSTREAM_BROKEN and "no follower or like count was found" in str(seen.value)
    assert ("competitor_pages", "social_profile") in AC.NEEDS and AC.NEEDS[("competitor_pages", "social_profile")] == "read_social_profile"
    route = SRC.load_sources(ROOT / "sources.yaml").source("competitor_pages").routes["social_profile"]
    assert route.tier == "public_polite" and route.login == "none" and route.access_type == "page_text"


def test_the_sheet_gets_a_social_profiles_tab_with_the_latest_numbers_and_the_change_since_the_reading_before(tmp_path, brands):
    swov = brands["swoveralls"]
    store, snaps = Store(tmp_path / "data"), SN.SnapshotStore(tmp_path / "data")
    sites = [{"id": "shop-example", "name": "Shop", "domain": "shop.example", "origin": "test", "kind": "store", "request_controls": ""}]
    REP.write_brand_sites(store, swov.slug, sites, T1)
    assert SP.sheet_tab(sites, snaps) is None
    first = {"platform": "tiktok", "handle": "swoveralls", "url": "https://www.tiktok.com/@swoveralls", "name": "Swoveralls", "bio": "Comfy", "metrics": {"followers": 1000, "following": 5, "likes": 70}, "quality": "real text", "complete": True}
    snaps.save("shop-example", "profile-tiktok", dict(first, captured_at=T1), T1)
    snaps.save("shop-example", "profile-tiktok", dict(first, captured_at=T2, metrics={"followers": 1100, "following": 5, "likes": 80}), T2)
    snaps.save("shop-example", "profile-linkedin", {"platform": "linkedin", "handle": "swoveralls", "url": "u", "name": "Swoveralls", "bio": "", "about": "About text", "metrics": {"employees": 2}, "quality": "real text", "captured_at": T1}, T1)
    tab = SP.sheet_tab(sites, snaps)
    assert tab["title"] == "Social Profiles" and tab["columns"] == SP.SOCIAL_COLUMNS and tab["key_col"] == "profile_key" and sorted(tab["rows_by_key"]) == ["shop-example|linkedin", "shop-example|tiktok"]
    tt = tab["rows_by_key"]["shop-example|tiktok"]
    assert (tt["competitor"], tt["domain"], tt["platform"], tt["handle"], tt["followers"], tt["followers_change"], tt["followers_change_pct"], tt["likes"], tt["first_read"], tt["last_read"]) == ("Shop", "shop.example", "TikTok", "swoveralls", 1100, 100, 10.0, 80, "2026-11-01", T2)
    li = tab["rows_by_key"]["shop-example|linkedin"]
    assert li["followers"] is None and li["followers_change"] is None and li["employees"] == 2 and li["bio"] == "About text" and li["platform"] == "LinkedIn"
    client = FakeClient()
    (slug, report, error), = sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now=T3, connect_fn=lambda key: (client, "bot@x.iam.gserviceaccount.com"))
    assert error is None
    ws = {w.title: w for w in client.created[0].worksheets()}
    header, rows = read(ws["Social Profiles"])
    assert header[:len(SP.SOCIAL_COLUMNS)] == SP.SOCIAL_COLUMNS and len(rows) == 2
    tt = next(r for r in rows if r["platform"] == "TikTok")
    assert tt["followers"] == 1100 and isinstance(tt["followers"], int) and tt["followers_change"] == 100 and tt["followers_change_pct"] == 10 and tt["likes"] == 80
