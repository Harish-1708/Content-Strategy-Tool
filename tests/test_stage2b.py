"""Update 70 round 3: the fixes for the stage 2 review (M201-M233) and the live findings F1-F5."""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from contentintel import batch2_cli as B2
from contentintel import canon as C
from contentintel import cli
from contentintel import foundation as FD
from contentintel import skills as SK
from contentintel import story_refresh as SR
from contentintel.store import Store

from test_stage2 import AT, NAME, PAGE, Claude, fake_cfg, reg, search_for, seeded

KIDS = {"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}


def refresh(r, claude, urls=("https://news.example/a",), pages=None, **kw):
    return SR.run(r, NAME, [], search=search_for(list(urls)), get=(lambda u: (pages or {}).get(u, PAGE)), claude=claude, now=AT, max_queries=1, **kw)


# ---- F3 / M203 / M212: a conflict means two statements that cannot both be true ---------------------------------------------------------------------------------

def test_two_facts_with_numbers_about_different_things_are_not_a_conflict(tmp_path):
    r = reg(tmp_path)
    r.add_claim("Free US returns and exchanges for 365 days", "figure", "unverified", "https://x.example", "read: \"free US returns and exchanges for 365 days\"", C.TOOL, AT)
    fact = {"fact": "Swoveralls has 4.75 out of 5.0 based on 1708 total reviews.", "quote": "Swoveralls is rated 4.75 out of 5.0 based on 1708 total reviews from customers.", "category": "figure"}
    page = PAGE.replace("The brand also launched a kids line.", "Swoveralls is rated 4.75 out of 5.0 based on 1708 total reviews from customers.")
    rep = refresh(r, Claude([fact], relation="unrelated"), pages={"https://news.example/a": page})
    assert rep["conflicts"] == 0 and rep["added"] == 1                                                    # the old detector called these a conflict because both contain numbers
    assert not any(c["status"] == "conflicted" for c in r.state()["claims"].values())


def test_a_non_numeric_disagreement_is_found_when_the_reviewer_says_so_and_it_is_linked_to_every_claim_it_contradicts(tmp_path):
    r = reg(tmp_path)
    a = r.add_claim("Swoveralls was founded in Portland.", "origin", "unverified", "https://a.example", "", C.TOOL, AT)
    b = r.add_claim("Swoveralls was founded by Kyle Bergman in Portland.", "origin", "unverified", "https://b.example", "", C.TOOL, AT)
    fact = {"fact": "Swoveralls was founded in Seattle.", "quote": "Swoveralls was founded in Seattle by Kyle Bergman, said the report on the company.", "category": "origin"}
    page = PAGE.replace("The brand also launched a kids line.", "Swoveralls was founded in Seattle by Kyle Bergman, said the report on the company.")
    rep = refresh(r, Claude([fact], relation="contradicts"), pages={"https://news.example/a": page})
    new = next(c for c in r.state()["claims"].values() if c["status"] == "conflicted")
    assert rep["conflicts"] == 1 and sorted(new["conflicts_with"]) == sorted([a, b])                      # a fact that contradicts two claims is linked to both (M212)
    assert a in r.state()["claims"][b]["conflicts_with"] or new["id"] in r.state()["claims"][a]["conflicts_with"]


def test_without_a_reviewer_no_conflict_is_called_and_a_failing_comparison_is_named(tmp_path):
    r = reg(tmp_path)
    r.add_claim("First-year sales about $65,000", "figure", "unverified", "x", "", C.TOOL, AT)
    assert SR.compare(r, "Swoveralls sold $230,000 in its first year.", "figure", name=NAME, claude=None) == ("new", [])
    report = {"calls": 0, "errors": 0, "notes": []}

    class Broken:
        def json_task(self, *a, **k):
            raise TimeoutError("slow")
    assert SR.compare(r, "Swoveralls sold $230,000 in its first year.", "figure", name=NAME, claude=Broken(), report=report) == ("new", [])
    assert report["errors"] == 1 and "treated as unrelated" in report["notes"][0]


def test_the_commands_take_back_a_conflict_the_tool_made_by_mistake_and_never_touch_a_decided_one(tmp_path, capsys):
    r = reg(tmp_path)
    old = r.add_claim("Free returns for 365 days", "figure", "unverified", "https://a.example", "", C.TOOL, AT)
    mistaken = r.add_conflict("Rated 4.75 out of 5.0 on 1708 reviews", "figure", "https://b.example", "read", old, AT)
    real = r.add_conflict("Returns are free for 30 days", "figure", "https://c.example", "read", old, AT)
    decided = r.add_conflict("Another disputed figure", "figure", "https://d.example", "read", old, AT)
    r.set_claim_status(decided, "conflicted", C.OWNER, AT)                                                # the owner looked at it and decided: it is theirs
    seen = []

    class Judge:
        def json_task(self, system, user, max_tokens=None):
            seen.append(user)
            return {"relation": "contradicts" if "30 days" in user else "unrelated"}
    args = cli.build_parser().parse_args(["canon", "--action", "reclassify-conflicts", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")])
    r2 = C.Registry(tmp_path / "data", "swoveralls")
    assert r2.state()["claims"][mistaken]["status"] == "conflicted"
    code = B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (Judge(), None, None))
    state = C.Registry(tmp_path / "data", "swoveralls").state()["claims"]
    out = capsys.readouterr().out
    assert code == 0 and "1 kept as real conflicts, 1 taken back to UNVERIFIED" in out and "nothing deleted" in out
    assert state[mistaken]["status"] == "unverified" and "not a conflict" in state[mistaken]["reason"] and state[real]["status"] == "conflicted"
    assert state[decided]["decided_at"] and state[old]["status"] == "unverified" and len(seen) == 2          # the owner's decided claim was not even re-asked
    try:
        r2.reconsider_conflict(decided, AT, "x")
        raised = False
    except C.CanonError:
        raised = True
    assert raised
    assert cli.main(["canon", "--action", "reclassify-conflicts", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]) == 1        # no AI client without a key
    assert "needs the AI client" in capsys.readouterr().out


# ---- M202: the gate note and the needs reach the Canon tab ---------------------------------------------------------------------------------------------------------

def test_the_canon_tab_carries_the_gate_note_and_the_structured_needs(tmp_path):
    r = reg(tmp_path)
    cid = r.propose_canon("positioning", "A text.", "skill", "aa" * 32, AT, note="gate: OK; needs input: competitor URLs", needs=["competitor URLs", "archived snapshots"], claims_used=["a verified claim"])
    assert C.CANON_COLUMNS[-1] == "gate_note" and "gate_note" not in C.CLAIM_COLUMNS
    tab = C.sheet_tabs(tmp_path / "data", "swoveralls")[0]
    assert tab["rows_by_key"][cid]["gate_note"] == "gate: OK; needs input: competitor URLs"
    assert r.canon_rows()[0][-2] == "Gate note" and r.canon_rows()[1][-2] == "gate: OK; needs input: competitor URLs" and r.canon_rows()[1][-1] == ""
    state = r.state()["canon"][cid]
    assert state["needs"] == ["competitor URLs", "archived snapshots"] and state["claims_used"] == ["a verified claim"]


# ---- F5 / M206 / M207 / M210 / M211 / M213 / M223 ------------------------------------------------------------------------------------------------------------------

def test_the_printed_dropped_lines_match_the_count_and_a_refresh_where_nothing_worked_fails(tmp_path, capsys):
    many = [{"fact": f"Fact {i}.", "quote": "", "category": "other"} for i in range(12)]
    r = reg(tmp_path)
    args = cli.build_parser().parse_args(["canon", "--action", "refresh", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")])
    code = B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (Claude(many), search_for(["https://news.example/a"]), lambda u: PAGE))
    out = capsys.readouterr().out
    import re
    counted = int(re.search(r"(\d+) dropped \(", out).group(1))
    assert code == 0 and counted >= 10 and out.count("canon:   dropped: ") == counted                       # every dropped fact is printed (up to 20): the count and the lines agree
    big = [{"fact": f"Fact {i}.", "quote": "", "category": "other"} for i in range(10)]
    B2.run_canon(args, Store(tmp_path / "x"), AT, clients=lambda: (Claude(big), search_for([f"https://news.example/p{i}" for i in range(3)]), lambda u: PAGE))
    out_big = capsys.readouterr().out
    assert "30 dropped (" in out_big and out_big.count("canon:   dropped: ") == 21 and "canon:   dropped: ... and 10 more" in out_big        # 20 printed, then how many more
    assert r is not None

    class Down:
        def json_task(self, *a, **k):
            raise RuntimeError("provider down")
    code2 = B2.run_canon(args, Store(tmp_path / "y"), AT, clients=lambda: (Down(), search_for(["https://news.example/a"]), lambda u: PAGE))
    out2 = capsys.readouterr().out
    assert code2 == 1 and "the refresh FAILED" in out2 and "RuntimeError: provider down" in out2                # nothing worked: a failed run, with the cause
    code3 = B2.run_canon(args, Store(tmp_path / "z"), AT, clients=lambda: (Claude([]), search_for([]), lambda u: PAGE))
    assert code3 == 0                                                                                        # nothing found is not a failure


def test_linkedin_and_threads_are_not_story_bank_sources():
    for host in ("linkedin.com", "threads.net", "bsky.app", "t.me"):
        assert host in SR.SKIP_HOSTS


def test_after_the_new_entry_limit_no_more_pages_are_fetched_or_reviewed_and_the_unfinished_page_is_read_again(tmp_path):
    r = reg(tmp_path)
    fetched = []

    def get(u):
        fetched.append(u)
        return PAGE
    facts = [{"fact": f"Swoveralls {w} its overalls.", "quote": KIDS["quote"], "category": "other"} for w in ("dyes", "stitches", "ships")]
    claude = Claude(facts)
    rep = SR.run(r, NAME, [], search=search_for(["https://news.example/a", "https://news.example/b", "https://news.example/c"]), get=get, claude=claude, now=AT, max_queries=1, max_new=1)
    assert fetched == ["https://news.example/a"] and rep["added"] == 1 and rep["calls"] == 2                  # one page: later pages cost nothing
    assert r.refresh_seen() == set()                                                                        # the unfinished page is read again next time
    r2 = reg(tmp_path / "e")

    class Boom(Claude):
        def json_task(self, system, user, max_tokens=None):
            if "extract" in system.lower() and "news.example/b" in user:
                raise KeyboardInterrupt("the run is stopped half-way through the second page")
            return super().json_task(system, user, max_tokens)
    raised = False
    try:
        SR.run(r2, NAME, [], search=search_for(["https://news.example/a", "https://news.example/b"]), get=lambda u: PAGE, claude=Boom([KIDS]), now=AT, max_queries=1)
    except KeyboardInterrupt:
        raised = True
    assert raised and r2.refresh_seen() == {"https://news.example/a"}                                       # the page handled before is remembered; the one that was cut off is read again


def test_a_long_exact_sentence_is_stored_whole_and_a_long_fact_is_cut_at_a_word(tmp_path):
    long_quote = ("Swoveralls " + "makes overalls for adults who want to be comfortable all day long and more. " * 8).strip()
    page = PAGE.replace("The brand also launched a kids line.", long_quote)
    r = reg(tmp_path)
    SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: page, claude=Claude([{"fact": long_quote, "quote": long_quote, "category": "other"}]), now=AT, max_queries=1)
    claim = next(iter(r.state()["claims"].values()))
    assert len(long_quote) > 300 and long_quote in claim["reason"] and claim["text"].endswith(" ...") and len(claim["text"]) <= 404          # the sentence it rests on is kept whole


def test_the_refresh_command_has_a_max_new_flag_and_names_exactly_what_is_missing(tmp_path, capsys):
    args = cli.build_parser().parse_args(["canon", "--action", "refresh", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data"), "--max-new", "1", "--max-calls", "4"])
    assert args.max_new == 1 and args.max_calls == 4
    B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (Claude([]), search_for([]), None))
    assert "this run has no page fetch, so nothing was refreshed" in capsys.readouterr().out
    B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (None, None, lambda u: PAGE))
    assert "no AI client (ANTHROPIC_API_KEY) and no Serper search (SERPER_API_KEY)" in capsys.readouterr().out


# ---- F1 / F2 / M208 / M209 / M214-M222 / M233: the foundation chain ----------------------------------------------------------------------------------------

def run_chain(r, claude, tmp_path, **kw):
    cfg, root = fake_cfg(tmp_path)
    kw.setdefault("brand_text", "Brand name: Swoveralls.")
    kw.setdefault("story_text", "Story bank text.")
    return FD.run_chain(r, cfg, root, claude, now=AT, verify_connection=False, retry_wait=0, **kw)


class Flaky:
    def __init__(self, answers, calls=None):
        self.answers, self.calls = list(answers), calls if calls is not None else []

    def json_task(self, system, user, max_tokens=None):
        self.calls.append("gateway")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            self.calls.append("claude")
            raise a
        return a


def test_a_failed_model_call_is_asked_once_more_and_the_failure_says_what_failed_and_which_route_was_tried(tmp_path):
    r = reg(tmp_path)
    ok = {"text": "A plain sentence about overalls.", "needs_input": []}
    flaky = Flaky([RuntimeError("HTTP 429: rate limited, key=sk-ant-abcdefghijklmnopqrstuv"), ok])
    rep = run_chain(r, flaky, tmp_path, only={"positioning"})
    assert rep[0]["result"] == "proposed"                                                                   # the second try answered (F2: a transient failure is not the end)
    down = Flaky([TimeoutError("read timed out"), TimeoutError("read timed out again")])
    rep2 = run_chain(reg(tmp_path / "d"), down, tmp_path, only={"positioning"})
    detail = rep2[0]["detail"]
    assert rep2[0]["result"] == "failed" and "TimeoutError: read timed out again" in detail and "attempt 2 of 2" in detail and "route tried: gateway, claude" in detail      # F1
    secret = Flaky([RuntimeError("bad key=sk-ant-abcdefghijklmnopqrstuv"), RuntimeError("bad key=sk-ant-abcdefghijklmnopqrstuv")])
    assert "sk-ant-abcdefghijklmnopqrstuv" not in run_chain(reg(tmp_path / "s"), secret, tmp_path, only={"positioning"})[0]["detail"]      # a key never reaches the log


def test_the_call_limit_counts_every_try_and_a_step_limit_of_zero_means_no_limit(tmp_path):
    ok = {"text": "A plain sentence about overalls.", "needs_input": []}
    r = reg(tmp_path)
    rep = run_chain(r, Flaky([ok, ok, ok]), tmp_path, only={"positioning", "language", "story_bank"}, max_calls=2)
    assert [x["result"] for x in rep] == ["proposed", "proposed", "skipped"] and "limit of 2 model call(s)" in rep[2]["detail"]
    retried = run_chain(reg(tmp_path / "r"), Flaky([RuntimeError("x"), ok, ok]), tmp_path, only={"positioning", "language"}, max_calls=2)
    assert [x["result"] for x in retried] == ["proposed", "skipped"]                                         # the retry used the second call
    every = run_chain(reg(tmp_path / "e"), Flaky([ok] * 9), tmp_path, max_steps=0)
    assert sum(1 for x in every if x["result"] == "proposed") == 9                                          # 0 is not "skip everything"


def test_unknown_steps_no_client_no_material_and_an_untrusted_connection_are_refused_in_words(tmp_path, capsys):
    r = reg(tmp_path)
    rep = run_chain(r, Flaky([]), tmp_path, only={"positionning"})
    assert rep[0]["result"] == "refused" and "'positionning' is not a section of the chain; the sections are: positioning" in rep[0]["detail"]
    assert {x["result"] for x in run_chain(r, None, tmp_path)} == {"refused"} and "no AI client" in run_chain(r, None, tmp_path)[0]["detail"]
    empty = run_chain(r, Flaky([]), tmp_path, brand_text="", story_text=" ")
    assert {x["result"] for x in empty} == {"needs_input"} and "would invent it" in empty[0]["detail"]
    cfg, root = fake_cfg(tmp_path / "u")
    untrusted = FD.run_chain(r, cfg, root, Flaky([]), brand_text="x", story_text="y", now=AT)               # the DEFAULT checks the connection itself (not a git checkout here)
    assert {x["result"] for x in untrusted} == {"refused"} and "the skills connection cannot be trusted" in untrusted[0]["detail"]
    args = cli.build_parser().parse_args(["canon", "--action", "foundation", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data"), "--steps", "positionning"])
    cfg_ok = SK.load_config("external_skills.yaml")
    assert cfg_ok and args.steps == "positionning"


def test_a_new_proposal_replaces_the_tools_own_unapproved_one_and_never_an_approved_one(tmp_path):
    r = reg(tmp_path)
    ok = {"text": "First version.", "needs_input": []}
    run_chain(r, Flaky([ok]), tmp_path, only={"positioning"})
    run_chain(r, Flaky([{"text": "Second version.", "needs_input": []}]), tmp_path, only={"positioning"}, redo=True)
    state = r.state()["canon"]
    assert [e["status"] for e in state.values()] == ["superseded", "proposed"] and state["canon-0002"]["text"] == "Second version."
    r.decide_canon("canon-0002", "approved", C.OWNER, AT)
    run_chain(r, Flaky([{"text": "Third version.", "needs_input": []}]), tmp_path, only={"positioning"}, redo=True)
    state = r.state()["canon"]
    assert state["canon-0002"]["status"] == "approved" and state["canon-0003"]["status"] == "proposed" and r.canonical() == {"positioning": "Second version."}      # approved text changes only by approval


def test_truncation_needs_and_used_claims_are_in_the_note_and_the_proposal(tmp_path):
    r = seeded(tmp_path)
    r.set_claim_status(next(i for i, c in r.state()["claims"].items() if c["text"] == "Awards"), "verified", C.OWNER, AT, source="https://proof.example")
    long_story = "Swoveralls makes overalls. " * 1200
    answer = {"text": "x" * 9000, "needs_input": ["a", "b", "c", "d"], "claims_used": ["Awards", "an invented award"]}
    rep = run_chain(r, Flaky([answer]), tmp_path, only={"positioning"}, story_text=long_story)
    note = rep[0]["detail"]
    assert "story bank were cut to fit" in note and "the text was cut at 8000 characters" in note and "(and 1 more)" in note and "relied on things it was not given: an invented award" in note
    entry = next(iter(r.state()["canon"].values()))
    assert entry["needs"] == ["a", "b", "c", "d"] and entry["claims_used"] == ["Awards", "an invented award"] and len(entry["text"]) == 8000


def test_the_prompt_separates_do_not_state_from_unconfirmed_and_status_counts_foundation_outcomes(tmp_path, capsys):
    r = seeded(tmp_path)
    prompt = FD.ledger_summary(r)
    assert prompt.index("DO NOT STATE") < prompt.index("UNCONFIRMED") < prompt.index("MAY STATE")
    blocked = next(c["text"] for c in r.state()["claims"].values() if c["status"] == "blocked")
    unverified = next(c["text"] for c in r.state()["claims"].values() if c["status"] == "unverified")
    assert blocked in prompt.split("UNCONFIRMED")[0] and unverified in prompt.split("UNCONFIRMED")[1].split("MAY STATE")[0] and "never state them as fact" in prompt
    run_chain(r, Flaky([{"text": "", "needs_input": ["x"]}]), tmp_path, only={"positioning"})
    assert cli.main(["canon", "--action", "status", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]) == 0
    assert "foundation: 1 step(s) waiting for input, 0 refused by the gate" in capsys.readouterr().out


# ---- the log (M205), seeds (M159), filter (M190), anchors (M227), commands (M225, M226, M228) ----------------------------------------------------------

def test_the_log_chain_covers_the_first_line_and_an_old_log_is_sealed_so_its_lines_are_covered_too(tmp_path):
    r = reg(tmp_path)
    for i in range(3):
        r.add_claim(f"Claim number {i} about awards", "award", "blocked", "", "", C.TOOL, AT)
    lines = r.path.read_text(encoding="utf-8").splitlines()
    r.path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")                                         # the FIRST line is deleted
    assert any("not the start of the chain" in p for p in reg(tmp_path).chain_problems)
    legacy = C.Registry(tmp_path / "old", "swoveralls")
    legacy.path.parent.mkdir(parents=True)
    old_lines = [json.dumps({"n": i + 1, "at": AT, "kind": "claim_added", "actor": "tool", "id": f"cl-{i}", "text": f"Old claim {i}", "category": "award", "status": "blocked"}) for i in range(5)]
    legacy.path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    C.Registry(tmp_path / "old", "swoveralls").add_claim("A new claim about press", "press", "blocked", "", "", C.TOOL, AT)
    sealed = legacy.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(sealed[5])["kind"] == "seal" and json.loads(sealed[5])["upto"] == 5 and C.Registry(tmp_path / "old", "swoveralls").chain_problems == []
    sealed[1] = sealed[1].replace("Old claim 1", "Old claim ONE")                                           # an edit in the OLD part is now caught
    legacy.path.write_text("\n".join(sealed) + "\n", encoding="utf-8")
    assert any("before the seal were edited" in p for p in C.Registry(tmp_path / "old", "swoveralls").chain_problems)


def test_a_seed_that_differs_from_the_ledger_is_counted_and_never_changes_it(tmp_path, capsys):
    r = reg(tmp_path)
    cid = r.add_claim("Awards", "award", "blocked", "story bank", "", C.TOOL, AT)
    r.set_claim_status(cid, "unverified", C.TOOL, AT)
    added, there, differs = C.apply_seed(r, [{"text": "Awards", "category": "award", "status": "blocked", "reason": "r"}, {"text": "New one", "category": "press", "status": "blocked", "reason": "r"}], AT)
    assert (added, there, differs) == (1, 1, 1) and r.state()["claims"][cid]["status"] == "unverified"


def test_the_medical_filter_also_catches_a_family_members_condition_and_keeps_fit_information():
    for line in ("- Mia: \"My son has autism and these are the only overalls without scratchy seams.\"", "- Joe: \"My wife was diagnosed with lupus and finds the fabric gentle.\"", "- Al: \"My dad had a stroke and these zip easily for him.\"", "- Sam: \"Her daughter has epilepsy and loves the soft fabric.\"", "- Ana: \"His mother was diagnosed with lupus last year.\""):
        assert C.is_medical_disclosure(line), line
    for line in ("- Pregnant buyer: \"I'm pretty much living in these while pregnant! I got a size up.\"", "- Tall buyer: \"I'm 6'4 and 300lbs and the XXL fits well.\"", "- Ed: \"Great for lounging, soft fabric.\""):
        assert not C.is_medical_disclosure(line), line                                                      # fit information stays (the owner's decision)


def test_generic_words_no_longer_anchor_a_claim_and_the_commands_check_brands_and_the_repository_root(tmp_path, capsys, monkeypatch):
    gate = C.load_gate("claims_gate.yaml")
    for word in ("above", "list", "page"):
        assert word in gate["generic_words"]
    assert cli.main(["canon", "--action", "status", "--brand", "nosuchbrand", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]) == 1
    assert "no brand folder named nosuchbrand in brands (the brands are: defi_snacks, duderobe, swoveralls)" in capsys.readouterr().out
    from test_batch2_skills import git
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    registry = C.Registry(repo / "data", "swoveralls")
    registry.add_claim("A claim about awards", "award", "blocked", "", "", C.TOOL, AT)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "registry")
    monkeypatch.chdir(repo / "sub")                                                                           # not the repository root: the history is still found
    assert cli.main(["canon", "--action", "history", "--brand", "swoveralls", "--brands-dir", str(Path(__file__).resolve().parent.parent / "brands"), "--data-dir", str(repo / "data")]) == 0
    out = capsys.readouterr().out
    assert "swoveralls: 1 commit(s) touched the registry" in out and "outside this repository" not in out


def test_verify_says_which_of_the_two_reads_failed(tmp_path):
    from test_batch2_skills import config, git, make_repo
    root = make_repo(tmp_path)
    cfg = config(root)
    git(root, "remote", "remove", "origin")
    problems = " ".join(SK.verify(cfg, root)["problems"])
    assert "origin could not be read" in problems and "no origin remote" in problems and "uncommitted changes could not be read" not in problems          # only the origin read failed


def test_waiting_skills_carry_a_second_lock_and_the_status_counts_them_once(tmp_path, capsys, monkeypatch):
    cfg = SK.load_config("external_skills.yaml")
    waiting = [p for p, s in cfg["skills"].items() if s["state"] == SK.WAITING]
    assert len(waiting) == 15 and all(cfg["skills"][p]["auto_run"] is False for p in waiting)
    not_useful = [p for p, s in cfg["skills"].items() if s["verdict"] == "not_useful"]
    assert len(not_useful) == 21 and sum(1 for p, s in cfg["skills"].items() if not s["auto_run"] and s["state"] != SK.WAITING) >= 21
    from contentintel import collectors as COL
    from contentintel import guards
    monkeypatch.setattr(guards, "on", lambda name: False)
    assert COL.active_cooldowns({"instagram": {"until": "2026-11-19T00:00:00Z", "reason": "rested by hand"}}, "2026-11-17T08:00:00Z") == {}                       # a reason with no time written: not taken as the tool's
    assert "instagram" in COL.active_cooldowns({"instagram": {"until": "2026-11-19T00:00:00Z", "reason": "rested by hand", "at": "2026-11-15T08:00:00Z"}}, "2026-11-17T08:00:00Z")    # Collect 62's rest: both


# ---- the workflows (M201, M183, M185) ----------------------------------------------------------------------------------------------------------------------------------

def test_the_brief_workflow_refreshes_the_story_bank_first_and_a_failure_never_stops_the_brief():
    wf = yaml.safe_load(Path(".github/workflows/brief.yml").read_text(encoding="utf-8"))
    assert wf[True]["workflow_dispatch"]["inputs"]["refresh_story_bank"]["default"] is True
    steps = wf["jobs"]["brief"]["steps"]
    names = [s.get("name") for s in steps]
    assert names.index("Refresh the story bank first") < names.index("Write the brief")
    step = steps[names.index("Refresh the story bank first")]
    assert step["continue-on-error"] is True and step["if"] == "inputs.refresh_story_bank" and "python -m contentintel canon --action refresh" in step["run"]
    assert step["env"]["SERPER_API_KEY"] == "${{ secrets.SERPER_API_KEY }}" and step["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"


def run_step(tmp_path, action, brand="swoveralls", extra=None):
    wf = yaml.safe_load(Path(".github/workflows/skills.yml").read_text(encoding="utf-8"))
    script = next(s["run"] for s in wf["jobs"]["skills"]["steps"] if s.get("name") == "Run it")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    for name in ("external_skills.yaml", "brands", "claims_gate.yaml"):
        link = work / name
        if not link.exists():
            link.symlink_to(Path(name).resolve(), target_is_directory=Path(name).is_dir())
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "SERPER_API_KEY")}
    env.update({"PYTHONPATH": os.getcwd(), "ACTION": action, "BRAND": brand, "TEXT": "", "STEPS": "", "MAX_STEPS": "3", "MAX_CALLS": "6", "AARON_SKILLS_DIR": str(tmp_path / "no-checkout"), **(extra or {})})
    return subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True, text=True)


def test_the_refresh_foundation_and_reclassify_branches_of_the_workflow_step_run_for_real(tmp_path):
    refresh = run_step(tmp_path, "refresh-story-bank")
    assert refresh.returncode == 1 and "canon: swoveralls: the refresh needs the AI client, Serper search and a page fetch" in refresh.stdout, (refresh.stdout, refresh.stderr)
    foundation = run_step(tmp_path, "foundation")
    assert foundation.returncode == 1 and "canon: PROBLEM: the skills repository is not checked out" in foundation.stdout and "the foundation chain does not run" in foundation.stdout, (foundation.stdout, foundation.stderr)
    reclassify = run_step(tmp_path, "reclassify-conflicts")
    assert reclassify.returncode == 1 and "re-checking conflicts needs the AI client" in reclassify.stdout, (reclassify.stdout, reclassify.stderr)


def test_the_skills_subcommand_runs_in_both_command_forms_against_a_real_checkout(tmp_path):
    from test_batch2_skills import config, make_repo
    root = make_repo(tmp_path)
    cfg = config(root)
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({"repo": cfg["repo"], "runtime_files": cfg["runtime_files"], "skills": cfg["skills"]}), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    for form in (["-m", "contentintel"], ["-m", "contentintel.cli"]):
        done = subprocess.run([sys.executable, *form, "skills", "--action", "verify", "--skills-config", str(policy), "--skills-dir", str(root), "--data-dir", str(tmp_path / "d")], capture_output=True, text=True, env=env)
        assert done.returncode == 0 and "skills: OK: 2 skills at " in done.stdout, (form, done.stdout, done.stderr)


def test_the_apify_lane_cannot_commit_more_than_its_run_spending_cap_however_many_calls_are_small(tmp_path):
    from contentintel import voice as VOICE
    from test_update65 import FakeApify, _ledger
    fake = FakeApify()
    lane = VOICE.ApifyVoice(fake, _ledger(), per_run=300, monthly=600, shared_cap=10000, normalize=lambda s, raw: (raw, 0), run_usd_cap=1.0)
    lane.run("sweatpant overalls", share=10)
    assert [c[0] for c in fake.calls] == ["clockworks/tiktok-scraper", "apify/instagram-hashtag-scraper"] and abs(lane.committed_usd - 0.75) < 1e-9
    _, notes = lane.run("bib overalls", share=10)
    assert [c[0] for c in fake.calls][2:] == ["apify/instagram-hashtag-scraper"]                              # TikTok's 0.50 would pass the 1.00 cap; Instagram's 0.25 just fits
    assert any("not asked (this run has already committed $0.75 of its $1.00 spending cap" in n for n in notes) and abs(lane.committed_usd - 1.0) < 1e-9
    _, notes3 = lane.run("loungewear", share=10)
    assert len(fake.calls) == 3 and sum("spending cap" in n for n in notes3) == 2                              # nothing more is asked once the cap is reached
    assert VOICE.ApifyVoice(fake, _ledger(), run_usd_cap=99).run_usd_cap == 10.0 and VOICE.ApifyVoice(fake, _ledger(), run_usd_cap=0).run_usd_cap == 0.25        # clamped
    big = VOICE.ApifyVoice(FakeApify(), _ledger(), per_run=300, monthly=600, normalize=lambda s, raw: (raw, 0))
    assert big.run_usd_cap == 3.0                                                                           # the default: about four full TikTok-plus-Instagram asks


def test_the_omniroute_research_is_recorded_without_wiring_anything_and_the_forbidden_providers_stay_forbidden():
    data = yaml.safe_load(Path("ai/omniroute_free_providers.yaml").read_text(encoding="utf-8"))
    recurring = {p["id"]: p for p in data["confirmed_recurring"]}
    assert len(recurring) == 16 and sum(1 for p in recurring.values() if p["new"]) == 8 and all(p["limit"] for p in recurring.values())
    forbidden = {p["id"] for p in data["do_not_wire"]}
    assert forbidden == {"kiro", "duckduckgo-ai-chat", "blackbox", "coze", "muse-spark-web"} and not forbidden & set(recurring)
    assert {p["id"] for p in data["excluded_not_free_now"]} >= {"cerebras", "together-ai", "github-models", "ollama-cloud", "hugging-face-inference"}
    assert data["retest_order"][0].startswith("the three big new pools") and any("failover" in step for step in data["retest_order"])
    assert "NOT a reliable free VIDEO route" in recurring["pollinations"]["caution"] and "NOT a free video go" in data["video"]["decision"]
    assert "disputed" in recurring["llm7"]["limit"].lower()
    providers = yaml.safe_load(Path("ai_providers.yaml").read_text(encoding="utf-8"))["providers"]
    for key in ("nara", "xkiro", "sambanova", "llm7", "siliconflow", "pollinations"):                       # already LISTED in the provider file, and still switched OFF with unverified terms and no approved model:
        assert providers[key]["enabled"] is False and providers[key]["terms"]["status"] == "unverified" and not any(m.get("approved") for m in providers[key].get("models", []))      # the owner switches them on
    assert "opencode-zen" not in providers
    assert Path("docs/ai/omniroute-free-providers.md").read_text(encoding="utf-8").count("Kiro, DuckDuckGo AI Chat, Blackbox, Coze, Muse Spark web") == 1


def test_a_claim_about_another_thing_is_never_sent_to_the_reviewer_so_no_ai_call_is_spent_on_it(tmp_path):
    r = reg(tmp_path)
    r.add_claim("Featured in Vogue", "press", "unverified", "https://a.example", "", C.TOOL, AT)
    r.add_claim("Founded by Kyle Bergman", "origin", "unverified", "https://b.example", "", C.TOOL, AT)
    asked = []

    class Judge:
        def json_task(self, system, user, max_tokens=None):
            asked.append(user)
            return {"relation": "contradicts"}
    assert SR.compare(r, "Swoveralls offers free returns on every pair.", "other", name=NAME, claude=Judge()) == ("new", []) and asked == []          # nothing is about returns: no call
    r.add_claim("Free returns within 30 days", "other", "unverified", "https://c.example", "", C.TOOL, AT)
    kind, ids = SR.compare(r, "Swoveralls offers free returns on every pair.", "other", name=NAME, claude=Judge())
    assert kind == "conflict" and len(ids) == 1 and len(asked) == 1 and "Free returns within 30 days" in asked[0]                                    # only the claim about returns is asked about


def test_a_conflict_that_could_not_be_re_checked_stays_a_conflict_for_the_owner(tmp_path, capsys):
    r = reg(tmp_path)
    old = r.add_claim("Free returns for 365 days", "figure", "unverified", "https://a.example", "", C.TOOL, AT)
    pending = r.add_conflict("Returns are free for 30 days", "figure", "https://c.example", "read", old, AT)

    class Down:
        def json_task(self, *a, **k):
            raise TimeoutError("busy")
    args = cli.build_parser().parse_args(["canon", "--action", "reclassify-conflicts", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")])
    code = B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (Down(), None, None))
    out = capsys.readouterr().out
    assert C.Registry(tmp_path / "data", "swoveralls").state()["claims"][pending]["status"] == "conflicted"        # no answer: the tool never decides for the owner
    assert "could not be re-checked (TimeoutError: busy): left as a conflict" in out and "1 kept as real conflicts, 0 taken back" in out and code == 0
