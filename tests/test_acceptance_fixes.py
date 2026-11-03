"""Fixes from the first live Acceptance run: Apify's 201 answer, the cost-ceiling refusal, no password login over cookies,
evidence from a login page that has changed, and YouTube through its official API."""
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from fake_browser import FakeBrowser                                      # noqa: E402
from fake_http import FakeHttp                                            # noqa: E402
from test_apify_backend import AP_AD, AP_IG, AP_TT, ADS_URL, IG_URL, SOURCES, TT_URL, run             # noqa: E402
from conftest import BRANDS_DIR                                           # noqa: E402
from test_collectors import BROWSER_YAML, CREDS, NOW, LoginFake, site, world            # noqa: E402

from contentintel import acceptance as A                                  # noqa: E402
from contentintel import browser as B                                     # noqa: E402
from contentintel import cli                                              # noqa: E402
from contentintel import collectors as C                                  # noqa: E402
from contentintel import contract as K                                    # noqa: E402
from contentintel import providers as P                                   # noqa: E402
from contentintel import research as R                                    # noqa: E402
from contentintel import sources as S                                     # noqa: E402
from contentintel.apify import Apify                                      # noqa: E402
from contentintel.store import Store                                      # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MINIMUM = {"error": {"type": "max-total-charge-usd-below-minimum", "message": "Maximum cost per run is less than the allowed minimum of $0.50"}}
# What the first live run really received (the start of each answer, as printed in its log).
REAL_IG_ITEM = {"inputUrl": "https://www.instagram.com/explore/tags/overalls", "id": "4022071767620158918", "type": "Sidecar", "shortCode": "DfRRPZ6IIXG",
                "caption": "overalls in a bag", "ownerUsername": "someone", "likesCount": 12, "commentsCount": 1,
                "timestamp": "2026-10-01T10:00:00.000Z", "displayUrl": "https://img.example/i.jpg"}


def one_call_http(status, body):
    calls = []

    def http(method, url, **kw):
        calls.append((method, url, kw))
        return status, body, None
    http.calls = calls
    return http


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def run_cli(tmp_path, monkeypatch):
    """The real command line, working in a temporary copy of the project's settings and brands."""
    monkeypatch.chdir(tmp_path)
    for name in ("browser.yaml", "sources.yaml", "research.yaml"):
        shutil.copy(BROWSER_YAML.replace("browser.yaml", name), tmp_path / name)
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")

    def run(*args):
        return cli.main([args[0], "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands"), *args[1:]])
    return run


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


# --- 1. Apify answers 201 Created when a synchronous run succeeds ---------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201])
def test_apify_items_are_accepted_with_either_success_status(status):
    http = one_call_http(status, [AP_AD, AP_AD, AP_AD])
    items = Apify("tok", http=http, sleep=lambda s: None).run("apify/facebook-ads-scraper", {}, max_items=2, max_cost_usd=0.25)
    assert len(items) == 2 and len(http.calls) == 1                         # accepted at once, never repeated, never more than the cap


@pytest.mark.parametrize("status", [202, 204, 301, 404, 422])
def test_other_statuses_are_still_failures_for_apify(status):
    with pytest.raises(P.ProviderError, match=f"HTTP {status}"):
        Apify("tok", http=one_call_http(status, {"error": {"message": "x"}}), sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=1)


def test_a_201_that_is_not_a_list_of_items_is_still_an_error():
    with pytest.raises(P.ProviderError, match="expected a list of items, got dict"):
        Apify("tok", http=one_call_http(201, {"id": "run1"}), sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=1)


def test_the_other_providers_still_accept_only_200():
    class Other(P.Provider):
        label = "Other"
    with pytest.raises(P.ProviderError, match="HTTP 201"):
        Other("k", http=one_call_http(201, {"x": 1}), sleep=lambda s: None)._call("GET", "https://x.example")


def test_the_real_instagram_answer_of_the_first_run_turns_into_usable_rows():
    http = one_call_http(201, [REAL_IG_ITEM])
    items = Apify("tok", http=http, sleep=lambda s: None).run("apify/instagram-hashtag-scraper", {}, max_items=5, max_cost_usd=0.25)
    rows, bad = C.normalize_items("instagram", items)
    assert bad == 0 and len(rows) == 1 and rows[0]["item_id"] and "overalls in a bag" in json.dumps(rows[0])


def test_a_whole_collection_through_apify_works_with_201_answers(tmp_path, cfg, settings, store, swov):
    http = FakeHttp()
    http.on("POST", ADS_URL, (201, [AP_AD])).on("POST", TT_URL, (201, [AP_TT])).on("POST", IG_URL, (201, [AP_IG]))
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=Apify("apify-secret-123", http=http, sleep=lambda s: None),
                 backend="apify", sources=SOURCES)
    assert len(http.calls) == 3 and not result.problems
    assert [r.status for r in result.brands["swoveralls"]] == ["ran", "ran", "ran"] and all("via Apify" in r.detail for r in result.brands["swoveralls"])
    assert {r["via"] for r in store.read_collected("swoveralls")} == {"apify"}


def test_the_check_reports_items_when_apify_answers_201(cfg, store, settings):
    http = FakeHttp()
    http.on("POST", IG_URL, (201, [REAL_IG_ITEM]))
    row = C.check_collectors(world(cfg), CREDS, settings, cfg, store, R.Ledger, NOW, ["instagram"],
                             apify=Apify("tok", http=http, sleep=lambda s: None), claude=None, backend="apify")[0]
    assert row["ok"] is True and row["items"] == 1 and "HTTP" not in row.get("detail", "")


# --- 5. the cost ceiling an actor refuses ----------------------------------------------------------------------------------------

def test_a_ceiling_below_the_actors_minimum_is_a_configuration_problem_with_nothing_spent():
    http = one_call_http(400, MINIMUM)
    with pytest.raises(P.ProviderConfigError) as seen:
        Apify("tok", http=http, sleep=lambda s: None).run("clockworks/tiktok-scraper", {}, max_items=3, max_cost_usd=0.25)
    text = str(seen.value)
    assert "$0.25" in text and "$0.50" in text and "Nothing was run and nothing was spent" in text and "only if you approve" in text
    assert len(http.calls) == 1 and K.code_for(seen.value) == K.BAD_INPUT


def test_the_actors_own_minimum_is_read_from_its_message():
    body = {"error": {"type": "max-total-charge-usd-below-minimum", "message": "allowed minimum of $1.25 per run"}}
    with pytest.raises(P.ProviderConfigError, match=r"\$1\.25"):
        Apify("tok", http=one_call_http(400, body), sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=0.1)
    odd = {"error": {"type": "max-total-charge-usd-below-minimum", "message": "too low"}}
    with pytest.raises(P.ProviderConfigError, match="a higher amount"):
        Apify("tok", http=one_call_http(400, odd), sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=0.1)


def test_other_400_answers_stay_ordinary_failures():
    body = {"error": {"type": "run-failed", "message": "The Actor run failed."}}
    with pytest.raises(P.ProviderError) as seen:
        Apify("tok", http=one_call_http(400, body), sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=1)
    assert not isinstance(seen.value, P.ProviderConfigError) and "run-failed" in str(seen.value)


def test_a_run_that_timed_out_is_not_repeated_because_that_could_charge_again():
    http = one_call_http(408, {"error": {"type": "run-timeout-exceeded", "message": "t"}})
    with pytest.raises(P.ProviderError) as seen:
        Apify("tok", http=http, sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=1)
    assert len(http.calls) == 1 and "not repeated" in str(seen.value) and K.code_for(seen.value) == K.TIMEOUT


def test_a_server_error_is_still_retried_as_before():
    http = one_call_http(503, {})
    with pytest.raises(P.ProviderError, match="gave up after retries"):
        Apify("tok", http=http, sleep=lambda s: None).run("a/b", {}, max_items=1, max_cost_usd=1)
    assert len(http.calls) == 3


def test_the_check_skips_a_route_whose_actor_needs_more_than_the_check_ceiling(cfg, store, settings):
    http = FakeHttp()
    http.on("POST", TT_URL, (400, MINIMUM))
    row = C.check_collectors(world(cfg), CREDS, settings, cfg, store, R.Ledger, NOW, ["tiktok"],
                             apify=Apify("tok", http=http, sleep=lambda s: None), claude=None, backend="apify")[0]
    assert row["ok"] is False and row["detail"].startswith("skipped: ") and "$0.50" in row["detail"] and row["code"] == K.BAD_INPUT
    assert http.calls[0]["params"]["maxTotalChargeUsd"] == 0.25                          # the ceiling was NOT raised on its own
    with pytest.raises(A.Skipped, match=r"\$0\.50"):                                  # the Acceptance run shows it as skipped, not failed
        A.row_result(row)


def test_the_check_ceiling_is_its_own_setting_and_never_exceeds_the_run_ceiling(cfg, store, settings):
    for check, run_cap, expected in ((0.5, 1.0, 0.5), (0.5, 0.3, 0.3), (0.25, 1.0, 0.25)):
        http = FakeHttp()
        http.on("POST", TT_URL, (201, [AP_TT]))
        tuned = replace(settings, apify_check_max_cost_usd=check, apify_max_cost_per_run_usd=run_cap)
        C.check_collectors(world(cfg), CREDS, tuned, cfg, store, R.Ledger, NOW, ["tiktok"], apify=Apify("tok", http=http, sleep=lambda s: None),
                           claude=None, backend="apify")
        assert http.calls[0]["params"]["maxTotalChargeUsd"] == expected


def test_the_check_ceiling_default_is_the_old_25_cents_and_is_validated(tmp_path):
    assert C.load_collector_settings("does-not-exist.yaml").apify_check_max_cost_usd == 0.25
    assert C.load_collector_settings(str(ROOT / "research.yaml")).apify_check_max_cost_usd == 0.25
    bad = tmp_path / "r.yaml"
    bad.write_text("collectors:\n  apify:\n    check_max_cost_usd: 0\n")
    with pytest.raises(C.CollectError, match="check_max_cost_usd"):
        C.load_collector_settings(str(bad))
    ok = tmp_path / "ok.yaml"
    ok.write_text("collectors:\n  apify:\n    check_max_cost_usd: 0.5\n")
    assert C.load_collector_settings(str(ok)).apify_check_max_cost_usd == 0.5


def test_an_http_408_or_504_in_a_message_is_a_timeout():
    assert K.classify_message("Apify: HTTP 408: not repeated") == K.TIMEOUT
    assert K.classify_message("Something: HTTP 504: gateway") == K.TIMEOUT


# --- 2. cookies were given but not accepted: no password login from a data-centre address ---------------------------------------

def test_with_cookies_a_signed_out_page_is_reported_not_logged_into_with_a_password(cfg):
    ig = site(cfg, "instagram")
    b = FakeBrowser({}, present_always={ig["login_wall_any"][0]}, typeable={"x"}, clickable={"x"})
    creds = {"cookies": json.dumps([{"name": "sessionid", "value": "v", "domain": ".instagram.com", "path": "/"}]), "username": "u", "password": "p"}
    with pytest.raises(B.LoginWall, match="export fresh ones.*Cookie facts"):
        C.ensure_session(b, "instagram", ig, creds)
    assert b.typed == [] and b.clicks == [] and b.opened == [ig["home"]] and b.shots == ["login-wall-instagram"]


def test_with_cookies_a_page_that_looks_like_neither_is_not_logged_into_either(cfg):
    ig = site(cfg, "instagram")
    b = FakeBrowser({}, typeable={"x"}, clickable={"x"})
    assert C.ensure_session(b, "instagram", ig, {"cookies": "[]", "username": "u", "password": "p"}) == "unknown"
    assert b.typed == [] and b.opened == [ig["home"]] and b.shots == ["unknown-state-instagram"]


def test_without_cookies_the_password_is_still_the_backup(cfg):
    b = LoginFake(cfg)
    assert C.ensure_session(b, "instagram", site(cfg, "instagram"), {"cookies": "", "username": "u", "password": "p"}) == "ok"
    assert b.typed == ["u", "p"]


# --- 2. evidence when a login page has changed ---------------------------------------------------------------------------------

def test_a_login_page_that_cannot_be_filled_in_leaves_a_picture_and_names_the_boxes_it_had(cfg):
    b = LoginFake(cfg)
    b.typeable = set()
    b.facts = {"url": "https://www.instagram.com/accounts/login/", "title": "Login • Instagram", "inputs": [
        {"tag": "input", "type": "text", "name": "email", "id": "", "placeholder": "Mobile number, username or email", "autocomplete": "username", "aria": "", "visible": True},
        {"tag": "input", "type": "password", "name": "pass", "id": "", "placeholder": "Password", "autocomplete": "current-password", "aria": "", "visible": True}]}
    with pytest.raises(B.BrowserError) as seen:
        C.ensure_session(b, "instagram", site(cfg, "instagram"), {"username": "u", "password": "SECRETPW"})
    text = str(seen.value)
    assert "could not find the username box" in text and "looked 'login_wall'" in text
    assert "name='email'" in text and "name='pass'" in text and "Mobile number, username or email" in text and "Login • Instagram" in text
    assert "SECRETPW" not in text and b.shots == ["login-form-instagram"]


def test_a_browser_that_cannot_list_boxes_still_gives_the_error_and_the_picture(cfg):
    class Bare(LoginFake):
        form_facts = None
    b = Bare(cfg)
    b.typeable = set()
    with pytest.raises(B.BrowserError, match="could not find the username box.*boxes could not be listed"):
        C.ensure_session(b, "instagram", site(cfg, "instagram"), {"username": "u", "password": "p"})
    assert b.shots == ["login-form-instagram"]


def test_describing_a_page_never_shows_a_value_and_stays_short():
    boxes = [{"tag": "input", "type": "text", "name": f"n{i}", "id": "", "placeholder": "", "autocomplete": "", "aria": "", "visible": i % 2 == 0, "value": "TOPSECRET"}
             for i in range(15)]
    text = B.describe_form_facts({"url": "https://x.example/login", "title": "T", "inputs": boxes})
    assert "TOPSECRET" not in text and text.count("input[type=text]") == 10 and "visible" in text and "hidden" in text
    assert "no boxes to type into" in B.describe_form_facts({"url": "https://x.example/", "title": "Blocked", "inputs": []})
    assert B.describe_form_facts(None) == "the page's boxes could not be listed"


class StubPage:
    def __init__(self, inputs=None, title="Login", fail=False):
        self._inputs, self._title, self._fail = inputs, title, fail
        self.url = "https://www.tiktok.com/login/phone-or-email/email?lang=en&redirect=x"
        self.scripts = []

    def on(self, *a):
        pass

    def eval_on_selector_all(self, selector, script):
        self.scripts.append((selector, script))
        if self._fail:
            raise RuntimeError("page closed")
        return self._inputs

    def title(self):
        if self._fail:
            raise RuntimeError("page closed")
        return self._title


def test_the_real_browser_lists_boxes_without_values_and_drops_the_address_query(tmp_path):
    page = StubPage([{"tag": "input", "type": "text", "name": "username", "id": "", "placeholder": "Email", "autocomplete": "", "aria": "", "visible": True}])
    facts = B.PlaywrightBrowser(page, tmp_path, None, {}).form_facts()
    assert facts == {"url": "https://www.tiktok.com/login/phone-or-email/email", "title": "Login", "inputs": page._inputs}
    selector, script = page.scripts[0]
    assert selector == "input, textarea" and ".value" not in script and "e.value" not in script          # the script never reads what was typed


def test_the_real_browser_survives_a_page_that_has_gone(tmp_path):
    facts = B.PlaywrightBrowser(StubPage(fail=True), tmp_path, None, {}).form_facts()
    assert facts["inputs"] == [] and facts["title"] == ""


# --- 4. YouTube through the official Data API ----------------------------------------------------------------------------------

class YT:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body if body is not None else {}, bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._body


GOOD = {"items": [{"snippet": {"title": "Me at the zoo"}, "statistics": {"viewCount": "1234567"}}]}
KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0"


def probe(monkeypatch, response):
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(cli, "_YT_API_GET", get)
    try:
        return cli._youtube_api_probe(KEY), seen
    except cli.Failure as exc:
        return exc, seen


def test_the_probe_reads_one_public_video_with_the_key_in_the_request_only(monkeypatch):
    (items, detail), seen = probe(monkeypatch, YT(200, GOOD))
    assert items == 1 and "Me at the zoo" in detail and "1234567 views" in detail and KEY not in detail
    assert seen["url"] == "https://www.googleapis.com/youtube/v3/videos" and seen["params"] == {"part": "snippet,statistics", "id": "jNQXAC9IVRw", "key": KEY}


@pytest.mark.parametrize("response, code, words", [
    (YT(200, {"items": []}), K.UPSTREAM_BROKEN, "no video"),
    (YT(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}), K.RATE_LIMITED, "midnight Pacific"),
    (YT(403, {"error": {"message": "x", "errors": [{"reason": "dailyLimitExceeded"}]}}), K.RATE_LIMITED, "quota"),
    (YT(400, {"error": {"message": "API key not valid. Please pass a valid API key.", "errors": [{"reason": "badRequest"}]}}), K.NEED_SETUP, "YOUTUBE_API_KEY"),
    (YT(400, {"error": {"message": "x", "errors": [{"reason": "keyInvalid"}]}}), K.NEED_SETUP, "YOUTUBE_API_KEY"),
    (YT(403, {"error": {"message": "API has not been used in project 1 before", "errors": [{"reason": "accessNotConfigured"}]}}), K.NEED_SETUP, "enable that API"),
    (YT(404, {}), K.NOT_FOUND, "not found"),
    (YT(503, {}), K.UPSTREAM_BROKEN, "503"),
    (YT(422, {"error": {"message": "odd", "errors": [{"reason": "weird"}]}}), K.BAD_INPUT, "weird"),
    (YT(500, bad_json=True), K.UPSTREAM_BROKEN, "500"),
    (requests.exceptions.Timeout("slow " + KEY), K.TIMEOUT, "30 seconds"),
    (requests.exceptions.ConnectionError("failed for url https://x?key=" + KEY), K.UPSTREAM_BROKEN, "ConnectionError")])
def test_every_way_the_official_api_can_answer_has_its_real_code_and_never_shows_the_key(monkeypatch, response, code, words):
    failure, _ = probe(monkeypatch, response)
    assert isinstance(failure, cli.Failure) and failure.code == code and words in str(failure) and KEY not in str(failure)


def test_youtube_is_declared_with_the_official_route_on_and_the_blocked_one_off_with_its_reason():
    reg = S.load_sources(str(ROOT / "sources.yaml"))
    routes = reg.source("youtube_public").routes
    assert routes["data_api"].enabled and routes["data_api"].tier == "official_api"
    off = routes["yt_dlp"]
    assert not off.enabled and "Sign in to confirm" in off.note and "2026-11-03" in off.note
    assert any("transcripts" in x for x in reg.source("youtube_public").limitations)


def test_only_the_enabled_youtube_route_is_probed_and_a_missing_key_says_so():
    ctx = A.Context(registry=S.load_sources(str(ROOT / "sources.yaml")))
    probes = {(p.source, p.route): p for p in A.build_probes(ctx, {"youtube_public"})}
    assert set(probes) == {("youtube_public", "data_api")} and probes[("youtube_public", "data_api")].skip == "no key: set YOUTUBE_API_KEY"
    ctx.youtube_api_check = lambda: (1, "ok")
    [p] = A.build_probes(ctx, {"youtube_public"})
    assert p.skip == "" and p.action == "video_details" and p.run() == (1, "ok")


def test_the_cli_runs_the_youtube_probe_only_when_the_key_secret_is_set(run_cli, capsys, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert run_cli("acceptance", "--only", "youtube_public", "--plan") == 0
    assert "no key: set YOUTUBE_API_KEY" in capsys.readouterr().out
    monkeypatch.setenv("YOUTUBE_API_KEY", "  " + KEY + "  ")                                  # stray spaces from pasting are ignored
    monkeypatch.setattr(cli, "_YT_API_GET", lambda url, **kw: (_ for _ in ()).throw(AssertionError("no network in --plan")))
    assert run_cli("acceptance", "--only", "youtube_public", "--plan") == 0
    assert "no key" not in capsys.readouterr().out


# --- the workflow and the documents --------------------------------------------------------------------------------------------

def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/acceptance.yml").read_text())


def test_the_acceptance_workflow_can_lift_a_rest_and_passes_the_youtube_key_without_printing_it():
    wf = workflow()
    steps = wf["jobs"]["acceptance"]["steps"] if "acceptance" in wf["jobs"] else next(iter(wf["jobs"].values()))["steps"]
    lift = next(s for s in steps if s.get("name", "").startswith("Lift rest"))
    assert lift["if"] == "inputs.clear != '' && inputs.plan != true" and "health --clear" in lift["run"]
    assert "${{" not in lift["run"] and lift["env"]["CLEAR"] == "${{ inputs.clear }}"                  # the input is read from the environment, never pasted into the script
    names = [s.get("name", "") for s in steps]
    assert names.index(lift["name"]) < names.index("Check each route")
    check = next(s for s in steps if s.get("name") == "Check each route")
    assert check["env"]["YOUTUBE_API_KEY"] == "${{ secrets.YOUTUBE_API_KEY }}"


def test_the_instructions_cover_each_failure_and_say_where_money_or_secrets_are_involved():
    text = (ROOT / "docs" / "acceptance-fixes.md").read_text()
    for needle in ("201", "YOUTUBE_API_KEY", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES", "GEMINI_COOKIES", "$0.50", "check_max_cost_usd", "clear", "approve"):
        assert needle in text, needle
    assert "YOUTUBE_API_KEY" in (ROOT / "README.md").read_text()


def test_spaces_pasted_around_the_key_never_reach_the_request(run_cli, monkeypatch):
    seen = []
    monkeypatch.setenv("YOUTUBE_API_KEY", "  " + KEY + "\n")
    monkeypatch.setattr(cli, "_YT_API_GET", lambda url, **kw: seen.append(kw["params"]["key"]) or YT(200, GOOD))
    assert run_cli("acceptance", "--only", "youtube_public") == 0 and seen == [KEY]
