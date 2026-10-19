"""Per-brief rules. Each brief carries its own set; nothing is shared between briefs.

Exact rules (banned characters, banned words, word order, product eligibility)
are checked by code here. Judgement rules (tone, visual direction) are kept as
text and handed to a human or a model reviewer later; code never pretends to
enforce them.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

import yaml


class RuleError(Exception):
    pass


_TOP = {"brief", "brand", "version", "source", "text_rules", "product_rules",
        "judgement_rules", "open_questions"}
_TEXT = {"banned_chars", "banned_phrases", "ordered_terms"}
_PRODUCT = {"block_flags", "allowed_statuses", "approved_products"}
_SEVERITIES = {"error", "warn"}


@dataclass(frozen=True)
class RuleSet:
    brief: str
    brand: str
    version: object
    rules: dict
    digest: str
    judgement_rules: tuple = ()
    open_questions: tuple = ()


@dataclass
class Violation:
    rule: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)


def _severity(item):
    sev = item.get("severity", "error")
    if sev not in _SEVERITIES:
        raise RuleError(f"severity must be one of {sorted(_SEVERITIES)}, got {sev!r}")
    return sev


def load_ruleset(path):
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    unknown = set(raw) - _TOP
    if unknown:
        raise RuleError(f"{path}: unknown keys {sorted(unknown)}")
    for key in ("brief", "brand", "version"):
        if raw.get(key) in (None, ""):
            raise RuleError(f"{path}: missing '{key}'")
    text = raw.get("text_rules") or {}
    if set(text) - _TEXT:
        raise RuleError(f"{path}: unknown text_rules {sorted(set(text) - _TEXT)}")
    product = raw.get("product_rules") or {}
    if set(product) - _PRODUCT:
        raise RuleError(f"{path}: unknown product_rules {sorted(set(product) - _PRODUCT)}")
    for item in text.get("banned_chars") or []:
        if not item.get("char"):
            raise RuleError(f"{path}: banned_chars entry without 'char'")
        _severity(item)
    for item in text.get("banned_phrases") or []:
        if not item.get("phrase"):
            raise RuleError(f"{path}: banned_phrases entry without 'phrase'")
        _severity(item)
    for item in text.get("ordered_terms") or []:
        if not item.get("first") or not item.get("then"):
            raise RuleError(f"{path}: ordered_terms entry needs 'first' and 'then'")
        if item.get("mode", "when_both_present") not in ("when_both_present", "strict"):
            raise RuleError(f"{path}: ordered_terms mode must be when_both_present or strict")
        _severity(item)
    rules = {"text_rules": text, "product_rules": product}
    blob = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return RuleSet(brief=raw["brief"], brand=raw["brand"], version=raw["version"],
                   rules=rules, digest=digest,
                   judgement_rules=tuple(raw.get("judgement_rules") or ()),
                   open_questions=tuple(raw.get("open_questions") or ()))


def _first_index(term, text):
    match = re.search(r"\b" + re.escape(term) + r"\b", text, re.I)
    return match.start() if match else -1


def check_text(ruleset, text, scope="copy", ordered_mode=None):
    """Exact text rules. `ordered_mode` forces 'strict' or 'when_both_present' for every
    ordered-terms rule, to compare readings of an ambiguous brief."""
    out = []
    rules = ruleset.rules["text_rules"]
    for item in rules.get("banned_chars") or []:
        count = text.count(item["char"])
        if count:
            name = item.get("name", item["char"])
            out.append(Violation("banned_char", _severity(item),
                                 f"Contains {name} ({count}x)", {"char": item["char"]}))
    for item in rules.get("banned_phrases") or []:
        if re.search(r"\b" + re.escape(item["phrase"]) + r"\b", text, re.I):
            out.append(Violation("banned_phrase", _severity(item),
                                 f"Contains banned word '{item['phrase']}'",
                                 {"phrase": item["phrase"]}))
    for item in rules.get("ordered_terms") or []:
        scopes = item.get("scopes") or []
        if scopes and scope not in scopes:
            continue
        mode = ordered_mode or item.get("mode", "when_both_present")
        i_first = _first_index(item["first"], text)
        i_then = _first_index(item["then"], text)
        if i_then == -1:
            continue
        broken = (i_first == -1 and mode == "strict") or (i_first != -1 and i_first > i_then)
        if broken:
            out.append(Violation(
                "ordered_terms", _severity(item),
                f"'{item['first']}' must appear before '{item['then']}'",
                {"first": item["first"], "then": item["then"], "mode": mode}))
    return out


def check_product(ruleset, record):
    """Is this product allowed under the brief? Returns a list of violations."""
    out = []
    rules = ruleset.rules["product_rules"]
    if record.get("excluded"):
        out.append(Violation("excluded_product", "error",
                             f"Product is excluded ({record.get('excluded_reason')})"))
    for flag in rules.get("block_flags") or []:
        if flag in record.get("flags", []):
            out.append(Violation("blocked_flag", "error",
                                 f"Product is flagged '{flag}', which this brief blocks",
                                 {"flag": flag}))
    allowed = rules.get("allowed_statuses")
    if allowed and record.get("status") not in allowed:
        out.append(Violation("status_not_allowed", "error",
                             f"Product status is '{record.get('status')}', allowed: {allowed}"))
    approved = rules.get("approved_products")
    if approved and record.get("handle") not in approved:
        out.append(Violation("not_approved_product", "error",
                             "Product is not on this brief's approved list",
                             {"handle": record.get("handle")}))
    return out
