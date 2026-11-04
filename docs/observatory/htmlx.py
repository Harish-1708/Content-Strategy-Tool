"""Reading a web page the way a person would: what is visible, what the headlines say, what offers are shown.

Hidden elements, scripts, styles and comments never count (the same rules as safety.visible_text). Everything returned is cleaned
of invisible characters, so text that is later shown to a model has had its hidden instructions removed first.
"""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..safety import _BLOCKS, _HIDDEN_STYLE, _SKIP_TAGS, _VOID, clean_text

_ANNOUNCE = re.compile(r"announce|promo|top-?bar|topbar|marquee|ticker|notice-bar|header-?message|site-?banner|promo-?bar|banner-?top|sale-?bar|shipping-?bar",
                       re.I)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
_LD_TYPES = ("Product", "Offer", "AggregateOffer", "ItemList", "Article", "BlogPosting", "FAQPage", "Organization")


class PageParser(HTMLParser):
    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.parts, self.stack = [], []              # stack: [tag, hides, announce_depth_marker]
        self.title_parts, self.in_title = [], False
        self.meta = {}
        self.canonical = ""
        self.headings, self._heading = [], None
        self.links, self._link = [], None
        self.alternates = []
        self.announcements, self._announce = [], None
        self.jsonld, self._ld = [], None
        self.images = 0

    # ---- helpers ---------------------------------------------------------------------------------------------------------------------------

    def _hidden_now(self):
        return any(entry[1] for entry in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:title", "og:description", "og:type", "og:image", "twitter:title", "robots") and a.get("content"):
                self.meta.setdefault(key, clean_text(a["content"]).strip())
        if tag == "link":
            rel, href = a.get("rel", "").lower(), a.get("href", "")
            if "canonical" in rel and href and not self.canonical:
                self.canonical = urljoin(self.base, href)
            if "alternate" in rel and href and any(t in a.get("type", "").lower() for t in ("rss", "atom", "xml", "json")):
                self.alternates.append({"href": urljoin(self.base, href), "type": a.get("type", ""), "title": clean_text(a.get("title", "")).strip()})
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld = []
        if tag == "img" and not self._hidden_now():
            self.images += 1
        if tag in _VOID:
            if tag in _BLOCKS and not self._hidden_now():
                self.parts.append("\n")
            return
        hides = (tag in _SKIP_TAGS or "hidden" in a or a.get("aria-hidden", "").lower() == "true" or bool(_HIDDEN_STYLE.search(a.get("style", ""))))
        marker = None
        label = f"{a.get('class', '')} {a.get('id', '')} {a.get('role', '')}"
        if not hides and self._announce is None and _ANNOUNCE.search(label):
            marker = self._announce = {"tag": tag, "depth": len(self.stack), "parts": []}
        self.stack.append([tag, hides, marker])
        if not self._hidden_now():
            if tag in _HEADINGS:
                self._heading = {"level": _HEADINGS[tag], "parts": []}
                self.parts.append("\n" + "#" * _HEADINGS[tag] + " ")
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag in _BLOCKS:
                self.parts.append("\n")
            if tag == "a" and a.get("href"):
                self._link = {"href": urljoin(self.base, a["href"]), "parts": []}

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self._ld is not None:
            raw = "".join(self._ld).strip()
            self._ld = None
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if data is not None and len(self.jsonld) < 40:
                self.jsonld.append(data)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                closing = self.stack[i:]
                del self.stack[i:]
                for entry in closing:
                    if entry[2] is not None and entry[2] is self._announce:
                        text = " ".join(clean_text("".join(entry[2]["parts"])).split())
                        if text and text not in self.announcements and len(self.announcements) < 6:
                            self.announcements.append(text[:300])
                        self._announce = None
                break
        if not self._hidden_now():
            if tag in _HEADINGS and self._heading:
                text = " ".join(clean_text("".join(self._heading["parts"])).split())
                if text and len(self.headings) < 60:
                    self.headings.append({"level": self._heading["level"], "text": text[:200]})
                self._heading = None
            if tag == "a" and self._link:
                text = " ".join(clean_text("".join(self._link["parts"])).split())
                if len(self.links) < 400:
                    self.links.append({"href": self._link["href"], "text": text[:120]})
                self._link = None
            if tag in _BLOCKS:
                self.parts.append("\n")

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self.in_title:
            self.title_parts.append(data)
        if self._hidden_now():
            return
        self.parts.append(data)
        if self._heading is not None:
            self._heading["parts"].append(data)
        if self._link is not None:
            self._link["parts"].append(data)
        if self._announce is not None:
            self._announce["parts"].append(data)

    def handle_comment(self, data):
        return


def parse_page(html, base_url="", max_chars=20000):
    """What a page says: title, description, headlines, announcement bars, links, feeds it advertises, structured data and its visible text."""
    parser = PageParser(base_url)
    parser.feed(str(html or "")[:2_000_000])
    parser.close()
    text = clean_text("".join(parser.parts))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return {"title": " ".join(clean_text("".join(parser.title_parts)).split())[:200], "description": parser.meta.get("description", "")[:400],
            "canonical": parser.canonical, "meta": parser.meta, "headings": parser.headings, "h1": [h["text"] for h in parser.headings if h["level"] == 1],
            "announcements": parser.announcements, "links": parser.links, "alternates": parser.alternates, "jsonld": parser.jsonld,
            "images": parser.images, "text": text[:max_chars], "text_truncated": len(text) > max_chars, "chars": len(text)}


# ---- offers ----------------------------------------------------------------------------------------------------------------------------------

_OFFER_PATTERNS = (
    ("percent_off", re.compile(r"\b(?:up to\s*)?(\d{1,2})\s?%\s*(?:off|discount|savings?)\b|\bsave\s*(?:up to\s*)?(\d{1,2})\s?%", re.I)),
    ("amount_off", re.compile(r"\$\s?(\d{1,4}(?:\.\d\d)?)\s*(?:off|discount)\b|\bsave\s*\$\s?(\d{1,4})\b", re.I)),
    ("free_shipping", re.compile(r"\bfree\s+(?:standard\s+|us\s+|domestic\s+|express\s+)?shipping(?:\s+on\s+orders?\s+(?:over|above|of)\s+\$?\s?\d+)?", re.I)),
    ("code", re.compile(r"\b(?:use|enter|apply|with)?\s*(?:promo\s+|discount\s+|coupon\s+)?code[:\s]+\"?([A-Z0-9]{3,20})\"?\b")),
    ("bundle", re.compile(r"\bbuy\s*(?:\d+|one|two|three)\b[^.\n]{0,40}\bget\b[^.\n]{0,30}|\bBOGO\b|\bbundle\s*(?:and|&)\s*save\b", re.I)),
    ("event", re.compile(r"\b(black friday|cyber monday|boxing day|memorial day|labor day|holiday sale|summer sale|winter sale|flash sale|clearance|limited[- ]time|"
                         r"sitewide sale|launch|new arrival|back in stock|pre-?order|final sale|last chance)\b", re.I)),
)


def extract_offers(*texts):
    """The offers a set of page texts shows, as short normalised phrases: '20% off', 'free shipping', 'code SAVE20', 'black friday'."""
    found = set()
    for text in texts:
        text = clean_text(text)
        for kind, pattern in _OFFER_PATTERNS:
            for match in pattern.finditer(text):
                groups = [g for g in match.groups() if g]
                if kind == "percent_off":
                    found.add(f"{groups[0]}% off")
                elif kind == "amount_off":
                    found.add(f"${groups[0]} off")
                elif kind == "free_shipping":
                    found.add(" ".join(match.group(0).lower().split()))
                elif kind == "code":
                    found.add(f"code {groups[0].upper()}")
                elif kind == "bundle":
                    found.add(" ".join(match.group(0).lower().split())[:60])
                else:
                    found.add(match.group(1).lower())
    return sorted(found)


def jsonld_products(blocks):
    """Product price and stock facts from a page's structured data: [{name, price, currency, availability}]."""
    out = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" in kinds:
                offers = node.get("offers")
                for offer in (offers if isinstance(offers, list) else [offers] if offers else []):
                    if isinstance(offer, dict):
                        out.append({"name": clean_text(str(node.get("name", ""))).strip()[:160], "price": str(offer.get("price") or offer.get("lowPrice") or ""),
                                    "currency": str(offer.get("priceCurrency", "")), "availability": str(offer.get("availability", "")).rsplit("/", 1)[-1]})
            for key in ("@graph", "itemListElement", "mainEntity"):
                if key in node:
                    walk(node[key])
            if "item" in node:
                walk(node["item"])
    walk(blocks)
    return out[:50]
