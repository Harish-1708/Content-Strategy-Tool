import copy
import inspect
import json

import gspread
import pytest
import yaml
from gspread.utils import ValueRenderOption

from contentintel import cli, sheets
from contentintel.export import PRODUCT_COLUMNS, VARIANT_COLUMNS
from contentintel.store import Store
from contentintel.sync import confirm_baseline, confirm_removed, run_sync

from conftest import raw_products
from fake_sheets import (FakeClient, FakeSpreadsheet, FakeWorksheet, api_error, col_of,
                         delete_column, insert_column, last_row, read, reverse_data_rows, row_of)

T0, T1, T2, T3 = ("2026-10-19T08:00:00Z", "2026-10-19T08:30:00Z",
                  "2026-10-19T08:40:00Z", "2026-10-19T08:50:00Z")
NOW = "2026-10-19T09:00:00Z"
BOT = "bot@kelson-tool.iam.gserviceaccount.com"
EMAIL = "me@kelson.agency"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(sheets, "_SLEEP", seen.append)
    return seen


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def baselined(brand, store, fixture, count):
    run_sync(brand, store, raw_products=raw_products(fixture), now=T0)
    confirm_baseline(brand.slug, store, count, "tester", T0)


def resync(brand, store, fixture, mutate, now=T1):
    raw = raw_products(fixture)
    mutate(raw)
    return run_sync(brand, store, raw_products=raw, now=now)


def run(brands, store, client, *, config=None, emails=(EMAIL,), dry_run=False, now=NOW,
        folder_id=None):
    return sheets.run_sheets(brands, store, key_json="{}", share_emails=list(emails),
                             config=config or {}, now=now, dry_run=dry_run, folder_id=folder_id,
                             connect_fn=lambda key: (client, BOT))


def one(brand, store, client, **kw):
    (slug, report, error), = run([brand], store, client, **kw)
    assert slug == brand.slug
    return report, error


def pid(store, slug, handle):
    return next(p["product_id"] for p in store.load_latest(slug)["products"].values()
                if p["handle"] == handle)


def navy_price(raw, price="89.00"):
    next(p for p in raw if p["handle"] == "swoveralls-navy")["variants"][0]["price"] = price


def tabs(sp):
    return {w.title: w for w in sp.worksheets()}


# --- the first run builds everything ----------------------------------------------------

def test_first_run_creates_the_spreadsheet_tabs_headers_and_rows(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    report, error = one(swov, store, client)
    assert error is None
    (sp,) = client.created
    assert "Swoveralls" in sp.title
    assert sp.shares == [(EMAIL, "user", "writer", False)]
    assert [w.title for w in sp.worksheets()] == ["Products", "Variants", "Changes", "Status"]
    info = store.load_sheet_info("swoveralls")
    assert info == {"spreadsheet_id": sp.id, "url": sp.url, "created_by_tool": True,
                    "shared_with": [EMAIL]}

    header, rows = read(tabs(sp)["Products"])
    assert header == PRODUCT_COLUMNS + ["last_updated", "Notes"] and len(rows) == 72
    assert {r["last_updated"] for r in rows} == {NOW}
    row = next(r for r in rows if r["handle"] == "swoveralls-midnight-black")
    assert row["price_min"] == 99.0 and row["in_stock"] is True and row["variant_count"] == 9
    assert row["product_id"] == "3223478698084" and row["pocket_claims"] == "6"
    assert row["Notes"] == ""

    header, rows = read(tabs(sp)["Variants"])
    assert header == VARIANT_COLUMNS + ["last_updated"] and len(rows) == 462
    assert {r["last_updated"] for r in rows} == {NOW}
    assert isinstance(rows[0]["variant_id"], str) and isinstance(rows[0]["price"], float)
    assert isinstance(rows[0]["available"], bool)

    header, rows = read(tabs(sp)["Changes"])
    assert header == sheets.CHANGE_COLUMNS and rows == []

    status = {r[0]: r[1] for r in tabs(sp)["Status"].get_all_values()}
    assert status["Brand"] == "Swoveralls" and status["Catalogue usable for generation"] == "Yes"
    assert status["Products in catalogue"] == "72" and status["This sheet updated (UTC)"] == NOW

    assert report.tabs_created == ["Products", "Variants", "Changes", "Status"]
    assert report.rows_added == 72 + 462 and report.events_added == 0 and report.created_spreadsheet


def test_new_tabs_get_a_frozen_bold_header_row(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    t = tabs(client.created[0])
    for name in ("Products", "Variants", "Changes"):
        assert t[name].frozen_rows == 1
        assert t[name].formats == [("1:1", {"textFormat": {"bold": True}})]
    assert t["Status"].frozen_rows == 0


def test_a_second_run_with_no_changes_writes_only_the_status_tab(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    calls = {w.title: list(w.write_calls) for w in sp.worksheets()}
    cells = {w.title: dict(w.cells) for w in sp.worksheets()}
    report, error = one(swov, store, client, now="2026-10-19T10:00:00Z")
    assert error is None
    for w in sp.worksheets():
        if w.title == "Status":
            assert w.write_calls != calls["Status"]
        else:
            assert w.write_calls == calls[w.title] and w.cells == cells[w.title]
    assert (report.rows_updated, report.cells_updated, report.rows_added, report.events_added) == (0, 0, 0, 0)
    assert report.tabs_created == [] and report.columns_added == {} and len(client.created) == 1


# --- people's work is protected ----------------------------------------------------------

def test_manual_columns_survive_sorting_inserted_columns_and_updates(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    products = tabs(client.created[0])["Products"]
    navy, black = pid(store, "swoveralls", "swoveralls-navy"), pid(store, "swoveralls", "swoveralls-midnight-black")
    products.cells[(row_of(products, "product_id", navy), col_of(products, "Notes"))] = "check price"
    products.cells[(row_of(products, "product_id", black), col_of(products, "Notes"))] = "hero product"
    insert_column(products, 3, "Owner", {row_of(products, "product_id", navy): "Alice"})
    reverse_data_rows(products)

    resync(swov, store, "swoveralls_full_72.json", navy_price)
    report, error = one(swov, store, client)
    assert error is None

    header, rows = read(products)
    assert header[2] == "Owner" and header.index("Notes") > header.index("Owner")
    assert len(rows) == 72 and len({r["product_id"] for r in rows}) == 72
    by = {r["product_id"]: r for r in rows}
    assert (by[navy]["Notes"], by[navy]["Owner"], by[navy]["price_min"]) == ("check price", "Alice", 89.0)
    assert (by[black]["Notes"], by[black]["Owner"]) == ("hero product", "")
    _, variant_rows = read(tabs(client.created[0])["Variants"])
    navy_variants = [r for r in variant_rows if r["product_title"] == "Swoveralls - Navy"]
    assert [r["price"] for r in navy_variants].count(89.0) == 1 and len(navy_variants) == 9
    assert (report.rows_updated, report.cells_updated, report.rows_added) == (2, 2, 0)
    assert report.events_added == 1


def test_a_deleted_auto_column_is_recreated_without_disturbing_notes(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    products = tabs(client.created[0])["Products"]
    black = pid(store, "swoveralls", "swoveralls-midnight-black")
    products.cells[(row_of(products, "product_id", black), col_of(products, "Notes"))] = "keep me"
    delete_column(products, "materials")
    report, error = one(swov, store, client)
    assert error is None and report.columns_added == {"Products": ["materials"]}
    header, rows = read(products)
    assert header[-1] == "materials" and header.count("materials") == 1
    by = {r["product_id"]: r for r in rows}
    assert by[black]["materials"] == "cotton 65%; polyester 31%; spandex 4%"
    assert by[black]["Notes"] == "keep me"


def test_a_column_the_tool_does_not_know_is_never_touched(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    variants = tabs(client.created[0])["Variants"]
    insert_column(variants, 1, "Checked by", {2: "Dana", 3: "Eli"})
    resync(swov, store, "swoveralls_full_72.json", navy_price)
    one(swov, store, client)
    header, rows = read(variants)
    assert header[0] == "Checked by" and header[1] == "product_id"
    assert [rows[0]["Checked by"], rows[1]["Checked by"]] == ["Dana", "Eli"]


def test_new_products_go_to_the_bottom_and_existing_rows_stay_put(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    products = tabs(client.created[0])["Products"]
    navy = pid(store, "swoveralls", "swoveralls-navy")
    navy_row = row_of(products, "product_id", navy)

    def add(raw):
        extra = copy.deepcopy(raw[0])
        extra.update(id=777, handle="the-new-robe", title="The New Robe")
        for v in extra["variants"]:
            v["id"] += 10_000_000
        raw.append(extra)
    resync(swov, store, "swoveralls_full_72.json", add)
    report, error = one(swov, store, client)
    assert error is None
    header, rows = read(products)
    assert len(rows) == 73 and rows[-1]["handle"] == "the-new-robe" and rows[-1]["Notes"] == ""
    assert row_of(products, "product_id", navy) == navy_row
    assert report.rows_added == 1 + len(raw_products("swoveralls_full_72.json")[0]["variants"])


def test_a_removed_product_keeps_its_row_and_is_marked_then_restored(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    products, variants = tabs(sp)["Products"], tabs(sp)["Variants"]
    navy = pid(store, "swoveralls", "swoveralls-navy")
    products.cells[(row_of(products, "product_id", navy), col_of(products, "Notes"))] = "discuss"

    def without_navy(raw):
        raw[:] = [p for p in raw if p["handle"] != "swoveralls-navy"]
    resync(swov, store, "swoveralls_full_72.json", without_navy, T1)
    report, _ = one(swov, store, client)
    _, rows = read(products)
    by = {r["product_id"]: r for r in rows}
    assert len(rows) == 72 and by[navy]["status"] == "suspected_removed" and by[navy]["Notes"] == "discuss"
    _, variant_rows = read(variants)
    gone = [r for r in variant_rows if r["product_title"] == "Swoveralls - Navy"]
    assert len(gone) == 9 and all(r["available"] == "removed" for r in gone)
    assert report.rows_marked_removed == 1 + 9

    confirm_removed("swoveralls", store, navy, "tester", T2)
    one(swov, store, client)
    assert {r["product_id"]: r for r in read(products)[1]}[navy]["status"] == "confirmed_removed"

    resync(swov, store, "swoveralls_full_72.json", lambda raw: None, T3)
    one(swov, store, client)
    back = {r["product_id"]: r for r in read(products)[1]}[navy]
    assert back["status"] == "active" and back["Notes"] == "discuss"
    assert all(isinstance(r["available"], bool) for r in read(variants)[1]
               if r["product_title"] == "Swoveralls - Navy")


def test_changes_are_appended_once_and_a_users_column_survives(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    changes = tabs(client.created[0])["Changes"]
    resync(swov, store, "swoveralls_full_72.json", navy_price, T1)
    report, _ = one(swov, store, client)
    one(swov, store, client)                                   # running again adds nothing
    header, rows = read(changes)
    assert len(rows) == 1 and report.events_added == 1
    assert (rows[0]["type"], rows[0]["handle"], rows[0]["summary"]) == ("changed", "swoveralls-navy", "changed: price")
    assert len(rows[0]["event_id"]) == 16 and rows[0]["at"] == T1

    insert_column(changes, 3, "Reviewed", {2: "yes"})

    def sell_out(raw):
        navy_price(raw)
        next(p for p in raw if p["handle"] == "swoveralls-midnight-black")["variants"][0]["available"] = False
    resync(swov, store, "swoveralls_full_72.json", sell_out, T2)
    one(swov, store, client)
    header, rows = read(changes)
    assert len(rows) == 2 and header[2] == "Reviewed"
    assert [r["Reviewed"] for r in rows] == ["yes", ""]
    assert rows[1]["type"] == "availability"


def test_the_status_tab_says_when_the_catalogue_cannot_be_used(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    status = tabs(client.created[0])["Status"]
    assert {r[0]: r[1] for r in status.get_all_values()}["Catalogue usable for generation"] == "Yes"
    run_sync(swov, store, raw_products=raw_products("swoveralls_truncated_30.json"), now=T1)
    one(swov, store, client)
    values = {r[0]: r[1] for r in status.get_all_values()}
    assert values["Catalogue usable for generation"] == "NO" and "failed" in values["Notes"]


def test_status_lists_products_that_need_facts(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    client = FakeClient()
    one(dude, store, client)
    values = {r[0]: r[1] for r in tabs(client.created[0])["Status"].get_all_values()}
    assert values["Products needing facts"].startswith("3: ")
    assert "The SheRobe" in values["Products needing facts"]


# --- setting up --------------------------------------------------------------------------

def test_an_existing_blank_spreadsheet_is_reused_and_its_starter_tab_renamed(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sp = FakeSpreadsheet("abc123", "My blank sheet")
    client = FakeClient([sp])
    report, error = one(dude, store, client, config={"duderobe": "abc123"})
    assert error is None and client.created == [] and sp.shares == []
    assert [w.title for w in sp.worksheets()] == ["Products", "Variants", "Changes", "Status"]
    assert store.load_sheet_info("duderobe") == {
        "spreadsheet_id": "abc123", "url": sp.url, "created_by_tool": False}
    assert report.url == sp.url


def test_a_starter_tab_that_already_has_content_is_left_alone(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sp = FakeSpreadsheet("abc123")
    sp.worksheets()[0].cells[(1, 1)] = "my own notes"
    one(dude, store, FakeClient([sp]), config={"duderobe": "abc123"})
    assert [w.title for w in sp.worksheets()] == ["Sheet1", "Products", "Variants", "Changes", "Status"]
    assert sp.worksheet("Sheet1").cells == {(1, 1): "my own notes"}


def test_a_sheet_url_in_the_config_is_accepted(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sp = FakeSpreadsheet("abc123")
    config = {"duderobe": sheets._extract_id("https://docs.google.com/spreadsheets/d/abc123/edit#gid=0")}
    report, error = one(dude, store, FakeClient([sp]), config=config)
    assert error is None and report.url == sp.url


def test_an_unshared_spreadsheet_gives_a_helpful_error(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    report, error = one(dude, store, FakeClient(), config={"duderobe": "nope"})
    assert report is None and BOT in error and "Editor" in error


def test_no_sheet_and_no_email_explains_both_ways_forward(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    report, error = one(dude, store, FakeClient(), emails=())
    assert "sheets.yaml" in error and "SHEET_SHARE_EMAIL" in error and BOT in error


def test_a_tab_with_content_but_no_id_column_is_refused_untouched(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sp = FakeSpreadsheet("abc123", blank_tab=False)
    ws = sp.add_worksheet("Products", 1000, 26)
    ws.cells = {(1, 1): "Name", (2, 1): "Alice"}
    report, error = one(dude, store, FakeClient([sp]), config={"duderobe": "abc123"})
    assert report is None and "product_id" in error
    assert ws.write_calls == [] and ws.cells == {(1, 1): "Name", (2, 1): "Alice"}


def test_dry_run_for_a_brand_with_no_spreadsheet_creates_nothing(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    report, error = one(swov, store, client, dry_run=True)
    assert error is None and client.created == [] and store.load_sheet_info("swoveralls") is None
    assert report.created_spreadsheet and report.dry_run
    assert any("would" in line for line in report.lines())


def test_dry_run_on_an_existing_sheet_writes_nothing_but_reports_the_plan(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sp = FakeSpreadsheet("abc123")
    client = FakeClient([sp])
    report, error = one(dude, store, client, config={"duderobe": "abc123"}, dry_run=True)
    assert error is None
    assert [w.title for w in sp.worksheets()] == ["Sheet1"] and sp.worksheets()[0].write_calls == []
    assert report.tabs_created == ["Products", "Variants", "Changes", "Status"]
    assert report.rows_added == 25 + 176 and store.load_sheet_info("duderobe") is None


def test_dry_run_after_a_real_run_shows_pending_changes_without_writing(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    resync(swov, store, "swoveralls_full_72.json", navy_price)
    calls = {w.title: list(w.write_calls) for w in sp.worksheets()}
    report, _ = one(swov, store, client, dry_run=True)
    assert report.cells_updated == 2 and report.events_added == 1
    assert {w.title: w.write_calls for w in sp.worksheets()} == calls


def test_a_brand_with_no_catalogue_yet_still_gets_its_structure(dude, store):
    client = FakeClient()
    report, error = one(dude, store, client)
    assert error is None
    t = tabs(client.created[0])
    assert read(t["Products"])[0][:2] == ["product_id", "handle"] and read(t["Products"])[1] == []
    status = {r[0]: r[1] for r in t["Status"].get_all_values()}
    assert status["Catalogue usable for generation"] == "NO" and "baseline" in status["Notes"].lower()


def test_a_failed_share_is_remembered_and_retried_on_the_next_run(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)

    class ShareFails(FakeClient):
        def create(self, title, folder_id=None):
            made = super().create(title)

            def boom(*args, **kwargs):
                raise api_error(403, "no permission")
            made.share = boom
            return made

    first = ShareFails()
    report, error = one(dude, store, first)
    assert report is None and "Google said" in error
    info = store.load_sheet_info("duderobe")
    assert info["spreadsheet_id"] == first.created[0].id and info["shared_with"] == []

    sp = first.created[0]
    del sp.share                                              # the permission problem is fixed
    second = FakeClient([sp])
    report, error = one(dude, store, second)
    assert error is None and second.created == []
    assert sp.shares == [(EMAIL, "user", "writer", False)]
    assert store.load_sheet_info("duderobe")["shared_with"] == [EMAIL]
    assert [w.title for w in sp.worksheets()] == ["Products", "Variants", "Changes", "Status"]


def test_sharing_is_not_repeated_once_done(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    client = FakeClient()
    one(dude, store, client)
    one(dude, store, client)
    assert len(client.created[0].shares) == 1


# --- several brands ----------------------------------------------------------------------

def test_each_brand_gets_its_own_spreadsheet_and_data_never_mixes(swov, dude, defi, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    baselined(defi, store, "defi_snacks.json", 7)
    client = FakeClient()
    results = run([swov, dude, defi], store, client)
    assert [r[0] for r in results] == ["swoveralls", "duderobe", "defi_snacks"]
    assert all(r[2] is None for r in results) and len(client.created) == 3
    assert len({sp.id for sp in client.created}) == 3
    counts = [len(read(tabs(sp)["Products"])[1]) for sp in client.created]
    assert counts == [72, 25, 7]
    ids = [{r["product_id"] for r in read(tabs(sp)["Products"])[1]} for sp in client.created]
    assert not (ids[0] & ids[1]) and not (ids[0] & ids[2]) and not (ids[1] & ids[2])
    assert "Swoveralls" in client.created[0].title and "Defi" in client.created[2].title
    infos = {store.load_sheet_info(s)["spreadsheet_id"] for s in ("swoveralls", "duderobe", "defi_snacks")}
    assert len(infos) == 3


def test_one_brand_failing_does_not_stop_the_others(swov, dude, defi, store):
    for brand, fixture, count in ((swov, "swoveralls_full_72.json", 72),
                                  (dude, "duderobe_fetch_a.json", 25), (defi, "defi_snacks.json", 7)):
        baselined(brand, store, fixture, count)
    client = FakeClient()
    results = run([swov, dude, defi], store, client, config={"duderobe": "missing-id"})
    errors = {slug: error for slug, _, error in results}
    assert errors["duderobe"] and not errors["swoveralls"] and not errors["defi_snacks"]
    assert len(client.created) == 2


# --- the Google API misbehaves -------------------------------------------------------------

def test_rate_limits_are_retried_with_backoff(dude, store, sleeps):
    baselined(dude, store, "duderobe_fetch_a.json", 25)

    class Flaky(FakeClient):
        failures = 2

        def create(self, title, folder_id=None):
            if self.failures:
                self.failures -= 1
                raise api_error(429, "quota")
            return super().create(title)
    client = Flaky()
    report, error = one(dude, store, client)
    assert error is None and sleeps == [2, 4] and len(client.created) == 1


def test_a_persistent_server_error_gives_up_with_a_clear_message(dude, store, sleeps):
    baselined(dude, store, "duderobe_fetch_a.json", 25)

    class Down(FakeClient):
        def create(self, title, folder_id=None):
            raise api_error(500, "backend error")
    report, error = one(dude, store, Down())
    assert report is None and error.startswith("Google said") and len(sleeps) == 5


def test_permission_errors_are_not_retried_and_explain_the_fix(dude, store, sleeps):
    baselined(dude, store, "duderobe_fetch_a.json", 25)

    class Denied(FakeClient):
        def create(self, title, folder_id=None):
            raise api_error(403, "The caller does not have permission")
    report, error = one(dude, store, Denied())
    assert sleeps == [] and "Editor" in error and "Drive API" in error


# --- keys, config, small helpers -------------------------------------------------------------

def test_bad_secrets_are_rejected_without_echoing_them():
    for bad in ("SUPERSECRET{", "[]", '{"client_email": "a@b"}', '{"private_key": "SUPERSECRET"}'):
        with pytest.raises(sheets.SheetsError) as exc:
            sheets.connect(bad)
        assert "SUPERSECRET" not in str(exc.value)


def test_a_valid_looking_key_is_handed_to_gspread(monkeypatch):
    seen = {}
    monkeypatch.setattr(sheets.gspread, "service_account_from_dict",
                        lambda info: seen.setdefault("info", info) and "client")
    client, email = sheets.connect(json.dumps({"client_email": BOT, "private_key": "k"}))
    assert client == "client" and email == BOT and seen["info"]["client_email"] == BOT


def test_no_key_means_nothing_runs(swov, store):
    assert sheets.run_sheets([swov], store, key_json="", share_emails=[], config={}, now=NOW) is None
    assert sheets.run_sheets([swov], store, key_json=None, share_emails=[], config={}, now=NOW) is None


def test_config_file_rules(tmp_path):
    known = {"swoveralls", "duderobe"}
    path = tmp_path / "sheets.yaml"
    assert sheets.load_sheet_config(path, known) == {}
    path.write_text(yaml.safe_dump({"sheets": {
        "swoveralls": "", "duderobe": " https://docs.google.com/spreadsheets/d/abc_DEF-1/edit "}}))
    assert sheets.load_sheet_config(path, known) == {"swoveralls": None, "duderobe": "abc_DEF-1"}
    for bad in ({"sheets": {"nope": "x"}}, {"sheets": {"duderobe": 5}}, {"other": 1}, {"sheets": ["a"]}):
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(sheets.SheetsError):
            sheets.load_sheet_config(path, known)


def test_the_shipped_config_file_is_valid(brands):
    from conftest import ROOT
    config = sheets.load_sheet_config(ROOT / "sheets.yaml", set(brands))
    assert set(config) <= set(brands)


def test_email_lists():
    assert sheets.parse_emails("a@x.com, b@y.com;c@z.com ,") == ["a@x.com", "b@y.com", "c@z.com"]
    assert sheets.parse_emails("") == [] and sheets.parse_emails(None) == []


def test_value_typing_and_comparison():
    assert sheets._typed("price", "99.00") == 99.0 and sheets._typed("variant_count", "9") == 9
    assert sheets._typed("in_stock", "true") is True and sheets._typed("in_stock", "false") is False
    assert sheets._typed("variant_id", 53755991392367) == "53755991392367"
    assert sheets._typed("title", "x") == "x" and sheets._typed("price", "") == ""
    assert sheets._same(99, 99.0) and sheets._same("", None) and not sheets._same(True, 1)
    assert not sheets._same("1", 1) and sheets._same(True, True) and not sheets._same(True, False)


def test_column_letters():
    assert [sheets._col_letter(n) for n in (1, 26, 27, 52, 53, 702, 703)] == ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"]
    assert sheets._a1(3, 28) == "AB3"


def test_event_ids_are_stable_and_distinguish_events():
    ev = {"at": T0, "type": "changed", "product_id": "1", "details": {"price": [1]}}
    assert sheets.event_id(ev) == sheets.event_id(dict(ev))
    assert sheets.event_id(ev) != sheets.event_id(dict(ev, details={"price": [2]}))
    assert sheets.event_id(ev) != sheets.event_id(dict(ev, at=T1))


def test_big_tables_are_resized_and_written_in_chunks():
    ws = FakeSpreadsheet("big", blank_tab=False).add_worksheet("T", 1000, 30)
    rows = {str(i): {"id": str(i), "v": float(i)} for i in range(1500)}
    kw = dict(tab="T", columns=["id", "v"], key_col="id", dry_run=False)
    sheets._sync_table(ws, rows_by_key=rows, report=sheets.SheetReport("x"), **kw)
    assert len(read(ws)[1]) == 1500 and ws.row_count >= 1501
    assert ws.write_calls.count("append_rows") == 4 and ws.write_calls.count("resize") == 1
    ws.write_calls.clear()
    changed = {k: {"id": k, "v": v["v"] + 1} for k, v in rows.items()}
    report = sheets.SheetReport("x")
    sheets._sync_table(ws, rows_by_key=changed, report=report, **kw)
    assert report.cells_updated == 1500 and ws.write_calls.count("batch_update") == 4
    assert read(ws)[1][1499]["v"] == 1500.0


def test_wide_tables_are_resized_in_columns_too():
    ws = FakeSpreadsheet("wide", blank_tab=False).add_worksheet("T", 100, 3)
    cols = ["id"] + [f"c{i}" for i in range(10)]
    rows = {"1": {c: ("1" if c == "id" else "x") for c in cols}}
    sheets._sync_table(ws, tab="T", columns=cols, rows_by_key=rows, key_col="id",
                       report=sheets.SheetReport("x"), dry_run=False)
    assert ws.col_count >= 11 and read(ws)[0] == cols


def test_the_fakes_only_accept_arguments_the_real_gspread_accepts():
    for fake, real in ((FakeWorksheet, gspread.Worksheet), (FakeSpreadsheet, gspread.Spreadsheet),
                       (FakeClient, gspread.Client)):
        for name, fn in inspect.getmembers(fake, inspect.isfunction):
            if name.startswith("_"):
                continue
            real_params = set(inspect.signature(getattr(real, name)).parameters)
            fake_params = set(inspect.signature(fn).parameters) - {"self"}
            assert fake_params <= real_params, (fake.__name__, name, fake_params - real_params)
    assert hasattr(gspread.Spreadsheet, "id") and hasattr(gspread.Spreadsheet, "url")
    assert ValueRenderOption.unformatted.value == "UNFORMATTED_VALUE"


def test_reading_without_the_unformatted_option_returns_text_like_the_real_api():
    ws = FakeSpreadsheet("t", blank_tab=False).add_worksheet("T", 10, 10)
    ws.cells = {(1, 1): 99.0, (1, 2): True}
    assert ws.get_all_values() == [["99", "TRUE"]]
    assert ws.get_all_values(value_render_option=ValueRenderOption.unformatted) == [[99.0, True]]


def test_the_fake_rejects_writes_outside_the_grid():
    ws = FakeSpreadsheet("t", blank_tab=False).add_worksheet("T", 2, 2)
    with pytest.raises(gspread.exceptions.APIError):
        ws.append_rows([["a"], ["b"], ["c"]])
    with pytest.raises(gspread.exceptions.APIError):
        ws.update(values=[["a", "b", "c"]], range_name="A1")


# --- command line ------------------------------------------------------------------------------

@pytest.fixture
def cli_world(tmp_path, monkeypatch, swov):
    data = tmp_path / "data"
    baselined(swov, Store(data), "swoveralls_full_72.json", 72)
    client = FakeClient()
    monkeypatch.setattr(cli.sheets_mod, "connect", lambda key: (client, BOT))
    monkeypatch.setenv("SHEET_SHARE_EMAIL", EMAIL)

    def go(*extra):
        return cli.main(["sheets", "--brand", "swoveralls", "--data-dir", str(data),
                         "--sheets-config", str(tmp_path / "none.yaml"), *extra])
    return client, go


def test_cli_updates_the_sheet_and_prints_its_address(cli_world, monkeypatch, capsys):
    client, go = cli_world
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    assert go() == 0
    out = capsys.readouterr().out
    assert client.created[0].url in out and "rows" in out


def test_cli_without_a_key_skips_with_a_visible_warning(cli_world, monkeypatch, capsys):
    client, go = cli_world
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert go() == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "::warning::" in out and client.created == []


def test_cli_dry_run_creates_nothing(cli_world, monkeypatch):
    client, go = cli_world
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    assert go("--dry-run") == 0 and client.created == []


def test_cli_reports_a_failure_with_a_github_error_annotation(cli_world, monkeypatch, capsys, tmp_path):
    client, go = cli_world
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(yaml.safe_dump({"sheets": {"swoveralls": "not-shared"}}))
    code = cli.main(["sheets", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"),
                     "--sheets-config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1 and "FAILED" in out and "::error::" in out


def test_cli_rejects_a_bad_key_without_printing_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "TOPSECRET-not-json")
    code = cli.main(["sheets", "--brand", "swoveralls", "--data-dir", str(tmp_path / "d"),
                     "--sheets-config", str(tmp_path / "none.yaml")])
    out = capsys.readouterr().out
    assert code == 1 and "not valid JSON" in out and "TOPSECRET" not in out


def test_cli_all_covers_every_brand(tmp_path, monkeypatch, swov, dude, defi):
    data = tmp_path / "data"
    for brand, fixture, count in ((swov, "swoveralls_full_72.json", 72),
                                  (dude, "duderobe_fetch_a.json", 25), (defi, "defi_snacks.json", 7)):
        baselined(brand, Store(data), fixture, count)
    client = FakeClient()
    monkeypatch.setattr(cli.sheets_mod, "connect", lambda key: (client, BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("SHEET_SHARE_EMAIL", EMAIL)
    assert cli.main(["sheets", "--all", "--data-dir", str(data),
                     "--sheets-config", str(tmp_path / "none.yaml")]) == 0
    assert len(client.created) == 3
    by_name = {sp.title.split()[0]: sp for sp in client.created}
    assert last_row(by_name["Swoveralls"].worksheet("Products")) == 73       # header + 72 products
    assert last_row(by_name["DudeRobe"].worksheet("Products")) == 26
    assert last_row(by_name["Defi"].worksheet("Products")) == 8


# --- a service account with no Drive storage (the real failure seen on GitHub) ---------------------

QUOTA_TEXT = "The user's Drive storage quota has been exceeded."


class FullDrive(FakeClient):
    """Google refuses to create files for this service account."""

    def __init__(self, text=QUOTA_TEXT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.text = text

    def create(self, title, folder_id=None):
        raise api_error(403, self.text)


@pytest.mark.parametrize("text", [QUOTA_TEXT, "storageQuotaExceeded"])
def test_a_service_account_with_no_storage_gets_the_real_explanation(dude, store, sleeps, text):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    client = FullDrive(text)
    report, error = one(dude, store, client)
    assert report is None and sleeps == []                          # not retried
    for part in (BOT, "your own Google Drive", "sheets.yaml", "SHEET_FOLDER_ID", "'duderobe'", "Editor"):
        assert part in error
    assert "Sheets API" not in error                                 # the old, misleading advice
    assert client.created == [] and store.load_sheet_info("duderobe") is None


def test_every_brand_gets_its_own_instruction_when_creation_is_refused(swov, dude, defi, store):
    for brand, fixture, count in ((swov, "swoveralls_full_72.json", 72),
                                  (dude, "duderobe_fetch_a.json", 25), (defi, "defi_snacks.json", 7)):
        baselined(brand, store, fixture, count)
    results = run([swov, dude, defi], store, FullDrive())
    assert [r[0] for r in results] == ["swoveralls", "duderobe", "defi_snacks"]
    for slug, report, error in results:
        assert report is None and f"'{slug}'" in error


def test_sheets_you_create_yourself_work_when_the_service_account_cannot_create_any(swov, dude, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    mine = [FakeSpreadsheet("sw-id", "Swoveralls Catalogue"), FakeSpreadsheet("dr-id", "DudeRobe Catalogue")]
    client = FullDrive(spreadsheets=mine)
    config = {"swoveralls": "sw-id", "duderobe": "dr-id"}
    results = run([swov, dude], store, client, config=config)
    assert all(r[2] is None for r in results)
    assert [len(read(tabs(sp)["Products"])[1]) for sp in mine] == [72, 25]
    assert client.created == [] and all(sp.shares == [] for sp in mine)


def test_other_permission_errors_still_give_the_sharing_advice(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    report, error = one(dude, store, FullDrive("The caller does not have permission"))
    assert "Editor" in error and "Drive API" in error and "your own Google Drive" not in error


def test_a_shared_drive_folder_is_used_for_created_spreadsheets(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    client = FakeClient()
    report, error = one(dude, store, client, folder_id="FOLDER1")
    assert error is None and client.create_folders == ["FOLDER1"]


def test_without_a_folder_nothing_is_passed(dude, store):
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    client = FakeClient()
    one(dude, store, client)
    assert client.create_folders == [None]


def test_folder_ids_and_urls():
    assert sheets.parse_folder_id("https://drive.google.com/drive/folders/F123_ab-c?usp=sharing") == "F123_ab-c"
    assert sheets.parse_folder_id("  F123  ") == "F123"
    assert sheets.parse_folder_id("") is None and sheets.parse_folder_id(None) is None


def test_cli_passes_the_folder_secret_through(cli_world, monkeypatch):
    client, go = cli_world
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("SHEET_FOLDER_ID", "https://drive.google.com/drive/folders/F123?usp=sharing")
    assert go() == 0 and client.create_folders == ["F123"]


def test_cli_shows_the_storage_explanation_and_fails_the_run(cli_world, monkeypatch, capsys):
    client, go = cli_world
    monkeypatch.setattr(cli.sheets_mod, "connect", lambda key: (FullDrive(), BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    assert go() == 1
    out = capsys.readouterr().out
    assert "your own Google Drive" in out and "::error::" in out


# --- last_updated stamps, missing_expected, refreshed wording ------------------------------------

LATER = "2026-10-19T12:00:00Z"
LATEST = "2026-10-19T15:00:00Z"


def stamps(ws, key_col):
    return {r[key_col]: r["last_updated"] for r in read(ws)[1]}


def test_rows_carry_the_time_they_were_last_changed(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    navy = pid(store, "swoveralls", "swoveralls-navy")
    resync(swov, store, "swoveralls_full_72.json", navy_price)
    one(swov, store, client, now=LATER)
    product_stamps = stamps(tabs(sp)["Products"], "product_id")
    assert product_stamps[navy] == LATER
    assert {v for k, v in product_stamps.items() if k != navy} == {NOW}
    variant_rows = read(tabs(sp)["Variants"])[1]
    changed = [r for r in variant_rows if r["last_updated"] == LATER]
    assert len(changed) == 1 and changed[0]["product_title"] == "Swoveralls - Navy" and changed[0]["price"] == 89.0
    assert {r["last_updated"] for r in variant_rows if r not in changed} == {NOW}


def test_a_run_with_nothing_new_leaves_every_stamp_alone(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    one(swov, store, client, now=LATER)
    one(swov, store, client, now=LATEST)
    assert set(stamps(tabs(sp)["Products"], "product_id").values()) == {NOW}
    assert {r["last_updated"] for r in read(tabs(sp)["Variants"])[1]} == {NOW}


def test_a_product_marked_removed_is_stamped_when_it_is_marked(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    sp = client.created[0]
    navy = pid(store, "swoveralls", "swoveralls-navy")
    resync(swov, store, "swoveralls_full_72.json",
           lambda raw: raw.__setitem__(slice(None), [p for p in raw if p["handle"] != "swoveralls-navy"]))
    one(swov, store, client, now=LATER)
    product_stamps = stamps(tabs(sp)["Products"], "product_id")
    assert product_stamps[navy] == LATER and {v for k, v in product_stamps.items() if k != navy} == {NOW}
    one(swov, store, client, now=LATEST)                      # still missing: no new stamp
    assert stamps(tabs(sp)["Products"], "product_id")[navy] == LATER


def test_an_older_sheet_without_the_new_columns_is_upgraded_in_place(defi, store):
    baselined(defi, store, "defi_snacks.json", 7)
    client = FakeClient()
    one(defi, store, client)
    sp = client.created[0]
    products, variants = tabs(sp)["Products"], tabs(sp)["Variants"]
    products.cells[(row_of(products, "product_id", pid(store, "defi_snacks", "4-flavor-variety-pack")),
                    col_of(products, "Notes"))] = "check allergens"
    delete_column(products, "missing_expected")
    delete_column(products, "last_updated")
    delete_column(variants, "last_updated")
    report, error = one(defi, store, client, now=LATER)
    assert error is None
    assert report.columns_added == {"Products": ["missing_expected", "last_updated"],
                                    "Variants": ["last_updated"]}
    header, rows = read(products)
    assert header[-2:] == ["missing_expected", "last_updated"]
    by = {r["handle"]: r for r in rows}
    assert by["dark-chocolate-crispy-superfood-bite"]["missing_expected"] == "nutrition_facts"
    assert by["shipping-protection"]["missing_expected"] == "allergens; nutrition_facts"
    assert by["4-flavor-variety-pack"]["Notes"] == "check allergens"
    assert {r["last_updated"] for r in rows} == {LATER}
    assert {r["last_updated"] for r in read(variants)[1]} == {LATER}


def test_a_cleared_stamp_is_refilled_without_touching_other_rows(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    products = tabs(client.created[0])["Products"]
    navy = pid(store, "swoveralls", "swoveralls-navy")
    products.cells[(row_of(products, "product_id", navy), col_of(products, "last_updated"))] = ""
    one(swov, store, client, now=LATER)
    got = stamps(products, "product_id")
    assert got[navy] == LATER and {v for k, v in got.items() if k != navy} == {NOW}


def test_the_changes_tab_has_no_stamp_column(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    assert "last_updated" not in read(tabs(client.created[0])["Changes"])[0]


def test_change_entries_name_the_variants_and_old_wording_is_refreshed(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    changes = tabs(client.created[0])["Changes"]

    def sell_out(raw):
        next(p for p in raw if p["handle"] == "swoveralls-midnight-black")["variants"][0]["available"] = False
    resync(swov, store, "swoveralls_full_72.json", sell_out)
    one(swov, store, client, now=LATER)
    (row,) = read(changes)[1]
    assert row["summary"] == "Sold out: XXS / Midnight Black"
    summary_cell = (row_of(changes, "event_id", row["event_id"]), col_of(changes, "summary"))
    changes.cells[summary_cell] = "1 variant(s) sold out, 0 restocked"          # the old wording
    report, _ = one(swov, store, client, now=LATEST)
    assert read(changes)[1][0]["summary"] == "Sold out: XXS / Midnight Black"
    assert report.cells_updated == 1 and report.events_added == 0


def test_dry_run_wording_reads_as_plain_english(swov, store):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    report, _ = one(swov, store, FakeClient(), dry_run=True)
    text = "\n".join(report.lines())
    assert "would create tabs: Products, Variants, Changes, Status" in text
    assert "would update 0 rows (0 cells), add 534 rows" in text and "would updated" not in text
    client = FakeClient()
    done, _ = one(swov, store, client)
    done_text = "\n".join(done.lines())
    assert "created tabs:" in done_text and "added 534 rows" in done_text and "would" not in done_text
