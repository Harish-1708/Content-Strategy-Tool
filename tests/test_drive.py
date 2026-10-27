"""The Shared Drive client: the folder layout, safe names, uploads of any size, and the setup mistakes explained as fixes."""
import json

import pytest

from contentintel import drive as D

from fake_drive import EMAIL, ROOT, FakeDrive


@pytest.fixture
def fake():
    return FakeDrive()


@pytest.fixture
def client(fake):
    return D.DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)


# ---- the folder address -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (ROOT, ROOT), (f"  {ROOT}\n", ROOT), (f"https://drive.google.com/drive/folders/{ROOT}", ROOT),
    (f"https://drive.google.com/drive/u/0/folders/{ROOT}?usp=sharing", ROOT), ("", None), ("  ", None), (None, None)])
def test_the_folder_is_found_from_its_address_or_its_bare_id(value, expected):
    assert D.parse_folder_id(value) == expected


@pytest.mark.parametrize("bad", ["short", "https://example.com/folders", "has spaces in it which is long enough", "https://drive.google.com/file/d/abc",
                                 "https://drive.google.com/drive/folders/short"])
def test_anything_else_is_refused_with_the_fix(bad):
    with pytest.raises(D.DriveError, match="IDEA_BANK_FOLDER_ID is not a Google Drive folder address or id"):
        D.parse_folder_id(bad)


def test_the_connection_asks_for_drive_access_and_never_echoes_the_key(monkeypatch):
    seen = {}

    class Creds:
        @staticmethod
        def from_service_account_info(info, scopes):
            seen["scopes"] = scopes
            return "creds"

    import google.auth.transport.requests as gr
    import google.oauth2.service_account as sa
    monkeypatch.setattr(sa, "Credentials", Creds)
    monkeypatch.setattr(gr, "AuthorizedSession", lambda creds: ("session", creds))
    session, email = D.connect(json.dumps({"client_email": EMAIL, "private_key": "SECRET-PEM"}))
    assert seen["scopes"] == ["https://www.googleapis.com/auth/drive"] and email == EMAIL and session == ("session", "creds")


@pytest.mark.parametrize("key,match", [("nope", "not valid JSON"), ("[]", "not a service account key"), (json.dumps({"client_email": EMAIL}), "not a service account key"),
                                       (None, "not valid JSON")])
def test_a_bad_key_is_explained_without_showing_it(key, match):
    with pytest.raises(D.DriveError, match=match):
        D.connect(key)


# ---- names -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("comfort hook demo", "comfort hook demo"), ("a/b\\c:d*e?f\"g<h>i|j", "a b c d e f g h i j"), ("  lots   of\n\nspace\t here ", "lots of space here"),
    ("trailing dots...", "trailing dots"), ("", "untitled"), (None, "untitled"), ("///", "untitled"), ("café ☕ idea", "café ☕ idea")])
def test_names_are_made_safe_and_readable(text, expected):
    assert D.safe_name(text) == expected


def test_a_long_name_is_cut_with_an_ellipsis_to_the_limit():
    name = D.safe_name("word " * 60)
    assert len(name) == D.NAME_LIMIT and name.endswith("…")
    assert D.safe_name("x" * 200, limit=20) == "x" * 19 + "…"


@pytest.mark.parametrize("idea,topic,expected", [("IB-0001", "comfort hook", "IB-0001 - comfort hook"), ("IB-0012", "", "IB-0012"),
                                                 ("IB-0012", "   ", "IB-0012"), ("IB-0003", None, "IB-0003"), ("IB-0004", "a/b", "IB-0004 - a b")])
def test_an_item_folder_is_named_by_its_id_and_topic(idea, topic, expected):
    assert D.item_folder_name(idea, topic) == expected


def test_addresses():
    assert D.folder_url("ABC") == "https://drive.google.com/drive/folders/ABC"
    assert D.file_url("XYZ") == "https://drive.google.com/file/d/XYZ/view"


# ---- the layout -------------------------------------------------------------------------------------------------------------

def test_a_folder_is_made_once_and_found_afterwards(client, fake):
    first = client.ensure_folder(ROOT, "Swoveralls")
    second = client.ensure_folder(ROOT, "Swoveralls")
    assert first == second and fake.children(ROOT) == ["Swoveralls"]


def test_the_layout_is_root_then_brand_then_one_folder_per_item(client, fake):
    brand = client.brand_folder("Swoveralls")
    client.item_folder(brand, "IB-0001", "comfort hook demo")
    client.item_folder(brand, "IB-0002", "gift guide reel")
    client.briefs_folder(brand)
    other = client.brand_folder("DudeRobe")
    client.item_folder(other, "IB-0001", "robe haul")
    assert fake.tree() == ["DudeRobe/", "  IB-0001 - robe haul/", "Swoveralls/", "  Briefs/", "  IB-0001 - comfort hook demo/", "  IB-0002 - gift guide reel/"]


def test_each_brand_gets_its_own_folder_and_the_same_id_can_exist_in_two_brands(client):
    a, b = client.brand_folder("Swoveralls"), client.brand_folder("DudeRobe")
    assert a != b and client.item_folder(a, "IB-0001", "x") != client.item_folder(b, "IB-0001", "x")


def test_names_with_quotes_and_backslashes_are_found_again(client, fake):
    odd = client.ensure_folder(ROOT, "Kid's \"best\" back\\slash")
    assert client.ensure_folder(ROOT, "Kid's \"best\" back\\slash") == odd and len([c for c in fake.children(ROOT)]) == 1
    query = [c for c in fake.calls if c["params"].get("q")][0]["params"]["q"]
    assert "\\'" in query


def test_the_search_for_a_folder_is_limited_to_its_parent_and_to_live_folders(client, fake):
    client.ensure_folder(ROOT, "Swoveralls")
    params = fake.calls[0]["params"]
    assert f"'{ROOT}' in parents" in params["q"] and "trashed = false" in params["q"] and "mimeType = 'application/vnd.google-apps.folder'" in params["q"]
    assert params["supportsAllDrives"] == "true" and params["includeItemsFromAllDrives"] == "true" and params["corpora"] == "allDrives"


def test_a_new_folder_is_made_in_the_right_parent_in_a_shared_drive_way(client, fake):
    client.ensure_folder(ROOT, "Swoveralls")
    create = [c for c in fake.calls if c["method"] == "POST"][0]
    assert create["json"] == {"name": "Swoveralls", "mimeType": D.FOLDER, "parents": [ROOT]} and create["params"]["supportsAllDrives"] == "true"


def test_the_listing_of_a_folder_follows_every_page(client, fake):
    folder = client.ensure_folder(ROOT, "Swoveralls")
    for n in range(205):
        client.upload(folder, f"file{n:03d}.jpg", b"x", "image/jpeg")
    assert len(client.list_files(folder)) == 205
    assert sum(1 for c in fake.calls if "nextPageToken" in str(c["params"].get("fields", ""))) == 2


# ---- files -------------------------------------------------------------------------------------------------------------------

def test_a_small_file_is_sent_in_one_piece_with_its_bytes_intact(client, fake):
    folder = client.ensure_folder(ROOT, "Swoveralls")
    data = bytes(range(256)) * 10 + b"\r\n--end\r\n"                  # includes bytes that look like multipart edges
    fid = client.upload(folder, "IB-0001-image.jpg", data, "image/jpeg")
    stored = fake.files[fid]
    assert stored["data"] == data and stored["name"] == "IB-0001-image.jpg" and stored["parents"] == [folder] and stored["mime"] == "image/jpeg"
    assert fake.calls[-1]["params"]["uploadType"] == "multipart"


def test_a_large_file_goes_by_the_resumable_route_with_its_size_and_type_declared(client, fake):
    folder = client.ensure_folder(ROOT, "Swoveralls")
    data = b"v" * (D.SMALL_UPLOAD + 10)
    fid = client.upload(folder, "IB-0002-video.mp4", data, "video/mp4")
    assert fake.files[fid]["data"] == data and fake.files[fid]["mime"] == "video/mp4"
    start = [c for c in fake.calls if c["params"].get("uploadType") == "resumable"][0]
    assert start["headers"] == {"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(data))}
    assert start["json"] == {"name": "IB-0002-video.mp4", "parents": [folder]}


def test_a_file_of_exactly_the_small_limit_is_still_sent_in_one_piece(client, fake):
    folder = client.ensure_folder(ROOT, "x")
    client.upload(folder, "a.bin", b"a" * D.SMALL_UPLOAD, "application/octet-stream")
    assert fake.calls[-1]["params"]["uploadType"] == "multipart"


def test_a_resumable_start_with_no_address_saves_nothing(client, fake):
    folder = client.ensure_folder(ROOT, "x")
    fake.script = [(200, {})]
    with pytest.raises(D.DriveError, match="did not say where to send a large file"):
        client.upload(folder, "big.mp4", b"v" * (D.SMALL_UPLOAD + 1), "video/mp4")


def test_a_file_name_is_made_safe_before_it_is_sent(client, fake):
    folder = client.ensure_folder(ROOT, "x")
    fid = client.upload(folder, "a/b:c.jpg", b"1", "image/jpeg")
    assert fake.files[fid]["name"] == "a b c.jpg"


def test_a_file_comes_back_as_it_went(client):
    folder = client.ensure_folder(ROOT, "x")
    fid = client.upload(folder, "p.png", b"\x89PNG\r\n\x1a\n" + bytes(300), "image/png")
    assert client.download(fid) == b"\x89PNG\r\n\x1a\n" + bytes(300)


def test_a_file_can_be_removed(client, fake):
    folder = client.ensure_folder(ROOT, "x")
    fid = client.upload(folder, "p.png", b"1", "image/png")
    client.delete(fid)
    assert fid not in fake.files


# ---- the check ---------------------------------------------------------------------------------------------------------------

def test_the_check_proves_the_folder_and_leaves_nothing_behind(client, fake):
    assert client.check() == {"name": "Idea Bank", "left_behind": ""}
    assert fake.children(ROOT) == []
    kinds = [(c["method"], bool(c["json"])) for c in fake.calls if c["method"] in ("POST", "DELETE")]
    assert kinds == [("POST", True), ("DELETE", False)]


def test_a_root_that_is_a_file_is_refused():
    fake = FakeDrive(root_is_folder=False)
    with pytest.raises(D.DriveError, match="is a file, not a folder"):
        D.DriveClient(fake, ROOT, EMAIL).check()


def test_a_folder_in_a_personal_drive_is_refused_with_the_reason():
    fake = FakeDrive(shared=False)
    with pytest.raises(D.DriveError, match="personal My Drive, where a service account has no storage"):
        D.DriveClient(fake, ROOT, EMAIL).check()
    assert len(fake.calls) == 1                                          # nothing was created


@pytest.mark.parametrize("status,body,said", [
    (404, {"error": {"message": "File not found: abc"}}, "Google Drive answered 404: File not found: abc."),
    (403, {"error": {"message": "The user does not have sufficient permissions for this file."}}, "Google Drive answered 403: The user does not have sufficient permissions for this file."),
    (500, None, "Google Drive answered 500.")])
def test_a_service_account_that_cannot_remove_the_test_folder_still_passes_with_googles_own_reason(status, body, said):
    fake = FakeDrive(delete_error=(status, body))
    out = D.DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None).check()
    assert out == {"name": "Idea Bank", "left_behind": said} and fake.children(ROOT) == [D.TEST_FOLDER]


def test_the_leftover_test_folder_is_reused_by_the_next_check_so_none_pile_up():
    fake = FakeDrive(delete_error=(404, {}))
    client = D.DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None)
    client.check()
    client.check()
    assert fake.children(ROOT) == [D.TEST_FOLDER]


def test_a_failed_delete_is_never_blamed_on_the_folder_id():
    fake = FakeDrive()
    with pytest.raises(D.DriveError) as err:
        D.DriveClient(fake, ROOT, EMAIL).delete("missing")
    assert str(err.value) == "Google Drive answered 404: File not found." or str(err.value).startswith("Google Drive answered 404") and "IDEA_BANK_FOLDER_ID" not in str(err.value)


# ---- Google's complaints, as fixes -----------------------------------------------------------------------------------------

def fail(status, body, retries=0):
    fake = FakeDrive(script=[(status, body)] * (retries + 1))
    with pytest.raises(D.DriveError) as err:
        D.DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None).find_folder(ROOT, "x")
    return str(err.value)


def test_the_drive_api_switched_off_names_the_link():
    link = "https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=12345"
    msg = fail(403, {"error": {"message": f"Google Drive API has not been used in project 12345 before or it is disabled. Enable it by visiting {link}", "details": [{"reason": "SERVICE_DISABLED"}]}})
    assert "Google Drive API is not switched on" in msg and link in msg and "press Enable" in msg


def test_the_drive_api_switched_off_without_a_link_points_at_the_library():
    assert "https://console.cloud.google.com/apis/library/drive.googleapis.com" in fail(403, {"error": {"message": "x", "details": [{"reason": "SERVICE_DISABLED"}]}})


def test_a_personal_drive_error_from_google_is_explained():
    msg = fail(403, {"error": {"message": "Service Accounts do not have storage quota.", "errors": [{"reason": "storageQuotaExceeded"}]}})
    assert "personal My Drive" in msg and "Shared Drive" in msg and EMAIL not in msg


def test_a_folder_not_shared_names_the_address_to_add():
    msg = fail(403, {"error": {"message": "The caller does not have permission"}})
    assert EMAIL in msg and "Content manager" in msg and "Editor" in msg and "not switched on" not in msg


def test_a_wrong_folder_id_says_to_check_the_secret():
    msg = fail(404, {"error": {"message": "File not found"}})
    assert "IDEA_BANK_FOLDER_ID" in msg and EMAIL in msg


def test_a_rejected_key_says_to_make_a_new_one():
    assert "Create a new key" in fail(401, {"error": {"message": "invalid"}})


@pytest.mark.parametrize("status,body,expect", [(400, {"error": {"message": "Bad query"}}, "Google Drive answered 400: Bad query."), (418, {}, "Google Drive answered 418."),
                                                (400, None, "Google Drive answered 400.")])
def test_other_complaints_are_passed_on_plainly(status, body, expect):
    assert expect in fail(status, body)


def test_a_busy_drive_is_retried_twice_with_a_growing_pause_then_succeeds():
    pauses = []
    fake = FakeDrive(script=[(429, {}), (503, {})])
    c = D.DriveClient(fake, ROOT, EMAIL, sleep=pauses.append)
    assert c.find_folder(ROOT, "nothing") is None
    assert pauses == [2, 4] and len(fake.calls) == 3


def test_a_drive_that_stays_busy_is_given_up_on_after_three_tries():
    pauses = []
    fake = FakeDrive(script=[(503, {})] * 3)
    with pytest.raises(D.DriveError, match="Google Drive answered 503"):
        D.DriveClient(fake, ROOT, EMAIL, sleep=pauses.append).find_folder(ROOT, "x")
    assert len(fake.calls) == 3 and pauses == [2, 4]


def test_real_errors_are_not_retried():
    fake = FakeDrive(script=[(403, {"error": {}}), (200, {"files": []})])
    with pytest.raises(D.DriveError):
        D.DriveClient(fake, ROOT, EMAIL, sleep=lambda s: pytest.fail("slept")).find_folder(ROOT, "x")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("message", ["Bad query.", "Bad query", "Bad query..."])
def test_googles_full_stop_is_never_doubled(message):
    assert fail(400, {"error": {"message": message}}).endswith("Google Drive answered 400: Bad query.")
