"""The photo trial (one photo per run, Pollinations, free, no key).

What these tests prove, with fakes (no image is ever really generated here):
- the image URL is built right: the prompt URL-encoded into the path, the
  model, size and nologo flag as parameters, a seed only when one is given;
- only a real image counts: a JPEG or a PNG is accepted, an error page or an
  empty body is refused plainly, and an HTTP error is reported with its code;
- the hard cap holds: one photo per run, exactly one generation;
- the brand save: a made photo goes only to the brand the run is for, under
  the name YYYY-MM-DD_<brand>_pollinations_<prompt-slug>.jpg, into that
  brand's Drive folder (Photos) and that brand's sheet tab (Photos), and a
  failed save never loses the verdict — the trial result stands, the exact
  error is recorded, and the run still exits 0;
- a dry run makes no image and touches nothing.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from contentintel import brands as brands_mod
from contentintel import drive as drive_mod
from contentintel import photo_trial as PT
from fake_drive import EMAIL as DRIVE_EMAIL
from fake_drive import ROOT as DRIVE_ROOT
from fake_drive import FakeDrive
from fake_sheets import FakeClient
from fake_sheets import FakeSpreadsheet
from fake_sheets import read as read_sheet

FIXED_NOW = lambda: datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"FAKEJPEG"
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"FAKEPNG"

SHEET_IDS = {"duderobe": "sheet-duderobe", "swoveralls": "sheet-swoveralls",
             "defi_snacks": "sheet-defi-snacks"}


def _brand(slug="duderobe"):
    return brands_mod.load_brand(slug)


def _fetch_ok(data=FAKE_JPEG):
    calls = []

    def fetch(url, timeout=None):
        calls.append(url)
        return data

    fetch.calls = calls
    return fetch


def _fetch_failing(reason="Pollinations answered HTTP 429 for the image"):
    def fetch(url, timeout=None):
        raise PT.TrialError(reason)

    return fetch


class RecordingSaver:
    """A PhotoSaver stand-in: records every save, returns a fixed Drive link,
    and never touches Google. `fail` makes save() raise; `sheet_error` makes
    it return the link with a Sheet failure, the way the real saver does."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.sheet_error = None

    def save(self, model, prompt, photo_rel_path, out_dir, run_date):
        self.calls.append((model, prompt, photo_rel_path, run_date))
        if self.fail is not None:
            raise PT.TrialError(self.fail)
        if self.sheet_error is not None:
            return "https://drive.google.com/file/d/PHOTOID/view", self.sheet_error
        return "https://drive.google.com/file/d/PHOTOID/view", None


def _real_saver(tmp_path, brand_slug="duderobe", sheet_ids=None):
    """A PhotoSaver wired to the fakes: a FakeDrive and a FakeClient holding
    one spreadsheet per id in `sheet_ids`. No Google call is ever made."""
    brand = brands_mod.load_brand(brand_slug)
    drive_client = drive_mod.DriveClient(FakeDrive(), DRIVE_ROOT, DRIVE_EMAIL)
    sheets_client = FakeClient([FakeSpreadsheet(sid, f"{slug} catalogue")
                                for slug, sid in (sheet_ids or {}).items()])
    config = tmp_path / "sheets.yaml"
    config.write_text("sheets:\n" + "".join(f"  {slug}: \"{sid}\"\n"
                                            for slug, sid in (sheet_ids or {}).items()),
                      encoding="utf-8")
    saver = PT.PhotoSaver(brand, {}, data_dir=str(tmp_path / "data"), sheets_config=str(config),
                          drive_client=drive_client, sheets_client=sheets_client)
    return saver, drive_client, sheets_client


# ---------------------------------------------------------------------------
# Filenames and the image URL
# ---------------------------------------------------------------------------

def test_photo_filename_puts_the_brand_in_the_name():
    assert PT.photo_filename("2026-11-18", "duderobe", "puppy in park") == \
        "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    assert PT.photo_filename("2026-11-18", "defi_snacks", "puppy in park", "png") == \
        "2026-11-18_defi_snacks_pollinations_puppy-in-park.png"


def test_slugify_never_returns_empty_and_cuts_at_a_word():
    assert PT._slugify("") == "photo"
    assert PT._slugify("a very long prompt with many words that keeps going on") == \
        "a-very-long-prompt-with-many-words-that"


def test_build_image_url_encodes_the_prompt_and_sets_the_parameters():
    url = PT.build_image_url("puppy in park")
    assert url.startswith("https://image.pollinations.ai/prompt/puppy%20in%20park?")
    assert "model=flux" in url and "width=1024" in url and "height=1024" in url
    assert "nologo=true" in url and "seed" not in url
    assert "seed=7" in PT.build_image_url("puppy in park", seed=7)
    assert "model=turbo" in PT.build_image_url("puppy in park", model="turbo")


def test_detect_image_accepts_jpeg_and_png_only():
    assert PT.detect_image(FAKE_JPEG) == ("jpg", "image/jpeg")
    assert PT.detect_image(FAKE_PNG) == ("png", "image/png")
    assert PT.detect_image(b"<html>not an image</html>") is None
    assert PT.detect_image(b"") is None


def test_fetch_image_returns_bytes_for_a_jpeg():
    assert PT.fetch_image("https://image.pollinations.ai/prompt/x", fetch=_fetch_ok()) == FAKE_JPEG


def test_fetch_image_refuses_a_non_image_body():
    with pytest.raises(PT.TrialError, match="not with an image"):
        PT.fetch_image("https://image.pollinations.ai/prompt/x",
                       fetch=_fetch_ok(b"<html>error page</html>"))


def test_fetch_bytes_reports_an_http_error_plainly(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.HTTPError(url="http://x", code=429, msg="Too Many Requests",
                                     hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(PT.TrialError, match="HTTP 429"):
        PT._fetch_bytes("https://image.pollinations.ai/prompt/x")


# ---------------------------------------------------------------------------
# The brand save (Drive + Sheet)
# ---------------------------------------------------------------------------

def test_a_photo_lands_only_in_its_brands_folder_and_sheet(tmp_path):
    saver, drive_client, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(FAKE_JPEG)
    link, sheet_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert sheet_error is None
    assert link.startswith("https://drive.google.com/file/d/") and link.endswith("/view")
    # Drive: one file, in Idea Bank / Generated Content / Photo / DudeRobe, with the right name and bytes
    assert drive_client.find_folder(DRIVE_ROOT, "DudeRobe") is None  # no brand folder at the root any more
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    assert generated_id is not None
    assert drive_client.find_folder(generated_id, "VIDEO") is None  # the photo save never creates the VIDEO side
    photo_id = drive_client.find_folder(generated_id, "Photo")
    assert photo_id is not None
    brand_id = drive_client.find_folder(photo_id, "DudeRobe")
    assert brand_id is not None
    assert drive_client.find_folder(photo_id, "duderobe") is None
    files = drive_client.list_files(brand_id)
    assert list(files) == ["2026-11-18_duderobe_pollinations_puppy-in-park.jpg"]
    assert drive_client.download(files["2026-11-18_duderobe_pollinations_puppy-in-park.jpg"]) == FAKE_JPEG
    # the other brands' folders were never created
    assert drive_client.find_folder(photo_id, "Swoveralls") is None
    assert drive_client.find_folder(photo_id, "Defi Snacks") is None
    # Sheet: the duderobe sheet has the tab with the header and one row; the other sheets have no such tab
    ws = sheets_client.spreadsheets["sheet-duderobe"].worksheet("Photos")
    header, rows = read_sheet(ws)
    assert header == PT.PHOTOS_HEADERS
    assert len(rows) == 1
    assert rows[0] == {"Date": "2026-11-18", "Brand": "duderobe", "Source": "pollinations",
                       "Model": "flux", "Prompt": "puppy in park", "Verdict": "go",
                       "Photo": link, "Credits used": "", "Notes": ""}
    for sid in ("sheet-swoveralls", "sheet-defi-snacks"):
        assert "Photos" not in [w.title for w in sheets_client.spreadsheets[sid].worksheets()]


def test_a_second_save_with_the_same_name_replaces_the_first_photo(tmp_path):
    saver, drive_client, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(b"\xff\xd8\xff" + b"FIRST")
    first_link, first_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert first_error is None
    (tmp_path / rel).write_bytes(b"\xff\xd8\xff" + b"SECOND")
    second_link, second_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert second_error is None
    assert second_link != first_link
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    photo_id = drive_client.find_folder(generated_id, "Photo")
    brand_id = drive_client.find_folder(photo_id, "DudeRobe")
    files = drive_client.list_files(brand_id)
    assert list(files) == ["2026-11-18_duderobe_pollinations_puppy-in-park.jpg"]
    assert drive_client.download(files["2026-11-18_duderobe_pollinations_puppy-in-park.jpg"]) == b"\xff\xd8\xffSECOND"
    ws = sheets_client.spreadsheets["sheet-duderobe"].worksheet("Photos")
    _, rows = read_sheet(ws)
    assert len(rows) == 2


def test_a_hand_changed_header_is_left_alone_and_the_link_is_kept(tmp_path):
    saver, _, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    ws = sheets_client.spreadsheets["sheet-duderobe"].add_worksheet(title="Photos", rows=1000, cols=30)
    ws.update(values=[["Date", "Wrong", "Columns"]], range_name="A1")
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(FAKE_JPEG)
    link, sheet_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert link is not None
    assert sheet_error is not None and "different header" in sheet_error
    header, rows = read_sheet(ws)
    assert header == ["Date", "Wrong", "Columns"] and rows == []


def test_a_photo_in_drive_with_no_sheet_mapping_keeps_its_link(tmp_path):
    saver, drive_client, _ = _real_saver(tmp_path, sheet_ids={})
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(FAKE_JPEG)
    link, sheet_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert link is not None and link.startswith("https://drive.google.com/file/d/")
    assert sheet_error is not None and "No spreadsheet is set up" in sheet_error
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    photo_id = drive_client.find_folder(generated_id, "Photo")
    brand_id = drive_client.find_folder(photo_id, "DudeRobe")
    assert list(drive_client.list_files(brand_id)) == ["2026-11-18_duderobe_pollinations_puppy-in-park.jpg"]


def test_a_real_save_without_the_service_account_key_fails_plainly(tmp_path):
    saver = PT.PhotoSaver(_brand(), {}, data_dir=str(tmp_path / "data"),
                          sheets_config=str(tmp_path / "sheets.yaml"))
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(FAKE_JPEG)
    with pytest.raises(PT.TrialError, match="GOOGLE_SERVICE_ACCOUNT_JSON is not set"):
        saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")


def test_a_png_photo_is_saved_with_a_png_name(tmp_path):
    saver, drive_client, _ = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    (tmp_path / rel).write_bytes(FAKE_PNG)
    link, sheet_error = saver.save("flux", "puppy in park", rel, tmp_path, "2026-11-18")
    assert sheet_error is None
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    photo_id = drive_client.find_folder(generated_id, "Photo")
    brand_id = drive_client.find_folder(photo_id, "DudeRobe")
    assert list(drive_client.list_files(brand_id)) == ["2026-11-18_duderobe_pollinations_puppy-in-park.png"]


# ---------------------------------------------------------------------------
# The run: verdicts, the hard cap, dry run, the CLI
# ---------------------------------------------------------------------------

def test_happy_path_makes_one_photo_and_saves_it(tmp_path):
    fetch = _fetch_ok()
    saver = RecordingSaver()
    verdicts, code = PT.run_trial("puppy in park", _brand(), {}, tmp_path,
                                  fetcher=fetch, saver=saver, now=FIXED_NOW)
    assert code == 0
    assert len(fetch.calls) == 1  # the hard cap: exactly one generation
    assert len(saver.calls) == 1
    assert verdicts[0]["verdict"] == "go"
    assert verdicts[0]["photo"] == "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    assert verdicts[0]["drive_link"] == "https://drive.google.com/file/d/PHOTOID/view"
    assert (tmp_path / "2026-11-18_duderobe_pollinations_puppy-in-park.jpg").read_bytes() == FAKE_JPEG
    written = json.loads((tmp_path / "photo_trial.json").read_text(encoding="utf-8"))
    assert written["brand"] == "duderobe" and written["source"] == "pollinations"
    assert written["verdicts"][0]["verdict"] == "go"


def test_a_failed_generation_is_a_no_go_verdict_and_exits_zero(tmp_path):
    verdicts, code = PT.run_trial("puppy in park", _brand(), {}, tmp_path,
                                  fetcher=_fetch_failing(), now=FIXED_NOW)
    assert code == 0
    assert verdicts[0]["verdict"] == "no-go"
    assert "HTTP 429" in verdicts[0]["reason"]
    assert "photo" not in verdicts[0]


def test_a_failed_save_keeps_the_verdict_and_exits_zero(tmp_path):
    saver = RecordingSaver()
    saver.fail = "GOOGLE_SERVICE_ACCOUNT_JSON is not set"
    verdicts, code = PT.run_trial("puppy in park", _brand(), {}, tmp_path,
                                  fetcher=_fetch_ok(), saver=saver, now=FIXED_NOW)
    assert code == 0
    assert verdicts[0]["verdict"] == "go"
    assert verdicts[0]["photo"] == "2026-11-18_duderobe_pollinations_puppy-in-park.jpg"
    assert "drive_link" not in verdicts[0]
    assert verdicts[0]["save_error"] == "GOOGLE_SERVICE_ACCOUNT_JSON is not set"


def test_a_sheet_failure_keeps_the_drive_link(tmp_path):
    saver = RecordingSaver()
    saver.sheet_error = "the 'Photos' tab has a different header"
    verdicts, code = PT.run_trial("puppy in park", _brand(), {}, tmp_path,
                                  fetcher=_fetch_ok(), saver=saver, now=FIXED_NOW)
    assert code == 0
    assert verdicts[0]["verdict"] == "go"
    assert verdicts[0]["drive_link"] == "https://drive.google.com/file/d/PHOTOID/view"
    assert verdicts[0]["save_error"] == "the 'Photos' tab has a different header"


def test_dry_run_shows_the_save_plan_and_touches_nothing(tmp_path, capsys):
    class ExplodingSaver:
        def save(self, *args, **kwargs):
            raise AssertionError("a dry run must never save")

    def exploding_fetch(url, timeout=None):
        raise AssertionError("a dry run must never fetch")

    verdicts, code = PT.run_trial("puppy in park", _brand(), {}, tmp_path,
                                  dry_run=True, fetcher=exploding_fetch,
                                  saver=ExplodingSaver(), now=FIXED_NOW)
    assert code == 0
    assert verdicts[0]["verdict"] == "skipped"
    out = capsys.readouterr().out
    assert "brand: duderobe (DudeRobe)" in out
    assert "save: Drive: Generated Content / Photo / DudeRobe / 2026-11-18_duderobe_pollinations_puppy-in-park.jpg" in out
    assert 'save: Sheet: the duderobe Catalogue sheet (sheets.yaml), tab "Photos"' in out
    assert (tmp_path / "photo_trial.json").exists()  # the verdicts file itself is written
    written = json.loads((tmp_path / "photo_trial.json").read_text(encoding="utf-8"))
    assert written["dry_run"] is True and written["verdicts"][0]["verdict"] == "skipped"
    assert list(tmp_path.glob("*.jpg")) == []


def test_run_trial_refuses_to_start_without_a_brand(tmp_path):
    with pytest.raises(PT.TrialError, match="no brand was given"):
        PT.run_trial("puppy in park", None, {}, tmp_path)


def test_main_rejects_an_unknown_brand_and_an_empty_prompt(tmp_path, capsys):
    assert PT.main(["--brand", "nope", "--out", str(tmp_path)]) == 1
    assert "unknown brand" in capsys.readouterr().out
    assert PT.main(["--brand", "duderobe", "--prompt", "  ", "--out", str(tmp_path)]) == 1
    assert "prompt is empty" in capsys.readouterr().out


def test_verdict_lines_show_the_drive_link_and_a_failed_save():
    entry = PT.verdict("pollinations", "go", "one flux photo made and downloaded",
                       photo="2026-11-18_duderobe_pollinations_puppy-in-park.jpg")
    entry["drive_link"] = "https://drive.google.com/file/d/PHOTOID/view"
    assert "GO" in PT.verdict_line(entry) and "PHOTOID" in PT.verdict_line(entry)
    entry["save_error"] = "the 'Photos' tab has a different header"
    assert "different header" in PT.verdict_line(entry)
    with pytest.raises(PT.TrialError, match="unknown verdict"):
        PT.verdict("pollinations", "blocked", "not a photo verdict")
