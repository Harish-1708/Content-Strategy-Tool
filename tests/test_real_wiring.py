"""The first live run of update-30 showed two things the tests had hidden. The `ideabank` command never defined --sheets-config, so the rescue
reader failed with an AttributeError that was quietly turned into a note; and it never defined --debug-dir, so both new browser features (the
Meta link refresh and the video browser route) called the real browser with no debug folder and failed with a TypeError before it started. The
tests had used stand-ins for the command's arguments and for the browser. These tests use neither: the real parser, the real startup code (with only
Playwright itself faked) and the real command."""
import pytest

from contentintel import browser as B
from contentintel import cli
from contentintel import sheets
from contentintel import ideabank as IB
from contentintel.store import Store

from fake_sheets import FakeClient
from test_collectors import NOW
from test_idea_commands import BOT, cells, decide, seed_bank
from test_ideabank import S
from test_link_refresh import ads
from test_playwright_sharing import FakePlaywright
from test_sheets import baselined, navy_price, one, resync


@pytest.fixture(autouse=True)
def only_playwright_is_fake(monkeypatch):
    FakePlaywright.running, FakePlaywright.log = False, []
    monkeypatch.setattr(B, "_start_playwright", FakePlaywright.start)
    monkeypatch.setattr(B, "_SHARED", B._SharedPlaywright())
    monkeypatch.setattr(B.time, "sleep", lambda seconds: None)
    yield
    assert FakePlaywright.running is False and B._SHARED.users == 0


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def parsed(*extra):
    return cli.build_parser().parse_args(["ideabank", "--brand", "swoveralls", *extra])


def test_the_ideabank_command_has_every_option_its_helpers_read():
    args = parsed()
    assert (args.sheets_config, args.debug_dir, args.browser_config, args.brands_dir) == ("sheets.yaml", "debug", "browser.yaml", "brands")
    assert args.audit is False and args.dry_run is False
    assert parsed("--debug-dir", "elsewhere", "--sheets-config", "other.yaml").debug_dir == "elsewhere"


def test_the_browser_for_ad_pages_really_starts_with_the_commands_own_arguments(monkeypatch):
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", None)
    factory = cli._ad_browser_factory(parsed())
    with factory("meta_ads") as browser:
        assert isinstance(browser, B.PlaywrightBrowser)
    assert FakePlaywright.log == ["start", "close", "stop"]


def test_the_browser_for_videos_really_starts_too_and_needs_no_debug_option_to_do_so(monkeypatch):
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", None)
    monkeypatch.setenv("TIKTOK_COOKIES", '[{"name": "a", "value": "b", "domain": ".tiktok.com"}]')

    class Bare:                                         # a command with no debug option at all still gets a folder to write to
        browser_config = "browser.yaml"
    for args in (parsed(), Bare()):
        with cli._video_browser_factory(args)("tiktok") as browser:
            assert isinstance(browser, B.PlaywrightBrowser)
        with cli._ad_browser_factory(args)("meta_ads") as browser:
            assert isinstance(browser, B.PlaywrightBrowser)


def test_the_meta_refresh_runs_end_to_end_through_the_real_browser_startup(monkeypatch):
    """Before: 'the browser could not start (TypeError)'. Now the browser starts, the page is read, and an ad that is gone is said to be gone."""
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", None)
    bank = ads("A")
    found, lines = IB.refresh_meta_links(bank, cli._ad_browser_factory(parsed()), S, NOW)
    assert found == 0 and not any("could not start" in line for line in lines)
    assert lines == ["1 expired Meta ad(s) could not be refreshed: they are no longer shown in the Ad Library"] and bank["items"]["IB-0001"]["link_refreshes"] == 1


def test_a_browser_that_will_not_start_says_the_real_reason(monkeypatch):
    def broken(browser_cfg, creds, debug):
        def factory(site):
            raise TypeError("argument should be a str or an os.PathLike object, not 'NoneType'")
        return factory
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", broken)
    found, lines = IB.refresh_meta_links(ads("A"), cli._ad_browser_factory(parsed()), S, NOW)
    assert lines == ["Meta ad links not refreshed: the browser could not start (TypeError: argument should be a str or an os.PathLike object, not 'NoneType')"]


# ---- the sheet: rescues and approvals, read through the real command ---------------------------------------------------------------------------------

@pytest.fixture
def sheet(store, swov, monkeypatch):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store, ids=("A", "B", "C"))
    IB._remove(bank["items"]["IB-0003"], "not relevant enough (scored 30, the minimum is 50)")
    store.write_ideabank("swoveralls", bank)
    client = FakeClient()
    one(swov, store, client)
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (client, BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    return client


def test_rescue_marks_are_read_with_the_commands_own_arguments(store, swov, sheet):
    ws, header, rows = cells(sheet.created[0], "Rejected")
    ws._put(2, header.index("Rescue") + 1, "Use")
    assert cli._rescues(swov, store, parsed()) == (["IB-0003"], "")


def test_approved_rows_are_read_and_only_use_and_maybe_count(store, swov, sheet):
    decide(sheet, **{"IB-0001": "Use", "IB-0002": "skip"})
    assert cli._decided(swov, store, parsed()) == (["IB-0001"], "")
    decide(sheet, **{"IB-0002": "Maybe"})
    assert cli._decided(swov, store, parsed())[0] == ["IB-0001", "IB-0002"]


def test_a_missing_sheet_is_a_note_but_a_mistake_in_the_program_is_not_hidden(store, swov, monkeypatch, tmp_path):
    config = tmp_path / "sheets.yaml"
    config.write_text('sheets:\n  swoveralls: "abc123"\n  duderobe: ""\n  defi_snacks: ""\n')
    parsed = lambda *extra: cli.build_parser().parse_args(["ideabank", "--brand", "swoveralls", "--sheets-config", str(config), *extra])  # noqa: E731
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (_ for _ in ()).throw(ConnectionError("no network")))
    ids, note = cli._rescues(swov, store, parsed())
    assert ids == [] and note == "swoveralls: the Rejected tab (for rescues) could not be read (ConnectionError: no network)"
    ids, note = cli._decided(swov, store, parsed())
    assert ids is None and note == "swoveralls: your Decisions could not be read (ConnectionError: no network)"      # unknown, not "nobody"

    class NoSheetsOption:
        brands_dir = "brands"
    with pytest.raises(AttributeError, match="sheets_config"):
        cli._rescues(swov, store, NoSheetsOption())
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (_ for _ in ()).throw(TypeError("a wrong call")))
    with pytest.raises(TypeError, match="a wrong call"):
        cli._decided(swov, store, parsed())


def test_no_key_or_a_dry_run_reads_nothing(store, swov, monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert cli._rescues(swov, store, parsed()) == ([], "") and cli._decided(swov, store, parsed()) == (None, "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    assert cli._rescues(swov, store, parsed("--dry-run")) == ([], "") and cli._decided(swov, store, parsed("--dry-run")) == (None, "")


# ---- where added rows go ------------------------------------------------------------------------------------------------------------------------------

def test_the_report_says_which_tab_the_added_rows_went_to(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store, ids=("A", "B", "C", "D"))
    for i in ("IB-0003", "IB-0004"):
        IB._remove(bank["items"][i], "not relevant enough")
    store.write_ideabank("swoveralls", bank)
    report, error = one(swov, store, FakeClient())
    assert error is None and report.rows_by_tab["Rejected"] == 2 and report.rows_by_tab["Idea Bank"] == 2 and "Changes" not in report.rows_by_tab
    text = "\n".join(report.lines())
    assert "added " + str(report.rows_added) + " rows (" in text and "Rejected 2" in text and "Idea Bank 2" in text


def test_a_run_that_adds_nothing_has_no_breakdown(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    report, _ = one(swov, store, client)
    assert report.rows_added == 0 and report.rows_by_tab == {} and "added 0 rows, marked" in "\n".join(report.lines())


def test_the_log_shows_why_the_browser_route_for_a_video_failed(tmp_path):
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    from test_video_outcomes import TIKTOK_REFUSED, posts
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = posts("A")
    why = f"{TIKTOK_REFUSED}; the browser route also failed: the browser could not load the page (TypeError: not a path)"
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", drive, S, lambda url, dest, cookies, mb, timeout: (None, why))
    assert lines == ["IB-0001: no video saved: TikTok refused the download from this server (the browser route also failed: the browser could not load the page (TypeError: not a path))"]
    assert bank["items"]["IB-0001"]["video_status"] == "blocked" and bank["items"]["IB-0001"]["video_note"] == "TikTok refused the download from this server"


def test_a_post_with_no_video_is_still_told_apart_even_after_a_browser_attempt(tmp_path):
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    from test_video_outcomes import posts
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = posts("A", platform="instagram")
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", drive, S, lambda url, dest, cookies, mb, timeout: (None, "There is no video in this post"))
    assert lines == ["IB-0001: no video saved: this post has no video"] and bank["items"]["IB-0001"]["video_status"] == "none"


# ---- an approved idea is never removed by the tool --------------------------------------------------------------------------------------------------

def test_an_item_you_approved_survives_the_technique_limit_and_a_low_score():
    from test_creative_review import _free, tech_claude, techniques
    for flag in ("approved", "rescued"):
        bank = techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 20, "t"))
        bank["items"]["IB-0003"][flag] = True
        IB.group_techniques(bank, tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003"])), IB.IdeaBankSettings(max_per_technique=1), _free())
        assert bank["items"]["IB-0003"]["state"] == "kept" and bank["items"]["IB-0001"]["state"] == "removed" and bank["items"]["IB-0002"]["state"] == "removed", flag
        IB.apply_relevance(bank, IB.IdeaBankSettings(min_relevance=90))
        assert bank["items"]["IB-0003"]["state"] == "kept", flag


def test_a_run_protects_the_ids_it_is_told_were_approved_and_a_later_skip_lifts_it(store, swov):
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    from test_ideabank import full_run, kept_bank
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = kept_bank("A", "B", "C")
    for item in bank["items"].values():
        item["score"] = 20
    store.write_ideabank("swoveralls", bank)
    full_run(store, swov, (drive, drive.session), protected=("IB-0001", "IB-9999"))
    items = store.read_ideabank("swoveralls")["items"]
    assert items["IB-0001"]["state"] == "kept" and items["IB-0001"]["approved"] and items["IB-0002"]["state"] == "removed" and not items["IB-0002"]["approved"]
    assert not items["IB-0001"]["rescued"]                                               # an approval is not a rescue
    bank = store.read_ideabank("swoveralls")
    store.write_ideabank("swoveralls", bank)
    full_run(store, swov, (drive, drive.session), protected=None)                         # the sheet could not be read: nothing is lifted
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["approved"] and store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "kept"
    full_run(store, swov, (drive, drive.session), protected=())                           # the sheet was read and IB-0001 is no longer approved
    after = store.read_ideabank("swoveralls")["items"]["IB-0001"]
    assert after["approved"] is False and after["state"] == "removed"


def test_a_rescue_is_permanent_whatever_the_sheet_says_about_approvals(store, swov):
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    from test_ideabank import full_run, kept_bank
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = kept_bank("A")
    bank["items"]["IB-0001"].update({"score": 20, "rescued": True})
    store.write_ideabank("swoveralls", bank)
    full_run(store, swov, (drive, drive.session), protected=())
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "kept"


def test_change_entries_are_counted_on_their_own_and_not_as_added_rows(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    client = FakeClient()
    one(swov, store, client)
    resync(swov, store, "swoveralls_full_72.json", navy_price)
    report, error = one(swov, store, client)
    assert error is None and report.events_added == 1 and report.rows_added == 0 and "Changes" not in report.rows_by_tab and report.rows_by_tab == {}


def test_an_item_the_tool_already_removed_is_not_marked_protected_by_an_approval_of_it(store, swov):
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    from test_ideabank import full_run, kept_bank
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = kept_bank("A", "B")
    bank["items"]["IB-0002"].update({"state": "removed", "removed_reason": "a copy of IB-0001", "duplicate_of": "IB-0001"})
    store.write_ideabank("swoveralls", bank)
    full_run(store, swov, (drive, drive.session), protected=("IB-0002",))
    item = store.read_ideabank("swoveralls")["items"]["IB-0002"]
    assert item["state"] == "removed" and not item["rescued"] and not item["approved"]


def test_the_readme_explains_what_is_protected_and_where_rows_are_added():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("What you approved is never removed by the tool", "--sheets-config", "Rejected 427", "never hidden as a note", "Approval follows the sheet; a rescue is permanent"):
        assert needle in text, needle


# ---- reading never creates anything; the tests never reach the real tools ----------------------------------------------------------------------------

def test_reading_the_sheet_never_creates_one_for_a_brand_that_has_none(store, swov, monkeypatch):
    """A brand with no spreadsheet yet is simply nothing to read, even when the secret that would let a spreadsheet be created is set."""
    client = FakeClient()
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (client, BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("SHEET_SHARE_EMAIL", "me@kelson.agency")
    monkeypatch.setenv("SHEET_FOLDER_ID", "folder123")
    assert cli._rescues(swov, store, parsed()) == ([], "") and cli._decided(swov, store, parsed()) == (None, "")
    assert client.created == []


def test_has_spreadsheet_knows_a_configured_sheet_and_one_the_tool_made(store, swov):
    assert sheets.has_spreadsheet(swov, store, {}) is False and sheets.has_spreadsheet(swov, store, {"swoveralls": ""}) is False
    assert sheets.has_spreadsheet(swov, store, {"swoveralls": "abc"}) is True and sheets.has_spreadsheet(swov, store, {"duderobe": "abc"}) is False
    store.save_sheet_info("swoveralls", {"spreadsheet_id": "made-by-the-tool"})
    assert sheets.has_spreadsheet(swov, store, {}) is True


def test_the_request_command_has_the_options_its_helpers_read():
    args = cli.build_parser().parse_args(["request", "run", "--brand", "swoveralls", "--request", "RQ-0001"])
    assert (args.sheets_config, args.debug_dir, args.browser_config) == ("sheets.yaml", "debug", "browser.yaml")


def test_the_tests_never_reach_the_real_download_tool_browser_google_or_the_repositorys_own_sheet_ids(monkeypatch):
    """On GitHub yt-dlp is installed and sheets.yaml names your real sheet; a test must not depend on either."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/yt-dlp")                 # as on GitHub
    assert cli._video_runner(parsed()) is not cli.videofetch_mod.ytdlp_download
    assert cli._video_runner(parsed())("https://www.instagram.com/p/x/", "/tmp", None, 80, 150) == (None, "no real downloads in tests")
    with pytest.raises(RuntimeError, match="no real browser in tests"):
        cli._ad_browser_factory(parsed())("meta_ads")
    with pytest.raises(ConnectionError, match="no real Google in tests"):
        cli._SHEETS_CONNECT("{}")
    assert not any(sheets.load_sheet_config("sheets.yaml", {"swoveralls", "duderobe", "defi_snacks"}).values())
