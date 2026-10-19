"""Clean, reusable exports of a brand's current catalogue.

Three files per brand, rewritten only when their content changes:
  products.json   one entry per product, with description and facts (for tools)
  products.csv    one row per product (for a sheet; opens straight in Google Sheets)
  variants.csv    one row per variant: size/colour options, price, stock

No timestamps are written into them, so identical data gives identical bytes and
git shows a diff only when something real changed. The time of the last check is
in last_checked.json.
"""
import csv
import io
import json

EXPORT_NAMES = ("products.json", "products.csv", "variants.csv")

PRODUCT_COLUMNS = [
    "product_id", "handle", "title", "url", "status", "flags", "excluded", "readiness",
    "missing_required", "category", "family", "audience_hints", "audience_stated",
    "in_stock", "price_min", "price_max", "on_sale", "variant_count", "image_count",
    "primary_image", "tags", "materials", "pocket_claims", "ingredients", "allergens",
]
VARIANT_COLUMNS = [
    "product_id", "product_title", "variant_id", "variant_title", "options", "sku",
    "price", "compare_at_price", "available",
]


def _join(items, sep="; "):
    return sep.join(str(i) for i in items)


def _money(value):
    return "" if value is None else f"{value:.2f}"


def _bool(value):
    return "true" if value else "false"


def _fact_value(rec, name):
    fact = rec["facts"].get(name)
    return fact["value"] if fact else None


def _materials(rec):
    value = _fact_value(rec, "materials")
    if not value:
        return ""
    return _join(f"{i['material']} {i['percent']}%" for i in value["items"])


def _pockets(rec):
    value = _fact_value(rec, "pocket_claims")
    if not value:
        return ""
    return _join(dict.fromkeys(c["count"] for c in value))


def _prices(rec):
    prices = [v["price"] for v in rec["variants"] if v["price"] is not None]
    return (min(prices), max(prices)) if prices else (None, None)


def _ordered(records):
    return sorted(records.values(), key=lambda r: (r["title"].lower(), r["product_id"]))


def _product_row(rec):
    low, high = _prices(rec)
    audience = _fact_value(rec, "audience")
    return {
        "product_id": rec["product_id"], "handle": rec["handle"], "title": rec["title"],
        "url": rec["url"], "status": rec["status"], "flags": _join(rec["flags"]),
        "excluded": _bool(rec["excluded"]), "readiness": rec["readiness"]["state"],
        "missing_required": _join(rec["readiness"]["missing_required"]),
        "category": rec["category"] or "", "family": rec["family"],
        "audience_hints": _join(rec["audience_hints"]),
        "audience_stated": audience if isinstance(audience, str) else "",
        "in_stock": _bool(rec["in_stock"]), "price_min": _money(low), "price_max": _money(high),
        "on_sale": _bool("on_sale" in rec["flags"]), "variant_count": len(rec["variants"]),
        "image_count": len(rec["images"]),
        "primary_image": rec["images"][0]["src"] if rec["images"] else "",
        "tags": _join(rec["tags"]), "materials": _materials(rec),
        "pocket_claims": _pockets(rec),
        "ingredients": _join(_fact_value(rec, "ingredients") or [], " || "),
        "allergens": _join(_fact_value(rec, "allergens") or []),
    }


def _csv(columns, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _options(rec, variant):
    """Size/colour options in the store's own order (kept in option_names)."""
    names = rec.get("option_names") or list(variant["options"])
    return [(n, variant["options"][n]) for n in names if n in variant["options"]]


def _variant_rows(records):
    for rec in _ordered(records):
        for v in rec["variants"]:
            yield {
                "product_id": rec["product_id"], "product_title": rec["title"],
                "variant_id": v["id"], "variant_title": v["title"],
                "options": _join(f"{k}={val}" for k, val in _options(rec, v)),
                "sku": v["sku"] or "", "price": _money(v["price"]),
                "compare_at_price": _money(v["compare_at_price"]),
                "available": _bool(v["available"]),
            }


def _product_json(rec):
    row = _product_row(rec)
    row.update({
        "flags": list(rec["flags"]), "excluded": rec["excluded"],
        "excluded_reason": rec["excluded_reason"],
        "audience_hints": list(rec["audience_hints"]),
        "missing_required": list(rec["readiness"]["missing_required"]),
        "missing_expected": list(rec["readiness"]["missing_expected"]),
        "in_stock": rec["in_stock"], "on_sale": "on_sale" in rec["flags"],
        "price_min": _prices(rec)[0], "price_max": _prices(rec)[1],
        "tags": list(rec["tags"]),
        "description": rec["body_text"],
        "facts": rec["facts"],
        "fact_conflicts": rec["fact_conflicts"],
        "images": [i["src"] for i in rec["images"]],
        "variants": [{"id": v["id"], "title": v["title"],
                      "options": [{"name": n, "value": val} for n, val in _options(rec, v)],
                      "sku": v["sku"], "price": v["price"],
                      "compare_at_price": v["compare_at_price"], "available": v["available"]}
                     for v in rec["variants"]],
    })
    return row


def build_exports(slug, records):
    """Return {file name: text} for one brand's records."""
    ordered = _ordered(records)
    doc = {"schema": 1, "brand": slug, "count": len(ordered),
           "products": [_product_json(r) for r in ordered]}
    return {
        "products.json": json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        "products.csv": _csv(PRODUCT_COLUMNS, [_product_row(r) for r in ordered]),
        "variants.csv": _csv(VARIANT_COLUMNS, _variant_rows(records)),
    }


def write_exports(store, slug, records):
    """Write the exports; return the names of the files whose content changed."""
    changed = []
    for name, text in build_exports(slug, records).items():
        if store.write_export(slug, name, text):
            changed.append(name)
    return changed
