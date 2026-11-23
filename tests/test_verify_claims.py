"""Verifying claims from public sources with the pool model (verify_claims.py), the evidence rule enforced where the ledger is read (canon.py), the foundation's use of it and the exit-code policy
(a section the gate rejects is recorded and the run goes on)."""
from pathlib import Path

import pytest

from contentintel import canon as C
from contentintel import cli
from contentintel import foundation as FD
from contentintel import skills as SK
from contentintel import verify_claims as VC

AT = "2026-11-18T08:00:00Z"
NAME = "Swoveralls"
QUOTE_A = "Swoveralls won the 2026 Probe Design Award, the awarding body said on 3 March."
QUOTE_B = "Trade press confirmed that Swoveralls won the 2026 Probe Design Award for its overalls."


def seeded(tmp_path):
    reg = C.Registry(tmp_path / "data", "swoveralls")
    C.apply_seed(reg, C.load_seed(Path("brands/swoveralls/claims_seed.yaml")), AT)
    return reg


def claim_id(reg, start):
    return next(c["id"] for c in reg.state()["claims"].values() if c["text"].startswith(start))


def page(quote, brand="Swoveralls"):
    return f"<html><body><h1>{brand} news</h1><p>{quote}</p><p>Other filler text about weather and shipping that makes this a page of real length for the check.</p></body></html>"


class Judge:
    """A scripted pool model: it makes queries and judges pages by what the page text says."""
    def __init__(self, verdicts=None, queries=None, fail_queries=False):
        self.verdicts, self.queries, self.fail_queries, self.calls = verdicts or {}, queries or ["Swoveralls award 2026"], fail_queries, []

    def json_task(self, system, user, max_tokens=None):
        self.calls.append(system[:30])
        if system == VC.QUERY_SYSTEM:
            if self.fail_queries:
                raise RuntimeError("down")
            return {"queries": self.queries}
        for needle, answer in self.verdicts.items():
            if needle in user:
                return answer
        return {"verdict": "silent", "quote": "", "about_brand": True}


def supports(quote, close=True, risk="low", medium="page"):
    return {"verdict": "supports", "quote": quote, "about_brand": True, "close_to_fact": close, "exaggeration_risk": risk, "medium": medium}


def ev(domain, quote=QUOTE_A, close=True, risk="low", medium="page"):
    return {"url": f"https://{domain}/x", "domain": domain, "quote": quote, "close": close, "risk": risk, "medium": medium}


def run(reg, judge, urls, pages, **kw):
    report = {"calls": 0, "errors": 0, "notes": []}
    done = VC.verify_ids(reg, NAME, [claim_id(reg, "Awards")], search=lambda q: [{"url": u, "title": ""} for u in urls], get=lambda u: pages.get(u), claude=judge, now=AT, report=report, **kw)
    return done, report


def test_two_close_sources_verify_a_blocked_claim_and_the_gate_then_lets_it_through(tmp_path):
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    assert reg.state()["claims"][cid]["status"] == "blocked" and reg.gate("Swoveralls won several awards for its design.")["verdict"] == "BLOCKED"
    urls = ["https://www.example-news.com/a", "https://trade.example.co.uk/b", "https://example-news.com/second-page"]
    judge = Judge({"Probe Design Award, the awarding": supports(QUOTE_A), "Trade press confirmed": supports(QUOTE_B)})
    done, report = run(reg, judge, urls, {urls[0]: page(QUOTE_A), urls[1]: page(QUOTE_B), urls[2]: page(QUOTE_A)})
    claim = reg.state()["claims"][cid]
    assert done == [cid] and claim["status"] == "verified" and claim["verified_by"] == "evidence" and claim["single_source"] is False and claim["source"] == urls[0]
    assert {s["domain"] for s in claim["evidence"]["supports"]} == {"example-news.com", "example.co.uk"} and "verified from 2 independent sources" in claim["reason"]
    assert reg.gate("Swoveralls won several awards for its design.")["verdict"] != "BLOCKED" and report["calls"] == 3                 # one query call and one judgement per source: the third page was never needed


def test_one_source_of_any_kind_in_any_medium_verifies_a_claim_flagged_as_single_source_for_a_spot_check(tmp_path):
    cases = (("page", "https://brand.example.com/about", "low"), ("social post", "https://www.instagram.com/startupcpg/p/abc", "high"), ("interview", "https://magazine.example.net/interview-with-the-founder", "low"),
             ("podcast", "https://podcasts.example.fm/episode-12-transcript", "low"), ("video", "https://www.youtube.com/watch?v=abc123", "high"), ("newsletter", "https://substack.example.com/p/issue-9", "low"),
             ("community post", "https://community.example.org/t/swoveralls-thread", "high"), ("press", "https://trade-journal.example.com/news", "low"))
    for n, (medium, url, risk) in enumerate(cases):
        reg = seeded(tmp_path / str(n))
        cid = claim_id(reg, "Awards")
        done, report = run(reg, Judge({"Probe Design Award": supports(QUOTE_A, True, risk, medium)}), [url], {url: page(QUOTE_A)})
        claim = reg.state()["claims"][cid]
        assert done == [cid] and claim["status"] == "verified" and claim["single_source"] is True and report["outcomes"][cid] == "verified", medium
        assert claim["reason"].startswith(f"VERIFIED FROM ONE SOURCE (spot-check it): {medium} at ") and claim["evidence"]["supports"][0]["medium"] == medium
        assert ("may have a reason to exaggerate this claim" in claim["reason"]) == (risk == "high") and claim["source"] == url                # a risky source is flagged too
        assert reg.gate("Swoveralls won several awards for its design.")["verdict"] != "BLOCKED"


def test_a_source_is_judged_by_closeness_not_medium_so_a_far_echo_never_verifies_and_two_posts_by_one_account_are_one_voice(tmp_path):
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    url = "https://www.instagram.com/some_repost_account/p/zzz"
    done, _ = run(reg, Judge({"Probe Design Award": supports(QUOTE_A, close=False, risk="high", medium="social post")}), [url], {url: page(QUOTE_A)})
    claim = reg.state()["claims"][cid]
    assert claim["status"] == "unverified" and claim["evidence"]["supports"] == [] and done == [cid]                                 # an echo is not close to the fact: nothing verified
    assert VC.source_id("https://www.instagram.com/StartupCPG/p/abc?x=1") == "instagram.com/startupcpg" and VC.source_id("https://www.youtube.com/@channel/videos") == "youtube.com/@channel"
    assert VC.source_id("https://news.example.co.uk/a/b") == "example.co.uk"
    reg2 = seeded(tmp_path / "same")
    cid2 = claim_id(reg2, "Awards")
    posts = ["https://www.instagram.com/startupcpg/p/one", "https://www.instagram.com/startupcpg/p/two"]
    run(reg2, Judge({"Probe Design Award": supports(QUOTE_A, True, "low", "social post")}), posts, {u: page(QUOTE_A) for u in posts})
    c2 = reg2.state()["claims"][cid2]
    assert len(c2["evidence"]["supports"]) == 1 and c2["single_source"] is True                                                   # two posts by one account: one voice
    reg3 = seeded(tmp_path / "two-accounts")
    cid3 = claim_id(reg3, "Awards")
    accounts = ["https://www.instagram.com/startupcpg/p/one", "https://www.instagram.com/enthuse/p/two"]
    run(reg3, Judge({"Probe Design Award": supports(QUOTE_A, True, "low", "social post")}), accounts, {u: page(QUOTE_A) for u in accounts})
    assert reg3.state()["claims"][cid3]["single_source"] is False and "verified from 2 independent sources" in reg3.state()["claims"][cid3]["reason"]


def test_a_source_that_contradicts_blocks_it_and_so_do_sources_that_disagree_but_nothing_else_does(tmp_path):
    contra = "Swoveralls did not win any award in 2026, the awarding body's list shows."
    urls = ["https://a.example.com/a", "https://b.example.org/b"]
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    judge = Judge({"Probe Design Award": supports(QUOTE_A), "did not win": {"verdict": "contradicts", "quote": contra, "about_brand": True, "close_to_fact": True, "exaggeration_risk": "low", "medium": "press"}})
    done, report = run(reg, judge, urls, {urls[0]: page(QUOTE_A), urls[1]: page(contra)})
    claim = reg.state()["claims"][cid]
    assert done == [] and claim["status"] == "conflicted" and "sources disagree: 1 support it and 1 contradict it" in claim["reason"] and report["outcomes"][cid] == "conflicted"
    assert reg.gate("Swoveralls won several awards for its design.")["verdict"] == "BLOCKED"                                      # the gate keeps blocking a disputed claim
    reg2 = seeded(tmp_path / "only-contra")
    cid2 = claim_id(reg2, "Awards")
    run(reg2, Judge({"did not win": {"verdict": "contradicts", "quote": contra, "about_brand": True, "close_to_fact": False, "exaggeration_risk": "high", "medium": "social post"}}), [urls[1]], {urls[1]: page(contra)})
    assert reg2.state()["claims"][cid2]["status"] == "conflicted"                                                                  # a source that directly contradicts blocks it, whatever its medium


def test_a_quotation_not_in_the_page_and_a_page_that_never_names_the_brand_verify_nothing(tmp_path):
    reg = seeded(tmp_path)
    urls = ["https://a.example.com/a", "https://b.example.org/b", "https://c.example.net/c"]
    judge = Judge({"Probe Design Award": supports("An invented sentence that is not anywhere on the page at all.")})
    pages = {urls[0]: page(QUOTE_A), urls[1]: page("Another Company won the 2026 Probe Design Award.", brand="Another Company"), urls[2]: page(QUOTE_A)}
    done, report = run(reg, judge, urls, pages)
    cid = claim_id(reg, "Awards")
    assert done == [cid] and reg.state()["claims"][cid]["status"] == "unverified" and reg.state()["claims"][cid]["evidence"]["supports"] == []          # nothing usable was found: unverified, never verified
    assert len([c for c in judge.calls if c != VC.QUERY_SYSTEM[:30]]) == 2 and report["errors"] == 0                              # the other brand's page was never sent to the model


def test_the_owners_own_decision_is_never_checked_or_overridden_and_a_failed_query_call_falls_back_to_the_claims_words(tmp_path):
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    reg.set_claim_status(cid, "blocked", C.OWNER, AT, reason="the owner says no")
    done, report = run(reg, Judge(), ["https://a.example.com/a"], {})
    assert done == [] and any("the owner decided this claim himself" in n for n in report["notes"]) and VC.targets(reg, 10) and cid not in VC.targets(reg, 10)
    reg2 = seeded(tmp_path / "x")
    searched = []
    report2 = {"calls": 0, "errors": 0, "notes": []}
    VC.verify_claim(reg2, NAME, reg2.state()["claims"][claim_id(reg2, "Awards")], search=lambda q: searched.append(q) or [], get=lambda u: None, claude=Judge(fail_queries=True), now=AT, report=report2)
    assert searched and searched[0].startswith("Swoveralls Awards") and report2["errors"] == 1 and any("search queries could not be made" in n for n in report2["notes"])


def test_the_rule_is_enforced_where_the_ledger_is_read_so_an_event_written_any_other_way_cannot_verify_or_block_a_claim(tmp_path):
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    far = [ev("a.com", close=False)]
    close = [ev("body.org", QUOTE_B, close=True, risk="low", medium="interview")]
    two = [ev("a.com"), ev("b.org", QUOTE_B)]
    contra = [{"url": "https://c.net", "domain": "c.net", "quote": "x" * 30}]
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=far, contradicts=[], outcome="verified")                  # a source that is not close to the fact
    assert reg.state()["claims"][cid]["status"] == "blocked"
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=close, contradicts=contra, outcome="verified")           # a close source, but another source contradicts
    assert reg.state()["claims"][cid]["status"] == "blocked"
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=[ev("body.org", "short")], contradicts=[], outcome="verified")
    assert reg.state()["claims"][cid]["status"] == "blocked"                                                              # a quotation too short to be real
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=[{"url": "https://body.org/y", "domain": "body.org", "quote": QUOTE_B}], contradicts=[], outcome="verified")
    assert reg.state()["claims"][cid]["status"] == "blocked"                                                              # no closeness judgement: it does not count
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=[], contradicts=[{"url": "https://c.net", "domain": "c.net", "quote": "no"}], outcome="conflicted")
    assert reg.state()["claims"][cid]["status"] == "blocked" and reg.state()["claims"][cid]["evidence"]["contradicts"] == []          # a contradiction without a real quotation blocks nothing new
    reg.append("claim_evidence", C.TOOL, AT, id=cid, supports=far, contradicts=contra, outcome="unverified")           # the contradiction forbids lowering it
    assert reg.state()["claims"][cid]["status"] == "blocked"
    reg.record_evidence(cid, close, [], "verified", AT)
    claim = reg.state()["claims"][cid]
    assert claim["status"] == "verified" and claim["single_source"] is True and claim["reason"].startswith("VERIFIED FROM ONE SOURCE (spot-check it): interview at body.org") and "exaggerate" not in claim["reason"]
    reg.record_evidence(cid, [ev("body.org", QUOTE_B, risk="high", medium="social post")], [], "verified", AT)
    assert "may have a reason to exaggerate this claim" in reg.state()["claims"][cid]["reason"]
    reg.record_evidence(cid, two, contra, "conflicted", AT)                                                               # later sources disagree: it is disputed again, the owner decides
    assert reg.state()["claims"][cid]["status"] == "conflicted"
    reg.record_evidence(cid, two, [], True, AT)                                                                           # True still means verified
    assert reg.state()["claims"][cid]["status"] == "verified" and reg.state()["claims"][cid]["single_source"] is False
    reg.set_claim_status(cid, "blocked", C.OWNER, AT, reason="owner overrides")
    for outcome, sup, con in (("verified", close, []), ("conflicted", two, contra), ("unverified", [], [])):
        reg.record_evidence(cid, sup, con, outcome, AT)                                                                   # evidence never moves the owner's decision
    assert reg.state()["claims"][cid]["status"] == "blocked" and reg.state()["claims"][cid]["owner_decided"]
    with pytest.raises(C.CanonError):
        reg.record_evidence("claim-9999", two, [], "verified", AT)
    with pytest.raises(C.CanonError):
        reg.record_evidence(cid, two, [], "maybe", AT)
    only_blocked = seeded(tmp_path / "lowering")
    unver = next(c["id"] for c in only_blocked.state()["claims"].values() if c["status"] == "unverified")
    conf = next((c["id"] for c in only_blocked.state()["claims"].values() if c["status"] == "conflicted"), None)
    only_blocked.record_evidence(unver, [], [], "unverified", AT)
    assert only_blocked.state()["claims"][unver]["status"] == "unverified"
    if conf:
        only_blocked.record_evidence(conf, [], [], "unverified", AT)
        assert only_blocked.state()["claims"][conf]["status"] == "conflicted"                                              # a real conflict between ledger entries is never lowered by finding nothing


def test_the_targets_are_blocked_then_conflicted_then_unverified_without_owner_decided_or_already_checked_claims(tmp_path):
    reg = seeded(tmp_path)
    order = VC.targets(reg, 100)
    statuses = [reg.state()["claims"][i]["status"] for i in order]
    assert statuses == sorted(statuses, key=lambda s: {"blocked": 0, "conflicted": 1, "unverified": 2}[s]) and order
    first = order[0]
    reg.set_claim_status(first, "blocked", C.OWNER, AT)
    reg.record_evidence(order[1], [ev("a.com")], [], False, AT)
    now = VC.targets(reg, 100)
    assert first not in now and order[1] not in now and len(VC.targets(reg, 2)) == 2
    assert VC.domain_of("https://www.news.example.co.uk/a/b") == "example.co.uk" and VC.domain_of("http://sub.site.com:8080/x") == "site.com"


# ---- the foundation uses it, and a rejected section no longer fails the run -------------------------------------------------------------------------------------------------------------------------------
class Chain:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def json_task(self, system, user, max_tokens=None):
        self.prompts.append(user)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def fake_cfg(tmp_path):
    root = tmp_path / "skills"
    skills = {}
    for i, (section, path) in enumerate(FD.CHAIN):
        d = root / path
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {section}\n---\nMethod for {section}.\n", encoding="utf-8")
        skills[path] = {"n": i + 1, "verdict": "useful", "state": "ready", "auto_run": True}
    return {"repo": {"max_skill_chars": 60000}, "skills": skills, "runtime_files": []}, root


def test_the_foundation_checks_the_blocking_claims_first_and_proposes_the_section_without_a_correction_round(tmp_path):
    cfg, root = fake_cfg(tmp_path)
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    asked = []

    def verifier(ids):
        asked.append(ids)
        reg.record_evidence(cid, [ev("a.com"), ev("b.org", QUOTE_B)], [], True, AT)
        return [cid]
    claude = Chain([bad])
    rep = FD.run_chain(reg, cfg, root, claude, brand_text="Brand name: Swoveralls.", story_text="Story.", now=AT, only={"positioning"}, verify_connection=False, retry_wait=0, verifier=verifier)
    assert rep[0]["result"] == "proposed" and asked == [[cid]] and len(claude.prompts) == 1                           # verified first: no correction was needed
    reg2 = seeded(tmp_path / "b")
    claude2 = Chain([bad, bad])
    rep2 = FD.run_chain(reg2, cfg, root, claude2, brand_text="Brand name: Swoveralls.", story_text="Story.", now=AT, only={"positioning"}, verify_connection=False, retry_wait=0, verifier=lambda ids: [])
    assert rep2[0]["result"] == "rejected" and len(claude2.prompts) == 2                                              # nothing verified: the old path, one correction, then recorded as rejected


def cli_args(tmp_path, *extra):
    return cli.build_parser().parse_args(["canon", "--data-dir", str(tmp_path / "data"), "--brand", "swoveralls", "--skills-config", "x.yaml", *extra])


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    cfg, root = fake_cfg(tmp_path)
    monkeypatch.setattr(SK, "load_config", lambda p: cfg)
    monkeypatch.setattr(SK, "checkout_dir", lambda c, *a, **k: root)
    monkeypatch.setattr(SK, "verify", lambda *a, **k: {"ok": True, "problems": []})
    return cfg, root


def run_canon(tmp_path, claude, *extra, search=None, get=None):
    from contentintel import batch2_cli as B2
    args = cli_args(tmp_path, *extra)
    seeded(tmp_path)
    return B2.run_canon(args, cli.Store(args.data_dir), AT, clients=lambda: (claude, search, get))


def test_a_rejected_section_is_recorded_and_the_run_still_succeeds_and_only_a_run_where_nothing_worked_fails(tmp_path, capsys, trusted):
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    ok = {"text": "Swoveralls makes soft overalls for grown-ups.", "needs_input": []}
    code = run_canon(tmp_path, Chain([bad, bad, ok]), "--action", "foundation", "--steps", "positioning,language", "--max-steps", "2")
    out = capsys.readouterr().out
    assert code == 0 and "1 section(s) REJECTED by the gate and recorded (positioning)" in out and "does not fail for it" in out and "1 section(s) PROPOSED" in out
    nothing = run_canon(tmp_path / "n", Chain([RuntimeError("down")] * 4), "--action", "foundation", "--steps", "positioning,language", "--max-steps", "2")
    assert nothing == 1                                                                                               # no section was proposed or merely blocked: the run itself broke
    partial = run_canon(tmp_path / "p", Chain([ok, RuntimeError("down"), RuntimeError("down")]), "--action", "foundation", "--steps", "positioning,language", "--max-steps", "2")
    assert partial == 0 and "could not be run" in capsys.readouterr().out                                             # one section saved, one could not run: the saved one counts
    noclient = run_canon(tmp_path / "c", None, "--action", "foundation", "--steps", "positioning")
    assert noclient == 1


def test_the_verify_claims_action_checks_claims_through_the_clients_and_says_what_is_missing(tmp_path, capsys, trusted):
    judge = Judge({"Probe Design Award, the awarding": supports(QUOTE_A), "Trade press confirmed": supports(QUOTE_B)})
    urls = ["https://a.example.com/a", "https://b.example.org/b"]
    pages = {urls[0]: page(QUOTE_A), urls[1]: page(QUOTE_B)}
    code = run_canon(tmp_path, judge, "--action", "verify-claims", "--max-claims", "1", search=lambda q: [{"url": u, "title": ""} for u in urls], get=lambda u: pages.get(u))
    out = capsys.readouterr().out
    assert code == 0 and "1 claim(s) checked from public sources by the pool model: 1 became VERIFIED" in out and "The owner can still change any status" in out
    assert run_canon(tmp_path / "m", judge, "--action", "verify-claims") == 1 and "needs the AI client, Serper search and a page fetch" in capsys.readouterr().out


def test_a_sentence_about_another_company_is_never_support_even_when_the_quotation_is_in_the_page(tmp_path):
    reg = seeded(tmp_path)
    cid = claim_id(reg, "Awards")
    urls = ["https://one.example.com/a", "https://two.example.org/b"]
    elsewhere = {"verdict": "supports", "quote": QUOTE_A, "about_brand": False, "close_to_fact": True, "exaggeration_risk": "low", "medium": "press"}
    done, _ = run(reg, Judge({"Probe Design Award": elsewhere}), urls, {u: page(QUOTE_A) for u in urls})
    assert reg.state()["claims"][cid]["status"] == "unverified" and reg.state()["claims"][cid]["evidence"]["supports"] == [] and done == [cid]


def test_the_stage_command_exits_one_only_when_nothing_worked_and_zero_when_something_was_proposed(tmp_path, capsys, monkeypatch):
    from contentintel import batch2_cli as B2
    from contentintel import cli
    from contentintel import skills as SK2
    from test_stages import GOOD, Model, cfg_for, seeded as seeded_stage
    cfg, root = cfg_for(tmp_path)
    monkeypatch.setattr(SK2, "load_config", lambda p: cfg)
    monkeypatch.setattr(SK2, "checkout_dir", lambda c, *a, **k: root)
    monkeypatch.setattr(SK2, "verify", lambda *a, **k: {"ok": True, "problems": []})
    args = cli.build_parser().parse_args(["canon", "--data-dir", str(tmp_path / "data"), "--brand", "swoveralls", "--skills-config", "x.yaml", "--action", "stage", "--stage", "seo_lens"])
    seeded_stage(tmp_path)
    store = cli.Store(args.data_dir)
    store.append_collected("swoveralls", {"caption": "People ask for soft overalls"})
    assert B2.run_canon(args, store, AT, clients=lambda: (Model([RuntimeError("down")] * 4), None, None)) == 1                  # nothing worked: the run itself broke
    capsys.readouterr()
    assert B2.run_canon(args, store, AT, clients=lambda: (Model([GOOD, RuntimeError("down"), RuntimeError("down")]), None, None)) == 0


def test_the_unverified_outcome_message_and_the_blocked_one_are_printed_for_the_owner(tmp_path, capsys, trusted):
    judge = Judge({"did not win": {"verdict": "contradicts", "quote": "Swoveralls did not win any award in 2026, the awarding body's list shows.", "about_brand": True, "source_kind": "official"}})
    url = "https://a.example.com/a"
    pages = {url: page("Swoveralls did not win any award in 2026, the awarding body's list shows.")}
    run_canon(tmp_path, judge, "--action", "verify-claims", "--max-claims", "1", search=lambda q: [{"url": url, "title": ""}], get=lambda u: pages.get(u))
    out = capsys.readouterr().out
    assert "0 became VERIFIED" in out and "1 blocked because sources contradict" in out
    run_canon(tmp_path / "second", Judge(), "--action", "verify-claims", "--max-claims", "1", search=lambda q: [], get=lambda u: None)
    assert "1 with no source close to the fact left UNVERIFIED for you to decide (a blocked one is no longer blocked)" in capsys.readouterr().out


def test_a_source_the_reviewer_gave_no_closeness_for_does_not_count_and_a_missing_risk_is_treated_as_high(tmp_path):
    url = "https://one.example.com/a"
    for n, verdict in enumerate(({"verdict": "supports", "quote": QUOTE_A, "about_brand": True}, {"verdict": "supports", "quote": QUOTE_A, "about_brand": True, "close_to_fact": "yes"})):
        r = seeded(tmp_path / str(n))
        c = claim_id(r, "Awards")
        run(r, Judge({"Probe Design Award": verdict}), [url], {url: page(QUOTE_A)})
        assert r.state()["claims"][c]["status"] == "unverified" and r.state()["claims"][c]["evidence"]["supports"] == []            # only an explicit true counts
    r = seeded(tmp_path / "risk")
    c = claim_id(r, "Awards")
    run(r, Judge({"Probe Design Award": {"verdict": "supports", "quote": QUOTE_A, "about_brand": True, "close_to_fact": True, "medium": "carrier pigeon"}}), [url], {url: page(QUOTE_A)})
    claim = r.state()["claims"][c]
    assert claim["status"] == "verified" and claim["evidence"]["supports"][0]["risk"] == "high" and claim["evidence"]["supports"][0]["medium"] == "other" and "exaggerate" in claim["reason"]


def test_the_verify_claims_log_shows_every_checked_claim_with_its_verdict_queries_and_sources(tmp_path, capsys, trusted):
    contra = "Swoveralls did not win any award in 2026, the awarding body's list shows."
    judge = Judge({"Probe Design Award, the awarding": supports(QUOTE_A, True, "high", "social post"), "did not win": {"verdict": "contradicts", "quote": contra, "about_brand": True, "close_to_fact": True, "exaggeration_risk": "low", "medium": "press"}},
                  queries=["Swoveralls award 2026", "Swoveralls Probe Design Award"])
    urls = ["https://www.instagram.com/startupcpg/p/abc", "https://news.example.com/b", "https://unread.example.org/c"]
    pages = {urls[0]: page(QUOTE_A), urls[1]: page(contra)}
    run_canon(tmp_path, judge, "--action", "verify-claims", "--max-claims", "3", search=lambda q: [{"url": u, "title": ""} for u in urls], get=lambda u: pages.get(u))
    out = capsys.readouterr().out
    claim_lines = [line for line in out.splitlines() if line.startswith("canon:   claim ")]
    assert len(claim_lines) == 3 and all(" -> " in line for line in claim_lines)                                                  # one block per claim checked, not only some
    assert "searched: Swoveralls award 2026 | Swoveralls Probe Design Award  (3 page(s) came back, 2 read)" in out
    assert "supports: https://www.instagram.com/startupcpg/p/abc (social post, close to the fact, exaggeration risk high)" in out and "CONTRADICTS: https://news.example.com/b (press)" in out and "-> CONFLICTED" in out
    quiet = run_canon(tmp_path / "q", Judge(), "--action", "verify-claims", "--max-claims", "1", search=lambda q: [], get=lambda u: None)
    assert quiet == 0 and "no usable source was found" in capsys.readouterr().out


def test_the_call_budget_and_the_claim_count_grow_with_the_work_waiting_so_no_claim_is_left_unchecked_for_a_small_default(tmp_path, capsys, trusted):
    assert VC.auto_budget(0) == VC.CALL_FLOOR == 10 and VC.auto_budget(6) == 40 and VC.auto_budget(18) == 100 and VC.auto_budget(10_000) == VC.CALL_CEILING
    reg = seeded(tmp_path)
    waiting = len(VC.targets(reg, 1000))
    assert waiting > 6                                                                                                       # more claims wait than the old fixed default of six
    url = "https://a.example.com/a"
    code = run_canon(tmp_path, Judge({"Probe Design Award": supports(QUOTE_A)}), "--action", "verify-claims", search=lambda q: [{"url": url, "title": ""}], get=lambda u: page(QUOTE_A))
    out = capsys.readouterr().out
    assert VC.MAX_PER_RUN == 60 and waiting > 6 and code == 0 and f"{min(waiting, 60)} claim(s) checked from public sources" in out                   # every waiting claim, not six and "the call limit was reached" not in out
    capped = run_canon(tmp_path / "small", Judge({"Probe Design Award": supports(QUOTE_A)}), "--action", "verify-claims", "--max-claims", "2", "--verify-calls", "2", search=lambda q: [{"url": url, "title": ""}], get=lambda u: page(QUOTE_A))
    assert capped == 0 and "the call limit was reached" in capsys.readouterr().out                                           # an explicit small budget is still honoured
