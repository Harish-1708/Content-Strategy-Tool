"""The Sessions and Session Reads tabs: a readable copy of the stored-login-session record, built from the tool's own JSON files and written by the normal sheet sync."""
import json

import pytest

from contentintel import session_live as SL
from contentintel import sessions as S
from contentintel import sheets
from contentintel.store import Store

from fake_sheets import FakeClient, read, row_of

NOW = "2026-11-13T10:00:00Z"
BOT = "bot@kelson-tool.iam.gserviceaccount.com"
X_REASON = ("X (Twitter) showed a bot-check or challenge page to this runner, so the session itself could not be judged. This is the platform blocking the runner, not a bad session. "
            "The last-resort twscrape check could not judge it either: NoAccountError: No account available for queue UserByRestId; twscrape logged: Unknown error. Account timeouted for 15 minutes. "
            "Err: HttpStatusError 403; username=session-check")
X_EVIDENCE = {"status": 403, "landed": "https://x.com/home", "title": "Just a moment...", "fetched_with": "browser", "html_chars": 4210, "text_chars": 180, "logged_in_found": [],
              "logged_in_missing": ["isloggedin", "sidebarcolumn"], "logged_in_text_found": [], "logged_in_text_missing": [], "logged_out_in_text": ["sign in to x"], "password_field": False,
              "worker_error": "PLATFORM_BLOCKED: x.com still refused after 1 attempt(s)", "cookies_sent": ["auth_token", "ct0"], "cookies_back": ["__cf_bm"]}


@pytest.fixture()
def ledger(tmp_path):
    led = SL.SessionLedger(tmp_path / "sessions_health.json")
    led.record("x", SL.BLOCKED, X_REASON, NOW, evidence=X_EVIDENCE, expires_on="2027-11-12", days_left=364, twscrape={"state": "unreachable", "detail": "NoAccountError: ... HttpStatusError 403"})
    led.record("reddit", SL.ACCEPTED, "the platform's own page showed a logged-in state", NOW, evidence={"status": 200, "landed": "https://www.reddit.com/settings/", "fetched_with": "browser", "html_chars": 91000,
               "text_chars": 2100, "logged_in_found": [], "logged_in_missing": ["logout"], "logged_in_text_found": ["expand user menu", "open profile menu"], "logged_in_text_missing": [],
               "logged_out_in_text": [], "password_field": False, "worker_error": ""}, expires_on="2027-05-12", days_left=180)
    return led


def test_the_sessions_tab_has_one_row_per_platform_with_the_whole_reason_the_dates_and_the_evidence(ledger):
    rows = SL.sessions_rows(ledger)
    assert list(rows) == list(S.PLATFORMS) and all(set(r) == set(SL.SESSIONS_COLUMNS) for r in rows.values())
    x = rows["x"]
    assert x["label"] == "X (Twitter)" and x["state"] == "BLOCKED" and x["reason"] == X_REASON and "HttpStatusError 403" in x["reason"] and x["checked_at"] == NOW and x["expires_on"] == "2027-11-12" and x["days_left"] == 364
    assert x["landed"] == "https://x.com/home" and x["http_status"] == 403 and x["fetched_with"] == "browser" and x["html_chars"] == 4210 and x["text_chars"] == 180 and x["password_field"] == "no"
    assert x["logged_in_markers_found"] == "" and x["logged_in_markers_missing"] == "isloggedin, sidebarcolumn" and x["logged_out_wording"] == "sign in to x" and x["proved_on"] == "" and x["proved_via"] == ""
    assert x["worker_error"].startswith("PLATFORM_BLOCKED: x.com still refused") and x["twscrape"] == "unreachable: NoAccountError: ... HttpStatusError 403"
    reddit = rows["reddit"]
    assert reddit["state"] == "ACCEPTED" and reddit["proved_on"] == NOW and reddit["proved_via"] == "browser" and reddit["logged_in_markers_found"] == "expand user menu, open profile menu" and reddit["twscrape"] == ""
    never = rows["instagram"]
    assert never["state"] == "NOT CHECKED" and never["reason"] == "no check has been run for this platform yet" and never["checked_at"] == never["proved_on"] == never["landed"] == never["password_field"] == ""
    assert "auth_token" not in json.dumps(rows) and "cookies_sent" not in json.dumps(rows)                              # cookie names are in the JSON record only: the sheet is for reading


def test_the_session_reads_tab_has_a_stable_row_per_read_and_compare():
    log = SL.ReadLog(None)
    log.items = []
    read_entry = {"at": "2026-11-13T09:00:00Z", "kind": "read", "platform": "x", "url": "https://x.com/swoveralls", "route": "primary", "skipped": False, "reason": "", "quality": "real text", "chars": 1362,
                  "title": "SWOVERALLS", "landed": "https://x.com/swoveralls", "starts": "SWOVERALLS 743 posts", "trail": [["primary", "ok", ""]]}
    nothing = {"at": "2026-11-13T09:05:00Z", "kind": "read", "platform": "instagram", "url": "https://www.instagram.com/swoveralls/", "route": "none", "skipped": False, "reason": "", "quality": "", "chars": 0,
               "title": "", "landed": "", "starts": "", "trail": [["primary", "UPSTREAM_BROKEN", "no readable text"], ["session", "off", "no live proof"], ["discovery", "empty", ""]]}
    compare = {"at": "2026-11-13T09:10:00Z", "kind": "compare", "platform": "tiktok", "url": "https://www.tiktok.com/@swoveralls", "verdict": "the public read works and the session read failed",
               "public": {"ok": True, "chars": 184, "quality": "real text", "title": "TikTok", "landed": "https://www.tiktok.com/@swoveralls", "starts": "# Swoveralls 405 Following", "error": ""},
               "session": {"used": True, "ok": False, "chars": 38, "quality": "challenge page", "title": "TikTok", "landed": "", "error": "no readable text"}}
    off = {"at": "2026-11-13T09:15:00Z", "kind": "compare", "platform": "x", "url": "https://x.com/swoveralls", "verdict": "only the public read was made",
           "public": {"ok": False, "chars": 0, "quality": "", "title": "", "landed": "", "error": "PLATFORM_BLOCKED: refused"}, "session": {"used": False, "reason": "no live proof yet for x"}}
    log.items = [read_entry, nothing, compare, off]
    rows = SL.reads_rows(log)
    assert len(rows) == 4 and all(set(r) == set(SL.READS_COLUMNS) for r in rows.values()) and all(k == r["read_id"] for k, r in rows.items())
    assert SL.read_id(read_entry) == SL.read_id(dict(read_entry)) and SL.read_id(read_entry) != SL.read_id(dict(read_entry, url="https://x.com/other")) and SL.read_id(read_entry).startswith("2026-11-13T09:00:00Z|x|read|")
    r1, r2, r3, r4 = list(rows.values())
    assert (r1["route"], r1["quality"], r1["chars"], r1["title"], r1["starts"], r1["session_used"], r1["verdict"]) == ("primary", "real text", 1362, "SWOVERALLS", "SWOVERALLS 743 posts", "", "")
    assert r2["route"] == "none" and r2["chars"] == 0 and r2["reason"] == "primary: UPSTREAM_BROKEN (no readable text) | session: off (no live proof) | discovery: empty"
    assert r3["kind"] == "compare" and r3["verdict"] == "the public read works and the session read failed" and r3["public_chars"] == 184 and r3["public_quality"] == "real text" and r3["session_used"] == "yes"
    assert r3["session_chars"] == 38 and r3["session_quality"] == "challenge page" and r3["starts"] == "# Swoveralls 405 Following" and r3["chars"] == "" and r3["landed"] == "https://www.tiktok.com/@swoveralls"
    assert r4["session_used"] == "no" and r4["session_chars"] == "" and r4["session_quality"] == "" and r4["reason"] == "no live proof yet for x" and r4["public_chars"] == 0


def test_a_tab_is_offered_only_when_its_record_has_something_in_it(ledger, tmp_path):
    empty_log = SL.ReadLog(tmp_path / "none.json")
    assert SL.sheet_tabs(SL.SessionLedger(tmp_path / "missing.json"), empty_log) == []
    only_health = SL.sheet_tabs(ledger, empty_log)
    assert [t["title"] for t in only_health] == ["Sessions"] and only_health[0]["columns"] == SL.SESSIONS_COLUMNS and only_health[0]["key_col"] == "platform" and only_health[0]["starter_manual"] == []
    log = SL.ReadLog(tmp_path / "reads.json")
    log.add({"kind": "read", "platform": "x", "url": "https://x.com/a"}, NOW)
    both = SL.sheet_tabs(ledger, log)
    assert [t["title"] for t in both] == ["Sessions", "Session Reads"] and both[1]["columns"] == SL.READS_COLUMNS and both[1]["key_col"] == "read_id" and len(both[1]["rows_by_key"]) == 1
    only_reads = SL.sheet_tabs(SL.SessionLedger(tmp_path / "missing.json"), log)
    assert [t["title"] for t in only_reads] == ["Session Reads"]


def test_the_normal_sheet_sync_writes_both_tabs_with_numbers_as_numbers_and_updates_rows_instead_of_repeating_them(swov, tmp_path, ledger):
    store = Store(tmp_path / "data")
    shared = tmp_path / "data" / "_shared"
    shared.mkdir(parents=True)
    ledger.path = shared / "sessions_health.json"
    ledger.save()
    log = SL.ReadLog(shared / "sessions_reads.json")
    log.add({"kind": "read", "platform": "x", "url": "https://x.com/swoveralls", "route": "primary", "quality": "real text", "chars": 1362, "title": "SWOVERALLS", "landed": "https://x.com/swoveralls",
             "starts": "SWOVERALLS 743 posts", "skipped": False, "reason": "", "trail": [["primary", "ok", ""]]}, "2026-11-13T09:00:00Z")
    log.save()
    client = FakeClient()
    (slug, report, error), = sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now=NOW, connect_fn=lambda key: (client, BOT))
    assert error is None
    (sp,) = client.created
    titles = [w.title for w in sp.worksheets()]
    assert "Sessions" in titles and "Session Reads" in titles
    ws = {w.title: w for w in sp.worksheets()}
    header, rows = read(ws["Sessions"])
    assert header[:len(SL.SESSIONS_COLUMNS)] == SL.SESSIONS_COLUMNS and [r["platform"] for r in rows] == list(S.PLATFORMS)
    x = rows[1]
    assert x["state"] == "BLOCKED" and x["days_left"] == 364 and x["http_status"] == 403 and x["html_chars"] == 4210 and isinstance(x["days_left"], int) and "HttpStatusError 403" in x["reason"]
    assert next(r for r in rows if r["platform"] == "instagram")["state"] == "NOT CHECKED" and next(r for r in rows if r["platform"] == "reddit")["proved_on"] == NOW
    header, reads = read(ws["Session Reads"])
    assert len(reads) == 1 and reads[0]["chars"] == 1362 and reads[0]["route"] == "primary" and reads[0]["quality"] == "real text" and reads[0]["last_updated"] == NOW
    ledger.record("x", SL.ACCEPTED, "changed", "2026-11-14T10:00:00Z")
    ledger.save()
    log.add({"kind": "read", "platform": "tiktok", "url": "https://www.tiktok.com/@swoveralls", "route": "primary", "quality": "real text", "chars": 184, "title": "TikTok"}, "2026-11-14T09:00:00Z")
    log.save()
    sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now="2026-11-14T11:00:00Z", connect_fn=lambda key: (client, BOT))
    ws = {w.title: w for w in sp.worksheets()}
    header, rows = read(ws["Sessions"])
    assert len(rows) == 8 and next(r for r in rows if r["platform"] == "x")["state"] == "ACCEPTED"
    header, reads = read(ws["Session Reads"])
    assert len(reads) == 2 and sorted(r["platform"] for r in reads) == ["tiktok", "x"] and row_of(ws["Session Reads"], "platform", "x") >= 2


def test_a_sheet_sync_with_no_session_record_gains_no_session_tab_and_a_broken_record_never_stops_the_sheet(swov, tmp_path):
    store = Store(tmp_path / "data")
    client = FakeClient()
    (slug, report, error), = sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now=NOW, connect_fn=lambda key: (client, BOT))
    assert error is None and not [w.title for w in client.created[0].worksheets() if w.title.startswith("Session")]
    shared = tmp_path / "data" / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "sessions_health.json").write_text("not json", encoding="utf-8")
    (shared / "sessions_reads.json").write_text("{\"a\": 1}", encoding="utf-8")
    (slug, report, error), = sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now=NOW, connect_fn=lambda key: (client, BOT))      # the same sheet, now with unreadable records
    assert error is None and not [w.title for w in client.created[0].worksheets() if w.title.startswith("Session")]


def test_the_session_tabs_step_never_stops_the_sheet_when_it_fails(swov, tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setattr(SL, "sheet_tabs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    client = FakeClient()
    (slug, report, error), = sheets.run_sheets([swov], store, key_json="{}", share_emails=["me@kelson.agency"], config={}, now=NOW, connect_fn=lambda key: (client, BOT))
    assert error is None and any("Sessions tabs skipped: boom" in n for n in report.notes) and "Products" in [w.title for w in client.created[0].worksheets()]


def test_the_sessions_workflow_updates_the_sheets_through_the_sheets_workflow_and_never_holds_the_sheets_key():
    from conftest import ROOT
    text = (ROOT / ".github" / "workflows" / "sessions.yml").read_text(encoding="utf-8")
    assert "sheets:\n        description:" in text and "type: boolean\n        default: true" in text and "  actions: write" in text
    step = text[text.index("- name: Update the Google Sheets with what was found"):text.index("- name: Show the run as red")]
    assert "inputs.sheets" in step and "inputs.what == 'check' || inputs.what == 'read' || inputs.what == 'compare'" in step
    assert 'gh workflow run sheets.yml --ref "$GITHUB_REF_NAME" -f brand=all -f dry_run=false' in step and "GH_TOKEN: ${{ github.token }}" in step
    assert text.index("- name: Save what was found") < text.index("- name: Update the Google Sheets") < text.index("- name: Show the run as red")      # the data is committed before the Sheets run starts
    assert "GOOGLE" not in text.upper().replace("GOOGLE SHEETS", "") and "SERVICE_ACCOUNT" not in text.upper()
