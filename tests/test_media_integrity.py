"""Saved creative must be whole, readable and honestly described.

The first review of the real Drive folders (2026-10-28) found no corrupt file, but it did find two pictures saved as .jpg that were really
PNG files (the server had said image/jpeg), twelve of sixteen files that were only the cover picture of a video while the sheet said
"saved in Drive", one picture only 486 pixels wide, and a download routine that never compared what arrived with what was announced and
crashed if a connection dropped half way. These tests keep each of those from coming back."""
import hashlib
import subprocess
from dataclasses import replace

import pytest

from contentintel import ideabank as IB
from contentintel import mediacheck as M
from contentintel import videofetch as V
from contentintel.drive import DriveClient, DriveError, DriveMismatch
from contentintel.store import Store

from fake_drive import EMAIL, ROOT, FakeDrive
from media_fixtures import blank_jpeg, cut, jpeg_for, png_for, tiny_mp4
from test_ideabank import Media, S, full_run, getter, kept_bank


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


# ---- reading what a file really is ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("head,expected", [
    (b"\xff\xd8\xff\xe0" + bytes(20), ("image", "image/jpeg", "jpg")), (b"\x89PNG\r\n\x1a\n" + bytes(20), ("image", "image/png", "png")),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(20), ("image", "image/webp", "webp")), (b"GIF89a" + bytes(20), ("image", "image/gif", "gif")),
    (b"GIF87a" + bytes(20), ("image", "image/gif", "gif")), (b"\x00\x00\x00\x20ftypisom" + bytes(20), ("video", "video/mp4", "mp4")),
    (b"\x00\x00\x00\x14ftypqt  " + bytes(20), ("video", "video/quicktime", "mov")), (b"\x1a\x45\xdf\xa3" + bytes(20), ("video", "video/webm", "webm")),
    (b"<html><body>", ("", "", "")), (b"", ("", "", "")), (b"plain text here", ("", "", ""))])
def test_the_real_type_is_read_from_the_first_bytes(head, expected):
    assert M.sniff(head) == expected


def test_a_good_picture_passes_and_says_how_big_it_is():
    for data, ext in ((jpeg_for("a"), "jpg"), (png_for("a"), "png")):
        c = M.check_media(data)
        assert c.ok and (c.kind, c.ext, c.width, c.height, c.bytes) == ("image", ext, 640, 960, len(data)) and c.describe() == "image 640x960"


def test_a_png_called_jpg_is_noticed_and_the_right_name_is_given():
    c = M.check_media(png_for("a"))
    assert not M.extension_matches("IB-0053-image.jpg", c) and M.extension_matches("IB-0053-image.png", c)
    assert M.corrected_name("IB-0053-image.jpg", c) == "IB-0053-image.png"
    assert M.extension_matches("a.jpeg", M.check_media(jpeg_for("a"))) and M.extension_matches("A.JPG", M.check_media(jpeg_for("a")))


@pytest.mark.parametrize("make,fragment", [
    (lambda: blank_jpeg(), "blank or a single colour"), (lambda: jpeg_for("a", (100, 100)), "too small to judge (100x100)"),
    (lambda: jpeg_for("a", (400, 150)), "too small to judge (400x150)"), (lambda: cut(jpeg_for("a"), .5), "is cut short"),
    (lambda: jpeg_for("a")[:-300], "is cut short"), (lambda: cut(png_for("a"), .6), "is cut short"),
    (lambda: b"\xff\xd8\xff" + bytes(5000), "does not open"), (lambda: b"<html>" + b"x" * 4000, "not a picture or video we can read"),
    (lambda: b"", "the file is empty"), (lambda: b"\xff\xd8\xff" + bytes(20), "too small to be real")])
def test_a_damaged_blank_tiny_or_fake_picture_is_refused_with_a_plain_reason(make, fragment):
    c = M.check_media(make())
    assert not c.ok and any(fragment in p for p in c.problems), c.problems


def test_a_file_that_arrives_short_of_what_was_announced_is_cut_short():
    data = jpeg_for("a")
    assert M.check_media(data, declared_length=len(data)).ok
    c = M.check_media(data, declared_length=len(data) + 500)
    assert not c.ok and f"the file is cut short ({len(data)} of {len(data) + 500} bytes arrived)" in c.problems


def test_a_picture_between_the_minimum_and_the_comfortable_size_passes_but_is_marked_low_resolution():
    c = M.check_media(jpeg_for("a", (486, 648)))
    assert c.ok and c.low_resolution and not M.check_media(jpeg_for("a", (600, 800))).low_resolution
    assert not M.check_media(tiny_mp4()).low_resolution                    # only pictures are marked


def test_without_pillow_a_picture_is_still_checked_by_its_header_and_end_and_says_so(monkeypatch):
    monkeypatch.setattr(M, "Image", None)
    good = M.check_media(jpeg_for("a"))
    assert good.ok and any("Pillow is not installed" in n for n in good.notes) and good.width == 0
    assert not M.check_media(cut(jpeg_for("a"), .5)).ok and M.check_media(png_for("a")).ok and not M.check_media(cut(png_for("a"), .6)).ok


# ---- videos ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_whole_video_passes_and_reports_its_length_and_size():
    for data in (tiny_mp4(seconds=27.5), tiny_mp4(seconds=27.5, moov_first=False)):
        c = M.check_media(data)
        assert c.ok and (c.kind, c.ext, c.width, c.height) == ("video", "mp4", 720, 1280) and abs(c.duration - 27.5) < 0.01
        assert c.describe() == "video 28s, 720x1280"


@pytest.mark.parametrize("share", [.2, .5, .7, .95])
def test_a_video_cut_short_is_refused_whichever_part_was_lost(share):
    c = M.check_media(cut(tiny_mp4(padding=40000), share))
    assert not c.ok and any("cut short" in p for p in c.problems), (share, c.problems)


def test_a_video_whose_header_never_arrived_is_refused():
    assert any("no header" in p for p in M.check_media(cut(tiny_mp4(moov_first=False, padding=40000), .6)).problems)


def test_a_video_with_no_picture_data_or_no_picture_track_or_under_a_second_is_refused():
    data = tiny_mp4()
    assert any("shorter than a second" in p for p in M.check_media(tiny_mp4(seconds=0.4)).problems)
    assert any("no picture track" in p for p in M.check_media(data.replace(b"vide", b"soun")).problems)
    assert any("no picture data" in p for p in M.check_media(data.replace(b"mdat", b"free")).problems)


def test_a_webm_video_is_accepted_with_a_note_that_it_was_not_checked_in_detail():
    c = M.check_media(b"\x1a\x45\xdf\xa3" + bytes(2000))
    assert c.ok and c.kind == "video" and any("WebM" in n for n in c.notes)


def test_a_still_is_taken_from_a_video_by_ffmpeg_when_it_is_there():
    calls = []

    def runner(command, **kw):
        calls.append(command)
        open(command[-1], "wb").write(b"JPEGBYTES")
    assert M.extract_frame(tiny_mp4(), runner=runner) == b"JPEGBYTES"
    assert calls[0][0] == "ffmpeg" and "-frames:v" in calls[0] and "-ss" in calls[0]


def test_a_clip_shorter_than_the_offset_falls_back_to_its_first_frame_and_a_missing_ffmpeg_is_not_an_error():
    state = {"n": 0}

    def runner(command, **kw):
        state["n"] += 1
        if state["n"] == 2:
            open(command[-1], "wb").write(b"FIRST")
    assert M.extract_frame(tiny_mp4(), runner=runner) == b"FIRST" and state["n"] == 2
    assert M.extract_frame(tiny_mp4(), runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())) is None
    assert M.extract_frame(tiny_mp4(), runner=lambda *a, **k: None) is None
    assert M.extract_frame(tiny_mp4(), runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffmpeg", 1))) is None


# ---- downloading ------------------------------------------------------------------------------------------------------------------------------------

class Flaky(Media):
    """A download whose connection drops half way through the body."""

    def iter_content(self, size):
        yield self.content[:len(self.content) // 2]
        raise ConnectionError("connection reset")


def sequence(*responses):
    calls = []

    def get(url, **kw):
        calls.append(url)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    get.calls = calls
    return get


def test_a_download_that_arrives_short_is_tried_again_and_the_second_try_is_used():
    data = jpeg_for("a")
    short = Media(content=cut(data, .5), length=len(data))
    got, check = IB.fetch_media_checked(sequence(short, Media(content=data, length=len(data))), "https://c.example/a", S)
    assert got == data and check.ok


def test_a_download_that_arrives_short_twice_is_refused_after_exactly_two_tries():
    data = jpeg_for("a")
    get = sequence(Media(content=cut(data, .5), length=len(data)))
    with pytest.raises(IB.AssetSkip, match="the file is cut short"):
        IB.fetch_media_checked(get, "https://c.example/a", S)
    assert len(get.calls) == 2


def test_a_connection_that_drops_in_the_middle_is_a_reason_not_a_crash_and_the_second_try_can_succeed():
    data = jpeg_for("a")
    assert IB.fetch_media_checked(sequence(Flaky(content=data), Media(content=data)), "https://c.example/a", S)[0] == data
    get = sequence(Flaky(content=data))
    with pytest.raises(IB.AssetSkip, match="the download was interrupted .ConnectionError."):
        IB.fetch_media_checked(get, "https://c.example/a", S)
    assert len(get.calls) == 2


def test_a_missing_link_is_not_retried():
    get = sequence(Media(status=404))
    with pytest.raises(IB.AssetSkip, match="answered 404"):
        IB.fetch_media_checked(get, "https://c.example/a", S)
    assert len(get.calls) == 1


@pytest.mark.parametrize("content,fragment", [(blank_jpeg(), "blank or a single colour"), (jpeg_for("a", (90, 90)), "too small to judge"),
                                              (b"<html>" + b"x" * 4000, "not a picture or video we can read"), (b"\xff\xd8\xff" + bytes(5000), "does not open")])
def test_a_download_that_is_not_a_usable_picture_is_refused_before_anything_is_uploaded(drive, content, fragment):
    client, fake = drive
    bank = kept_bank("A")
    saved, lines = IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(content=content)), S)
    item = bank["items"]["IB-0001"]
    assert saved == 0 and item["assets"] == [] and fragment in item["asset_note"] and fragment in lines[0] and fake.tree() == []


# ---- saving, naming and proving the copy -----------------------------------------------------------------------------------------------------

def test_a_file_is_named_and_typed_by_what_it_really_is(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(mime="image/jpeg", content=png_for("a"))), S)
    asset = bank["items"]["IB-0001"]["assets"][0]
    assert asset["name"] == "IB-0001-image.png" and fake.files[asset["drive_id"]]["mime"] == "image/png"


def test_what_is_known_about_each_saved_file_is_recorded(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    video = tiny_mp4(seconds=12.3)
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(mime="video/mp4", content=video)), S)
    IB.save_assets(bank, ["IB-0002"], "Swoveralls", client, getter(), S)
    v, i = bank["items"]["IB-0001"]["assets"][0], bank["items"]["IB-0002"]["assets"][0]
    assert (v["kind"], v["width"], v["height"], v["duration"], v["verified"]) == ("video", 720, 1280, 12.3, True) and v["md5"] == hashlib.md5(video).hexdigest()
    assert (i["kind"], i["width"], i["height"], i["duration"], i["verified"]) == ("image", 640, 960, 0.0, True) and len(i["sha256"]) == 64


@pytest.mark.parametrize("platform,source", [("tiktok", "cover"), ("instagram", "cover"), ("meta_ads", "media"), ("web", "media")])
def test_a_picture_from_a_tiktok_or_instagram_post_is_marked_as_only_its_cover(drive, platform, source):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["platform"] = platform
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    assert bank["items"]["IB-0001"]["assets"][0]["source"] == source


def test_a_video_from_instagram_is_a_video_not_a_cover(drive):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["platform"] = "instagram"
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(mime="video/mp4", content=tiny_mp4())), S)
    assert bank["items"]["IB-0001"]["assets"][0]["source"] == "media"


def test_a_copy_in_drive_that_does_not_match_is_trashed_and_sent_again(drive):
    client, fake = drive
    state = {"n": 0}

    def damage_first(data):
        state["n"] += 1
        return data[:100] if state["n"] == 1 else data
    fake.mangle = damage_first
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    asset = bank["items"]["IB-0001"]["assets"][0]
    assert state["n"] == 2 and asset["verified"] and fake.files[asset["drive_id"]]["data"] == jpeg_for("https://cdn.example/A.jpg")
    assert sum(1 for f in fake.files.values() if f.get("trashed")) == 1 and [n for n in fake.tree() if "image" in n] == ["    IB-0001-image.jpg"]


def test_a_copy_that_never_matches_is_an_error_with_nothing_recorded_and_nothing_left_in_the_folder(drive):
    client, fake = drive
    fake.mangle = lambda data: data[:100]
    bank = kept_bank("A")
    with pytest.raises(DriveError, match="did not match the download, even on a second try"):
        IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    item = bank["items"]["IB-0001"]
    assert item["assets"] == [] and "did not match" in item["asset_note"] and not [n for n in fake.tree() if "image" in n]


def test_a_damaged_file_already_waiting_in_the_folder_is_replaced_not_trusted(drive):
    client, fake = drive
    bank = kept_bank("A")
    folder = client.item_folder(client.brand_folder("Swoveralls"), "IB-0001", "post A")
    client.upload(folder, "IB-0001-image.jpg", b"half a picture", "image/jpeg")
    bank["items"]["IB-0001"].update({"drive_folder_id": folder, "topic": "post A"})
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    asset = bank["items"]["IB-0001"]["assets"][0]
    assert fake.files[asset["drive_id"]]["data"] == jpeg_for("https://cdn.example/A.jpg")


def test_drive_verifies_size_and_checksum_and_can_rename(drive):
    client, fake = drive
    folder = client.brand_folder("Swoveralls")
    fid = client.upload(folder, "x.jpg", b"abcdef", "image/jpeg")
    assert client.verify(fid, b"abcdef") == hashlib.md5(b"abcdef").hexdigest()
    with pytest.raises(DriveMismatch, match="different file from the one sent .size 6 instead of 3."):
        client.verify(fid, b"abc")
    with pytest.raises(DriveMismatch):
        client.verify(fid, b"abcdeX")
    client.rename(fid, "y/z.png")
    assert fake.files[fid]["name"] == "y z.png"
    assert issubclass(DriveMismatch, DriveError)


def test_a_file_that_is_missing_from_drive_cannot_be_verified(drive):
    client, fake = drive
    with pytest.raises(DriveError):
        client.verify("nothing", b"x")


# ---- telling the truth in the sheet -------------------------------------------------------------------------------------------------------------

def item_with(*assets, **kw):
    base = {"assets": list(assets), "asset_note": ""}
    base.update(kw)
    return base


def asset(**kw):
    base = {"kind": "image", "source": "media", "width": 1122, "height": 1402, "duration": 0.0, "verified": True}
    base.update(kw)
    return base


@pytest.mark.parametrize("item,label", [
    (item_with(), "no saved media: original link only"),
    (item_with(asset_note="the link expired on 2026-10-27; the file is saved if the ad is collected again"), "no saved media: the original link expired"),
    (item_with(asset_note="could not be downloaded (ConnectionError)"), "no saved media: the original link is unavailable"),
    (item_with(asset()), "image saved (1122x1402)"),
    (item_with(asset(source="cover", width=1080, height=1920)), "cover image only: the video has not been fetched yet"),
    (item_with(asset(source="cover", width=1080, height=1920), video_status="blocked"), "cover image only: the video could not be downloaded (the platform refused the download)"),
    (item_with(asset(source="cover", width=1080, height=1920), video_status="gone"), "cover image only: the video could not be downloaded (the post is no longer available)"),
    (item_with(asset(source="cover", width=1080, height=1920), video_status="failed", video_note="yt-dlp gave up after 150 seconds"), "cover image only: the video could not be downloaded (yt-dlp gave up after 150 seconds)"),
    (item_with(asset(source="media", width=1080, height=1920), video_status="none"), "image post saved (1080x1920), it has no video"),
    (item_with(asset(source="media", width=1080, height=1920), video_status="none", format="carousel"), "carousel: first slide saved, the other slides were not"),
    (item_with(asset(), media_format="CAROUSEL"), "carousel: first slide saved, the other slides were not"),
    (item_with(asset(kind="video", duration=27.5, width=720, height=1280)), "video saved and checked (28s, 720x1280)"),
    (item_with(asset(source="cover"), asset(kind="video", source="page", duration=9, width=720, height=1280)), "video saved and checked (9s, 720x1280)"),
    (item_with(asset(source="cover"), asset(kind="video", source="page", duration=9, width=720, height=1280), video_status="blocked", video_note="old"), "video saved and checked (9s, 720x1280)"),
    (item_with(asset(width=486, height=648)), "image saved (486x648) - low resolution"),
    (item_with(asset(verified=False)), "image saved (1122x1402) - not yet checked"),
    (item_with(asset(width=486, height=648, verified=False)), "image saved (486x648) - low resolution - not yet checked"),
    (item_with({"name": "x"}), "image saved - not yet checked"),
    (item_with(asset(kind="video", width=0, height=0, duration=0)), "video saved and checked")])
def test_the_preview_says_what_a_person_will_really_find_in_drive(item, label):
    assert IB.preview_label(item) == label


def test_the_sheet_row_uses_the_honest_label():
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["assets"] = [asset(source="cover")]
    assert IB.sheet_rows(bank)["IB-0001"]["preview"] == "cover image only: the video has not been fetched yet"


def test_files_saved_before_they_were_checked_are_marked_unchecked_and_their_kind_is_worked_out():
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"].update({"platform": "tiktok", "assets": [{"name": "IB-0001-image.jpg", "drive_id": "x", "kind": "image", "bytes": 5}]})
    bank["items"]["IB-0002"].update({"platform": "meta_ads", "assets": [{"name": "IB-0002-video.mp4", "drive_id": "y", "kind": "video", "bytes": 5}]})
    IB.normalise_bank(bank)
    one, two = bank["items"]["IB-0001"]["assets"][0], bank["items"]["IB-0002"]["assets"][0]
    assert (one["source"], one["verified"], two["source"], two["verified"]) == ("cover", False, "media", False)


# ---- the audit: look at the files in Drive as a person would -----------------------------------------------------------------------------------

def saved_bank(drive, *ids, **kw):
    client, fake = drive
    bank = kept_bank(*ids)
    IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(**kw), S)
    return bank


def test_an_audit_of_good_files_confirms_them_and_fills_in_what_is_known(drive):
    client, fake = drive
    bank = saved_bank(drive, "A", "B")
    for item in bank["items"].values():                                   # as if saved by the old code
        for a in item["assets"]:
            for key in ("md5", "width", "height", "duration", "verified", "source"):
                a.pop(key, None)
    lines = IB.audit_files(bank, client)
    assert lines == ["audit: 2 saved file(s) downloaded from Drive and checked: 2 complete, 0 renamed (the ending did not match the real type), "
                     "0 broken, 0 missing; 0 low resolution; 0 item(s) have only a cover picture, not the video"]
    a = bank["items"]["IB-0001"]["assets"][0]
    assert a["verified"] is True and (a["width"], a["height"]) == (640, 960) and len(a["md5"]) == 32


def test_an_audit_renames_a_picture_whose_ending_is_wrong(drive):
    client, fake = drive
    bank = kept_bank("A")
    folder = client.item_folder(client.brand_folder("Swoveralls"), "IB-0001", "post A")
    fid = client.upload(folder, "IB-0001-image.jpg", png_for("a"), "image/jpeg")                  # the real case: a PNG saved as .jpg
    bank["items"]["IB-0001"].update({"drive_folder_id": folder, "assets": [{"name": "IB-0001-image.jpg", "drive_id": fid, "kind": "image", "bytes": len(png_for("a"))}]})
    lines = IB.audit_files(bank, client)
    assert "1 renamed" in lines[0] and bank["items"]["IB-0001"]["assets"][0]["name"] == "IB-0001-image.png" and fake.files[fid]["name"] == "IB-0001-image.png"


def test_an_audit_removes_a_file_that_is_broken_and_asks_for_it_again(drive):
    client, fake = drive
    bank = saved_bank(drive, "A", "B")
    bad = bank["items"]["IB-0001"]["assets"][0]
    fake.files[bad["drive_id"]]["data"] = cut(fake.files[bad["drive_id"]]["data"], .5)
    lines = IB.audit_files(bank, client)
    item = bank["items"]["IB-0001"]
    assert "1 complete" in lines[0] and "1 broken" in lines[0] and any("IB-0001-image.jpg is broken (the file is cut short" in l and "the picture is cut short" in l for l in lines)
    assert item["assets"] == [] and item["asset_tries"] == 0 and "audit: a saved file was broken" in item["asset_note"] and fake.files[bad["drive_id"]].get("trashed")
    assert bank["items"]["IB-0002"]["assets"]


def test_an_audit_notices_a_file_removed_from_drive_by_hand(drive):
    client, fake = drive
    bank = saved_bank(drive, "A")
    del fake.files[bank["items"]["IB-0001"]["assets"][0]["drive_id"]]
    lines = IB.audit_files(bank, client)
    assert "1 missing" in lines[0] and bank["items"]["IB-0001"]["assets"] == [] and any("is missing or unreadable in Drive" in l for l in lines)


def test_an_audit_counts_low_resolution_pictures_and_items_with_only_a_cover(drive):
    client, fake = drive
    bank = kept_bank("A", "B", "C")
    bank["items"]["IB-0001"]["platform"] = "tiktok"
    bank["items"]["IB-0002"]["platform"] = "tiktok"
    bank["items"]["IB-0003"]["platform"] = "meta_ads"
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(content=jpeg_for("small", (486, 648)))), S)
    IB.save_assets(bank, ["IB-0002"], "Swoveralls", client, getter(), S)
    IB.save_assets(bank, ["IB-0003"], "Swoveralls", client, getter(), S)
    line = IB.audit_files(bank, client)[0]
    assert "1 low resolution" in line and "2 item(s) have only a cover picture, not the video" in line


def test_an_audit_with_nothing_saved_downloads_nothing(drive):
    client, fake = drive
    before = len(fake.calls)
    assert IB.audit_files(kept_bank("A"), client)[0].startswith("audit: 0 saved file(s)") and len(fake.calls) == before


def test_an_audit_lists_at_most_twelve_problems_and_says_how_many_more(drive):
    client, fake = drive
    bank = saved_bank(drive, *"ABCDEFGHIJKLMNOP")
    for item in bank["items"].values():
        a = item["assets"][0]
        fake.files[a["drive_id"]]["data"] = b"broken"
    lines = IB.audit_files(bank, client)
    assert len([l for l in lines if "is broken" in l]) == 12 and lines[-1] == "audit: ... and 4 more" and "16 broken" in lines[0]


def test_a_run_with_audit_repairs_what_it_finds_in_the_same_run(store, swov, drive):
    client, fake = drive
    bank = saved_bank(drive, "A")
    bad = bank["items"]["IB-0001"]["assets"][0]
    fake.files[bad["drive_id"]]["data"] = cut(fake.files[bad["drive_id"]]["data"], .4)
    store.write_ideabank("swoveralls", bank)
    new, lines = full_run(store, swov, (client, fake), audit=True)
    text = "\n".join(lines)
    again = store.read_ideabank("swoveralls")["items"]["IB-0001"]["assets"]
    assert "swoveralls: audit: 1 saved file(s) downloaded from Drive and checked: 0 complete" in text and "1 broken" in text and "files saved for 1 item(s)" in text
    assert len(again) == 1 and again[0]["verified"] and fake.files[again[0]["drive_id"]]["data"] == jpeg_for("https://cdn.example/A.jpg")


def test_a_run_without_audit_does_not_download_files_back(store, swov, drive):
    client, fake = drive
    bank = saved_bank(drive, "A")
    store.write_ideabank("swoveralls", bank)
    before = [c for c in fake.calls if c["params"].get("alt") == "media"]
    new, lines = full_run(store, swov, (client, fake))
    assert not any("audit" in l for l in lines) and len([c for c in fake.calls if c["params"].get("alt") == "media"]) == len(before)


def test_a_drive_failure_during_the_audit_is_a_line_not_a_crash(store, swov, drive):
    client, fake = drive
    bank = saved_bank(drive, "A")
    store.write_ideabank("swoveralls", bank)
    monkey = IB.audit_files
    IB.audit_files = lambda *a, **k: (_ for _ in ()).throw(DriveError("quota"))
    try:
        new, lines = full_run(store, swov, (client, fake), audit=True)
    finally:
        IB.audit_files = monkey
    assert "swoveralls: the audit of saved files stopped: quota" in lines


# ---- the real video of a post ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.tiktok.com/@creator/video/7652797339182140703", True), ("https://tiktok.com/@a.b_c/video/1", True), ("https://m.tiktok.com/@x/video/22", True),
    ("https://vm.tiktok.com/ZMabc123/", True), ("https://vt.tiktok.com/ZSabc/", True),
    ("https://www.instagram.com/reel/DKp83a2uTTd/", True), ("https://www.instagram.com/p/DZVk1KFmZlk/", True), ("https://www.instagram.com/tv/ABC123/", True),
    ("https://www.instagram.com/someone/reel/DKp83a2uTTd/", True), ("https://www.instagram.com/reels/DKp83a2uTTd/", True),
    ("https://www.tiktok.com/@creator", False), ("https://www.instagram.com/creator/", False), ("https://www.instagram.com/explore/tags/overalls/", False),
    ("https://www.facebook.com/ads/library/?id=123", False), ("https://nymag.com/strategist/tags/father%27s-day-gifts/", False), ("", False), (None, False)])
def test_which_pages_hold_a_video_yt_dlp_can_fetch(url, expected):
    assert V.video_page({"url": url}) is expected


def settings(**kw):
    return replace(S, **kw)


def kept_with_cover(*ids, url="https://www.tiktok.com/@c/video/1"):
    bank = kept_bank(*ids)
    for n, item in enumerate(bank["items"].values()):
        item.update({"platform": "tiktok", "url": f"https://www.tiktok.com/@c/video/{n + 1}", "video_tries": 0})
    return bank


@pytest.mark.parametrize("change,expected", [({}, True), ({"state": "removed"}, False), ({"state": "new"}, False), ({"video_tries": 2}, False), ({"video_tries": 1}, True),
                                              ({"url": "https://example.com/a"}, False), ({"assets": [{"kind": "video"}]}, False), ({"assets": [{"kind": "image"}]}, True)])
def test_which_items_still_need_their_real_video(change, expected):
    item = kept_with_cover("A")["items"]["IB-0001"]
    item.update({"assets": [], **change})
    assert IB.needs_video(item, S) is expected


def test_video_fetching_can_be_switched_off_or_limited_to_nothing():
    item = kept_with_cover("A")["items"]["IB-0001"]
    assert not IB.needs_video(item, settings(fetch_videos=False)) and not IB.needs_video(item, settings(video_fetches_per_run=0))


def runner_writing(data, log=None, name="video.mp4"):
    def runner(url, dest, cookies, max_mb, timeout):
        if log is not None:
            log.append((url, cookies, max_mb, timeout))
        (__import__("pathlib").Path(dest) / name).write_bytes(data)
        return __import__("pathlib").Path(dest) / name, ""
    return runner


def test_a_real_video_is_downloaded_checked_saved_beside_the_cover_and_proved(drive):
    client, fake = drive
    bank = kept_with_cover("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    log = []
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(tiny_mp4(seconds=20), log))
    item = bank["items"]["IB-0001"]
    assert saved == 1 and lines == [] and [a["kind"] for a in item["assets"]] == ["image", "video"] and item["assets"][1]["name"] == "IB-0001-video.mp4"
    assert item["assets"][1]["source"] == "page" and item["assets"][1]["verified"] and item["video_tries"] == 0 and item["video_status"] == "saved" and item["video_note"] == ""
    assert log == [("https://www.tiktok.com/@c/video/1", None, S.video_max_mb, S.video_timeout_seconds)]
    assert IB.preview_label(item) == "video saved and checked (20s, 720x1280)"
    assert sorted(n.strip() for n in fake.tree() if "IB-0001-" in n) == ["IB-0001-image.jpg", "IB-0001-video.mp4"]


def test_a_video_for_an_item_with_no_cover_still_gets_a_folder(drive):
    client, fake = drive
    bank = kept_with_cover("A")
    bank["items"]["IB-0001"]["media_url"] = ""
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(tiny_mp4()))
    assert bank["items"]["IB-0001"]["drive_folder_id"] and bank["items"]["IB-0001"]["assets"][0]["kind"] == "video"


def test_a_video_that_is_gone_is_a_reason_and_is_not_asked_for_again(drive):
    client, fake = drive
    bank = kept_with_cover("A")
    calls = []

    def runner(url, dest, cookies, max_mb, timeout):
        calls.append(url)
        return None, "Video not available"
    for _ in range(4):
        saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner)
    assert len(calls) == 1 and bank["items"]["IB-0001"]["video_status"] == "gone" and bank["items"]["IB-0001"]["video_note"] == "the post is no longer available"


@pytest.mark.parametrize("data,fragment", [(b"junk" * 500, "not a picture or video"), (cut(tiny_mp4(), .5), "cut short"), (jpeg_for("a"), "what came back is not a video")])
def test_what_comes_back_must_be_a_whole_video(drive, data, fragment):
    client, fake = drive
    bank = kept_with_cover("A")
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(data, name="video.mp4"))
    assert saved == 0 and fragment in bank["items"]["IB-0001"]["video_note"] and "the video was not usable" in lines[0] and bank["items"]["IB-0001"]["assets"] == []
    assert bank["items"]["IB-0001"]["video_status"] == "failed" and bank["items"]["IB-0001"]["video_tries"] == 1


def test_a_video_bigger_than_the_limit_is_refused(drive):
    client, fake = drive
    bank = kept_with_cover("A")
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, settings(video_max_mb=1), runner_writing(tiny_mp4(padding=1_200_000)))
    assert saved == 0 and bank["items"]["IB-0001"]["video_note"] == "a video bigger than 1 MB"


def test_only_so_many_videos_are_fetched_a_run_and_the_rest_are_announced(drive):
    client, fake = drive
    bank = kept_with_cover("A", "B", "C", "D", "E")
    log = []
    saved, lines = IB.save_videos(bank, list(bank["items"]), "Swoveralls", client, settings(video_fetches_per_run=2), runner_writing(tiny_mp4(), log))
    assert saved == 2 and len(log) == 2 and lines == ["3 more video(s) wait for the next run (limit 2 a run)"]


def test_a_run_saves_the_covers_then_the_real_videos_and_does_not_fetch_a_video_twice(store, swov, drive):
    client, fake = drive
    bank = kept_with_cover("A", "B")
    store.write_ideabank("swoveralls", bank)
    log = []
    new, lines = full_run(store, swov, (client, fake), video_runner=runner_writing(tiny_mp4(), log), cookies={})
    text = "\n".join(lines)
    items = store.read_ideabank("swoveralls")["items"]
    assert "files saved for 2 item(s)" in text and "real videos saved for 2 item(s)" in text and len(log) == 2
    assert all(IB.preview_label(it).startswith("video saved") for it in items.values())
    full_run(store, swov, (client, fake), video_runner=runner_writing(tiny_mp4(), log), cookies={})
    assert len(log) == 2


def test_a_run_without_yt_dlp_says_so_and_keeps_the_covers(store, swov, drive):
    client, fake = drive
    store.write_ideabank("swoveralls", kept_with_cover("A", "B"))
    new, lines = full_run(store, swov, (client, fake), video_runner=None)
    assert "swoveralls: 2 TikTok/Instagram video(s) not saved: yt-dlp is not available here, so only cover pictures are kept" in lines
    assert all(IB.preview_label(it) == "cover image only: the video has not been fetched yet" for it in store.read_ideabank("swoveralls")["items"].values())


def test_the_login_cookies_are_handed_to_the_video_download_as_a_file(store, swov, drive):
    client, fake = drive
    store.write_ideabank("swoveralls", kept_with_cover("A"))
    seen = {}

    def runner(url, dest, cookies, max_mb, timeout):
        seen["jar"] = open(cookies).read() if cookies else None
        return None, "no"
    cookie = '[{"name": "sessionid", "value": "abc", "domain": ".tiktok.com", "path": "/", "secure": true, "expirationDate": 1900000000}]'
    full_run(store, swov, (client, fake), video_runner=runner, cookies={"tiktok": cookie, "instagram": ""})
    assert seen["jar"].startswith("# Netscape HTTP Cookie File") and ".tiktok.com\tTRUE\t/\tTRUE\t1900000000\tsessionid\tabc" in seen["jar"]
    full_run(store, swov, (client, fake), video_runner=runner, cookies={})
    assert seen["jar"] is None


def test_a_video_is_only_fetched_for_a_kept_item(store, swov, drive):
    client, fake = drive
    bank = kept_with_cover("A", "B")
    bank["items"]["IB-0002"]["state"] = "removed"
    store.write_ideabank("swoveralls", bank)
    log = []
    full_run(store, swov, (client, fake), video_runner=runner_writing(tiny_mp4(), log), cookies={})
    assert [u for u, *_ in log] == ["https://www.tiktok.com/@c/video/1"]


def test_cookie_files_skip_what_is_not_usable():
    assert V.cookie_file(["", None, "not json"], "/tmp/none.txt") is None


def test_the_cookie_file_has_the_netscape_columns(tmp_path):
    text = '[{"name": "a", "value": "1", "domain": "www.instagram.com", "path": "/x", "secure": false}, {"name": "b", "value": "2", "domain": ".tiktok.com"}]'
    path = V.cookie_file([text], tmp_path / "jar.txt")
    lines = open(path).read().splitlines()
    assert lines[1] == "www.instagram.com\tFALSE\t/x\tFALSE\t0\ta\t1" and lines[2] == ".tiktok.com\tTRUE\t/\tTRUE\t0\tb\t2"


class Done:
    def __init__(self, code=0, stderr=""):
        self.returncode, self.stderr = code, stderr


def test_the_yt_dlp_command_is_careful_about_size_time_and_a_single_video(tmp_path):
    seen = {}

    def runner(command, **kw):
        seen["command"], seen["kw"] = command, kw
        (tmp_path / "video.mp4").write_bytes(b"x")
        return Done()
    path, why = V.ytdlp_download("https://www.tiktok.com/@a/video/1", str(tmp_path), "/tmp/jar.txt", 80, 150, runner=runner)
    cmd = seen["command"]
    assert path == tmp_path / "video.mp4" and why == "" and cmd[0] == "yt-dlp" and cmd[-1] == "https://www.tiktok.com/@a/video/1"
    assert "--no-playlist" in cmd and cmd[cmd.index("--max-filesize") + 1] == "80M" and cmd[cmd.index("--cookies") + 1] == "/tmp/jar.txt" and cmd[cmd.index("-f") + 1] == "b[ext=mp4]/b"
    assert seen["kw"]["timeout"] == 150
    seen.clear()
    V.ytdlp_download("https://x/y", str(tmp_path), None, 80, 150, runner=runner)
    assert "--cookies" not in seen["command"]


@pytest.mark.parametrize("runner,expected", [
    (lambda c, **k: Done(1, "WARNING: x\nERROR: [TikTok] 7652797339182140703: Video not available\n"), "Video not available"),
    (lambda c, **k: Done(1, "ERROR: [Instagram] DKp83a2uTTd: Requested content is not available, rate-limit reached or login required"), "Requested content is not available, rate-limit reached or login required"),
    (lambda c, **k: Done(1, ""), "yt-dlp could not download it"), (lambda c, **k: Done(0, ""), "yt-dlp could not download it"),
    (lambda c, **k: (_ for _ in ()).throw(FileNotFoundError()), "yt-dlp is not installed"),
    (lambda c, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("yt-dlp", 150)), "yt-dlp gave up after 150 seconds")])
def test_a_failed_download_gives_the_real_reason(tmp_path, runner, expected):
    path, why = V.ytdlp_download("https://x/y", str(tmp_path), None, 80, 150, runner=runner)
    assert path is None and why == expected


def test_half_finished_downloads_are_not_taken_for_the_video(tmp_path):
    def runner(command, **kw):
        (tmp_path / "video.mp4.part").write_bytes(b"half")
        return Done(0)
    assert V.ytdlp_download("https://x/y", str(tmp_path), None, 80, 150, runner=runner)[0] is None


# ---- settings, workflow, dependencies, documentation ----------------------------------------------------------------------------------------

def test_the_video_settings_have_safe_defaults_and_are_checked(tmp_path):
    import yaml
    assert (S.fetch_videos, S.video_fetches_per_run, S.video_timeout_seconds) == (True, 12, 150)
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"ideabank": {"fetch_videos": False, "video_fetches_per_run": 3, "video_timeout_seconds": 60}}))
    assert (IB.load_settings(path).fetch_videos, IB.load_settings(path).video_fetches_per_run, IB.load_settings(path).video_timeout_seconds) == (False, 3, 60)
    for bad, match in (({"video_fetches_per_run": -1}, "at least 0"), ({"video_timeout_seconds": 5}, "at least 10"), ({"fetch_videos": "yes"}, "true or false")):
        path.write_text(yaml.safe_dump({"ideabank": bad}))
        with pytest.raises(IB.IdeaBankError, match=match):
            IB.load_settings(path)


def test_the_tidy_workflow_audits_the_saved_files_and_the_step_can_use_the_logins():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))
    step = next(s for s in wf["jobs"]["collect"]["steps"] if s.get("name") == "Add new items to the Idea Bank")
    assert step["env"]["MODE"] == "${{ inputs.mode }}" and step["env"]["INSTAGRAM_COOKIES"] == "${{ secrets.INSTAGRAM_COOKIES }}" and step["env"]["TIKTOK_COOKIES"] == "${{ secrets.TIKTOK_COOKIES }}"
    assert 'if [ "$MODE" = "tidy" ]; then args+=(--audit); fi' in step["run"]


def test_the_picture_checker_and_the_video_downloader_are_installed_with_the_tool():
    text = open("requirements.txt", encoding="utf-8").read().lower()
    assert "pillow" in text and "yt-dlp" in text


def test_the_readme_explains_file_checks_the_audit_labels_and_videos():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("Saved files are checked", "cover image only", "audit", "yt-dlp", "not yet checked", "low resolution", "the real type", "no saved media",
                   "it has no video", "carousel: first slide saved", "Careful mode (slower, smaller, spread out) is OFF by default"):
        assert needle in text, needle


class Untouchable(Media):
    """A response whose body must never be read."""

    def iter_content(self, size):
        raise AssertionError("the body was downloaded")


def test_a_video_is_refused_before_its_body_is_downloaded_when_videos_are_switched_off():
    with pytest.raises(IB.AssetSkip, match="saving videos is switched off"):
        IB.fetch_media_checked(sequence(Untouchable(mime="video/mp4", content=tiny_mp4())), "https://c.example/v", replace(S, save_videos=False))


def test_a_video_that_turns_out_to_be_a_video_is_refused_too_if_the_server_called_it_a_picture():
    with pytest.raises(IB.AssetSkip, match="saving videos is switched off"):
        IB.fetch_media_checked(sequence(Media(mime="image/jpeg", content=tiny_mp4())), "https://c.example/v", replace(S, save_videos=False))
