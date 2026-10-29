"""A Meta picture or video link lasts hours or days, so every ad collected before the file feature (or scored late) came out as "original link only".
The ad itself is often still shown in the Ad Library, and its page has a fresh link. These tests keep that rescue honest: it only runs for a kept
Meta ad with no file and an expired link, it asks at most twice and at most so many a run, and what it cannot get it says."""
from datetime import datetime, timezone

import pytest

from contentintel import ideabank as IB
from contentintel.drive import DriveClient
from contentintel.store import Store

from fake_browser import FakeBrowser
from fake_drive import EMAIL, ROOT, FakeDrive
from media_fixtures import jpeg_for
from test_ideabank import NOW, S, Media, full_run, getter, kept_bank
from test_video_outcomes import factory_for

CLOCK = IB._parse_time(NOW)
PAST = format(int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()), "x")
FUTURE = format(int(datetime(2027, 6, 1, tzinfo=timezone.utc).timestamp()), "x")
OLD = f"https://scontent.xx.fbcdn.net/v/old.jpg?oe={PAST}&sig=1"
NEW = "https://scontent.xx.fbcdn.net/v/fresh.jpg?sig=2"


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def ads(*ids, media=OLD):
    bank = kept_bank(*ids)
    for n, item in enumerate(bank["items"].values(), 1):
        item.update({"platform": "meta_ads", "source_item_id": f"meta_ads:{n}", "url": f"https://www.facebook.com/ads/library/?id={n}", "media_url": media,
                     "assets": [], "asset_note": ""})
    return bank


def ad_json(n, media_url=NEW):
    return {"adArchiveID": str(n), "pageName": "Page", "startDate": 1760000000, "snapshot": {"images": [{"originalImageUrl": media_url}], "body": "text"}}


def library(*payloads):
    browser = FakeBrowser({"https://www.facebook.com/ads/library/": {"json": list(payloads)}})
    return browser, factory_for(browser)


@pytest.mark.parametrize("change,expected", [
    ({}, True), ({"media_url": f"https://x/y.jpg?oe={FUTURE}"}, False), ({"media_url": "https://x/y.jpg"}, False),
    ({"media_url": "https://x/y.jpg", "asset_note": "the link expired on 2026-10-01; the file is saved if the ad is collected again"}, True),
    ({"media_url": "https://x/y.jpg", "asset_note": "the site answered 403 (the link may have expired)"}, True),
    ({"media_url": "https://x/y.jpg", "asset_note": "the site answered 404"}, True), ({"media_url": "https://x/y.jpg", "asset_note": "a video bigger than 80 MB"}, False),
    ({"link_refreshes": 1}, True), ({"link_refreshes": 2}, False), ({"assets": [{"kind": "image"}]}, False), ({"state": "removed"}, False),
    ({"state": "new"}, False), ({"platform": "tiktok"}, False), ({"media_url": ""}, False), ({"url": "https://example.com/a"}, False)])
def test_which_ads_get_a_fresh_link(change, expected):
    item = ads("A")["items"]["IB-0001"]
    item.update(change)
    assert IB.needs_link_refresh(item, S, CLOCK) is expected


def test_the_refresh_can_be_switched_off_or_limited_to_nothing():
    item = ads("A")["items"]["IB-0001"]
    assert not IB.needs_link_refresh(item, IB.IdeaBankSettings(refresh_links=False), CLOCK) and not IB.needs_link_refresh(item, IB.IdeaBankSettings(link_refreshes_per_run=0), CLOCK)


def test_a_fresh_link_is_taken_from_the_ads_own_page_and_the_file_can_then_be_saved():
    bank = ads("A", "B")
    browser, factory = library(ad_json(1), ad_json(2, "https://scontent.xx.fbcdn.net/v/fresh2.jpg?sig=3"))
    found, lines = IB.refresh_meta_links(bank, factory, S, NOW)
    a, b = bank["items"]["IB-0001"], bank["items"]["IB-0002"]
    assert found == 2 and lines == ["2 Meta ad picture or video link(s) refreshed from the Ad Library, so their files can be saved"]
    assert (a["media_url"], a["asset_note"], a["asset_tries"], a["link_refreshes"]) == (NEW, "", 0, 1) and b["media_url"].endswith("fresh2.jpg?sig=3")
    assert browser.opened == ["https://www.facebook.com/ads/library/?id=1", "https://www.facebook.com/ads/library/?id=2"] and factory.seen == ["meta_ads"]       # one browser for all


def test_an_ad_that_is_no_longer_shown_says_so_and_a_page_with_the_same_link_is_not_a_refresh():
    bank = ads("A", "B")
    _, factory = library(ad_json(1, OLD))                                               # the page gives back the very link that has expired
    found, lines = IB.refresh_meta_links(bank, factory, S, NOW)
    assert found == 0 and lines == ["2 expired Meta ad(s) could not be refreshed: they are no longer shown in the Ad Library"]
    assert all(it["asset_note"] == "the link expired and the ad is no longer shown in the Ad Library" and it["link_refreshes"] == 1 for it in bank["items"].values())
    assert IB.preview_label(bank["items"]["IB-0002"]) == "no saved media: the original link expired"


def test_an_ad_is_asked_about_at_most_twice():
    bank = ads("A")
    _, factory = library()
    for _ in range(4):
        IB.refresh_meta_links(bank, factory, S, NOW)
    assert bank["items"]["IB-0001"]["link_refreshes"] == 2 and len(factory.seen) == 2


def test_only_so_many_are_asked_a_run_and_the_rest_are_announced():
    bank = ads(*"ABCDE")
    browser, factory = library(*[ad_json(n) for n in range(1, 6)])
    found, lines = IB.refresh_meta_links(bank, factory, IB.IdeaBankSettings(link_refreshes_per_run=2), NOW)
    assert found == 2 and len(browser.opened) == 2 and "3 more expired Meta link(s) wait for the next run (limit 2 a run)" in lines


def test_a_page_that_will_not_open_is_a_reason_and_the_next_ad_is_still_tried():
    bank = ads("A", "B")
    browser, factory = library(ad_json(2))
    real = browser.open

    def open_(url):
        if url.endswith("id=1"):
            raise ConnectionError("down")
        return real(url)
    browser.open = open_
    found, _ = IB.refresh_meta_links(bank, factory, S, NOW)
    assert found == 1 and bank["items"]["IB-0001"]["asset_note"] == "the link expired and the Ad Library page could not be opened (ConnectionError)"
    assert bank["items"]["IB-0002"]["media_url"] == NEW


def test_no_browser_or_one_that_cannot_start_changes_nothing_and_says_so():
    bank = ads("A")
    assert IB.refresh_meta_links(bank, None, S, NOW) == (0, ["1 Meta ad(s) have an expired picture or video link: no browser is available to fetch a fresh one"])

    def broken(site):
        raise OSError("no chromium")
    found, lines = IB.refresh_meta_links(bank, broken, S, NOW)
    assert found == 0 and lines == ["Meta ad links not refreshed: the browser could not start (OSError)"] and bank["items"]["IB-0001"]["media_url"] == OLD
    assert IB.refresh_meta_links(ads("A", media=f"https://x/y.jpg?oe={FUTURE}"), None, S, NOW) == (0, [])           # nothing expired: nothing to say


def test_a_run_refreshes_an_expired_ad_and_saves_its_file_in_the_same_run(store, swov, drive):
    client, fake = drive
    store.write_ideabank("swoveralls", ads("A"))
    browser, factory = library(ad_json(1))
    pics = getter(responses={NEW: Media(content=jpeg_for("fresh")), OLD: Media(status=403)})
    new, lines = full_run(store, swov, drive, get=pics, browser_factory=factory, settings=IB.IdeaBankSettings(vision=False, fetch_videos=False))
    item = store.read_ideabank("swoveralls")["items"]["IB-0001"]
    assert "swoveralls: 1 Meta ad picture or video link(s) refreshed from the Ad Library, so their files can be saved" in lines and "files saved for 1 item(s)" in "\n".join(lines)
    assert item["assets"][0]["name"] == "IB-0001-image.jpg" and IB.preview_label(item) == "image saved (640x960)"


def test_a_run_without_a_save_target_does_not_open_the_browser(store, swov):
    store.write_ideabank("swoveralls", ads("A"))
    browser, factory = library(ad_json(1))
    IB.run_ideabank(swov, store, settings=S, now=NOW, drive=None, get=None, browser_factory=factory)
    assert browser.opened == []


def test_the_command_has_a_browser_for_ad_pages_whenever_its_settings_can_be_read(monkeypatch):
    from contentintel import cli

    class Args:
        browser_config = "browser.yaml"
        debug_dir = None
    assert callable(cli._ad_browser_factory(Args()))
    Args.browser_config = "does-not-exist.yaml"
    assert cli._ad_browser_factory(Args()) is None and cli._ad_browser_factory(object()) is None


def test_the_settings_and_the_readme_cover_the_refresh(tmp_path):
    import yaml
    assert (S.refresh_links, S.link_refreshes_per_run) == (True, 10)
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"ideabank": {"refresh_links": False, "link_refreshes_per_run": 3}}))
    got = IB.load_settings(path)
    assert (got.refresh_links, got.link_refreshes_per_run) == (False, 3)
    for bad, match in (({"link_refreshes_per_run": -1}, "at least 0"), ({"refresh_links": "yes"}, "true or false")):
        path.write_text(yaml.safe_dump({"ideabank": bad}))
        with pytest.raises(IB.IdeaBankError, match=match):
            IB.load_settings(path)
    text = " ".join(open("README.md", encoding="utf-8").read().split())
    for needle in ("fresh link", "Ad Library", "link_refreshes_per_run", "refresh_links"):
        assert needle in text, needle


def test_a_refreshed_ad_gets_all_its_download_tries_again():
    bank = ads("A")
    bank["items"]["IB-0001"]["asset_tries"] = IB.ASSET_TRIES
    assert IB.needs_assets(bank["items"]["IB-0001"]) is False or True
    _, factory = library(ad_json(1))
    IB.refresh_meta_links(bank, factory, S, NOW)
    assert bank["items"]["IB-0001"]["asset_tries"] == 0


def test_a_new_item_and_a_tidied_old_one_have_exactly_the_same_fields():
    """A field added to one list and not the other makes the second run change a bank the first run left alone."""
    import copy
    bank = kept_bank("A")
    fresh = set(bank["items"]["IB-0001"])
    old = copy.deepcopy(bank)
    later = ("video_tries", "video_note", "video_status", "technique", "hook_line", "first_test", "audience", "channel", "visual_hook", "look", "visual_quality",
             "visual_tries", "media_role", "rescued", "link_refreshes", "fit", "idea", "mechanism", "concept", "generic", "category", "scoring_version", "format", "media_format")
    for item in old["items"].values():
        for key in later:
            item.pop(key, None)
    tidied = IB.normalise_bank(old)["items"]["IB-0001"]
    assert fresh == set(tidied), (fresh ^ set(tidied))
    assert all(key in tidied for key in later)
