"""The Agent-Reach Instagram route beside the existing collector: OpenCLI calls, normalising, merging, comparing and the hosted-runner refusal. MOCK: OpenCLI itself is not run."""
import json
import re
import subprocess

import pytest

from contentintel import agent_reach_instagram as AR
from contentintel import collectors as C
from contentintel import contract as K

NOW = "2026-11-04T10:00:00Z"
POST = {"url": "https://www.instagram.com/p/ABC123/?utm_source=x", "author": "graza", "text": "New squeeze bottle", "date": "2026-10-30T09:00:00Z", "likes": 1200, "comments": 40, "thumbnail": "https://cdn/x.jpg"}


class Proc:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def cli(out="[]", err="", code=0, *, environ=None, installed=True, raises=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if raises:
            raise raises
        return Proc(out if isinstance(out, str) else json.dumps(out), err, code)
    c = AR.OpenCLI("opencli", run=run, which=(lambda b: "/usr/bin/opencli") if installed else (lambda b: None), environ=environ if environ is not None else {})
    c.calls = calls
    return c


# ---- calling OpenCLI -------------------------------------------------------------------------------------------------------------------------

def test_the_command_is_exactly_opencli_instagram_with_json_output_and_one_try():
    c = cli([POST])
    assert AR.rows_from(c.call(["user", "graza", "--limit", 5])) == [POST]
    argv, kw = c.calls[0]
    assert argv == ["opencli", "instagram", "user", "graza", "--limit", "5", "-f", "json"] and kw["timeout"] == 90 and len(c.calls) == 1


def test_it_never_runs_on_a_github_hosted_runner_even_if_the_program_is_there():
    c = cli([POST], environ={"RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_ACTIONS": "true"})
    with pytest.raises(K.Failure) as seen:
        c.call(["user", "x"])
    assert seen.value.code == K.NEED_SETUP and "GitHub-hosted runner" in str(seen.value) and "stay on a machine you control" in str(seen.value) and c.calls == []
    assert AR.hosted_runner({"RUNNER_ENVIRONMENT": "github-hosted"}) and not AR.hosted_runner({"RUNNER_ENVIRONMENT": "self-hosted"}) and not AR.hosted_runner({})
    assert AR.rows_from(cli([POST], environ={"RUNNER_ENVIRONMENT": "self-hosted"}).call(["user", "x"])) == [POST]          # a runner you own is allowed


def test_a_missing_program_a_timeout_and_a_start_failure_have_their_own_codes():
    with pytest.raises(K.Failure) as seen:
        cli(installed=False).call(["user", "x"])
    assert seen.value.code == K.NEED_SETUP and "not installed" in str(seen.value)
    with pytest.raises(K.Failure) as seen:
        cli(raises=subprocess.TimeoutExpired("opencli", 90)).call(["user", "x"])
    assert seen.value.code == K.TIMEOUT
    with pytest.raises(K.Failure) as seen:
        cli(raises=PermissionError("no")).call(["user", "x"])
    assert seen.value.code == K.NEED_SETUP


def block(code, message):
    return f"(node:1) ExperimentalWarning: something\nok: false\nerror:\n  code: {code}\n  message: {message}\n"


@pytest.mark.parametrize("text, expected", [
    (block("AUTH_REQUIRED", "Please log in to Instagram"), K.NEED_LOGIN), (block("X", "You are not logged in"), K.NEED_LOGIN), (block("X", "HTTP 401"), K.NEED_LOGIN),
    (block("X", "login required"), K.NEED_LOGIN), (block("X", "redirected to the login page"), K.NEED_LOGIN), (block("X", "Login wall shown"), K.NEED_LOGIN),     # the word alone, with no error code to help
    (block("ACCOUNT_RISK", "unusual activity"), K.PLATFORM_BLOCKED), (block("X", "A captcha is required"), K.PLATFORM_BLOCKED), (block("X", "checkpoint_required"), K.PLATFORM_BLOCKED),
    (block("RATE_LIMITED", "slow down"), K.RATE_LIMITED), (block("X", "HTTP 429 Too Many Requests"), K.RATE_LIMITED),
    (block("X", "browser bridge extension is not connected"), K.NEED_SETUP), (block("X", "HTTP 404 user not found"), K.NOT_FOUND), (block("X", "HTTP 404 please log in to see more"), K.NOT_FOUND),
    (block("X", "something odd happened"), K.UPSTREAM_BROKEN), ("totally not yaml: [", K.UPSTREAM_BROKEN)])
def test_errors_get_their_real_codes_and_a_security_check_is_never_worked_around(text, expected):
    c = cli(text, code=1)
    with pytest.raises(K.Failure) as seen:
        c.call(["user", "x"])
    assert seen.value.code == expected and len(c.calls) == 1
    if expected == K.PLATFORM_BLOCKED:
        assert "Nothing is worked around" in str(seen.value)
    if expected == K.NEED_LOGIN:
        assert "the tool never does" in str(seen.value)


def test_secrets_in_an_error_message_are_scrubbed_and_the_text_is_kept_short():
    secret = "sk-ant-api03-" + "A" * 40
    with pytest.raises(K.Failure) as seen:
        cli(block("X", f"failed with key {secret} " + "word " * 200), code=1).call(["user", "x"])
    assert secret not in str(seen.value) and len(str(seen.value)) < 400


def test_an_ok_false_answer_with_a_zero_exit_status_is_still_an_error():
    with pytest.raises(K.Failure) as seen:
        cli({"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "log in"}}).call(["user", "x"])
    assert seen.value.code == K.NEED_LOGIN


# ---- normalising -----------------------------------------------------------------------------------------------------------------------------

def test_rows_are_found_in_a_list_or_under_items_or_data():
    assert AR.rows_from([POST, "junk", 3]) == [POST] and AR.rows_from({"items": [POST]}) == [POST] and AR.rows_from({"ok": True, "data": [POST]}) == [POST]
    assert AR.rows_from({"data": []}) == [] and AR.rows_from("nothing") == [] and AR.rows_from({"name": "profile"}) == [{"name": "profile"}]


def test_a_post_becomes_the_same_record_and_id_the_existing_collector_makes_so_a_post_is_never_counted_twice():
    ours = AR.normalize_post(POST)
    theirs = C.normalize_instagram({"code": "ABC123", "user": {"username": "graza"}, "caption": {"text": "New squeeze bottle"}, "like_count": 1200, "comment_count": 40,
                                    "taken_at": 1761814800})
    assert ours["item_id"] == theirs["item_id"] == "instagram:ABC123" and ours["url"] == theirs["url"] == "https://www.instagram.com/p/ABC123/"
    assert (ours["handle"], ours["text"], ours["likes"], ours["comments"], ours["media_url"], ours["found_via"]) == ("graza", "New squeeze bottle", 1200, 40, "https://cdn/x.jpg", "agent_reach")
    assert set(ours) == set(theirs) and ours["posted_at"].startswith("2026-10-30")


def test_reels_codes_without_urls_hidden_text_and_unusable_rows():
    reel = AR.normalize_post({"url": "https://www.instagram.com/reel/RRR1/", "username": "@x", "caption": "Hi\u200b there", "play_count": "9,000"})
    assert reel["item_id"] == "instagram:RRR1" and reel["handle"] == "x" and reel["text"] == "Hi there" and reel["views"] == 9000
    by_code = AR.normalize_post({"shortcode": "ZZ9", "is_video": True}, handle_hint="@h")
    assert by_code["url"] == "https://www.instagram.com/reel/ZZ9/" and by_code["handle"] == "h"
    assert AR.normalize_post({"text": "no link"}) is None and AR.normalize_post({"url": "https://www.instagram.com/graza/"}) is None


def test_collecting_stops_the_whole_route_on_a_login_or_block_but_skips_a_missing_account():
    calls = []

    class Fake:
        def call(self, args):
            calls.append(args[1])
            if args[1] == "ghost":
                raise K.Failure(K.NOT_FOUND, "no such account")
            if args[1] == "locked":
                raise K.Failure(K.NEED_LOGIN, "log in")
            return [dict(POST, url=f"https://www.instagram.com/p/{args[1].upper()}/")]
    rows, problems = AR.collect_handles(Fake(), ["@graza", "ghost", "elev8", "locked", "never"], 3)
    assert [r["item_id"] for r in rows] == ["instagram:GRAZA", "instagram:ELEV8"] and calls == ["graza", "ghost", "elev8", "locked"]
    assert [(p["handle"], p["code"]) for p in problems] == [("ghost", K.NOT_FOUND), ("locked", K.NEED_LOGIN)]


# ---- merging and comparing -------------------------------------------------------------------------------------------------------------------

def held(**kw):
    return {**C.normalize_instagram({"code": "ABC123", "user": {"username": "graza"}, "caption": "Old caption", "like_count": 1000, "comment_count": 30}), **kw}


def test_the_established_route_wins_and_the_new_route_only_fills_gaps_and_notes_real_disagreement():
    old = held(posted_at="", media_url="")
    merged, filled, conflicts = AR.merge_rows(old, AR.normalize_post(POST))
    assert merged["text"] == "Old caption" and merged["likes"] == 1000                      # the established values stand
    assert merged["posted_at"].startswith("2026-10-30") and merged["media_url"] == "https://cdn/x.jpg" and sorted(filled) == ["media_url", "posted_at"]
    assert conflicts == ["likes 1000 vs 1200", "comments 30 vs 40"]
    assert "routes disagree: likes 1000 vs 1200, comments 30 vs 40 (kept the established route's)" in merged["details"] and "filled from agent_reach" in merged["details"]
    assert merged["found_via"] == "browser+agent_reach"
    near, _, quiet = AR.merge_rows(held(), AR.normalize_post({**POST, "likes": 1050, "comments": 30}))
    assert quiet == [] and near["likes"] == 1000                                              # within 10%: not a disagreement


def test_merging_with_history_counts_new_and_overlapping_posts_and_ignores_its_own_earlier_batches():
    batches = [{"source": "instagram", "via": "browser", "items": [held()]}, {"source": "instagram", "via": "apify+browser", "items": [held(item_id="instagram:APF1")]},
               {"source": "instagram", "via": "agent_reach", "items": [held(text="from the new route earlier")]}, {"source": "tiktok", "via": "browser", "items": [held(item_id="tiktok:1")]}]
    rows, stats = AR.merge_with_existing(batches, [AR.normalize_post(POST), AR.normalize_post({"url": "https://www.instagram.com/p/NEW1/", "text": "fresh"})])
    assert stats == {"new": 1, "overlap": 1, "filled": stats["filled"], "conflicts": 2} and [r["item_id"] for r in rows] == ["instagram:ABC123", "instagram:NEW1"]
    assert rows[0]["text"] == "Old caption"                                                   # not "from the new route earlier"


def test_reading_the_history_back_gives_one_record_per_post_with_the_established_numbers():
    browser = {"at": "2026-11-01T10:00:00Z", "source": "instagram", "via": "browser", "seeds": ["#x"], "items": [held()]}
    merged, _ = AR.merge_with_existing([browser], [AR.normalize_post(POST)])
    agent = {"at": NOW, "source": "instagram", "via": "agent_reach", "seeds": ["graza"], "items": merged}
    got = C.collected_rows([browser, agent])
    rows = list(got.values()) if isinstance(got, dict) else list(got)
    assert len(rows) == 1 and rows[0]["likes"] == 1000 and rows[0]["times_seen"] == 2 and rows[0]["first_seen"] == "2026-11-01T10:00:00Z"


def batches_for_compare(agent_posts, browser_posts):
    mk = lambda code, **kw: {**C.normalize_instagram({"code": code, "user": {"username": "u"}, "caption": kw.pop("caption", "text"), "like_count": kw.pop("likes", 10), "comment_count": 1,   # noqa: E731
                                                       "taken_at": 1761814800}), **kw}
    return [{"source": "instagram", "via": "browser", "items": [mk(c) for c in browser_posts]},
            {"source": "instagram", "via": "agent_reach", "items": [mk(c, found_via="agent_reach") for c in agent_posts]}]


def test_the_comparison_counts_overlap_unique_items_and_completeness():
    got = AR.compare(batches_for_compare(["a", "b", "c", "d"], ["c", "d", "e"]), {"success_rate_7d": 90})
    assert (got["overlap"], got["unique_to_agent_reach"], got["unique_to_browser"]) == (2, 2, 1) and got["browser"]["posts"] == 3 and got["agent_reach"]["usable"] == 4
    assert got["verdict"].startswith("Keep it on")
    assert "Found by both: 2. Only by Agent-Reach (usable): 2." in AR.report_text(got, "Defi", NOW)


@pytest.mark.parametrize("agent, browser, rate, fragment", [
    ([], ["a"], None, "has collected nothing yet"), (["a", "b"], ["a", "b", "c"], 90, "only 0% of its posts are new"), (["a", "b"], ["c"], 40, "succeeded only 40%")])
def test_the_verdict_names_the_reason_and_never_switches_anything_off(agent, browser, rate, fragment):
    got = AR.compare(batches_for_compare(agent, browser), {"success_rate_7d": rate} if rate is not None else None)
    assert fragment in got["verdict"] and (got["verdict"].endswith("nothing was switched off.") or "nothing to compare" in got["verdict"])


def test_a_less_complete_route_is_called_out():
    b = batches_for_compare(["x", "y"], ["a", "b"])
    for item in b[1]["items"]:
        item.update(text="", likes=None, posted_at="")
        item["url"] = item["url"]
    got = AR.compare(b, {"success_rate_7d": 95})
    assert "noticeably less complete" in got["verdict"]


# ---- the check, and nothing secret in the workflows ------------------------------------------------------------------------------------------

def test_the_acceptance_check_never_raises_and_reports_the_real_code():
    ok = AR.check_row(cli([POST]), "instagram")
    assert ok == {"ok": True, "items": 1, "detail": "read 1 post(s) of @instagram through OpenCLI"}
    bad = AR.check_row(cli(block("AUTH_REQUIRED", "log in"), code=1), "instagram")
    assert bad["ok"] is False and bad["code"] == K.NEED_LOGIN
    hosted = AR.check_row(cli([POST], environ={"RUNNER_ENVIRONMENT": "github-hosted"}), "instagram")
    assert hosted["ok"] is False and hosted["code"] == K.NEED_SETUP


def test_no_workflow_gives_the_instagram_agent_reach_route_a_cookie_or_password():
    from conftest import ROOT
    text = (ROOT / ".github" / "workflows" / "observatory.yml").read_text()
    # the observatory workflow (where this route's command lives) is given no Instagram login of any kind; the route reads through a browser a person logged in on their own machine
    assert not re.search(r"secrets\.\w*(INSTAGRAM|OPENCLI|COOKIE)\w*", text)


@pytest.mark.parametrize("value, expected", [("9,000", 9000), ("1.2K", 1200), ("3M", 3_000_000), (" 45 ", 45), (7, 7), ("about 5", None), (None, None), ("", None)])
def test_counts_written_as_text_are_understood_and_anything_else_is_left_empty(value, expected):
    row = AR.normalize_post({"url": "https://www.instagram.com/p/C1/", "likes": value})
    assert row["likes"] == expected
