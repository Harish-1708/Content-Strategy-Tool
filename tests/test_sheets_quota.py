"""Google allows 60 sheet writes a minute per user. The first big tidy (2026-10-27) deleted 137 rows one request at a time and was refused with
    429: Quota exceeded for quota metric 'Write requests' and limit 'Write requests per minute per user'
These tests keep that from coming back: writes are paced under the limit, a refusal is waited out for a whole minute, and deleting many rows
is one request, not one per row."""
import gspread
import pytest

from contentintel import ideabank as IB
from contentintel import sheets
from contentintel.sheets import SheetsError, WritePacer, _call, _prune_rows

from fake_sheets import FakeClient, FakeSpreadsheet, FakeWorksheet
from test_bank_tidy import row_ids
from test_ideabank import kept_bank
from test_sheets import baselined, one


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def waits(monkeypatch):
    seen = []
    monkeypatch.setattr(sheets, "_SLEEP", seen.append)
    return seen


def api_error(code, message="x"):
    class Resp:
        status_code = code
        text = ""

        def json(self):
            return {"error": {"code": code, "message": message, "status": "X"}}
    return gspread.exceptions.APIError(Resp())


def writes(pacer, n):
    for _ in range(n):
        pacer.wait()


# ---- pacing ---------------------------------------------------------------------------------------------------------------------------

def test_the_pace_is_safely_under_googles_limit():
    assert sheets.WRITES_PER_MINUTE == 45 < 60


def test_the_first_writes_of_a_minute_never_wait(waits):
    pacer = WritePacer(clock=Clock())
    writes(pacer, 45)
    assert waits == []


def test_the_write_that_would_be_one_too_many_waits_for_the_minute_to_pass(waits):
    clock = Clock()
    pacer = WritePacer(clock=clock)
    writes(pacer, 45)
    clock.t += 10                                              # 10 seconds later the oldest write is still inside the minute
    pacer.wait()
    assert waits == [pytest.approx(51)]                         # until it is a minute old, plus a second


def test_time_that_really_passed_is_not_waited_for_again(waits):
    clock = Clock()
    pacer = WritePacer(clock=clock)
    writes(pacer, 45)
    clock.t += 61
    writes(pacer, 45)
    assert waits == []


def test_a_long_run_of_writes_waits_once_per_minute_of_allowance_and_never_for_ever(waits):
    pacer = WritePacer(clock=Clock())                           # time only moves when the pacer sleeps, as with a no-op sleep in a test
    writes(pacer, 137)
    assert len(waits) == 3 and all(60 <= w <= 61 for w in waits)


def test_after_google_says_the_quota_is_spent_the_next_write_waits(waits):
    pacer = WritePacer(clock=Clock())
    writes(pacer, 3)
    pacer.spent()
    pacer.wait()
    assert len(waits) == 1 and waits[0] > 59


def test_reads_are_not_paced_and_every_kind_of_write_is(waits):
    class Pacer:
        count = 0

        def wait(self):
            self.count += 1

        def spent(self):
            pass
    sheets._PACER = Pacer()
    ws = FakeWorksheet(FakeSpreadsheet("s"), "t")
    _call(ws.get_all_values)
    _call(ws.col_values, 1)
    assert sheets._PACER.count == 0
    for name in ("update", "batch_update", "append_rows", "resize", "freeze", "clear", "update_title"):
        fn = getattr(ws, name)
        try:
            _call(fn, *{"update": ([[1]],), "batch_update": ([],), "append_rows": ([[1]],), "resize": (), "freeze": (), "clear": (), "update_title": ("u",)}[name])
        except Exception:
            pass
    assert sheets._PACER.count == 7


# ---- waiting out a refusal ---------------------------------------------------------------------------------------------------------------

def flaky(code, failures, message="x", name="update"):
    state = {"n": 0}

    def call():
        state["n"] += 1
        if state["n"] <= failures:
            raise api_error(code, message)
        return "done"
    call.__name__ = name
    return call, state


def minute(waits):
    return all(60 <= w <= 62 for w in waits)


def test_a_refused_write_waits_out_a_whole_minute_and_then_goes_through(waits):
    fn, state = flaky(429, 3)
    assert _call(fn) == "done" and len(waits) == 3 and minute(waits) and state["n"] == 4


def test_a_write_that_is_refused_every_time_gives_up_after_five_minutes_of_waiting_with_googles_words(waits):
    fn, state = flaky(429, 99, "Quota exceeded for quota metric 'Write requests'")
    with pytest.raises(SheetsError, match="Quota exceeded for quota metric 'Write requests'"):
        _call(fn)
    assert len(waits) == 5 and minute(waits) and state["n"] == 6


def test_a_refused_read_waits_the_shorter_steps_because_reads_have_their_own_larger_quota(waits):
    fn, state = flaky(429, 3, name="get_all_values")
    assert _call(fn) == "done" and waits == [20, 65, 65]
    fn, state = flaky(429, 99, name="get_all_values")
    waits.clear()
    with pytest.raises(SheetsError):
        _call(fn)
    assert waits == [20, 65, 65, 65, 65] and state["n"] == 6


def test_a_temporary_error_is_retried_with_short_waits(waits):
    fn, state = flaky(503, 99)
    with pytest.raises(SheetsError):
        _call(fn)
    assert waits == [2, 4, 8, 16, 32] and state["n"] == 6


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_other_errors_are_never_retried(waits, code):
    fn, state = flaky(code, 99)
    with pytest.raises(SheetsError):
        _call(fn)
    assert waits == [] and state["n"] == 1


def test_a_refusal_for_quota_makes_the_pacer_treat_the_minute_as_used(waits):
    sheets._PACER = WritePacer(clock=Clock())
    fn, _ = flaky(429, 1)
    assert _call(fn) == "done" and len(waits) == 1 and minute(waits)
    assert len(sheets._PACER.stamps) <= sheets.WRITES_PER_MINUTE + 1


# ---- deleting many rows is one request ---------------------------------------------------------------------------------------------------

def tab_with(keys, extra=None):
    sp = FakeSpreadsheet("s", blank_tab=False)
    ws = sp.add_worksheet("Idea Bank", 1000, 26)
    ws.update([["idea_id", "Decision", "Notes"]] + [[k, "", ""] for k in keys], range_name="A1")
    for (row, col), value in (extra or {}).items():
        ws._put(row, col, value)
    return sp, ws


def deletions(sp):
    return [r["deleteDimension"]["range"] for b in sp.batches for r in b["requests"] if "deleteDimension" in r]


def test_137_rows_are_deleted_in_one_request_not_137(waits):
    keys = [f"IB-{n:04d}" for n in range(1, 151)]
    sp, ws = tab_with(keys)
    gone = set(keys[:137])
    assert _prune_rows(sp, ws, "idea_id", gone) == (137, 0)
    assert len(sp.batches) == 1 and "delete_rows" not in ws.write_calls
    assert deletions(sp) == [{"sheetId": ws.id, "dimension": "ROWS", "startIndex": 1, "endIndex": 138}]
    assert [r[0] for r in ws.get_all_values()[1:]] == keys[137:]


def test_neighbouring_rows_become_one_range_and_the_last_range_goes_first(waits):
    keys = [f"IB-{n:04d}" for n in range(1, 11)]
    sp, ws = tab_with(keys)
    _prune_rows(sp, ws, "idea_id", {keys[0], keys[1], keys[4], keys[7], keys[8]})
    assert [(d["startIndex"], d["endIndex"]) for d in deletions(sp)] == [(8, 10), (5, 6), (1, 3)]      # 0-based, end exclusive, bottom first
    assert [r[0] for r in ws.get_all_values()[1:]] == [keys[2], keys[3], keys[5], keys[6], keys[9]]


def test_a_very_large_tidy_is_sent_in_chunks_of_a_hundred_requests(waits):
    keys = [f"IB-{n:04d}" for n in range(1, 502)]
    sp, ws = tab_with(keys)
    gone = {k for n, k in enumerate(keys) if n % 2 == 0}                     # every other row: 251 separate ranges
    assert _prune_rows(sp, ws, "idea_id", gone)[0] == 251
    assert [len(b["requests"]) for b in sp.batches] == [100, 100, 51]
    assert [r[0] for r in ws.get_all_values()[1:]] == [k for n, k in enumerate(keys) if n % 2 == 1]


def test_rows_a_person_wrote_on_split_the_ranges_and_are_kept(waits):
    keys = [f"IB-{n:04d}" for n in range(1, 8)]
    sp, ws = tab_with(keys, {(5, 2): "Use", (7, 3): "later"})                # IB-0004 and IB-0006
    assert _prune_rows(sp, ws, "idea_id", set(keys)) == (5, 2)
    assert [r[0] for r in ws.get_all_values()[1:]] == ["IB-0004", "IB-0006"]


def test_nothing_to_delete_sends_nothing(waits):
    sp, ws = tab_with(["IB-0001"])
    assert _prune_rows(sp, ws, "idea_id", {"IB-9999"}) == (0, 0) and sp.batches == []


def test_a_tab_without_the_key_column_is_left_alone(waits):
    sp = FakeSpreadsheet("s", blank_tab=False)
    ws = sp.add_worksheet("Idea Bank", 100, 5)
    ws.update([["title"], ["x"]], range_name="A1")
    assert _prune_rows(sp, ws, "idea_id", {"x"}) == (0, 0) and sp.batches == []


# ---- the whole tidy, as it failed ---------------------------------------------------------------------------------------------------------

class QuotaClient(FakeClient):
    """Refuses with a 429, as Google did, once more than 60 write requests have been made in the run (the fake has no clock)."""
    budget = 60

    def spend(self):
        self.budget -= 1
        if self.budget < 0:
            raise api_error(429, "Quota exceeded for quota metric 'Write requests' and limit 'Write requests per minute per user'")


def test_a_tidy_that_removes_137_rows_stays_far_below_the_write_quota(tmp_path, swov, waits):
    from contentintel.store import Store
    store = Store(tmp_path / "data")
    baselined(swov, store, "swoveralls_full_72.json", 72)
    store.write_ideabank("swoveralls", kept_bank(*[f"P{n:03d}" for n in range(150)]))
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    assert len(row_ids(sp)) == 150
    before = sum(len(w.write_calls) for w in sp.worksheets()) + len(sp.batches)
    bank = store.read_ideabank("swoveralls")
    for item in list(bank["items"].values())[:137]:
        IB._remove(item, "a copy of IB-0150", "IB-0150")
    store.write_ideabank("swoveralls", bank)
    report, error = one(swov, store, client)
    used = sum(len(w.write_calls) for w in sp.worksheets()) + len(sp.batches) - before
    assert error is None and len(row_ids(sp)) == 13 and used < 15 and "delete_rows" not in [c for w in sp.worksheets() for c in w.write_calls]
    assert "removed 137 row(s)" in report.notes[-1]
