"""A site that rate-limited us, or keeps erroring, rests: the pause is written down and survives between runs.

Kept in one small file (data/_observatory/pauses.json), never in a brand's folder, because the pause belongs to the site and
not to any brand. It also remembers the delay the throttle had learned, so the next run starts gently instead of at full speed.
"""
import json
from pathlib import Path

from .. import health

_FILE = "pauses.json"
_KEEP_DAYS = 30


class PauseLedger:
    def __init__(self, data_dir="data"):
        self.path = Path(data_dir) / "_observatory" / _FILE
        self.data = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self.data = {}
        self.dirty = False

    def paused(self, host, now):
        """(reason, until) while the site rests, else None."""
        entry = self.data.get(host)
        if not entry or not entry.get("until"):
            return None
        until, current = health._parse(entry["until"]), health._parse(now)
        if until is None or current is None or until <= current:
            return None
        return entry.get("reason", ""), entry["until"], entry.get("code", "")

    def pause(self, host, seconds, reason, now, code="RATE_LIMITED"):
        entry = self.data.setdefault(host, {})
        entry.update(until=health.add_seconds(now, seconds), reason=reason[:200], code=code, updated=now)
        self.dirty = True

    def clear(self, host=None):
        if host is None:
            self.dirty = self.dirty or bool(self.data)
            self.data = {}
        elif host in self.data:
            del self.data[host]
            self.dirty = True

    def delay_hint(self, host):
        value = (self.data.get(host) or {}).get("delay")
        return float(value) if isinstance(value, (int, float)) else None

    def remember_delay(self, host, delay, now):
        entry = self.data.setdefault(host, {})
        if entry.get("delay") != round(delay, 2):
            entry.update(delay=round(delay, 2), updated=now)
            self.dirty = True

    def rows(self, now):
        return [{"host": h, "resting": self.paused(h, now) is not None, **{k: v for k, v in e.items() if k in ("until", "reason", "code", "delay", "updated")}}
                for h, e in sorted(self.data.items())]

    def save(self, now=None):
        if not self.dirty:
            return False
        if now:                                  # forget old entries so the file does not grow for ever
            self.data = {h: e for h, e in self.data.items()
                         if (health.age_days(e.get("updated") or now, now) or 0) <= _KEEP_DAYS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False
        return True
