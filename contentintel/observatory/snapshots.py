"""Dated snapshots of what was read, kept compressed, with a retention policy and an append-only event log.

One folder per competitor site, shared by every brand that watches it (a site is fetched once per run, however many brands care):

    data/_observatory/sites/<site>/<kind>/<stamp>.json.gz     the snapshots, one folder per kind (catalogue, sitemap, feed, page-<n>)
    data/_observatory/sites/<site>/<kind>/index.json          what exists, with hashes and the last time each was confirmed
    data/_observatory/sites/<site>/events.jsonl               every change ever detected
    data/_observatory/sites/<site>/state.json                 small memory the diff needs between runs (e.g. how long a product has been missing)

When a new reading is identical to the latest one it is not written again; the index just records that it was confirmed unchanged at
that time. That keeps the repository small, and the history is still complete (a snapshot's `confirmed_at` says how long it held).
"""
import gzip
import hashlib
import json
import re
from pathlib import Path

from .. import health

_VOLATILE = ("captured_at", "stats", "waited")


class SnapshotError(Exception):
    pass


def _canonical(snapshot):
    body = {k: v for k, v in snapshot.items() if k not in _VOLATILE}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(snapshot):
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()[:20]


def _atomic(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _stamp(now):
    return re.sub(r"[^0-9TZ]", "", now) or "unknown"


class SnapshotStore:
    def __init__(self, data_dir="data"):
        self.root = Path(data_dir) / "_observatory" / "sites"

    def site_dir(self, site):
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", site or ""):
            raise SnapshotError(f"{site!r} is not a valid site name")
        return self.root / site

    def sites(self):
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []

    # ---- snapshots -------------------------------------------------------------------------------------------------------------------------

    def _index_path(self, site, kind):
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", kind or ""):
            raise SnapshotError(f"{kind!r} is not a valid snapshot kind")
        return self.site_dir(site) / kind / "index.json"

    def entries(self, site, kind):
        """Index entries, oldest first."""
        path = self._index_path(site, kind)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SnapshotError(f"the index of {site}/{kind} is damaged; nothing was changed") from None
        return sorted((e for e in data.get("entries", []) if isinstance(e, dict)), key=lambda e: e["captured_at"])

    def _write_index(self, site, kind, entries):
        _atomic(self._index_path(site, kind), json.dumps({"version": 1, "entries": entries}, indent=1, sort_keys=True).encode("utf-8"))

    def save(self, site, kind, snapshot, now):
        """Store a reading. Returns (entry, written): written is False when it matched the latest one and was only confirmed."""
        entries = self.entries(site, kind)
        digest = content_hash(snapshot)
        if entries and entries[-1]["hash"] == digest and entries[-1].get("complete") == bool(snapshot.get("complete", True)):
            entries[-1]["confirmed_at"] = now
            self._write_index(site, kind, entries)
            return entries[-1], False
        name = f"{_stamp(snapshot.get('captured_at') or now)}.json.gz"
        folder = self.site_dir(site) / kind
        n, path = 1, folder / name
        while path.exists():
            path = folder / name.replace(".json.gz", f"-{n}.json.gz")
            n += 1
        raw = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        _atomic(path, gzip.compress(raw, 6, mtime=0))
        entry = {"file": path.name, "captured_at": snapshot.get("captured_at") or now, "confirmed_at": now, "hash": digest, "bytes": path.stat().st_size,
                 "complete": bool(snapshot.get("complete", True)), "count": _count(snapshot)}
        entries.append(entry)
        self._write_index(site, kind, entries)
        return entry, True

    def load(self, site, kind, entry):
        path = self.site_dir(site) / kind / entry["file"]
        try:
            return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, EOFError):
            raise SnapshotError(f"the snapshot {site}/{kind}/{entry['file']} could not be read") from None

    def latest(self, site, kind, complete_only=False):
        """(entry, snapshot) of the newest reading, or (None, None)."""
        for entry in reversed(self.entries(site, kind)):
            if complete_only and not entry.get("complete"):
                continue
            return entry, self.load(site, kind, entry)
        return None, None

    def history(self, site, kind, limit=None):
        entries = self.entries(site, kind)
        return entries[-limit:] if limit else entries

    def kinds(self, site):
        base = self.site_dir(site)
        return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "index.json").is_file()) if base.is_dir() else []

    def prune(self, site, kind, retention, now):
        """Apply the retention policy; returns the file names removed. The newest few are never removed."""
        entries = self.entries(site, kind)
        keep_latest = retention["always_keep_latest"]
        keep = {e["file"] for e in entries[-keep_latest:]}
        seen_weeks, seen_months = set(), set()
        for e in reversed(entries):
            age = health.age_days(e["captured_at"], now) or 0
            week = e["captured_at"][:4] + "-W" + _week(e["captured_at"])
            month = e["captured_at"][:7]
            if age <= retention["daily_days"]:
                keep.add(e["file"])
            elif age <= retention["daily_days"] + retention["weekly_weeks"] * 7 and week not in seen_weeks:
                seen_weeks.add(week)
                keep.add(e["file"])
            elif age <= retention["monthly_months"] * 31 and month not in seen_months:
                seen_months.add(month)
                keep.add(e["file"])
        removed = []
        for e in entries:
            if e["file"] not in keep:
                try:
                    (self.site_dir(site) / kind / e["file"]).unlink()
                except FileNotFoundError:
                    pass
                removed.append(e["file"])
        if removed:
            self._write_index(site, kind, [e for e in entries if e["file"] in keep])
        return removed

    # ---- events and state ------------------------------------------------------------------------------------------------------------------

    def append_events(self, site, events):
        if not events:
            return 0
        path = self.site_dir(site) / "events.jsonl"
        known = {e["event_id"] for e in self.read_events(site)}
        fresh = [e for e in events if e["event_id"] not in known]
        if fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for e in fresh:
                    fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        return len(fresh)

    def read_events(self, site):
        path = self.site_dir(site) / "events.jsonl"
        if not path.is_file():
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue            # a half-written last line must not hide the rest
        return out

    def load_state(self, site):
        path = self.site_dir(site) / "state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def read_social(self, site):
        """The accounts (instagram, tiktok, youtube) found on a site's own pages, {} when none have been found."""
        try:
            data = json.loads((self.site_dir(site) / "social.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if k in ("instagram", "tiktok", "youtube") and isinstance(v, str) and v}

    def write_social(self, site, social, now):
        data = {k: v for k, v in social.items() if k in ("instagram", "tiktok", "youtube") and v}
        data["found_at"] = now
        _atomic(self.site_dir(site) / "social.json", json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))

    def save_state(self, site, state):
        _atomic(self.site_dir(site) / "state.json", json.dumps(state, indent=1, sort_keys=True).encode("utf-8"))


def _week(ts):
    from datetime import datetime
    try:
        return f"{datetime.strptime(ts[:10], '%Y-%m-%d').isocalendar()[1]:02d}"
    except ValueError:
        return "00"


def _count(snapshot):
    for key in ("products", "urls", "items", "pages"):
        value = snapshot.get(key)
        if isinstance(value, (list, dict)):
            return len(value)
    return 1
