"""The footage inventory built from the brand's Drive folder, and the cuts brief that uses it (Update 73 v6)."""
from pathlib import Path
from types import SimpleNamespace

import yaml

from contentintel import drive as D
from contentintel import footage as FT
from contentintel import footage_cli
from contentintel.store import Store

NOW = "2026-11-27T08:00:00Z"
FILES = [
    {"id": "f1", "name": "IMG_0001_unboxing.mov", "path": "unboxing", "mime": "video/quicktime", "size": 52_000_000, "modified": "2026-11-01T10:00:00Z", "seconds": 14.5, "width": 1080, "height": 1920},
    {"id": "f2", "name": "sarah-zip-demo.mp4", "path": "creators/Sarah", "mime": "video/mp4", "size": 20_000_000, "modified": "2026-11-20T10:00:00Z", "seconds": 31.0, "width": 1920, "height": 1080},
    {"id": "f3", "name": "fresh-upload.mp4", "path": "", "mime": "video/mp4", "size": 9_000_000, "modified": "2026-11-26T10:00:00Z", "seconds": None, "width": None, "height": None},
    {"id": "f4", "name": "navy-front.jpg", "path": "stills", "mime": "image/jpeg", "size": 2_000_000, "modified": "2026-10-01T10:00:00Z", "seconds": None, "width": 1000, "height": 1000},
    {"id": "f5", "name": "shot-list.docx", "path": "", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 5000, "modified": "2026-10-02T10:00:00Z", "seconds": None, "width": None, "height": None},
]


def test_the_inventory_is_built_from_the_files_with_stable_numbers_shapes_tags_and_the_other_files_ignored():
    clips, ignored = FT.build_inventory(FILES)
    assert ignored == 1 and [c["name"] for c in clips] == ["fresh-upload.mp4", "sarah-zip-demo.mp4", "navy-front.jpg", "IMG_0001_unboxing.mov"]
    by = {c["name"]: c for c in clips}
    assert by["IMG_0001_unboxing.mov"]["shape"] == "vertical" and by["sarah-zip-demo.mp4"]["shape"] == "horizontal" and by["navy-front.jpg"]["shape"] == "square" and by["fresh-upload.mp4"]["shape"] == ""
    assert by["sarah-zip-demo.mp4"]["tags"] == ["creators", "sarah", "zip", "demo"] and by["IMG_0001_unboxing.mov"]["tags"] == ["unboxing"]
    assert by["sarah-zip-demo.mp4"]["id"] == FT.clip_id("f2") and FT.clip_id("f2") == FT.clip_id("f2") != FT.clip_id("f1") and by["sarah-zip-demo.mp4"]["id"].startswith("FT-") and by["fresh-upload.mp4"]["seconds"] is None
    assert by["sarah-zip-demo.mp4"]["size_mb"] == 20.0 and by["sarah-zip-demo.mp4"]["url"] == "https://drive.google.com/file/d/f2/view" and by["navy-front.jpg"]["kind"] == "image"
    assert FT.shape(0, 0) == "" and FT.shape(1080, 1080) == "square" and FT.shape(1000, 1100) == "square"


def test_the_writer_is_shown_only_video_clips_that_have_a_length_newest_first():
    clips, _ = FT.build_inventory(FILES)
    block = FT.prompt_block(clips)
    assert block.startswith("Footage you may cut from. Use ONLY these clips") and block.index("sarah-zip-demo") < block.index("IMG_0001_unboxing") and "fresh-upload" not in block and "navy-front" not in block
    assert f"- {FT.clip_id('f2')}: creators/Sarah/sarah-zip-demo.mp4 (31 s, horizontal; creators, sarah, zip, demo)" in block and FT.prompt_block([]) == ""


def brief_of(*clip_lists, fmt="15 second cut"):
    return {"concepts": [{"name": f"C{i}", "format": fmt, "clips": parts} for i, parts in enumerate(clip_lists, start=1)]}


def test_a_cuts_concept_must_cut_from_real_clips_inside_their_length_and_close_to_the_format_length():
    clips, _ = FT.build_inventory(FILES)
    a, b = FT.clip_id("f1"), FT.clip_id("f2")
    target = lambda fmt: 15                                                                                                               # noqa: E731
    ok = FT.check_clips(brief_of([{"clip": a, "start": 0, "end": 8}, {"clip": b, "start": 10, "end": 17}]), clips, target)
    assert ok == []
    assert [f["rule"] for f in FT.check_clips(brief_of([]), clips, target)] == ["cuts_no_clips"]
    assert [f["rule"] for f in FT.check_clips(brief_of([{"clip": "FT-nope00", "start": 0, "end": 3}]), clips, target)] == ["cuts_unknown_clip"]
    assert [f["rule"] for f in FT.check_clips(brief_of([{"clip": a, "start": 5, "end": 5}]), clips, target)] == ["cuts_bad_times"]
    past = FT.check_clips(brief_of([{"clip": a, "start": 10, "end": 20}]), clips, target)
    assert [f["rule"] for f in past] == ["cuts_past_the_end"] and "14.5 s long" in past[0]["message"] and past[0]["severity"] == "error"
    long = FT.check_clips(brief_of([{"clip": b, "start": 0, "end": 30}]), clips, target)
    assert [f["rule"] for f in long] == ["cuts_length"] and long[0]["severity"] == "warning"


class FakeDrive:
    def __init__(self, files):
        self.files, self.made = files, []

    def footage_folder(self, name):
        self.made.append(name)
        return "FOLDER1"

    def list_tree(self, folder):
        return self.files


def test_the_footage_command_scans_the_drive_folder_saves_the_inventory_and_says_where_the_clips_go(tmp_path, capsys):
    store = Store(tmp_path / "data")
    args = SimpleNamespace(action="scan", brand="swoveralls", brands_dir="brands")
    fake = FakeDrive(FILES)
    assert footage_cli.run_footage(args, store, NOW, drive=fake) == 0
    out = capsys.readouterr().out
    assert fake.made == ["Swoveralls"]                                                                                                    # the brand's own folder, by the brand's name
    assert "scanned Swoveralls/Footage: 4 clip(s) (3 video, 45.5 s in all), 1 video(s) with no length read by Drive yet, 1 other file(s) ignored" in out
    saved = FT.load(store, "swoveralls")
    assert saved["count"] == 4 and saved["videos"] == 3 and saved["total_seconds"] == 45.5 and saved["without_length"] == 1 and saved["folder_url"].endswith("/FOLDER1") and "built by the tool" in saved["note"]
    footage_cli.run_footage(SimpleNamespace(action="status", brand="swoveralls", brands_dir="brands"), store, NOW)
    assert "4 clip(s) (3 video, 45.5 s in all, 1 without a length yet), scanned 2026-11-27" in capsys.readouterr().out
    assert footage_cli.run_footage(SimpleNamespace(action="scan", brand="swoveralls", brands_dir="brands"), store, NOW, drive=None, missing="IDEA_BANK_FOLDER_ID") == 1
    assert "the IDEA_BANK_FOLDER_ID secret is not set" in capsys.readouterr().out
    empty = Store(tmp_path / "e")
    footage_cli.run_footage(args, empty, NOW, drive=FakeDrive([]))
    assert "The folder is empty: put the clips in https://drive.google.com/drive/folders/FOLDER1" in capsys.readouterr().out
    footage_cli.run_footage(SimpleNamespace(action="status", brand="swoveralls", brands_dir="brands"), Store(tmp_path / "none"), NOW)
    assert "no footage inventory yet: put the clips in <Idea Bank folder>/Swoveralls/Footage" in capsys.readouterr().out


def test_the_drive_client_lists_a_whole_folder_tree_with_video_lengths_and_makes_the_footage_folder():
    class Client(D.DriveClient):
        def __init__(self):
            self.calls, self.root_id = [], "ROOT"

        def _json(self, method, url, **kw):
            q = kw["params"]["q"]
            self.calls.append(q)
            if "'ROOT'" in q:
                return {"files": [{"id": "sub", "name": "unboxing", "mimeType": D.FOLDER}, {"id": "a", "name": "a.mp4", "mimeType": "video/mp4", "size": "100", "videoMediaMetadata": {"durationMillis": "12500", "width": 1080, "height": 1920}}]}
            return {"files": [{"id": "b", "name": "b.jpg", "mimeType": "image/jpeg", "imageMediaMetadata": {"width": 10, "height": 10}}]}
    got = Client().list_tree("ROOT")
    assert [(f["name"], f["path"], f["seconds"]) for f in got] == [("a.mp4", "", 12.5), ("b.jpg", "unboxing", None)] and got[0]["width"] == 1080 and got[1]["width"] == 10
    made = []

    class Maker(D.DriveClient):
        def __init__(self):
            self.root_id = "ROOT"

        def ensure_folder(self, parent, name):
            made.append((parent, name))
            return f"{name}-id"
    assert Maker().footage_folder("Swoveralls") == "Footage-id" and made == [("ROOT", "Swoveralls"), ("Swoveralls-id", "Footage")]


def test_the_footage_workflow_runs_weekly_and_scans_by_default_and_says_where_the_clips_go():
    wf = yaml.safe_load(Path(".github/workflows/footage.yml").read_text(encoding="utf-8"))
    text = Path(".github/workflows/footage.yml").read_text(encoding="utf-8")
    assert wf[True]["schedule"] == [{"cron": "15 6 * * 1"}] and wf[True]["workflow_dispatch"]["inputs"]["action"]["default"] == "scan"
    assert "<Idea Bank folder> / <Brand name> / Footage /" in text and "GOOGLE_SERVICE_ACCOUNT_JSON" in text and "IDEA_BANK_FOLDER_ID" in text and "add_each data/*/extras/footage.json" in text and "bash .github/scripts/push-with-retry.sh" in text
