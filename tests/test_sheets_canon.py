"""The Canon and Claims tabs of the sheet: settled decisions are cleared, each tab has its Decision list, a tab that cannot be synced does not stop the others, and a brand with no events leaves old tabs alone."""
from types import SimpleNamespace

from contentintel import canon as C
from contentintel import sheets as SH
from contentintel.store import Store

from fake_sheets import FakeClient, FakeSpreadsheet, FakeWorksheet, read
from test_sheets import baselined, one

import pytest

AT = "2026-11-17T12:00:00Z"
BRAND = SimpleNamespace(slug="swoveralls")


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    monkeypatch.setattr(SH, "_SLEEP", lambda s: None)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def fill(ws, rows):
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row, 1):
            ws.cells[(r, c)] = value


def seeded(tmp_path):
    reg = C.Registry(tmp_path / "data", "swoveralls")
    first = reg.propose_canon("positioning", "First.", "skill", "aa" * 32, AT)
    second = reg.propose_canon("audience_beliefs", "Second.", "skill", "bb" * 32, AT)
    third = reg.propose_canon("language", "Third.", "skill", "cc" * 32, AT)
    return reg, first, second, third


def sheet_with_decisions(reg, decisions):
    sp = FakeSpreadsheet("s1", blank_tab=False)
    ws = sp.add_worksheet(C.CANON_TAB, 100, 12)
    rows = reg.canon_rows()
    rows[0][-1] = "Decision (approve / reject)"
    for row in rows[1:]:
        row[-1] = decisions.get(row[0], "")
    fill(ws, rows)
    ws.cells[(2, len(rows[0]) + 1)] = "owner note"                         # a Notes cell beyond the Decision column must survive
    return sp, ws


def test_a_decision_that_was_applied_is_cleared_from_its_cell_while_notes_and_other_cells_stay(tmp_path):
    reg, first, second, third = seeded(tmp_path)
    reg.decide_canon(second, "approved", C.OWNER, AT)                      # applied earlier: its cell still says approve
    sp, ws = sheet_with_decisions(reg, {first: "approve", second: "approve", third: "maybe later"})
    report = SimpleNamespace(notes=[])
    SH._read_canon_decisions(sp, BRAND, Store(tmp_path / "data"), AT, report, False)
    decision_col = len(reg.canon_rows()[0])
    now = C.Registry(tmp_path / "data", "swoveralls").state()["canon"]                                          # read again: the sheet's reader wrote to the log
    assert (now[first]["status"], now[second]["status"], now[third]["status"]) == ("approved", "approved", "proposed")
    assert ws.cells.get((2, decision_col)) is None and ws.cells.get((3, decision_col)) is None                    # applied now and applied earlier: both cleared
    assert ws.cells[(4, decision_col)] == "maybe later"                                                          # an unknown word is NOT cleared: the owner sees it was not understood
    assert ws.cells[(2, decision_col + 1)] == "owner note" and ws.cells[(2, 3)] == "First."                       # nothing else was touched
    assert any("2 applied Decision cell(s) cleared" in n for n in report.notes) and any("ignored" in n for n in report.notes)
    again = SimpleNamespace(notes=[])
    SH._read_canon_decisions(sp, BRAND, Store(tmp_path / "data"), AT, again, False)
    assert not any("cleared" in n for n in again.notes)                                                          # a second read has nothing to clear


def test_a_tab_without_a_decision_column_or_without_matches_clears_nothing(tmp_path):
    sp = FakeSpreadsheet("s2", blank_tab=False)
    ws = sp.add_worksheet("Claims", 20, 6)
    fill(ws, [["ID", "Claim", "Status"], ["cl-1", "x", "verified"]])
    assert SH._clear_settled_decisions(ws, {"cl-1"}) == 0 and ws.cells[(2, 3)] == "verified"
    ws2 = sp.add_worksheet("Canon", 20, 6)
    fill(ws2, [["ID", "Decision"], ["canon-1", "approve"]])
    assert SH._clear_settled_decisions(ws2, {"canon-9"}) == 0 and ws2.cells[(2, 2)] == "approve"
    assert SH._clear_settled_decisions(ws2, {"canon-1"}) == 1 and (2, 2) not in ws2.cells


def test_each_registry_tab_gets_its_own_decision_list_and_the_tab_titles_match_the_registry(tmp_path):
    assert SH.CANON_TAB_TITLE == C.CANON_TAB and SH.CLAIMS_TAB_TITLE == C.CLAIMS_TAB
    reg, *_ = seeded(tmp_path)
    sp, ws = sheet_with_decisions(reg, {})
    SH._decision_dropdown(sp, ws, SH.CANON_CHOICES)
    SH._decision_dropdown(sp, ws, SH.CLAIM_CHOICES)
    rules = [b["requests"][0]["setDataValidation"] for b in sp.batches]
    assert [[v["userEnteredValue"] for v in r["rule"]["condition"]["values"]] for r in rules] == [["approve", "reject"], ["verified", "blocked", "unverified", "conflicted"]]
    assert rules[0]["range"]["startColumnIndex"] == len(reg.canon_rows()[0]) - 1                                 # on the Decision column, not another
    plain = FakeWorksheet(sp, "Idea Bank")
    fill(plain, [["idea", "Decision", "Notes"]])
    SH._decision_dropdown(sp, plain)
    assert [v["userEnteredValue"] for v in sp.batches[-1]["requests"][0]["setDataValidation"]["rule"]["condition"]["values"]] == list(SH.DECISIONS)      # the Idea Bank's own list is unchanged


def test_old_canon_tabs_of_a_brand_with_no_registry_events_are_left_alone_and_said(tmp_path):
    sp = FakeSpreadsheet("s3", blank_tab=False)
    ws = sp.add_worksheet(C.CANON_TAB, 20, 8)
    fill(ws, [["ID", "Section", "Text", "Status", "Source", "Proposed", "Gate note", "Decision"], ["canon-0001", "positioning", "Old.", "approved", "skill", AT, "", ""]])
    report = SimpleNamespace(notes=[])
    SH._read_canon_decisions(sp, BRAND, Store(tmp_path / "data"), AT, report, False)
    assert any("exist in the sheet but this brand has no registry events: the tabs are left as they are" in n for n in report.notes) and ws.cells[(2, 3)] == "Old."


def test_a_real_sync_survives_a_registry_tab_that_cannot_be_synced_and_still_writes_the_next_tab_and_the_decision_lists(swov, store, monkeypatch):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    reg = C.Registry(store.data_dir, "swoveralls")
    reg.propose_canon("positioning", "A text.", "skill", "aa" * 32, AT)
    reg.add_claim("A claim about awards", "award", "blocked", "", "", C.TOOL, AT)
    real, called = SH._sync_table, []

    def flaky(ws, **kw):
        called.append(kw["tab"])
        if kw["tab"] == C.CANON_TAB:
            raise ValueError("no id column")
        return real(ws, **kw)
    monkeypatch.setattr(SH, "_sync_table", flaky)
    client = FakeClient()
    report, error = one(swov, store, client)
    (sp,) = client.created
    assert error is None and C.CANON_TAB in called and C.CLAIMS_TAB in called                                     # the Claims tab was synced after the Canon tab failed
    assert any("Canon: not synced (ValueError: no id column)" in n and "the other tabs were" in n for n in report.notes)
    claims = next(w for w in sp.worksheets() if w.title == C.CLAIMS_TAB)
    assert read(claims)[0][0] == "id" or read(claims)[0][0] == "ID"
    lists = [[v["userEnteredValue"] for v in r["setDataValidation"]["rule"]["condition"]["values"]] for b in sp.batches for r in b["requests"] if "setDataValidation" in r]
    assert SH.CLAIM_CHOICES in lists                                                                              # the Claims tab got its own Decision list
    assert [w.title for w in sp.worksheets()].count(C.CANON_TAB) <= 1


def test_a_worksheet_that_cannot_clear_cells_still_has_its_decisions_applied_and_the_failure_is_said(tmp_path):
    reg, first, *_ = seeded(tmp_path)
    sp, ws = sheet_with_decisions(reg, {first: "approve"})
    ws.batch_clear = None                                                                                       # a worksheet whose clearing call fails
    report = SimpleNamespace(notes=[])
    SH._read_canon_decisions(sp, BRAND, Store(tmp_path / "data"), AT, report, False)
    assert C.Registry(tmp_path / "data", "swoveralls").state()["canon"][first]["status"] == "approved"
    assert any(n == "Canon and Claims decisions read from the sheet: 1 applied" for n in report.notes) and any("could not be cleared (TypeError)" in n and "the decisions themselves were applied" in n for n in report.notes)
