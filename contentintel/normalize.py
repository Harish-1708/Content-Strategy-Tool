"""Turn a raw Shopify product into one clean, hashed record.

The content hash covers what matters for creative work (title, copy, price, tags,
images, facts) and deliberately ignores updated_at: Shopify touches that field on
every product between fetches even when nothing changed.
"""
import hashlib
import json

from .classify import classify
from .facts import (compute_readiness, extract_facts, flatten, html_to_lines,
                    merge_manual_facts)


class NormalizeError(Exception):
    """A raw product is too malformed to use."""


def _tags(raw_tags):
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.split(",")
    return [t.strip() for t in (raw_tags or []) if str(t).strip()]


def _price(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NormalizeError(f"bad price {value!r}") from exc


def image_key(src):
    """Image identity without the CDN version query (?v=...) that changes on re-upload."""
    return (src or "").split("?", 1)[0]


def _variants(raw):
    names = [o.get("name") for o in raw.get("options") or []]
    out = []
    for v in raw.get("variants") or []:
        options = {}
        for idx, name in enumerate(names, start=1):
            value = v.get(f"option{idx}")
            if name and value is not None:
                options[name] = value
        out.append({
            "id": v["id"], "title": v.get("title"), "options": options,
            "sku": v.get("sku") or None, "price": _price(v.get("price")),
            "compare_at_price": _price(v.get("compare_at_price")),
            "available": bool(v.get("available")),
        })
    return out


def _images(raw):
    return [{"id": i.get("id"), "src": i.get("src"), "key": image_key(i.get("src")),
             "width": i.get("width"), "height": i.get("height"),
             "position": i.get("position"), "variant_ids": i.get("variant_ids") or []}
            for i in raw.get("images") or []]


def content_hash(record):
    payload = {
        "title": record["title"], "handle": record["handle"],
        "product_type": record["product_type"], "tags": sorted(record["tags"]),
        "body_text": record["body_text"],
        "variants": sorted(
            ([v["id"], v["title"], v["sku"], v["price"], v["compare_at_price"]]
             for v in record["variants"]), key=lambda x: x[0]),
        "images": sorted(([i["id"], i["key"], i["width"], i["height"]]
                          for i in record["images"]), key=lambda x: str(x[0])),
        "facts": {k: [f["value"], f["source"]] for k, f in sorted(record["facts"].items())},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_product(raw, brand):
    if not isinstance(raw, dict):
        raise NormalizeError("product is not an object")
    for key in ("id", "handle", "title"):
        if not raw.get(key):
            raise NormalizeError(f"product missing '{key}'")
    if not isinstance(raw.get("variants"), list):
        raise NormalizeError(f"product {raw['id']}: no variants list")

    tags = _tags(raw.get("tags"))
    variants = _variants(raw)
    images = _images(raw)
    title = raw["title"]
    product_type = raw.get("product_type") or ""
    info = classify(title, product_type, tags, variants, brand.catalogue)

    lines = html_to_lines(raw.get("body_html"))
    facts = extract_facts(raw.get("body_html"), brand.category_schema, len(images))
    facts, conflicts, family_override = merge_manual_facts(
        facts, brand.manual_facts.get(raw["handle"]))
    if family_override:
        info["family"] = family_override

    record = {
        "brand": brand.slug,
        "product_id": str(raw["id"]),
        "handle": raw["handle"],
        "title": title,
        "product_type": product_type,
        "vendor": raw.get("vendor"),
        "url": f"https://{brand.domain}/products/{raw['handle']}",
        "tags": tags,
        "published_at": raw.get("published_at"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "status": info["status"],
        "flags": info["flags"],
        "excluded": info["excluded"],
        "excluded_reason": info["excluded_reason"],
        "category": info["category"],
        "audience_hints": info["audience_hints"],
        "family": info["family"],
        "body_text": flatten(lines),
        "facts": facts,
        "fact_conflicts": conflicts,
        "variants": variants,
        "images": images,
        "in_stock": any(v["available"] for v in variants),
        "availability": {str(v["id"]): v["available"] for v in variants},
    }
    record["readiness"] = compute_readiness(facts, brand, info["status"], info["excluded"])
    record["content_hash"] = content_hash(record)
    return record


def normalize_all(raw_products, brand):
    """Return ({product_id: record}, problems). Duplicate ids are reported, first one wins."""
    records = {}
    problems = []
    for raw in raw_products:
        try:
            rec = normalize_product(raw, brand)
        except (NormalizeError, KeyError) as exc:
            ident = raw.get("id") if isinstance(raw, dict) else "?"
            problems.append({"product_id": str(ident), "problem": str(exc)})
            continue
        if rec["product_id"] in records:
            problems.append({"product_id": rec["product_id"], "problem": "duplicate id"})
            continue
        records[rec["product_id"]] = rec
    return records, problems
