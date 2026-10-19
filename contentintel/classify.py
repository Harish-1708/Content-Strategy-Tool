"""Classify a raw Shopify product using the brand's own configuration.

product_type is unreliable (DudeRobe leaves it blank on its main products and
Swoveralls files kids' products under 'Swoveralls'), so tags and titles are used
alongside it. All rules come from brands/<slug>/brand.yaml.
"""
import re

_DEFAULT_SUFFIXES = [r"\s*-\s*coming soon!?$", r"\s*\(coming soon\)$"]


def _matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def _tag_set(tags):
    return {t.strip().lower() for t in tags}


def _has_tag(wanted, tag_set):
    return any(w.strip().lower() in tag_set for w in wanted)


def is_excluded(title, product_type, tags, cfg):
    ex = cfg["exclude"]
    tag_set = _tag_set(tags)
    if product_type and product_type.strip().lower() in {t.lower() for t in ex["product_types"]}:
        return f"product type '{product_type}'"
    if _matches(ex["title_patterns"], title):
        return "title matches an exclusion pattern"
    if _has_tag(ex["tags"], tag_set):
        return "excluded tag"
    return None


def lifecycle_status(title, tags, cfg):
    tag_set = _tag_set(tags)
    for name in ("coming_soon", "pre_order"):
        rule = cfg["status_rules"].get(name)
        if rule and (_has_tag(rule["tags"], tag_set) or _matches(rule["title_patterns"], title)):
            return name
    active_tag = cfg.get("active_tag")
    if active_tag and active_tag.strip().lower() not in tag_set:
        return "inactive"
    return "active"


def product_flags(title, tags, variants, cfg):
    tag_set = _tag_set(tags)
    flags = []
    for name, rule in cfg["flags"].items():
        if _has_tag(rule["tags"], tag_set) or _matches(rule["title_patterns"], title):
            flags.append(name)
    if any(v.get("compare_at_price") and v["compare_at_price"] > v["price"] for v in variants):
        flags.append("on_sale")
    return sorted(set(flags))


def audience_hints(title, tags, cfg):
    hints = []
    tag_lookup = {k.strip().lower(): v for k, v in cfg["audience"]["tags"].items()}
    for tag in tags:
        hint = tag_lookup.get(tag.strip().lower())
        if hint and hint not in hints:
            hints.append(hint)
    for pattern, hint in cfg["audience"]["title_patterns"].items():
        if re.search(pattern, title, re.I) and hint not in hints:
            hints.append(hint)
    return hints


def family_key(title, cfg):
    """Group colour/print variants sold as separate products, e.g. 'UFC DudeRobe - Navy'."""
    clean = re.sub(r"\s+", " ", title).strip()
    suffixes = cfg.get("family", {}).get("strip_suffix_patterns", _DEFAULT_SUFFIXES)
    for pattern in suffixes:
        clean = re.sub(pattern, "", clean, flags=re.I).strip()
    if " - " in clean:
        clean = clean.rsplit(" - ", 1)[0].strip()
    return clean


def classify(title, product_type, tags, variants, cfg):
    reason = is_excluded(title, product_type, tags, cfg)
    status = "excluded" if reason else lifecycle_status(title, tags, cfg)
    return {
        "excluded": reason is not None,
        "excluded_reason": reason,
        "status": status,
        "flags": product_flags(title, tags, variants, cfg),
        "category": cfg["category_by_type"].get(product_type) if product_type else None,
        "audience_hints": audience_hints(title, tags, cfg),
        "family": family_key(title, cfg),
    }
