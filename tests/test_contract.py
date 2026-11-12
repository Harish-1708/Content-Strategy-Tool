"""The collector contract: one envelope, eight codes, and the rule that a final error ends the run."""
import json

import jsonschema
import pytest
import requests

from contentintel import contract as K
from contentintel.browser import AccountWarning, BrowserError, Challenge, LoginWall
from contentintel.collectors import CollectError
from contentintel.contract import Failure
from contentintel.health import HealthLedger
from contentintel.providers import AuthError, ProviderError, QuotaError
from contentintel.research import ResearchError

from conftest import ROOT
from stage1_helpers import NOW, at, registry


class Resp:
    def __init__(self, status):
        self.status_code = status


def http_error(status):
    return requests.exceptions.HTTPError(f"{status} Client Error", response=Resp(status))


# ---- classification ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc, code", [
    (LoginWall("instagram: not logged in"), K.NEED_LOGIN),
    (Challenge("the site asked for a verification"), K.PLATFORM_BLOCKED),
    (AccountWarning("instagram", "We suspect automated behavior"), K.PLATFORM_BLOCKED),         # a subclass of Challenge: still a block, never a login problem
    (AuthError("the key was rejected"), K.NEED_SETUP),
    (QuotaError("out of credits"), K.RATE_LIMITED),
    (ProviderError("serper answered 500"), K.UPSTREAM_BROKEN),
    (BrowserError("the page did not load"), K.UPSTREAM_BROKEN),
    (BrowserError("navigation timed out after 30 s"), K.TIMEOUT),
    (BrowserError("page not found"), K.NOT_FOUND),
    (requests.exceptions.Timeout("slow"), K.TIMEOUT),
    (requests.exceptions.ConnectTimeout("slow"), K.TIMEOUT),
    (TimeoutError("x"), K.TIMEOUT),
    (requests.exceptions.ConnectionError("refused"), K.UPSTREAM_BROKEN),
    (http_error(401), K.NEED_LOGIN), (http_error(403), K.PLATFORM_BLOCKED), (http_error(404), K.NOT_FOUND),
    (http_error(429), K.RATE_LIMITED), (http_error(502), K.UPSTREAM_BROKEN),
    (FileNotFoundError(2, "No such file", "yt-dlp"), K.NEED_SETUP),
    (json.JSONDecodeError("bad", "{", 1), K.UPSTREAM_BROKEN),
    (CollectError("collectors.safety must be a list"), K.BAD_INPUT),
    (CollectError("TikTok rate limit reached"), K.RATE_LIMITED),
    (ResearchError("unknown provider x"), K.BAD_INPUT),
    (RuntimeError("something odd"), K.UPSTREAM_BROKEN),
    (RuntimeError("HTTP 429 for https://shop.example.com/products.json"), K.RATE_LIMITED),
    (Failure(K.NOT_FOUND, "gone"), K.NOT_FOUND),
])
def test_every_kind_of_failure_gets_the_right_code(exc, code):
    assert K.classify_exception(exc)["code"] == code
    assert K.code_for(exc) == code


@pytest.mark.parametrize("text, code", [
    ("HTTP 429 for https://x/products.json", K.RATE_LIMITED), ("out of credits for this month", K.RATE_LIMITED), ("rate limit reached", K.RATE_LIMITED),
    ("the ratelimit was hit", K.RATE_LIMITED), ("daily quota exceeded", K.RATE_LIMITED), ("usage limit", K.RATE_LIMITED),
    ("Too Many Requests", K.RATE_LIMITED), ("instagram: not logged in (the cookies were not accepted)", K.NEED_LOGIN),
    ("sign in to continue", K.NEED_LOGIN), ("Tiktok refused the download from this server", K.PLATFORM_BLOCKED),
    ("a verification challenge appeared", K.PLATFORM_BLOCKED), ("not a bot check", K.PLATFORM_BLOCKED),
    ("404 Not Found for https://x", K.NOT_FOUND), ("the post is no longer available", K.NOT_FOUND),
    ("Video unavailable. This video is private", K.NOT_FOUND), ("Video not available", K.NOT_FOUND), ("This video is unavailable", K.NOT_FOUND),
    ("timed out waiting", K.TIMEOUT), ("no key: set APIFY_TOKEN", K.NEED_SETUP), ("yt-dlp is not installed", K.NEED_SETUP),
    ("HTTP 503 for https://x", K.UPSTREAM_BROKEN), ("item 4290 and 4040 were counted", ""), ("", ""), (None, ""),
])
def test_plain_messages_point_to_a_code_or_to_none(text, code):
    assert K.classify_message(text) == code


def test_rate_limit_wins_over_login_wording_and_block_wins_over_login():
    assert K.classify_message("429 Too Many Requests: please log in") == K.RATE_LIMITED
    assert K.classify_message("a challenge appeared; log in again") == K.PLATFORM_BLOCKED


def test_errors_are_scrubbed_cut_and_always_carry_a_hint():
    err = K.make_error(K.NEED_SETUP, "the key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV failed " + "x" * 800)
    assert "sk-ant" not in err["message"] and len(err["message"]) <= 500
    assert err["hint"] == K.HINTS[K.NEED_SETUP]
    assert K.make_error(K.TIMEOUT, "slow", hint="custom")["hint"] == "custom"
    with pytest.raises(ValueError):
        K.make_error("MADE_UP", "x")
    with pytest.raises(ValueError):
        Failure("MADE_UP")


def test_the_six_final_codes_are_exactly_the_ones_another_route_cannot_fix():
    assert K.FINAL_CODES == {K.NOT_FOUND, K.BAD_INPUT, K.PLATFORM_BLOCKED, K.RATE_LIMITED, K.NEED_LOGIN, K.PROVIDER_RETIRED}      # a retired provider is withdrawn for good: no other route of it exists
    assert not K.FINAL_CODES & {K.TIMEOUT, K.UPSTREAM_BROKEN, K.NEED_SETUP}
    assert set(K.HINTS) == set(K.CODES) and len(K.CODES) == 9


# ---- the envelope -----------------------------------------------------------------------------------------------------------

def ok_env():
    return K.envelope("meta_ads", "check", "browser", items=[{"a": 1}], elapsed_ms=12, attempts=[{"route": "browser", "result": "ok", "items": 1}])


def bad_env():
    return K.envelope("meta_ads", "check", error=K.make_error(K.RATE_LIMITED, "slow down"), attempts=[{"route": "browser", "result": "RATE_LIMITED"}])


def test_a_success_and_a_failure_keep_the_contract_and_the_published_schema():
    schema = json.loads((ROOT / "contracts" / "envelope.v1.schema.json").read_text(encoding="utf-8"))
    for env in (ok_env(), bad_env()):
        assert K.validate_envelope(env) == []
        jsonschema.validate(env, schema)
    assert ok_env()["route"] == "browser" and ok_env()["error"] is None
    assert bad_env()["route"] is None and bad_env()["items"] == [] and bad_env()["error"]["code"] == K.RATE_LIMITED


def test_a_failed_envelope_never_carries_items_or_a_route_even_if_asked():
    env = K.envelope("x", "y", "browser", items=[1, 2], error=K.make_error(K.TIMEOUT, "t"))
    assert env["items"] == [] and env["route"] is None


def test_the_schema_file_is_the_one_in_the_code():
    stored = json.loads((ROOT / "contracts" / "envelope.v1.schema.json").read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(K.ENVELOPE_SCHEMA))


@pytest.mark.parametrize("mutate, fragment", [
    (lambda e: e.pop("route"), "missing 'route'"), (lambda e: e.update(schema_version=2), "schema_version"),
    (lambda e: e.update(ok="yes"), "true or false"), (lambda e: e.update(source=""), "source and action"),
    (lambda e: e.update(items="x"), "items must be a list"), (lambda e: e.update(meta={}), "meta needs"),
    (lambda e: e["meta"]["attempts"].append({"route": "x"}), "every attempt"),
    (lambda e: e.update(error=K.make_error(K.TIMEOUT, "t")), "must not carry an error"), (lambda e: e.update(route=None), "must name its route"),
    (lambda e: e.update(items=[1], ok=False, error=K.make_error(K.TIMEOUT, "t")), "must not carry items"),
    (lambda e: e.update(ok=False, error={"code": "NOPE"}), "contract codes"), (lambda e: e.update(ok=False, error=None), "contract codes"),
])
def test_validation_names_each_way_an_envelope_can_break_the_contract(mutate, fragment):
    env = ok_env()
    mutate(env)
    assert any(fragment in p for p in K.validate_envelope(env)), K.validate_envelope(env)
    assert K.validate_envelope("nope") == ["the envelope is not an object"]


def test_the_schema_rejects_what_the_code_rejects():
    schema = json.loads((ROOT / "contracts" / "envelope.v1.schema.json").read_text(encoding="utf-8"))
    bad = ok_env()
    bad["error"] = K.make_error(K.TIMEOUT, "t")                         # ok with an error
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    worse = bad_env()
    worse["items"] = [1]                                                # failed with items
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(worse, schema)
    code = bad_env()
    code["error"]["code"] = "MADE_UP"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(code, schema)


# ---- trying routes in order -------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.25
        return self.t


def test_the_first_route_that_answers_wins_and_later_routes_are_not_called():
    calls = []
    env = K.run_action("web_search", "search", [("tavily", lambda: calls.append("tavily") or [1, 2]), ("serpapi", lambda: calls.append("serpapi") or [3])],
                       clock=Clock())
    assert env["ok"] and env["route"] == "tavily" and env["items"] == [1, 2] and calls == ["tavily"]
    assert env["meta"]["attempts"] == [{"route": "tavily", "result": "ok", "items": 2}]
    assert K.validate_envelope(env) == []


def test_a_non_final_failure_moves_on_to_the_next_route():
    def broken():
        raise ProviderError("500 from serper")
    env = K.run_action("web_search", "search", [("serper_dev", broken), ("tavily", lambda: ["x"])], clock=Clock())
    assert env["ok"] and env["route"] == "tavily"
    assert [a["result"] for a in env["meta"]["attempts"]] == ["UPSTREAM_BROKEN", "ok"]


@pytest.mark.parametrize("exc, code", [(LoginWall("not logged in"), K.NEED_LOGIN), (Challenge("challenge"), K.PLATFORM_BLOCKED),
                                       (QuotaError("out of credits"), K.RATE_LIMITED), (CollectError("bad seeds"), K.BAD_INPUT),
                                       (Failure(K.NOT_FOUND, "gone"), K.NOT_FOUND)])
def test_a_final_failure_stops_the_run_and_no_other_route_is_tried(exc, code):
    second = []

    def first():
        raise exc
    env = K.run_action("instagram", "check", [("browser", first), ("apify", lambda: second.append(1) or ["x"])], clock=Clock())
    assert not env["ok"] and env["error"]["code"] == code and second == []
    assert [a["route"] for a in env["meta"]["attempts"]] == ["browser"]
    assert K.validate_envelope(env) == []


def test_an_empty_answer_is_a_failure_unless_empty_is_fine():
    env = K.run_action("web_search", "search", [("tavily", lambda: [])], clock=Clock())
    assert not env["ok"] and env["error"]["code"] == K.UPSTREAM_BROKEN
    env = K.run_action("web_search", "search", [("tavily", lambda: [])], clock=Clock(), empty_ok=True)
    assert env["ok"] and env["items"] == [] and env["route"] == "tavily"
    assert K.run_action("x", "y", [("r", lambda: None)], clock=Clock())["error"]["code"] == K.UPSTREAM_BROKEN


def test_no_routes_and_every_failure_are_reported_clearly():
    env = K.run_action("x", "y", [], clock=Clock())
    assert env["error"]["code"] == K.BAD_INPUT and K.validate_envelope(env) == []

    def setup():
        raise AuthError("bad key")

    def timeout():
        raise TimeoutError("slow")
    env = K.run_action("x", "y", [("a", setup), ("b", timeout)], clock=Clock())
    assert env["error"]["code"] == K.TIMEOUT                    # a real problem is reported ahead of "not set up"
    assert [a["result"] for a in env["meta"]["attempts"]] == ["NEED_SETUP", "TIMEOUT"]


def test_the_error_reported_is_the_final_one_even_when_a_milder_one_came_first():
    def mild():
        raise ProviderError("500")

    def final():
        raise QuotaError("out of credits")
    env = K.run_action("x", "y", [("a", mild), ("b", final), ("c", lambda: ["never"])], clock=Clock())
    assert env["error"]["code"] == K.RATE_LIMITED and len(env["meta"]["attempts"]) == 2


def test_any_exception_from_an_adapter_is_classified_never_raised():
    def awful():
        raise KeyError("surprise")
    env = K.run_action("x", "y", [("a", awful)], clock=Clock())
    assert not env["ok"] and env["error"]["code"] == K.UPSTREAM_BROKEN and "surprise" in env["error"]["message"]


def test_elapsed_time_is_measured():
    env = K.run_action("x", "y", [("a", lambda: [1])], clock=Clock())
    assert env["meta"]["elapsed_ms"] == 500                    # the fake clock ticks 0.25 s per reading: start, route start, finish = 0.5 s


def test_a_resting_route_is_skipped_not_called_and_recorded(tmp_path):
    reg = registry(tmp_path)
    ledger = HealthLedger({}, reg)
    ledger.observe("web_search", "serper_dev", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    called = []
    env = K.run_action("web_search", "search", [("serper_dev", lambda: called.append("s") or [1]), ("tavily", lambda: ["t"])], health=ledger, now=NOW, clock=Clock())
    assert called == [] and env["ok"] and env["route"] == "tavily"
    first = env["meta"]["attempts"][0]
    assert first["skipped"] is True and first["result"] == K.RATE_LIMITED and first["retry_after_seconds"] == 3600
    # the skip is not an attempt: it must not count against the route
    assert len(ledger.state["routes"]["web_search|serper_dev"]["history"]) == 1
    # once the rest is over the route is used again
    later = K.run_action("web_search", "search", [("serper_dev", lambda: called.append("s") or [1])], health=ledger, now=at(NOW, hours=2), clock=Clock())
    assert later["ok"] and called == ["s"]


def test_every_attempt_is_recorded_with_its_code(tmp_path):
    ledger = HealthLedger({}, registry(tmp_path))

    def broken():
        raise ProviderError("500")
    K.run_action("web_search", "search", [("serper_dev", broken), ("tavily", lambda: ["a", "b", "c"])], health=ledger, now=NOW, clock=Clock())
    s, t = ledger.state["routes"]["web_search|serper_dev"], ledger.state["routes"]["web_search|tavily"]
    assert s["history"][0]["ok"] is False and s["history"][0]["code"] == K.UPSTREAM_BROKEN
    assert t["history"][0]["ok"] is True and t["history"][0]["items"] == 3 and t["last_success"] == NOW
