"""One sync run: fetch -> normalise -> completeness gate -> diff -> save.

A run that fails the gate never replaces the last good catalogue and never touches
the product registry, so a truncated feed cannot cause false removals.

Storage is deliberately frugal. A snapshot is kept only when real content changed;
a stock-only move updates latest.json; a run that finds nothing new writes only
last_checked.json (a few bytes). That keeps a daily schedule from bloating the repo.
"""
import collections
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import diff as diff_mod
from .export import write_exports
from .gate import GateResult, check_completeness
from .normalize import catalogue_fingerprint, normalize_all
from . import contract
from .shopify_fetch import FetchError, fetch_products

SCHEMA_VERSION = 1
STALE_AFTER_HOURS = 48


class BaselineError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class SyncResult:
    brand: str
    state: str                      # ok | warn | blocked | needs_baseline
    gate: GateResult
    count: int
    events: list = field(default_factory=list)
    snapshot_path: str = None       # None when nothing new needed a snapshot
    pending_removals: list = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    needs_facts: list = field(default_factory=list)
    unknown_manual_handles: list = field(default_factory=list)
    fact_conflicts: list = field(default_factory=list)
    wrote: list = field(default_factory=list)       # what changed on disk this run

    @property
    def generation_allowed(self):
        return self.state in ("ok", "warn")


def _readiness_summary(records):
    counts = collections.Counter(r["readiness"]["state"] for r in records.values())
    needs = sorted(r["title"] for r in records.values()
                   if r["readiness"]["state"] == "needs_facts")
    return dict(counts), needs


def _blocked(brand, gate, now, store, meta, count):
    last = store.load_last_checked(brand.slug)
    store.log_failed(brand.slug, {
        "at": now, "count": count, "status": gate.status,
        "after_run": last["run_id"] if last else None,
        "reasons": gate.reasons, "source": {k: v for k, v in meta.items() if k != "fetched_at"}})
    return SyncResult(brand=brand.slug, state="blocked", gate=gate, count=count)


def _make_snapshot(brand, now, meta, gate_status, reasons, records):
    return {"schema_version": SCHEMA_VERSION, "brand": brand.slug, "fetched_at": now,
            "source": meta, "count": len(records),
            "gate": {"status": gate_status, "reasons": reasons}, "products": records}


def _record_check(store, slug, run_at, count, gate_status):
    """Mark a good run. The unique run_id lets a later failure say which good run it followed,
    even if two runs share the same second."""
    store.save_last_checked(slug, {"run_at": run_at, "run_id": uuid.uuid4().hex[:12],
                                   "count": count, "gate_status": gate_status})


def run_sync(brand, store, *, raw_products=None, fetch_meta=None, fetcher=None,
             sitemap_handles=None, now=None, health=None):
    """Run one sync for `brand`. Supply raw_products (e.g. from a saved file) or let it fetch."""
    now = now or utc_now()
    if raw_products is None:
        try:
            fetched = (fetcher or fetch_products)(brand.domain)
        except FetchError as exc:
            if health is not None:
                err = contract.classify_exception(exc)
                health.observe("shopify_catalogue", "public_json", ok=False, code=err["code"], message=err["message"], now=now)
            gate = GateResult(status="fail", count=0, reasons=[
                {"level": "fail", "code": "fetch_error", "message": str(exc)}])
            return _blocked(brand, gate, now, store, {"source": "live"}, 0)
        raw_products, fetch_meta = fetched.products, fetched.meta()
        if health is not None:
            health.observe("shopify_catalogue", "public_json", ok=True, items=len(raw_products), now=now)
    meta = fetch_meta or {"source": "file", "terminal_empty_page": None}

    records, problems = normalize_all(raw_products, brand)
    latest = store.load_latest(brand.slug)
    baseline = store.load_baseline(brand.slug)
    gate = check_completeness(
        len(records), meta, cfg=brand.gate,
        reference_count=latest["count"] if latest else None,
        baseline_count=baseline["count"] if baseline else None,
        feed_handles=[r["handle"] for r in records.values()],
        sitemap_handles=sitemap_handles, problems=problems)

    if gate.status == "fail":
        return _blocked(brand, gate, now, store, meta, len(records))

    readiness, needs = _readiness_summary(records)
    handles = {r["handle"] for r in records.values()}
    unknown = sorted(h for h in brand.manual_facts if h not in handles)
    conflicts = [{"product": r["title"], **c} for r in records.values()
                 for c in r["fact_conflicts"]]
    common = dict(brand=brand.slug, count=len(records), readiness=readiness, needs_facts=needs,
                  unknown_manual_handles=unknown, fact_conflicts=conflicts)
    snapshot = _make_snapshot(brand, now, meta, gate.status, gate.reasons, records)

    if gate.status == "unbaselined":
        wrote = []
        pending = store.load_pending(brand.slug)
        same = None
        if pending:
            old = store.load_snapshot(pending["snapshot"])
            if old and (catalogue_fingerprint(old["products"], False)
                        == catalogue_fingerprint(records, False)):
                same = pending["snapshot"]
        if same:
            path = same                                     # nothing new to keep
        else:
            path = str(store.save_snapshot(brand.slug, snapshot))
            store.save_pending(brand.slug, {"snapshot": path, "count": len(records),
                                            "created_at": now})
            wrote = ["snapshot", "pending"]
        return SyncResult(state="needs_baseline", gate=gate, snapshot_path=path,
                          wrote=wrote, **common)

    prev = latest["products"] if latest else {}
    events, registry = diff_mod.diff_snapshots(
        prev, records, store.load_registry(brand.slug), now, initial=False)
    wrote = []
    path = None
    content_changed = catalogue_fingerprint(prev, False) != catalogue_fingerprint(records, False)
    stock_changed = catalogue_fingerprint(prev, True) != catalogue_fingerprint(records, True)
    if content_changed:
        path = str(store.save_snapshot(brand.slug, snapshot))
        wrote.append("snapshot")
    if content_changed or stock_changed:
        store.promote_latest(brand.slug, snapshot)
        wrote.append("latest")
    if store.save_registry(brand.slug, registry):
        wrote.append("registry")
    if store.append_changes(brand.slug, now, events):
        wrote.append("changes")
    wrote.extend(f"export:{name}" for name in write_exports(store, brand.slug, records))
    _record_check(store, brand.slug, now, len(records), gate.status)
    return SyncResult(
        state="warn" if gate.status == "warn" else "ok", gate=gate, events=events,
        snapshot_path=path, pending_removals=diff_mod.pending_removals(registry),
        wrote=wrote, **common)


def confirm_baseline(slug, store, confirmed_count, confirmed_by, now=None):
    """A person confirms the product count of the pending snapshot. Starts the baseline."""
    now = now or utc_now()
    if store.load_baseline(slug):
        raise BaselineError("A baseline is already confirmed. Reset it first, on purpose.")
    pending = store.load_pending(slug)
    if not pending:
        raise BaselineError("Nothing is waiting for confirmation. Run a sync first.")
    snapshot = store.load_snapshot(pending["snapshot"])
    if snapshot["count"] != int(confirmed_count):
        raise BaselineError(
            f"You confirmed {confirmed_count} products but the snapshot holds "
            f"{snapshot['count']}. Nothing was saved.")
    if not confirmed_by:
        raise BaselineError("Say who is confirming the baseline.")
    registry = store.load_registry(slug)
    prev = (store.load_latest(slug) or {}).get("products", {})
    events, registry = diff_mod.diff_snapshots(
        prev, snapshot["products"], registry, now, initial=not registry)
    store.save_registry(slug, registry)
    store.promote_latest(slug, snapshot)
    store.append_changes(slug, now, events)
    write_exports(store, slug, snapshot["products"])
    _record_check(store, slug, snapshot["fetched_at"], snapshot["count"], "pass")
    baseline = {"count": snapshot["count"], "confirmed_by": confirmed_by,
                "confirmed_at": now, "snapshot": pending["snapshot"]}
    store.save_baseline(slug, baseline)
    store.clear_pending(slug)
    return baseline, events


def reset_baseline(slug, store, by, now=None):
    """Deliberately retire the baseline. The next clean sync must be confirmed again."""
    now = now or utc_now()
    baseline = store.load_baseline(slug)
    if not baseline:
        raise BaselineError("There is no baseline to reset.")
    if not by:
        raise BaselineError("Say who is resetting the baseline.")
    store.log_failed(slug, {"at": now, "status": "baseline_reset", "by": by,
                            "previous_count": baseline["count"]})
    store.clear_baseline(slug)
    return baseline


def confirm_removed(slug, store, product_id, by, now=None):
    now = now or utc_now()
    if not by:
        raise ValueError("Say who is confirming the removal.")
    registry = diff_mod.confirm_removed(store.load_registry(slug), product_id, by, now)
    store.save_registry(slug, registry)
    return registry[str(product_id)]


def catalogue_state(slug, store, now=None, stale_after_hours=STALE_AFTER_HOURS):
    """Can generation use this brand's catalogue right now? Used by later phases."""
    now = now or utc_now()
    latest = store.load_latest(slug)
    baseline = store.load_baseline(slug)
    last = store.load_last_checked(slug)
    if latest is None or baseline is None or last is None:
        why = ("Baseline not confirmed." if latest is not None and baseline is None
               else "No confirmed baseline and good catalogue yet.")
        return {"usable": False, "reasons": [why], "last_good_run_at": None}
    run_at = last["run_at"]
    reasons = []
    usable = True
    failures = [f for f in store.read_failed(slug)
                if f.get("status") != "baseline_reset" and f.get("after_run") == last["run_id"]]
    if failures:
        usable = False
        reasons.append(f"The latest sync ({failures[-1]['at']}) failed; the last good "
                       f"sync ran at {run_at}.")
    age = (_parse(now) - _parse(run_at)).total_seconds() / 3600
    if age > stale_after_hours:
        reasons.append(f"The last good sync is {age:.0f} hours old (limit {stale_after_hours}).")
    if last.get("gate_status") == "warn":
        reasons.append("The last accepted sync carried warnings; check the report.")
    return {"usable": usable, "reasons": reasons, "last_good_run_at": run_at,
            "age_hours": round(age, 1), "product_count": last["count"]}


def _names(titles, limit=4):
    titles = sorted(titles)
    shown = ", ".join(titles[:limit])
    extra = len(titles) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def describe_event(ev):
    detail = ev["details"]
    if ev["type"] == "changed":
        return f"changed: {', '.join(sorted(detail))}"
    if ev["type"] == "availability":
        parts = []
        if detail["variants_sold_out"]:
            parts.append("Sold out: " + _names(detail["variants_sold_out"]))
        if detail["variants_restocked"]:
            parts.append("Back in stock: " + _names(detail["variants_restocked"]))
        text = "; ".join(parts)
        head = {"sold_out": "Product sold out", "back_in_stock": "Product back in stock"}.get(
            detail.get("product"))
        return f"{head}. {text}" if head else text
    if ev["type"] == "returned":
        return f"returned (was {detail['was']})"
    return ev["type"].replace("_", " ")


def format_report(result):
    g = result.gate
    lines = [f"Brand: {result.brand}", f"Catalogue sync: {result.state.upper()}",
             f"Products: {result.count} | Gate: {g.status}"]
    for r in g.reasons:
        lines.append(f"  [{r['level']}] {r['message']}")
    if result.state == "needs_baseline":
        lines.append(f"ACTION: a person must confirm the product count ({result.count}) "
                     f"(Actions > Catalogue admin > confirm-baseline) before generation is allowed.")
    if result.state == "blocked":
        lines.append("Generation blocked. The previous good catalogue was kept.")
    if result.state in ("ok", "warn"):
        lines.append("Saved: " + (", ".join(result.wrote) if result.wrote
                                  else "nothing new (no changes found)"))
    if result.events:
        lines.append("Changes since the last good sync:")
        by_type = collections.Counter(e["type"] for e in result.events)
        lines.append("  " + ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(by_type.items())))
        for ev in result.events[:25]:
            lines.append(f"  - {ev['title']}: {describe_event(ev)}")
        if len(result.events) > 25:
            lines.append(f"  ... and {len(result.events) - 25} more")
    if result.pending_removals:
        lines.append("Suspected removals awaiting confirmation:")
        for p in result.pending_removals:
            lines.append(f"  - {p['title']} (id {p['product_id']}, since {p['suspected_since']})")
    if result.readiness:
        lines.append("Product readiness: " + ", ".join(
            f"{n} {s.replace('_', ' ')}" for s, n in sorted(result.readiness.items())))
    for title in result.needs_facts:
        lines.append(f"  needs facts: {title}")
    for handle in result.unknown_manual_handles:
        lines.append(f"  WARNING: manual facts name an unknown product handle: {handle}")
    for c in result.fact_conflicts:
        lines.append(f"  WARNING: {c['product']}: manual '{c['fact']}' differs from the website")
    return "\n".join(lines)
