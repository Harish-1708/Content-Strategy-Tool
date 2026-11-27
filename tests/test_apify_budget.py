"""The Apify money plan: every cap comes from the 20 dollar month split per feature, with a daily share so no feature uses its month in two or three days."""
import math
from pathlib import Path

from contentintel import apify_budget as AB
from contentintel import collectors as C
from contentintel import research as R
from contentintel import research_tools as RT
from contentintel import session_live as SL
from contentintel import contract as K
from contentintel import voice as V

import pytest


def test_the_plan_adds_up_to_the_twenty_dollars_and_every_item_cap_is_dollars_over_the_most_an_item_costs():
    p = AB.plan(4)
    assert round(sum(v["usd"] for v in p.values()), 2) == 20.0 and abs(sum(AB.SHARES.values()) - 1.0) < 1e-9
    for feature, cost in AB.COST_PER_ITEM.items():
        assert p[feature]["items"] == int(p[feature]["usd"] / cost) and p[feature]["items"] * cost <= p[feature]["usd"] + 1e-9
    assert (p["collect"]["items"], p["voice"]["items"], p["sessions_read"]["items"], p["reddit_backup"]["items"], p["analyze_comments"]["items"]) == (1200, 1000, 400, 411, 1000)
    assert p["reserve"] == {"usd": 3.6, "items": 0, "per_day": 0}
    spend = sum(p[f]["items"] * c for f, c in AB.COST_PER_ITEM.items())
    assert spend + p["reserve"]["usd"] <= 20.0 + 1e-6                                                                             # even if every item cost its upper estimate, the month is not exceeded
    assert AB.plan(1)["voice"]["items"] == 250 and AB.plan(2)["collect"]["items"] == 600 and AB.plan(0)["voice"]["items"] == 250         # fewer keys, a smaller month: one key is the floor


def test_a_feature_may_use_at_most_four_days_worth_of_its_month_in_one_day_and_only_the_last_three_days_are_kept():
    p = AB.plan(4)["voice"]
    assert p["per_day"] == math.ceil(1000 / 30 * AB.DAY_FACTOR) == 134 and p["per_day"] < p["items"] / 7                              # the month cannot go in two or three days
    led = R.Ledger({}, "2026-11")
    assert AB.room("voice", led, "2026-11-19") == 134
    AB.charge("voice", led, "2026-11-19", 60)
    assert AB.room("voice", led, "2026-11-19") == 74 and AB.room("voice", led, "2026-11-20") == 134 and led.month_used("apify_voice_items") == 60 and led.month_used("apify_items") == 60
    for d in ("20", "21", "22"):
        AB.charge("voice", led, f"2026-11-{d}", 1)
    assert sorted(led.usage["days"]) == ["2026-11-20", "2026-11-21", "2026-11-22"]
    led2 = R.Ledger({}, "2026-11")
    for day in range(1, 8):
        AB.charge("voice", led2, f"2026-11-{day:02d}", 134)
    assert AB.room("voice", led2, "2026-11-08") == 1000 - 7 * 134 == 62                                                             # the month's own cap still stops it
    assert "collect: 6.00 dollars = 1200 items a month at most 0.005 each, 160 a day at most" in AB.report(4)[1] and "reserve: 3.60 dollars kept in reserve" in AB.report(4)[-1]


def test_the_collect_default_and_the_shipped_research_file_and_the_voice_default_come_from_the_plan():
    assert C.DEFAULT_COLLECTORS["apify"]["monthly_items"] == AB.items("collect") == 1200
    import yaml
    assert yaml.safe_load(Path("research.yaml").read_text(encoding="utf-8"))["collectors"]["apify"]["monthly_items"] == AB.items("collect")
    assert "monthly=_env_number(environ, \"CONTENTINTEL_VOICE_APIFY_MONTHLY\", apify_budget_mod.items(\"voice\", keys)" in Path("contentintel/research_tools.py").read_text(encoding="utf-8")


class Apify:
    def __init__(self):
        self.calls = []

    def run(self, actor, payload, *, max_items, max_cost_usd, **kw):
        self.calls.append((actor, payload, max_items, max_cost_usd))
        if actor == "trudax/reddit-scraper-lite":
            return [{"title": "Overalls review", "body": "I wore these for a month and the cotton is great", "url": "https://reddit.com/r/x/1", "upVotes": 40, "numberOfComments": 3, "createdAt": "2026-11-18"}] * max_items
        return []


def lane(apify, **kw):
    return V.ApifyVoice(apify, R.Ledger({}, "2026-11"), per_run=60, monthly=500, shared_cap=600, normalize=lambda source, raw: ([], len(raw)), day="2026-11-19", **kw)


def test_reddit_through_apify_can_be_put_back_on_hold_and_then_no_call_is_made(monkeypatch):
    from contentintel import apify as AP
    monkeypatch.setattr(V, "reddit_held", lambda *a, **k: True)                                            # the owner's switch (research.yaml apify.reddit_hold: true) puts Reddit back on hold
    assert AP.reddit_held("research.yaml") is False and AP.reddit_held("no-such-file.yaml") is True       # ON in the file since 2026-11-27 (owner decision); the safe default when the file cannot be read stays on hold;       # the owner's hold (the residential proxy costs about 8 dollars a GB): on in the file, and on when the file cannot be read
    a = Apify()
    items, notes = lane(a).run("overalls", reddit_backup=True)
    assert not [c for c in a.calls if c[0] == "trudax/reddit-scraper-lite"] and any("apify reddit for 'overalls': not asked (Reddit through Apify is on hold" in n for n in notes)


def test_the_reddit_actor_is_asked_only_as_a_backup_when_the_public_reddit_search_failed_and_from_its_own_share(monkeypatch):
    monkeypatch.setattr(V, "reddit_held", lambda *a, **k: False)                                           # the hold lifted, as it will be when Reddit comes back
    a = Apify()
    quiet, _notes = lane(a).run("overalls")
    assert not [c for c in a.calls if c[0] == "trudax/reddit-scraper-lite"]                                                        # not asked when the public search worked
    b = Apify()
    lb = lane(b)
    items, notes = lb.run("overalls", reddit_backup=True)
    reddit = [c for c in b.calls if c[0] == "trudax/reddit-scraper-lite"]
    assert len(reddit) == 1 and reddit[0][1] == {"searches": ["overalls"], "searchPosts": True, "searchComments": False, "maxItems": 25, "sort": "top"} and reddit[0][3] == 0.25
    assert [i for i in items if i["source"] == "apify reddit"] and any("apify reddit for 'overalls': asked for 25, 25 item(s) read" in n for n in notes)
    quality = [n for n in notes if n.startswith("apify reddit quality for 'overalls': ")]
    assert len(quality) == 1 and quality[0].split(": ", 1)[1].startswith("25 post(s): ") and " with a score, " in quality[0] and " with a comment count, " in quality[0] and " with text, " in quality[0] and " with a community" in quality[0]          # every Reddit read says how good it was
    assert lb.ledger.month_used("apify_reddit_backup_items") == 25 and lb.ledger.month_used("voice_apify_items") == 0 and lb.committed_usd == pytest.approx(0.25 + 0.5 + 0.25)           # its own counter, its own dollars


def test_the_voice_lane_keeps_to_its_daily_share_and_the_sessions_page_read_to_its_own():
    a = Apify()
    lv = lane(a)
    lv.ledger.charge("voice_apify_items", 60)
    AB.charge_day(lv.ledger, "voice_apify_items", "2026-11-19", 60)
    assert lv.day_room() == 74 and lv.room() == 60                                                                                         # the day leaves 74, the run allows 60
    assert V.ApifyVoice(a, R.Ledger({}, "2026-11"), per_run=60, monthly=500, shared_cap=600).day_room() == 10 ** 9                  # no day given (older callers): no daily limit
    usage = R.Ledger({}, "2026-11")

    class Page:
        def run(self, actor, payload, *, max_items, max_cost_usd, **kw):
            self.seen = (actor, payload, max_items, max_cost_usd)
            return [{"caption": "Soft overalls for grown-ups, bio of the account", "url": "https://www.instagram.com/swoveralls_/"}]
    page = Page()
    rows = SL.apify_rows("instagram", "https://www.instagram.com/swoveralls_/", page, usage, "2026-11-19T10:00:00Z")
    assert rows[0]["route"] == "apify" and rows[0]["actor"] == "apify/instagram-scraper" and page.seen == ("apify/instagram-scraper", {"directUrls": ["https://www.instagram.com/swoveralls_/"], "resultsType": "posts", "resultsLimit": 1}, 1, 0.25)
    assert usage.month_used("apify_sessions_read_items") == 1 and usage.month_used("apify_items") == 1
    full = R.Ledger({}, "2026-11")
    AB.charge("sessions_read", full, "2026-11-19", 54)
    with pytest.raises(K.Failure) as err:
        SL.apify_rows("instagram", "https://www.instagram.com/x/", page, full, "2026-11-19T10:00:00Z")
    assert err.value.code == K.RATE_LIMITED and "share of this month's Apify money" in err.value.message
    assert SL.apify_rows("facebook", "https://www.facebook.com/x", page, None, "2026-11-19T10:00:00Z")[0]["actor"] == "apify/website-content-crawler"          # other sites: the generic page reader


def test_the_sessions_read_chain_has_apify_as_an_additional_step_between_the_session_and_discovery():
    import inspect
    src = inspect.getsource(SL.read_page)
    assert src.index('SC.Step("session"') < src.index('SC.Step("apify"') < src.index('SC.Step("discovery"') and 'SC.Step("primary"' in src and "ADDITIONAL" in src


def test_comment_text_reading_is_charged_to_its_own_share_and_refused_when_the_share_is_used_up(tmp_path, capsys):
    import json
    import subprocess
    from contentintel import cli
    from contentintel.store import Store
    info = {"webpage_url": "https://www.tiktok.com/@a/video/1", "extractor_key": "TikTok", "title": "t", "duration": 19, "view_count": 5, "comment_count": 8}

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(info), "")

    class Client:
        def run(self, actor, payload, *, max_items, max_cost_usd, **kw):
            return [{"text": "nice one", "diggCount": 2, "uniqueId": "u"}]
    store = Store(tmp_path / "data")
    args = cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/1", "--comments", "--no-speech", "--data-dir", str(tmp_path / "data")])
    RT.run_analyze(args, "2026-11-19T12:00:00Z", runner=runner, store=store, environ={}, apify=Client())
    usage = store.load_usage()
    assert usage["months"]["2026-11"]["apify_analyze_comments_items"] == 1 and usage["days"]["2026-11-19"]["apify_analyze_comments_items"] == 1 and "Comments route: Apify" in capsys.readouterr().out
    usage["months"]["2026-11"]["apify_analyze_comments_items"] = 990
    store.save_usage(usage)
    RT.run_analyze(args, "2026-11-19T13:00:00Z", runner=runner, store=store, environ={}, apify=Client())
    assert "share of the Apify money for comments is used up" in capsys.readouterr().out


def test_the_revised_plan_still_adds_up_to_twenty_dollars_with_x_and_linkedin_inside_it_and_the_new_costs_are_the_ones_read_from_apify():
    p = AB.plan(4)
    assert round(sum(v["usd"] for v in p.values()), 2) == 20.0 and abs(sum(AB.SHARES.values()) - 1.0) < 1e-9
    assert (p["collect"]["usd"], p["voice"]["usd"], p["sessions_read"]["usd"], p["reddit_backup"]["usd"], p["analyze_comments"]["usd"], p["x_read"]["usd"], p["linkedin_read"]["usd"], p["reserve"]["usd"]) == (6.0, 5.0, 2.0, 1.4, 1.0, 0.5, 0.5, 3.6)
    assert (p["sessions_read"]["items"], p["sessions_read"]["per_day"], p["reddit_backup"]["items"], p["reddit_backup"]["per_day"]) == (400, 54, 411, 55)
    assert (p["x_read"]["items"], p["x_read"]["per_day"], p["linkedin_read"]["items"], p["linkedin_read"]["per_day"]) == (250, 34, 250, 34)
    assert AB.COST_PER_ITEM["reddit_backup"] == 0.0034 and AB.COST_PER_ITEM["x_read"] == 0.002 and AB.COST_PER_ITEM["linkedin_read"] == 0.002 and AB.COST_PER_ITEM["collect"] == 0.005
    for feature in ("x_read", "linkedin_read", "reddit_backup", "sessions_read"):
        assert p[feature]["per_day"] < p[feature]["items"] / 7                                                                  # no feature can use its month in a week
    assert abs(sum(v["usd"] for v in AB.plan(3).values()) - 15.0) < 0.05                                                          # fewer keys: the same split of a smaller month
    doc = Path("docs/platform/apify-budget.md").read_text(encoding="utf-8")
    assert "| x read | 2% | 0.50 |" in doc or "x read" in doc and "linkedin read" in doc and "khadinakbar/x-tweet-scraper" in doc and "harvestapi/linkedin-company-posts" in doc


def test_x_and_linkedin_and_reddit_page_reads_go_through_their_own_actors_and_are_counted_against_their_own_shares(monkeypatch):
    monkeypatch.setattr(SL, "reddit_held", lambda *a, **k: False)                                         # the hold lifted, as it will be when Reddit comes back
    calls = []

    class Fake:
        def run(self, actor, payload, max_items, max_cost_usd):
            calls.append((actor, payload, max_items, max_cost_usd))
            return [{"text": "a tweet about overalls", "url": "https://x.com/NASA/status/1"}]
    usage = R.Ledger({}, "2026-11")
    rows = SL.apify_rows("x", "https://x.com/NASA", Fake(), usage, "2026-11-24T10:00:00Z")
    assert calls[-1][0] == "khadinakbar/x-tweet-scraper" and calls[-1][1] == {"handles": ["NASA"], "maxTweetsPerQuery": 1} and calls[-1][3] == 0.25 and rows[0]["actor"] == "khadinakbar/x-tweet-scraper"
    assert usage.month_used(AB.counter("x_read")) == 1 and usage.month_used(AB.counter("sessions_read")) == 0
    SL.apify_rows("linkedin", "https://www.linkedin.com/company/nasa/", type("L", (Fake,), {"run": lambda s, a, p, max_items, max_cost_usd: calls.append((a, p, max_items, max_cost_usd)) or [{"content": "a company post", "url": "u"}]})(), usage, "2026-11-24T10:00:00Z")
    assert calls[-1][0] == "harvestapi/linkedin-company-posts" and calls[-1][1] == {"targetUrls": ["https://www.linkedin.com/company/nasa/"], "maxPosts": 1} and usage.month_used(AB.counter("linkedin_read")) == 1
    SL.apify_rows("reddit", "https://www.reddit.com/r/overalls/", Fake(), usage, "2026-11-24T10:00:00Z")
    assert calls[-1][0] == "trudax/reddit-scraper-lite" and calls[-1][1]["startUrls"] == [{"url": "https://www.reddit.com/r/overalls/"}] and usage.month_used(AB.counter("reddit_backup")) == 1
    assert SL._x_handle("https://twitter.com/NASA/status/5") == "NASA" and SL._x_handle("https://x.com/home") == "" and SL._x_handle("https://example.com/NASA") == ""
    with pytest.raises(K.Failure, match="not an X profile"):
        SL.apify_rows("x", "https://x.com/search?q=a", Fake(), usage, "2026-11-24T10:00:00Z")
    with pytest.raises(K.Failure, match="not a LinkedIn company page"):
        SL.apify_rows("linkedin", "https://www.linkedin.com/in/someone/", Fake(), usage, "2026-11-24T10:00:00Z")
    full = R.Ledger({}, "2026-11")
    AB.charge("x_read", full, "2026-11-24", 34)
    with pytest.raises(K.Failure) as err:
        SL.apify_rows("x", "https://x.com/NASA", Fake(), full, "2026-11-24T10:00:00Z")
    assert err.value.code == K.RATE_LIMITED and "x-read share" in err.value.message                                                  # its own daily share is a hard stop
    ok = R.Ledger({}, "2026-11")
    AB.charge("sessions_read", ok, "2026-11-24", 54)
    assert SL.apify_rows("x", "https://x.com/NASA", Fake(), ok, "2026-11-24T10:00:00Z")                                              # the sessions-read share being used up does not stop X
