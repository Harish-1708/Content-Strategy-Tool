"""A clean Idea Bank: one original of every idea, nothing of the brand's own, nothing irrelevant, and the Drive folders and sheet rows of
everything that was removed taken away with it.

The first live fill (2026-10-27) put the same ad in the bank three times ("Bib Overalls That Feel Like a Day Off" as IB-0053, IB-0054 and
IB-0055; "Your nervous system called... It wants this" as IB-0057, IB-0058 and IB-0059) and included the brand's own content.
"""
from dataclasses import replace

import pytest

from contentintel import brief as BR
from contentintel import drive as D
from contentintel import ideabank as IB
from contentintel.drive import DriveClient
from contentintel.store import Store

from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from fake_sheets import FakeClient
from test_collectors import NOW
from test_idea_commands import cells, seed_bank
from media_fixtures import jpeg_for
from test_ideabank import Media, S, bank_with, full_run, getter, good, kept_bank, record, row
from test_sheets import baselined, one


def ad(ident, text="Bib overalls that feel like a day off. Soft, roomy and made to move.", handle="Acme", **kw):
    kw.setdefault("days_running", 30)
    return row("meta_ads", ident, handle=handle, title=kw.pop("title", "Bib Overalls That Feel Like a Day Off"), text=text,
               url=f"https://www.facebook.com/ads/library/?id={ident}", media_url=f"https://cdn.example/{ident}.jpg", **kw)


def make_item(**kw):
    bank = {"next": 1, "items": {}}
    base = {"source": "meta_ads", "ident": "1"}
    base.update(kw)
    r = ad(base["ident"]) if base["source"] == "meta_ads" else row(base["source"], base["ident"])
    IB.intake_collected(bank, [record([r])], S, NOW)
    item = bank["items"]["IB-0001"]
    item.update({k: v for k, v in kw.items() if k not in ("source", "ident")})
    return item


# ---- what makes two items the same idea ---------------------------------------------------------------------------------------------

def test_the_same_ad_under_different_ids_is_one_idea():
    a, b = make_item(ident="111"), make_item(ident="222")
    assert IB.fingerprint(a) == IB.fingerprint(b) != ""


def test_case_punctuation_and_spacing_do_not_make_a_different_ad():
    a = make_item(text="Bib overalls that feel like a day off!  Soft, roomy.")
    b = make_item(text="BIB OVERALLS that feel like a day off... soft roomy")
    assert IB.fingerprint(a) == IB.fingerprint(b)


def test_different_wording_is_a_different_idea():
    assert IB.fingerprint(make_item(text="A completely different line about gift cards and shipping")) != IB.fingerprint(make_item())


def test_an_ad_is_the_same_whoever_runs_it():
    assert IB.fingerprint(make_item(handle="One Brand")) == IB.fingerprint(make_item(handle="Another Brand"))


def test_an_ad_with_very_short_text_is_compared_by_its_headline():
    a, b = make_item(text="Shop now", title="Alpha headline"), make_item(text="Shop now", title="Beta headline")
    assert IB.fingerprint(a) != IB.fingerprint(b) and IB.fingerprint(a) == IB.fingerprint(make_item(text="Shop now", title="Alpha headline"))


def test_a_social_post_is_its_account_plus_its_words_so_a_common_caption_is_not_merged_across_accounts():
    a = make_item(source="instagram", ident="p1", handle="maker_one", text="love these overalls")
    b = make_item(source="instagram", ident="p2", handle="maker_two", text="love these overalls")
    c = make_item(source="instagram", ident="p3", handle="maker_one", text="Love these overalls!")
    assert IB.fingerprint(a) != IB.fingerprint(b) and IB.fingerprint(a) == IB.fingerprint(c)


@pytest.mark.parametrize("a,b", [("http://www.x.example/guide/", "https://x.example/guide"), ("https://x.example/guide?utm=1#top", "https://x.example/guide"),
                                 ("HTTPS://X.EXAMPLE/Guide", "https://x.example/guide")])
def test_a_web_result_is_its_address_without_the_decoration(a, b):
    bank = {"next": 1, "items": {}}
    first = IB._new_item("IB-0001", "web", "web:1", {"title": "t", "url": a}, NOW, None)
    second = IB._new_item("IB-0002", "web", "web:2", {"title": "t", "url": b}, NOW, None)
    assert IB.fingerprint(first) == IB.fingerprint(second) != "" and bank["next"] == 1


def test_an_item_with_nothing_to_compare_is_never_merged_with_another():
    a = IB._new_item("IB-0001", "instagram", "instagram:1", {}, NOW, None)
    b = IB._new_item("IB-0002", "instagram", "instagram:2", {}, NOW, None)
    assert IB.fingerprint(a) == "" == IB.fingerprint(b)
    bank = {"next": 1, "items": {}}
    new = IB.intake_collected(bank, [record([row("instagram", "1"), row("instagram", "2")])], S, NOW)
    assert len(new) == 2


# ---- keeping the original ----------------------------------------------------------------------------------------------------------------

def test_the_original_is_the_longest_running_ad_then_the_oldest_then_the_first_added():
    mk = lambda i, days, posted: {"id": i, "metrics": {"days_running": days} if days else {}, "posted_at": posted}   # noqa: E731
    assert IB.pick_original([mk("IB-0002", 10, "2026-01-01"), mk("IB-0001", 90, "2026-09-01")])["id"] == "IB-0001"
    assert IB.pick_original([mk("IB-0002", 0, "2026-09-01"), mk("IB-0001", 0, "2026-01-01")])["id"] == "IB-0001"
    assert IB.pick_original([mk("IB-0003", 0, ""), mk("IB-0002", 0, "")])["id"] == "IB-0002"
    assert IB.pick_original([mk("IB-0002", 0, ""), mk("IB-0001", 0, "2026-01-01")])["id"] == "IB-0001"


def test_three_copies_of_one_ad_become_one_item_and_it_is_the_longest_running(tmp_path):
    bank = {"next": 1, "items": {}}
    skipped = {}
    new = IB.intake_collected(bank, [record([ad("111", days_running=5), ad("222", days_running=60), ad("333", days_running=20)])], S, NOW, skipped=skipped)
    assert len(new) == 1 and bank["items"][new[0]]["source_item_id"] == "meta_ads:222" and skipped["duplicate"] == 2


def test_a_copy_of_something_already_in_the_bank_is_never_added(tmp_path):
    bank = {"next": 1, "items": {}}
    IB.intake_collected(bank, [record([ad("111")])], S, NOW)
    skipped = {}
    assert IB.intake_collected(bank, [record([ad("999")])], S, NOW, skipped=skipped) == [] and skipped["duplicate"] == 1 and len(bank["items"]) == 1


def test_a_copy_of_a_removed_item_does_not_come_back():
    bank = {"next": 1, "items": {}}
    (first,) = IB.intake_collected(bank, [record([ad("111")])], S, NOW)
    IB._remove(bank["items"][first], "not relevant enough")
    assert IB.intake_collected(bank, [record([ad("999")])], S, NOW) == []


def test_different_ads_are_all_kept():
    bank = {"next": 1, "items": {}}
    new = IB.intake_collected(bank, [record([ad("1", text="First idea about comfort that goes on for a while"), ad("2", text="Second idea about gifts that goes on for a while")])], S, NOW)
    assert len(new) == 2


def test_the_cap_applies_to_ideas_not_to_copies():
    bank = {"next": 1, "items": {}}
    recs = [record([ad(f"a{n}", text=f"Idea number {n} " + "words " * 6) for n in range(3)] + [ad(f"b{n}") for n in range(10)])]
    new = IB.intake_collected(bank, recs, replace(S, max_new_per_run=3), NOW)
    assert len(new) == 3 and len({bank["items"][i]["fingerprint"] for i in new}) == 3


def test_copies_already_in_the_bank_are_tidied_keeping_the_original():
    bank = kept_bank("A", "B", "C")
    for item in bank["items"].values():
        item.update({"kind": "ad", "text": "The very same ad text goes here for everyone to see", "handle": "Acme", "fingerprint": ""})
    bank["items"]["IB-0002"]["metrics"] = {"days_running": 90}
    IB.normalise_bank(bank)
    assert IB.tidy_duplicates(bank) == 2
    states = {i: it["state"] for i, it in bank["items"].items()}
    assert states == {"IB-0001": "removed", "IB-0002": "kept", "IB-0003": "removed"}
    assert bank["items"]["IB-0001"]["duplicate_of"] == "IB-0002" and bank["items"]["IB-0001"]["removed_reason"] == "a copy of IB-0002"
    assert IB.tidy_duplicates(bank) == 0                                              # nothing more to do the second time


def test_unrelated_items_are_untouched_by_the_copy_tidy():
    bank = kept_bank("A", "B")
    IB.normalise_bank(bank)
    assert IB.tidy_duplicates(bank) == 0 and {it["state"] for it in bank["items"].values()} == {"kept"}


# ---- the brand's own content -------------------------------------------------------------------------------------------------------------

def item_of(**kw):
    base = {"handle": "", "page_id": "", "url": "", "title": "", "text": ""}
    base.update(kw)
    return base


def test_the_brands_own_spellings(swov, brands):
    assert IB.own_names(swov) == {"swoveralls"}
    assert IB.own_names(brands["defi_snacks"]) == {"defi snacks", "defisnacks"}


@pytest.mark.parametrize("item,own_pages,expected", [
    (item_of(handle="Swoveralls"), [], "it comes from the brand's own account"),
    (item_of(handle="@swoveralls_official"), [], "it comes from the brand's own account"),
    (item_of(handle="SWOVERALLS"), [], "it comes from the brand's own account"),
    (item_of(handle="Acme", page_id="427103324323897"), ["427103324323897"], "it comes from the brand's own Meta page"),
    (item_of(handle="Acme", page_id="999999999"), ["427103324323897"], ""),
    (item_of(handle="Acme", url="https://www.swoveralls.com/products/navy"), [], "it is a page on the brand's own site"),
    (item_of(handle="Acme", text="Love my Swoveralls so much"), [], "it names the brand itself"),
    (item_of(handle="Acme", title="#swoveralls haul"), [], "it names the brand itself"),
    (item_of(handle="Acme", text="Sweatpant overalls from a rival"), [], ""),
    (item_of(handle="The Oodie", text="Wearable blankets"), [], ""),
    (item_of(handle="Acme", text="swoverallsy is not it"), [], "")])
def test_what_counts_as_the_brands_own_content(swov, item, own_pages, expected):
    assert IB.own_reason(swov, own_pages, item) == expected


def test_a_short_brand_name_never_matches_by_accident(swov):
    from types import SimpleNamespace
    tiny = SimpleNamespace(name="Ox", slug="ox", domain="ox.co")
    assert IB.own_names(tiny) == set() and IB.own_reason(tiny, [], item_of(handle="Oxford", text="ox ox")) == ""


def test_own_content_is_left_out_of_the_bank_and_counted(swov):
    bank = {"next": 1, "items": {}}
    skipped = {}
    recs = [record([ad("1", handle="Swoveralls", text="Our own ad about our own overalls, for us"), ad("2", handle="Acme", text="Rival ad copy that is quite long and different"),
                    ad("3", handle="Acme", text="Rival says Swoveralls is overpriced and says it at length")])]
    new = IB.intake_collected(bank, recs, S, NOW, brand=swov, skipped=skipped)
    assert [bank["items"][i]["source_item_id"] for i in new] == ["meta_ads:2"] and skipped["own"] == 2


def test_own_content_can_be_allowed_back_by_a_setting(swov):
    bank = {"next": 1, "items": {}}
    new = IB.intake_collected(bank, [record([ad("1", handle="Swoveralls")])], replace(S, skip_own_brand=False), NOW, brand=swov)
    assert len(new) == 1


def test_the_brands_own_meta_page_is_known_from_the_benchmarks(store_in_tmp, swov):
    store = store_in_tmp
    store.write_benchmarks("swoveralls", {"pages": [{"name": "Swoveralls", "page_id": "427103324323897", "kind": "own"},
                                                    {"name": "The Oodie", "page_id": "601495866852901", "kind": "benchmark"}, {"name": "x", "kind": "own"}]})
    assert IB.own_page_ids(store, "swoveralls") == ["427103324323897"] and IB.own_page_ids(store, "duderobe") == []


@pytest.fixture
def store_in_tmp(tmp_path):
    return Store(tmp_path / "data")


def test_own_content_already_in_the_bank_is_removed(swov):
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"].update({"handle": "Swoveralls"})
    assert IB.tidy_own(bank, swov, [], S) == 1
    assert bank["items"]["IB-0001"]["state"] == "removed" and bank["items"]["IB-0001"]["removed_reason"] == "the brand's own content: it comes from the brand's own account"
    assert bank["items"]["IB-0002"]["state"] == "kept" and IB.tidy_own(bank, swov, [], S) == 0
    own_item = kept_bank("A")
    own_item["items"]["IB-0001"]["handle"] = "Swoveralls"
    assert IB.tidy_own(own_item, swov, [], replace(S, skip_own_brand=False)) == 0 and own_item["items"]["IB-0001"]["state"] == "kept"
    assert IB.tidy_own(own_item, swov, [], S) == 1


# ---- old banks, relevance and the minimum ---------------------------------------------------------------------------------------------------

def test_an_old_bank_without_state_is_brought_up_to_date():
    bank = kept_bank("A", "B")
    for item in bank["items"].values():
        for key in ("state", "page_id", "removed_reason", "duplicate_of", "removal_note", "fingerprint"):
            item.pop(key)
    bank["items"]["IB-0002"]["score"] = None
    IB.normalise_bank(bank)
    assert [it["state"] for it in bank["items"].values()] == ["kept", "new"] and all(it["fingerprint"] for it in bank["items"].values())
    assert bank["items"]["IB-0001"]["page_id"] == "" and IB.normalise_bank(bank) is bank


@pytest.mark.parametrize("score,state", [(100, "kept"), (50, "kept"), (49, "removed"), (0, "removed"), (None, "new")])
def test_the_minimum_decides_between_kept_and_removed(score, state):
    bank = bank_with("A")
    bank["items"]["IB-0001"].update({"score": score, "state": "new"})
    IB.apply_relevance(bank, S)
    assert bank["items"]["IB-0001"]["state"] == state


def test_a_removed_item_stays_removed_and_the_count_is_exact():
    bank = bank_with("A", "B", "C")
    for i, sc in zip(bank["items"], (90, 30, 40)):
        bank["items"][i].update({"score": sc})
    bank["items"]["IB-0002"]["state"] = "removed"
    assert IB.apply_relevance(bank, S) == 1 and bank["items"]["IB-0002"]["state"] == "removed" and bank["items"]["IB-0003"]["removed_reason"].startswith("not relevant enough (scored 40")


def test_the_minimum_is_a_setting_between_0_and_100(tmp_path):
    import yaml
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"ideabank": {"min_relevance": 75, "skip_own_brand": False}}))
    s = IB.load_settings(path)
    assert (s.min_relevance, s.skip_own_brand) == (75, False) and IB.load_settings("does-not-exist.yaml").min_relevance == 50 == IB.load_settings("research.yaml").min_relevance
    for bad, match in ((101, "between 0 and 100"), (-1, "at least 0"), (60.5, "whole number"), (True, "whole number"), ("60", "whole number")):
        path.write_text(yaml.safe_dump({"ideabank": {"min_relevance": bad}}))
        with pytest.raises(IB.IdeaBankError, match=match):
            IB.load_settings(path)
    path.write_text(yaml.safe_dump({"ideabank": {"skip_own_brand": "yes"}}))
    with pytest.raises(IB.IdeaBankError, match="must be true or false"):
        IB.load_settings(path)


def test_items_that_are_not_kept_are_never_used_in_a_brief():
    bank = kept_bank("A", "B")
    bank["items"]["IB-0002"]["state"] = "removed"
    got = BR.approved_items(bank, {"IB-0001": {"decision": "use"}, "IB-0002": {"decision": "use"}})
    assert [i["id"] for i in got] == ["IB-0001"]


# ---- the very same file ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


def test_two_items_with_the_very_same_file_are_one_idea(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    same = Media(content=jpeg_for("the-same-picture"))
    saved, lines = IB.save_assets(bank, ["IB-0001", "IB-0002"], "Swoveralls", client, getter(default=same), S)
    second = bank["items"]["IB-0002"]
    assert saved == 1 and second["state"] == "removed" and second["duplicate_of"] == "IB-0001" and second["removed_reason"] == "a copy of IB-0001 (the very same file)"
    assert not second["assets"] and second["drive_folder_id"] == "" and "IB-0002: removed, it is a copy of IB-0001 (the very same file)" in lines
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - post A/"]


def test_a_file_the_same_as_one_saved_in_an_earlier_run_is_a_copy_too(drive):
    client, fake = drive
    bank = kept_bank("A")
    same = Media(content=jpeg_for("the-same-picture"))
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=same), S)
    later = kept_bank("B")["items"]["IB-0001"]
    later["id"] = "IB-0002"
    bank["items"]["IB-0002"] = later
    IB.save_assets(bank, ["IB-0002"], "Swoveralls", client, getter(default=same), S)
    assert bank["items"]["IB-0002"]["state"] == "removed" and len(fake.tree()) == 3          # the brand folder, one item folder, one file


def test_different_files_are_both_kept_and_a_removed_items_file_is_not_a_reason_to_drop_another(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    saved, _ = IB.save_assets(bank, ["IB-0001", "IB-0002"], "Swoveralls", client, getter(), S)
    assert saved == 2 and {i["state"] for i in bank["items"].values()} == {"kept"}
    assert all(len(a["sha256"]) == 64 for it in bank["items"].values() for a in it["assets"])


def test_a_removed_or_waiting_item_never_gets_a_download_or_folder(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"]["state"] = "removed"
    bank["items"]["IB-0002"].update({"state": "new", "score": None})
    get = getter()
    assert IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, get, S) == (0, []) and get.calls == [] and fake.tree() == []
    assert not IB.needs_assets(bank["items"]["IB-0001"]) and not IB.needs_assets(bank["items"]["IB-0002"])


# ---- the Drive trash ----------------------------------------------------------------------------------------------------------------------

def foldered(drive, *ids):
    client, fake = drive
    bank = kept_bank(*ids)
    IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(), S)
    return client, fake, bank


def test_the_folders_of_removed_items_go_to_the_trash_and_the_rest_stay(drive):
    client, fake, bank = foldered(drive, "A", "B", "C")
    IB._remove(bank["items"]["IB-0002"], "a copy of IB-0001", "IB-0001")
    moved, lines = IB.trash_folders(bank, client)
    gone = bank["items"]["IB-0002"]
    assert moved == 1 and lines == [] and gone["drive_folder_id"] == "" and gone["drive_folder_url"] == "" and gone["assets"] == []
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - post A/", "  IB-0003 - post C/"]
    assert bank["items"]["IB-0001"]["drive_folder_id"] and bank["items"]["IB-0003"]["assets"]


def test_trashing_asks_for_a_recoverable_trash_not_a_delete(drive):
    client, fake, bank = foldered(drive, "A")
    IB._remove(bank["items"]["IB-0001"], "x")
    IB.trash_folders(bank, client)
    call = [c for c in fake.calls if c["method"] in ("PATCH", "DELETE")][0]
    assert call["method"] == "PATCH" and call["json"] == {"trashed": True} and call["params"]["supportsAllDrives"] == "true"


def test_a_folder_that_cannot_be_trashed_is_noted_named_and_tried_again_next_time(drive):
    client, fake, bank = foldered(drive, "A", "B")
    for item in bank["items"].values():
        IB._remove(item, "x")
    fake.trash_error = (403, {"error": {"message": "The user does not have sufficient permissions for this file."}})
    moved, lines = IB.trash_folders(bank, client)
    assert moved == 0 and len(lines) == 1
    assert "2 folder(s) of removed items could not be moved to the Drive trash (Google Drive answered 403: The user does not have sufficient permissions for this file.)" in lines[0]
    assert "Content manager role" in lines[0] and "IB-0001 - post A; IB-0002 - post B" in lines[0]
    assert all(it["drive_folder_id"] and it["removal_note"].startswith("the folder could not be moved to the trash") for it in bank["items"].values())
    fake.trash_error = None
    moved, lines = IB.trash_folders(bank, client)
    assert moved == 2 and lines == [] and all(it["drive_folder_id"] == "" and it["removal_note"] == "" for it in bank["items"].values())


def test_only_six_stuck_folders_are_named(drive):
    client, fake, bank = foldered(drive, *"ABCDEFGH")
    for item in bank["items"].values():
        IB._remove(item, "x")
    fake.trash_error = (403, {})
    _, lines = IB.trash_folders(bank, client)
    assert lines[0].count(" - post ") == 6 and lines[0].endswith(" ...")


def test_removed_items_without_a_folder_cost_no_drive_call(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB._remove(bank["items"]["IB-0001"], "x")
    assert IB.trash_folders(bank, client) == (0, []) and fake.calls == []


def test_the_drive_client_trashes_with_googles_own_words_on_failure():
    fake = FakeDrive()
    with pytest.raises(D.DriveError, match="Google Drive answered 404: File not found: nope"):
        DriveClient(fake, ROOT, EMAIL).trash("nope")


# ---- one whole run, as in the screenshot ------------------------------------------------------------------------------------------------------

def screenshot_data(store):
    store.append_collected("swoveralls", record([
        ad("1", text="Bib overalls that feel like a day off. Soft, roomy and made to move.", days_running=40),
        ad("2", text="Bib overalls that feel like a day off. Soft, roomy and made to move.", days_running=10),
        ad("3", text="Bib overalls that feel like a day off. Soft, roomy and made to move.", days_running=5),
        ad("4", text="Your nervous system called. It wants this. A calming weighted blanket hoodie.", title="Your nervous system called...It wants this", handle="Cloud Co"),
        ad("5", text="Your nervous system called. It wants this. A calming weighted blanket hoodie.", title="Your nervous system called...It wants this", handle="Cloud Co"),
        ad("6", text="20% off, code FALL20 on our best sellers this fall only", title="20% Off, Code FALL20", handle="Swoveralls"),
        ad("7", text="Protein shaker bottles in nine colours with a leak proof lid", title="Shaker", handle="FitBottle"),
    ]))


def test_the_screenshot_case_one_original_no_own_content_no_irrelevant(store_in_tmp, swov):
    store = store_in_tmp
    screenshot_data(store)
    client = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    drive = (client, client.session)
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 82, topic="day off bibs"), good("IB-0002", 71, topic="calm hoodie"), good("IB-0003", 12, topic="shaker bottles")]}]
    new, lines = full_run(store, swov, drive, claude)
    bank = store.read_ideabank("swoveralls")
    text = "\n".join(lines)
    assert len(new) == 3 and "left out 1 of the brand's own and 3 cop(ies)" in text
    assert {i["state"] for i in bank["items"].values()} == {"kept", "removed"}
    kept = [i for i in bank["items"].values() if i["state"] == "kept"]
    assert sorted(i["title"] for i in kept) == ["Bib Overalls That Feel Like a Day Off", "Your nervous system called...It wants this"]
    assert [i["source_item_id"] for i in kept if i["title"].startswith("Bib")] == ["meta_ads:1"]           # the longest-running copy
    assert [n for n in client.session.tree() if n.startswith("  IB-")] == ["  IB-0001 - day off bibs/", "  IB-0002 - calm hoodie/"]
    assert bank["items"]["IB-0003"]["removed_reason"] == "not relevant enough (scored 12, the minimum is 50)"
    assert "swoveralls: 1 item(s) removed (0 the brand's own, 0 from brands you skip, 0 cop(ies), 1 not relevant enough) in this run; the bank now holds 2 kept item(s), 2 of them new in this run" in lines
    assert len(IB.sheet_rows(bank)) == 2


def test_running_it_again_adds_nothing_and_changes_nothing(store_in_tmp, swov):
    store = store_in_tmp
    screenshot_data(store)
    client = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 82, topic="a"), good("IB-0002", 71, topic="b"), good("IB-0003", 12, topic="c")]}]
    full_run(store, swov, (client, client.session), claude)
    before, calls = store.read_ideabank("swoveralls"), len(client.session.calls)
    new, lines = full_run(store, swov, (client, client.session))
    assert new == [] and store.read_ideabank("swoveralls") == before and len(client.session.calls) == calls


# ---- the sheet ----------------------------------------------------------------------------------------------------------------------------------

def row_ids(sp):
    return [r["idea_id"] for r in cells(sp, "Idea Bank")[2]]


def remove(store, ids, reason="a copy of IB-0001"):
    bank = store.read_ideabank("swoveralls")
    for i in ids:
        IB._remove(bank["items"][i], reason)
    store.write_ideabank("swoveralls", bank)


def test_the_rows_of_removed_items_are_deleted_from_the_sheet(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B", "C"))
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    assert row_ids(sp) == ["IB-0001", "IB-0002", "IB-0003"]
    remove(store, ["IB-0002"])
    report, error = one(swov, store, client)
    assert error is None and row_ids(sp) == ["IB-0001", "IB-0003"]
    assert "Idea Bank: removed 1 row(s) of items that were copies, the brand's own content or not relevant enough" in report.lines()[-1]


def test_a_row_a_person_wrote_on_is_never_deleted(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B", "C"))
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    ws, header, _ = cells(sp, "Idea Bank")
    ws._put(2, header.index("Decision") + 1, "Use")
    ws._put(3, header.index("Notes") + 1, "check this later")
    remove(store, ["IB-0001", "IB-0002", "IB-0003"])
    report, _ = one(swov, store, client)
    assert row_ids(sp) == ["IB-0001", "IB-0002"]
    assert report.notes[-1].endswith("removed 1 row(s) of items that were copies, the brand's own content or not relevant enough; kept 2 row(s) you had written a Decision or Notes on") or \
        "kept 2 row(s) you had written a Decision or Notes on" in report.notes[-1]


def test_rows_are_deleted_from_the_bottom_so_none_is_missed(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, tuple("ABCDEF"))
    client = FakeClient()
    one(swov, store, client)
    remove(store, ["IB-0001", "IB-0002", "IB-0004", "IB-0005"])
    one(swov, store, client)
    assert row_ids(client.created[0]) == ["IB-0003", "IB-0006"]


def test_other_rows_and_their_manual_columns_do_not_move_or_change(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B", "C"))
    client = FakeClient()
    one(swov, store, client)
    (sp,) = client.created
    ws, header, rows = cells(sp, "Idea Bank")
    ws._put(4, header.index("Decision") + 1, "Maybe")                       # IB-0003
    remove(store, ["IB-0001"])
    one(swov, store, client)
    _, _, rows = cells(sp, "Idea Bank")
    assert [(r["idea_id"], r["Decision"]) for r in rows] == [("IB-0002", ""), ("IB-0003", "Maybe")]


def test_unscored_items_have_no_row_until_they_are_scored(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B"), scored=False)
    client = FakeClient()
    one(swov, store, client)
    assert "Idea Bank" not in [w.title for w in client.created[0].worksheets()]
    bank = store.read_ideabank("swoveralls")
    bank["items"]["IB-0001"].update({"state": "kept", "score": 75, "summary": "s"})
    store.write_ideabank("swoveralls", bank)
    one(swov, store, client)
    assert row_ids(client.created[0]) == ["IB-0001"]


def test_when_everything_is_removed_an_existing_tab_is_emptied_but_not_created_from_nothing(store_in_tmp, swov):
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B"))
    client = FakeClient()
    one(swov, store, client)
    remove(store, ["IB-0001", "IB-0002"])
    one(swov, store, client)
    assert row_ids(client.created[0]) == []
    other = Store(store.data_dir.parent / "data2")
    baselined(swov, other, "swoveralls_full_72.json", 72)
    seed_bank(other, ("A",))
    bank = other.read_ideabank("swoveralls")
    IB._remove(bank["items"]["IB-0001"], "x")
    other.write_ideabank("swoveralls", bank)
    fresh = FakeClient()
    one(swov, other, fresh)
    assert "Idea Bank" not in [w.title for w in fresh.created[0].worksheets()]


def test_a_dry_run_deletes_no_rows(store_in_tmp, swov):
    from test_sheets import run
    store = store_in_tmp
    baselined(swov, store, "swoveralls_full_72.json", 72)
    seed_bank(store, ("A", "B"))
    client = FakeClient()
    one(swov, store, client)
    remove(store, ["IB-0001"])
    run([swov], store, client, dry_run=True)
    assert row_ids(client.created[0]) == ["IB-0001", "IB-0002"]


# ---- the tidy mode of the workflow ----------------------------------------------------------------------------------------------------------

def test_the_tidy_mode_collects_nothing_but_tidies_and_updates_the_sheets():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))
    steps = {s.get("name"): s for s in wf["jobs"]["collect"]["steps"]}
    assert "tidy" in wf[True]["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert steps["Collect"]["if"] == "inputs.mode != 'tidy'"
    for name in ("Remember the downloaded browser", "Install the browser"):
        assert "inputs.mode != 'tidy'" not in steps[name]["if"]            # tidy fetches videos through the browser now, so it is installed
    for name in ("Add new items to the Idea Bank", "Update Google Sheets"):
        assert steps[name]["if"] == "inputs.mode == 'collect' || inputs.mode == '' || inputs.mode == 'tidy'"
    assert steps["Add Deep Research reports to the Google Doc"]["if"] == "inputs.mode == 'collect' || inputs.mode == ''"


def test_the_readme_explains_the_clean_bank():
    text = open("README.md", encoding="utf-8").read()
    for needle in ("one original", "min_relevance", "own content", "Drive trash", "tidy"):
        assert needle in text, needle


# ---- web results, and the tidy-up of a bank that already exists ---------------------------------------------------------------------------

def web(**rows):
    return {k: {"title": v.get("title", "t"), "url": v["url"], "snippet": v.get("snippet", f"snippet {k}"), "domain": v.get("domain", "x.example")} for k, v in rows.items()}


def test_a_web_result_on_the_brands_own_site_is_left_out(swov):
    bank = {"next": 1, "items": {}}
    skipped = {}
    new = IB.intake_web(bank, web(a={"url": "https://www.swoveralls.com/blogs/news/fall"}, b={"url": "https://x.example/gift-guide"}), S, NOW, brand=swov, skipped=skipped)
    assert [bank["items"][i]["url"] for i in new] == ["https://x.example/gift-guide"] and skipped["own"] == 1
    assert IB.intake_web(bank, web(c={"url": "https://www.swoveralls.com/x"}), replace(S, skip_own_brand=False), NOW, brand=swov)


def test_the_same_web_page_found_twice_is_one_item(swov):
    bank = {"next": 1, "items": {}}
    skipped = {}
    new = IB.intake_web(bank, web(a={"url": "https://x.example/guide?utm=1"}, b={"url": "http://www.x.example/guide/"}), S, NOW, brand=swov, skipped=skipped)
    assert len(new) == 1 and skipped["duplicate"] == 1 and bank["next"] == 2                  # and no id was wasted on the copy
    again = IB.intake_web(bank, web(c={"url": "https://x.example/guide"}), S, NOW, brand=swov, skipped=skipped)
    assert again == [] and skipped["duplicate"] == 2


def test_a_removed_item_is_never_brought_back_whatever_it_scored():
    bank = bank_with("A")
    bank["items"]["IB-0001"].update({"score": 95, "state": "removed", "removed_reason": "a copy of IB-0009"})
    assert IB.apply_relevance(bank, S) == 0 and bank["items"]["IB-0001"]["state"] == "removed"


def test_a_removed_item_does_not_count_when_looking_for_copies():
    bank = kept_bank("A", "B")
    for item in bank["items"].values():
        item.update({"kind": "ad", "text": "The very same ad text goes here for everyone to see", "fingerprint": ""})
    bank["items"]["IB-0001"]["state"] = "removed"
    IB.normalise_bank(bank)
    assert IB.tidy_duplicates(bank) == 0 and bank["items"]["IB-0002"]["state"] == "kept"


def test_a_run_tidies_a_bank_that_already_has_the_brands_own_content(store_in_tmp, swov):
    store = store_in_tmp
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"]["handle"] = "Swoveralls"
    store.write_ideabank("swoveralls", bank)
    client = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    new, lines = full_run(store, swov, (client, client.session))
    after = store.read_ideabank("swoveralls")["items"]
    assert after["IB-0001"]["state"] == "removed" and after["IB-0002"]["state"] == "kept"
    assert "swoveralls: 1 item(s) removed (1 the brand's own, 0 from brands you skip, 0 cop(ies), 0 not relevant enough) in this run; the bank now holds 1 kept item(s), 0 of them new in this run" in lines


def test_a_run_removes_existing_copies_before_spending_a_claude_call_on_them(store_in_tmp, swov):
    store = store_in_tmp
    bank = bank_with("A", "B", "C")
    for item in bank["items"].values():
        item.update({"kind": "ad", "text": "The very same ad text goes here for everyone to see", "fingerprint": "", "state": "new"})
    store.write_ideabank("swoveralls", bank)
    client = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 80)]}]
    full_run(store, swov, (client, client.session), claude)
    prompt = claude.json_calls[0][1]
    assert prompt.count("IB-000") == 1 and "IB-0001" in prompt
    states = {i: it["state"] for i, it in store.read_ideabank("swoveralls")["items"].items()}
    assert states == {"IB-0001": "kept", "IB-0002": "removed", "IB-0003": "removed"}


def test_a_run_with_the_minimum_raised_empties_the_drive_of_what_no_longer_qualifies(store_in_tmp, swov):
    store = store_in_tmp
    seeded_items = bank_with("A", "B")
    store.write_ideabank("swoveralls", seeded_items)
    client = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    full_run(store, swov, (client, client.session), _scoring(("IB-0001", 90), ("IB-0002", 70)))
    assert len([n for n in client.session.tree() if n.startswith("  IB-")]) == 2
    new, lines = full_run(store, swov, (client, client.session), settings=replace(S, min_relevance=80))
    assert len([n for n in client.session.tree() if n.startswith("  IB-")]) == 1 and "1 folder(s) of removed items moved to the Drive trash" in " ".join(lines)


def _scoring(*pairs):
    c = FakeClaude()
    c.json_answers = [{"items": [good(i, rel, topic="t " + i[-1]) for i, rel in pairs]}]
    return c
