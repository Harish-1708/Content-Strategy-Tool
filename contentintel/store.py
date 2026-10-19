"""Per-brand storage on disk (data/<brand>/...). Brands can never share a folder.

Layout
  snapshots/<stamp>.json   every accepted fetch
  latest.json              the last good snapshot (what generation reads)
  baseline.json            the human-confirmed product count
  pending.json             a clean snapshot waiting for baseline confirmation
  registry.json            every product ever seen, with presence state
  failed_runs.jsonl        one line per rejected run (no product data kept)

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


def _atomic_write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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
        _atomic_write(path, snapshot)
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
        _atomic_write(self.dir(slug) / "latest.json", snapshot)

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
        _atomic_write(self.dir(slug) / "registry.json", registry)

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
