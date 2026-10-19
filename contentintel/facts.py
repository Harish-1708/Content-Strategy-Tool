"""Product facts: what a description states, what a person told us, what is missing.

Extraction is deliberately conservative: a fact is recorded only when the text
states it plainly. Anything not stated stays missing, so a later claim check can
refuse to write about it.
"""
import html as html_lib
import re

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_BLOCK_BREAK = re.compile(r"<\s*(?:br\s*/?|/p|/li|/div|/ul|/ol|/h[1-6]|/tr)\s*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.I | re.S)

_FIBRES = ("cotton", "polyester", "poly", "spandex", "elastane", "rayon", "viscose",
           "nylon", "wool", "linen", "bamboo", "modal", "lyocell", "acrylic",
           "polyamide", "silk", "hemp", "cashmere")
_FIBRE_ALT = "|".join(_FIBRES)
_PCT_AFTER = re.compile(
    r"(\d{1,3})\s*%\s*((?:[A-Za-z][A-Za-z-]*\s+){0,3}?)(" + _FIBRE_ALT + r")\b", re.I)
_PCT_BEFORE = re.compile(r"\b(" + _FIBRE_ALT + r")\s*\(\s*(\d{1,3})\s*%\s*\)", re.I)

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_POCKETS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+(?:[A-Za-z]+\s+)?pockets?\b",
    re.I)

_INGREDIENTS = re.compile(
    r"ingredients\s*:\s*(.*?)(?=\s*\.?\s*\bcontains?\b|\s*ingredients\s*:|$)", re.I | re.S)
_ALLERGENS = re.compile(r"\bcontains?\s*:?\s*([A-Za-z][A-Za-z ,&/-]{1,80}?)\s*(?:\.|$)", re.I | re.M)
_NUTRITION = re.compile(
    r"nutrition\s+facts|\bcalories\b|\b\d+(?:\.\d+)?\s*g\s+(?:of\s+)?protein\b|protein\s*[:\-]?\s*\d+(?:\.\d+)?\s*g\b",
    re.I)


def _clean(text):
    text = text.translate(_INVISIBLE).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def html_to_lines(body_html):
    """Plain-text lines from catalogue HTML (block tags become line breaks)."""
    raw = body_html or ""
    raw = _BLOCK_BREAK.sub("\n", raw)
    raw = _TAGS.sub(" ", raw)
    raw = html_lib.unescape(raw)
    lines = [_clean(line) for line in raw.split("\n")]
    return [line for line in lines if line]


def flatten(lines):
    return " ".join(lines)


def list_items(body_html):
    items = []
    for chunk in _LI.findall(body_html or ""):
        text = _clean(html_lib.unescape(_TAGS.sub(" ", chunk)))
        if text:
            items.append(text)
    return items


def _fact(value, source="catalogue_description", status="stated"):
    return {"value": value, "source": source, "status": status}


def extract_materials(text):
    """Fibre percentages, one entry per (fibre, percent); qualifiers are kept as a label."""
    found = {}
    for pct, qualifier, fibre in _PCT_AFTER.findall(text):
        label = (qualifier + fibre).strip()
        found.setdefault((fibre.lower(), int(pct)), label)
    for fibre, pct in _PCT_BEFORE.findall(text):
        found.setdefault((fibre.lower(), int(pct)), fibre)
    return [{"material": m, "percent": p, "label": label} for (m, p), label in found.items()]


def extract_pocket_claims(text):
    claims = []
    seen = set()
    for match in _POCKETS.finditer(text):
        token = match.group(1).lower()
        count = _NUMBER_WORDS.get(token)
        if count is None:
            count = int(token)
        phrase = match.group(0)
        key = (count, phrase.lower())
        if key not in seen:
            seen.add(key)
            claims.append({"count": count, "phrase": phrase})
    return claims


def extract_facts(body_html, schema, image_count=0):
    """Return {fact_name: {value, source, status}} for what the description states."""
    lines = html_to_lines(body_html)
    text = "\n".join(lines)
    facts = {}
    if lines:
        facts["description"] = _fact({"chars": len(flatten(lines))})
    if image_count:
        facts["images"] = _fact({"count": image_count}, source="catalogue_images")
    features = list_items(body_html)
    if features:
        facts["features"] = _fact(features)
    if schema == "apparel":
        items = extract_materials(text)
        if items:
            total = sum(i["percent"] for i in items)
            facts["materials"] = _fact({"items": items, "total": total, "consistent": total == 100})
        pockets = extract_pocket_claims(text)
        if pockets:
            facts["pocket_claims"] = _fact(pockets)
    elif schema == "food":
        lists = [m.strip(" .\n") for m in _INGREDIENTS.findall(text) if m.strip(" .\n")]
        if lists:
            facts["ingredients"] = _fact(lists)
        allergens = list(dict.fromkeys(a.strip() for a in _ALLERGENS.findall(text)))
        if allergens:
            facts["allergens"] = _fact(allergens)
        match = _NUTRITION.search(text)
        if match:
            facts["nutrition_facts"] = _fact(match.group(0))
    return facts


def merge_manual_facts(facts, manual):
    """Manual facts outrank the catalogue. Returns (facts, conflicts, family_override)."""
    merged = dict(facts)
    conflicts = []
    family = None
    for name, value in (manual or {}).items():
        if name == "family":
            family = str(value)
            continue
        if name in facts and name not in ("description", "images") and facts[name]["value"] != value:
            conflicts.append({"fact": name, "catalogue": facts[name]["value"], "manual": value})
        merged[name] = _fact(value, source="manual", status="stated_by_user")
    return merged, conflicts, family


def compute_readiness(facts, brand, status, excluded):
    missing_required = [n for n in brand.required_facts if n not in facts]
    missing_expected = [n for n in brand.expected_facts if n not in facts]
    if excluded:
        state = "excluded"
    elif status == "inactive":
        state = "inactive"
    elif missing_required:
        state = "needs_facts"
    else:
        state = "ready"
    return {"state": state, "missing_required": missing_required,
            "missing_expected": missing_expected}
