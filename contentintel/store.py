"""Per-brand storage on disk (data/<brand>/...). Brands can never share a folder.

Layout
  snapshots/<stamp>.json   a snapshot whenever real content changed (not for stock-only moves)
  latest.json              the last good catalogue (what generation reads)
  last_checked.json        tiny: when the last good sync ran, and what it found
  baseline.json            the human-confirmed product count
  pending.json             a clean snapshot waiting for baseline confirmation
  registry.json            every product ever seen, with presence state
  sheet.json               which Google spreadsheet belongs to this brand
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

    def dir(self, slug, create=True):
        """A brand's data folder. Reading must never create it, so readers pass create=False."""
        path = brand_data_dir(self.data_dir, slug)
        if create:
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
        snap = _read(self.dir(slug, create=False) / "latest.json")
        if snap is not None:
            self._check_brand(slug, snap)
        return snap

    def promote_latest(self, slug, snapshot):
        self._check_brand(slug, snapshot)
        return _atomic_write(self.dir(slug) / "latest.json", snapshot, compact=True)

    def load_sheet_info(self, slug):
        return _read(self.dir(slug, create=False) / "sheet.json")

    def save_sheet_info(self, slug, info):
        return _atomic_write(self.dir(slug) / "sheet.json", info)

    def load_last_checked(self, slug):
        return _read(self.dir(slug, create=False) / "last_checked.json")

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
        path = self.dir(slug, create=False) / "changes.jsonl"
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    # --- research: what the search providers returned, per brand -------------------------
    def append_searches(self, slug, records):
        """Add search records (one JSON line each) to research/searches.jsonl."""
        if not records:
            return 0
        path = self.dir(slug) / "research" / "searches.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return len(records)

    def _research_file(self, slug, name):
        """A research file's path. Reading must not create folders, so this never mkdirs."""
        return brand_data_dir(self.data_dir, slug) / "research" / name

    def read_searches(self, slug):
        path = self._research_file(slug, "searches.jsonl")
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def append_research_run(self, slug, record):
        path = self.dir(slug) / "research" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def read_research_runs(self, slug):
        path = self._research_file(slug, "runs.jsonl")
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def append_collected(self, slug, record):
        path = self._research_file(slug, "collected.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def read_collected(self, slug):
        path = self._research_file(slug, "collected.jsonl")
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def read_auto_seeds(self, slug):
        path = self._research_file(slug, "auto_seeds.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def write_auto_seeds(self, slug, data):
        path = self._research_file(slug, "auto_seeds.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    def read_benchmarks(self, slug):
        path = self._research_file(slug, "benchmarks.json")
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except ValueError:
            return None                 # a damaged file is treated as "nothing decided yet", never as a crash

    def write_benchmarks(self, slug, data):
        path = self._research_file(slug, "benchmarks.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))

    def read_deep_archive(self, slug):
        path = self._research_file(slug, "deep_archive.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except ValueError:
            return None                 # damaged: treated as "nothing archived yet" (a tab may then be added twice, never lost)
        return data if isinstance(data, dict) and isinstance(data.get("items"), dict) else None

    def write_deep_archive(self, slug, data):
        path = self._research_file(slug, "deep_archive.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))

    def read_deep_report(self, slug, name):
        """The full text of a saved Deep Research report, or "" if there is none. Reading creates nothing."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", name):
            return ""
        path = brand_data_dir(self.data_dir, slug) / "research" / "deep" / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def write_deep_report(self, slug, name, text):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", name):
            raise StoreError(f"unsafe report name {name!r}")
        path = brand_data_dir(self.data_dir, slug) / "research" / "deep" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # --- research credits are shared by every brand (one key, one budget) ----------------
    def load_usage(self):
        return _read(self.data_dir / "_shared" / "research_usage.json",
                     {"months": {}, "lifetime": {}})

    def save_usage(self, usage):
        return _atomic_write(self.data_dir / "_shared" / "research_usage.json", usage)

    def _brand_file(self, slug, *parts):
        """A path inside a brand's data folder. Reading must not create folders, so this never mkdirs."""
        return brand_data_dir(self.data_dir, slug).joinpath(*parts)

    def read_ideabank(self, slug):
        """{"next": n, "items": {"IB-0001": {...}}}: the brand's Idea Bank. Reading creates nothing."""
        try:
            data = _read(self._brand_file(slug, "ideabank", "items.json"), {})
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        items = data.get("items") if isinstance(data.get("items"), dict) else {}
        nxt = data.get("next") if isinstance(data.get("next"), int) and data["next"] >= 1 else len(items) + 1
        return {"next": max(nxt, len(items) + 1), "items": items}

    def write_ideabank(self, slug, data):
        return _atomic_write(self._brand_file(slug, "ideabank", "items.json"), data)

    def list_requests(self, slug):
        folder = self._brand_file(slug, "requests")
        out = []
        for path in sorted(folder.glob("RQ-*.json")) if folder.is_dir() else []:
            try:
                data = _read(path, None)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == path.stem:
                out.append(data)
        return out

    def read_request(self, slug, request_id):
        if not re.fullmatch(r"RQ-\d{4,}", str(request_id)):
            return None
        try:
            data = _read(self._brand_file(slug, "requests", f"{request_id}.json"), None)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def write_request(self, slug, request):
        return _atomic_write(self._brand_file(slug, "requests", f"{request['id']}.json"), request)

    def next_request_id(self, slug):
        highest = max([int(r["id"][3:]) for r in self.list_requests(slug)] or [0])
        return f"RQ-{highest + 1:04d}"

    def list_briefs(self, slug):
        folder = self._brand_file(slug, "briefs")
        out = []
        for path in sorted(folder.glob("BR-*.json")) if folder.is_dir() else []:
            try:
                data = _read(path, None)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == path.stem:
                out.append(data)
        return out

    def write_brief(self, slug, record, docx_bytes=None):
        wrote = _atomic_write(self._brand_file(slug, "briefs", f"{record['id']}.json"), record)
        if docx_bytes is not None:
            path = self._brand_file(slug, "briefs", f"{record['id']}.docx")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(docx_bytes)
        return wrote

    def next_brief_id(self, slug):
        highest = max([int(b["id"][3:]) for b in self.list_briefs(slug)] or [0])
        return f"BR-{highest + 1:04d}"

    def read_claude_usage(self):
        """{"2026-10": calls}: Claude calls made by the Idea Bank, request and brief features. Reading creates nothing."""
        try:
            data = _read(self.data_dir / "_shared" / "claude_usage.json", {})
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_claude_usage(self, data):
        return _atomic_write(self.data_dir / "_shared" / "claude_usage.json", data)

    def read_cooldowns(self):
        """site -> {until, at, reason}: accounts resting after a warning. Reading creates nothing."""
        try:
            data = _read(self.data_dir / "_shared" / "cooldowns.json", {})
        except ValueError:
            return {}                   # a damaged file means "nothing is resting", never a crash
        return data if isinstance(data, dict) else {}

    def write_cooldowns(self, data):
        return _atomic_write(self.data_dir / "_shared" / "cooldowns.json", data)

    def read_page_log(self):
        """{"days": {date: {site: pages opened}}, "cursor": {site: where the next run starts}}. Reading creates nothing."""
        try:
            data = _read(self.data_dir / "_shared" / "page_log.json", {})
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        days, cursor = data.get("days"), data.get("cursor")
        return {"days": days if isinstance(days, dict) else {}, "cursor": cursor if isinstance(cursor, dict) else {}}

    def write_page_log(self, data):
        keep = sorted(data.get("days", {}))[-14:]                       # two weeks is plenty
        return _atomic_write(self.data_dir / "_shared" / "page_log.json",
                             {"days": {d: data["days"][d] for d in keep}, "cursor": data.get("cursor", {})})

    def write_export(self, slug, name, text):
        """Write one file under catalogue/. Returns True only if the content changed."""
        if "/" in name or "\\" in name or name.startswith("."):
            raise StoreError(f"Unsafe export name {name!r}")
        return _write_text(self.dir(slug) / "catalogue" / name, text)

    def load_baseline(self, slug):
        return _read(self.dir(slug, create=False) / "baseline.json")

    def save_baseline(self, slug, baseline):
        _atomic_write(self.dir(slug) / "baseline.json", baseline)

    def clear_baseline(self, slug):
        path = self.dir(slug) / "baseline.json"
        if path.exists():
            path.unlink()

    def load_pending(self, slug):
        return _read(self.dir(slug, create=False) / "pending.json")

    def save_pending(self, slug, pending):
        _atomic_write(self.dir(slug) / "pending.json", pending)

    def clear_pending(self, slug):
        path = self.dir(slug) / "pending.json"
        if path.exists():
            path.unlink()

    def load_registry(self, slug):
        return _read(self.dir(slug, create=False) / "registry.json", default={})

    def save_registry(self, slug, registry):
        return _atomic_write(self.dir(slug) / "registry.json", registry)

    def log_failed(self, slug, entry):
        path = self.dir(slug) / "failed_runs.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def read_failed(self, slug):
        path = self.dir(slug, create=False) / "failed_runs.jsonl"
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
