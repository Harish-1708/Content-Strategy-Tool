"""What a creative team needs to act on the Idea Bank, whatever the brand's industry.

Five things asked for after the first creative review: the same technique should not fill the bank (six "gift guide by dad type" ideas are one
idea), removed items should be visible and rescuable (the tool once dropped a 437-day ad with no way to see why), Claude should look at the saved
pictures (zero items were labelled AI though one plainly was), TikTok results found by search should still get a picture, and each idea should
come with a hook line, a first test, an audience and a channel."""
import io

import pytest

from contentintel import ideabank as IB
from contentintel import sheets
from contentintel.claude_client import Claude, ProviderError, TruncatedError
from contentintel.drive import DriveClient, DriveError
from contentintel.store import Store

from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from fake_http import FakeHttp
from fake_sheets import FakeClient
from media_fixtures import jpeg_for, png_for, tiny_mp4
from test_ideabank import NOW, S, Media, bank_with, full_run, getter, kept_bank, seeded
from test_idea_commands import BOT, cells, seed_bank
from test_sheets import baselined, one


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def budget(store, cap=100):
    return IB.ClaudeBudget(store, NOW, cap)


def techniques(*rows):
    """A bank of kept items: (id letter, score, mechanism)."""
    bank = kept_bank(*[r[0] for r in rows])
    for item, (_, score, mechanism) in zip(bank["items"].values(), rows):
        item.update({"score": score, "relevance": score, "mechanism": mechanism, "concept": f"idea of {mechanism}", "fit": 70, "idea": 70, "category": "same"})
    return bank


# ---- one technique, at most twice ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({"groups": [{"technique": "gift guide", "ids": ["IB-0001", "IB-0002", "IB-0003"]}]}, [("gift guide", ["IB-0001", "IB-0002", "IB-0003"])]),
    ({"groups": [{"technique": "x", "ids": ["IB-0001"]}]}, []),                                                            # a group of one is not a group
    ({"groups": [{"technique": "x", "ids": ["IB-0001", "IB-9999"]}]}, []),                                                  # an id that does not exist
    ({"groups": [{"technique": "a", "ids": ["IB-0001", "IB-0002"]}, {"technique": "b", "ids": ["IB-0002", "IB-0003"]}]}, [("a", ["IB-0001", "IB-0002"])]),
    ({"groups": [{"technique": "a", "ids": ["IB-0001", "IB-0001", "IB-0002"]}]}, [("a", ["IB-0001", "IB-0002"])]),
    ({"groups": [{"ids": ["IB-0001", "IB-0002"]}]}, [("same technique", ["IB-0001", "IB-0002"])]),
    ({"groups": ["junk", {"technique": "a", "ids": "IB-0001"}, {"technique": "a"}]}, []), ([], []), ("junk", []), ({}, [])])
def test_a_models_groups_are_checked_before_they_are_used(raw, expected):
    assert IB.clean_groups(raw, {"IB-0001", "IB-0002", "IB-0003"}) == expected


def tech_claude(*groups):
    c = FakeClaude()
    c.technique_answers = [{"groups": [{"technique": n, "ids": ids} for n, ids in groups]}]
    return c


def test_each_technique_keeps_its_best_two_and_says_why_the_rest_went(store):
    bank = techniques(("A", 80, "gift guide"), ("B", 70, "gift guide"), ("C", 60, "gift guide"), ("D", 50, "gift guide"), ("E", 65, "reaction clip"))
    claude = tech_claude(("gift guide", ["IB-0001", "IB-0002", "IB-0003", "IB-0004"]))
    removed, lines = IB.group_techniques(bank, claude, S, budget(store))
    items = bank["items"]
    assert removed == 2 and [items[i]["state"] for i in ("IB-0001", "IB-0002", "IB-0003", "IB-0004", "IB-0005")] == ["kept", "kept", "removed", "removed", "kept"]
    assert items["IB-0003"]["removed_reason"] == "another idea with the same technique (gift guide) scored higher: IB-0001, IB-0002" and items["IB-0003"]["duplicate_of"] == "IB-0001"
    assert items["IB-0001"]["technique"] == items["IB-0002"]["technique"] == "gift guide" and items["IB-0005"]["technique"] == ""
    assert lines == ["2 item(s) removed: the same technique as 2 better idea(s) (see the Rejected tab to rescue any)"] and len(claude.technique_calls) == 1


def test_a_proven_long_running_ad_outranks_a_higher_score_and_a_rescued_one_outranks_everything():
    bank = techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 60, "t"))
    bank["items"]["IB-0003"]["metrics"]["days_running"] = 300
    removed, _ = IB.group_techniques(bank, tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003"])), IB.IdeaBankSettings(max_per_technique=2), IB.ClaudeBudget(Store.__new__(Store), NOW, 0) if False else _free())
    assert removed == 1 and bank["items"]["IB-0002"]["state"] == "removed" and bank["items"]["IB-0003"]["state"] == "kept"
    bank = techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 10, "t"))
    bank["items"]["IB-0003"]["rescued"] = True
    IB.group_techniques(bank, tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003"])), IB.IdeaBankSettings(max_per_technique=1), _free())
    assert [bank["items"][i]["state"] for i in ("IB-0001", "IB-0002", "IB-0003")] == ["removed", "removed", "kept"]


class _free:
    """A budget that never runs out."""
    cap = 10 ** 9

    def take(self):
        return True


def test_a_higher_limit_keeps_more_and_zero_switches_it_off():
    bank = techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 60, "t"), ("D", 50, "t"))
    IB.group_techniques(bank, tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003", "IB-0004"])), IB.IdeaBankSettings(max_per_technique=3), _free())
    assert [bank["items"][i]["state"] for i in bank["items"]] == ["kept", "kept", "kept", "removed"]
    claude = tech_claude(("t", ["IB-0001", "IB-0002"]))
    assert IB.group_techniques(techniques(("A", 1, "t"), ("B", 2, "t"), ("C", 3, "t")), claude, IB.IdeaBankSettings(max_per_technique=0), _free()) == (0, []) and not claude.technique_calls


def test_no_call_is_spent_when_there_is_nothing_to_compare_or_no_claude_or_no_budget(store):
    claude = tech_claude(("t", ["IB-0001", "IB-0002"]))
    assert IB.group_techniques(techniques(("A", 1, "t"), ("B", 2, "t")), claude, S, _free()) == (0, [])                            # fewer than three
    assert IB.group_techniques(techniques(("A", 1, "t"), ("B", 2, "t"), ("C", 3, "t")), None, S, _free()) == (0, [])
    assert IB.group_techniques(techniques(("A", 1, "t"), ("B", 2, "t"), ("C", 3, "t")), claude, S, None) == (0, [])
    bank = bank_with("A", "B", "C")
    for it in bank["items"].values():
        it.update({"state": "kept", "score": 80, "summary": "s"})                                                               # no technique or idea known yet
    assert IB.group_techniques(bank, claude, S, _free()) == (0, []) and not claude.technique_calls


def test_a_spent_budget_or_a_failed_call_removes_nothing_and_says_so(store):
    bank = techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 60, "t"))
    removed, lines = IB.group_techniques(bank, tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003"])), IB.IdeaBankSettings(max_per_technique=1), budget(store, 0))
    assert removed == 0 and lines == ["techniques not compared: the monthly Claude limit (0 calls) is reached"]
    claude = FakeClaude()
    claude.technique_answers = [ProviderError("Claude: overloaded")]
    removed, lines = IB.group_techniques(bank, claude, S, _free())
    assert removed == 0 and lines == ["techniques not compared: Claude: overloaded"] and all(i["state"] == "kept" for i in bank["items"].values())


def test_the_comparison_lists_the_best_150_with_their_technique_and_idea():
    bank = techniques(*[(f"{n:03d}", n, f"tech {n}") for n in range(1, 201)])
    claude = FakeClaude()
    IB.group_techniques(bank, claude, S, _free())
    listing = claude.technique_calls[0][1].splitlines()[1:]
    assert len(listing) == 150 and listing[0].startswith("IB-0200: technique: tech 200; idea: idea of tech 200") and "score 200" in listing[0] and "IB-0051" in listing[-1]


def test_a_run_compares_techniques_and_says_how_many_went(store, swov, drive):
    store.write_ideabank("swoveralls", techniques(("A", 80, "t"), ("B", 70, "t"), ("C", 60, "t")))
    claude = tech_claude(("t", ["IB-0001", "IB-0002", "IB-0003"]))
    new, lines = full_run(store, swov, drive, claude, settings=IB.IdeaBankSettings(max_per_technique=2, vision=False))
    text = "\n".join(lines)
    assert "1 the same technique as better ideas" in text and "1 item(s) removed: the same technique as 2 better idea(s)" in text and len(claude.technique_calls) == 1
    assert store.read_ideabank("swoveralls")["items"]["IB-0003"]["state"] == "removed"


# ---- the Rejected tab and rescue ------------------------------------------------------------------------------------------------------------------

def removed_bank():
    bank = techniques(("A", 80, "t"), ("B", 40, "t"), ("C", 30, "u"))
    IB._remove(bank["items"]["IB-0002"], "not relevant enough (scored 40, the minimum is 50)")
    IB._remove(bank["items"]["IB-0003"], "a copy of IB-0001", "IB-0001")
    return bank


def test_the_rejected_rows_are_only_the_removed_items_newest_first_with_the_reason():
    rows = IB.rejected_rows(removed_bank())
    assert list(rows) == ["IB-0003", "IB-0002"] and list(rows["IB-0002"]) == IB.REJECTED_COLUMNS
    assert rows["IB-0002"]["removed_reason"] == "not relevant enough (scored 40, the minimum is 50)" and rows["IB-0003"]["duplicate_of"] == "IB-0001"
    assert rows["IB-0002"]["score"] == 40 and rows["IB-0002"]["mechanism"] == "t" and rows["IB-0002"]["preview"] == "no saved media: original link only"
    assert len(IB.rejected_rows(removed_bank(), limit=1)) == 1 and IB.rejected_rows(techniques(("A", 1, "t"))) == {}


class Sheet:
    def __init__(self, values):
        self.values = values

    def get_all_values(self):
        return self.values


@pytest.mark.parametrize("mark,rescued", [("Use", True), ("use", True), ("Maybe", True), ("yes", True), ("Skip", False), ("", False), ("no", False), ("nonsense", False)])
def test_only_use_or_maybe_in_the_rescue_column_rescues(mark, rescued):
    sheet = Sheet([["idea_id", "title", "Rescue"], ["IB-0002", "x", mark], ["junk", "y", "Use"], ["IB-0003", "z"]])
    assert IB.read_rescues(sheet) == (["IB-0002"] if rescued else [])


def test_a_sheet_with_no_rescue_column_or_no_rows_rescues_nothing():
    assert IB.read_rescues(Sheet([])) == [] and IB.read_rescues(Sheet([["idea_id", "title"], ["IB-0001", "x"]])) == [] and IB.read_rescues(Sheet([["title", "Rescue"]])) == []


def test_a_rescued_item_is_kept_and_nothing_removes_it_again():
    bank = removed_bank()
    rescued, lines = IB.rescue_items(bank, ["IB-0002", "IB-0001", "IB-9999"])
    item = bank["items"]["IB-0002"]
    assert rescued == 1 and lines == ["1 item(s) rescued from the Rejected tab and kept"] and item["state"] == "kept" and item["rescued"] and item["removed_reason"] == ""
    assert IB._remove(item, "again") is False and item["state"] == "kept"
    assert IB.apply_relevance(bank, IB.IdeaBankSettings(min_relevance=90)) == 1 and item["state"] == "kept"                  # below any minimum, still kept
    item["fingerprint"] = bank["items"]["IB-0001"]["fingerprint"] = "same"
    IB.tidy_duplicates(bank)
    assert item["state"] == "kept"


def test_a_rescued_item_with_no_score_gets_one_so_it_can_be_shown():
    bank = bank_with("A")
    IB._remove(bank["items"]["IB-0001"], "a copy of IB-0009", "IB-0009")
    IB.rescue_items(bank, ["IB-0001"])
    assert bank["items"]["IB-0001"]["score"] == 0 and IB.state_of(bank["items"]["IB-0001"]) == "kept"


def test_a_rescue_takes_the_folder_back_out_of_the_trash(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    item = bank["items"]["IB-0001"]
    folder = item["drive_folder_id"]
    IB._remove(item, "not relevant enough")
    IB.trash_folders(bank, client)
    assert fake.files[folder].get("trashed") and item["assets"] == [] and item["drive_folder_id"] == ""            # trashed, and the item let go of it
    IB.rescue_items(bank, ["IB-0001"], client)
    assert fake.files[folder]["trashed"] is False and item["assets"][0]["name"] == "IB-0001-image.jpg" and item["drive_folder_id"] == folder
    assert "trashed_folder" not in item and "trashed_assets" not in item


def test_a_folder_that_cannot_be_restored_is_forgotten_so_the_files_are_saved_afresh(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    item = bank["items"]["IB-0001"]
    IB._remove(item, "x")
    del fake.files[item["drive_folder_id"]]
    IB.rescue_items(bank, ["IB-0001"], client)
    assert item["assets"] == [] and item["drive_folder_id"] == "" and item["asset_tries"] == 0
    bank = kept_bank("A")
    item = bank["items"]["IB-0001"]
    item.update({"drive_folder_id": "gone", "assets": [{"name": "x"}]})
    IB._remove(item, "x")
    IB.rescue_items(bank, ["IB-0001"], None)
    assert item["assets"] == []


def test_a_run_brings_rescued_items_back_and_keeps_them(store, swov, drive):
    bank = removed_bank()
    store.write_ideabank("swoveralls", bank)
    new, lines = full_run(store, swov, drive, rescues=("IB-0002",), settings=IB.IdeaBankSettings(min_relevance=90, vision=False))
    got = store.read_ideabank("swoveralls")["items"]["IB-0002"]
    assert "swoveralls: 1 item(s) rescued from the Rejected tab and kept" in lines and got["state"] == "kept" and got["rescued"]


def test_the_rejected_tab_lists_what_was_removed_and_a_rescue_mark_is_read_back(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store, ids=("A", "B", "C"))
    IB._remove(bank["items"]["IB-0003"], "not relevant enough (scored 30, the minimum is 50)")
    store.write_ideabank("swoveralls", bank)
    client = FakeClient()
    report, error = one(swov, store, client)
    (sp,) = client.created
    ws, header, rows = cells(sp, "Rejected")
    assert error is None and header == IB.REJECTED_COLUMNS + ["Rescue"] and [r["idea_id"] for r in rows] == ["IB-0003"]
    assert rows[0]["removed_reason"].startswith("not relevant enough") and rows[0]["Rescue"] == ""
    ws._put(2, header.index("Rescue") + 1, "Use")
    assert sheets.fetch_rescues(swov, store, key_json="{}", share_emails=["me@kelson.agency"], config={}, connect_fn=lambda key: (client, BOT)) == ["IB-0003"]


def test_a_rescued_row_leaves_the_tab_unless_you_wrote_on_it(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = seed_bank(store, ids=("A", "B", "C"))
    for i in ("IB-0002", "IB-0003"):
        IB._remove(bank["items"][i], "not relevant enough")
    store.write_ideabank("swoveralls", bank)
    client = FakeClient()
    one(swov, store, client)
    ws, header, rows = cells(client.created[0], "Rejected")
    assert [r["idea_id"] for r in rows] == ["IB-0003", "IB-0002"]
    ws._put(2, header.index("Rescue") + 1, "Use")                                                             # IB-0003 is marked, IB-0002 is not
    bank = store.read_ideabank("swoveralls")
    IB.rescue_items(bank, ["IB-0002", "IB-0003"])
    store.write_ideabank("swoveralls", bank)
    one(swov, store, client)
    _, _, rows = cells(client.created[0], "Rejected") if any(w.title == "Rejected" for w in client.created[0].worksheets()) else (None, None, [])
    assert [r["idea_id"] for r in rows] == ["IB-0003"]


def test_there_is_no_rejected_tab_until_something_is_removed_and_a_missing_one_rescues_nothing(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store)
    client = FakeClient()
    one(swov, store, client)
    assert "Rejected" not in [w.title for w in client.created[0].worksheets()]
    assert sheets.fetch_rescues(swov, store, key_json="{}", share_emails=["me@kelson.agency"], config={}, connect_fn=lambda key: (client, BOT)) == []


def test_the_command_reads_rescues_and_never_fails_because_the_sheet_is_missing(store, swov, monkeypatch, capsys, tmp_path):
    from contentintel import cli
    config = tmp_path / "sheets.yaml"
    config.write_text('sheets:\n  swoveralls: "abc123"\n  duderobe: ""\n  defi_snacks: ""\n')

    class Args:
        dry_run = False
        sheets_config = str(config)
        brands_dir = "brands"
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert cli._rescues(swov, store, Args()) == ([], "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", lambda key: (_ for _ in ()).throw(ConnectionError("no network")))
    ids, note = cli._rescues(swov, store, Args())
    assert ids == [] and note.startswith("swoveralls: the Rejected tab (for rescues) could not be read (ConnectionError: no network)")
    Args.dry_run = True
    assert cli._rescues(swov, store, Args()) == ([], "")


# ---- Claude looks at the saved pictures ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry,expected", [
    ({"id": "IB-0001", "visual_hook": "A woman holds a bag", "look": "ugc", "quality": "good"}, {"visual_hook": "A woman holds a bag", "look": "ugc", "visual_quality": "good"}),
    ({"id": "IB-0001", "visual_hook": "x", "look": "AI", "quality": "OK"}, {"visual_hook": "x", "look": "ai-generated", "visual_quality": "ok"}),
    ({"id": "IB-0001", "visual_hook": "x", "look": "AI rendered", "quality": "poor"}, {"visual_hook": "x", "look": "ai-generated", "visual_quality": "poor"}),
    ({"id": "IB-0001", "visual_hook": "x", "look": "ai-generated"}, {"visual_hook": "x", "look": "ai-generated", "visual_quality": ""}),
    ({"id": "IB-0001", "visual_hook": "x", "look": "cartoon", "quality": "great"}, {"visual_hook": "x", "look": "unclear", "visual_quality": ""}),
    ({"id": "IB-0001", "look": "ugc"}, None), ({"id": "IB-0009", "visual_hook": "x"}, None), ("junk", None)])
def test_what_claude_says_about_a_picture_is_checked(entry, expected):
    got = IB.clean_visuals({"items": [entry]}, {"IB-0001"})
    assert (got.get("IB-0001") if expected else got) == (expected or {})


def pictured(*ids, **kw):
    bank = kept_bank(*ids)
    for item in bank["items"].values():
        item["assets"] = [{"name": "x", "drive_id": "d", "kind": "image", "bytes": 5}]
        item.update(kw)
    return bank


def vision_claude(*answers):
    c = FakeClaude()
    c.vision_answers = [{"items": [{"id": i, "visual_hook": f"hook {i}", "look": look, "quality": "good"} for i, look in a]} for a in answers]
    return c


def test_claude_looks_at_each_picture_and_an_ai_made_one_is_labelled_ai_whatever_its_text_said():
    bank = pictured("A", "B")
    bank["items"]["IB-0001"]["format"] = "static"
    bank["items"]["IB-0002"]["format"] = "video"
    claude = vision_claude((("IB-0001", "ai-generated"), ("IB-0002", "ugc")))
    done, lines = IB.review_visuals(bank, claude, S, _free(), lambda item: ("image/jpeg", b"pic"))
    a, b = bank["items"]["IB-0001"], bank["items"]["IB-0002"]
    assert done == 2 and lines == [] and (a["format"], a["look"], a["visual_hook"]) == ("ai", "ai-generated", "hook IB-0001") and (b["format"], b["look"]) == ("video", "ugc")
    system, user, images, _ = claude.vision_calls[0]
    assert system == IB.VISION_SYSTEM and len(images) == 2 and "Picture 1: IB-0001" in user and "Picture 2: IB-0002" in user


def test_pictures_go_in_batches_within_the_per_run_limit_and_are_not_looked_at_twice():
    bank = pictured(*"ABCDEFGHIJ")
    claude = vision_claude(*[[(f"IB-{n:04d}", "photo") for n in range(s, s + 4)] for s in (1, 5, 9)])
    done, lines = IB.review_visuals(bank, claude, IB.IdeaBankSettings(vision_batch=4, vision_calls_per_run=2), _free(), lambda item: ("image/jpeg", b"p"))
    assert done == 8 and len(claude.vision_calls) == 2 and lines == ["2 item(s) wait for a look at their picture: this run's limit of 2 calls is reached"]
    done, _ = IB.review_visuals(bank, claude, IB.IdeaBankSettings(vision_batch=4, vision_calls_per_run=2), _free(), lambda item: ("image/jpeg", b"p"))
    assert done == 2 and len(claude.vision_calls) == 3                                                         # only the two left over


def test_a_picture_that_cannot_be_loaded_costs_no_call_and_is_given_up_on_after_two_tries():
    bank = pictured("A")
    claude = vision_claude()
    for _ in range(4):
        IB.review_visuals(bank, claude, S, _free(), lambda item: None)
    assert not claude.vision_calls and bank["items"]["IB-0001"]["visual_tries"] == 2
    boom = lambda item: (_ for _ in ()).throw(DriveError("gone"))  # noqa: E731
    bank = pictured("A")
    assert IB.review_visuals(bank, claude, S, _free(), boom) == (0, []) and not claude.vision_calls


def test_a_failed_look_or_a_spent_budget_or_a_switch_off_changes_nothing(store):
    bank = pictured("A")
    claude = FakeClaude()
    claude.vision_answers = [ProviderError("Claude: overloaded")]
    done, lines = IB.review_visuals(bank, claude, S, _free(), lambda item: ("image/jpeg", b"p"))
    assert done == 0 and lines == ["1 picture(s) not reviewed: Claude: overloaded"] and bank["items"]["IB-0001"]["visual_hook"] == ""
    done, lines = IB.review_visuals(pictured("A"), vision_claude((("IB-0001", "ugc"),)), S, budget(store, 0), lambda item: ("image/jpeg", b"p"))
    assert done == 0 and lines == ["1 item(s) wait for a look at their picture: the monthly Claude limit (0 calls) is reached"]
    for settings in (IB.IdeaBankSettings(vision=False), IB.IdeaBankSettings(vision_calls_per_run=0)):
        c = vision_claude((("IB-0001", "ugc"),))
        assert IB.review_visuals(pictured("A"), c, settings, _free(), lambda item: ("image/jpeg", b"p")) == (0, []) and not c.vision_calls
    assert IB.review_visuals(pictured("A"), object(), S, _free(), lambda item: ("image/jpeg", b"p")) == (0, [])


def test_only_kept_items_with_a_saved_file_and_no_look_yet_are_candidates():
    bank = pictured("A", "B", "C", "D")
    bank["items"]["IB-0002"]["state"] = "removed"
    bank["items"]["IB-0003"]["assets"] = []
    bank["items"]["IB-0004"]["visual_hook"] = "already looked at"
    assert IB.vision_candidates(bank, S) == ["IB-0001"]


def test_the_picture_sent_to_claude_is_shrunk_and_a_video_is_sent_as_one_frame(drive, monkeypatch):
    client, fake = drive
    folder = client.brand_folder("Swoveralls")
    big = client.upload(folder, "big.png", png_for("a", (2400, 3200)), "image/png")
    small = client.upload(folder, "v.mp4", tiny_mp4(), "video/mp4")
    load = IB.load_picture_from_drive(client)
    media, data = load({"assets": [{"kind": "image", "drive_id": big, "bytes": 100}]})
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    assert media == "image/jpeg" and max(im.size) == 1200 and im.format == "JPEG"
    monkeypatch.setattr(IB.mediacheck, "extract_frame", lambda d: jpeg_for("frame"))
    assert load({"assets": [{"kind": "video", "drive_id": small, "bytes": 100}]})[0] == "image/jpeg"
    monkeypatch.setattr(IB.mediacheck, "extract_frame", lambda d: None)
    assert load({"assets": [{"kind": "video", "drive_id": small, "bytes": 100}]}) is None
    assert load({"assets": []}) is None and load({"assets": [{"kind": "image", "drive_id": big, "bytes": 10 ** 9}]}) is None


def test_a_picture_that_cannot_be_shrunk_is_sent_as_it_is(monkeypatch):
    monkeypatch.setattr(IB.mediacheck, "Image", None)
    assert IB._shrunk(jpeg_for("a")) == ("image/jpeg", jpeg_for("a")) and IB._shrunk(b"junk")[0] == "image/jpeg"


def claude_with(reply, status=200):
    http = FakeHttp().on("POST", "https://api.anthropic.com/v1/messages", (status, reply))
    return Claude("key", http=http, sleep=lambda s: None), http


def test_the_pictures_travel_with_the_question_and_the_answer_is_read_as_json():
    reply = {"content": [{"type": "text", "text": '{"items": [{"id": "IB-0001", "look": "ugc"}]}'}], "stop_reason": "end_turn"}
    claude, http = claude_with(reply)
    got = claude.json_task_with_images("system", "Picture 1: IB-0001", [("image/jpeg", b"abc"), ("image/png", b"def")], max_tokens=900)
    body = http.calls[0]["json"]
    content = body["messages"][0]["content"]
    assert got == {"items": [{"id": "IB-0001", "look": "ugc"}]} and body["system"] == "system" and body["max_tokens"] == 900
    assert [c["type"] for c in content] == ["image", "image", "text"] and content[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "YWJj"}
    assert content[1]["source"]["media_type"] == "image/png" and content[2]["text"] == "Picture 1: IB-0001"


def test_an_answer_cut_off_or_empty_is_an_error_not_half_an_answer():
    with pytest.raises(TruncatedError):
        claude_with({"content": [{"type": "text", "text": '{"items": ['}], "stop_reason": "max_tokens"})[0].json_task_with_images("s", "u", [("image/jpeg", b"x")])
    with pytest.raises(ProviderError, match="no text"):
        claude_with({"content": [], "stop_reason": "end_turn"})[0].json_task_with_images("s", "u", [("image/jpeg", b"x")])


def test_a_run_looks_at_the_saved_pictures_and_the_sheet_shows_what_was_seen(store, swov, drive):
    client, fake = drive
    seeded(store)
    claude = FakeClaude()
    claude.json_answers = [{"items": [{"id": "IB-0001", "relevance": 90, "topic": "a", "summary": "s", "how_to_use": "h", "risk": "none"},
                                       {"id": "IB-0002", "relevance": 90, "topic": "b", "summary": "s", "how_to_use": "h", "risk": "none"}]}]
    claude.vision_answers = [{"items": [{"id": "IB-0001", "visual_hook": "A rendered man in overalls", "look": "ai-generated", "quality": "good"},
                                         {"id": "IB-0002", "visual_hook": "A phone clip of a gift", "look": "ugc", "quality": "ok"}]}]
    new, lines = full_run(store, swov, drive, claude)
    rows = IB.sheet_rows(store.read_ideabank("swoveralls"))
    assert "2 saved picture(s) looked at" in "\n".join(lines) and (rows["IB-0001"]["look"], rows["IB-0001"]["format"]) == ("ai-generated", "ai")
    assert rows["IB-0002"]["visual_hook"] == "A phone clip of a gift" and rows["IB-0002"]["look"] == "ugc"


# ---- a picture for a TikTok found by search --------------------------------------------------------------------------------------------------------

class Json:
    def __init__(self, status=200, body=None, boom=None):
        self.status_code, self._body, self._boom = status, body, boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._body


@pytest.mark.parametrize("response,expected", [
    (Json(200, {"thumbnail_url": "https://p16.tiktokcdn.com/cover.jpg?x=1"}), "https://p16.tiktokcdn.com/cover.jpg?x=1"),
    (Json(200, {"thumbnail_url": "http://insecure.example/c.jpg"}), ""), (Json(200, {}), ""), (Json(404, {}), ""), (Json(200, None), ""),
    (Json(404, {"thumbnail_url": "https://p16.tiktokcdn.com/error-page.jpg"}), ""), (Json(500, {"thumbnail_url": "https://p16.tiktokcdn.com/x.jpg"}), ""),
    (Json(200, boom=ValueError("not json")), "")])
def test_tiktok_gives_the_thumbnail_of_its_own_video(response, expected):
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return response
    assert IB.tiktok_cover("https://www.tiktok.com/@c/video/1", get) == expected
    assert seen["url"] == "https://www.tiktok.com/oembed" and seen["params"] == {"url": "https://www.tiktok.com/@c/video/1"} and seen["timeout"] == 20


def test_a_lookup_that_fails_is_just_no_picture():
    def boom(url, **kw):
        raise ConnectionError("down")
    assert IB.tiktok_cover("https://www.tiktok.com/@c/video/1", boom) == ""


def web_posts(*urls):
    bank = kept_bank(*[chr(65 + n) for n in range(len(urls))])
    for item, url in zip(bank["items"].values(), urls):
        item.update({"platform": "web", "url": url, "media_url": "", "assets": []})
    return bank


def test_a_kept_tiktok_video_page_with_no_picture_gets_its_thumbnail_whichever_source_found_it():
    bank = web_posts("https://www.tiktok.com/@c/video/1")
    bank["items"]["IB-0001"]["platform"] = "tiktok"
    found, _ = IB.resolve_covers(bank, lambda url, **kw: Json(200, {"thumbnail_url": "https://p16.tiktokcdn.com/c.jpg"}), S)
    assert found == 1 and bank["items"]["IB-0001"]["media_role"] == "cover"


def test_a_kept_tiktok_web_result_gets_its_thumbnail_address_and_is_marked_a_cover():
    bank = web_posts("https://www.tiktok.com/@c/video/1", "https://www.instagram.com/reel/R1/", "https://www.tiktok.com/@creator", "https://nymag.com/a")
    bank["items"]["IB-0005"] = dict(bank["items"]["IB-0001"], id="IB-0005", url="https://www.tiktok.com/@d/video/2", media_url="https://cdn.example/own.jpg")
    asked = []

    def get(url, **kw):
        asked.append(kw["params"]["url"])
        return Json(200, {"thumbnail_url": "https://p16.tiktokcdn.com/c.jpg"})
    found, lines = IB.resolve_covers(bank, get, S)
    items = bank["items"]
    assert found == 1 and lines == ["1 TikTok thumbnail(s) found for web results that had no picture"] and asked == ["https://www.tiktok.com/@c/video/1"]
    assert (items["IB-0001"]["media_url"], items["IB-0001"]["media_role"]) == ("https://p16.tiktokcdn.com/c.jpg", "cover")
    assert all(items[i]["media_url"] == "" for i in ("IB-0002", "IB-0003", "IB-0004")) and items["IB-0005"]["media_url"] == "https://cdn.example/own.jpg"


def test_only_so_many_thumbnails_are_asked_for_a_run_and_removed_or_failed_items_are_skipped():
    bank = web_posts(*[f"https://www.tiktok.com/@c/video/{n}" for n in range(1, 6)])
    bank["items"]["IB-0002"]["state"] = "removed"
    bank["items"]["IB-0003"]["asset_tries"] = IB.ASSET_TRIES
    asked = []
    IB.resolve_covers(bank, lambda url, **kw: asked.append(1) or Json(404), IB.IdeaBankSettings(cover_lookups_per_run=1))
    assert len(asked) == 1
    asked.clear()
    IB.resolve_covers(bank, lambda url, **kw: asked.append(1) or Json(404), IB.IdeaBankSettings(cover_lookups_per_run=9))
    assert len(asked) == 3 and IB.resolve_covers(bank, lambda *a, **k: Json(404), S) == (0, [])


def test_a_thumbnail_found_by_search_is_saved_as_a_cover_and_labelled_so(store, swov, drive):
    client, fake = drive
    bank = web_posts("https://www.tiktok.com/@c/video/1")
    store.write_ideabank("swoveralls", bank)
    pics = getter(responses={"https://p16.tiktokcdn.com/c.jpg": Media(content=jpeg_for("thumb"))})

    def get(url, **kw):
        return Json(200, {"thumbnail_url": "https://p16.tiktokcdn.com/c.jpg"}) if "oembed" in url else pics(url, **kw)
    new, lines = full_run(store, swov, drive, get=get, settings=IB.IdeaBankSettings(fetch_videos=False, vision=False))
    item = store.read_ideabank("swoveralls")["items"]["IB-0001"]
    assert item["assets"][0]["source"] == "cover" and "1 TikTok thumbnail(s) found" in "\n".join(lines)
    assert IB.preview_label(item) == "cover image only: the video has not been fetched yet"


# ---- what a reviewer needs in order to act ----------------------------------------------------------------------------------------------------------

def test_the_scoring_instructions_ask_for_a_hook_line_a_first_test_an_audience_and_a_channel():
    for needle in ("hook_line", "first_test", "audience", "channel", "the cheapest first test", "paid social", "organic social", "creator", "email", "site", "mixed"):
        assert needle in IB.SCORE_SYSTEM, needle


def scored_entry(**kw):
    base = {"id": "IB-0001", "fit": 70, "idea": 70, "generic": False, "category": "same", "format": "ugc", "mechanism": "m", "concept": "c", "topic": "t", "summary": "s",
            "how_to_use": "h", "risk": "none"}
    base.update(kw)
    return base


@pytest.mark.parametrize("given,expected", [
    ({"hook_line": "Open on the zip", "first_test": "Make 3 clips", "audience": "dads", "channel": "Paid Social"}, ("Open on the zip", "Make 3 clips", "dads", "paid social")),
    ({"channel": "billboards"}, ("", "", "", "")), ({}, ("", "", "", "")), ({"hook_line": "x" * 500}, ("x" * 160, "", "", ""))])
def test_the_new_fields_are_read_clipped_and_the_channel_is_one_of_ours(given, expected):
    got = IB.clean_scores({"items": [scored_entry(**given)]}, {"IB-0001"})["IB-0001"]
    expected = tuple(IB._clip(e, 160) if n == 0 else e for n, e in enumerate(expected))
    assert (got["hook_line"], got["first_test"], got["audience"], got["channel"]) == expected
    assert len(got["hook_line"]) <= 160


def test_scoring_writes_the_new_fields_and_never_blanks_ones_already_there(store, swov):
    bank = bank_with("A", "B")
    bank["items"]["IB-0002"].update({"hook_line": "kept hook", "first_test": "kept test"})
    claude = FakeClaude()
    claude.json_answers = [{"items": [scored_entry(hook_line="New hook", first_test="New test", audience="parents", channel="creator"), scored_entry(id="IB-0002")]}]
    IB.score_items(bank, list(bank["items"]), claude, swov, store, S, budget(store), NOW)
    a, b = bank["items"]["IB-0001"], bank["items"]["IB-0002"]
    assert (a["hook_line"], a["first_test"], a["audience"], a["channel"]) == ("New hook", "New test", "parents", "creator") and (b["hook_line"], b["first_test"]) == ("kept hook", "kept test")


def test_the_sheet_has_the_technique_and_the_new_columns_next_to_the_idea():
    cols = IB.IDEABANK_COLUMNS
    assert cols[cols.index("how_to_use"):cols.index("how_to_use") + 6] == ["how_to_use", "hook_line", "first_test", "audience", "channel", "risk"]
    assert cols.index("mechanism") + 1 == cols.index("technique") and cols.index("preview") + 2 == cols.index("look") and "visual_hook" in cols
    bank = kept_bank("A")
    bank["items"]["IB-0001"].update({"hook_line": "h", "first_test": "t", "audience": "a", "channel": "email", "technique": "gift guide", "visual_hook": "v", "look": "ugc"})
    row = IB.sheet_rows(bank)["IB-0001"]
    assert [row[c] for c in ("hook_line", "first_test", "audience", "channel", "technique", "visual_hook", "look")] == ["h", "t", "a", "email", "gift guide", "v", "ugc"]
    assert list(row) == cols


@pytest.mark.parametrize("change,expected", [({}, True), ({"hook_line": "x"}, False), ({"first_test": "x"}, False), ({"state": "removed"}, False), ({"state": "new"}, False),
                                              ({"summary": "", "concept": ""}, False), ({"summary": "", "concept": "c"}, True)])
def test_which_items_still_need_their_hook_line_and_first_test(change, expected):
    item = kept_bank("A")["items"]["IB-0001"]
    item.update({"summary": "s", "concept": ""})
    item.update(change)
    assert IB.needs_enrichment(item) is expected


def enrich_claude(*batches):
    c = FakeClaude()
    c.enrich_answers = [{"items": [{"id": i, "hook_line": f"hook {i}", "first_test": f"test {i}", "audience": "everyone", "channel": "Creator"} for i in b]} for b in batches]
    return c


def test_older_ideas_get_the_new_fields_without_their_scores_changing(store, swov):
    bank = kept_bank("A", "B")
    before = [(i["score"], i["fit"], i["state"]) for i in bank["items"].values()]
    claude = enrich_claude(["IB-0001", "IB-0002"])
    filled, lines = IB.enrich_items(bank, claude, swov, store, S, budget(store))
    items = list(bank["items"].values())
    assert filled == 2 and lines == [] and [(i["score"], i["fit"], i["state"]) for i in items] == before
    assert (items[0]["hook_line"], items[0]["first_test"], items[0]["audience"], items[0]["channel"]) == ("hook IB-0001", "test IB-0001", "everyone", "creator")
    assert IB.enrich_items(bank, claude, swov, store, S, budget(store)) == (0, []) and len(claude.enrich_calls) == 1        # nothing left to fill


def test_enrichment_goes_in_batches_best_first_within_the_per_run_limit(store, swov):
    bank = kept_bank(*"ABCDEFGH")
    for n, item in enumerate(bank["items"].values()):
        item["score"] = 50 + n
    claude = enrich_claude(["IB-0008", "IB-0007", "IB-0006"], ["IB-0005", "IB-0004", "IB-0003"])
    filled, lines = IB.enrich_items(bank, claude, swov, store, IB.IdeaBankSettings(enrich_batch=3, enrich_calls_per_run=2), budget(store))
    assert filled == 6 and lines == ["2 item(s) wait for their hook line and first test: this run's limit of 2 calls is reached"]
    assert claude.enrich_calls[0][1].count("IB-0008") == 1 and "IB-0001" not in claude.enrich_calls[0][1] + claude.enrich_calls[1][1]


def test_enrichment_without_claude_budget_or_a_working_call_changes_nothing(store, swov):
    bank = kept_bank("A")
    assert IB.enrich_items(bank, None, swov, store, S, budget(store)) == (0, []) and IB.enrich_items(bank, FakeClaude(), swov, store, S, None) == (0, [])
    assert IB.enrich_items(bank, enrich_claude(["IB-0001"]), swov, store, IB.IdeaBankSettings(enrich_calls_per_run=0), budget(store)) == (0, [])
    filled, lines = IB.enrich_items(bank, enrich_claude(["IB-0001"]), swov, store, S, budget(store, 0))
    assert filled == 0 and lines == ["1 item(s) wait for their hook line and first test: the monthly Claude limit (0 calls) is reached"]
    c = FakeClaude()
    c.enrich_answers = [ProviderError("Claude: overloaded")]
    assert IB.enrich_items(bank, c, swov, store, S, budget(store)) == (0, ["1 item(s) got no hook line: Claude: overloaded"]) and bank["items"]["IB-0001"]["hook_line"] == ""


def test_an_answer_with_nothing_useful_or_other_ids_fills_nothing():
    assert IB.clean_enrichment({"items": [{"id": "IB-0001"}, {"id": "IB-0009", "hook_line": "x"}, "junk", {"id": "IB-0001", "audience": "only audience"}]}, {"IB-0001"}) == {}
    assert IB.clean_enrichment({"items": [{"id": "IB-0001", "first_test": "t", "channel": "radio"}]}, {"IB-0001"}) == {"IB-0001": {"hook_line": "", "first_test": "t", "audience": "", "channel": ""}}


def test_a_run_fills_the_new_fields_for_kept_ideas_and_says_so(store, swov, drive):
    store.write_ideabank("swoveralls", kept_bank("A", "B"))
    new, lines = full_run(store, swov, drive, enrich_claude(["IB-0001", "IB-0002"]), settings=IB.IdeaBankSettings(vision=False, max_per_technique=0))
    items = store.read_ideabank("swoveralls")["items"]
    assert "swoveralls: 2 item(s) got a hook line, first test, audience and channel" in lines and items["IB-0001"]["hook_line"] == "hook IB-0001"


# ---- settings and documentation ---------------------------------------------------------------------------------------------------------------------

def test_the_new_settings_have_defaults_and_are_checked(tmp_path):
    import yaml
    assert (S.max_per_technique, S.vision, S.vision_calls_per_run, S.vision_batch, S.enrich_calls_per_run, S.enrich_batch, S.cover_lookups_per_run) == (2, True, 3, 4, 4, 10, 20)
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"ideabank": {"max_per_technique": 3, "vision": False, "vision_batch": 2}}))
    got = IB.load_settings(path)
    assert (got.max_per_technique, got.vision, got.vision_batch) == (3, False, 2)
    for bad, match in (({"max_per_technique": -1}, "at least 0"), ({"vision_batch": 0}, "at least 1"), ({"enrich_batch": 0}, "at least 1"), ({"vision": "yes"}, "true or false")):
        path.write_text(yaml.safe_dump({"ideabank": bad}))
        with pytest.raises(IB.IdeaBankError, match=match):
            IB.load_settings(path)


def test_the_readme_explains_techniques_rescue_the_picture_review_and_the_new_columns():
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("max_per_technique", "Rejected tab", "Rescue", "looks at the saved pictures", "hook_line", "first_test", "ai-generated", "oEmbed", "vision_calls_per_run"):
        assert needle in text, needle


# ---- the format searches fit the brand's own kind of product ------------------------------------------------------------------------------------------

def test_format_searches_name_the_brands_own_kind_of_product_and_never_another_industry():
    from contentintel import planner as PL
    apparel_words = ("apparel", "loungewear", "clothing", "fashion", "try-on", "garment")
    assert PL.FORMAT_QUERIES is PL.QUERIES_BY_SCHEMA["apparel"] and PL.queries_for("apparel") is PL.FORMAT_QUERIES
    for schema in ("food", "something-new", ""):
        for fmt in PL.FORMATS:
            for query in PL.queries_for(schema)[fmt]:
                assert not any(w in query.lower() for w in apparel_words), (schema, query)
    assert PL.queries_for("food")["ugc"][0] == "UGC ad examples snack brand" and PL.queries_for("unknown") is PL.GENERIC_QUERIES
    assert set(PL.GENERIC_QUERIES) == set(PL.QUERIES_BY_SCHEMA["food"]) == set(PL.QUERIES_BY_SCHEMA["apparel"]) == set(PL.FORMATS)
    for query in sum(PL.GENERIC_QUERIES.values(), []):
        assert not any(w in query.lower() for w in ("snack", "food", "skin", "software")), query


def test_the_searches_for_a_request_follow_the_brand_and_keep_their_order_and_limit():
    from contentintel import planner as PL
    assert PL.format_queries(["ugc", "ai"], 4, "food") == ["UGC ad examples snack brand", "AI generated ad examples food brand", "UGC taste test reaction food brand",
                                                         "AI generated snack ad campaign"]
    assert PL.format_queries(["ugc", "ai"], 3) == PL.format_queries(["ugc", "ai"], 3, "apparel") and PL.format_queries(["ugc"], 1, "other") == ["UGC ad examples consumer brand"]
    assert PL.format_queries([], 5, "food") == [] and len(PL.format_queries(list(PL.FORMATS), 3, "food")) == 3


def test_a_plan_for_a_food_brand_searches_for_snack_examples_not_loungewear():
    from contentintel import planner as PL
    from test_planner import ALL, raw_plan
    food, _ = PL.clean_plan(raw_plan(), ALL, S, "x", ["ugc"], schema="food")
    apparel, _ = PL.clean_plan(raw_plan(), ALL, S, "x", ["ugc"])
    queries = lambda plan: " | ".join(str(q) for tool in plan["tools"].values() for q in (tool.get("queries") or []))  # noqa: E731
    assert "UGC ad examples snack brand" in queries(food) and "UGC creator try-on haul loungewear" not in queries(food) and "UGC ad examples apparel brand" not in queries(food)
    assert "UGC ad examples apparel brand" in queries(apparel) and "UGC ad examples snack brand" not in queries(apparel)


def test_a_request_for_defi_snacks_is_planned_with_its_own_searches(store):
    from conftest import BRANDS_DIR
    from contentintel.brands import load_brand
    from test_planner import make, raw_plan
    defi = load_brand("defi_snacks", BRANDS_DIR)
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    request, _ = make(store, defi, claude, formats=["ugc"])
    text = repr(request["plan"])
    assert "UGC ad examples snack brand" in text and "UGC taste test reaction food brand" in text and "UGC ad examples apparel brand" not in text


def test_a_revised_plan_for_a_food_brand_keeps_the_food_searches(store):
    from conftest import BRANDS_DIR
    from contentintel import planner as PL
    from contentintel.brands import load_brand
    from test_planner import make, raw_plan
    defi = load_brand("defi_snacks", BRANDS_DIR)
    claude = FakeClaude()
    claude.json_answers = [raw_plan(), raw_plan()]
    request, _ = make(store, defi, claude, formats=["ugc"])
    revised = PL.revise_request(store, defi, request, claude, _free(), S, changes="add more about gifts", now=NOW)
    text = repr(revised["plan"])
    assert "UGC ad examples snack brand" in text and "UGC ad examples apparel brand" not in text
