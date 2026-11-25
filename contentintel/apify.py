"""Apify: ready-made scrapers run on Apify's servers. An optional extra, off unless switched on in
research.yaml (collectors.apify.mode). Needs APIFY_TOKEN.

Two ways to run an actor, and the switch between them is the size of the ask:

* sync -- POST /v2/acts/{actor}/run-sync-get-dataset-items -- waits for the items in one call.
  Only for SMALL asks (max_items at most SYNC_MAX_ITEMS, 10): Apify enforces a hard 300-second
  ceiling on sync calls, and a big ask can outrun it. A small ask that answers in time is one
  HTTP call and nothing else changes for it.
* async -- POST /v2/acts/{actor}/runs returns a run ID at once (no 300-second cap); the run is
  then polled (GET /v2/actor-runs/{runId} every poll_interval seconds, at most max_wait) until
  SUCCEEDED/FAILED/TIMED-OUT, and the items are fetched from its dataset
  (GET /v2/datasets/{datasetId}/items). Anything bigger than a small ask goes this way, and so
  does any call that asks for mode="async" outright. mode="sync" forces the sync endpoint.

A 408 from a sync call means "not finished yet", never "failed": the run keeps going server-side.
When the 408 answer names the run, this client polls that run and fetches its dataset instead of
starting anything again. When it names no run, it raises ApifyRunUnfinished: the run may still be
going server-side, so the same ask must NOT be started again (that would run, and charge, twice).
A 408 never drops a key in ApifyPool (it is not a refusal) and never counts as a key failure.

A payload that names several targets (hashtags, profiles, searchQueries) is split into one call
per target, each carrying its own per-target count, instead of one big call; the items are merged
in order, capped at max_items, and a call stops being made once the cap is reached. Each split
call carries the same cost ceiling (maxTotalChargeUsd): the ceiling caps one call, and what a call
actually spends is per result, so splitting does not multiply the spend -- it only multiplies the
worst case, which the ceiling still bounds per call.

The clockworks TikTok actor's own date filter does not work on hashtags (live side-by-side test:
#booktok videos up to 3 years 8 months old came back), so when a date window is asked for
(since/until), this client filters by createTimeISO itself after the call. Rows with no readable
date are kept: their age cannot be proved, so dropping them would lose data on a guess.
"""
import json
import re
import time
from datetime import date, datetime, timezone

from .providers import AuthError, Provider, ProviderConfigError, ProviderError, QuotaError

SYNC_MAX_ITEMS = 10          # a sync call is only for an ask this small; anything bigger starts async and is polled
DEFAULT_POLL_INTERVAL = 5    # seconds between status reads of an async run (Apify's own guidance: 5-10)
DEFAULT_MAX_WAIT = 900       # seconds an async run (or a 408 recovery) is waited for at most: 15 minutes, then it is reported unfinished
SPLIT_KEYS = ("hashtags", "profiles", "searchQueries")     # one target per call: a payload naming several is split on these
DATE_KEYS = ("createTimeISO", "createTime", "timestamp", "posted_at")
_TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")
_QUOTA_WORDS = ("not enough credits", "run out of searches", "out of searches", "usage limit",
                "quota", "exceeds your plan", "out of credits", "plan's set")


class ApifyRunUnfinished(ProviderError):
    """The actor run did not finish and its items could not be fetched -- but it may still be going
    server-side. Never start the same ask again on this error: that would run, and charge, twice.
    Never a reason to drop the key (ApifyPool only drops a key on a refusal or a 429)."""


class _Sync408(Exception):
    """A sync call answered 408: the run did not finish in time. Carries the answer's body, which
    may name the run that is still going."""

    def __init__(self, body):
        super().__init__("sync run did not finish in time")
        self.body = body


def _parse_date(value):
    """An ISO date/datetime, a date/datetime, or epoch seconds as an aware UTC datetime, or None when
    the value cannot be read as a date."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _item_date(item):
    """When a result row was posted, from the first date field it carries that can be read, or None."""
    if not isinstance(item, dict):
        return None
    for key in DATE_KEYS:
        parsed = _parse_date(item.get(key))
        if parsed is not None:
            return parsed
    return None


def filter_by_create_time(items, since=None, until=None):
    """The rows whose posted date falls inside [since, until] (either end may be left out). Rows with
    no readable date are kept: their age cannot be proved. `since`/`until` take an ISO date or
    datetime, a date/datetime, or epoch seconds."""
    start = _parse_date(since) if since is not None else None
    end = _parse_date(until) if until is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError(f"Apify: since ({since!r}) is after until ({until!r})")
    if start is None and end is None:
        return list(items or [])
    kept = []
    for item in items or []:
        posted = _item_date(item)
        if posted is None or ((start is None or posted >= start) and (end is None or posted <= end)):
            kept.append(item)
    return kept


def _run_id_from_body(body):
    """The run ID an Apify answer names, if it names one. Apify's public docs do not pin the shape
    of a 408 answer, so every shape seen in its error answers is tried: a data/run id, an id inside
    the error, and an id inside the error's message ("run ID: <id>"). None when nothing names one."""
    if isinstance(body, dict):
        for place in (body.get("data"), (body.get("error") or {}).get("data") if isinstance(body.get("error"), dict) else None):
            if isinstance(place, dict):
                for key in ("id", "runId"):
                    if isinstance(place.get(key), str) and place[key]:
                        return place[key]
        for key in ("id", "runId"):
            if isinstance(body.get(key), str) and body[key]:
                return body[key]
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            found = re.search(r"run ID: ([A-Za-z0-9_-]+)", error["message"])
            if found:
                return found.group(1)
    return None


def reddit_held(path="research.yaml"):
    """True while the owner has Reddit on hold (research.yaml apify.reddit_hold, default TRUE). Reddit refuses Apify's datacenter addresses and its residential proxy costs about 8 dollars a GB, which would use the 20 dollar month up too fast, so no Apify Reddit call is made until the owner lifts the hold."""
    try:
        import yaml
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        return bool((data.get("apify") or {}).get("reddit_hold", True))
    except Exception:                                                                    # noqa: BLE001 - no readable file: the hold stays on
        return True


class Apify(Provider):
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"
    base = "https://api.apify.com/v2"

    def _auth(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _call_sync(self, actor, payload, *, max_items, max_cost_usd, timeout):
        """One sync call, with the shared provider's retry shape (a 429 or a server error is retried;
        a refusal is not). Returns the dataset items. Raises _Sync408 on HTTP 408 instead of failing:
        the run is still going server-side and the caller decides what to do with it."""
        url = f"{self.base}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
        params = {"timeout": timeout, "maxItems": max_items, "maxTotalChargeUsd": max_cost_usd}
        last = "no response"
        for attempt in range(3):
            try:
                status, body, retry_after = self.http(
                    "POST", url, params=params, json_body=payload, headers=self._auth(),
                    timeout=timeout + 30)
            except Exception as exc:                      # network problem
                last = f"network error ({exc.__class__.__name__})"
                self.sleep(2 ** attempt)
                continue
            if status in (200, 201):
                self.last_note = "response contained: " + (f"list of {len(body)}" if isinstance(body, list) else type(body).__name__)
                if not isinstance(body, list):
                    raise ProviderError(f"{self.label}: expected a list of items, got {type(body).__name__}")
                return body
            text = body if isinstance(body, str) else json.dumps(body)
            low = text.lower()
            if status in (401, 403):
                raise AuthError(f"{self.label} rejected the API key (HTTP {status})")
            if status in (402, 432, 433) or any(w in low for w in _QUOTA_WORDS):
                raise QuotaError(f"{self.label} is out of credits (HTTP {status})")
            if status == 408:
                raise _Sync408(body)
            if status == 429 or status >= 500:
                last = f"HTTP {status}"
                wait = int(retry_after) if str(retry_after or "").isdigit() else 2 ** attempt
                self.sleep(min(wait, 30))
                continue
            raise ProviderError(f"{self.label}: HTTP {status}: {self._clean(text)[:200]}" + self._why_run_failed(body))
        raise ProviderError(f"{self.label}: gave up after retries ({last})")

    def _why_run_failed(self, body):
        """' (the actor said: ...)': what the failed run's own status message says, read with one GET of the run the answer names. '' when no run is named or the read fails: a diagnostic, never a new failure."""
        text = body if isinstance(body, str) else json.dumps(body)
        run_id = _run_id_from_body(body) if isinstance(body, dict) else None
        if not run_id:
            found = re.search(r"run ID:? ?([A-Za-z0-9]{12,24})", text)
            run_id = found.group(1) if found else None
        if not run_id:
            return ""
        try:
            status, got, _retry = self.http("GET", f"{self.base}/actor-runs/{run_id}", params=None, json_body=None, headers=self._auth(), timeout=20)
            data = (got.get("data") if isinstance(got, dict) else None) or {}
            said = str(data.get("statusMessage") or "").strip()
            return f" (run {run_id} {data.get('status', '')}: the actor said: {self._clean(said)[:240]})" if said else f" (run {run_id}: the actor gave no reason)"
        except Exception:                                                                 # noqa: BLE001
            return f" (run {run_id}; its status message could not be read)"

    def _start_run(self, actor, payload, *, max_items, max_cost_usd, max_wait):
        """Start an async run and return its run record at once. One attempt only, never retried: a
        retry could start a second, billed run while the first is already going."""
        url = f"{self.base}/acts/{actor.replace('/', '~')}/runs"
        params = {"timeout": max_wait, "maxItems": max_items, "maxTotalChargeUsd": max_cost_usd}
        try:
            status, body, _retry = self.http("POST", url, params=params, json_body=payload,
                                             headers=self._auth(), timeout=30)
        except Exception as exc:                      # network problem: the run may have started anyway
            raise ProviderError(f"{self.label}: the run could not be started (network error ({exc.__class__.__name__})); "
                                "it may have started server-side, so do not start the same ask again without checking") from None
        if status in (200, 201):
            run = body.get("data", body) if isinstance(body, dict) else {}
            if not isinstance(run, dict) or not run.get("id"):
                raise ProviderError(f"{self.label}: the run started but Apify named no run ID")
            return run
        text = body if isinstance(body, str) else json.dumps(body)
        low = text.lower()
        if status in (401, 403):
            raise AuthError(f"{self.label} rejected the API key (HTTP {status})")
        if status in (402, 432, 433) or any(w in low for w in _QUOTA_WORDS):
            raise QuotaError(f"{self.label} is out of credits (HTTP {status})")
        raise ProviderError(f"{self.label}: HTTP {status}: {self._clean(text)[:200]}")

    def _poll_run(self, run_id, *, poll_interval, max_wait):
        """The run record once its status is terminal (SUCCEEDED, FAILED, ABORTED or TIMED-OUT), or
        ApifyRunUnfinished when it is still going after max_wait: it may finish later, so the same
        ask must not be started again."""
        deadline = time.monotonic() + max_wait
        while True:
            body = self._call("GET", f"{self.base}/actor-runs/{run_id}", headers=self._auth(), ok=(200,), timeout=30)
            run = body.get("data", body) if isinstance(body, dict) else {}
            status = run.get("status") if isinstance(run, dict) else None
            if status == "SUCCEEDED":
                return run
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise ProviderError(f"{self.label}: the actor run {run_id} ended {status}"
                                    + ("; asking again for fewer items may finish sooner" if status == "TIMED-OUT" else ""))
            if time.monotonic() >= deadline:
                raise ApifyRunUnfinished(f"{self.label}: the actor run {run_id} is still running after {max_wait:g} seconds; "
                                         "its items cannot be fetched yet and the run may still finish server-side -- "
                                         "do not start the same ask again or it would run, and charge, twice")
            self.sleep(poll_interval)

    def _fetch_dataset(self, run, *, max_items):
        """The dataset items of a finished run, at most max_items."""
        run_id = run.get("id") if isinstance(run, dict) else None
        dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else None
        if dataset_id:
            url = f"{self.base}/datasets/{dataset_id}/items"
        elif run_id:
            url = f"{self.base}/actor-runs/{run_id}/dataset/items"
        else:
            raise ProviderError(f"{self.label}: the finished run named no dataset to read")
        body = self._call("GET", url, params={"limit": max_items, "offset": 0, "format": "json"},
                          headers=self._auth(), ok=(200,), timeout=30)
        if not isinstance(body, list):
            raise ProviderError(f"{self.label}: expected a list of items, got {type(body).__name__}")
        return body

    def _run_async(self, actor, payload, *, max_items, max_cost_usd, poll_interval, max_wait):
        """Start, poll until terminal, fetch the dataset."""
        run = self._start_run(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd, max_wait=max_wait)
        finished = self._poll_run(run["id"], poll_interval=poll_interval, max_wait=max_wait)
        return self._fetch_dataset(finished, max_items=max_items)

    def _run_sync_or_recover(self, actor, payload, *, max_items, max_cost_usd, timeout, poll_interval, max_wait):
        """One sync call; on a 408, poll the run that is still going (when its ID is known) and fetch
        its dataset instead of starting anything again."""
        try:
            return self._call_sync(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd, timeout=timeout)
        except _Sync408 as timed_out:
            run_id = _run_id_from_body(timed_out.body)
            if run_id is None:
                raise ApifyRunUnfinished(
                    f"{self.label}: HTTP 408: the run did not finish within {timeout:g} seconds and Apify returned no run ID, "
                    "so its items cannot be fetched; the run may still be going server-side -- not repeated, and do not start "
                    "the same ask again or it would run, and charge, twice") from None
            finished = self._poll_run(run_id, poll_interval=poll_interval, max_wait=max_wait)
            return self._fetch_dataset(finished, max_items=max_items)

    def _run_one(self, actor, payload, *, max_items, max_cost_usd, timeout, mode, poll_interval, max_wait):
        """One call, sync or async as the mode and the size of the ask say. mode "auto" (the default)
        uses sync only for a small ask (max_items at most SYNC_MAX_ITEMS) and async for anything
        bigger; "sync" and "async" force one way."""
        if mode == "sync" or (mode == "auto" and max_items <= SYNC_MAX_ITEMS):
            return self._run_sync_or_recover(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd,
                                             timeout=timeout, poll_interval=poll_interval, max_wait=max_wait)
        return self._run_async(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd,
                               poll_interval=poll_interval, max_wait=max_wait)

    def _split_payload(self, payload):
        """One payload per target: a payload naming several hashtags, profiles or search queries
        becomes one payload per target (each keeping the other fields, such as resultsPerPage).
        Anything else -- one target, no such list, or a payload that is not a dict -- is one call
        as it was. Meta Ads startUrls are never split: that scraper takes a list as one ask."""
        if not isinstance(payload, dict):
            return [payload]
        targets = [(key, item) for key in SPLIT_KEYS for item in (payload.get(key) or []) if isinstance(payload.get(key), list)]
        if len(targets) < 2:
            return [payload]
        rest = {k: v for k, v in payload.items() if k not in SPLIT_KEYS}
        return [{**rest, key: [item]} for key, item in targets]

    def _per_target_items(self, payload):
        """How many items one split call asks for: the payload's own per-target count
        (resultsPerPage / resultsLimit) when it names one, else the call's cap."""
        if isinstance(payload, dict):
            for key in ("resultsPerPage", "resultsLimit"):
                if isinstance(payload.get(key), (int, float)) and not isinstance(payload.get(key), bool) and payload[key] > 0:
                    return int(payload[key])
        return None

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280, mode="auto", poll_interval=5,
            max_wait=900, since=None, until=None):
        """Run an actor and return its items, at most max_items, within the max_cost_usd ceiling
        Apify enforces per call. mode: "auto" (sync for a small ask, async above SYNC_MAX_ITEMS),
        "sync" or "async". poll_interval is the seconds between status reads of an async run;
        max_wait is the longest an async run (or a 408 recovery) is waited for. since/until filter
        the rows by their posted date afterwards (the actor's own date filter cannot be trusted on
        hashtags). Raises ApifyRunUnfinished, never a second run, when a run has not finished."""
        if mode not in ("auto", "sync", "async"):
            raise ValueError(f"{self.label}: mode must be auto, sync or async, got {mode!r}")
        if poll_interval is not None and poll_interval < 0:
            raise ValueError(f"{self.label}: poll_interval must not be negative, got {poll_interval!r}")
        if max_wait is not None and max_wait <= 0:
            raise ValueError(f"{self.label}: max_wait must be positive, got {max_wait!r}")
        poll_interval = DEFAULT_POLL_INTERVAL if poll_interval is None else poll_interval
        max_wait = DEFAULT_MAX_WAIT if max_wait is None else max_wait
        self.forget_usage()                                                          # the call is about to spend money: the next reading must be a fresh one
        try:
            merged, seen_ids = [], set()
            for single in self._split_payload(payload):
                if len(merged) >= max_items:
                    break                                                      # the cap is reached: no more calls, no more spend
                per_target = self._per_target_items(single)
                take = max_items if per_target is None else min(max_items, per_target)
                got = self._run_one(actor, single, max_items=take, max_cost_usd=max_cost_usd, timeout=timeout,
                                    mode=mode, poll_interval=poll_interval, max_wait=max_wait)
                for item in got:
                    if len(merged) >= max_items:
                        break
                    ident = item.get("id") if isinstance(item, dict) else None
                    if ident is not None:
                        if ident in seen_ids:
                            continue                                         # the same video under two targets is kept once
                        seen_ids.add(ident)
                    merged.append(item)
        except ProviderError as exc:
            if "max-total-charge-usd-below-minimum" in str(exc):
                found = re.search(r"minimum of \$([0-9]+(?:\.[0-9]+)?)", str(exc))
                minimum = f"${found.group(1)}" if found else "a higher amount"
                raise ProviderConfigError(
                    f"{self.label}: the cost ceiling for this run (${max_cost_usd:g}) is below the minimum this actor accepts ({minimum}). "
                    f"Nothing was run and nothing was spent. Using this actor needs a ceiling of at least {minimum} per run (that is the most one run can cost); "
                    "raise it only if you approve that spending") from None
            raise
        return filter_by_create_time(merged, since, until)

    def usage(self):
        """What Apify itself says about this key's month: {'used': dollars spent, 'limit': dollars allowed, 'ends': the day the cycle ends} from its account limits endpoint, or None when it cannot be read (the platform then
        relies on its item counters alone). Nothing carries over: Apify's allowance is per monthly cycle, and what is not spent lapses."""
        self.usage_error = ""
        cached = getattr(self, "_usage_cache", None)
        if cached is not None and time.monotonic() - cached[0] < 20:                      # a reading younger than 20 seconds is reused (one call asks about it twice); a call that spent money forgets it
            return cached[1]
        try:
            body = self._call("GET", f"{self.base}/users/me/limits", headers={"Authorization": f"Bearer {self.api_key}"}, ok=(200,))
        except ProviderError as exc:
            self.usage_error = f"{exc.__class__.__name__}: {self._clean(str(exc))[:160]}"
            return None
        data = body.get("data", body) if isinstance(body, dict) else {}
        used, limit = (data.get("current") or {}).get("monthlyUsageUsd"), (data.get("limits") or {}).get("maxMonthlyUsageUsd")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (used, limit)):
            seen = sorted(f"{k}.{j}" if isinstance(v, dict) and v else str(k) for k, v in (data if isinstance(data, dict) else {}).items() for j in (v if isinstance(v, dict) and v else [None]))
            self.usage_error = "Apify answered, but its reply has no monthlyUsageUsd / maxMonthlyUsageUsd numbers (fields seen: " + ", ".join(s.replace(".None", "") for s in seen)[:160] + ")"
            return None
        reading = {"used": float(used), "limit": float(limit), "ends": str((data.get("monthlyUsageCycle") or {}).get("endAt") or "")[:10]}
        self._usage_cache = (time.monotonic(), reading)
        return reading

    def forget_usage(self):
        self._usage_cache = None


KEY_NAMES = ("APIFY_TOKEN", "APIFY_TOKEN_2", "APIFY_TOKEN_3", "APIFY_TOKEN_4")


class ApifyPool:
    """Several Apify keys, tried in order, for FAILOVER only: when a key is rejected (401/403) or has no credit left (402/429), the next key answers and the first is not asked again in this run. It is not a way to
    multiply the free allowance: the item allowances in research.yaml and the voice lane are counted for Apify as a whole, whichever key served a call. A refusal that another key cannot cure (our own cost ceiling,
    a timeout, a failed actor) is raised at once. A run that has not finished (ApifyRunUnfinished: a 408 with no run ID, or a poll that ran out of waiting time) is also raised at once and drops no key: the run may
    still be going server-side, so no other key starts it again. `events` says, in words, which key was dropped and why (never the key)."""
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"

    def __init__(self, tokens):
        self.clients = [Apify(t) for t in tokens]
        self.dropped = {}
        self.events = []

    def usages(self):
        """One usage reading per key (None where unreadable), in key order."""
        return [c.usage() if hasattr(c, "usage") else None for c in self.clients]

    def _note(self, text):
        if text not in self.events:                                                       # the events are the run's only record: each is said once
            self.events.append(text)

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280, **kwargs):
        last, short = None, False
        for i, client in enumerate(self.clients):
            if i in self.dropped:
                continue
            later = any(j > i and j not in self.dropped for j in range(len(self.clients)))
            reading = client.usage() if hasattr(client, "usage") else None
            floor = min(max_cost_usd, 0.25)                                               # Apify returns partial results up to what is left: only a key below the smallest ceiling we ever use is skipped
            if reading is not None and reading["limit"] - reading["used"] < floor:
                short = True                                                              # skipped for THIS call only, never dropped for the run: a later call re-reads it
                self._note(f"Apify key {i + 1} of {len(self.clients)} has ${max(0.0, reading['limit'] - reading['used']):.2f} left, less than the ${floor:g} the smallest call needs; " + ("the next key is used" if later else "no other key is left"))
                continue
            try:
                got = client.run(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd, **kwargs,
                                 **({} if timeout == 280 else {"timeout": timeout}))
                if hasattr(client, "forget_usage"):
                    client.forget_usage()                                                 # the call spent money: the next reading is a fresh one
                return got
            except (AuthError, QuotaError) as exc:
                self.dropped[i] = exc.__class__.__name__
                self._note(f"Apify key {i + 1} of {len(self.clients)} was refused ({exc.__class__.__name__}); " + ("the next key answers instead" if later else "no other key is left"))
                last = exc
            except ProviderConfigError:
                raise
            except ProviderError as exc:
                if "429" not in str(exc):                                                 # a plain 429 (after the provider's own retries) is a rate or credit limit: the next key may answer
                    raise
                self.dropped[i] = "RateLimited"
                self._note(f"Apify key {i + 1} of {len(self.clients)} was rate-limited (HTTP 429); " + ("the next key answers instead" if later else "no other key is left"))
                last = QuotaError(f"{self.label}: HTTP 429")
        if last is not None:
            raise type(last)(f"{self.label}: every key was refused ({'; '.join(self.events)})") from None
        if short:
            raise QuotaError(f"{self.label}: no key has enough money left ({'; '.join(self.events)})")
        raise AuthError(f"{self.label}: no key is available")


def build_apify(environ):
    """None with no key; the plain client with one; an ApifyPool with more (APIFY_TOKEN, APIFY_TOKEN_2, APIFY_TOKEN_3, APIFY_TOKEN_4: the same key twice counts once)."""
    tokens = list(dict.fromkeys(t for t in ((environ.get(n) or "").strip() for n in KEY_NAMES) if t))
    if not tokens:
        return None
    return Apify(tokens[0]) if len(tokens) == 1 else ApifyPool(tokens)


def budget_report(client):
    """(lines to print, dollars left in all keys or None). The remaining Apify money in this monthly cycle, per key and in total, as Apify itself reports it; every month is its own budget and nothing is carried forward."""
    readings = client.usages() if hasattr(client, "usages") else [client.usage() if hasattr(client, "usage") else None]
    known = [r for r in readings if r is not None]
    lines = []
    clients = getattr(client, "clients", [client])
    for n, r in enumerate(readings, 1):
        why = getattr(clients[n - 1], "usage_error", "") if n - 1 < len(clients) else ""
        lines.append(f"  key {n}: " + (f"${r['used']:.2f} of ${r['limit']:.2f} used" + (f", cycle ends {r['ends']}" if r["ends"] else "") if r is not None else "usage could not be read" + (f" ({why})" if why else "")))
    if not known:
        return ["Apify budget: Apify did not report the money left, so only the item counters guard the spending (the item allowances still apply):"] + lines, None
    left = sum(max(0.0, r["limit"] - r["used"]) for r in known)
    ends = {r["ends"] for r in known if r["ends"]}
    different = "; the keys' cycles end on different days (see each key), so this total is not one window" if len(ends) > 1 else ""
    return [f"Apify budget this cycle (each key's allowance is per monthly cycle and nothing carries forward): ${left:.2f} left of ${sum(r['limit'] for r in known):.2f} across {len(known)} key(s){different}"] + lines, left


def best_key_left(client):
    """Dollars left on the single key with the most (a call runs on ONE key, so this, not the total, is what a call can spend), or None when Apify does not say."""
    readings = client.usages() if hasattr(client, "usages") else [client.usage() if hasattr(client, "usage") else None]
    known = [max(0.0, r["limit"] - r["used"]) for r in readings if r is not None]
    return max(known) if known else None
