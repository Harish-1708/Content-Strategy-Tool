"""A bank that copes with how the data really behaves: Meta's picture links expire within hours, a run takes in more items than the old scoring
limit could score, and the minimum score is a dial that can be turned either way.

The tidy of 2026-10-27 showed all three: eight items could not be saved because Meta answered 403 for a link collected days before, 28 items
were left unscored, and 92 items were removed with no way to see how close they had come."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from contentintel import ideabank as IB
from contentintel.drive import DriveClient
from contentintel.store import Store

from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from test_collectors import NOW
from test_ideabank import S, bank_with, full_run, getter, good, kept_bank, record, row


def ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def url_ending(text, name="https://scontent.fbcdn.net/v/a.jpg?stp=x&oh=00_abc&oe={}&_nc=1"):
    return name.format(format(int(ts(text).timestamp()), "X"))


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


# ---- reading when a link stops working -----------------------------------------------------------------------------------------------

def test_the_end_of_a_meta_link_is_read_from_its_oe_part():
    assert IB.link_expiry(url_ending("2026-10-25T08:00:00Z")) == ts("2026-10-25T08:00:00Z")
    assert IB.link_expiry("https://x.example/a.jpg?oe=" + format(int(ts("2027-01-01T00:00:00Z").timestamp()), "x")) == ts("2027-01-01T00:00:00Z")      # lower case too


@pytest.mark.parametrize("url", ["", None, "https://cdn.example/a.jpg", "https://x/a.jpg?oe=ZZZZZZZZ", "https://x/a.jpg?oe=123", "https://x/a.jpg?foe=6902A3F1",
                                 "https://x/a.jpg?noe=6902A3F1&x=1"])
def test_a_link_with_no_readable_end_is_not_treated_as_expiring(url):
    assert IB.link_expiry(url) is None


def test_time_text_is_read_strictly():
    assert IB._parse_time("2026-10-20T07:00:00Z") == ts("2026-10-20T07:00:00Z") and IB._parse_time("yesterday") is None and IB._parse_time(None) is None


# ---- not trying what cannot work ---------------------------------------------------------------------------------------------------------

def items_with(*urls):
    bank = kept_bank(*[f"X{n}" for n in range(len(urls))])
    for item, url in zip(bank["items"].values(), urls):
        item["media_url"] = url
    return bank


def test_an_expired_link_is_not_downloaded_not_counted_as_a_try_and_explained(drive):
    client, fake = drive
    bank = items_with(url_ending("2026-10-19T00:00:00Z"))
    get = getter()
    saved, lines = IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S, now=NOW)
    item = bank["items"]["IB-0001"]
    assert saved == 0 and get.calls == [] and item["asset_tries"] == 0 and fake.tree() == []
    assert item["asset_note"] == "the link expired on 2026-10-19; the file is saved if the ad is collected again"
    assert lines == ["1 item(s): the picture or video link has expired (Meta's links last only hours or days), so no file was saved; it is saved if the ad is collected again"]


def test_a_link_that_has_not_expired_yet_is_downloaded(drive):
    client, fake = drive
    bank = items_with(url_ending("2026-10-21T00:00:00Z"))
    get = getter()
    saved, lines = IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S, now=NOW)
    assert saved == 1 and len(get.calls) == 1 and lines == []


def test_a_link_that_ends_at_this_very_moment_counts_as_expired(drive):
    client, fake = drive
    bank = items_with(url_ending(NOW))
    get = getter()
    assert IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S, now=NOW)[0] == 0 and get.calls == []


def test_many_expired_links_are_one_line_and_the_good_ones_are_still_saved(drive):
    client, fake = drive
    bank = items_with(*[url_ending("2026-10-01T00:00:00Z", f"https://s.example/{n}.jpg?oe={{}}") for n in range(5)], "https://cdn.example/fine.jpg")
    saved, lines = IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(), S, now=NOW)
    assert saved == 1 and len(lines) == 1 and lines[0].startswith("5 item(s): the picture or video link has expired")


def test_the_real_clock_is_used_when_no_time_is_given(drive):
    client, fake = drive
    bank = items_with(url_ending("2020-01-01T00:00:00Z"))
    assert IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)[0] == 0


def test_a_run_judges_the_link_against_its_own_time_not_the_real_one(store, swov, drive):
    link = url_ending("2026-10-25T00:00:00Z")                  # after this run's time (2026-10-20), before today's real date
    store.write_ideabank("swoveralls", items_with(link))
    get = getter()
    new, lines = full_run(store, swov, drive, get=get)
    assert len(get.calls) == 1 and "files saved for 1 item(s)" in " ".join(lines)


# ---- a fresh link when the ad is collected again --------------------------------------------------------------------------------------------

def collected(store, *rows, at="2026-10-20T09:00:00Z"):
    store.append_collected("swoveralls", record(list(rows), at=at))


def waiting_bank(url="https://old.example/a.jpg"):
    bank = kept_bank("A")
    item = bank["items"]["IB-0001"]
    item.update({"media_url": url, "asset_tries": 3, "asset_note": "no file saved: the site answered 403", "source_item_id": "instagram:A"})
    return bank


def test_an_item_with_no_file_gets_the_fresh_link_and_a_fresh_start():
    bank = waiting_bank()
    skipped = {}
    new = IB.intake_collected(bank, [record([row("instagram", "A", media_url="https://new.example/a.jpg", text="caption A")])], S, NOW, skipped=skipped)
    item = bank["items"]["IB-0001"]
    assert new == [] and item["media_url"] == "https://new.example/a.jpg" and item["asset_tries"] == 0 and item["asset_note"] == "" and skipped["refreshed"] == 1
    assert IB.needs_assets(item)


@pytest.mark.parametrize("change", [{"assets": [{"name": "x"}]}, {"state": "removed"}])
def test_an_item_that_has_its_file_or_was_removed_keeps_its_link(change):
    bank = waiting_bank()
    bank["items"]["IB-0001"].update(change)
    skipped = {}
    IB.intake_collected(bank, [record([row("instagram", "A", media_url="https://new.example/a.jpg")])], S, NOW, skipped=skipped)
    assert bank["items"]["IB-0001"]["media_url"] == "https://old.example/a.jpg" and skipped["refreshed"] == 0


@pytest.mark.parametrize("new_url", ["https://old.example/a.jpg", "", None])
def test_the_same_or_a_missing_link_changes_nothing(new_url):
    bank = waiting_bank()
    skipped = {}
    IB.intake_collected(bank, [record([row("instagram", "A", media_url=new_url)])], S, NOW, skipped=skipped)
    assert bank["items"]["IB-0001"]["asset_tries"] == 3 and skipped["refreshed"] == 0


def test_the_whole_story_an_expired_link_then_a_new_collection_then_the_file(store, swov, drive):
    client, fake = drive
    stale = url_ending("2026-10-10T00:00:00Z")
    bank = waiting_bank(stale)
    bank["items"]["IB-0001"]["asset_tries"] = 0
    store.write_ideabank("swoveralls", bank)
    new, lines = full_run(store, swov, drive)
    assert fake.tree() == [] and any("has expired" in line for line in lines)
    collected(store, row("instagram", "A", media_url=url_ending("2026-10-25T00:00:00Z"), text="caption A"))
    new, lines = full_run(store, swov, drive)
    text = " ".join(lines)
    assert "1 picture or video link(s) were refreshed from this collection, for items with no file yet" in text and "files saved for 1 item(s)" in text
    assert len([n for n in fake.tree() if n.startswith("  IB-")]) == 1


# ---- seeing how close the removed items came ----------------------------------------------------------------------------------------------

def test_the_breakdown_counts_the_bands_below_the_minimum():
    line = IB.relevance_bands([59, 55, 50, 49, 45, 40, 39, 10], 60)
    assert line.startswith("8 item(s) were not relevant enough: 3 scored 50-59, 3 scored 40-49, 2 scored lower. ")
    assert "lower ideabank.min_relevance in research.yaml and run tidy again" in line and "come back when they reach the new minimum" in line


def test_the_breakdown_never_shows_negative_scores_for_a_low_minimum():
    assert IB.relevance_bands([3, 1], 15).startswith("2 item(s) were not relevant enough: 0 scored 5-14, 2 scored 0-4, 0 scored lower.")
    assert IB.relevance_bands([12, 7], 15).startswith("2 item(s) were not relevant enough: 2 scored 5-14, 0 scored 0-4, 0 scored lower.")


def test_a_run_shows_the_breakdown_only_for_what_it_removed_now(store, swov, drive):
    store.write_ideabank("swoveralls", bank_with("A", "B", "C", "D"))
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 90), good("IB-0002", 57), good("IB-0003", 44), good("IB-0004", 20)]}]       # the minimum is 50
    new, lines = full_run(store, swov, drive, claude)
    assert any(line.startswith("swoveralls: 2 item(s) were not relevant enough: 1 scored 40-49, 0 scored 30-39, 1 scored lower.") for line in lines)
    again, lines = full_run(store, swov, drive)
    assert not any("were not relevant enough" in line for line in lines)


# ---- the minimum works both ways ------------------------------------------------------------------------------------------------------------

def weak_bank():
    bank = bank_with("A", "B", "C")
    for item, (score, state, reason) in zip(bank["items"].values(), [(70, "kept", ""), (55, "removed", "not relevant enough (scored 55, the minimum is 60)"),
                                                                       (30, "removed", "not relevant enough (scored 30, the minimum is 60)")]):
        item.update({"score": score, "state": state, "removed_reason": reason, "removal_note": "stale"})
    return bank


def test_lowering_the_minimum_brings_back_what_now_qualifies_and_only_that():
    bank = weak_bank()
    assert IB.revive_relevant(bank, replace(S, min_relevance=50)) == 1
    states = {i: it["state"] for i, it in bank["items"].items()}
    assert states == {"IB-0001": "kept", "IB-0002": "kept", "IB-0003": "removed"}
    assert bank["items"]["IB-0002"]["removed_reason"] == "" and bank["items"]["IB-0002"]["removal_note"] == ""


def test_an_item_exactly_at_the_new_minimum_comes_back():
    assert IB.revive_relevant(weak_bank(), replace(S, min_relevance=55)) == 1


def test_a_copy_or_the_brands_own_content_never_comes_back_however_well_it_scored():
    bank = weak_bank()
    bank["items"]["IB-0002"].update({"score": 95, "removed_reason": "a copy of IB-0001"})
    bank["items"]["IB-0003"].update({"score": 95, "removed_reason": "the brand's own content: it comes from the brand's own account"})
    assert IB.revive_relevant(bank, replace(S, min_relevance=10)) == 0


def test_an_unscored_removed_item_is_not_revived():
    bank = weak_bank()
    bank["items"]["IB-0002"]["score"] = None
    bank["items"]["IB-0003"]["score"] = None
    assert IB.revive_relevant(bank, replace(S, min_relevance=0)) == 0 and bank["items"]["IB-0002"]["state"] == "removed"


def test_a_run_with_a_lower_minimum_brings_items_back_and_saves_their_files_again(store, swov, drive):
    client, fake = drive
    store.write_ideabank("swoveralls", bank_with("A", "B"))
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 90, topic="strong"), good("IB-0002", 42, topic="middling")]}]
    full_run(store, swov, drive, claude)
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - strong/"]
    new, lines = full_run(store, swov, drive, settings=replace(S, min_relevance=40))
    assert "1 item(s) removed earlier for a low score reach the minimum now and were brought back" in " ".join(lines)
    bank = store.read_ideabank("swoveralls")
    assert bank["items"]["IB-0002"]["state"] == "kept" and bank["items"]["IB-0002"]["assets"] and bank["items"]["IB-0002"]["drive_folder_id"]
    assert len([n for n in fake.tree() if n.startswith("  IB-")]) == 2


# ---- a run scores everything it takes in -------------------------------------------------------------------------------------------------------

def test_the_default_scoring_limit_covers_a_full_intake_and_matches_the_shipped_file():
    assert IB.IdeaBankSettings().max_scoring_calls == 16 == IB.load_settings("research.yaml").max_scoring_calls
    needed = 2 * -(-IB.IdeaBankSettings().max_new_per_run // IB.IdeaBankSettings().score_batch)          # ads and posts, and web results, each have the limit
    assert IB.IdeaBankSettings().max_scoring_calls >= needed


def test_sixty_new_items_are_all_scored_in_one_run(store, swov, drive):
    ids = [f"N{n:02d}" for n in range(60)]
    store.write_ideabank("swoveralls", bank_with(*ids))
    claude = FakeClaude()
    claude.json_answers = [{"items": [good(f"IB-{n:04d}", 80, topic=f"t{n}") for n in range(start, min(start + 8, 61))]} for start in range(1, 61, 8)]
    new, lines = full_run(store, swov, drive, claude)
    bank = store.read_ideabank("swoveralls")
    assert len(claude.json_calls) == 8 and all(it["score"] == 80 and it["state"] == "kept" for it in bank["items"].values())
    assert not any("left unscored" in line or "wait to be scored" in line for line in lines)


def test_the_readme_explains_the_limits_expiring_links_and_the_dial():
    text = open("README.md", encoding="utf-8").read()
    for needle in ("Google's limits", "60 sheet writes a minute", "one request instead of one request per", "Picture links that expire", "The minimum score works both ways"):
        assert needle in text, needle
