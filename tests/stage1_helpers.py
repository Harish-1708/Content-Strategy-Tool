"""Shared builders for the Stage 1 tests: the shipped registry, and copies of it with other thresholds."""
from pathlib import Path

import yaml

from contentintel import sources as S

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "sources.yaml"
NOW = "2026-11-02T10:00:00Z"


def shipped():
    return S.load_sources(SHIPPED)


def registry(tmp_path, **defaults):
    """The shipped registry with some defaults changed (for example degrade_after_failures=2)."""
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    pauses = defaults.pop("pause_seconds", None)
    raw["defaults"].update(defaults)
    if pauses:
        raw["defaults"]["pause_seconds"].update(pauses)
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return S.load_sources(path)


def at(base, **delta):
    """A timestamp shifted from base (an ISO string) by days/hours/minutes/seconds."""
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(**delta)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")
