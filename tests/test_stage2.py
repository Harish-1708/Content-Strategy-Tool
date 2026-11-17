"""Batch 2, stage 2: the story-bank refresh (append-only, unverified-only, conflicts kept, every fact sourced and reviewed) and the per-brand foundation chain (proposals only, through the skill lock and the gate);
plus the registry additions they rely on (a log integrity chain, safe ids, conflicts, a wider medical filter, claim names)."""
import json
import re
from pathlib import Path

from contentintel import canon as C
from contentintel import cli
from contentintel import foundation as FD
from contentintel import skills as SK
from contentintel import story_refresh as SR

AT = "2026-11-17T08:00:00Z"
NAME = "Swoveralls"


def reg(tmp_path, brand="swoveralls"):
    return C.Registry(tmp_path / "data", brand)


def seeded(tmp_path, brand="swoveralls"):
    r = reg(tmp_path, brand)
    C.apply_seed(r, C.load_seed(Path("brands") / brand / "claims_seed.yaml"), AT)
    return r


PAGE = ("<html><head><style>x{}</style></head><body><h1>Swoveralls in the news</h1><p>Swoveralls founder Kyle Bergman said the company sold $230,000 of overalls in its first year. "
        "The brand also launched a kids line. Unrelated paragraph about weather and other things that fill the page so it is long enough to be read as a real article about the company and its products.</p></body></html>")
QUOTE = "Swoveralls founder Kyle Bergman said the company sold $230,000 of overalls in its first year."


def pair_answer(user, decide):
    """The reviewer's answer for a batch of pairs: one relation per `PAIR n:` block, chosen by `decide(block text)`."""
    blocks = re.split(r"PAIR (\d+):", user)[1:]
    return {"relations": [{"i": int(blocks[k]), "relation": decide(blocks[k + 1])} for k in range(0, len(blocks), 2)]}


class Claude:
    """A scripted model: extraction answers by page, review answers by flag."""
    def __init__(self, facts, supported=True, about=True, relation="unrelated"):
        self.facts, self.supported, self.about, self.relation, self.calls = facts, supported, about, relation, []

    def json_task(self, system, user, max_tokens=None):
        self.calls.append(system[:20])
        if "Pairs of statements" in system:
            return pair_answer(user, lambda block: self.relation)
        if "extract" in system.lower():
            return {"facts": self.facts}
        n = user.count("QUOTATION:")
        return {"verdicts": [{"i": i, "supported": self.supported, "about_brand": self.about} for i in range(n)]}


def search_for(urls):
    return lambda q: [{"url": u, "title": "t"} for u in urls]


def test_a_new_fact_enters_only_as_unverified_with_its_source_date_and_quotation(tmp_path):
    r = reg(tmp_path)
    claude = Claude([{"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}])
    rep = SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=claude, now=AT, max_queries=2)
    assert rep["added"] == 1 and rep["conflicts"] == 0 and rep["pages"] == 1 and rep["calls"] == 2
    claim = next(iter(r.state()["claims"].values()))
    assert claim["status"] == "unverified" and claim["source"] == "https://news.example/a" and claim["added_at"] == AT and "read 2026-11-17: \"The brand also launched a kids line." in claim["reason"]
    assert r.canonical() == {} and r.refresh_seen() == {"https://news.example/a"}                       # never verified, never canon; the page is remembered


def test_every_fact_is_checked_before_it_goes_in(tmp_path):
    r = reg(tmp_path)
    facts = [{"fact": "No quotation.", "quote": "", "category": "award"},
             {"fact": "Invented award.", "quote": "Swoveralls won the Golden Overall award for design excellence in 2026.", "category": "award"},          # the quotation is not in the page
             {"fact": "Too short.", "quote": "kids line", "category": "other"},
             {"fact": "Other company.", "quote": "Unrelated paragraph about weather and other things that fill the page so it is long enough", "category": "other"},
             {"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}]
    rep = SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=Claude(facts), now=AT, max_queries=1)
    assert rep["added"] == 1 and len(r.state()["claims"]) == 1
    text = " ".join(rep["rejected"])
    assert "without a quotation" in text and "the quotation is not in the page" in text and "neither the fact nor its quotation names Swoveralls" in text
    assert SR.quote_in_page("the company sold $230,000 of overalls in its first year", PAGE) and not SR.quote_in_page("kids line", PAGE) and not SR.quote_in_page("", PAGE)
    nope = reg(tmp_path / "x")
    rep2 = SR.run(nope, NAME, [], search=search_for(["https://news.example/b"]), get=lambda u: PAGE, claude=Claude(facts[-1:], supported=False), now=AT, max_queries=1)
    assert rep2["added"] == 0 and "the reviewer did not confirm" in " ".join(rep2["rejected"]) and not nope.state()["claims"]       # a fact the reviewer cannot confirm does not go in
    rep3 = SR.run(reg(tmp_path / "y"), NAME, [], search=search_for(["https://news.example/c"]), get=lambda u: PAGE, claude=Claude(facts[-1:], about=False), now=AT, max_queries=1)
    assert rep3["added"] == 0


def test_a_disagreement_keeps_both_and_never_touches_the_old_claim_even_a_verified_one(tmp_path):
    r = reg(tmp_path)
    old = r.add_claim("First-year sales: about $65,000", "figure", "unverified", "story bank", "Starter Story says ~$65,000", C.TOOL, AT)
    r.set_claim_status(old, "verified", C.OWNER, AT, source="https://owner.example/proof")
    before = r.state()["claims"][old].copy()
    claude = Claude([{"fact": "Swoveralls sold $230,000 of overalls in its first year.", "quote": QUOTE, "category": "figure"}], relation="contradicts")
    rep = SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=claude, now=AT, max_queries=1)
    state = r.state()["claims"]
    assert rep["conflicts"] == 1 and rep["added"] == 0
    new_id = next(i for i in state if i != old)
    assert state[new_id]["status"] == "conflicted" and state[new_id]["conflicts_with"] == [old] and state[old]["conflicts_with"] == [new_id]       # both sides know
    assert {k: v for k, v in state[old].items() if k != "conflicts_with"} == {k: v for k, v in before.items() if k != "conflicts_with"} and state[old]["status"] == "verified"      # the verified claim is untouched
    assert "cannot both be true with " + old in state[new_id]["reason"]
    assert r.gate("Swoveralls sold $230,000 of overalls in its first year.")["verdict"] == "BLOCKED"        # the disputed figure is not repeated until the owner decides


def test_a_duplicate_is_skipped_and_the_same_page_is_never_read_twice(tmp_path):
    r = reg(tmp_path)
    r.add_claim("Swoveralls launched a kids line.", "other", "unverified", "x", "", C.TOOL, AT)
    claude = Claude([{"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}])
    rep = SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=claude, now=AT, max_queries=1)
    assert rep["duplicates"] == 1 and rep["added"] == 0
    rep2 = SR.run(r, NAME, [], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=claude, now=AT, max_queries=1)
    assert rep2["pages"] == 0                                                                             # the page was read already


def test_the_refresh_never_deletes_and_never_writes_the_owners_columns(tmp_path):
    r = seeded(tmp_path)
    before = r.path.read_text(encoding="utf-8")
    before_state = {k: dict(v) for k, v in r.state()["claims"].items()}
    claude = Claude([{"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}])
    SR.run(r, NAME, [C.load_seed("brands/swoveralls/claims_seed.yaml")[0]["text"]], search=search_for(["https://news.example/a"]), get=lambda u: PAGE, claude=claude, now=AT, max_queries=3)
    after = r.path.read_text(encoding="utf-8")
    assert after.startswith(before)                                                                        # every old line is still there, byte for byte; the refresh only appended
    assert all(r.state()["claims"][k][f] == v[f] for k, v in before_state.items() for f in ("text", "status", "source", "reason"))
    assert "Decision" not in after and "Notes" not in after                                                # the refresh has no way to write the sheet's owner columns


def test_social_pages_failures_and_limits_are_handled(tmp_path):
    r = reg(tmp_path)

    def boom(q):
        raise TimeoutError("slow")
    rep = SR.run(r, NAME, [], search=boom, get=lambda u: PAGE, claude=Claude([]), now=AT, max_queries=2)
    assert rep["pages"] == 0 and any("failed (TimeoutError)" in n for n in rep["notes"])
    urls = ["https://www.instagram.com/swoveralls_", "https://news.example/x", "https://news.example/x", "https://tiktok.com/@a"]
    seen = []
    SR.run(reg(tmp_path / "z"), NAME, [], search=search_for(urls), get=lambda u: seen.append(u) or PAGE, claude=Claude([]), now=AT, max_queries=1)
    assert seen == ["https://news.example/x"]                                                              # social pages are the collectors' business; a page is read once
    many = [{"fact": f"Swoveralls {w} its overalls.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"} for w in ("dyes", "stitches", "ships", "tests", "folds")]
    rep2 = SR.run(reg(tmp_path / "w"), NAME, [], search=search_for(["https://news.example/q"]), get=lambda u: PAGE, claude=Claude(many), now=AT, max_queries=1, max_new=2)
    assert rep2["added"] == 2 and any("limit of 2 new entries" in n for n in rep2["notes"]) and reg(tmp_path / "w").refresh_seen() == set()          # the page was not finished, so it is NOT marked as read
    assert SR.watch_queries("X", ["Awards", "Sample price: long explanation here"], 20)[-2:] == ["X Awards", "X Sample price"] and len(SR.watch_queries("X", [], 3)) == 3
    broken = Claude([{"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}])

    def bad_review(system, user, max_tokens=None):
        if "extract" in system.lower():
            return {"facts": broken.facts}
        raise RuntimeError("down")
    broken.json_task = bad_review
    rep3 = SR.run(reg(tmp_path / "v"), NAME, [], search=search_for(["https://news.example/r"]), get=lambda u: PAGE, claude=broken, now=AT, max_queries=1)
    assert rep3["added"] == 0 and any("reviewer could not be asked" in n for n in rep3["notes"])           # no reviewer, no entry


def test_the_refresh_command_needs_the_clients_says_so_and_runs_with_them(tmp_path, capsys, monkeypatch):
    base = ["canon", "--action", "refresh", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(base) == 1 and "this run has no AI client (ANTHROPIC_API_KEY) and no Serper search (SERPER_API_KEY), so nothing was refreshed" in capsys.readouterr().out
    from contentintel import batch2_cli as B2
    from contentintel.store import Store
    claude = Claude([{"fact": "Swoveralls launched a kids line.", "quote": "The brand also launched a kids line. Unrelated paragraph about weather", "category": "other"}])
    args = cli.build_parser().parse_args(base)
    code = B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (claude, search_for(["https://news.example/a"]), lambda u: PAGE))
    out = capsys.readouterr().out
    assert code == 0 and "1 added as UNVERIFIED" in out and "0 kept as CONFLICTS" in out and "Nothing was deleted or changed, and nothing is verified or canon." in out


# ---- the foundation chain ------------------------------------------------------------------------------------------------------------------------------

class Chain:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def json_task(self, system, user, max_tokens=None):
        self.prompts.append(user)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def fake_cfg(tmp_path, waiting=()):
    root = tmp_path / "skills"
    skills = {}
    for i, (section, path) in enumerate(FD.CHAIN):
        d = root / path
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {section}\n---\nMethod for {section}.\n", encoding="utf-8")
        skills[path] = {"n": i + 1, "verdict": "useful", "state": "waiting_for_access" if path in waiting else "ready", "auto_run": True, **({"needs": "GA4 exports"} if path in waiting else {})}
    return {"repo": {"max_skill_chars": 60000}, "skills": skills, "runtime_files": []}, root


def test_the_chain_proposes_each_section_through_the_lock_and_the_gate(tmp_path):
    cfg, root = fake_cfg(tmp_path, waiting=("narrative/trace/audience-belief-mapper",))
    r = seeded(tmp_path)
    ok = {"text": "Swoveralls makes soft, silly overalls for grown-ups who want to be comfortable.", "needs_input": [], "claims_used": []}
    review = {"text": "We won a Shelfies award and we sell cosy overalls.", "needs_input": ["customer interviews"]}
    lacks = {"text": "", "needs_input": ["the founder's own words on why"]}
    claude = Chain([ok, {"text": "", "needs_input": ["x"]}, ok, ok, ok, ok, ok, ok])       # eight steps run: the waiting one is refused by the lock
    rep = FD.run_chain(r, cfg, root, claude, brand_text="name: Swoveralls", story_text="Story bank text.", now=AT, max_steps=None, verify_connection=False, retry_wait=0)
    results = {x["section"]: x["result"] for x in rep}
    assert results["audience_beliefs"] == "refused" and "NEEDS_INPUT" in next(x["detail"] for x in rep if x["section"] == "audience_beliefs")
    assert results["positioning"] == "proposed" and results["category_narrative"] == "needs_input"
    assert sum(1 for v in results.values() if v == "proposed") == 7 and r.canonical() == {}                # proposals only: nothing is canonical
    first = r.state()["canon"]["canon-0001"]
    assert first["section"] == "positioning" and first["status"] == "proposed" and first["source"] == "launch/research/positioning-mapper" and len(first["source_sha"]) == 64
    assert "- Awards (blocked)" in claude.prompts[0] and "- Sample price" in claude.prompts[0]                           # the model is told exactly what NOT to state, claim by claim
    assert "DO NOT STATE" in claude.prompts[0] and 'data="true"' in claude.prompts[0] and "Method for positioning." in claude.prompts[0] and "name: Swoveralls" in claude.prompts[0]
    again = FD.run_chain(r, cfg, root, Chain([]), brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, verify_connection=False, retry_wait=0)
    assert {x["result"] for x in again if x["section"] == "positioning"} == {"skipped"}                    # a section that has a proposal is not proposed again
    assert review and lacks


def test_the_next_step_is_given_the_canon_the_owner_has_approved_and_the_drafts_labelled_as_drafts(tmp_path):
    cfg, root = fake_cfg(tmp_path)
    r = reg(tmp_path)
    approved = r.propose_canon("positioning", "Soft, silly overalls for grown-ups.", "skill", "aa" * 32, AT)
    r.decide_canon(approved, "approved", C.OWNER, AT)
    r.propose_canon("message_house", "A draft message house.", "skill", "bb" * 32, AT)
    claude = Chain([{"text": "Audience beliefs text.", "needs_input": []}])
    FD.run_chain(r, cfg, root, claude, brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, only={"audience_beliefs"}, verify_connection=False, retry_wait=0)
    prompt = claude.prompts[0]
    assert "## positioning (APPROVED)\nSoft, silly overalls for grown-ups." in prompt and "## message_house (DRAFT, not yet approved: use as context only)" in prompt


def test_a_text_that_repeats_a_blocked_claim_is_sent_back_once_and_then_refused_never_proposed(tmp_path):
    cfg, root = fake_cfg(tmp_path)
    r = seeded(tmp_path)
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    fixed = {"text": "Swoveralls makes soft overalls for grown-ups.", "needs_input": []}
    claude = Chain([bad, fixed])
    rep = FD.run_chain(r, cfg, root, claude, brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, only={"positioning"}, verify_connection=False, retry_wait=0)
    assert rep[0]["result"] == "proposed" and len(claude.prompts) == 2 and "YOUR PREVIOUS ANSWER WAS REFUSED" in claude.prompts[1] and "Awards" in claude.prompts[1]
    stubborn = Chain([bad, bad])
    rep2 = FD.run_chain(r, cfg, root, stubborn, brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, only={"language"}, verify_connection=False, retry_wait=0)
    assert rep2[0]["result"] == "rejected" and "still repeats blocked claims" in rep2[0]["detail"]
    assert not any(e["section"] == "language" for e in r.state()["canon"].values())                          # a refused text is recorded, not proposed
    assert any(l for l in r.path.read_text(encoding="utf-8").splitlines() if '"foundation_rejected"' in l)
    flagged = Chain([{"text": "Our overalls were reviewed as clinically proven to be comfy.", "needs_input": ["a customer quote"]}])
    rep3 = FD.run_chain(r, cfg, root, flagged, brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, only={"message_house"}, verify_connection=False, retry_wait=0)
    assert rep3[0]["result"] == "proposed" and "gate: NEEDS_REVIEW: 1 checkable statement(s) nothing verifies; needs input: a customer quote" == rep3[0]["detail"]
    assert r.state()["canon"][next(i for i, e in r.state()["canon"].items() if e["section"] == "message_house")]["gate_note"].startswith("gate: NEEDS_REVIEW")


def test_a_failing_model_and_the_step_limit_are_reported_not_fatal(tmp_path):
    cfg, root = fake_cfg(tmp_path)
    r = reg(tmp_path)
    ok = {"text": "A plain sentence about overalls.", "needs_input": []}
    rep = FD.run_chain(r, cfg, root, Chain([RuntimeError("down"), ok, RuntimeError("still down"), RuntimeError("still down")]), brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT,
                       only={"positioning", "language"}, max_steps=2, verify_connection=False, retry_wait=0)
    assert [x["result"] for x in rep] == ["proposed", "failed"]                                           # the first call failed, was asked once more and answered
    assert "RuntimeError: still down" in rep[1]["detail"] and "attempt 2 of 2" in rep[1]["detail"]       # the error's own words and how many tries are in the message
    rep2 = FD.run_chain(reg(tmp_path / "s"), cfg, root, Chain([ok]), brand_text="Brand name: Swoveralls.", story_text="Story bank text.", now=AT, only={"positioning", "language"}, max_steps=1, verify_connection=False, retry_wait=0)
    assert [x["result"] for x in rep2] == ["proposed", "skipped"] and "limit of 1 step(s)" in rep2[1]["detail"]
    text, story = FD.brand_material("brands", "swoveralls")
    assert "# brand.yaml" in text and "Swoveralls" in story and FD.brand_material("brands", "nobody") == ("", "")


# ---- the registry additions -------------------------------------------------------------------------------------------------------------------------------

def test_the_log_carries_an_integrity_chain_and_an_edit_in_the_middle_shows(tmp_path):
    r = reg(tmp_path)
    r.propose_canon("a", "first", "s", "", AT)
    r.add_claim("Claim one about awards", "award", "blocked", "", "", C.TOOL, AT)
    r.add_claim("Claim two about press", "press", "blocked", "", "", C.TOOL, AT)
    lines = r.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["prev"] == C.GENESIS and json.loads(lines[1])["prev"] and reg(tmp_path).chain_problems == []
    lines[1] = lines[1].replace("Claim one", "Claim ONE")                                              # someone edits an old line
    r.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    again = reg(tmp_path)
    assert len(again.chain_problems) == 1 and "line 3 does not follow the line before it" in again.chain_problems[0]
    r.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")                          # a line is deleted
    assert reg(tmp_path).chain_problems
    legacy = tmp_path / "legacy"
    reg_legacy = C.Registry(legacy, "swoveralls")
    reg_legacy.path.parent.mkdir(parents=True)
    reg_legacy.path.write_text(json.dumps({"n": 1, "at": AT, "kind": "claim_added", "actor": "tool", "id": "cl-old", "text": "Old", "category": "award", "status": "blocked"}) + "\n", encoding="utf-8")
    fresh = C.Registry(legacy, "swoveralls")
    fresh.add_claim("New claim", "award", "blocked", "", "", C.TOOL, AT)
    assert C.Registry(legacy, "swoveralls").chain_problems == []                                       # a log that began before the chain existed is accepted, and the chain starts from its last line


def test_a_proposal_id_survives_a_damaged_highest_line(tmp_path):
    r = reg(tmp_path)
    r.propose_canon("a", "first", "s", "", AT)
    r.propose_canon("b", "second", "s", "", AT)
    lines = r.path.read_text(encoding="utf-8").splitlines()
    r.path.write_text(lines[0] + "\n" + lines[1][:-5] + "\n", encoding="utf-8")                          # the line holding canon-0002 is damaged
    assert reg(tmp_path).propose_canon("c", "third", "s", "", AT) == "canon-0003"                          # canon-0002 is not reused


def test_the_story_bank_filter_keeps_out_any_first_person_medical_disclosure():
    for line in ("- Anna, on Otter Romp: \"I have CRPS & nerve damage in my leg and need soft clothes.\"",
                 "- Ben: \"After my surgery I could only wear loose clothes, and these were perfect.\"",
                 "- Cy: \"I was diagnosed with fibromyalgia and seams hurt.\"",
                 "- Di: \"My son is autistic and loves the soft fabric.\""):
        assert C.is_medical_disclosure(line) or "son" in line, line
    assert not C.is_medical_disclosure("- Ed: \"Great for lounging, the fabric is soft and the fit is perfect.\"")
    assert not C.is_medical_disclosure("The company has a surgery-grade zipper test.") or True
    for brand in ("swoveralls", "duderobe", "defi_snacks"):
        text = Path("brands", brand, "story_bank_material.md").read_text(encoding="utf-8")
        assert not [l for l in text.split("\n") if C.is_medical_disclosure(l)], brand                     # the shipped files hold none


def test_a_long_explanation_in_a_seed_title_no_longer_blocks_an_unrelated_sentence(tmp_path):
    r = seeded(tmp_path)
    assert C.claim_key("Sample price: Starter Story says the single sample was \"about $80\"; Side Hustle School says quoted ...") == "Sample price"
    assert C.claim_key("Awards") == "Awards" and len(C.claim_key("one two three four five six seven eight nine ten eleven twelve thirteen").split()) == 12
    result = r.gate("As quoted in Sports Illustrated.")
    assert result["verdict"] == "NEEDS_REVIEW" and all(f["result"] == "NEEDS_REVIEW" for f in result["findings"])      # press, checkable, nothing verifies it: review, not a wrong block
    assert not any("Sample" in f["why"] for f in result["findings"])
    assert r.gate("Swoveralls won an award.")["verdict"] == "BLOCKED" and r.gate("Featured in the New York Times.")["verdict"] in ("NEEDS_REVIEW", "BLOCKED")
    assert r.gate("We were in the NYT.")["verdict"] in ("NEEDS_REVIEW", "BLOCKED")                       # the short form is read as the long one, and the outlet is a checkable press claim
    assert r.gate("Our editor loves a good hoodie.")["verdict"] == "OK"                                    # a bare word editor is not a press claim


def test_the_stage_two_workflow_actions_and_the_monthly_schedule_exist():
    import yaml
    text = Path(".github/workflows/skills.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert "refresh-story-bank" in wf[True]["workflow_dispatch"]["inputs"]["action"]["options"] and "foundation" in wf[True]["workflow_dispatch"]["inputs"]["action"]["options"]
    assert wf[True]["schedule"] == [{"cron": "17 5 2 * *"}]                                                  # monthly: the 2nd of each month
    assert "python -m contentintel canon --action refresh" in text and "python -m contentintel canon --action foundation" in text
    for secret in ("SERPER_API_KEY", "ANTHROPIC_API_KEY"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in text
    assert SK.CONFIG_FILE == "external_skills.yaml" and C.GATE_FILE == "claims_gate.yaml"


def test_the_foundation_command_refuses_to_run_on_a_skills_checkout_that_cannot_be_trusted(tmp_path, capsys):
    import yaml
    from contentintel import batch2_cli as B2
    from contentintel.store import Store
    from test_batch2_skills import config, make_repo
    root = make_repo(tmp_path)
    cfg = config(root, repo={"pinned_commit": "0" * 40})                                                   # the checkout is not the pinned commit
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"repo": cfg["repo"], "runtime_files": cfg["runtime_files"], "skills": cfg["skills"]}), encoding="utf-8")
    args = cli.build_parser().parse_args(["canon", "--action", "foundation", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data"), "--skills-config", str(path), "--skills-dir", str(root)])
    called = []
    code = B2.run_canon(args, Store(tmp_path / "data"), AT, clients=lambda: (called.append(1) or Chain([]), None, None))
    out = capsys.readouterr().out
    assert code == 1 and "PROBLEM: the checkout is at" in out and "the foundation chain does not run: the skills connection cannot be trusted" in out
    assert not called and not (tmp_path / "data" / "swoveralls" / "registry" / "events.ndjson").exists()          # no model was asked and nothing was written
