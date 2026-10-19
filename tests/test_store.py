import json

import pytest

from contentintel.store import Store, StoreError, _write_text


def test_write_text_only_writes_when_content_differs(tmp_path):
    path = tmp_path / "a" / "file.txt"
    assert _write_text(path, "one") is True
    mtime = path.stat().st_mtime_ns
    assert _write_text(path, "one") is False
    assert path.stat().st_mtime_ns == mtime
    assert _write_text(path, "two") is True and path.read_text() == "two"


def test_no_temporary_files_are_left_behind(tmp_path):
    store = Store(tmp_path)
    store.save_registry("swoveralls", {"1": {"presence": "present"}})
    store.save_registry("swoveralls", {"1": {"presence": "suspected_removed"}})
    assert not list(tmp_path.rglob("*.tmp"))


def test_unchanged_registry_reports_no_write(tmp_path):
    store = Store(tmp_path)
    assert store.save_registry("swoveralls", {"1": {"a": 1}}) is True
    assert store.save_registry("swoveralls", {"1": {"a": 1}}) is False
    assert store.save_registry("swoveralls", {"1": {"a": 2}}) is True


def test_latest_and_snapshots_are_compact_and_key_sorted(tmp_path):
    store = Store(tmp_path)
    snap = {"brand": "swoveralls", "fetched_at": "2026-10-19T08:00:00Z", "z": 1, "a": [1, 2]}
    path = store.save_snapshot("swoveralls", snap)
    text = path.read_text()
    assert "\n" not in text and text.startswith('{"a":[1,2]')
    assert store.promote_latest("swoveralls", snap) is True
    assert store.promote_latest("swoveralls", snap) is False
    assert store.load_latest("swoveralls") == snap


def test_last_checked_round_trips_and_is_small(tmp_path):
    store = Store(tmp_path)
    info = {"run_at": "2026-10-19T08:00:00Z", "run_id": "abc", "count": 72, "gate_status": "pass"}
    assert store.load_last_checked("swoveralls") is None
    store.save_last_checked("swoveralls", info)
    assert store.load_last_checked("swoveralls") == info
    assert (store.dir("swoveralls") / "last_checked.json").stat().st_size < 400


def test_changes_are_appended_not_rewritten(tmp_path):
    store = Store(tmp_path)
    assert store.read_changes("duderobe") == []
    assert store.append_changes("duderobe", "T1", []) == 0
    assert not (store.dir("duderobe") / "changes.jsonl").exists()
    ev = {"type": "new", "product_id": "1", "title": "A", "handle": "a", "details": {}}
    assert store.append_changes("duderobe", "T1", [ev]) == 1
    assert store.append_changes("duderobe", "T2", [dict(ev, type="changed"), ev]) == 2
    got = store.read_changes("duderobe")
    assert [(g["at"], g["type"]) for g in got] == [("T1", "new"), ("T2", "changed"), ("T2", "new")]
    lines = (store.dir("duderobe") / "changes.jsonl").read_text().splitlines()
    assert len(lines) == 3 and all(json.loads(line) for line in lines)


def test_failed_runs_are_logged_per_brand(tmp_path):
    store = Store(tmp_path)
    store.log_failed("swoveralls", {"at": "T1", "status": "fail"})
    store.log_failed("swoveralls", {"at": "T2", "status": "fail"})
    assert [f["at"] for f in store.read_failed("swoveralls")] == ["T1", "T2"]
    assert store.read_failed("duderobe") == []


def test_brands_have_separate_files_for_everything(tmp_path):
    store = Store(tmp_path)
    store.save_registry("swoveralls", {"1": {}})
    store.save_last_checked("swoveralls", {"run_at": "T", "run_id": "x", "count": 1, "gate_status": "pass"})
    store.append_changes("swoveralls", "T", [{"type": "new", "product_id": "1", "title": "A", "handle": "a", "details": {}}])
    assert store.load_registry("duderobe") == {}
    assert store.load_last_checked("duderobe") is None
    assert store.read_changes("duderobe") == []


def test_snapshot_must_belong_to_the_brand_it_is_saved_under(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(StoreError):
        store.save_snapshot("duderobe", {"brand": "swoveralls", "fetched_at": "T"})
