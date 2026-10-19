"""What changed between two accepted snapshots.

Rules that matter
  * updated_at is never used: it changes on every product between fetches.
  * a product that disappears is only "suspected removed"; a person confirms.
  * a product that comes back is reported as "returned", with what changed meanwhile.
  * stock flips (sold out / back in stock) are reported apart from content changes.
"""
import copy


def _excerpt(old, new, width=70):
    i = 0
    limit = min(len(old), len(new))
    while i < limit and old[i] == new[i]:
        i += 1
    start = max(0, i - 20)
    return {"old": old[start:start + width], "new": new[start:start + width]}


def _variant_map(rec):
    return {v["id"]: v for v in rec["variants"]}


def diff_records(old, new):
    """Field-level differences between two versions of one product (or {} if none)."""
    out = {}
    for key in ("title", "handle", "product_type", "status"):
        if old.get(key) != new.get(key):
            out[key] = {"old": old.get(key), "new": new.get(key)}
    added = sorted(set(new["tags"]) - set(old["tags"]))
    removed = sorted(set(old["tags"]) - set(new["tags"]))
    if added or removed:
        out["tags"] = {"added": added, "removed": removed}
    fa = sorted(set(new["flags"]) - set(old["flags"]))
    fr = sorted(set(old["flags"]) - set(new["flags"]))
    if fa or fr:
        out["flags"] = {"added": fa, "removed": fr}
    if old["body_text"] != new["body_text"]:
        out["body_text"] = _excerpt(old["body_text"], new["body_text"])

    ov, nv = _variant_map(old), _variant_map(new)
    va = [nv[i]["title"] for i in nv if i not in ov]
    vr = [ov[i]["title"] for i in ov if i not in nv]
    if va or vr:
        out["variants"] = {"added": va, "removed": vr}
    prices, compare, skus = [], [], []
    for vid in ov.keys() & nv.keys():
        a, b = ov[vid], nv[vid]
        if a["price"] != b["price"]:
            prices.append({"variant": b["title"], "old": a["price"], "new": b["price"]})
        if a["compare_at_price"] != b["compare_at_price"]:
            compare.append({"variant": b["title"], "old": a["compare_at_price"],
                            "new": b["compare_at_price"]})
        if a["sku"] != b["sku"]:
            skus.append({"variant": b["title"], "old": a["sku"], "new": b["sku"]})
    if prices:
        out["price"] = prices
    if compare:
        out["compare_at_price"] = compare
    if skus:
        out["sku"] = skus

    oi = {i["key"]: i for i in old["images"]}
    ni = {i["key"]: i for i in new["images"]}
    ia, ir = sorted(set(ni) - set(oi)), sorted(set(oi) - set(ni))
    if ia or ir:
        out["images"] = {"added": len(ia), "removed": len(ir)}

    fa = sorted(set(new["facts"]) - set(old["facts"]))
    fr = sorted(set(old["facts"]) - set(new["facts"]))
    fc = sorted(k for k in set(old["facts"]) & set(new["facts"])
                if old["facts"][k]["value"] != new["facts"][k]["value"])
    if fa or fr or fc:
        out["facts"] = {"added": fa, "removed": fr, "changed": fc}

    if not out and old["content_hash"] != new["content_hash"]:
        out["unclassified"] = {"note": "content hash differs but no field difference was found"}
    return out


def diff_availability(old, new):
    """Stock flips for variants present in both versions (or {} if none)."""
    ov, nv = _variant_map(old), _variant_map(new)
    sold_out, restocked = [], []
    for vid in ov.keys() & nv.keys():
        if ov[vid]["available"] and not nv[vid]["available"]:
            sold_out.append(nv[vid]["title"])
        elif not ov[vid]["available"] and nv[vid]["available"]:
            restocked.append(nv[vid]["title"])
    if not sold_out and not restocked:
        return {}
    product = None
    if old["in_stock"] and not new["in_stock"]:
        product = "sold_out"
    elif not old["in_stock"] and new["in_stock"]:
        product = "back_in_stock"
    return {"product": product, "variants_sold_out": sorted(sold_out),
            "variants_restocked": sorted(restocked)}


def _event(kind, rec, details=None):
    return {"type": kind, "product_id": rec["product_id"], "title": rec["title"],
            "handle": rec["handle"], "details": details or {}}


def _entry(rec, now):
    return {"handle": rec["handle"], "title": rec["title"], "first_seen": now,
            "last_seen": now, "presence": "present"}


def diff_snapshots(prev_records, cur_records, registry, now, initial=False):
    """Return (events, new_registry). Inputs are not modified."""
    registry = copy.deepcopy(registry or {})
    events = []

    for pid, rec in cur_records.items():
        entry = registry.get(pid)
        if entry is None:
            registry[pid] = _entry(rec, now)
            if not initial:
                events.append(_event("new", rec))
            continue
        before = prev_records.get(pid) or entry.get("last_record")
        if entry["presence"] != "present":
            events.append(_event("returned", rec, {
                "was": entry["presence"],
                "changes": diff_records(before, rec) if before else {}}))
            entry["presence"] = "present"
            for key in ("suspected_since", "last_record", "confirmed_removed_at", "confirmed_by"):
                entry.pop(key, None)
        elif before is not None:
            if before["content_hash"] != rec["content_hash"]:
                events.append(_event("changed", rec, diff_records(before, rec)))
            stock = diff_availability(before, rec)
            if stock:
                events.append(_event("availability", rec, stock))
        entry.update({"handle": rec["handle"], "title": rec["title"], "last_seen": now})

    for pid, entry in registry.items():
        if pid in cur_records or entry["presence"] != "present":
            continue
        last = prev_records.get(pid)
        entry["presence"] = "suspected_removed"
        entry["suspected_since"] = now
        if last is not None:
            entry["last_record"] = last
        events.append({"type": "suspected_removed", "product_id": pid,
                       "title": entry["title"], "handle": entry["handle"], "details": {}})
    return events, registry


def confirm_removed(registry, product_id, by, now):
    """A person confirms a suspected removal. Returns the updated registry."""
    registry = copy.deepcopy(registry)
    entry = registry.get(str(product_id))
    if entry is None:
        raise KeyError(f"Unknown product id {product_id}")
    if entry["presence"] != "suspected_removed":
        raise ValueError(f"Product {product_id} is {entry['presence']}, not suspected_removed")
    entry["presence"] = "confirmed_removed"
    entry["confirmed_removed_at"] = now
    entry["confirmed_by"] = by
    return registry


def pending_removals(registry):
    return sorted(
        ({"product_id": pid, "title": e["title"], "suspected_since": e.get("suspected_since")}
         for pid, e in (registry or {}).items() if e["presence"] == "suspected_removed"),
        key=lambda x: x["product_id"])
