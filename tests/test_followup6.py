"""Update 73 v3, follow-up 6: the fixes for what the live test round found (reclassify budget and dropped claims, the trending lane, the grid stall,
the Idea Bank ledger, the Apify failure reason, the Reddit hold, the save wording, the transcript echo and the corrected documents)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentintel import apify as AP
from contentintel import batch2_cli as B2
from contentintel import canon as C
from contentintel import collectors as CO
from contentintel import contract as K
from contentintel import ideabank as IB
from contentintel import research_tools as RT
from contentintel import session_live as SL
from contentintel import verify_claims as VC

AT = "2026-11-24T08:00:00Z"
NAME = "Swoveralls"


class Judge:
    def json_task(self, system, user, max_tokens=None):
        if system == VC.QUERY_SYSTEM:
            return {"queries": ["Swoveralls award 2026"]}
        return {"verdict": "silent", "quote": "", "about_brand": True}


def CANON_VERIFY_TARGETS(reg):
    return len(VC.targets(reg, 1000))


def seeded(tmp_path):
    reg = C.Registry(tmp_path / "data", "swoveralls")
    C.apply_seed(reg, C.load_seed(Path("brands/swoveralls/claims_seed.yaml")), AT)
    return reg


def open_ids(reg, n):
    return [c["id"] for c in reg.state()["claims"].values() if c["status"] != "verified"][:n]


def test_a_call_limit_never_drops_claims_silently(tmp_path):
    reg = seeded(tmp_path)
    ids = open_ids(reg, 3)
    events, report = [], {"calls": 0, "errors": 0, "notes": []}
    VC.verify_ids(reg, NAME, ids, search=lambda q: [], get=lambda u: None, claude=Judge(), now=AT, report=report, max_calls=1, progress=lambda *a: events.append((a[0], a[1])))
    assert report["outcomes"][ids[0]] != "not checked" and report["outcomes"][ids[1]] == "not checked" and report["outcomes"][ids[2]] == "not checked"
    assert ("skipped", ids[1]) in events and ("skipped", ids[2]) in events and ("start", ids[1]) not in events and ("start", ids[2]) not in events             # no 'starting' line for a claim that never ran
    assert any("2 claim(s) were NOT checked" in n for n in report["notes"])


def test_the_budget_of_a_recheck_is_sized_to_the_claims_being_rechecked(tmp_path):
    reg = seeded(tmp_path)
    ids = open_ids(reg, 3)
    args = SimpleNamespace(max_claims=0, verify_calls=0, brands_dir="brands", action="reclassify-conflicts")
    verifier, rep = B2._claim_verifier(reg, "swoveralls", args, Judge(), lambda q: [], lambda u: None, AT, extra_waiting=len(ids))
    verifier(ids)
    assert "not checked" not in rep["outcomes"].values() and len(rep["outcomes"]) == 3
    many = [c["id"] for c in reg.state()["claims"].values()][:12]
    assert len(many) == 12
    for cid in many:
        reg.record_evidence(cid, [], [], "unverified", AT)                                                  # already checked once: the verifier's own list of claims waiting is now empty, as in a re-check
    assert CANON_VERIFY_TARGETS(reg) == 0
    none_extra, r0 = B2._claim_verifier(reg, "swoveralls", args, Judge(), lambda q: [], lambda u: None, AT, extra_waiting=0)
    none_extra(many)
    assert list(r0["outcomes"].values()).count("not checked") == 2                                          # a budget of 10 calls for 12 claims: two are not reached, and said so
    sized, r1 = B2._claim_verifier(reg, "swoveralls", args, Judge(), lambda q: [], lambda u: None, AT, extra_waiting=len(many))
    sized(many)
    assert "not checked" not in r1["outcomes"].values() and len(r1["outcomes"]) == 12                       # sized for the 12 being re-checked: all reached
    text = Path("contentintel/batch2_cli.py").read_text(encoding="utf-8")
    assert "announce=True, extra_waiting=len(by_evidence)" in text and "recheck_explanations(slug, reg.state()[\"claims\"], by_evidence, outcomes, after)" in text


def test_every_recheck_whose_status_differs_from_what_the_new_check_found_is_explained():
    claims = {"c1": {"conflicts_with": "c9"}, "c2": {}, "c3": {}, "c4": {}}
    lines = B2.recheck_explanations("swoveralls", claims, ["c1", "c2", "c3", "c4"], ["unverified", "verified", "not checked", "conflicted"], ["conflicted", "verified", "conflicted", "conflicted"])
    assert len(lines) == 2                                                                                                            # c2 and c4 agree with their outcome: nothing to explain
    assert "c1: the new check found UNVERIFIED, the claim is CONFLICTED: it also conflicts with claim c9" in lines[0]
    assert "c3: NOT CHECKED (call limit): stays CONFLICTED" in lines[1]


def test_the_trending_lane_keeps_only_items_that_share_a_word_with_the_brands_terms():
    terms = ["Swoveralls", "sweatpant overalls", "loungewear"]
    assert RT._lane_ok({"title": "Why loungewear is back", "url": "https://x.example/1"}, terms) is True
    assert RT._lane_ok({"title": "Nvidia Reinstates Jensen Huang as CEO", "url": "https://x.example/2"}, terms) is False                      # off-topic, even with a link
    assert RT._lane_ok({"title": "Loungewear boom", "url": ""}, terms) is True                                                                 # a topic needs no link (voice does)
    text = Path("contentintel/research_tools.py").read_text(encoding="utf-8")
    assert "trending_lane = [x for x in trending_lane if x.get(\"tier\") == \"judged\" or _lane_ok(x, terms)]" in text and "Strongest in the saved voice" in text


class Grid:
    def __init__(self, states):
        self.states, self.polls, self.nudges, self.waited = list(states), 0, 0, 0

    def images_state(self, spinner, images, tiles):
        s = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return s

    def nudge(self, tiles=()):
        self.nudges += 1

    def wait(self, seconds):
        self.waited += seconds


def test_a_grid_that_stops_changing_is_read_as_it_stands_instead_of_waited_for_the_whole_minute():
    stuck = {"loaded": 7, "total": 7, "tiles": 24, "blank": 17, "spinner": True}
    g = Grid([stuck])
    note = CO.settle_images(g, {"settle_images": {"max_seconds": 60, "poll_seconds": 3, "stable_polls": 2, "stall_polls": 5}})
    assert "the grid stalled: 7 of 7 images drawn, 17 blank tile(s)" in note and g.nudges >= 2 and g.waited < 60 and g.polls <= 8                        # Acceptance #18 waited the whole 60 s for this
    moving = Grid([{"loaded": 2, "total": 7, "tiles": 24, "blank": 17, "spinner": True}, {"loaded": 4, "total": 7, "tiles": 24, "blank": 10, "spinner": True},
                   {"loaded": 7, "total": 7, "tiles": 24, "blank": 0, "spinner": False}])
    assert CO.settle_images(moving, {"settle_images": {"max_seconds": 60, "poll_seconds": 3, "stable_polls": 2, "stall_polls": 5}}) == ""             # a grid that is still filling in is waited for
    import yaml
    assert yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["sites"]["instagram"]["settle_images"]["stall_polls"] == 8


def test_the_ledger_line_makes_the_kept_count_add_up_and_says_what_became_of_every_item_taken_in():
    def it(state, score=None, reason=""):
        return {"state": state, "score": score, "removed_reason": reason}
    bank = {"items": {"a": it("kept", 80), "b": it("kept", 70), "old": it("kept", 60),                                                             # a, b, old were kept before
                      "n1": it("kept", 75), "n2": it("removed", 10, "not relevant enough (scored 10, the minimum is 30)"), "n3": it("new"),
                      "gone": it("removed", 20, "a copy of a (the very same file)")}}
    # 'gone' was kept before and is a copy now; the three new ones were taken in this run
    line = IB._ledger_line("duderobe", bank, {"a", "b", "old", "gone"}, {"a", "b", "old", "gone"}, {"kept": 4, "new": 1, "removed": 2})
    assert "kept 4 -> 4 = 4 + 1 now kept (1 taken in this run, 0 brought back or judged again) - 1 no longer kept (1 a copy of a)" in line
    assert "of the 3 taken in: 1 kept, 1 dropped at once, 1 still waiting to be scored (not shown)" in line and "DOES NOT ADD UP" not in line
    wrong = IB._ledger_line("duderobe", bank, {"a", "b", "old", "gone"}, {"a", "b", "old", "gone"}, {"kept": 9, "new": 0, "removed": 0})
    assert "THE LEDGER DOES NOT ADD UP" in wrong
    assert "lines.append(_ledger_line(brand.slug, bank, ids_before, kept_before, after))" in Path("contentintel/ideabank.py").read_text(encoding="utf-8")


def test_a_failed_apify_run_says_why_in_the_actors_own_words():
    calls = []

    def http(method, url, params=None, json_body=None, headers=None, timeout=30):
        calls.append(method)
        if method == "POST":
            return 400, {"error": {"message": "Actor run did not succeed (run ID: AbCdEf0123456789x)"}}, None
        return 200, {"data": {"status": "FAILED", "statusMessage": "Reddit blocked the request from a datacenter address"}}, None
    with pytest.raises(Exception) as e:
        AP.Apify("tok", http=http, sleep=lambda s: None)._call_sync("trudax/reddit-scraper-lite", {}, max_items=1, max_cost_usd=0.25, timeout=30)
    assert "HTTP 400" in str(e.value) and "the actor said: Reddit blocked the request from a datacenter address" in str(e.value) and calls == ["POST", "GET"]
    def dead(method, url, params=None, json_body=None, headers=None, timeout=30):
        if method == "POST":
            return 400, {"error": {"message": "Actor run did not succeed (run ID: AbCdEf0123456789x)"}}, None
        raise RuntimeError("no network")
    with pytest.raises(Exception) as e2:
        AP.Apify("tok", http=dead, sleep=lambda s: None)._call_sync("a/b", {}, max_items=1, max_cost_usd=0.25, timeout=30)
    assert "could not be read" in str(e2.value)                                                                                                   # a diagnostic, never a second failure


def test_reddit_stays_on_hold_everywhere_it_would_spend_apify_money(monkeypatch):
    with pytest.raises(K.Failure) as f:
        SL.apify_rows("reddit", "https://www.reddit.com/r/overalls/", client=SimpleNamespace(run=lambda *a, **k: pytest.fail("no Apify call while Reddit is on hold")))
    assert f.value.code == K.NEED_SETUP and "on hold" in str(f.value)
    assert SL.APIFY_PAGE_ACTORS["reddit"][1]("https://www.reddit.com/r/x/", 1) == {"startUrls": [{"url": "https://www.reddit.com/r/x/"}], "maxItems": 1, "maxPostCount": 1, "skipComments": True}   # no proxy field: the residential proxy is about 8 dollars a GB
    assert "proxy" not in str(__import__("contentintel.voice", fromlist=["x"]).__dict__.get("REDDIT_PROXY", ""))
    assert "apify" in Path("research.yaml").read_text(encoding="utf-8") and "reddit_hold: true" in Path("research.yaml").read_text(encoding="utf-8")
    assert "on hold by the owner's decision" in Path("contentintel/cli.py").read_text(encoding="utf-8")


def test_the_save_steps_say_plainly_what_was_saved_and_skipped_and_hide_no_error():
    sessions = Path(".github/workflows/sessions.yml").read_text(encoding="utf-8")
    assert "Saved: commit $(git rev-parse --short HEAD)" in sessions and "Skipped (this kind of run does not write it)" in sessions and "does not exist yet (this kind of run does not write it)" not in sessions
    skills = Path(".github/workflows/skills.yml").read_text(encoding="utf-8")
    assert "git add -f data/_shared/skills_catalogue.json 2>/dev/null || true" not in skills and "if [ -e data/_shared/skills_catalogue.json ]; then git add -f" in skills


def test_the_transcript_text_is_shown_in_the_run_summary():
    text = Path("contentintel/stage_cli.py").read_text(encoding="utf-8")
    assert 'f"stage: {args.brand}: transcript"' in text and "first 1500 of" in text and "[:1500]" in text


def test_the_documents_say_what_is_true_now():
    assert "Node 22 (tested)" not in Path("docs/ai/freellmapi-version-comparison.md").read_text(encoding="utf-8")
    assert "Node 24" in Path("docs/platform/connections.md").read_text(encoding="utf-8") and '"node": "24"' in Path("ai/gateway.lock.json").read_text(encoding="utf-8")
    assert "Nothing in Stage 3 is live-verified yet" not in Path("docs/observatory/README.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "Collect 6 dollars (600 items), voice 5 (500), sessions read 3 (300)" not in readme and "sessions read 2, the Reddit backup 1.40" in readme
    assert "Pixabay by default" in Path("contentintel/mpt.py").read_text(encoding="utf-8") and "Dropped 2026-11-24: a desktop GUI editor" in Path("docs/platform/integration-matrix.md").read_text(encoding="utf-8")
