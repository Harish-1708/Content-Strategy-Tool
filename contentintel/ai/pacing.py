"""Pacing: every call to a provider is sent at no more than that provider's OWN stated limits, built once here so every lane and every future stage gets it.

This is not a limit of ours. It stays at the provider's own ceiling: the tool uses the full allowance the provider gives, and never sends a call the provider would only reject. What it reads
from each provider's `limits` in ai_providers.yaml: requests_per_minute, requests_per_hour and concurrency (how many calls at the same time: Ollama allows one). What it honours from the
provider's own answers: Retry-After on a 429 (nothing more is sent to that provider until that time). A call that would have to wait longer than `pacing.fast_wait_seconds` is passed over
for the next provider first (load is spread, not queued behind one provider); only when no provider can answer sooner does it wait, up to `pacing.max_wait_seconds`.
"""
import threading
import time
from collections import deque

POLL = 0.25                                  # seconds between looks when the provider's concurrency limit is full
HOUR = 3600.0


class Pacer:
    def __init__(self, pacing=None, *, clock=time.monotonic, sleep=time.sleep):
        self.cfg = {"fast_wait_seconds": 2, "max_wait_seconds": 120, **(pacing or {})}
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._sent, self._busy, self._blocked = {}, {}, {}
        self.waited = {}                       # provider id -> seconds spent waiting for a slot, for the report

    @property
    def fast_wait(self):
        return float(self.cfg["fast_wait_seconds"])

    @property
    def max_wait(self):
        return float(self.cfg["max_wait_seconds"])

    def _need(self, provider, now):
        """Seconds until one more request may be sent to this provider: 0 now. Counts Retry-After, the minute and hour windows and the concurrency limit. Call with the lock held."""
        lim = provider.limits or {}
        sent = self._sent.setdefault(provider.id, deque())
        while sent and now - sent[0] >= HOUR:
            sent.popleft()
        waits = [self._blocked.get(provider.id, 0.0) - now]
        for key, span in (("requests_per_minute", 60.0), ("requests_per_hour", HOUR)):
            cap = lim.get(key)
            if cap is not None and cap >= 1:
                inside = [t for t in sent if now - t < span]
                if len(inside) >= cap:
                    waits.append(inside[len(inside) - int(cap)] + span - now)         # when the oldest call that still counts leaves the window
        conc = lim.get("concurrency")
        if conc is not None and self._busy.get(provider.id, 0) >= conc:
            waits.append(POLL)
        return max(0.0, *waits)

    def wait_for(self, provider):
        """How long until a call could be sent to this provider, without sending anything (for plans and reports)."""
        with self._lock:
            return self._need(provider, self.clock())

    def acquire(self, provider, max_wait):
        """Reserve a slot at this provider. Returns (True, seconds_waited) when a call may be sent now, or (False, seconds_until_it_could) when that would take longer than max_wait."""
        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                need = self._need(provider, now)
                if need <= 0:
                    self._sent[provider.id].append(now)
                    self._busy[provider.id] = self._busy.get(provider.id, 0) + 1
                    if waited:
                        self.waited[provider.id] = self.waited.get(provider.id, 0.0) + waited
                    return True, waited
            if waited + need > max_wait:
                return False, need
            self.sleep(need)
            waited += need

    def release(self, provider):
        with self._lock:
            self._busy[provider.id] = max(0, self._busy.get(provider.id, 0) - 1)

    def rate_limited(self, provider, retry_after):
        """The provider said 'too many requests' and how long to wait (Retry-After, in seconds): nothing more is sent to it until then."""
        if retry_after:
            with self._lock:
                self._blocked[provider.id] = max(self._blocked.get(provider.id, 0.0), self.clock() + float(retry_after))
