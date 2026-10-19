"""Per-brand storage on disk (data/<brand>/...). Brands can never share a folder.

Layout
  snapshots/<stamp>.json   a snapshot whenever real content changed (not for stock-only moves)
  latest.json              the last good catalogue (what generation reads)
  last_checked.json        tiny: when the last good sync ran, and what it found
  baseline.json            the human-confirmed product count
  pending.json             a clean snapshot waiting for baseline confirmation
  registry.json            every product ever seen, with presence state
  changes.jsonl            one line per detected change, appended over time
  failed_runs.jsonl        one line per rejected run (no product data kept)
  catalogue/               clean exports for reuse (products.json, products.csv, variants.csv)

Files are rewritten only when their content actually changes, so a quiet day adds
almost nothing to the repository.

This sits behind a small class so the files can later move to a database
without touching the rest of the tool.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from .brands import brand_data_dir


class StoreError(Exception):
    pass


def _dump(obj, compact):
    if compact:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1)


def _write_text(path, text):
    """Write text atomically, but only if it differs from what is there. True if written."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def _atomic_write(path, obj, compact=False):
    return _write_text(path, _dump(obj, compact))


def _read(path, default=None):
    if not path.is_file():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Store:
    def __init__(self, data_dir="data"):
        self.data_dir = Path(data_dir)

    def dir(self, slug):
        path = brand_data_dir(self.data_dir, slug)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _check_brand(self, slug, snapshot):
        if snapshot.get("brand") != slug:
            raise StoreError(
                f"Snapshot belongs to brand {snapshot.get('brand')!r}, not {slug!r}")

    def save_snapshot(self, slug, snapshot):
        self._check_brand(slug, snapshot)
        stamp = re.sub(r"[^0-9TZ]", "", snapshot["fetched_at"]) or "unknown"
        folder = self.dir(slug) / "snapshots"
        path = folder / f"{stamp}.json"
        n = 1
        while path.exists():
            path = folder / f"{stamp}-{n}.json"
            n += 1
        _atomic_write(path, snapshot, compact=True)
        return path

    def load_snapshot(self, path):
        return _read(Path(path))

    def load_latest(self, slug):
        snap = _read(self.dir(slug) / "latest.json")
        if snap is not None:
            self._check_brand(slug, snap)
        return snap

    def promote_latest(self, slug, snapshot):
        self._check_brand(slug, snapshot)
        return _atomic_write(self.dir(slug) / "latest.json", snapshot, compact=True)

    def load_last_checked(self, slug):
        return _read(self.dir(slug) / "last_checked.json")

    def save_last_checked(self, slug, info):
        return _atomic_write(self.dir(slug) / "last_checked.json", info)

    def append_changes(self, slug, run_at, events):
        if not events:
            return 0
        path = self.dir(slug) / "changes.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps({"at": run_at, **ev}, ensure_ascii=False, sort_keys=True) + "\n")
        return len(events)

    def read_changes(self, slug):
        path = self.dir(slug) / "changes.jsonl"
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def write_export(self, slug, name, text):
        """Write one file under catalogue/. Returns True only if the content changed."""
        if "/" in name or "\\" in name or name.startswith("."):
            raise StoreError(f"Unsafe export name {name!r}")
        return _write_text(self.dir(slug) / "catalogue" / name, text)

    def load_baseline(self, slug):
        return _read(self.dir(slug) / "baseline.json")

    def save_baseline(self, slug, baseline):
        _atomic_write(self.dir(slug) / "baseline.json", baseline)

    def clear_baseline(self, slug):
        path = self.dir(slug) / "baseline.json"
        if path.exists():
            path.unlink()

    def load_pending(self, slug):
        return _read(self.dir(slug) / "pending.json")

    def save_pending(self, slug, pending):
        _atomic_write(self.dir(slug) / "pending.json", pending)

    def clear_pending(self, slug):
        path = self.dir(slug) / "pending.json"
        if path.exists():
            path.unlink()

    def load_registry(self, slug):
        return _read(self.dir(slug) / "registry.json", default={})

    def save_registry(self, slug, registry):
        return _atomic_write(self.dir(slug) / "registry.json", registry)

    def log_failed(self, slug, entry):
        path = self.dir(slug) / "failed_runs.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def read_failed(self, slug):
        path = self.dir(slug) / "failed_runs.jsonl"
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
