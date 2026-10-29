"""The Idea Bank, request and brief features as a person uses them: the sheet tabs, the three commands, and the workflows."""
from pathlib import Path

import pytest
import yaml

from contentintel import brief as BR
from contentintel import browser as B
from contentintel import cli
from contentintel import ideabank as IB
from contentintel import providers as P
from contentintel import sheets
from contentintel.store import Store

from conftest import BRANDS_DIR, BRIEFS_DIR
from fake_browser import FakeClaude
from fake_drive import EMAIL as ROBOT, ROOT, FakeDrive
from fake_http import FakeHttp, serper_web
from fake_sheets import FakeClient
from test_collectors import BROWSER_YAML, NOW as COLLECT_NOW, world
from test_ideabank import getter, record, row
from test_planner import raw_plan
from test_sheets import BOT, baselined, one

REPO = Path(BRANDS_DIR).parent
# The tests own their configuration, so they never depend on the settings in your real research.yaml or the spreadsheet ids in your real
# sheets.yaml. A research file that does not exist means "the built-in defaults", which the shipped file is tested to match.
RESEARCH_YAML = str(REPO / "tests" / "fixtures" / "use_the_defaults.yaml")
SHEETS_YAML = str(REPO / "tests" / "fixtures" / "sheets_blank.yaml")
GOOD = {"verdict": "ok"}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def seed_bank(store, ids=("A", "B"), scored=True):
    store.append_collected("swoveralls", record([row("instagram", i, handle="u", title=f"post {i}", text=f"caption {i}", url=f"https://www.instagram.com/p/{i}/",
                                                     media_url=f"https://cdn.example/{i}.jpg", likes=10) for i in ids]))
    bank = store.read_ideabank("swoveralls")
    IB.intake_collected(bank, store.read_collected("swoveralls"), IB.IdeaBankSettings(), COLLECT_NOW)
    if scored:
        for item in bank["items"].values():
            item.update({"state": "kept", "score": 80, "relevance": 80, "summary": "s", "how_to_use": "h", "risk": "none", "topic": "t", "scored_at": COLLECT_NOW,
                         "scoring_version": IB.SCORING_VERSION})
    store.write_ideabank("swoveralls", bank)
    return bank


def cells(sp, title):
    ws = next(w for w in sp.worksheets() if w.title == title)
    values = ws.get_all_values()
    return ws, values[0], [dict(zip(values[0], r)) for r in values[1:]]


# ---- the sheet -----------------------------------------------------------------------------------------------------------------------

def test_the_idea_bank_tab_appears_with_the_tools_columns_then_decision_and_notes(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    report, error = one(swov, store, client)
    (sp,) = client.created
    assert error is None and "Idea Bank" in [w.title for w in sp.worksheets()]
    _, header, rows = cells(sp, "Idea Bank")
    assert header == IB.IDEABANK_COLUMNS + ["Decision", "Notes"] and [r["idea_id"] for r in rows] == ["IB-0001", "IB-0002"]
    assert rows[0]["score"] == "80" and rows[0]["summary"] == "s" and rows[0]["Decision"] == "" and rows[0]["platform"] == "instagram"


def test_there_is_no_idea_bank_tab_until_there_is_something_in_it(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    assert "Idea Bank" not in [w.title for w in client.created[0].worksheets()]


def test_the_decision_column_gets_a_use_maybe_skip_list_once_when_the_tab_is_new(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    (batch,) = sp.batches
    (req,) = batch["requests"]
    rule = req["setDataValidation"]["rule"]
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == ["Use", "Maybe", "Skip"] and rule["condition"]["type"] == "ONE_OF_LIST" and rule["strict"] is False
    ws, header, _ = cells(sp, "Idea Bank")
    assert req["setDataValidation"]["range"] == {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 5000, "startColumnIndex": header.index("Decision"),
                                                 "endColumnIndex": header.index("Decision") + 1}
    one(swov, store, client)
    assert len(sp.batches) == 1                                                       # not repeated on later updates


def test_a_persons_decisions_and_notes_survive_every_update_while_the_tools_columns_change(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    ws, header, _ = cells(sp, "Idea Bank")
    ws._put(2, header.index("Decision") + 1, "Use")
    ws._put(2, header.index("Notes") + 1, "love this one")
    bank["items"]["IB-0001"].update({"score": 91, "summary": "now scored", "drive_folder_url": "https://drive.google.com/drive/folders/F1"})
    store.write_ideabank("swoveralls", bank)
    one(swov, store, client)
    _, _, rows = cells(sp, "Idea Bank")
    first = next(r for r in rows if r["idea_id"] == "IB-0001")
    assert first["Decision"] == "Use" and first["Notes"] == "love this one" and first["score"] == "91" and first["summary"] == "now scored"
    assert first["drive_folder"] == "https://drive.google.com/drive/folders/F1" and [r["idea_id"] for r in rows] == ["IB-0001", "IB-0002"]


def test_new_items_go_to_the_bottom_and_rows_are_matched_by_id_not_position(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store, ("A",))
    client = FakeClient()
    one(swov, store, client)
    (late,) = IB.intake_collected(bank, [record([row("instagram", "Z", title="late post", text="a late post")])], IB.IdeaBankSettings(), COLLECT_NOW)
    bank["items"][late].update({"state": "kept", "score": 70, "summary": "s"})
    store.write_ideabank("swoveralls", bank)
    one(swov, store, client)
    _, _, rows = cells(client.created[0], "Idea Bank")
    assert [r["idea_id"] for r in rows] == ["IB-0001", "IB-0002"]


def test_the_requests_tab_shows_each_request_with_its_plan_and_leaves_a_changes_column(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    req = {"id": "RQ-0001", "brand": "swoveralls", "text": "Father's Day gifting", "created": COLLECT_NOW, "status": "done", "allowed": ["meta_ads", "tiktok"],
           "region": "gb", "plan": {"tools": {"meta_ads": {"keywords": ["gift"]}, "tiktok": {"hashtags": ["dad"]}}}, "run": {"items_added": ["IB-0001"], "notes": ["collect: ok", "idea bank: 1"]}}
    store.write_request("swoveralls", req)
    client = FakeClient()
    one(swov, store, client)
    _, header, rows = cells(client.created[0], "Requests")
    assert header == sheets.REQUEST_COLUMNS + ["Changes"] and "formats" in header
    assert rows[0] == {"request_id": "RQ-0001", "created": COLLECT_NOW, "status": "done", "text": "Father's Day gifting", "region": "gb", "formats": "", "tools": "meta_ads, tiktok",
                       "plan": "ads: gift | tiktok: #dad", "items_added": "1", "run_notes": "collect: ok | idea bank: 1", "Changes": ""}


def test_the_briefs_tab_shows_each_brief_with_its_link_and_a_feedback_column(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    store.write_brief("swoveralls", {"id": "BR-0001", "brief": "swoveralls-new-audience", "ask": "Father's Day", "created": COLLECT_NOW, "status": "needs review",
                                     "items": ["IB-0001", "IB-0002"], "violations": [{"x": 1}], "models": ["claude-sonnet-5-5"], "drive_link": "https://drive.google.com/file/d/X/view"})
    client = FakeClient()
    one(swov, store, client)
    _, header, rows = cells(client.created[0], "Briefs")
    assert header == sheets.BRIEF_COLUMNS + ["Feedback"]
    assert rows[0]["items"] == "IB-0001, IB-0002" and rows[0]["violations"] == "1" and rows[0]["drive_link"].endswith("/X/view") and rows[0]["status"] == "needs review"


def decide(client, **choices):
    ws, header, rows = cells(client.created[0], "Idea Bank")
    for n, r in enumerate(rows, start=2):
        if r["idea_id"] in choices:
            ws._put(n, header.index("Decision") + 1, choices[r["idea_id"]])


def fetch(swov, store, client):
    return sheets.fetch_decisions(swov, store, key_json="{}", share_emails=["me@kelson.agency"], config={}, connect_fn=lambda key: (client, BOT))


def test_decisions_are_read_back_from_the_brands_own_sheet(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    decide(client, **{"IB-0001": "Use", "IB-0002": "skip"})
    assert fetch(swov, store, client) == {"IB-0001": {"decision": "use", "notes": ""}, "IB-0002": {"decision": "skip", "notes": ""}}


def test_a_sheet_with_no_idea_bank_tab_says_to_run_the_update_first(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    with pytest.raises(sheets.SheetsError, match="has no Idea Bank tab yet: run the Sheets update first"):
        fetch(swov, store, client)


def test_a_damaged_idea_bank_tab_is_a_sheets_error(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    ws, header, _ = cells(client.created[0], "Idea Bank")
    ws._put(1, header.index("Decision") + 1, "Verdict")
    with pytest.raises(sheets.SheetsError, match="no idea_id and Decision columns"):
        fetch(swov, store, client)


# ---- the commands -----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch, swov):
    for name in ("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID", "APIFY_TOKEN", "SHEET_SHARE_EMAIL", "SHEET_FOLDER_ID",
                 "SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES", "GEMINI_COOKIES"):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "data")
    baselined(swov, store, "swoveralls_full_72.json", 72)
    state = {"claude": FakeClaude(), "drive": FakeDrive(), "get": getter(), "factory": world(B.load_browser_settings(BROWSER_YAML)), "http": FakeHttp()}
    state["http"].on("POST", "https://google.serper.dev/search", (200, serper_web(3)))
    monkeypatch.setattr(cli, "Claude", lambda key, **kw: state["claude"])
    monkeypatch.setattr(cli, "_DRIVE_CONNECT", lambda key: (state["drive"], ROBOT))
    monkeypatch.setattr(cli, "_HTTP_GET", lambda *a, **k: state["get"](*a, **k))
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda browser_cfg, creds, debug: state["factory"])
    monkeypatch.setattr(cli.providers_mod, "build_providers", lambda environ: {"serper_dev": P.SerperDev("k", http=state["http"], sleep=lambda s: None)})
    monkeypatch.setattr(cli.sync_mod, "utc_now", lambda: "2026-10-20T09:00:00Z")

    def secrets(*names):
        values = {"ANTHROPIC_API_KEY": "k", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"client_email": "x", "private_key": "y"}', "IDEA_BANK_FOLDER_ID": ROOT,
                  "INSTAGRAM_COOKIES": "c", "TIKTOK_COOKIES": "c"}
        for n in names:
            monkeypatch.setenv(n, values[n])

    def run(*args):
        base = ["--data-dir", str(tmp_path / "data"), "--brands-dir", str(BRANDS_DIR), "--research-config", RESEARCH_YAML]
        return cli.main([args[0], *base, *args[1:]])
    return state, run, secrets, store, tmp_path


# -- ideabank ---------------------------------------------------------------------------------------------------------------------------

def test_the_drive_check_is_skipped_politely_without_the_folder_secret(env, capsys):
    state, run, secrets, store, _ = env
    assert run("ideabank", "--check") == 0
    assert "Idea Bank Drive check skipped: the IDEA_BANK_FOLDER_ID secret is not set." in capsys.readouterr().out


def test_the_drive_check_proves_the_folder_and_names_who_it_is_shared_with(env, capsys):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    assert run("ideabank", "--check") == 0
    assert f"Idea Bank Drive works: the folder 'Idea Bank' is in a Shared Drive, and a test folder was made and removed. Shared with {ROBOT}." in capsys.readouterr().out
    assert state["drive"].children(ROOT) == []


def test_a_service_account_that_cannot_delete_still_passes_the_check_and_is_told_what_to_tidy(env, capsys):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    state["drive"] = FakeDrive(delete_error=(404, {"error": {"message": "File not found: abc"}}))
    assert run("ideabank", "--check") == 0
    out = capsys.readouterr().out
    assert "Idea Bank Drive works: the folder 'Idea Bank' is in a Shared Drive and a test folder could be made in it, but the test folder could not be removed " in out
    assert "(Google Drive answered 404: File not found: abc.) Delete the folder called 'connection test (safe to delete)' by hand." in out
    assert "Saving files is not affected, because the tool never deletes anything." in out and f"Shared with {ROBOT}." in out


def test_a_folder_in_a_personal_drive_fails_the_check_with_the_fix(env, capsys):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    state["drive"] = FakeDrive(shared=False)
    assert run("ideabank", "--check") == 1
    assert "Error: The folder is in a personal My Drive, where a service account has no storage" in capsys.readouterr().out


def test_a_nonsense_folder_secret_is_an_error_with_the_fix(env, capsys, monkeypatch):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON")
    monkeypatch.setenv("IDEA_BANK_FOLDER_ID", "nope")
    assert run("ideabank", "--check") == 1
    assert "Error: IDEA_BANK_FOLDER_ID is not a Google Drive folder address or id" in capsys.readouterr().out


def test_the_ideabank_command_scores_and_saves_files_for_new_items(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    seed_bank(store, scored=False)
    store.write_ideabank("swoveralls", {"next": 1, "items": {}})
    state["claude"].json_answers = [{"items": [{"id": "IB-0001", "relevance": 88, "topic": "comfort", "summary": "s", "how_to_use": "h", "risk": "none"},
                                               {"id": "IB-0002", "relevance": 65, "topic": "gift", "summary": "s2", "how_to_use": "h2", "risk": "none"}]}]
    assert run("ideabank", "--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "2 new item(s) taken in" in out and "2 item(s) scored" in out and "files saved for 2 item(s)" in out
    bank = store.read_ideabank("swoveralls")
    assert [i["score"] for i in bank["items"].values()] == [88, 65] and all(i["drive_folder_url"] for i in bank["items"].values())
    assert state["drive"].tree()[:3] == ["Swoveralls/", "  IB-0001 - comfort/", "    IB-0001-image.jpg"]
    assert store.read_claude_usage() == {"2026-10": 3}                 # scoring, the hook lines and first tests, and a look at the saved pictures


def test_without_claude_or_drive_the_items_are_taken_in_but_wait_with_no_folder_or_row(env, capsys):
    state, run, secrets, store, _ = env
    store.append_collected("swoveralls", record([row("instagram", "A", title="post A", text="caption A", media_url="https://cdn.example/a.jpg")]))
    assert run("ideabank", "--all") == 0
    out = capsys.readouterr().out
    assert "Files will not be saved in Drive: the IDEA_BANK_FOLDER_ID secret is not set." in out
    assert "1 item(s) wait to be scored before they are shown or get a folder (no Claude key)" in out
    (item,) = store.read_ideabank("swoveralls")["items"].values()
    assert item["state"] == "new" and item["score"] is None


def test_a_dry_run_only_counts(env, capsys):
    state, run, secrets, store, tmp_path = env
    store.append_collected("swoveralls", record([row("instagram", "A")]))
    assert run("ideabank", "--brand", "swoveralls", "--dry-run") == 0
    assert "1 new item(s) taken in" in capsys.readouterr().out and not (tmp_path / "data" / "swoveralls" / "ideabank").exists()


def test_the_command_needs_to_know_which_brand(env, capsys):
    state, run, *_ = env
    assert run("ideabank") == 1 and "Error: choose --brand NAME, --all or --check" in capsys.readouterr().out


def test_the_idea_bank_can_be_switched_off(env, capsys, tmp_path):
    state, run, secrets, store, _ = env
    path = tmp_path / "off.yaml"
    path.write_text("ideabank:\n  enabled: false\n")
    store.append_collected("swoveralls", record([row("instagram", "A")]))
    assert cli.main(["ideabank", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(BRANDS_DIR), "--research-config", str(path), "--all"]) == 0
    assert "The Idea Bank is switched off" in capsys.readouterr().out and store.read_ideabank("swoveralls")["items"] == {}


# -- request ----------------------------------------------------------------------------------------------------------------------------

MANUAL = ["--tools", "manual", "--allow", "meta_ads,instagram,tiktok,web_search"]


def plan(run, state, *extra, text="Father's Day gifting ideas for dads"):
    state["claude"].json_answers = [raw_plan()]
    return run("request", "plan", "--brand", "swoveralls", "--text", text, *extra)


def test_planning_shows_the_plan_saves_it_and_searches_nothing(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state) == 0
    out = capsys.readouterr().out
    assert "Request RQ-0001 (swoveralls), region: worldwide" in out and "It will use at most:" in out and "Nothing has been searched yet" in out and "RQ-0001" in out.split("Nothing has")[1]
    assert store.read_request("swoveralls", "RQ-0001")["status"] == "planned" and state["factory"].opened == [] and state["http"].calls == []


def test_a_manual_plan_keeps_to_the_ticked_tools_and_a_region_is_stored_lower_case(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state, "--tools", "manual", "--allow", "tiktok,web_search", "--region", "GB", "--apify-extra") == 0
    req = store.read_request("swoveralls", "RQ-0001")
    assert set(req["plan"]["tools"]) == {"tiktok", "web_search"} and req["region"] == "gb" and req["apify_extra"] is True and req["allowed"] == ["tiktok", "web_search"]


@pytest.mark.parametrize("extra,message", [(["--region", "usa"], "--region must be a two-letter code such as us, or worldwide"),
                                           (["--tools", "manual"], "you chose to pick the tools yourself but ticked none")])
def test_a_bad_plan_request_is_an_error_with_the_fix(env, capsys, extra, message):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state, *extra) == 1 and f"Error: {message}" in capsys.readouterr().out and store.list_requests("swoveralls") == []


def test_planning_without_a_claude_key_says_so(env, capsys):
    state, run, *_ = env
    assert plan(run, state) == 1 and "Error: planning needs the ANTHROPIC_API_KEY secret" in capsys.readouterr().out


def test_planning_needs_a_sentence(env, capsys):
    state, run, secrets, *_ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state, text="hi") == 1 and "describe the request in a sentence or two" in capsys.readouterr().out


def planned_and_run(env, extra_run=(), providers=True):
    state, run, secrets, store, tmp_path = env
    secrets("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES")
    plan(run, state, *MANUAL)
    state["claude"].json_answers = [{"items": [{"id": f"IB-{n:04d}", "relevance": 70, "topic": f"idea {n}", "summary": "s", "how_to_use": "h", "risk": "none"} for n in range(1, 40)]}] * 3
    return run("request", "run", "--brand", "swoveralls", "--request", "RQ-0001", "--browser-config", BROWSER_YAML, *extra_run)


def test_running_a_request_collects_searches_and_fills_the_idea_bank(env, capsys):
    state, run, secrets, store, _ = env
    assert planned_and_run(env) == 0
    out = capsys.readouterr().out
    req = store.read_request("swoveralls", "RQ-0001")
    assert req["status"] == "done" and "Request RQ-0001 finished: done" in out and req["run"]["items_added"]
    assert any(n.startswith("collect: meta_ads ran") for n in req["run"]["notes"]) and any(n.startswith("web search: ") for n in req["run"]["notes"])
    bank = store.read_ideabank("swoveralls")
    assert all(i["request_id"] == "RQ-0001" and i["region"] == "worldwide" and i["occasion"] == "Father's Day" for i in bank["items"].values())
    assert {i["platform"] for i in bank["items"].values()} >= {"meta_ads", "web"} and "Run the Sheets update, review the Idea Bank tab" in out
    assert any("site:tiktok.com" in c["json"]["q"] for c in state["http"].calls) and all("gl" not in c["json"] for c in state["http"].calls)


def test_a_run_for_one_country_searches_that_country(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES")
    plan(run, state, *MANUAL, "--region", "gb")
    state["claude"].json_answers = []
    run("request", "run", "--brand", "swoveralls", "--request", "RQ-0001", "--browser-config", BROWSER_YAML)
    assert all(c["json"]["gl"] == "gb" for c in state["http"].calls) and state["http"].calls
    assert all("country=GB" in u for u in state["factory"].browsers["meta_ads"].opened if "&q=" in u)


def test_without_a_search_key_the_web_step_fails_and_the_rest_still_runs(env, capsys, monkeypatch):
    state, run, secrets, store, _ = env
    monkeypatch.setattr(cli.providers_mod, "build_providers", lambda environ: {})
    assert planned_and_run(env) == 0
    req = store.read_request("swoveralls", "RQ-0001")
    assert req["status"] == "partial" and any("web search failed: no search API key is set" in n for n in req["run"]["notes"]) and req["run"]["items_added"]


def test_changes_are_applied_before_the_run_and_shown(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES")
    plan(run, state, *MANUAL)
    revised = raw_plan()
    del revised["tools"]["tiktok"]
    state["claude"].json_answers = [revised]
    capsys.readouterr()
    run("request", "run", "--brand", "swoveralls", "--request", "RQ-0001", "--changes", "drop TikTok", "--browser-config", BROWSER_YAML)
    out = capsys.readouterr().out
    req = store.read_request("swoveralls", "RQ-0001")
    assert "The plan after your changes:" in out and "tiktok" not in req["plan"]["tools"] and req["changes"][0]["text"] == "drop TikTok"
    assert "tiktok" not in state["factory"].opened


def test_running_an_unknown_request_is_an_error(env, capsys):
    state, run, secrets, *_ = env
    secrets("ANTHROPIC_API_KEY")
    assert run("request", "run", "--brand", "swoveralls", "--request", "RQ-0042") == 1 and "Error: there is no request RQ-0042 for swoveralls" in capsys.readouterr().out
    assert run("request", "run", "--brand", "swoveralls") == 1 and "(none given)" in capsys.readouterr().out


def test_a_request_cannot_be_run_twice(env, capsys):
    state, run, secrets, store, _ = env
    planned_and_run(env)
    capsys.readouterr()
    assert run("request", "run", "--brand", "swoveralls", "--request", "RQ-0001", "--browser-config", BROWSER_YAML) == 1
    assert "Error: RQ-0001 has already run; make a new request to search again" in capsys.readouterr().out


def test_a_failing_bank_step_makes_the_request_partial_not_lost(env, capsys, monkeypatch):
    state, run, secrets, store, _ = env
    monkeypatch.setattr(cli.ib_mod, "run_ideabank", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bank down")))
    assert planned_and_run(env) == 0
    req = store.read_request("swoveralls", "RQ-0001")
    assert req["status"] == "partial" and "idea bank failed: bank down" in req["run"]["notes"] and req["run"]["items_added"] == []
    assert any(n.startswith("collect: meta_ads ran") for n in req["run"]["notes"])


# -- brief ------------------------------------------------------------------------------------------------------------------------------

def brief_args(*extra, brief="swoveralls-new-audience"):
    return ("brief", "--brand", "swoveralls", "--brief", brief, "--briefs-dir", str(BRIEFS_DIR), "--sheets-config", SHEETS_YAML, *extra)


def good_brief():
    return {"title": "Father's Day comfort", "objective": "o", "audience": "a", "key_message": "k", "tone": "t",
            "concepts": [{"name": "Switch", "format": "video", "hook": "Sweatpants that zip into overalls", "script": "s", "visual_direction": "v", "cta": "c",
                          "product": "swoveralls-navy", "references": ["IB-0001"]}], "dos": [], "donts": [], "deliverables": [], "notes_for_creators": ""}


@pytest.fixture
def ready(env, monkeypatch):
    state, run, secrets, store, tmp_path = env
    secrets("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    seed_bank(store)
    client = FakeClient()
    from test_sheets import run as run_sheets
    run_sheets([cli.load_brand("swoveralls", str(BRANDS_DIR))], store, client)
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (client, BOT))
    monkeypatch.setattr(BR, "catalogue_state", lambda slug, store, now: {"usable": True, "reasons": []})
    return state, run, store, client, tmp_path


def test_a_brief_is_written_from_the_rows_marked_use_and_saved(ready, capsys):
    state, run, store, client, tmp_path = ready
    decide(client, **{"IB-0001": "Use", "IB-0002": "Skip"})
    state["claude"].json_answers = [good_brief()]
    assert run(*brief_args("--ask", "Father's Day gifting")) == 0
    out = capsys.readouterr().out
    assert "Brief BR-0001 (ready): Father's Day comfort" in out and "built from 1 approved item(s); 0 rule problem(s) left" in out
    assert "saved in the repository as data/swoveralls/briefs/BR-0001.docx and in Drive: https://drive.google.com/file/d/" in out
    assert store.list_briefs("swoveralls")[0]["items"] == ["IB-0001"] and "BR-0001 - Father's Day comfort.docx" in " ".join(state["drive"].tree())


def test_with_no_approved_row_the_brief_says_how_to_approve_one(ready, capsys):
    state, run, store, client, _ = ready
    assert run(*brief_args("--ask", "Father's Day gifting")) == 1
    assert "Error: no Idea Bank item is approved: mark rows Use in the Decision column" in capsys.readouterr().out


def test_a_rule_problem_that_remains_is_printed(ready, capsys):
    state, run, store, client, _ = ready
    decide(client, **{"IB-0001": "Use"})
    bad = good_brief()
    bad["concepts"][0]["hook"] = "Sweatpants - wow!"
    state["claude"].json_answers = [bad, bad]
    assert run(*brief_args("--ask", "x for dads")) == 0
    out = capsys.readouterr().out
    assert "Brief BR-0001 (needs review)" in out and "error: concept 1 (Switch) hook: Contains exclamation point (1x)" in out and "note: " in out


def test_a_brief_for_another_brand_or_a_missing_rules_file_is_refused(ready, capsys):
    state, run, *_ = ready
    assert run(*brief_args("--ask", "x", brief="duderobe-swipe-to-robe")) == 1 and "belongs to duderobe, not swoveralls" in capsys.readouterr().out
    assert run(*brief_args("--ask", "x", brief="no-such-brief")) == 1 and "there is no rules file" in capsys.readouterr().out


def test_a_brief_needs_the_claude_and_google_secrets(env, capsys, monkeypatch):
    state, run, secrets, store, _ = env
    assert run(*brief_args("--ask", "x")) == 1 and "writing a brief needs the ANTHROPIC_API_KEY secret" in capsys.readouterr().out
    secrets("ANTHROPIC_API_KEY")
    assert run(*brief_args("--ask", "x")) == 1 and "the GOOGLE_SERVICE_ACCOUNT_JSON secret is needed" in capsys.readouterr().out


def test_without_a_drive_folder_the_brief_is_still_written_and_says_what_is_missing(ready, capsys, monkeypatch):
    state, run, store, client, _ = ready
    monkeypatch.delenv("IDEA_BANK_FOLDER_ID")
    decide(client, **{"IB-0001": "Use"})
    state["claude"].json_answers = [good_brief()]
    assert run(*brief_args("--ask", "Father's Day gifting")) == 0
    out = capsys.readouterr().out
    assert "Pictures will not be placed in the brief and the file will not be saved in Drive: the IDEA_BANK_FOLDER_ID secret is not set." in out and "and in Drive" not in out


# ---- the workflows ----------------------------------------------------------------------------------------------------------------------

def load(name):
    return yaml.safe_load(open(f".github/workflows/{name}.yml", encoding="utf-8"))


def step(wf, job, name_part):
    (found,) = [s for s in wf["jobs"][job]["steps"] if name_part in s.get("name", "")]
    return found


def test_the_request_workflow_has_the_inputs_a_person_needs():
    inputs = load("request")[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"brand", "action", "idea", "request_id", "changes", "tools", "use_meta_ads", "use_instagram", "use_tiktok", "use_web_search",
                           "use_deep_research", "apify_extra", "region", "formats"}
    assert inputs["action"]["options"] == ["plan", "run"] and inputs["action"]["default"] == "plan" and inputs["tools"]["options"] == ["auto", "manual"]
    assert inputs["use_deep_research"]["default"] is False and inputs["use_web_search"]["default"] is True and inputs["region"]["default"] == ""


def test_the_request_workflow_passes_text_by_environment_never_into_the_script():
    s = step(load("request"), "request", "Plan or run the request")
    assert s["env"]["IDEA"] == "${{ inputs.idea }}" and s["env"]["CHANGES"] == "${{ inputs.changes }}" and "${{ inputs." not in s["run"]
    assert '--text "$IDEA"' in s["run"] and '--changes "$CHANGES"' in s["run"] and '--request "$REQUEST_ID"' in s["run"]


def test_the_request_workflow_turns_ticked_boxes_into_the_allowed_list_and_passes_region_and_apify():
    run = step(load("request"), "request", "Plan or run the request")["run"]
    for tool in ("meta_ads", "instagram", "tiktok", "web_search", "deep_research"):
        assert f'allow="$allow,{tool}"' in run
    assert '--allow "${allow#,}"' in run and '--region "$REGION"' in run and "--apify-extra" in run and '--tools "$TOOLS"' in run


def test_the_request_workflow_has_every_secret_the_tools_need():
    env = step(load("request"), "request", "Plan or run the request")["env"]
    for name in ("INSTAGRAM_COOKIES", "TIKTOK_COOKIES", "GEMINI_COOKIES", "ANTHROPIC_API_KEY", "APIFY_TOKEN", "SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY",
                 "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID"):
        assert env[name] == "${{ secrets." + name + " }}", name


def test_the_request_workflow_installs_the_browser_only_to_run_and_updates_the_sheet_after():
    wf = load("request")
    names = [s.get("name", "") for s in wf["jobs"]["request"]["steps"]]
    assert step(wf, "request", "Install the browser")["if"] == "inputs.action == 'run'"
    assert names.index("Plan or run the request") < names.index("Update Google Sheets") < names.index("Save the data")
    assert step(wf, "request", "Update Google Sheets")["continue-on-error"] is True and step(wf, "request", "Add Deep Research reports")["if"] == "inputs.action == 'run'"
    assert "steps.request.outcome == 'failure'" in step(wf, "request", "Show the run as red")["if"]


def test_the_brief_workflow_has_its_inputs_and_secrets():
    wf = load("brief")
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"brand", "brief", "ask", "request_id", "include_maybe"} and inputs["include_maybe"]["default"] is False and inputs["ask"]["required"] is True
    s = step(wf, "brief", "Write the brief")
    assert s["env"]["ASK"] == "${{ inputs.ask }}" and "${{ inputs." not in s["run"] and '--ask "$ASK"' in s["run"] and "--include-maybe" in s["run"] and '--request "$REQUEST_ID"' in s["run"]
    for name in ("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID", "SHEET_SHARE_EMAIL", "SHEET_FOLDER_ID"):
        assert s["env"][name] == "${{ secrets." + name + " }}", name


def test_the_brief_workflow_updates_the_sheet_after_and_saves_the_file_and_shows_failures():
    wf = load("brief")
    names = [s.get("name", "") for s in wf["jobs"]["brief"]["steps"]]
    assert names.index("Write the brief") < names.index("Update Google Sheets") < names.index("Save the data")
    assert "steps.brief.outcome == 'failure'" in step(wf, "brief", "Show the run as red")["if"]


def test_the_collect_workflow_adds_to_the_idea_bank_before_the_sheet_update():
    wf = load("collect")
    names = [s.get("name", "") for s in wf["jobs"]["collect"]["steps"]]
    assert names.index("Add new items to the Idea Bank") < names.index("Update Google Sheets")
    s = step(wf, "collect", "Add new items to the Idea Bank")
    assert s["if"] == "inputs.mode == 'collect' || inputs.mode == '' || inputs.mode == 'tidy'" and s["continue-on-error"] is True and "python -m contentintel ideabank" in s["run"]
    assert {"ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID"} <= set(s["env"])
    check = step(wf, "collect", "Check the Idea Bank Drive folder")
    assert check["if"] == "inputs.mode == 'check'" and "ideabank --check" in check["run"]


def test_a_failed_idea_bank_step_alerts_and_shows_red():
    wf = load("collect")
    for name in ("Alert if collection needs attention", "Show the run as red when attention is needed"):
        condition = step(wf, "collect", name)["if"]
        assert "steps.ideabank.outcome == 'failure'" in condition and "steps.ideabankcheck.outcome == 'failure'" in condition


def test_the_new_secret_and_every_new_command_are_documented():
    readme = open("README.md", encoding="utf-8").read()
    for needle in ("IDEA_BANK_FOLDER_ID", "Idea Bank", "Request mode", "Briefs from approved items only", "Deep Research check", "Shared Drive", "Decision"):
        assert needle in readme, needle


def test_the_request_workflow_caches_and_installs_the_browser_only_for_a_run():
    wf = load("request")
    assert step(wf, "request", "Remember the downloaded browser")["if"] == "inputs.action == 'run'" == step(wf, "request", "Install the browser")["if"]


def test_these_tests_never_read_the_real_sheets_or_research_files():
    assert not Path(RESEARCH_YAML).exists() and Path(SHEETS_YAML).is_file() and SHEETS_YAML.endswith("tests/fixtures/sheets_blank.yaml")
    assert yaml.safe_load(open(SHEETS_YAML, encoding="utf-8"))["sheets"] == {"swoveralls": "", "duderobe": "", "defi_snacks": ""}


def test_the_ideabank_command_removes_items_from_skipped_brands(env, capsys):
    state, run, secrets, store, tmp_path = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    seed_bank(store, ("A", "B"))
    bank = store.read_ideabank("swoveralls")
    bank["items"]["IB-0001"]["handle"] = "Cotopaxi"
    store.write_ideabank("swoveralls", bank)
    assert run("ideabank", "--brand", "swoveralls") == 0
    assert "1 from brands you skip" in capsys.readouterr().out
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "removed"


def test_a_request_run_passes_the_skipped_brands_to_the_idea_bank(env, monkeypatch):
    state, run, secrets, store, _ = env
    seen = {}
    real = cli.ib_mod.run_ideabank

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(cli.ib_mod, "run_ideabank", spy)
    planned_and_run(env)
    assert sorted(seen["skip_brands"]) == ["Allbirds", "Cotopaxi", "Dollar Shave Club", "Liquid Death"]


def test_planning_with_formats_stores_them_shows_them_and_tells_claude(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state, "--formats", "UGC, AI content and posts") == 0
    out = capsys.readouterr().out
    assert "  Formats wanted: ugc, ai, static" in out
    assert store.read_request("swoveralls", "RQ-0001")["formats"] == ["ugc", "ai", "static"]
    assert "Formats wanted: ugc = UGC: real customers" in state["claude"].json_calls[0][1]


def test_an_unknown_format_is_an_error_with_the_list_and_nothing_is_saved(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state, "--formats", "ugc, blogs") == 1
    out = capsys.readouterr().out
    assert "Error: 'blogs' is not a content format I know. Use any of: ugc (" in out and store.list_requests("swoveralls") == []


def test_a_request_without_formats_has_none(env, capsys):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY")
    assert plan(run, state) == 0
    assert store.read_request("swoveralls", "RQ-0001")["formats"] == [] and "Formats wanted" not in capsys.readouterr().out


def test_a_request_run_searches_for_its_formats_and_scores_with_them(env, monkeypatch):
    state, run, secrets, store, _ = env
    secrets("ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID", "INSTAGRAM_COOKIES", "TIKTOK_COOKIES")
    plan(run, state, *MANUAL, "--formats", "ugc, ai")
    state["claude"].json_answers = [{"items": []}] * 3
    seen = {}
    real = cli.ib_mod.run_ideabank

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(cli.ib_mod, "run_ideabank", spy)
    assert run("request", "run", "--brand", "swoveralls", "--request", "RQ-0001", "--browser-config", BROWSER_YAML) == 0
    assert seen["request"]["formats"] == ["ugc", "ai"]
    asked = [c["json"]["q"] for c in state["http"].calls]
    assert any("UGC ad examples apparel brand" in q for q in asked) and any("AI generated ad examples fashion brand" in q for q in asked)


# ---- the saved files: audit, videos, logins -----------------------------------------------------------------------------------------------

def _saved_in_drive(state, store, *ids):
    from contentintel.drive import DriveClient as _Client
    from test_ideabank import S as _S, getter as _getter, kept_bank as _kept
    client = _Client(state["drive"], ROOT, ROBOT, sleep=lambda s: None)
    bank = _kept(*ids)
    ib_mod = cli.ib_mod
    ib_mod.save_assets(bank, list(bank["items"]), "Swoveralls", client, _getter(), _S)
    store.write_ideabank("swoveralls", bank)
    return bank


def test_the_ideabank_command_checks_every_saved_file_again_when_asked(env, capsys):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    bank = _saved_in_drive(state, store, "A", "B")
    bad = bank["items"]["IB-0001"]["assets"][0]["drive_id"]
    state["drive"].files[bad]["data"] = state["drive"].files[bad]["data"][:300]
    assert run("ideabank", "--brand", "swoveralls", "--audit") == 0
    out = capsys.readouterr().out
    assert "swoveralls: audit: 2 saved file(s) downloaded from Drive and checked: 1 complete" in out and "1 broken" in out
    assert "IB-0001-image.jpg is broken" in out


def test_the_ideabank_command_does_not_download_files_back_unless_asked(env, capsys):
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    _saved_in_drive(state, store, "A")
    before = len([c for c in state["drive"].calls if c["params"].get("alt") == "media"])
    assert run("ideabank", "--brand", "swoveralls") == 0
    assert "audit" not in capsys.readouterr().out and len([c for c in state["drive"].calls if c["params"].get("alt") == "media"]) == before


def test_the_video_download_is_used_when_yt_dlp_is_there_and_skipped_when_it_is_not(monkeypatch):
    monkeypatch.setattr(cli, "_VIDEO_RUNNER", None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli._video_runner() is None
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yt-dlp")
    assert callable(cli._video_runner())                                  # yt-dlp, with the browser as a second route when there is a login
    monkeypatch.setattr(cli, "_VIDEO_RUNNER", "fake")
    assert cli._video_runner() == "fake"


def test_the_instagram_and_tiktok_logins_are_taken_from_the_secrets(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_COOKIES", "ig")
    monkeypatch.setenv("TIKTOK_COOKIES", "tt")
    assert cli._cookie_secrets() == {"instagram": "ig", "tiktok": "tt"}
    monkeypatch.delenv("INSTAGRAM_COOKIES")
    monkeypatch.delenv("TIKTOK_COOKIES")
    assert cli._cookie_secrets() == {"instagram": "", "tiktok": ""}


def test_the_ideabank_command_saves_real_videos_for_kept_tiktok_pages(env, capsys, monkeypatch):
    from media_fixtures import tiny_mp4
    from pathlib import Path as _Path
    state, run, secrets, store, _ = env
    secrets("GOOGLE_SERVICE_ACCOUNT_JSON", "IDEA_BANK_FOLDER_ID")
    bank = _saved_in_drive(state, store, "A")
    bank["items"]["IB-0001"].update({"platform": "tiktok", "url": "https://www.tiktok.com/@c/video/9"})
    store.write_ideabank("swoveralls", bank)

    def runner(url, dest, cookies, max_mb, timeout):
        (_Path(dest) / "video.mp4").write_bytes(tiny_mp4(seconds=15))
        return _Path(dest) / "video.mp4", ""
    monkeypatch.setattr(cli, "_VIDEO_RUNNER", runner)
    assert run("ideabank", "--brand", "swoveralls") == 0
    assert "real videos saved for 1 item(s)" in capsys.readouterr().out
    item = store.read_ideabank("swoveralls")["items"]["IB-0001"]
    assert [a["kind"] for a in item["assets"]] == ["image", "video"]
