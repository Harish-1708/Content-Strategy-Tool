"""One sync run: fetch -> normalise -> completeness gate -> snapshot -> diff.

A run that fails the gate never replaces the last good snapshot and never touches
the product registry, so a truncated feed cannot cause false removals.
"""
import collections
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import diff as diff_mod
from .gate import GateResult, check_completeness
from .normalize import normalize_all
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
    snapshot_path: str = None
    pending_removals: list = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    needs_facts: list = field(default_factory=list)
    unknown_manual_handles: list = field(default_factory=list)
    fact_conflicts: list = field(default_factory=list)

    @property
    def generation_allowed(self):
        return self.state in ("ok", "warn")


def _readiness_summary(records):
    counts = collections.Counter(r["readiness"]["state"] for r in records.values())
    needs = sorted(r["title"] for r in records.values()
                   if r["readiness"]["state"] == "needs_facts")
    return dict(counts), needs


def _blocked(brand, gate, now, store, meta, count):
    latest = store.load_latest(brand.slug)
    store.log_failed(brand.slug, {
        "at": now, "count": count, "status": gate.status,
        "after_snapshot": latest["fetched_at"] if latest else None,
        "reasons": gate.reasons, "source": {k: v for k, v in meta.items() if k != "fetched_at"}})
    return SyncResult(brand=brand.slug, state="blocked", gate=gate, count=count)


def run_sync(brand, store, *, raw_products=None, fetch_meta=None, fetcher=None,
             sitemap_handles=None, now=None):
    """Run one sync for `brand`. Supply raw_products (e.g. from a saved file) or let it fetch."""
    now = now or utc_now()
    if raw_products is None:
        try:
            fetched = (fetcher or fetch_products)(brand.domain)
        except FetchError as exc:
            gate = GateResult(status="fail", count=0, reasons=[
                {"level": "fail", "code": "fetch_error", "message": str(exc)}])
            return _blocked(brand, gate, now, store, {"source": "live"}, 0)
        raw_products, fetch_meta = fetched.products, fetched.meta()
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

    snapshot = {
        "schema_version": SCHEMA_VERSION, "brand": brand.slug, "fetched_at": now,
        "source": meta, "count": len(records),
        "gate": {"status": gate.status, "reasons": gate.reasons}, "products": records,
    }
    path = store.save_snapshot(brand.slug, snapshot)
    readiness, needs = _readiness_summary(records)
    handles = {r["handle"] for r in records.values()}
    unknown = sorted(h for h in brand.manual_facts if h not in handles)
    conflicts = [{"product": r["title"], **c} for r in records.values()
                 for c in r["fact_conflicts"]]

    if gate.status == "unbaselined":
        store.save_pending(brand.slug, {"snapshot": str(path), "count": len(records),
                                        "created_at": now})
        return SyncResult(brand=brand.slug, state="needs_baseline", gate=gate,
                          count=len(records), snapshot_path=str(path), readiness=readiness,
                          needs_facts=needs, unknown_manual_handles=unknown,
                          fact_conflicts=conflicts)

    prev = latest["products"] if latest else {}
    events, registry = diff_mod.diff_snapshots(
        prev, records, store.load_registry(brand.slug), now, initial=False)
    store.save_registry(brand.slug, registry)
    store.promote_latest(brand.slug, snapshot)
    return SyncResult(
        brand=brand.slug, state="warn" if gate.status == "warn" else "ok", gate=gate,
        count=len(records), events=events, snapshot_path=str(path),
        pending_removals=diff_mod.pending_removals(registry), readiness=readiness,
        needs_facts=needs, unknown_manual_handles=unknown, fact_conflicts=conflicts)


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
    reasons = []
    latest = store.load_latest(slug)
    baseline = store.load_baseline(slug)
    if latest is None or baseline is None:
        reasons.append("No confirmed baseline and good snapshot yet."
                       if latest is None else "Baseline not confirmed.")
        return {"usable": False, "reasons": reasons, "snapshot_at": None}
    snap_at = latest["fetched_at"]
    usable = True
    failures = [f for f in store.read_failed(slug)
                if f.get("status") != "baseline_reset" and f.get("after_snapshot") == snap_at]
    if failures:
        usable = False
        reasons.append(f"The latest sync ({failures[-1]['at']}) failed; the last good "
                       f"snapshot is from {snap_at}.")
    age = (_parse(now) - _parse(snap_at)).total_seconds() / 3600
    if age > stale_after_hours:
        reasons.append(f"Snapshot is {age:.0f} hours old (limit {stale_after_hours}).")
    if latest["gate"]["status"] == "warn":
        reasons.append("The last accepted sync carried warnings; check the report.")
    return {"usable": usable, "reasons": reasons, "snapshot_at": snap_at,
            "snapshot_age_hours": round(age, 1)}


def _list_event(ev):
    detail = ev["details"]
    if ev["type"] == "changed":
        bits = sorted(detail)
        return f"changed: {', '.join(bits)}"
    if ev["type"] == "availability":
        if detail.get("product"):
            return detail["product"].replace("_", " ")
        return (f"{len(detail['variants_sold_out'])} variant(s) sold out, "
                f"{len(detail['variants_restocked'])} restocked")
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
                     f"with confirm-baseline before generation is allowed.")
    if result.state == "blocked":
        lines.append("Generation blocked. The previous good snapshot was kept.")
    if result.events:
        lines.append("Changes since the last good snapshot:")
        by_type = collections.Counter(e["type"] for e in result.events)
        lines.append("  " + ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(by_type.items())))
        for ev in result.events[:25]:
            lines.append(f"  - {ev['title']}: {_list_event(ev)}")
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
