"""The Idea Bank: items sourced by the collectors, scored by Claude, with their files saved in the Shared Drive, waiting for a
person's Decision. Everything here is deterministic: a fake Claude, a fake Drive, a fake download."""
from dataclasses import replace

import pytest
import yaml

from contentintel import collectors as C
from contentintel import ideabank as IB
from contentintel.drive import DriveClient, DriveError
from contentintel.providers import ProviderError
from contentintel.store import Store

from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from test_collectors import NOW
from test_sheets import baselined

S = IB.IdeaBankSettings()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


def row(source, ident, **kw):
    return C._row(source, ident, **kw)


def record(items, at=NOW):
    return {"at": at, "run": "r", "source": items[0]["source"], "key": "k", "seeds": "s", "via": "browser", "from_site_data": len(items),
            "from_links": 0, "items": items}


def bank_with(*ids, **kw):
    bank = {"next": 1, "items": {}}
    IB.intake_collected(bank, [record([row("instagram", i, handle="u", title=f"post {i}", text="caption " + i, url=f"https://www.instagram.com/p/{i}/",
                                           media_url=f"https://cdn.example/{i}.jpg", likes=10) for i in ids])], S, NOW)
    return bank


def kept_bank(*ids):
    """A bank whose items were judged relevant (scored and kept): the only items that get a folder and a row."""
    bank = bank_with(*ids)
    for item in bank["items"].values():
        item.update({"state": "kept", "score": 80, "relevance": 80, "summary": "s", "how_to_use": "h", "risk": "none"})
    return bank


class Media:
    def __init__(self, status=200, mime="image/jpeg", content=b"\xff\xd8\xff" + bytes(200), length=None):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": mime}
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def iter_content(self, size):
        yield self.content


def getter(responses=None, default=None, boom=None):
    """requests.get stand-in: answers by url, records every call."""
    calls = []

    def get(url, timeout=None, stream=None, headers=None):
        calls.append(url)
        if boom:
            raise boom
        return (responses or {}).get(url) or default or Media(content=b"\xff\xd8\xff" + url.encode() + bytes(200))
    get.calls = calls
    return get


# ---- settings -----------------------------------------------------------------------------------------------------------------

def write(tmp_path, data):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_defaults_and_the_shipped_file_agree():
    assert IB.load_settings("does-not-exist.yaml") == IB.IdeaBankSettings() == IB.load_settings("research.yaml")


def test_every_setting_can_be_changed(tmp_path):
    s = IB.load_settings(write(tmp_path, {
        "ideabank": {"enabled": False, "sources": ["tiktok", "tiktok"], "max_new_per_run": 5, "score_batch": 3, "max_scoring_calls": 0, "save_assets": False,
                     "save_videos": False, "image_max_mb": 2, "video_max_mb": 9, "claude_monthly_calls": 11},
        "planner": {"max_keywords": 2, "max_hashtags": 3, "max_queries": 4, "max_questions": 1, "max_pages": 7},
        "brief": {"models": ["m1", "m2", "m1"], "max_concepts": 3, "max_calls": 4, "images_per_item": 0}}))
    assert (s.enabled, s.sources, s.max_new_per_run, s.score_batch, s.max_scoring_calls, s.save_assets, s.save_videos, s.image_max_mb, s.video_max_mb,
            s.claude_monthly_calls) == (False, ("tiktok",), 5, 3, 0, False, False, 2, 9, 11)
    assert (s.plan_max_keywords, s.plan_max_hashtags, s.plan_max_queries, s.plan_max_questions, s.plan_max_pages) == (2, 3, 4, 1, 7)
    assert (s.brief_models, s.brief_max_concepts, s.brief_max_calls, s.brief_images_per_item) == (("m1", "m2"), 3, 4, 0)


@pytest.mark.parametrize("section,key,value,match", [
    ("ideabank", "colour", 1, "unknown 'colour'"), ("planner", "colour", 1, "unknown 'colour'"), ("brief", "colour", 1, "unknown 'colour'"),
    ("ideabank", "enabled", "yes", "must be true or false"), ("ideabank", "save_videos", 1, "must be true or false"),
    ("ideabank", "score_batch", 0, "at least 1"), ("ideabank", "max_new_per_run", -3, "at least 1"), ("ideabank", "image_max_mb", 1.5, "whole number"),
    ("ideabank", "max_scoring_calls", True, "whole number"), ("ideabank", "claude_monthly_calls", "300", "whole number"),
    ("ideabank", "sources", ["facebook"], "must be a list of: meta_ads, instagram, tiktok"), ("ideabank", "sources", [], "must be a list of"),
    ("ideabank", "sources", "tiktok", "must be a list of"), ("planner", "max_queries", 0, "at least 1"),
    ("brief", "models", [], "list of model names"), ("brief", "models", [""], "list of model names"), ("brief", "models", "m", "list of model names"),
    ("brief", "max_concepts", 0, "at least 1"), ("brief", "images_per_item", -1, "at least 0")])
def test_a_bad_setting_is_refused_naming_it(tmp_path, section, key, value, match):
    with pytest.raises(IB.IdeaBankError, match=match):
        IB.load_settings(write(tmp_path, {section: {key: value}}))


def test_a_section_that_is_not_name_value_pairs_is_refused(tmp_path):
    with pytest.raises(IB.IdeaBankError, match="must be name: value pairs"):
        IB.load_settings(write(tmp_path, {"ideabank": [1, 2]}))


def test_the_other_sections_of_research_yaml_still_load_with_the_new_ones_present():
    from contentintel import research as R
    assert R.load_settings("research.yaml").country == "worldwide"
    assert C.load_collector_settings("research.yaml").claude_review_rounds == 1


# ---- the shared Claude budget ---------------------------------------------------------------------------------------------------

def test_calls_are_counted_against_the_month_and_remembered(store):
    b = IB.ClaudeBudget(store, NOW, 5)
    assert b.left == 5 and b.take() and b.take(2) and b.left == 2
    assert store.read_claude_usage() == {"2026-10": 3}
    assert IB.ClaudeBudget(store, NOW, 5).left == 2                       # a new run remembers


def test_a_call_over_the_cap_is_refused_and_not_counted(store):
    b = IB.ClaudeBudget(store, NOW, 2)
    assert b.take(2) and not b.take() and not b.take(3) and b.used == 2 and store.read_claude_usage() == {"2026-10": 2}


def test_a_new_month_starts_again(store):
    IB.ClaudeBudget(store, NOW, 1).take()
    assert IB.ClaudeBudget(store, "2026-11-01T00:00:00Z", 1).left == 1 and IB.ClaudeBudget(store, NOW, 1).left == 0


def test_a_cap_of_zero_allows_nothing(store):
    assert not IB.ClaudeBudget(store, NOW, 0).take()


def test_the_usage_file_is_never_created_by_reading_and_a_damaged_one_means_zero(store, tmp_path):
    IB.ClaudeBudget(store, NOW, 5)
    assert not (tmp_path / "data").exists()
    path = tmp_path / "data" / "_shared" / "claude_usage.json"
    path.parent.mkdir(parents=True)
    for content in ("{ broken", "[]", '{"2026-10": "many"}'):
        path.write_text(content)
        assert IB.ClaudeBudget(store, NOW, 5).left == 5


# ---- intake ------------------------------------------------------------------------------------------------------------------

def test_new_collected_items_get_sequential_ids_and_their_facts():
    bank = {"next": 1, "items": {}}
    rows = [row("instagram", "A1", handle="maker", title="Haul", text="x " * 400, url="https://www.instagram.com/p/A1/", likes=50, comments=3, views=900,
                media_url="https://cdn.example/a.jpg", posted_at="2026-10-01T00:00:00Z"),
            row("meta_ads", "555", handle="Acme", title="Ad", text="buy", url="https://www.facebook.com/ads/library/?id=555", days_running=90, variants=4)]
    new = IB.intake_collected(bank, [record(rows[:1]), record(rows[1:])], S, NOW, {"id": "RQ-0001", "region": "gb", "occasion": "Father's Day"})
    assert sorted(new) == ["IB-0001", "IB-0002"] and bank["next"] == 3
    insta = next(i for i in bank["items"].values() if i["platform"] == "instagram")
    assert (insta["kind"], insta["handle"], insta["title"], insta["url"], insta["posted_at"], insta["media_url"]) == (
        "social post", "maker", "Haul", "https://www.instagram.com/p/A1/", "2026-10-01T00:00:00Z", "https://cdn.example/a.jpg")
    assert insta["metrics"] == {"likes": 50, "comments": 3, "views": 900} | {k: v for k, v in insta["metrics"].items() if k == "engagement_pct"}
    assert len(insta["text"]) == IB.TEXT_LIMIT and (insta["request_id"], insta["region"], insta["occasion"]) == ("RQ-0001", "gb", "Father's Day")
    assert (insta["score"], insta["summary"], insta["drive_folder_id"], insta["assets"], insta["asset_tries"], insta["added_at"]) == (None, "", "", [], 0, NOW)
    ad = next(i for i in bank["items"].values() if i["platform"] == "meta_ads")
    assert ad["kind"] == "ad" and ad["metrics"]["days_running"] == 90 and ad["metrics"]["variants"] == 4 and ad["metrics"]["signal"].startswith("worth studying")


def test_an_item_is_never_added_twice():
    bank = bank_with("A", "B")
    again = IB.intake_collected(bank, [record([row("instagram", "A", title="post A"), row("instagram", "C", title="post C")])], S, NOW)
    assert len(again) == 1 and len(bank["items"]) == 3 and bank["items"]["IB-0003"]["source_item_id"] == "instagram:C"
    assert IB.intake_collected(bank, [record([row("instagram", "C")])], S, NOW) == []


def test_ids_keep_counting_even_if_the_bank_is_reloaded(store):
    bank = store.read_ideabank("swoveralls")
    IB.intake_collected(bank, [record([row("instagram", "A")])], S, NOW)
    store.write_ideabank("swoveralls", bank)
    again = store.read_ideabank("swoveralls")
    assert again["next"] == 2 and IB.intake_collected(again, [record([row("instagram", "B")])], S, NOW) == ["IB-0002"]


def test_only_the_chosen_sources_feed_the_bank():
    bank = {"next": 1, "items": {}}
    new = IB.intake_collected(bank, [record([row("instagram", "A")]), record([row("tiktok", "T")]), record([row("deep_research", "D")])],
                              replace(S, sources=("tiktok",)), NOW)
    assert [bank["items"][i]["platform"] for i in new] == ["tiktok"]


def test_only_records_since_a_time_are_taken():
    bank = {"next": 1, "items": {}}
    old, fresh = record([row("instagram", "OLD")], at="2026-10-01T00:00:00Z"), record([row("instagram", "NEW")], at="2026-10-20T08:00:00Z")
    new = IB.intake_collected(bank, [old, fresh], S, NOW, since="2026-10-20T00:00:00Z")
    assert [bank["items"][i]["source_item_id"] for i in new] == ["instagram:NEW"]


def test_a_run_takes_in_no_more_than_its_limit_most_recent_and_most_interesting_first():
    bank = {"next": 1, "items": {}}
    items = [row("instagram", f"p{n}", likes=n, text=f"t{n}") for n in range(1, 6)]
    recs = [record([items[0]], at="2026-10-01T00:00:00Z"), record([items[1]], at="2026-10-02T00:00:00Z"), record(items[2:], at="2026-10-05T00:00:00Z")]
    new = IB.intake_collected(bank, recs, replace(S, max_new_per_run=2), NOW)
    assert [bank["items"][i]["source_item_id"] for i in new] == ["instagram:p5", "instagram:p4"]      # newest first, then the more liked


def test_web_results_become_items_once_and_need_a_link():
    bank = {"next": 1, "items": {}}
    sources = {"s1": {"title": "Gift guide", "url": "https://x.example/guide", "snippet": "ideas", "published": "2026-09-30", "domain": "x.example"},
               "s2": {"title": "No link", "url": "", "snippet": "z"}, "s3": {"title": "Reel", "url": "https://www.tiktok.com/@a/video/1", "snippet": "y", "domain": "tiktok.com"}}
    new = IB.intake_web(bank, sources, S, NOW, {"id": "RQ-0002", "region": "worldwide"})
    assert [bank["items"][i]["source_item_id"] for i in new] == ["web:s1", "web:s3"]
    first = bank["items"][new[0]]
    assert (first["kind"], first["platform"], first["handle"], first["text"], first["posted_at"], first["request_id"]) == (
        "web result", "web", "x.example", "ideas", "2026-09-30", "RQ-0002")
    assert IB.intake_web(bank, sources, S, NOW) == []


def test_web_intake_respects_the_limit():
    bank = {"next": 1, "items": {}}
    sources = {f"s{n}": {"title": "t", "url": f"https://x.example/{n}", "snippet": "s"} for n in range(5)}
    assert len(IB.intake_web(bank, sources, replace(S, max_new_per_run=2), NOW)) == 2


# ---- fetching a file ------------------------------------------------------------------------------------------------------------

def test_a_picture_and_a_video_are_downloaded_with_their_kind():
    data, mime, kind = IB.fetch_media(getter(default=Media()), "https://cdn.example/a.jpg", S)
    assert (mime, kind) == ("image/jpeg", "image") and data.startswith(b"\xff\xd8")
    data, mime, kind = IB.fetch_media(getter(default=Media(mime="video/mp4", content=b"mp4data")), "https://cdn.example/v.mp4", S)
    assert (data, mime, kind) == (b"mp4data", "video/mp4", "video")


def test_the_content_type_parameters_and_case_are_ignored():
    assert IB.fetch_media(getter(default=Media(mime="IMAGE/PNG; charset=binary")), "https://c.example/a", S)[1] == "image/png"


@pytest.mark.parametrize("url,get,match", [
    ("http://cdn.example/a.jpg", getter(), "no https address"), ("", getter(), "no https address"),
    ("https://c.example/a", getter(default=Media(status=404)), "answered 404 .the link may have expired."),
    ("https://c.example/a", getter(default=Media(mime="text/html")), "not a picture or video .text/html."),
    ("https://c.example/a", getter(default=Media(mime="image/svg+xml")), "not a picture or video"),
    ("https://c.example/a", getter(default=Media(mime="")), "unknown type"),
    ("https://c.example/a", getter(default=Media(length=99 * 1024 * 1024)), "bigger than 15 MB"),
    ("https://c.example/a", getter(default=Media(content=b"")), "an empty file"),
    ("https://c.example/a", getter(boom=ConnectionError("down")), "could not be downloaded .ConnectionError.")])
def test_a_file_that_cannot_be_saved_says_why(url, get, match):
    with pytest.raises(IB.AssetSkip, match=match):
        IB.fetch_media(get, url, S)


def test_a_file_bigger_than_the_limit_in_the_body_alone_is_refused():
    with pytest.raises(IB.AssetSkip, match="bigger than 1 MB"):
        IB.fetch_media(getter(default=Media(content=b"x" * (1024 * 1024 + 1))), "https://c.example/a", replace(S, image_max_mb=1))


def test_videos_have_their_own_limit_and_can_be_switched_off():
    big = Media(mime="video/mp4", content=b"v" * (2 * 1024 * 1024))
    with pytest.raises(IB.AssetSkip, match="bigger than 1 MB"):
        IB.fetch_media(getter(default=big), "https://c.example/v", replace(S, video_max_mb=1))
    assert IB.fetch_media(getter(default=big), "https://c.example/v", replace(S, image_max_mb=1, video_max_mb=5))[2] == "video"
    with pytest.raises(IB.AssetSkip, match="saving videos is switched off"):
        IB.fetch_media(getter(default=big), "https://c.example/v", replace(S, save_videos=False))


# ---- saving files in Drive --------------------------------------------------------------------------------------------------------

def test_each_items_file_goes_in_its_own_folder_under_the_brands(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    bank["items"]["IB-0001"]["topic"] = "comfort hook"
    saved, lines = IB.save_assets(bank, ["IB-0001", "IB-0002"], "Swoveralls", client, getter(), S)
    assert saved == 2 and lines == []
    assert fake.tree() == ["Swoveralls/", "  IB-0001 - comfort hook/", "    IB-0001-image.jpg", "  IB-0002 - post B/", "    IB-0002-image.jpg"]
    one = bank["items"]["IB-0001"]
    assert one["drive_folder_url"].startswith("https://drive.google.com/drive/folders/") and one["drive_folder_id"] in fake.files
    assert one["assets"][0]["name"] == "IB-0001-image.jpg" and one["assets"][0]["kind"] == "image" and one["assets"][0]["bytes"] == 3 + len("https://cdn.example/A.jpg") + 200 and len(one["assets"][0]["sha256"]) == 64
    assert fake.files[one["assets"][0]["drive_id"]]["data"] == b"\xff\xd8\xff" + b"https://cdn.example/A.jpg" + bytes(200)


def test_the_brand_folder_is_made_once_for_many_items(drive):
    client, fake = drive
    IB.save_assets(kept_bank("A", "B", "C"), ["IB-0001", "IB-0002", "IB-0003"], "Swoveralls", client, getter(), S)
    assert len([f for f in fake.files.values() if f["name"] == "Swoveralls"]) == 1


def test_a_video_is_saved_as_a_video_with_its_type(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(mime="video/mp4", content=b"mp4")), S)
    asset = bank["items"]["IB-0001"]["assets"][0]
    assert asset["name"] == "IB-0001-video.mp4" and asset["kind"] == "video" and fake.files[asset["drive_id"]]["mime"] == "video/mp4"


def test_an_item_with_no_file_to_save_gets_no_folder_and_no_download(drive):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["media_url"] = ""
    get = getter()
    assert IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S) == (0, []) and fake.tree() == [] and get.calls == []


def test_a_file_that_cannot_be_downloaded_leaves_a_note_and_is_tried_again_up_to_three_times(drive):
    client, fake = drive
    bank = kept_bank("A")
    get = getter(default=Media(status=404))
    for attempt in range(1, 5):
        saved, lines = IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S)
        assert saved == 0
    item = bank["items"]["IB-0001"]
    assert item["asset_tries"] == IB.ASSET_TRIES == 3 and len(get.calls) == 3 and "answered 404" in item["asset_note"] and fake.tree() == []


def test_a_file_that_saves_on_a_later_try_clears_the_note(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(default=Media(status=500)), S)
    assert bank["items"]["IB-0001"]["asset_note"]
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    assert bank["items"]["IB-0001"]["asset_note"] == "" and len(bank["items"]["IB-0001"]["assets"]) == 1


def test_a_file_already_in_the_folder_is_not_uploaded_again(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    uploads = sum(1 for c in fake.calls if "upload/drive" in c["url"])
    item = bank["items"]["IB-0001"]
    item["assets"], item["asset_tries"] = [], 0                                    # as if the memory had been lost
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    assert sum(1 for c in fake.calls if "upload/drive" in c["url"]) == uploads and len(fake.children(item["drive_folder_id"])) == 1


def test_a_topic_comes_from_the_title_when_none_was_scored(drive):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"]["title"] = "A very long title " * 10
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    name = fake.children(fake.folder_named(ROOT, "Swoveralls") or "")[0] if False else [n for n in fake.tree() if n.startswith("  IB-0001")][0].strip("/ ")
    assert name.startswith("IB-0001 - A very long title") and len(bank["items"]["IB-0001"]["topic"]) <= 40


def test_a_drive_problem_stops_the_saving_notes_it_and_is_raised(drive):
    client, fake = drive
    bank = kept_bank("A", "B")
    fake.script = [(403, {"error": {"message": "no"}})]
    with pytest.raises(DriveError, match="is not shared with"):
        IB.save_assets(bank, ["IB-0001", "IB-0002"], "Swoveralls", client, getter(), S)
    assert "not saved in Drive" in bank["items"]["IB-0001"]["asset_note"] and bank["items"]["IB-0002"]["asset_tries"] == 0


def test_items_that_already_have_files_are_left_alone(drive):
    client, fake = drive
    bank = kept_bank("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    assert not IB.needs_assets(bank["items"]["IB-0001"])
    get = getter()
    assert IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, get, S) == (0, []) and get.calls == []


# ---- scoring -----------------------------------------------------------------------------------------------------------------------

def good(idea_id, rel=80, **kw):
    return {"id": idea_id, "relevance": rel, "topic": kw.get("topic", "comfort hook"), "summary": kw.get("summary", "A creator shows comfort."),
            "how_to_use": kw.get("how_to_use", "Show our sweatpants overalls the same way."), "risk": kw.get("risk", "none")}


@pytest.mark.parametrize("entry,kept", [
    (good("IB-0001"), True), (good("IB-0001", 0), True), (good("IB-0001", 100), True), (good("IB-0001", 79.6), True),
    (good("IB-0001", 101), False), (good("IB-0001", -1), False), (good("IB-0001", True), False), (good("IB-0001", "80"), False), (good("IB-0001", None), False),
    (good("IB-0009"), False), ({**good("IB-0001"), "summary": "  "}, False), ({**good("IB-0001"), "summary": None}, False), ("text", False), (None, False)])
def test_only_valid_scores_are_kept(entry, kept):
    assert bool(IB.clean_scores({"items": [entry]}, {"IB-0001"})) is kept


def test_a_score_is_rounded_and_every_text_is_tidied_and_cut():
    got = IB.clean_scores({"items": [{**good("IB-0001", 79.6), "summary": "  two   spaces\n", "topic": "t" * 500}]}, {"IB-0001"})["IB-0001"]
    assert got["relevance"] == 80 and got["summary"] == "two spaces" and len(got["topic"]) == 240


def test_a_bare_list_is_accepted_and_garbage_gives_nothing():
    assert "IB-0001" in IB.clean_scores([good("IB-0001")], {"IB-0001"})
    assert IB.clean_scores({"items": "x"}, {"IB-0001"}) == {} and IB.clean_scores(None, {"IB-0001"}) == {} and IB.clean_scores({}, set()) == {}


def run_scoring(store, swov, bank, claude, settings=S, budget_cap=100, request=None):
    budget = IB.ClaudeBudget(store, NOW, budget_cap)
    return IB.score_items(bank, list(bank["items"]), claude, swov, store, settings, budget, NOW, request), budget


def test_items_are_scored_in_batches_with_the_model_recorded(store, swov):
    bank = bank_with("A", "B", "C")
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 90), good("IB-0002", 70)]}, {"items": [good("IB-0003", 30, topic="weak idea")]}]
    (scored, lines), budget = run_scoring(store, swov, bank, claude, replace(S, score_batch=2))
    assert scored == 3 and lines == [] and len(claude.json_calls) == 2 and budget.used == 2
    item = bank["items"]["IB-0001"]
    assert (item["score"], item["relevance"], item["summary"], item["how_to_use"], item["risk"], item["scored_at"], item["model"], item["topic"]) == (
        90, 90, "A creator shows comfort.", "Show our sweatpants overalls the same way.", "none", NOW, "claude-sonnet-5-5", "comfort hook")


def test_the_most_interesting_items_are_scored_first_when_the_calls_run_out(store, swov):
    bank = {"next": 1, "items": {}}
    IB.intake_collected(bank, [record([row("instagram", "low", likes=1), row("instagram", "high", likes=900), row("instagram", "mid", likes=50)])], S, NOW)
    ids = {i["source_item_id"]: i["id"] for i in bank["items"].values()}
    claude = FakeClaude()
    claude.json_answers = [{"items": [good(ids["instagram:high"])]}]
    (scored, lines), _ = run_scoring(store, swov, bank, claude, replace(S, score_batch=1, max_scoring_calls=1))
    assert scored == 1 and bank["items"][ids["instagram:high"]]["score"] == 80
    assert bank["items"][ids["instagram:mid"]]["score"] is None and bank["items"][ids["instagram:low"]]["score"] is None
    assert ids["instagram:high"] in claude.json_calls[0][1] and ids["instagram:mid"] not in claude.json_calls[0][1]
    assert lines == ["2 item(s) left unscored: this run's limit of 1 scoring calls is reached"]


def test_the_monthly_limit_stops_scoring_with_a_message(store, swov):
    bank = bank_with("A", "B")
    claude = FakeClaude()
    (scored, lines), budget = run_scoring(store, swov, bank, claude, replace(S, score_batch=1), budget_cap=0)
    assert scored == 0 and claude.json_calls == [] and "monthly Claude limit (0 calls) is reached" in lines[0] and lines[0].startswith("2 item(s)")


def test_a_failing_call_is_a_note_and_the_next_batch_still_runs(store, swov):
    bank = bank_with("A", "B")
    claude = FakeClaude()
    claude.json_answers = [ProviderError("Claude: overloaded"), {"items": [good("IB-0002")]}]
    (scored, lines), _ = run_scoring(store, swov, bank, claude, replace(S, score_batch=1))
    assert scored == 1 and lines == ["1 item(s) not scored: Claude: overloaded"] and bank["items"]["IB-0001"]["score"] is None


def test_unusable_answers_leave_those_items_unscored_and_say_so(store, swov):
    bank = bank_with("A", "B")
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001"), {"id": "IB-0002", "relevance": 500, "summary": "x"}]}]
    (scored, lines), _ = run_scoring(store, swov, bank, claude)
    assert scored == 1 and lines == ["1 item(s) in a batch came back unusable and stay unscored"]


def test_already_scored_items_are_not_scored_again(store, swov):
    bank = bank_with("A", "B")
    bank["items"]["IB-0001"]["score"] = 55
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0002")]}]
    run_scoring(store, swov, bank, claude)
    assert "IB-0001" not in claude.json_calls[0][1] and "IB-0002" in claude.json_calls[0][1] and bank["items"]["IB-0001"]["score"] == 55


def test_the_request_and_the_brand_are_part_of_what_claude_is_asked(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    bank = bank_with("A")
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001")]}]
    run_scoring(store, swov, bank, claude, request={"id": "RQ-0001", "text": "Father's Day gifting", "occasion": "Father's Day"})
    system, user, _ = claude.json_calls[0]
    assert "Request: Father's Day gifting (occasion: Father's Day)" in user and "Brand: Swoveralls" in user and "Catalogue:" in user
    assert "IB-0001 [social post, instagram] by u:" in user and "Never invent numbers" in system and "creators" in system


def test_a_topic_from_scoring_is_kept_only_while_the_folder_does_not_exist_yet(store, swov):
    bank = bank_with("A", "B")
    bank["items"]["IB-0002"]["drive_folder_id"] = "FOLDER"
    bank["items"]["IB-0002"]["topic"] = "old name"
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", topic="new idea"), good("IB-0002", topic="renamed")]}]
    run_scoring(store, swov, bank, claude)
    assert bank["items"]["IB-0001"]["topic"] == "new idea" and bank["items"]["IB-0002"]["topic"] == "old name"


def test_the_brand_context_describes_the_catalogue_briefly(store, swov):
    baselined(swov, store, "swoveralls_full_72.json", 72)
    text = IB.brand_context(swov, store)
    assert text.startswith(f"Brand: {swov.name} ({swov.domain}).") and "Catalogue: 42 core $89-$159" in text and "6 kids $43.99-$49" in text
    assert "Example products: " in text and text.count(";") >= 6


def test_a_brand_with_no_catalogue_still_has_a_context(store, swov):
    assert IB.brand_context(swov, store) == f"Brand: {swov.name} ({swov.domain})."


# ---- the sheet --------------------------------------------------------------------------------------------------------------------

def test_the_sheet_rows_have_exactly_the_columns_the_tool_owns():
    bank = kept_bank("A")
    (r,) = IB.sheet_rows(bank).values()
    assert list(r) == IB.IDEABANK_COLUMNS and "Decision" not in r and "Notes" not in r and "decision" not in r


def test_only_kept_items_are_shown_not_unscored_or_removed_ones():
    bank = kept_bank("A", "B", "C")
    bank["items"]["IB-0002"]["state"] = "removed"
    bank["items"]["IB-0003"].update({"state": "new", "score": None})
    assert list(IB.sheet_rows(bank)) == ["IB-0001"]


def test_a_shown_item_has_its_score_summary_link_and_file_count(drive, store, swov):
    client, fake = drive
    bank = kept_bank("A")
    bank["items"]["IB-0001"].update({"score": 77})
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    (scored,) = IB.sheet_rows(bank).values()
    assert scored["score"] == 77 and scored["summary"] and scored["drive_folder"].startswith("https://drive.google.com/drive/folders/") and scored["assets"] == 1
    assert scored["likes"] == 10


# ---- decisions ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [("Use", "use"), ("  USE ", "use"), ("yes", "use"), ("Y", "use"), ("approved", "use"), ("✅", "use"), ("Maybe", "maybe"), ("?", "maybe"),
                                           ("Skip", "skip"), ("no", "skip"), ("REJECT", "skip"), ("", ""), (None, ""), ("later", ""), ("use it", ""), (5, "")])
def test_whatever_a_person_types_is_understood_or_ignored(text, expected):
    assert IB.normalise_decision(text) == expected


class Sheet:
    def __init__(self, values):
        self.values = values

    def get_all_values(self):
        return self.values


def test_decisions_are_read_by_id_whatever_the_column_order_and_case():
    sheet = Sheet([["Notes", "DECISION", "Idea_ID", "title"], ["love it", "Use", "IB-0002", "x"], ["", "maybe", "IB-0001", "y"], ["", "", "IB-0003", "z"],
                   ["junk", "Use", "not an id", "w"], ["", "Use", "", "v"]])
    assert IB.read_decisions(sheet) == {"IB-0002": {"decision": "use", "notes": "love it"}, "IB-0001": {"decision": "maybe", "notes": ""},
                                        "IB-0003": {"decision": "", "notes": ""}}


def test_short_rows_and_a_missing_notes_column_are_fine():
    assert IB.read_decisions(Sheet([["idea_id", "Decision"], ["IB-0001"], ["IB-0002", "use"]])) == {
        "IB-0001": {"decision": "", "notes": ""}, "IB-0002": {"decision": "use", "notes": ""}}
    assert IB.read_decisions(Sheet([])) == {}


@pytest.mark.parametrize("header", [["idea_id", "title"], ["Decision", "title"], ["a", "b"]])
def test_a_tab_without_the_columns_is_an_error_that_says_what_to_do(header):
    with pytest.raises(IB.IdeaBankError, match="no idea_id and Decision columns; run the Sheets update first"):
        IB.read_decisions(Sheet([header, ["x", "y"]]))


# ---- one run -----------------------------------------------------------------------------------------------------------------------

def seeded(store, ids=("A", "B")):
    store.append_collected("swoveralls", record([row("instagram", i, handle="u", title=f"post {i}", text=f"caption {i}", url=f"https://www.instagram.com/p/{i}/",
                                                     media_url=f"https://cdn.example/{i}.jpg", likes=10) for i in ids]))


def full_run(store, swov, drive, claude=None, **kw):
    client, fake = drive
    budget = IB.ClaudeBudget(store, NOW, 100) if claude else None
    return IB.run_ideabank(swov, store, settings=kw.pop("settings", S), now=NOW, claude=claude, drive=client, get=kw.pop("get", getter()), budget=budget, **kw)


def scored(*pairs):
    """A Claude answer scoring items: (id, relevance, topic)."""
    c = FakeClaude()
    c.json_answers = [{"items": [good(i, rel, topic=topic) for i, rel, topic in pairs]}]
    return c


def test_a_run_scores_first_then_saves_files_in_folders_named_by_the_scored_topic(store, swov, drive):
    seeded(store)
    new, lines = full_run(store, swov, drive, scored(("IB-0001", 90, "comfort hook"), ("IB-0002", 70, "gift guide")))
    client, fake = drive
    text = " ".join(lines)
    assert sorted(new) == ["IB-0001", "IB-0002"] and "2 item(s) scored" in text and "files saved for 2 item(s)" in text
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - comfort hook/", "  IB-0002 - gift guide/"]
    bank = store.read_ideabank("swoveralls")
    assert all(i["state"] == "kept" and i["score"] and i["assets"] and i["drive_folder_url"] for i in bank["items"].values())


def test_an_item_below_the_minimum_is_removed_and_never_gets_a_folder(store, swov, drive):
    seeded(store)
    new, lines = full_run(store, swov, drive, scored(("IB-0001", 90, "comfort hook"), ("IB-0002", 59, "off topic")))
    client, fake = drive
    bank = store.read_ideabank("swoveralls")
    low = bank["items"]["IB-0002"]
    assert low["state"] == "removed" and low["removed_reason"] == "not relevant enough (scored 59, the minimum is 60)" and not low["assets"] and low["drive_folder_id"] == ""
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - comfort hook/"]
    assert "swoveralls: 1 item(s) removed (0 the brand's own, 0 cop(ies), 1 not relevant enough); 1 kept" in lines


def test_an_item_that_scores_exactly_the_minimum_is_kept(store, swov, drive):
    seeded(store, ("A",))
    full_run(store, swov, drive, scored(("IB-0001", 60, "just enough")))
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "kept"


def test_raising_the_minimum_later_tidies_the_bank_and_the_drive(store, swov, drive):
    seeded(store)
    full_run(store, swov, drive, scored(("IB-0001", 90, "strong"), ("IB-0002", 65, "middling")))
    client, fake = drive
    assert len([n for n in fake.tree() if n.startswith("  IB-")]) == 2
    new, lines = full_run(store, swov, drive, settings=replace(S, min_relevance=80))
    bank = store.read_ideabank("swoveralls")
    assert bank["items"]["IB-0002"]["state"] == "removed" and bank["items"]["IB-0001"]["state"] == "kept"
    assert [n for n in fake.tree() if n.startswith("  IB-")] == ["  IB-0001 - strong/"]
    assert "1 folder(s) of removed items moved to the Drive trash" in " ".join(lines)


def test_without_claude_items_wait_unscored_with_no_folder_and_no_row(store, swov, drive):
    seeded(store)
    client, fake = drive
    new, lines = full_run(store, swov, drive)
    text = " ".join(lines)
    assert len(new) == 2 and "2 item(s) wait to be scored before they are shown or get a folder (no Claude key)" in text and "files saved for 0 item(s)" in text
    bank = store.read_ideabank("swoveralls")
    assert all(i["state"] == "new" and i["score"] is None and not i["assets"] for i in bank["items"].values()) and IB.sheet_rows(bank) == {}
    assert fake.tree() == []


def test_without_drive_the_items_are_added_and_scored_and_the_files_are_skipped_with_the_reason(store, swov):
    seeded(store)
    claude = scored(("IB-0001", 80, "a"), ("IB-0002", 80, "b"))
    new, lines = IB.run_ideabank(swov, store, settings=S, now=NOW, claude=claude, drive=None, get=getter(), budget=IB.ClaudeBudget(store, NOW, 10))
    assert len(new) == 2 and "files not saved (no Drive folder is set up: add the IDEA_BANK_FOLDER_ID secret)" in " ".join(lines)
    assert all(i["score"] == 80 and i["state"] == "kept" and not i["assets"] for i in store.read_ideabank("swoveralls")["items"].values())


def test_files_can_be_switched_off(store, swov, drive):
    seeded(store)
    get = getter()
    full_run(store, swov, drive, scored(("IB-0001", 80, "a"), ("IB-0002", 80, "b")), settings=replace(S, save_assets=False), get=get)
    assert get.calls == []


def test_a_dry_run_counts_and_changes_nothing(store, swov, drive, tmp_path):
    seeded(store)
    new, lines = full_run(store, swov, drive, dry_run=True)
    assert len(new) == 2 and lines == ["swoveralls: 2 new item(s) taken in"] and not (tmp_path / "data" / "swoveralls" / "ideabank").exists()


def test_a_second_run_adds_nothing_and_does_not_touch_drive_again(store, swov, drive):
    seeded(store)
    client, fake = drive
    full_run(store, swov, drive, scored(("IB-0001", 80, "a"), ("IB-0002", 80, "b")))
    calls = len(fake.calls)
    new, lines = full_run(store, swov, drive)
    assert new == [] and len(fake.calls) == calls and lines[0] == "swoveralls: 0 new item(s) taken in"


def test_files_that_failed_before_are_tried_again_on_the_next_run(store, swov, drive):
    seeded(store, ("A",))
    full_run(store, swov, drive, scored(("IB-0001", 80, "a")), get=getter(default=Media(status=500)))
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["assets"] == []
    new, lines = full_run(store, swov, drive)
    assert new == [] and len(store.read_ideabank("swoveralls")["items"]["IB-0001"]["assets"]) == 1


def test_a_drive_problem_stops_the_saving_with_a_message_but_keeps_the_items_and_scores(store, swov, drive):
    seeded(store)
    client, fake = drive
    fake.script = [(403, {"error": {"message": "no"}})]
    new, lines = full_run(store, swov, drive, scored(("IB-0001", 80, "a"), ("IB-0002", 80, "b")))
    assert any("Drive stopped the saving of files" in line and "is not shared with" in line for line in lines)
    bank = store.read_ideabank("swoveralls")
    assert len(bank["items"]) == 2 and all(i["score"] == 80 and i["state"] == "kept" for i in bank["items"].values())


def test_a_request_tags_its_items_and_only_takes_what_its_run_collected(store, swov, drive):
    store.append_collected("swoveralls", record([row("instagram", "OLD", text="old words")], at="2026-10-01T00:00:00Z"))
    store.append_collected("swoveralls", record([row("instagram", "NEW", text="new words")], at="2026-10-20T09:00:00Z"))
    new, _ = full_run(store, swov, drive, request={"id": "RQ-0003", "region": "gb", "occasion": "", "text": "x"}, since="2026-10-20T08:00:00Z")
    (item,) = store.read_ideabank("swoveralls")["items"].values()
    assert item["source_item_id"] == "instagram:NEW" and item["request_id"] == "RQ-0003" and item["region"] == "gb"


def test_web_results_join_the_same_bank(store, swov, drive):
    sources = {"s1": {"title": "Gift guide", "url": "https://x.example/g", "snippet": "ideas", "domain": "x.example"}}
    new, lines = full_run(store, swov, drive, web_sources=sources, request={"id": "RQ-0004", "region": "worldwide", "occasion": "", "text": "x"})
    assert len(new) == 1 and store.read_ideabank("swoveralls")["items"]["IB-0001"]["platform"] == "web"


def test_the_bank_file_is_never_created_by_reading(store, tmp_path):
    assert store.read_ideabank("swoveralls") == {"next": 1, "items": {}} and not (tmp_path / "data").exists()


@pytest.mark.parametrize("content", ["{ broken", "[]", "null", '{"next": "x", "items": []}'])
def test_a_damaged_bank_file_reads_as_empty(store, tmp_path, content):
    path = tmp_path / "data" / "swoveralls" / "ideabank" / "items.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert store.read_ideabank("swoveralls") == {"next": 1, "items": {}}


def test_the_next_id_never_goes_backwards_even_if_the_counter_was_lost(store):
    store.write_ideabank("swoveralls", {"items": {"IB-0007": {}, "IB-0008": {}}})
    assert store.read_ideabank("swoveralls")["next"] == 3 and len(store.read_ideabank("swoveralls")["items"]) == 2


def test_a_counter_lower_than_the_items_already_there_never_reuses_an_id(store):
    store.write_ideabank("swoveralls", {"next": 1, "items": {"IB-0001": {}, "IB-0002": {}}})
    assert store.read_ideabank("swoveralls")["next"] == 3
    store.write_ideabank("swoveralls", {"next": 9, "items": {"IB-0001": {}}})
    assert store.read_ideabank("swoveralls")["next"] == 9
