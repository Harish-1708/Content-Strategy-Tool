"""Update 62: what the live runs of Update 61 showed. The wheel's missing __init__.py, models the pinned catalogue lacks, TikTok's verification page, an account that needs the owner, human-like browsing, and the
main official page of a benchmark brand (Serper to find it, a reviewer to confirm it, never a regional page when a main one exists, nothing hardcoded)."""
import json
import random
import re

import pytest

from contentintel import benchmarks as bench
from contentintel import browser as B
from contentintel import cli
from contentintel import contract as K
from contentintel import human as H
from contentintel import main_page as MP
from contentintel import session_live as SL
from contentintel.ai import gateway as G
from contentintel.ai import registry as R
from contentintel.providers import Result

from conftest import ROOT

# ---- the wheel: contentintel/__init__.py is not empty (GitHub's web upload skips empty files) --------------------------------------------------------------

def test_the_package_has_a_non_empty_init_and_no_file_in_the_repository_is_empty():
    import contentintel
    init = ROOT / "contentintel" / "__init__.py"
    assert init.stat().st_size > 0 and isinstance(contentintel.__file__, str)                # an empty one was never uploaded, so the installed package had no __file__ (Tests #936)
    skip = {"data", "__pycache__", ".pytest_cache", ".git", "node_modules"}
    empty = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.stat().st_size == 0 and not (skip & set(p.relative_to(ROOT).parts))]
    assert empty == [], f"GitHub's web upload skips empty files, so these would never reach the repository: {empty}"


# ---- TikTok's verification page ------------------------------------------------------------------------------------------------------------------------

VERIFICATION = "Verification An unexpected error occurred. Please try again. Black Friday deals start now " + "Log in Sign up Download the app About Newsroom Contact Careers ByteDance TikTok for Good " * 12


def test_tiktoks_verification_page_is_a_challenge_page_even_when_its_footer_makes_it_long():
    assert len(VERIFICATION) > 600
    assert SL.judge_content(VERIFICATION)[0] == "challenge page"
    assert SL.judge_content("An unexpected error occurred. Please try again. " + "words about our products " * 40)[0] == "challenge page"


def test_a_real_profile_page_is_not_taken_for_a_challenge_page():
    real = "Free the Sheep @freethesheepco 69 Following 122.3K Followers 1.6M Likes Luxury designs for anxiety " + "a video about a hoodie " * 20
    assert SL.judge_content(real)[0] == "real text"
    assert SL.judge_content("Verifying your order shipped: " + "words about a thing " * 60)[0] == "real text"            # 'verifying' is not 'verification '


def test_the_profile_reader_treats_that_page_as_a_block_and_never_asks_again(monkeypatch):
    from test_update60 import page, reader
    read, calls = reader(monkeypatch, [page(VERIFICATION), page(VERIFICATION)])
    from contentintel.observatory import social_profiles as SP
    with pytest.raises(K.Failure) as seen:
        read("tiktok", "https://www.tiktok.com/@madepants")
    assert seen.value.code == K.PLATFORM_BLOCKED and len(calls) == 1
    assert SP.NEEDS_COUNTS                                                                                # (the search layer is tried next for a blocked page, as for Instagram)


# ---- models the pinned catalogue lacks are added through the gateway ------------------------------------------------------------------------------------

class FakeGateway:
    def __init__(self, have, post_status=201):
        self.have, self.post_status, self.posts = list(have), post_status, []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        if url.endswith("/api/auth/login"):
            return 200, {"token": "t"}, {}
        if method == "GET" and url.endswith("/api/models"):
            return 200, [{"platform": p, "model_id": m} for p, m in self.have], {}
        if method == "POST" and url.endswith("/api/models"):
            self.posts.append(body)
            return self.post_status, ({"error": {"message": "no"}} if self.post_status >= 400 and self.post_status != 409 else {}), {}
        return 404, {}, {}


def sidecar(http):
    side = G.Sidecar("/x", {}, http=http)
    side.base_url = "http://gw"
    return side


REG = R.load_registry(ROOT / "ai_providers.yaml")
KILO_NEW = [("kilo", "kilo-auto/free"), ("kilo", "nvidia/nemotron-3.5-super-vl:free")]


def test_approved_models_the_catalogue_lacks_are_added_for_a_provider_that_is_switched_on_and_for_no_other():
    http = FakeGateway(have=[("kilo", "poolside/laguna-m.1:free"), ("groq", "openai/gpt-oss-120b")])
    added, problems = sidecar(http).add_missing_models("pw", REG, {})
    assert sorted(added) == sorted(f"{p}:{m}" for p, m in KILO_NEW) and problems == []                      # Kilo needs no key; nothing is added for a keyed provider without its key
    assert {(b["platform"], b["modelId"]) for b in http.posts} == set(KILO_NEW) and all(set(b) == {"platform", "modelId", "displayName"} for b in http.posts)
    http = FakeGateway(have=[])
    added, _ = sidecar(http).add_missing_models("pw", REG, {"GEMINI_API_KEY": "k"})
    assert ("google:gemma-4-31b-it" in added) and ("groq:openai/gpt-oss-120b" not in added)
    http = FakeGateway(have=[])
    added, _ = sidecar(http).add_missing_models("pw", REG, {"ANTHROPIC_API_KEY": "k", "OPENROUTER_API_KEY": "k"})
    assert not [a for a in added if a.startswith(("claude:", "openrouter:"))] and not [b for b in http.posts if b["platform"] in ("claude", "openrouter")]      # Claude and OpenRouter never go through the gateway, key or not                  # with a key, that provider's approved models are added too (and still not Groq's, which has none)


def test_a_model_that_is_already_in_the_gateway_is_left_alone_and_a_refusal_is_reported():
    http = FakeGateway(have=KILO_NEW)
    assert sidecar(http).add_missing_models("pw", REG, {}) == ([], []) and http.posts == []
    added, problems = sidecar(FakeGateway(have=[], post_status=409)).add_missing_models("pw", REG, {})
    assert added == [] and problems == []                                                                  # HTTP 409: it was there already
    added, problems = sidecar(FakeGateway(have=[], post_status=400)).add_missing_models("pw", REG, {})
    assert added == [] and len(problems) == 2 and problems[0].startswith("kilo:kilo-auto/free (HTTP 400: no)")


def test_a_failed_admin_login_adds_nothing_and_says_so():
    def refuse(method, url, **kw):
        return 401, {}, {}
    assert sidecar(refuse).add_missing_models("pw", REG, {}) == ([], ["the admin login failed (401)"])


# ---- human-like browsing --------------------------------------------------------------------------------------------------------------------------------

class Page:
    """A page that records what it is asked to do and refuses anything but looking: moving the mouse, turning the wheel and waiting."""
    def __init__(self):
        self.log, self.gotos = [], []
        self.mouse = self
        self.stock = None

    def move(self, x, y, steps=1):
        self.log.append(("move", x, y, steps))

    def wheel(self, dx, dy):
        self.log.append(("wheel", dy))

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    def goto(self, url, **kw):
        self.gotos.append(url)

    def on(self, *a):
        pass

    def __getattr__(self, name):
        raise AssertionError(f"a human-like read may only look: it asked the page for .{name}")


def human(seed=1, **block):
    sleeps = []
    return H.Human(block or None, rng=random.Random(seed), sleep=sleeps.append), sleeps


def test_the_human_settings_are_checked_and_default_to_on():
    assert H.resolve(None)["enabled"] is True and H.resolve({})["wheel_steps"] == [5, 9] and H.resolve({"enabled": False})["enabled"] is False
    for bad in ({"nope": 1}, {"enabled": "yes"}, {"wheel_steps": [9, 5]}, {"wheel_steps": [0, 3]}, {"settle_seconds": [1]}, {"distraction_chance": 2}, {"reading_variation": -1}, {"break_every_pages": [0, 4]}, [1]):
        with pytest.raises(H.HumanConfigError):
            H.resolve(bad)


def test_the_shipped_browser_yaml_carries_a_valid_human_block_and_the_loader_rejects_a_bad_one(tmp_path):
    cfg = B.load_browser_settings(ROOT / "browser.yaml")
    assert cfg["human"]["enabled"] is True and cfg["human"]["break_every_pages"] == [6, 10]
    text = (ROOT / "browser.yaml").read_text(encoding="utf-8").replace("wheel_steps: [5, 9]", "wheel_steps: [9, 5]")
    (tmp_path / "b.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(B.BrowserError, match="wheel_steps"):
        B.load_browser_settings(tmp_path / "b.yaml")


def test_waits_before_pages_vary_stay_in_range_and_a_longer_break_comes_every_six_to_ten_pages():
    person, sleeps = human(seed=7)
    pacing = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}
    waits, breaks, since = [], [], 0
    for _ in range(60):
        n = len(sleeps)
        person.before_page(pacing)
        since += 1
        waits.append(sleeps[n])
        if len(sleeps) - n == 2:                                                           # a break followed the wait
            breaks.append((since, sleeps[n + 1]))
            since = 0
    assert all(3 <= w <= 180 for w in waits) and len({round(w, 3) for w in waits}) > 30       # never the same pause twice
    assert max(waits) > 8 or min(waits) < 8                                                   # the spread is wider than a plain uniform pause
    assert len(breaks) >= 5 and all(6 <= pages <= 10 and 20 <= secs <= 60 for pages, secs in breaks)


def test_no_pause_is_shorter_than_its_floor_or_longer_than_its_ceiling_however_much_reading_time_varies():
    person, _ = human(seed=11, reading_variation=2.0, distraction_chance=0.0)
    samples = [person.reading_time(5, 3, 8) for _ in range(500)]
    assert min(samples) >= 3 and max(samples) <= 8 and len({round(x, 2) for x in samples}) > 20                 # a wide spread, kept inside the limits
    person, _ = human(seed=12, reading_variation=2.0, distraction_chance=1.0, distraction_factor=[4.0, 4.0])
    assert max(person.reading_time(5, 3, 8) for _ in range(200)) <= H.LONGEST_PAUSE                              # even a distraction never passes the longest pause


def test_a_scroll_is_several_small_turns_with_pauses_a_moving_mouse_and_sometimes_a_little_back():
    person, _ = human(seed=3, scroll_back_chance=1.0)
    page = Page()
    total = person.scroll(page, 4, 3)
    wheels = [e[1] for e in page.log if e[0] == "wheel"]
    downs, backs = [w for w in wheels if w > 0], [w for w in wheels if w < 0]
    assert 4 * 5 <= len(downs) <= 4 * 9 and all(120 <= w <= 420 for w in downs) and len(backs) == 4 and all(80 <= -w <= 300 for w in backs)
    assert total == sum(wheels) and any(e[0] == "move" and 8 <= e[3] <= 22 for e in page.log)
    waits = [e[1] for e in page.log if e[0] == "wait"]
    assert all(40 <= w <= 160 or w >= 300 for w in waits) and len(set(waits)) > 8             # the turns, the little pause after a scroll back, and the reading pauses all differ
    reading = [e[1] for e in page.log if e[0] == "wait" and e[1] >= 1500]
    assert len(reading) >= 4 and all(w >= 1500 for w in reading)                              # at least half the stated pause, every time


def test_with_scroll_back_off_nothing_goes_up_and_with_the_mouse_off_it_never_moves():
    person, _ = human(seed=5, scroll_back_chance=0.0, mouse_moves=False)
    page = Page()
    person.scroll(page, 3, 2)
    person.after_load(page)
    assert all(e[1] > 0 for e in page.log if e[0] == "wheel") and not [e for e in page.log if e[0] == "move"]


def test_after_a_page_opens_the_person_settles_and_moves_the_mouse():
    person, _ = human(seed=2)
    page = Page()
    person.after_load(page)
    assert page.log[0][0] == "wait" and 1500 <= page.log[0][1] <= 4000 and page.log[1][0] == "move"


def test_the_browser_with_a_human_opens_and_scrolls_like_a_person_and_without_one_exactly_as_before(monkeypatch):
    pacing = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}
    slept = []
    monkeypatch.setattr(B.time, "sleep", slept.append)
    plain, page = None, Page()
    plain = B.PlaywrightBrowser(page, "dbg", pacing, [], human=None)
    plain.open("https://www.instagram.com/x/")
    plain.scroll(2, 3)
    wheels = [e for e in page.log if e[0] == "wheel"]
    assert len(wheels) == 2 and all(1800 <= w[1] <= 2600 for w in wheels) and len(slept) == 1 and 3 <= slept[0] <= 8 and not [e for e in page.log if e[0] == "move"]      # the earlier behaviour, unchanged
    person, psleeps = human(seed=4)
    page2 = Page()
    browser = B.PlaywrightBrowser(page2, "dbg", pacing, [], human=person)
    browser.open("https://www.instagram.com/x/")
    assert page2.gotos == ["https://www.instagram.com/x/"] and len(psleeps) == 1 and 3 <= psleeps[0] <= 180
    assert page2.log[0][0] == "wait" and 1500 <= page2.log[0][1] <= 4000 and page2.log[1][0] == "move"          # it settled on the page and moved the mouse before doing anything else
    browser.scroll(2, 3)
    assert len([e for e in page2.log if e[0] == "wheel"]) >= 10


def test_only_a_logged_in_session_gets_a_human_and_the_owner_can_turn_it_off():
    assert isinstance(B.person_for("cookie-json", None), H.Human) and B.person_for("", None) is None             # the public Meta Ads library keeps its plain pauses
    assert B.person_for("cookie-json", {"enabled": False}) is None and B.person_for("cookie-json", {"mouse_moves": False}).cfg["mouse_moves"] is False
    with pytest.raises(H.HumanConfigError):
        B.person_for("cookie-json", {"wheel_steps": [9, 5]})


# ---- the main official page of a benchmark brand --------------------------------------------------------------------------------------------------------

def serp(*urls):
    return [{"url": u, "title": t, "snippet": s} for u, t, s in urls]


CHOBANI_RESULTS = serp(("https://www.facebook.com/ChobaniAu", "Chobani Australia", "Chobani Greek yogurt in Australia"),
                       ("https://www.facebook.com/ChobaniNZ/", "Chobani New Zealand", "Chobani NZ"),
                       ("https://www.facebook.com/Chobani", "Chobani", "Chobani. 1.3M likes. Real food made with natural ingredients."),
                       ("https://www.facebook.com/Chobani/posts/123", "A post", "a post"),
                       ("https://www.facebook.com/groups/chobanifans", "Fans", "a group"),
                       ("https://en.wikipedia.org/wiki/Chobani", "Chobani - Wikipedia", "article"))
DUDE_RESULTS = serp(("https://www.facebook.com/dudewipes", "DUDE Wipes", "DUDE Wipes. 61K likes. Flushable wipes."), ("https://www.dudewipes.com/", "Dude Wipes", "shop"),
                    ("https://www.facebook.com/watch/?v=1", "video", "video"))


def reviewer(main_slug, regional=False, is_main=True, reason="the brand's own primary page"):
    seen = []

    def review(name, candidates):
        seen.append((name, [c["url"] for c in candidates]))
        pick = next((c["url"] for c in candidates if c["slug"].lower() == (main_slug or "").lower()), None)
        return {"choice": pick, "is_main": is_main and pick is not None, "regional": regional, "reason": reason}
    review.seen = seen
    return review


def test_facebook_pages_are_picked_out_of_search_results_once_each_and_posts_groups_and_videos_are_not_pages():
    got = MP.candidates_from(CHOBANI_RESULTS + serp(("https://facebook.com/chobani", "again", "dup")))
    assert [c["slug"] for c in got] == ["ChobaniAu", "ChobaniNZ", "Chobani"]
    assert [c["slug"] for c in MP.candidates_from(DUDE_RESULTS)] == ["dudewipes"]
    assert MP.slug_of("https://www.facebook.com/profile.php?id=5") == "" and MP.slug_of("https://www.facebook.com/pages/Name/123") == "" and MP.slug_of("https://m.facebook.com/Brand?ref=x") == "Brand"


def test_a_regional_page_is_recognised_by_its_address_or_its_title_and_the_brands_own_page_is_not():
    cands = {c["slug"]: c for c in MP.candidates_from(CHOBANI_RESULTS)}
    assert MP.looks_regional("Chobani", cands["ChobaniAu"]) and MP.looks_regional("Chobani", cands["ChobaniNZ"]) and not MP.looks_regional("Chobani", cands["Chobani"])
    assert MP.looks_regional("Brand", {"slug": "brand.uk", "title": "Brand", "snippet": ""}) and MP.looks_regional("Brand", {"slug": "brandshop", "title": "Brand India", "snippet": ""})
    assert not MP.looks_regional("India Hicks", {"slug": "indiahicks", "title": "India Hicks", "snippet": ""})           # a region in the brand's own name is not a region


def test_the_ranking_puts_the_brands_own_page_first_and_regional_pages_last():
    ranked = MP.rank("Chobani", MP.candidates_from(CHOBANI_RESULTS))
    assert [c["slug"] for c in ranked] == ["Chobani", "ChobaniAu", "ChobaniNZ"]
    mixed = MP.candidates_from(serp(("https://www.facebook.com/ChobaniAu", "Chobani Australia", "x"), ("https://www.facebook.com/ChobaniOfficialPage", "Chobani Official", "y"), ("https://www.facebook.com/ChobaniNZ", "Chobani New Zealand", "z")))
    assert [c["slug"] for c in MP.rank("Chobani", mixed)] == ["ChobaniOfficialPage", "ChobaniAu", "ChobaniNZ"]          # a regional page the search listed first still goes after a page that is not regional


def test_the_page_id_is_read_from_the_pages_own_html_in_each_form_it_appears():
    for html in ('<script>{"pageID":"229927120383343","x":1}</script>', '{"page_id":"229927120383343"}', '<meta property="al:android:url" content="fb://page/?id=229927120383343">', '{"entity_id":"229927120383343"}'):
        assert MP.read_page_id(html) == "229927120383343"
    assert MP.read_page_id("<html>no id here</html>") == "" and MP.read_page_id(None) == ""


def test_chobani_the_main_page_is_chosen_over_the_two_regional_ones_and_its_id_comes_from_the_page_not_the_code():
    review, opened = reviewer("Chobani"), []

    def read_id(url):
        opened.append(url)
        return "1111111111" if url == "https://www.facebook.com/Chobani" else "999"
    out = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, review, read_id)
    assert out["status"] == "resolved" and out["page_id"] == "1111111111" and opened == ["https://www.facebook.com/Chobani"]
    assert "Serper" in out["reason"] and "confirmed as the main official page" in out["reason"] and out["page_name"] == "Chobani"
    assert review.seen[0][1][0] == "https://www.facebook.com/Chobani" and set(review.seen[0][1]) == {"https://www.facebook.com/Chobani", "https://www.facebook.com/ChobaniAu", "https://www.facebook.com/ChobaniNZ"}
    assert not re.search(r"\d{9,}", open(MP.__file__, encoding="utf-8").read().replace("229927120383343", ""))            # no page id is written into the module


def test_dude_wipes_a_page_the_ad_library_missed_is_found_with_serper():
    out = MP.lookup_main_page("Dude Wipes", lambda q: DUDE_RESULTS, reviewer("dudewipes", reason="the brand's own page, DUDE Wipes"), lambda url: MP.read_page_id('{"pageID":"229927120383343"}'))
    assert out["status"] == "resolved" and out["page_id"] == "229927120383343" and out["page_name"] == "DUDE Wipes" and "facebook.com/dudewipes" in out["reason"]


def test_the_rule_is_enforced_in_code_a_reviewer_that_picks_a_regional_page_when_a_main_one_exists_is_not_followed():
    out = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, reviewer("ChobaniAu", regional=True), lambda url: "5555555")
    assert out["status"] == "ambiguous" and "looks regional" in out["reason"] and "standing rule" in out["reason"] and out["page_id"] == ""
    sneaky = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, reviewer("ChobaniNZ", regional=False), lambda url: "5555555")
    assert sneaky["status"] == "ambiguous"                                                                     # said 'not regional', but its address says so


def test_a_regional_page_is_accepted_only_when_there_is_no_main_page_among_the_candidates():
    only = serp(("https://www.facebook.com/ChobaniAu", "Chobani Australia", "x"), ("https://www.facebook.com/ChobaniNZ", "Chobani New Zealand", "y"))
    out = MP.lookup_main_page("Chobani", lambda q: only, reviewer("ChobaniAu", regional=True), lambda url: "777777")
    assert out["status"] == "ambiguous"                                                                        # two regional pages and no main one: which region is the owner's call
    single = serp(("https://www.facebook.com/ChobaniAu", "Chobani Australia", "x"))
    out = MP.lookup_main_page("Chobani", lambda q: single, reviewer("ChobaniAu", regional=True), lambda url: "777777")
    assert out["status"] == "resolved" and out["page_id"] == "777777"                                          # the only page there is


def test_a_page_the_reviewer_names_but_does_not_confirm_as_the_main_one_is_not_accepted():
    def doubtful(name, candidates):
        return {"choice": candidates[0]["url"], "is_main": False, "regional": False, "reason": "it looks like a fan page"}
    read = []
    out = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, doubtful, lambda u: read.append(u) or "1234567")
    assert out["status"] == "ambiguous" and "it looks like a fan page" in out["reason"] and out["page_id"] == "" and read == []         # no page id is even read for it


def test_every_way_the_lookup_can_fail_says_why_and_never_resolves():
    unsure = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, reviewer(None, is_main=False, reason="two pages look equally official"), lambda u: "1")
    assert unsure["status"] == "ambiguous" and "two pages look equally official" in unsure["reason"] and unsure["candidates"] and unsure["candidates"][0]["url"].endswith("/Chobani")

    def broken(q):
        raise ConnectionError("the key is KEY-123")
    failed = MP.lookup_main_page("Chobani", broken, reviewer("Chobani"), lambda u: "1")
    assert failed["status"] == "unresolved" and "ConnectionError" in failed["reason"] and "KEY-123" not in failed["reason"]
    assert MP.lookup_main_page("Chobani", lambda q: serp(("https://en.wikipedia.org/wiki/Chobani", "x", "y")), reviewer("Chobani"), lambda u: "1")["status"] == "unresolved"
    noid = MP.lookup_main_page("Chobani", lambda q: CHOBANI_RESULTS, reviewer("Chobani"), lambda u: "")
    assert noid["status"] == "ambiguous" and "page id could not be read" in noid["reason"]
    assert MP.clean_review(None, [])["choice"] == "" and MP.clean_review({"choice": "https://www.facebook.com/Other", "is_main": True}, MP.candidates_from(CHOBANI_RESULTS))["choice"] == ""      # a page that was not offered is never chosen


def test_the_reviewer_is_told_the_rule_and_the_finder_is_built_from_serper_a_json_client_and_a_page_fetch():
    for phrase in ("MAIN official page", "NEVER the main page", "regional", "do not guess", "JSON only"):
        assert phrase in MP.REVIEW_SYSTEM
    prompts, searched = [], []

    class Serper:
        def search(self, query, num=10):
            searched.append(query)
            return [Result(rank=n, title=r["title"], url=r["url"], snippet=r["snippet"]) for n, r in enumerate(CHOBANI_RESULTS, 1)]

    class Claude:
        def json_task(self, system, user, max_tokens=0):
            prompts.append((system, user))
            return {"choice": "https://www.facebook.com/Chobani", "is_main": True, "regional": False, "reason": "the brand's own page"}
    finder = MP.make_finder(Serper(), Claude(), lambda url: '{"pageID":"424242424"}')
    out = finder("Chobani")
    assert out["status"] == "resolved" and out["page_id"] == "424242424" and searched and "Chobani" in searched[0]
    assert prompts[0][0] == MP.REVIEW_SYSTEM and "Brand: Chobani" in prompts[0][1] and "https://www.facebook.com/ChobaniAu" in prompts[0][1]

    def boom(url):
        raise OSError("blocked")
    assert MP.make_finder(Serper(), Claude(), boom)("Chobani")["status"] == "ambiguous"                         # a page that cannot be read leaves the decision to a person, it does not crash


def state_with(**entries):
    items = {}
    for name, (status, source, checked) in entries.items():
        items[bench.norm_name(name)] = {"name": name, "kind": "benchmark", "status": status, "page_id": "", "page_name": "", "page_likes": 0, "reason": "r", "candidates": [], "checked_at": checked, "source": source}
    return {"version": 1, "items": items}


def resolved(name):
    return {"status": "resolved", "page_id": "1234567", "page_name": name, "page_likes": 0, "candidates": [], "reason": "found with Serper"}


NOW = "2026-11-15T10:00:00Z"


def test_benchmarks_the_ad_library_could_not_settle_are_looked_for_again_and_a_persons_decision_is_never_touched():
    state = state_with(**{"Chobani": ("ambiguous", "auto", "2026-11-14T10:00:00Z"), "Dude Wipes": ("unresolved", "auto", "2026-11-14T10:00:00Z"), "Gymshark": ("ambiguous", "auto", "2026-11-14T10:00:00Z")})
    asked = []

    def finder(name):
        asked.append(name)
        return resolved(name) if name != "Gymshark" else {"status": "ambiguous", "page_id": "", "page_name": "", "page_likes": 0, "candidates": [], "reason": "two pages look equally official"}
    new, lines = MP.heal_benchmarks(state, {"Gymshark": "skip"}, finder, NOW, 5)
    assert sorted(asked) == ["Chobani", "Dude Wipes"]                                                           # Gymshark has a person's decision (skip)
    assert new["items"]["chobani"]["status"] == "resolved" and new["items"]["chobani"]["source"] == "serper_review" and new["items"]["chobani"]["page_id"] == "1234567"
    assert new["items"]["gymshark"]["status"] == "ambiguous" and any(l.startswith("Chobani: resolved - found with Serper") for l in lines)
    again = MP.heal_benchmarks(state_with(Gymshark=("ambiguous", "auto", "2026-11-14T10:00:00Z")), {}, finder, NOW, 5)[1]
    assert again == ["Gymshark: still ambiguous after a search for its Facebook page - two pages look equally official"]


def test_the_per_run_limit_a_settled_brand_and_a_recent_unconfirmed_search_are_respected():
    state = state_with(**{"A One": ("unresolved", "auto", "2026-11-01T00:00:00Z"), "B Two": ("unresolved", "auto", "2026-11-01T00:00:00Z"), "C Three": ("resolved", "auto", "2026-11-01T00:00:00Z"),
                          "D Four": ("ambiguous", "serper_review", "2026-11-15T09:00:00Z")})
    state["items"][bench.norm_name("C Three")]["page_id"] = "55555"
    asked = []
    MP.heal_benchmarks(state, {}, lambda n: asked.append(n), NOW, 1)
    assert asked == ["A One"]                                                                                     # one per run when the limit is one
    asked.clear()
    MP.heal_benchmarks(state, {}, lambda n: asked.append(n), NOW, 5)
    assert asked == ["A One", "B Two"]                                                                            # not the resolved one, not the one the reviewer could not confirm an hour ago


def test_the_collect_command_builds_the_finder_only_with_a_serper_key_and_an_ai_client(monkeypatch):
    class Claude:
        def json_task(self, *a, **k):
            return {}
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert cli._page_finder(Claude(), None) is None
    monkeypatch.setenv("SERPER_API_KEY", "k")
    assert cli._page_finder(None, None) is None and cli._page_finder(object(), None) is None and callable(cli._page_finder(Claude(), None))


# ---- an account that needs the owner is a warning, not a red run ---------------------------------------------------------------------------------------

def test_an_account_problem_ends_the_report_with_a_needs_you_block_and_never_counts_as_a_failure():
    from contentintel import collectors as C
    result = C.CollectResult(run_id="r", now="2026-11-14T10:00:00Z", dry_run=False)
    result.needs_you["instagram"] = "Instagram showed a warning: the page address contains /accounts/suspended"
    assert result.problems is False
    text = C.format_collect_report(result, C.CollectSettings() if hasattr(C, "CollectSettings") else None)
    assert text.endswith("NEEDS YOU (these are skipped, the rest of the run is fine):\n  instagram: Instagram showed a warning: the page address contains /accounts/suspended")


# ---- the documents ------------------------------------------------------------------------------------------------------------------------------------

def test_the_documents_record_the_update():
    guide = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    for phrase in ("## Update 62", "NEEDS YOU", "never clicks, likes, follows, posts or messages", "Verification An unexpected error occurred", "never a regional one", "Nothing is ever hardcoded"):
        assert phrase in guide, phrase
    rules = (ROOT / "docs" / "standing-rules.md").read_text(encoding="utf-8")
    assert "Always the main page, never a regional one" in rules and "Serper" in rules and "asks the owner only when that is still genuinely unclear" in rules
    pool = (ROOT / "docs" / "ai" / "shared-pool-and-pacing.md").read_text(encoding="utf-8")
    for phrase in ("## Update 62", "add-model call", "kilo-auto/free", "No newer pin is needed for Kilo", "switched off by a live check of ours", "after about 15:00 IST on 15 November 2026"):
        assert phrase in pool, phrase
    assert json.loads((ROOT / "docs" / "ai" / "provider_inventory.json").read_text(encoding="utf-8"))["platforms"]


def test_open_browser_gives_a_logged_in_session_a_human_and_a_public_one_none(monkeypatch, tmp_path):
    pages = []

    class Ctx:
        def add_cookies(self, cookies):
            pass

        def new_page(self):
            pages.append(Page())
            pages[-1].set_default_timeout = lambda ms: None
            return pages[-1]

    class Br:
        def new_context(self, **kw):
            return Ctx()

        def close(self):
            pass

    class PW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return Br()

    class Shared:
        def acquire(self, start):
            return PW

        def release(self):
            pass
    monkeypatch.setattr(B, "_SHARED", Shared())
    pacing = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}
    cookies = json.dumps([{"name": "sessionid", "value": "x", "domain": ".instagram.com", "path": "/"}])
    with B.open_browser("instagram", {}, pacing, cookies, tmp_path, human=None) as logged_in:
        assert isinstance(logged_in.human, H.Human)
    with B.open_browser("meta_ads", {}, pacing, "", tmp_path, human=None) as public:
        assert public.human is None
    with B.open_browser("instagram", {}, pacing, cookies, tmp_path, human={"enabled": False}) as off:
        assert off.human is None


def test_the_collect_command_hands_the_finder_to_the_run(monkeypatch, capsys, tmp_path):
    from contentintel import collectors as C
    seen = {}

    def fake(brands, store, **kw):
        seen.update(kw)
        return C.CollectResult(run_id="r", now="2026-11-15T10:00:00Z", dry_run=True)
    monkeypatch.setattr(C, "run_collect", fake)
    (tmp_path / "s.yaml").write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    base = ["collect", "--brand", "swoveralls", "--sources", "meta_ads", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(ROOT / "brands"), "--sources-config", str(tmp_path / "s.yaml")]
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    cli.main(base)
    assert seen["page_finder"] is None                                                                          # no Serper key: nothing is searched, a person decides as before
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cli.main(base)
    capsys.readouterr()
    assert callable(seen["page_finder"])


def test_a_challenge_phrase_inside_the_first_two_hundred_characters_counts_even_when_something_else_comes_first():
    page = "TikTok - Make Your Day An unexpected error occurred. Please try again. " + "Download the app About Newsroom Contact Careers " * 20
    assert len(page) > 600 and SL.judge_content(page)[0] == "challenge page"
    late = "Welcome to our shop and a long story about how we started. " * 6 + "An unexpected error occurred. Please try again. " + "words about things " * 40
    assert SL.judge_content(late)[0] == "real text"                                                              # the same words far down a long real page are not a challenge


def test_the_init_file_says_why_it_is_not_empty():
    assert "GitHub" in (ROOT / "contentintel" / "__init__.py").read_text(encoding="utf-8")


def test_the_finders_search_goes_through_the_owners_search_switches(monkeypatch):
    class Claude:
        def json_task(self, *a, **k):
            return {}
    asked = []

    class Serper:
        def search(self, *a, **k):
            return []
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: asked.append(purpose) or {})
    assert cli._page_finder(Claude(), None) is None and asked == ["discovery"]                                   # search switched off for discovery: no finder, a person decides as before
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: {"serper_dev": Serper()})
    assert callable(cli._page_finder(Claude(), None))
