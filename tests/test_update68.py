"""Update 68: the planner's review of Update 67 (M28-M66), the live questions, and the owner's account-safety items: careful mode with caps, a gap between runs, a week of rest."""
from pathlib import Path

import pytest

from contentintel import apify as A
from contentintel import cli
from contentintel import contract as K
from contentintel import main_page as MP
from contentintel import research_tools as RT
from contentintel import voice as VOICE
from contentintel.providers import ProviderConfigError, ProviderError, QuotaError, Result
from contentintel.store import Store

from test_collectors import NOW
from test_update64 import FIX, Get, Runner, SETTINGS, checkout
from test_update65 import FakeApify, _args, _ledger, _limits_http, _voice_env


@pytest.fixture(autouse=True)
def _health_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "health-data"))


class Key(A.Apify):
    """An Apify key whose reported balance can change between calls."""
    def __init__(self, left, error=None):
        self.state = {"left": left}
        super().__init__("t", http=lambda method, url, *, params=None, json_body=None, headers=None, timeout=30: (200, {"data": {"current": {"monthlyUsageUsd": 5 - self.state["left"]}, "limits": {"maxMonthlyUsageUsd": 5}}}, None))
        self.calls, self.error = 0, error

    def run(self, *a, **k):
        self.calls += 1
        if self.error:
            raise self.error
        return [{"ok": 1}]


def test_a_key_below_the_smallest_call_is_skipped_for_that_call_only_and_the_next_key_answers():
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [Key(0.10), Key(4.0)]
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.50) == [{"ok": 1}] and pool.clients[0].calls == 0 and pool.clients[1].calls == 1
    assert pool.events == ["Apify key 1 of 2 has $0.10 left, less than the $0.25 the smallest call needs; the next key is used"] and not pool.dropped          # not dropped for the run
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.50) == [{"ok": 1}] and len(pool.events) == 1                                                       # the same event is said once
    pool.clients[0].state["left"] = 3.0                                                                                                                      # Apify reports more later: the key is asked again
    pool.clients[0].forget_usage()
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.50) == [{"ok": 1}] and pool.clients[0].calls == 1
    low, only = Key(0.30), A.ApifyPool(["a"])
    only.clients = [low]
    assert only.run("x/y", {}, max_items=1, max_cost_usd=1.0) == [{"ok": 1}] and low.calls == 1                       # Collect asks with a 1 dollar ceiling: 30 cents buys a partial answer, not a refusal
    broke, last = Key(0.10), A.ApifyPool(["a"])
    last.clients = [broke]
    with pytest.raises(QuotaError) as exc:
        last.run("x/y", {}, max_items=1, max_cost_usd=0.50)
    assert "no key has enough money left" in str(exc.value) and "$0.10 left" in str(exc.value) and "no other key is left" in str(exc.value) and broke.calls == 0       # a money problem, not an authentication one


def test_a_plain_429_fails_over_to_the_next_key_and_a_config_error_or_an_outage_does_not():
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [Key(5, ProviderError("Apify: HTTP 429: too many requests")), Key(5)]
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.25) == [{"ok": 1}] and pool.dropped == {0: "RateLimited"}
    assert pool.events == ["Apify key 1 of 2 was rate-limited (HTTP 429); the next key answers instead"]
    for error in (ProviderConfigError("HTTP 429 inside a config message"), ProviderError("Apify: HTTP 500")):
        pool = A.ApifyPool(["a", "b"])
        pool.clients = [Key(5, error), Key(5)]
        with pytest.raises(ProviderError):
            pool.run("x/y", {}, max_items=1, max_cost_usd=0.25)
        assert pool.clients[1].calls == 0                                                                       # another key cures neither
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [Key(5, ProviderError("Apify: HTTP 429")), Key(5, ProviderError("Apify: HTTP 429"))]
    with pytest.raises(QuotaError, match="every key was refused"):
        pool.run("x/y", {}, max_items=1, max_cost_usd=0.25)


def test_the_best_single_key_is_what_a_call_can_spend_and_the_lane_checks_it():
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [A.Apify("a", http=_limits_http(4.7, 5)), A.Apify("b", http=_limits_http(4.7, 5))]
    assert abs(A.best_key_left(pool) - 0.3) < 1e-9 and abs(A.budget_report(pool)[1] - 0.6) < 1e-9               # two keys with 30 cents each: 60 cents in all, but a call runs on one key
    assert A.best_key_left(type("C", (), {"usages": lambda self: [None]})()) is None
    fake = FakeApify()
    lane = VOICE.ApifyVoice(fake, _ledger(), normalize=lambda s, raw: (raw, 0), budget_fn=lambda: A.best_key_left(pool))
    _, notes = lane.run("sweatpant overalls")
    assert [c[0] for c in fake.calls] == ["apify/instagram-hashtag-scraper"] and "on the best key" in notes[0]       # TikTok needs 0.50: 30 cents on the best key is not enough; Instagram needs 0.25


def test_the_limits_reading_is_reused_for_twenty_seconds_and_forgotten_after_a_call_and_nested_fields_are_named():
    n = {"calls": 0}

    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        n["calls"] += 1
        return 200, {"data": {"current": {"monthlyUsageUsd": 1}, "limits": {"maxMonthlyUsageUsd": 5}}}, None
    key = A.Apify("t", http=http)
    key.usage(), key.usage(), key.usage()
    assert n["calls"] == 1                                                                                   # one GET, not three
    key.forget_usage()
    key.usage()
    assert n["calls"] == 2
    odd = A.Apify("t", http=_limits_http(0, 5, body={"data": {"current": {"somethingElse": 1}, "limits": {}}}))
    assert odd.usage() is None and "current.somethingElse" in odd.usage_error and "limits" in odd.usage_error


def test_a_timed_out_actor_run_is_asked_once_more_with_half_and_other_errors_are_not_retried():
    calls = []

    class Timeout:
        def run(self, actor, payload, *, max_items, max_cost_usd):
            calls.append((actor, max_items))
            if len(calls) == 1:
                raise RuntimeError('Apify: HTTP 400: "type": "run-failed", "message": "Actor run did not succeed (run ID: K4WGG, status: TIMED-OUT)."')
            return [{"text": "a caption about overalls", "likes": 5, "url": "https://x/1"}] * max_items
    lane = VOICE.ApifyVoice(Timeout(), _ledger(), per_run=20, normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("sweatpant overalls")
    assert calls[0] == ("clockworks/tiktok-scraper", 10) and calls[1] == ("clockworks/tiktok-scraper", 5) and any("timed out on Apify's side; asking once more for 5" in n for n in notes)
    assert lane.spent_this_run == 5 + 15 and len(items) == 20                                                  # the failed run read nothing; the retry read 5; Instagram was given the rest of the ask (15)
    calls.clear()

    class Broken:
        def run(self, *a, **k):
            calls.append(1)
            raise RuntimeError("Apify: HTTP 500")
    _, notes = VOICE.ApifyVoice(Broken(), _ledger(), normalize=lambda s, raw: (raw, 0)).run("x")
    assert len(calls) == 2 and not any("asking once more" in n for n in notes)                                 # one call per source, no retry


def test_the_engines_account_survives_a_hard_failure_and_a_wider_wording_and_a_402_is_said_plainly(tmp_path, capsys, monkeypatch):
    stderr = "[Planner] fine\nTikTok returned no results\nInstagram: no results found\nMeta Ads skipped: no credits\nmissing scrapecreators key\nTikTok failed: 402 Payment Required\n"
    ext = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path), environ={"SCRAPECREATORS_API_KEY": "k"}, runner=Runner("", 1, stderr))
    with pytest.raises(K.Failure):
        ext.run("x")
    for phrase in ("TikTok returned no results", "Instagram: no results found", "Meta Ads skipped: no credits", "missing scrapecreators key", "TikTok failed: 402 Payment Required"):
        assert phrase in ext.engine_summary, phrase
    assert "fine" not in ext.engine_summary and ext.sc_out_of_credits is True                                   # kept although the engine exited with an error
    ok = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path / "y"), environ={}, runner=Runner((FIX / "l30_research_mock_real.json").read_text(), 0, "all fine\n"))
    ok.run("x")
    assert ok.sc_out_of_credits is False and ok.engine_summary == ""
    store = _voice_env(tmp_path, monkeypatch)
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, NOW, get=Get({}), runner=Runner((FIX / "l30_research_mock_real.json").read_text(), 0, "[YouTube] SC YouTube search error: HTTP 402: Payment Required\n"),
                 environ={"SCRAPECREATORS_API_KEY": "k"}, clock=lambda: 1)
    assert "ScrapeCreators answered 'payment required' (HTTP 402): its credits are spent" in capsys.readouterr().out


class _Serper:
    def search(self, query, num=10):
        return [Result(rank=1, title="Hu Kitchen | Facebook", url="https://www.facebook.com/HuKitchen", snippet="Hu")]


class _Claude:
    def json_task(self, system, user, max_tokens=0):
        return {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "main"}


def test_the_browser_identity_gets_its_turn_after_a_failed_honest_fetch_and_a_failing_first_reviewer_is_a_reason():
    def raising(url):
        raise TimeoutError("slow")
    raising.alt = lambda url: '{"pageID":"100034777406193"}'
    out = MP.make_finder(_Serper(), _Claude(), raising)("Hu Kitchen")
    assert out["status"] == "resolved" and out["page_id"] == "100034777406193"                                  # the honest fetch raised: the browser identity still ran

    def plain(url):
        return "x"
    plain.alt = lambda url: "yy"
    reason = MP.make_finder(_Serper(), _Claude(), plain)("Hu Kitchen")["reason"]
    assert reason.index("www.facebook.com: no page id in the page (1 characters)") < reason.index("www.facebook.com as a browser: no page id in the page (2 characters)")      # the order they were tried

    class Broken:
        def json_task(self, system, user, max_tokens=0):
            raise RuntimeError("the reviewer is down")
    bad = MP.make_finder(_Serper(), Broken(), lambda url: "")("Hu Kitchen")
    assert bad["status"] == "ambiguous" and "the reviewer could not be asked (RuntimeError)" in bad["reason"]


def test_the_pageid_check_ignores_a_long_phrase_as_a_name_lists_candidates_on_request_and_keeps_its_closing_line_honest(capsys):
    get = lambda url, headers=None: (200, '{"pageID":"123456789"}')           # noqa: E731
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani", "--name", "Chobani greek yogurt cups and more"]), get=get, search=lambda q: [])
    out = capsys.readouterr().out
    assert "does not look like a name" in out and "No brand name was given" in out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani", "--name", "Chobani Yogurt"]), get=get, search=lambda q: [])
    assert "Search for the numeric id" not in capsys.readouterr().out                                           # an id was read: no search unless asked
    peoples = lambda q: [{"url": "https://www.facebook.com/people/Chobani/100064549155148/", "title": "Chobani", "snippet": ""}] if "people" in q else []          # noqa: E731
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani", "--name", "Chobani", "--cross-check"]), get=get, search=peoples)
    assert "id 100064549155148 from https://www.facebook.com/people/Chobani/100064549155148/" in capsys.readouterr().out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/HuKitchen"]), get=lambda u, headers=None: (200, "log in"), search=lambda q: [])
    assert "the main page is still confirmed by name" not in capsys.readouterr().out


def test_an_override_outside_the_lanes_bounds_is_said_and_the_end_line_says_when_no_call_was_made(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    import contentintel.apify as AP
    fake = FakeApify()
    monkeypatch.setattr(AP, "Apify", lambda token: fake)
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, '{"hits": []}')}), runner=Runner("", 1, "boom"),
                 environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_PER_RUN": "5000", "CONTENTINTEL_VOICE_APIFY_MONTHLY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "Note: CONTENTINTEL_VOICE_APIFY_PER_RUN=5000 is outside what the lane allows (1 to 300); 300 is used." in out and "Note: CONTENTINTEL_VOICE_APIFY_MONTHLY=0 is outside what the lane allows (1 to 2000); 1 is used." in out
    assert VOICE.ApifyVoice(FakeApify(), _ledger(), shared_cap=10**9).shared_cap == 10000                      # the Collect cap is clamped too


def test_the_extra_apify_keys_are_listed_for_collect_and_acceptance_and_collect_prints_the_budget(tmp_path):
    assert all(f'"{n}"' in Path("contentintel/cli.py").read_text(encoding="utf-8") for n in ("APIFY_TOKEN_2", "APIFY_TOKEN_3", "APIFY_TOKEN_4"))
    body = Path(".github/workflows/acceptance.yml").read_text(encoding="utf-8")
    assert all(f"APIFY_TOKEN_{n}: ${{{{ secrets.APIFY_TOKEN_{n} }}}}" in body for n in (2, 3, 4))
    lines = cli.apify_budget_lines(A.Apify("a", http=_limits_http(1.0, 5)))
    assert lines[0].startswith("Apify budget this cycle") and "$4.00 left of $5.00 across 1 key(s)" in lines[0] and cli.apify_budget_lines(None) == []


# ---- the owner's account-safety items: caps, a gap between runs, a week of rest ----------------------------------------------------------------------------

def _gap_world(tmp_path, last, gap=12, now="2026-10-20T07:00:00Z"):
    """One careful Instagram site with a page log that says the account was last read at `last`."""
    import copy
    from careful import careful_text
    from contentintel import browser as B
    from contentintel import collectors as C
    from contentintel.brands import load_brand
    from test_careful_mode import ig_world, go, statuses
    text = careful_text(Path("browser.yaml").read_text(encoding="utf-8"), gap=bool(gap))
    path = tmp_path / "browser.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = B.load_browser_settings(str(path))
    if gap:
        cfg = copy.deepcopy(cfg)
        cfg["sites"]["instagram"]["min_hours_between_runs"] = gap
    store = Store(tmp_path / "data")
    if last:
        store.write_page_log({"days": {}, "cursor": {}, "last": {"instagram": last}})
    return cfg, store, C, go, ig_world, statuses, load_brand("swoveralls", "brands")


def test_an_account_read_less_than_the_gap_ago_is_left_alone_and_one_read_after_the_gap_goes_ahead_and_is_recorded(tmp_path):
    cfg, store, C, go, ig_world, statuses, swov = _gap_world(tmp_path, "2026-10-20T03:00:00Z")
    result = go(store, tmp_path, swov, cfg, C.load_collector_settings("research.yaml"), ig_world(cfg))
    detail = result.brands["swoveralls"][0].detail
    assert statuses(result) == [("instagram", "skipped")]                                                      # skipped, not failed
    assert detail == "careful mode: instagram was last read 4.0 hours ago; it is read at most once every 12 hours (min_hours_between_runs in browser.yaml), so this run leaves it for about 8.0 more hours"
    assert store.read_page_log()["last"]["instagram"] == "2026-10-20T03:00:00Z"                                  # nothing was opened, so the time is unchanged


def test_after_the_gap_the_account_is_read_and_the_time_is_recorded_and_no_gap_means_no_wait(tmp_path):
    cfg, store, C, go, ig_world, statuses, swov = _gap_world(tmp_path, "2026-10-19T18:00:00Z")                  # 13 hours ago
    f = ig_world(cfg)
    result = go(store, tmp_path, swov, cfg, C.load_collector_settings("research.yaml"), f)
    assert statuses(result) == [("instagram", "ran")] and store.read_page_log()["last"]["instagram"] == "2026-10-20T07:00:00Z"
    (tmp_path / "b").mkdir()
    cfg2, store2, C2, go2, ig_world2, statuses2, swov2 = _gap_world(tmp_path / "b", "2026-10-20T06:59:00Z", gap=0)
    result2 = go2(store2, tmp_path / "b", swov2, cfg2, C2.load_collector_settings("research.yaml"), ig_world2(cfg2))
    assert statuses2(result2) == [("instagram", "ran")]                                                         # without min_hours_between_runs nothing waits
    assert cfg["sites"]["instagram"].get("min_hours_between_runs") in (None, 12)


def test_the_shipped_sites_carry_the_gap_and_the_page_log_keeps_the_time_the_account_was_last_read(tmp_path):
    import yaml
    sites = yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["sites"]
    assert sites["instagram"]["min_hours_between_runs"] == 12 and sites["tiktok"]["min_hours_between_runs"] == 12 and "min_hours_between_runs" not in sites["meta_ads"]
    store = Store(tmp_path / "d")
    store.write_page_log({"days": {"2026-10-20": {"instagram": 2}}, "cursor": {"instagram": 2}, "last": {"instagram": "2026-10-20T07:00:00Z"}})
    assert store.read_page_log()["last"] == {"instagram": "2026-10-20T07:00:00Z"}
    assert Store(tmp_path / "empty").read_page_log()["last"] == {}
    (tmp_path / "d" / "_shared" / "page_log.json").write_text('{"last": "damaged"}', encoding="utf-8")
    assert store.read_page_log()["last"] == {}                                                                   # a damaged log means "never read", never a crash


def test_the_browser_profile_directory_is_outside_data_and_never_committed():
    import yaml
    from contentintel import stealth as ST
    assert ST.DEFAULTS["profiles_root"] == ".stealth-profiles" and yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["stealth"]["profiles_root"] == ".stealth-profiles"
    assert ".stealth-profiles/" in Path(".gitignore").read_text(encoding="utf-8").split("\n")
    for name in ("collect.yml", "request.yml"):
        body = Path(".github/workflows") / name
        assert "git reset -q -- data/_shared/stealth-profiles" in body.read_text(encoding="utf-8"), name


def test_the_sabotage_runs_are_split_into_slices_that_cover_everything_once():
    import sys
    sys.path.insert(0, "tests/sabotage")
    import run_observatory_sabotage as O
    import run_sabotage as S
    items = list(range(1000))
    for select in (O.select_chunk, S.select_chunk):
        parts = [select(items, f"{i}/7") for i in range(1, 8)]
        assert sorted(x for p in parts for x in p) == items and all(not set(a) & set(b) for k, a in enumerate(parts) for b in parts[k + 1:])
        assert select(items, "") == items
        with pytest.raises(SystemExit):
            select(items, "9/7")
    assert O.UPDATED_IDS and O.UPDATED_IDS <= {m[0] for m in O.MUTATIONS} and len(O.UPDATED_IDS) < len(O.MUTATIONS)
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/tests.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert {"sabotage-updated", "sabotage-full-observatory", "sabotage-full-main"} <= set(jobs) and len(jobs["sabotage-updated"]["strategy"]["matrix"]["chunk"]) == 3
    assert len(jobs["sabotage-full-observatory"]["strategy"]["matrix"]["chunk"]) == 6 and len(jobs["sabotage-full-main"]["strategy"]["matrix"]["chunk"]) == 12
    assert all(j["timeout-minutes"] <= 345 for k, j in jobs.items() if k.startswith("sabotage"))                 # none comes near GitHub's six-hour limit


def test_a_regional_page_is_ranked_behind_a_plain_one_even_when_the_search_listed_it_first():
    cands = [{"slug": "chobani-nz", "title": "Chobani", "url": "https://www.facebook.com/chobani-nz", "id": ""}, {"slug": "ChobaniOfficial", "title": "Chobani", "url": "https://www.facebook.com/ChobaniOfficial", "id": ""}]          # the same title: only the address tells them apart
    ranked = MP.rank("Chobani", cands)
    assert [c["slug"] for c in ranked] == ["ChobaniOfficial", "chobani-nz"]
    exact = MP.rank("Chobani", [dict(cands[1]), {"slug": "Chobani", "title": "Chobani", "url": "https://www.facebook.com/Chobani", "id": ""}])
    assert exact[0]["slug"] == "Chobani"                                                                            # the page whose address is the brand's own name comes first


def test_the_collect_command_hands_fresh_to_the_run_only_when_asked(tmp_path, monkeypatch):
    from contentintel import collectors as C
    from test_careful_mode import ENV, BROWSER_YAML, BRANDS_DIR, FULL
    import shutil
    import yaml
    seen = []
    real = C.run_collect

    def spy(*a, **k):
        seen.append(k.get("fresh"))
        return real(*a, **k)
    monkeypatch.setattr(C, "run_collect", spy)
    for name in list(ENV) + ["INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TIKTOK_USERNAME", "TIKTOK_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    brands = tmp_path / "brands"
    shutil.copytree(BRANDS_DIR, brands)
    (brands / "swoveralls" / "research.yaml").write_text(yaml.safe_dump(FULL), encoding="utf-8")
    base = ["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands), "--research-config", "research.yaml", "--browser-config", BROWSER_YAML, "--debug-dir", str(tmp_path / "debug"),
            "--brand", "swoveralls", "--sources", "meta_ads", "--dry-run"]
    cli.main(base)
    cli.main(base + ["--fresh"])
    assert seen == [False, True]

