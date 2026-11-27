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
    monkeypatch.setattr(SL, "reddit_held", lambda *a, **k: True)                                           # the owner's switch back to hold (research.yaml collectors.apify.reddit_hold: true)
    with pytest.raises(K.Failure) as f:
        SL.apify_rows("reddit", "https://www.reddit.com/r/overalls/", client=SimpleNamespace(run=lambda *a, **k: pytest.fail("no Apify call while Reddit is on hold")))
    assert f.value.code == K.NEED_SETUP and "on hold" in str(f.value)
    assert SL.APIFY_PAGE_ACTORS["reddit"][1]("https://www.reddit.com/r/x/", 1) == {"startUrls": [{"url": "https://www.reddit.com/r/x/"}], "maxItems": 1, "maxPostCount": 1, "skipComments": True}   # no proxy field: the residential proxy is about 8 dollars a GB
    assert "proxy" not in str(__import__("contentintel.voice", fromlist=["x"]).__dict__.get("REDDIT_PROXY", ""))
    assert "apify" in Path("research.yaml").read_text(encoding="utf-8") and "reddit_hold: false" in Path("research.yaml").read_text(encoding="utf-8")
    assert "on hold by the owner's decision" in Path("contentintel/cli.py").read_text(encoding="utf-8")                                              # the hold can still be switched back on


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


def test_the_idea_bank_run_says_which_phase_is_under_way_as_it_happens_and_sums_the_time_by_phase():
    out, t = [], [0.0]
    ph = IB._Phases("swoveralls", out=out.append, clock=lambda: t[0])
    t[0] = 5.0
    ph.mark("intake", "47 new")
    t[0] = 2525.0
    ph.mark("saving videos", "30 of 47")
    t[0] = 2530.0
    ph.mark("visual review")
    assert out[0] == "swoveralls: [5s] intake done in 5s (47 new)" and out[1] == "swoveralls: [2525s] saving videos done in 2520s (30 of 47)"
    line = ph.line(paused=2300.0)
    assert line.startswith("swoveralls: time by phase (slowest first): saving videos 2520s, intake 5s, visual review 5s; total 2530s") and "2300s were the deliberate pauses between video downloads" in line
    src = Path("contentintel/ideabank.py").read_text(encoding="utf-8")
    assert src.count("ph.mark(") >= 8 and "lines.append(ph.line(paused[0]))" in src


SITE = '<html><head><meta property="og:image" content="/front.jpg"></head><body><img src="/p/milk-chocolate.jpg" alt="Milk chocolate bar"><img src="/p/logo-icon.png" alt="logo"></body></html>'


def test_the_brand_sites_front_page_picture_is_offered_only_when_the_topic_is_about_the_brand():
    from contentintel import assets_find as AF
    about = AF.site_media(lambda url: SITE, "defisnacks.com", "DeFi Snacks")
    assert [a["title"] for a in about] == ["front page image"] and all(a["license"] == "brand-own" for a in about)
    other = AF.site_media(lambda url: SITE, "defisnacks.com", "sweatpant overalls", front_page=False)
    assert other == []                                                                                                                         # nothing on the site matches the topic and no front page is offered
    chocolate = AF.site_media(lambda url: SITE, "defisnacks.com", "milk chocolate", front_page=False)
    assert [a["title"] for a in chocolate] == ["Milk chocolate bar"]                                                                           # a real topic match is still found


def test_an_unrelated_topic_no_longer_stops_at_the_brand_sites_front_page_so_the_pixabay_fallback_is_reachable(monkeypatch):
    from types import SimpleNamespace
    from contentintel import assets_find as AF
    brand = SimpleNamespace(name="DeFi Snacks", domain="defisnacks.com")
    monkeypatch.setattr(AF.OV, "find", lambda get, q, kind, **kw: ([], "openverse"))
    monkeypatch.setattr(AF, "split_derived", lambda query, b, store, research: (["healthy snack"], []))
    asked = []

    def pixabay(get, key, q, limit=8):
        asked.append(q)
        return [{"kind": "image", "title": "sweatpant overalls on a hanger", "url": "https://pixabay.example/a.jpg", "thumbnail": "x", "page": "p", "creator": "c", "license": "pixabay", "license_version": "", "license_url": "", "attribution": "c / Pixabay", "source": "pixabay"}]
    monkeypatch.setattr(AF, "pixabay_search", pixabay)
    kept, report = AF.find_usable(lambda url: SITE, "sweatpant overalls", "images", brand=brand, pixabay_key="k")
    assert [a["url"] for a in kept] == ["https://pixabay.example/a.jpg"] and asked[0] == "sweatpant overalls" and report["used"].endswith("(Pixabay)")
    assert any(t["source"] == "brand site" and t["returned"] == 0 for t in report["tried"])                                                    # the site was asked and had nothing on this topic
    kept2, report2 = AF.find_usable(lambda url: SITE, "DeFi Snacks", "images", brand=brand, pixabay_key="k")
    assert report2["used"] == "the brand's own site" and kept2[0]["title"] == "front page image"                                               # a topic about the brand still gets the brand's own picture


def test_word_error_rate_and_agreement_catch_any_misheard_word_not_one_word_in_particular():
    from contentintel import transcribe as TR
    ref = TR.SPEECH_TEXT
    assert TR.word_error_rate(ref, ref) == 0.0 and TR.word_error_rate(ref, ref.upper().replace(".", "")) == 0.0                                  # case and punctuation do not count
    one = TR.word_error_rate(ref, ref.replace("transcription", "subscription"))
    other = TR.word_error_rate(ref, ref.replace("chain", "train"))
    assert one == other == round(1 / 11, 3)                                                                                                    # any single wrong word costs the same
    assert TR.word_error_rate(ref, "") == 1.0 and TR.word_error_rate("", "") == 0.0 and TR.word_error_rate(ref, "this is a test") == round(7 / 11, 3)
    share, diffs = TR.agreement("it is a shiny day and everybody is feeling okay", "it is a shiny day and everybody is feeling ok")
    assert share == 0.9 and diffs == [("okay", "ok")]
    assert TR.agreement("", "")[0] == 1.0


def test_the_probe_ranks_the_accurate_backend_first_and_refuses_one_that_gets_most_of_a_sentence_wrong(tmp_path):
    from contentintel import transcribe as TR
    speech = tmp_path / "probe_speech.wav"
    speech.write_bytes(b"RIFF" + b"\0" * 2000)
    said = {"groq_whisper": TR.SPEECH_TEXT, "cloudflare_whisper": TR.SPEECH_TEXT.replace("transcription", "subscription"), "faster_whisper": "this is a test", "vibevoice_asr": TR.SPEECH_TEXT}
    seconds = {"groq_whisper": 5.0, "cloudflare_whisper": 1.0, "faster_whisper": 0.5, "vibevoice_asr": 9.0}
    now = [0.0]

    def fake_transcribe(path, *, backends, **kw):
        now[0] += seconds[backends[0]]
        return {"text": said[backends[0]], "language": "en", "backend": backends[0]}
    original = TR.transcribe
    TR.transcribe = fake_transcribe
    try:
        res = TR.probe(tmp_path, clock=lambda: now[0])
    finally:
        TR.transcribe = original
    by = {r["backend"]: r for r in res["rows"]}
    assert by["cloudflare_whisper"]["ok"] and by["cloudflare_whisper"]["wer"] == round(1 / 11, 3) and by["groq_whisper"]["wer"] == 0.0
    assert not by["faster_whisper"]["ok"] and "got 64% of the spoken sentence's words wrong" in by["faster_whisper"]["why"]                        # fast but wrong: refused
    assert res["ranking"] == ["groq_whisper", "vibevoice_asr", "cloudflare_whisper"]                                                          # perfect ones first (the faster of them first), the one that misheard a word last


def test_a_real_transcript_gets_a_second_opinion_and_is_flagged_when_the_two_disagree(tmp_path):
    from contentintel import transcribe as TR
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    texts = {"groq_whisper": "the new subscription chain is live today for all of us", "cloudflare_whisper": "the new transcription chain is live today for all of us"}

    def fake(path, *, backends, **kw):
        if backends[0] not in texts:
            raise TR.Failure(TR.contract.UPSTREAM_BROKEN, "not asked")
        return {"text": texts[backends[0]], "language": "en", "backend": backends[0]}
    original = TR.transcribe
    TR.transcribe = fake
    try:
        primary = {"backend": "vibevoice_asr", "text": "the new subscription chain is live today for all of us"}
        got = TR.cross_check(audio, primary, ("vibevoice_asr", "cloudflare_whisper", "groq_whisper"))
        assert got["with"] == "cloudflare_whisper" and got["differences"] == [("subscription", "transcription")] and got["agreement"] == round(10 / 11, 3) and got["flag"] is False
        flagged = TR.cross_check(audio, {"backend": "groq_whisper", "text": "completely different words were heard here instead of that"}, ("groq_whisper", "cloudflare_whisper"))
        assert flagged["flag"] is True and flagged["agreement"] < TR.CHECK_AGREEMENT
        none = TR.cross_check(audio, {"backend": "groq_whisper", "text": "x"}, ("groq_whisper", "faster_whisper"))
        assert none["with"] == "" and "no second hosted backend" in none["why"]
    finally:
        TR.transcribe = original
    assert "second_opinion" in Path("contentintel/stage_cli.py").read_text(encoding="utf-8") and "CHECK THIS TRANSCRIPT" in Path("contentintel/stage_cli.py").read_text(encoding="utf-8")


def test_a_scheduled_collect_has_two_later_tries_and_a_guard_that_runs_them_only_when_the_week_has_not_been_saved(tmp_path):
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/collect.yml").read_text(encoding="utf-8"))
    crons = [c["cron"] for c in wf[True]["schedule"]]
    assert crons == ["41 7 * * 3", "41 9 * * 3", "41 11 * * 3"] and wf["jobs"]["collect"]["needs"] == "guard"
    cond = wf["jobs"]["collect"]["if"]
    assert "github.event_name != 'schedule'" in cond and "needs.guard.outputs.run == 'true'" in cond                                          # a person's own run is never held back
    script = next(s["run"] for s in wf["jobs"]["guard"]["steps"] if s.get("id") == "week")
    import subprocess

    counter = [0]

    def guard(event, commits):
        counter[0] += 1
        repo = tmp_path / f"g-{counter[0]}"
        repo.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        for msg, when in commits:
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", msg, "--date", when], cwd=repo, check=True, env={**env, "GIT_COMMITTER_DATE": when})
        out = repo / "out.txt"
        done = subprocess.run(["bash", "-e", "-c", script], cwd=repo, capture_output=True, text=True, env={**env, "EVENT": event, "GITHUB_OUTPUT": str(out)})
        return done.returncode, out.read_text(encoding="utf-8").strip(), done.stdout
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    assert guard("schedule", [("Collect 2026-11-25T07:41Z", recent)])[1] == "run=false"                                                     # saved yesterday: the later tries do nothing
    assert guard("schedule", [("Collect 2026-11-18T07:41Z", old)])[1] == "run=true"                                                         # last saved 9 days ago: this try runs it
    assert guard("schedule", [("Something else", recent)])[1] == "run=true" and guard("workflow_dispatch", [("Collect now", recent)])[1] == "run=true"
