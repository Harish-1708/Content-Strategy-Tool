"""Batch 2, stage 1: the narrative canon and the offer-claims ledger (append-only events, owner-only approval, the claims gate, the story-bank seeds, the Sheets tabs)."""
import json
from pathlib import Path

import pytest
import yaml

from contentintel import canon as C
from contentintel import cli
from contentintel import sheets as SH
from contentintel.store import Store

AT = "2026-11-17T08:00:00Z"
BRANDS = ("swoveralls", "duderobe", "defi_snacks")


def reg(tmp_path, brand="defi_snacks"):
    return C.Registry(tmp_path / "data", brand)


def test_the_log_is_append_only_and_a_damaged_line_is_counted_never_rewritten(tmp_path):
    r = reg(tmp_path)
    r.propose_canon("positioning", "We are the snack for people who train.", "launch/research/positioning-mapper", "ab" * 32, AT)
    r.add_claim("Shelfies award winner", "award", "blocked", "story bank", "finalist only", C.TOOL, AT)
    before = r.path.read_text(encoding="utf-8")
    assert len(before.splitlines()) == 2 and json.loads(before.splitlines()[0])["n"] == 1 and json.loads(before.splitlines()[1])["n"] == 2
    r.path.write_text(before + "this line is damaged\n{\"no_kind\": 1}\n", encoding="utf-8")
    again = reg(tmp_path)
    assert again.damaged == 2 and len(again.state()["canon"]) == 1 and len(again.state()["claims"]) == 1
    again.add_claim("Another claim about revenue", "figure", "unverified", "", "", C.TOOL, AT)
    after = r.path.read_text(encoding="utf-8")
    assert after.startswith(before + "this line is damaged\n") and len(after.splitlines()) == 5          # old lines untouched, the new one appended
    with pytest.raises(C.CanonError, match="not a brand folder name"):
        C.Registry(tmp_path / "data", "../evil")


def test_the_tool_only_proposes_canon_and_nothing_is_canonical_until_the_owner_approves(tmp_path):
    r = reg(tmp_path)
    first = r.propose_canon("positioning", "Positioning v1", "skill-a", "11" * 32, AT)
    assert first == "canon-0001" and r.canonical() == {} and r.state()["canon"][first]["status"] == "proposed"
    with pytest.raises(C.CanonError, match="only the owner approves"):
        r.decide_canon(first, "approved", C.TOOL, AT)
    with pytest.raises(C.CanonError, match="only the owner approves"):
        r.decide_canon(first, "approved", "someone", AT)
    r.decide_canon(first, "approved", C.OWNER, AT, note="good")
    assert r.canonical() == {"positioning": "Positioning v1"}
    second = r.propose_canon("positioning", "Positioning v2", "skill-a", "22" * 32, AT)
    assert r.canonical() == {"positioning": "Positioning v1"}                                       # a new proposal changes nothing by itself
    r.decide_canon(second, "approved", C.OWNER, AT)
    state = r.state()["canon"]
    assert r.canonical() == {"positioning": "Positioning v2"} and state[first]["status"] == "superseded"
    third = r.propose_canon("narrative", "Story", "skill-b", "33" * 32, AT)
    r.decide_canon(third, "rejected", C.OWNER, AT)
    assert r.canonical() == {"positioning": "Positioning v2"} and r.state()["canon"][third]["status"] == "rejected"
    with pytest.raises(C.CanonError, match="not a proposal waiting"):
        r.decide_canon(third, "approved", C.OWNER, AT)
    with pytest.raises(C.CanonError, match="needs a section and a text"):
        r.propose_canon("", "x", "s", "", AT)


def test_only_the_owner_can_mark_a_claim_verified_and_the_same_claim_is_never_added_twice(tmp_path):
    r = reg(tmp_path)
    with pytest.raises(C.CanonError, match="only the owner marks a claim verified"):
        r.add_claim("Won the Shelfies", "award", "verified", "url", "", C.TOOL, AT)
    cid = r.add_claim("Won the Shelfies award", "award", "blocked", "story bank", "finalist only", C.TOOL, AT)
    assert r.add_claim("won the shelfies AWARD", "award", "blocked", "", "", C.TOOL, AT) == cid and len(r.state()["claims"]) == 1 and len(r.path.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(C.CanonError, match="only the owner marks a claim verified"):
        r.set_claim_status(cid, "verified", C.TOOL, AT, source="url")
    r.set_claim_status(cid, "verified", C.OWNER, AT, source="https://example.test/winners", reason="the winners list names them")
    claim = r.state()["claims"][cid]
    assert claim["status"] == "verified" and claim["source"] == "https://example.test/winners" and claim["decided_at"] == AT
    r.set_claim_status(cid, "unverified", C.TOOL, AT)                                                # the tool may always take a claim back
    assert r.state()["claims"][cid]["status"] == "unverified"
    with pytest.raises(C.CanonError):
        r.add_claim("x", "nonsense", "blocked", "", "", C.TOOL, AT)
    with pytest.raises(C.CanonError, match="not in the ledger"):
        r.set_claim_status("cl-missing", "blocked", C.TOOL, AT)


def seeded(tmp_path, brand="defi_snacks"):
    r = reg(tmp_path, brand)
    C.apply_seed(r, C.load_seed(Path("brands") / brand / "claims_seed.yaml"), AT)
    return r


def test_the_gate_blocks_what_the_ledger_says_not_to_claim_and_flags_what_nothing_verifies(tmp_path):
    r = seeded(tmp_path)
    blocked = r.gate("DEFI was featured on Good Morning America last spring.")
    assert blocked["verdict"] == "BLOCKED" and blocked["findings"][0]["result"] == "BLOCKED" and "Good Morning America segment" in blocked["findings"][0]["why"]
    assert r.gate("The brand posted revenue and sales figures of $2 million.")["verdict"] == "BLOCKED"       # a plural or a figure does not hide it
    review = r.gate("Clinically proven to boost your energy. It tastes great.")
    assert review["verdict"] == "NEEDS_REVIEW" and review["findings"][0]["categories"] == ["health"] and len(review["findings"]) == 1
    assert r.gate("A crispy bite of dark chocolate you can eat at your desk.") == {"verdict": "OK", "findings": []}
    assert r.gate("") == {"verdict": "OK", "findings": []}
    mixed = r.gate("It is clinically proven. It was on Good Morning America.")
    assert mixed["verdict"] == "BLOCKED" and [f["result"] for f in mixed["findings"]] == ["NEEDS_REVIEW", "BLOCKED"]           # one blocked sentence makes the whole text blocked
    assert r.gate("It was on Good Morning America. It is clinically proven.")["verdict"] == "BLOCKED"         # the order of the sentences does not matter
    claim = next(c for c in r.state()["claims"].values() if c["text"].startswith("Good Morning America"))
    r.set_claim_status(claim["id"], "verified", C.OWNER, AT, source="https://abc.example/gma")
    assert r.gate("DEFI was featured on Good Morning America last spring.")["verdict"] == "OK"                    # once the owner verifies it, it may be said
    assert r.gate("Clinically proven to boost your energy.")["verdict"] == "NEEDS_REVIEW"                          # but one verified claim does not cover a different statement


def test_the_other_brands_ledgers_gate_their_own_gaps(tmp_path):
    sw = seeded(tmp_path, "swoveralls")
    assert sw.gate("Swoveralls has won several awards.")["verdict"] == "BLOCKED"
    assert sw.gate("Swoveralls has won an award.")["verdict"] == "BLOCKED"                                        # singular and plural are the same claim
    assert sw.gate("It was featured in a Thrillist article.")["verdict"] == "BLOCKED"
    dr = seeded(tmp_path, "duderobe")
    assert dr.gate("DudeRobe made $2M in revenue.")["verdict"] == "BLOCKED" and dr.gate("Seen in GQ and Esquire.")["verdict"] in ("BLOCKED", "NEEDS_REVIEW")


def test_the_story_bank_is_split_per_brand_and_each_gaps_section_becomes_seeds_that_are_never_verified():
    md = Path("/mnt/user-data/uploads/Story-Bank-Material.md")
    for brand in BRANDS:
        text = Path("brands", brand, "story_bank_material.md").read_text(encoding="utf-8")
        assert "DudeRobe is a SEPARATE company from DUDE Products" in text and "Kyle Bergman" in text           # the shared header with the two corrections travels with every part
        seeds = C.load_seed(Path("brands", brand, "claims_seed.yaml"))
        assert seeds == [{"text": t, "category": c, "status": s, "reason": r, "source": "Story-Bank-Material.md (compiled 2026-11-16), Gaps / not found"} for t, c, s, r in C.seed_from_story_bank(brand, text)]
        assert seeds and all(s["status"] in ("blocked", "unverified", "conflicted") and s["category"] in C.CATEGORIES and s["reason"] for s in seeds)
    assert [len(C.load_seed(Path("brands", b, "claims_seed.yaml"))) for b in BRANDS] == [13, 10, 13]
    if md.exists():
        parts = C.split_story_bank(md.read_text(encoding="utf-8"))
        assert {b: parts[b] == Path("brands", b, "story_bank_material.md").read_text(encoding="utf-8") for b in BRANDS} == {b: True for b in BRANDS}
    with pytest.raises(C.CanonError, match="three 'Gaps / not found' sections"):
        C.split_story_bank("# x\n\n---\n\n## Gaps / not found\n- a\n")
    swapped = "# h\n---\nDudeRobe\n## Gaps / not found\n- a\n---\nSwoveralls\n## Gaps / not found\n- b\n---\nDEFI\n## 5. Gaps / not found\n1. c\n"
    with pytest.raises(C.CanonError, match="the file's order changed"):
        C.split_story_bank(swapped)


def test_gap_points_are_read_with_their_parents_and_classified_conservatively():
    md = "## Gaps / not found\n- **Awards:** none found. Do not claim any.\n- **Conflicting figures — flag before using:**\n  - First-year sales: A says 65k, B says 230k.\n  - Sample price: unclear.\n- Plain line\n  continued here\n## Next\n- not a gap\n"
    items = C.gap_items(md)
    assert [i["title"] for i in items] == ["Awards", "Conflicting figures — flag before using", "First-year sales: A says 65k, B says 230k.", "Sample price: unclear.", "Plain line continued here"]
    assert items[4]["text"] == "Plain line continued here" and items[2]["parent"].startswith("**Conflicting") is False and "Conflicting figures" in items[2]["parent"]
    seeds = {s[0][:10]: s for s in C.seed_from_story_bank("x", md)}
    assert seeds["Awards"][1:3] == ("award", "blocked") and seeds["First-year"][2] == "conflicted" and seeds["Sample pri"][2] == "conflicted"      # a nested point inherits "conflicting"
    assert C.gap_items("no gaps here") == []


def test_seeding_twice_adds_nothing_new_and_the_command_prints_the_counts(tmp_path, capsys):
    data = str(tmp_path / "data")
    assert cli.main(["canon", "--action", "seed", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 0
    assert "swoveralls: claims seeded from the story-bank gaps: 13 added, 0 already in the ledger (never as verified)" in capsys.readouterr().out
    assert cli.main(["canon", "--action", "seed", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 0
    assert "0 added, 13 already in the ledger" in capsys.readouterr().out
    assert cli.main(["canon", "--action", "status", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 0
    assert "claims {'blocked': 4, 'conflicted': 4, 'unverified': 5}; canon nothing proposed yet; 0 section(s) approved by the owner" in capsys.readouterr().out
    assert cli.main(["canon", "--action", "gate", "--text", "Swoveralls won awards.", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 2           # BLOCKED fails the run
    assert "BLOCKED" in capsys.readouterr().out
    assert cli.main(["canon", "--action", "gate", "--text", "Cosy overalls.", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 0
    assert cli.main(["canon", "--action", "gate", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 2 and "gate needs --text" in capsys.readouterr().out
    assert cli.main(["canon", "--action", "export", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", data]) == 0
    rows = json.loads((tmp_path / "data" / "swoveralls" / "registry" / "claim_rows.json").read_text(encoding="utf-8"))
    assert rows[0][0] == "ID" and len(rows) == 14


def test_a_seed_file_that_is_damaged_or_tries_to_seed_a_verified_claim_adds_nothing_wrong(tmp_path):
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump([{"text": "ok", "category": "award", "status": "verified", "reason": "x"}, {"text": "fine", "category": "award", "status": "blocked", "reason": "r"},
                                    {"text": "bad cat", "category": "zzz", "status": "blocked"}, "not a mapping", {"category": "award", "status": "blocked"}]), encoding="utf-8")
    assert [s["text"] for s in C.load_seed(path)] == ["fine"]                                       # verified cannot be seeded; malformed entries are dropped
    path.write_text("a: [unclosed\n", encoding="utf-8")
    assert C.load_seed(path) == [] and C.load_seed(tmp_path / "missing.yaml") == []


def test_the_sheet_tabs_have_the_sync_shape_and_appear_only_once_the_brand_has_a_registry(tmp_path):
    assert C.sheet_tabs(tmp_path / "data", "defi_snacks") == []
    r = seeded(tmp_path)
    r.propose_canon("positioning", "Positioning v1", "skill-a", "11" * 32, AT)
    canon_tab, claims_tab = C.sheet_tabs(tmp_path / "data", "defi_snacks")
    assert canon_tab["title"] == "Canon" and claims_tab["title"] == "Claims" and canon_tab["key_col"] == claims_tab["key_col"] == "id"
    assert canon_tab["starter_manual"] == claims_tab["starter_manual"] == ["Decision", "Notes"] and "Decision" not in canon_tab["columns"]          # the Decision column is the owner's
    assert list(canon_tab["rows_by_key"]) == ["canon-0001"] and len(claims_tab["rows_by_key"]) == 13
    row = next(iter(claims_tab["rows_by_key"].values()))
    assert set(row) == set(C.CLAIM_COLUMNS)
    assert SH._canon_tabs(type("B", (), {"slug": "defi_snacks"})(), Store(tmp_path / "data"), AT, type("R", (), {"notes": []})())[0]["title"] == "Canon"


def test_the_owner_decisions_in_the_sheet_are_read_by_column_name_applied_once_and_the_rest_named(tmp_path):
    r = seeded(tmp_path)
    cid = r.propose_canon("positioning", "Positioning v1", "skill-a", "11" * 32, AT)
    claim = next(iter(r.state()["claims"]))
    canon_values = [["Notes", "Decision", "id", "text"], ["", "approve", cid, "x"], ["", "", "canon-9999", "y"], ["", "maybe", "canon-0002", "z"]]
    claim_values = [["id", "claim", "Decision", "Notes"], [claim, "c", "verified", ""], ["cl-nope", "c", "blocked", ""], [claim, "c", "", ""]]
    done = r.owner_decisions(C.rows_from_sheet(canon_values), C.rows_from_sheet(claim_values), at=AT)
    assert done["applied"] == 2 and len(done["ignored"]) == 2 and any("'maybe' is not approve or reject" in i for i in done["ignored"]) and any("cl-nope" in i for i in done["ignored"])
    assert r.canonical() == {"positioning": "Positioning v1"} and r.state()["claims"][claim]["status"] == "verified"
    again = r.owner_decisions(C.rows_from_sheet(canon_values), C.rows_from_sheet(claim_values), at=AT)
    assert again["applied"] == 0 and len(again["ignored"]) == 2                                      # the Decision cell still shows what was applied: nothing is applied twice
    assert C.rows_from_sheet([]) == [] and C.rows_from_sheet([["a", "b"]]) == [] and C.rows_from_sheet([["id"], ["x"]]) == []


class FakeWorksheet:
    def __init__(self, values):
        self.values = values

    def get_all_values(self, **kw):
        return self.values


class FakeSheet:
    def __init__(self, tabs):
        self.tabs = {t: FakeWorksheet(v) for t, v in tabs.items()}

    def worksheets(self):
        return [type("W", (), {"title": t})() for t in self.tabs]

    def worksheet(self, title):
        return self.tabs[title]


class Report:
    def __init__(self):
        self.notes = []


def test_the_sheet_sync_reads_the_decisions_before_it_rewrites_the_tabs(tmp_path):
    r = seeded(tmp_path)
    cid = r.propose_canon("positioning", "Positioning v1", "skill-a", "11" * 32, AT)
    sheet = FakeSheet({"Canon": [["id", "section", "Decision"], [cid, "positioning", "approve"]], "Claims": [["id", "claim", "Decision"]]})
    brand, store, report = type("B", (), {"slug": "defi_snacks"})(), Store(tmp_path / "data"), Report()
    SH._read_canon_decisions(sheet, brand, store, AT, report, dry_run=True)
    assert "1 + 0 row(s) seen (dry run: none applied)" in report.notes[0] and C.Registry(tmp_path / "data", "defi_snacks").canonical() == {}
    SH._read_canon_decisions(sheet, brand, store, AT, report, dry_run=False)
    assert report.notes[1] == "Canon and Claims decisions read from the sheet: 1 applied" and C.Registry(tmp_path / "data", "defi_snacks").canonical() == {"positioning": "Positioning v1"}
    quiet = Report()
    SH._read_canon_decisions(FakeSheet({"Products": [["x"]]}), brand, store, AT, quiet, dry_run=False)
    assert quiet.notes == []                                                                         # no Canon or Claims tab yet: nothing to read, nothing said
    broken = Report()
    SH._read_canon_decisions(object(), brand, store, AT, broken, dry_run=False)
    assert broken.notes and broken.notes[0].startswith("Canon and Claims decisions not read:")        # a failure never stops the sheet
    src = Path("contentintel/sheets.py").read_text(encoding="utf-8")
    call = "    _read_canon_decisions(spreadsheet, brand, store, now, report, dry_run)\n    for tab in "                  # the CALL (the definition has the same words), right before the tabs are rewritten
    assert call in src and src.index(call) < src.index("_canon_tabs(brand, store, now, report) + _controls_tabs")
