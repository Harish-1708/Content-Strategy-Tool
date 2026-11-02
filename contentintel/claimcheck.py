"""Does a brief claim anything the brand cannot back up?

A brief goes to creators who will say its words on camera. A price that is not the price, an offer nobody approved, "20g of protein" on a snack that does
not say so, "gluten free" with no such statement, "as seen on" a programme the brand was never on: each is a real-world problem, and the prompt's request
"do not invent claims" is a hope, not a check. This module is the check. Like the other rules it is code, not opinion: it looks for the kinds of statement
that can be wrong, and for each one asks whether it is supported.

A claim is supported only by:
  - the catalogue: the product's title, description, tags, extracted facts (materials, pocket counts) and its real prices;
  - the brand's approved-claims register (brands/<brand>/claims.yaml), where a claim has a status;
  - the brief's own `claim_rules` (allowed_offers, allowed_claims) in its rules file.
Anything else is a violation: an error for something that can be factually wrong or can cause harm (a price, an offer, a number, a dietary or health claim,
social proof, an unregistered endorsement), a warning for an unprovable boast ("best-selling"). A brief may switch a rule off with `claim_rules.skip`.
"""
import re

from .facts import _FIBRES, _NUMBER_WORDS
from .rules import Violation

RULES = ("price_not_in_catalogue", "offer_not_provided", "material_claim_unsupported", "count_claim_unsupported", "nutrition_claim_unsupported",
         "dietary_claim_unsupported", "health_claim_prohibited", "social_proof_unverified", "result_claim_unproven", "unregistered_claim",
         "claim_status", "superlative_unproven")

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212-"), " ")


def norm(text):
    """Lower case, dashes and punctuation as spaces, one space between words: so 'Gluten-Free', 'gluten free' and 'GLUTEN  FREE.' are the same."""
    text = str(text or "").translate(_DASHES).replace("\xa0", " ").lower()
    text = re.sub(r"[^\w%$.#+&/ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .")


# ---- what the catalogue supports -----------------------------------------------------------------------------------------------------------------------

_FIBRE_NAME = {"poly": "polyester", "elastane": "spandex", "viscose": "rayon"}


def _fibre(name):
    name = str(name or "").lower().strip()
    return _FIBRE_NAME.get(name, name)


class Evidence:
    """What one product (or, for a claim about no particular product, every allowed product) supports."""

    def __init__(self):
        self.text = ""                  # normalised title, description and tags
        self.prices = set()
        self.materials = set()          # {(fibre, percent)}
        self.has_materials = False
        self.pockets = set()
        self.has_pockets = False

    def absorb(self, record):
        facts = record.get("facts") or {}
        parts = [record.get("title"), record.get("body_text"), " ".join(str(t) for t in record.get("tags") or []), record.get("product_type")]
        self.text += " " + norm(" ".join(str(p or "") for p in parts))
        for variant in record.get("variants") or []:
            for key in ("price", "compare_at_price"):
                value = variant.get(key) if isinstance(variant, dict) else None
                try:
                    if value is not None and float(value) > 0:
                        self.prices.add(round(float(value), 2))
                except (TypeError, ValueError):
                    pass
        value = (facts.get("materials") or {}).get("value") if isinstance(facts.get("materials"), dict) else None
        for entry in (value or {}).get("items") or [] if isinstance(value, dict) else []:
            if isinstance(entry, dict) and entry.get("percent") is not None:
                self.has_materials = True
                self.materials.add((_fibre(entry.get("material")), int(entry["percent"])))
        pockets = (facts.get("pocket_claims") or {}).get("value") if isinstance(facts.get("pocket_claims"), dict) else None
        for entry in pockets or []:
            if isinstance(entry, dict) and str(entry.get("count", "")).isdigit():
                self.has_pockets = True
                self.pockets.add(int(entry["count"]))
        return self


def evidence_for(records):
    ev = Evidence()
    for record in records:
        ev.absorb(record)
    return ev


# ---- what to look for ---------------------------------------------------------------------------------------------------------------------------------------

_MONEY = re.compile(r"(?<![\w.])(?:us\$|usd\s?|\$|\u20ac|\u00a3|\u20b9|rs\.?\s?)\s?(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)(?!\d)|"
                    r"(?<![\w.])(\d{1,6}(?:\.\d{1,2})?)\s?(?:dollars|bucks|usd|euros|pounds|rupees)\b", re.I)
_OFFERS = re.compile(
    r"\b\d{1,3}\s*%\s*off\b|\bsave\s+(?:up to\s+)?\$?\d+\s*%?|\b(?:buy|get)\s+(?:one|1|two|2|three|3)\s+(?:get|free)\b|\bbogo\b|\bfree\s+(?:shipping|delivery|returns|gift)\b|"
    r"\b(?:discount|promo|coupon|voucher|referral)\s+code\b|\buse\s+code\b|\bcoupon\b|\b(?:flash|final|clearance|holiday|summer|winter)\s+sale\b|\bon\s+sale\b|"
    r"\blimited[- ]time\s+(?:offer|deal|discount|price)\b|(?<![\w$])\$\d+(?:\.\d\d)?\s+off\b|\bmoney[- ]back\s+guarantee\b|\bfree\s+trial\b", re.I)
_MATERIAL = re.compile(r"(\d{1,3})\s*%\s*((?:[A-Za-z][A-Za-z-]*\s+){0,2}?)(" + "|".join(_FIBRES) + r")\b", re.I)
_POCKETS = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+(?:[A-Za-z]+\s+)?pockets?\b", re.I)
_NUTRITION = re.compile(r"(\d+(?:\.\d+)?)\s*(g|mg|grams?)\s*(?:of\s+)?(protein|sugars?|fib(?:er|re)|fat|carbs?|carbohydrates?|sodium|caffeine)\b|"
                        r"(\d+)\s*(calories|kcal)\b", re.I)
_DIETARY = tuple((re.compile(p, re.I), label) for p, label in (
    (r"\bvegan\b", "vegan"), (r"\bvegetarian\b", "vegetarian"), (r"\bgluten[\s-]*free\b", "gluten free"), (r"\bdairy[\s-]*free\b", "dairy free"),
    (r"\bnut[\s-]*free\b", "nut free"), (r"\bsoy[\s-]*free\b", "soy free"), (r"\begg[\s-]*free\b", "egg free"), (r"\bnon[\s-]*gmo\b", "non GMO"),
    (r"\borganic\b", "organic"), (r"\bkosher\b", "kosher"), (r"\bhalal\b", "halal"), (r"\bpaleo\b", "paleo"), (r"\bketo(?:genic)?\b", "keto"),
    (r"\bplant[\s-]*based\b", "plant based"), (r"\bsugar[\s-]*free\b|\bzero\s+(?:added\s+)?sugar\b|\bno\s+(?:added\s+)?sugar\b", "sugar free"),
    (r"\blow[\s-]*carb\b", "low carb"), (r"\blow[\s-]*calorie\b", "low calorie"), (r"\bhigh[\s-]*protein\b", "high protein"),
    (r"\ballergen[\s-]*free\b", "allergen free"), (r"\bpreservative[\s-]*free\b|\bno\s+preservatives\b", "preservative free")))
_HEALTH = tuple((re.compile(p, re.I), label) for p, label in (
    (r"\bcures?\b", "cures"), (r"\btreats?\s+(?:acne|anxiety|depression|pain|eczema|[a-z]+itis)\b", "treats"), (r"\bprevents?\s+(?:disease|cancer|illness|colds?|infection)", "prevents"),
    (r"\bheals?\b", "heals"), (r"\bdetox(?:es|ify)?\b|\bcleanses?\b", "detox"), (r"\bboosts?\s+(?:your\s+)?(?:immun\w*|metabolism|energy|mood)", "boosts"),
    (r"\banti[\s-]*inflammatory\b", "anti-inflammatory"), (r"\b(?:lose|losing)\s+weight\b|\bweight\s+loss\b|\bburns?\s+fat\b", "weight loss"),
    (r"\bclinically\s+(?:proven|tested|shown)\b", "clinically proven"), (r"\bdoctor[\s-]*(?:recommended|approved)\b|\bdermatologist[\s-]*(?:recommended|approved|tested)\b", "doctor approved"),
    (r"\b(?:reduces?|relieves?)\s+(?:stress|anxiety|pain|inflammation)\b", "relieves"), (r"\bimproves?\s+(?:sleep|focus|digestion|gut health)\b", "improves"),
    (r"\bfda[\s-]*(?:approved|cleared)\b", "FDA approved")))
_SOCIAL = re.compile(
    r"(?<![\w,])(?:\d{1,3}(?:,\d{3})+|\d{3,})\+?\s*(?:happy\s+)?(?:customers|reviews|people|fans|parents|moms|dads|orders|units|sold|five[\s-]*star|5[\s-]*star)\b|\brated\s+\d(?:\.\d)?\b|"
    r"\b\d(?:\.\d)?\s*(?:stars|/\s*5)\b|\b(?:5|five)[\s-]*star\b|\b(?:thousands|millions|hundreds)\s+of\s+(?:customers|people|reviews|fans|parents|happy)\b", re.I)
_RESULT = re.compile(r"\bresults?\s+(?:in|within)\b|\b(?:in|within)\s+(?:just\s+)?(?:\d+|a\s+few|one|two|three|four|five|six|seven|ten)\s+(?:days?|weeks?|months?)\b", re.I)
_ENDORSE = re.compile(r"\bas\s+seen\s+(?:on|in)\s+[A-Z][\w&' ]{1,40}|\bfeatured\s+(?:on|in)\s+[A-Z][\w&' ]{1,40}|\b(?:recommended|approved|endorsed|certified|trusted)\s+by\s+[\w&' ]{2,40}|"
                      r"\b(?:voted|ranked)\s+(?:#?\d|best|top)[\w ]{0,30}", re.I)
_SUPERLATIVE = re.compile(r"#\s?1\b|\bnumber\s+one\b|\bbest[\s-]*sell(?:ing|er)\b|\bworld'?s\s+(?:best|first|most|#1)\b|\baward[\s-]*winning\b|\bmost\s+(?:comfortable|popular|loved|trusted)\b|"
                          r"\bthe\s+best\s+(?:\w+\s+){0,2}(?:ever|in\s+the\s+world)\b", re.I)

_HEAD = {"price_not_in_catalogue": "price", "offer_not_provided": "offer", "material_claim_unsupported": "material claim", "count_claim_unsupported": "count",
         "nutrition_claim_unsupported": "nutrition claim", "dietary_claim_unsupported": "dietary claim", "health_claim_prohibited": "health claim",
         "social_proof_unverified": "social proof", "result_claim_unproven": "result claim", "unregistered_claim": "endorsement or ranking",
         "superlative_unproven": "boast"}


def _spans(text, pattern):
    return [(m.group(0), m) for m in pattern.finditer(text or "")]


def _allowed(match_text, allowed):
    """Is this wording inside something the brief explicitly allows (or the other way round)?"""
    m = norm(match_text)
    return any(a and (m in a or a in m) for a in allowed)


def _within(evidence_text, phrase):
    return norm(phrase) in evidence_text


def check_text(text, evidence, claims=(), claim_rules=None):
    """Every unsupported claim in `text`, as rules.Violation objects. `evidence` is an Evidence; `claims` the brand's register; `claim_rules` the brief's."""
    rules = claim_rules or {}
    off = set(rules.get("skip") or ())
    offers = [norm(o) for o in rules.get("allowed_offers") or []]
    allowed = [norm(c) for c in rules.get("allowed_claims") or []] + offers
    register = [(c, norm(c.get("text"))) for c in claims if isinstance(c, dict) and c.get("text")]
    allowed += [t for c, t in register if str(c.get("status", "")).lower() in ("licensed", "approved", "active")]
    out = []

    def flag(rule, severity, message, match):
        if rule not in off and not _allowed(match, allowed):
            out.append(Violation(rule, severity, message, {"match": match}))

    seen_offer_spans = []
    for phrase, m in _spans(text, _OFFERS):
        seen_offer_spans.append(m.span())
        flag("offer_not_provided", "error", f"the offer '{phrase.strip()}' was not provided: state no offer, discount or guarantee unless the brief allows it", phrase)
    for phrase, m in _spans(text, _MONEY):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in seen_offer_spans):
            continue                                                     # "$10 off" is an offer, already reported
        amount = float((m.group(1) or m.group(2)).replace(",", ""))
        if round(amount, 2) in evidence.prices or any(f"{amount:g}" in a or f"{amount:.2f}" in a for a in allowed):
            continue
        shown = ", ".join(f"${p:g}" for p in sorted(evidence.prices)[:6]) or "none known"
        flag("price_not_in_catalogue", "error", f"the price '{phrase.strip()}' is not a catalogue price (catalogue prices: {shown}); use a real price or none", phrase)
    for phrase, m in _spans(text, _MATERIAL):
        percent, fibre = int(m.group(1)), _fibre(m.group(3))
        if (fibre, percent) in evidence.materials:
            continue
        why = "the catalogue does not state this product's materials" if not evidence.has_materials else \
            "the catalogue says " + ", ".join(f"{p}% {f}" for f, p in sorted(evidence.materials))
        flag("material_claim_unsupported", "error", f"'{phrase.strip()}' is not supported: {why}", phrase)
    for phrase, m in _spans(text, _POCKETS):
        word = m.group(1).lower()
        count = _NUMBER_WORDS.get(word, int(word) if word.isdigit() else None)
        if count in evidence.pockets or (count is not None and re.search(rf"\b{count}\s+(?:\w+\s+)?pockets?\b|\b{word}\s+(?:\w+\s+)?pockets?\b", evidence.text)):
            continue
        why = "the catalogue does not state the number of pockets" if not evidence.has_pockets else \
            "the catalogue says " + ", ".join(str(c) for c in sorted(evidence.pockets))
        flag("count_claim_unsupported", "error", f"'{phrase.strip()}' is not supported: {why}", phrase)
    for phrase, m in _spans(text, _NUTRITION):
        number = m.group(1) or m.group(4)
        nutrient = (m.group(3) or m.group(5) or "").lower().rstrip("s")
        nutrient = "fib(?:er|re)" if nutrient.startswith("fib") else re.escape(nutrient)
        near = rf"\b{re.escape(number)}\s*(?:g|mg|grams?)?\b[^.]{{0,40}}\b{nutrient}|\b{nutrient}\w*\b[^.]{{0,25}}\b{re.escape(number)}\b"
        if re.search(near, evidence.text):
            continue
        flag("nutrition_claim_unsupported", "error", f"'{phrase.strip()}' is not stated in the catalogue; say no nutrition figure unless the product states it", phrase)
    for pattern, label in _DIETARY:
        for m in pattern.finditer(text or ""):
            if pattern.search(evidence.text):
                break
            flag("dietary_claim_unsupported", "error", f"'{m.group(0).strip()}' ({label}) is not stated in the catalogue; do not claim it", m.group(0))
            break
    for pattern, label in _HEALTH:
        for m in pattern.finditer(text or ""):
            if pattern.search(evidence.text):
                break
            flag("health_claim_prohibited", "error", f"'{m.group(0).strip()}' is a health claim the catalogue does not make; remove it", m.group(0))
            break
    for phrase, m in _spans(text, _SOCIAL):
        if _within(evidence.text, phrase):
            continue
        flag("social_proof_unverified", "error", f"'{phrase.strip()}' is social proof nobody has verified; remove it or use a figure the brand provides", phrase)
    for phrase, m in _spans(text, _RESULT):
        if _within(evidence.text, phrase):
            continue
        flag("result_claim_unproven", "error", f"'{phrase.strip()}' promises a result or a time the catalogue does not support", phrase)
    registered = [t for _, t in register]
    for phrase, m in _spans(text, _ENDORSE):
        if any(t and t in norm(phrase) for t in registered) or _within(evidence.text, phrase):
            continue
        flag("unregistered_claim", "error", f"'{phrase.strip()}' is an endorsement or ranking that is not in the brand's approved claims", phrase)
    for phrase, m in _spans(text, _SUPERLATIVE):
        if _within(evidence.text, phrase) or any(t and t in norm(phrase) for t in registered):
            continue
        flag("superlative_unproven", "warn", f"'{phrase.strip()}' is a boast the catalogue does not back; keep it only if you can prove it", phrase)
    low = norm(text)
    for claim, claim_text in register:
        if claim_text and claim_text in low:
            status = str(claim.get("status", "")).lower()
            label = claim.get("id") or claim.get("text")
            if status in ("prohibited", "expired", "withdrawn", "revoked", "denied"):
                flag("claim_status", "error", f"the claim '{claim.get('text')}' ({label}) is {status} and must not be used", claim_text)
            elif str(claim.get("guidelines", "")).lower() in ("pending", "missing", "none", "") and status in ("licensed", "approved", "active"):
                if "claim_status" not in off:
                    out.append(Violation("claim_status", "warn", f"the claim '{claim.get('text')}' ({label}) is licensed but its usage guidelines have not been received; "
                                                                  "check them before using it", {"match": claim.get("text")}))
    return out
