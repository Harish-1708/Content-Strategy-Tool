"""Customer reviews, and quotes that are word for word.

A brief may quote a customer only VERBATIM: never invented, never combined from two reviews, never polished (the DudeRobe rule: "Customer quotes must be verbatim from the Judge.me export; never invent or combine"). This module holds the
reviews and makes that rule exact:

* the store: data/<brand>/extras/reviews.json, filled from the brand's Judge.me EXPORT FILE (the complete route: Judge.me > Reviews > Export, uploaded to the repository) and/or from the PUBLIC widget Judge.me draws on each product page of
  the brand's own shop (a best-effort route: the first page of each product's reviews only). No email address or IP address is ever read or kept.
* the writer is shown a short list of reviews it may quote (5 star, the right product, a sentence or two long) and must copy a quote exactly and say which review it came from;
* the checks: a quote the writer attaches to a concept, and any quoted passage of four words or more anywhere in a concept's text, must be found, character for character (spacing and curly or straight quotes aside), inside ONE stored review.
  A quote that is not, or any quote when no review is stored at all, is an ERROR: the brief needs review.
"""
import csv
import hashlib
import io
import re
from html.parser import HTMLParser

MIN_QUOTE_WORDS = 4
MIN_QUOTE_CHARS = 12
PICK_MIN_CHARS, PICK_MAX_CHARS = 40, 320
SYNONYMS = {"body": ("body", "review", "content", "review_body", "text"), "title": ("title", "review_title", "headline"), "rating": ("rating", "stars", "score", "review_rating"),
            "date": ("review_date", "date", "created_at", "created", "reviewed_at"), "reviewer": ("reviewer_name", "name", "author", "reviewer", "customer_name"),
            "handle": ("product_handle", "handle", "product", "product_slug"), "source": ("source",)}
KEEP_NEVER = ("reviewer_email", "email", "ip_address", "ip")                       # read and thrown away unseen: nothing in this module ever stores them


def squash(text):
    """The text as it is COMPARED: spacing collapsed and curly quotes, apostrophes and dashes made plain. Case and every word and punctuation mark are kept: this is the whole of what 'verbatim' forgives."""
    t = str(text or "")
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-"), ("\u00a0", " ")):
        t = t.replace(a, b)
    return " ".join(t.split())


def review_id(reviewer, date, body):
    return "RV-" + hashlib.sha1(f"{squash(reviewer)}|{str(date)[:10]}|{squash(body)}".encode("utf-8")).hexdigest()[:8]


def _stars(value):
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return max(0, min(5, int(round(float(m.group(0)))))) if m else 0


def make_review(body, *, rating=0, title="", reviewer="", date="", handle="", source="", url=""):
    body = str(body or "").strip()
    if not squash(body):
        return None
    return {"id": review_id(reviewer, date, body), "body": body, "title": squash(title)[:200], "rating": _stars(rating), "reviewer": squash(reviewer)[:80], "date": str(date or "")[:10],
            "handle": squash(handle)[:120], "source": source, "url": url}


def parse_export(text, source="judge.me export"):
    """The reviews in a Judge.me export (CSV, columns found by name, in any order). Returns (reviews, skipped): rows with no review text are skipped and counted. Emails and IP addresses are never read into a review."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return [], 0
    names = {n.strip().lower(): n for n in reader.fieldnames}

    def col(key):
        for synonym in SYNONYMS[key]:
            if synonym in names:
                return names[synonym]
        return None
    cols = {k: col(k) for k in SYNONYMS}
    out, skipped = [], 0
    for row in reader:
        review = make_review(row.get(cols["body"]) if cols["body"] else "", rating=row.get(cols["rating"]) if cols["rating"] else 0, title=row.get(cols["title"]) if cols["title"] else "",
                             reviewer=row.get(cols["reviewer"]) if cols["reviewer"] else "", date=row.get(cols["date"]) if cols["date"] else "", handle=row.get(cols["handle"]) if cols["handle"] else "",
                             source=source)                                                          # the file's own 'source' column says where the customer wrote it (web, email...): the label here says where WE read it from
        if review is None:
            skipped += 1
        else:
            out.append(review)
    return out, skipped


class _Widget(HTMLParser):
    """Reads the reviews Judge.me writes into a product page: a block with class jdgm-rev holds a rating (data-score), an author, a timestamp (data-content), a title and a body."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.reviews, self.cur, self.field, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "jdgm-rev" in classes:                                      # a review block (a wrapper that also carries the class is replaced by the first review inside it, so it never becomes a review itself)
            self.cur, self.depth = {"body": "", "title": "", "author": "", "date": "", "score": ""}, 1
            return
        if self.cur is None:
            return
        if tag == "div":
            self.depth += 1
        if "jdgm-rev__rating" in classes and a.get("data-score"):
            self.cur["score"] = a["data-score"]
        if "jdgm-rev__timestamp" in classes:
            self.cur["date"] = a.get("data-content") or a.get("datetime") or ""
        for cls, field in (("jdgm-rev__author", "author"), ("jdgm-rev__title", "title"), ("jdgm-rev__body", "body")):
            if cls in classes:
                self.field = field

    def handle_endtag(self, tag):
        if self.cur is None:
            return
        if tag == "div":
            self.depth -= 1
            self.field = None
            if self.depth <= 0:
                self.reviews.append(self.cur)
                self.cur, self.field = None, None

    def handle_data(self, data):
        if self.cur is not None and self.field:
            self.cur[self.field] += data + (" " if self.field == "body" and data.endswith(("\n",)) else "")


def parse_widget(html, handle="", url="", source="judge.me widget on the product page"):
    """The reviews drawn into one product page by the Judge.me widget (the first page of them: the widget loads the rest with scripts). Returns reviews; an unreadable page gives none."""
    parser = _Widget()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:                                                                    # noqa: BLE001 - a page that will not parse gives no reviews, never a crash
        return []
    out = []
    for r in parser.reviews:
        review = make_review(r["body"], rating=r["score"], title=r["title"], reviewer=r["author"], date=r["date"], handle=handle, source=source, url=url)
        if review:
            out.append(review)
    return out


def load(store, slug):
    return store.read_extra(slug, "reviews") or {"reviews": []}


def save(store, slug, reviews, now, collect=None):
    store.write_extra(slug, "reviews", {"note": "customer reviews kept word for word, so a brief can quote them and be checked (never an email address or an IP address)", "updated": now,
                                        "sources": sorted({r.get("source", "") for r in reviews if r.get("source")}), "count": len(reviews), "collect": collect or {}, "reviews": reviews})


def merge(store, slug, fresh, source, now, progress=None):
    """Add reviews to the brand's store (the same review is never added twice). `progress` ({handle: {...}}) is merged into the collection state kept beside them. Returns (added, total). The whole store is rewritten in one piece."""
    data = load(store, slug)
    have = {r["id"]: r for r in data.get("reviews", [])}
    added = 0
    for r in fresh:
        if r["id"] not in have:
            have[r["id"]] = r
            added += 1
    reviews = sorted(have.values(), key=lambda r: (r.get("date", ""), r["id"]), reverse=True)
    save(store, slug, reviews, now, {**(data.get("collect") or {}), **(progress or {})})
    return added, len(reviews)


# ---- reading ALL of a product's reviews: Judge.me draws five or ten on the page and loads the rest, page by page, from its own public address ----------------------------------------------------------

_SHOP = (re.compile(r"Shopify\.shop\s*=\s*[\"']([^\"']+)[\"']"), re.compile(r"data-shop-domain=[\"']([^\"']+)[\"']"), re.compile(r"[\"']shop_domain[\"']\s*[:=]\s*[\"']([^\"']+)[\"']"))
_PRODUCT_ID = (re.compile(r"class=[\"'][^\"']*jdgm-[^\"']*[\"'][^>]*data-id=[\"'](\d+)[\"']"), re.compile(r"data-id=[\"'](\d+)[\"'][^>]*class=[\"'][^\"']*jdgm-"), re.compile(r"jdgm-[^>]*data-product-id=[\"'](\d+)[\"']"))


def shop_info(html):
    """{'shop': 'x.myshopify.com', 'product_id': '123'} as far as the product page says; a missing piece is ''. Judge.me's widget carries both in its own markup and settings."""
    html = html or ""
    shop = next((m.group(1) for rx in _SHOP for m in [rx.search(html)] if m), "")
    pid = next((m.group(1) for rx in _PRODUCT_ID for m in [rx.search(html)] if m), "")
    return {"shop": shop, "product_id": pid}


def page_url(shop, product_id, page, per_page=10):
    return (f"https://judge.me/reviews/reviews_for_widget?url={shop}&shop_domain={shop}&platform=shopify&page={page}&per_page={per_page}&product_id={product_id}")


def parse_page(text, handle="", url=""):
    """The reviews in one answer of Judge.me's page address: a JSON object whose html holds the reviews (or the html itself). Returns reviews; nothing readable gives none."""
    import json as _json
    body = str(text or "").strip()
    html = body
    if body.startswith("{"):
        try:
            data = _json.loads(body)
        except ValueError:
            return []
        html = str(data.get("html") or data.get("reviews_html") or data.get("review_html") or "") if isinstance(data, dict) else ""
    return parse_widget(html, handle=handle, url=url, source="judge.me widget (all review pages)")


def pick_for_prompt(reviews, handles=(), limit=8):
    """The reviews the writer may quote: five star, a sentence or two long, the brief's products first, newest first. Deterministic."""
    want = set(handles)
    ok = [r for r in reviews if r.get("rating", 0) >= 5 and PICK_MIN_CHARS <= len(squash(r["body"])) <= PICK_MAX_CHARS]
    ok.sort(key=lambda r: (0 if (r.get("handle") in want) else 1, [-ord(c) for c in r.get("date", "")], r["id"]))
    return ok[:limit]


def prompt_block(picked):
    if not picked:
        return ""
    lines = [f'- {r["id"]}: "{squash(r["body"])}" ({r["reviewer"] or "a customer"}, {r["date"] or "no date"}, {r["rating"]} stars)' for r in picked]
    return ("Customer reviews you may quote. A quote must be copied from ONE review exactly as written: never edited, shortened inside a sentence, corrected, polished or joined to another review, and no other customer words may be "
            "put in quotation marks. To use one, set the concept's \"quote\" to {\"review_id\": \"RV-...\", \"text\": \"the exact words\"}:\n" + "\n".join(lines))


def quoted_passages(text):
    """The passages a text puts in double quotation marks that are long enough to be a customer's words (four words or more)."""
    t = str(text or "")
    found = re.findall(r'"([^"]{%d,})"' % MIN_QUOTE_CHARS, squash(t))
    return [p for p in found if len(p.split()) >= MIN_QUOTE_WORDS]


def find_quote(passage, reviews):
    """The stored review a passage comes from, word for word, or None. A passage must sit inside ONE review's text (or its title): two reviews never join."""
    needle = squash(passage).strip(' .,"')
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    for r in reviews:
        if needle in squash(r["body"]) or needle in r.get("title", ""):
            return r
    return None


QUOTE_FIELDS = ("hook", "alt_hook", "script", "visual_direction", "cta")


def check_quotes(brief, reviews):
    """Findings {where, rule, severity, message} for every customer quote in a brief. Each concept's structured quote must name a stored review and match it exactly; every quoted passage in its texts must come from one stored review."""
    findings = []
    by_id = {r["id"]: r for r in reviews}
    for n, c in enumerate(brief.get("concepts", []), start=1):
        label = f"concept {n} ({c.get('name', '')})"
        q = c.get("quote") or {}
        if q.get("text"):
            r = by_id.get(q.get("review_id"))
            if r is None:
                findings.append({"where": f"{label} quote", "rule": "quote_unknown_review", "severity": "error", "message": f"the quote names {q.get('review_id') or 'no review'}, which is not a stored review: a quote must come from a real review"})
            elif squash(q["text"]).strip(' .,"') not in squash(r["body"]) and squash(q["text"]).strip(' .,"') not in r.get("title", ""):
                findings.append({"where": f"{label} quote", "rule": "quote_not_verbatim", "severity": "error", "message": f"the quote is not word for word in review {r['id']} (never edited, shortened inside a sentence or combined): \"{squash(q['text'])[:100]}\""})
        for field in QUOTE_FIELDS:
            for passage in quoted_passages(c.get(field)):
                if not reviews:
                    findings.append({"where": f"{label} {field.replace('_', ' ')}", "rule": "quote_unverifiable", "severity": "error", "message": f"\"{passage[:80]}\" is a customer quote but no review is stored for this brand, so it cannot be verified: never invent a quote (collect the reviews first)"})
                elif find_quote(passage, reviews) is None:
                    findings.append({"where": f"{label} {field.replace('_', ' ')}", "rule": "quote_not_verbatim", "severity": "error", "message": f"\"{passage[:80]}\" is not word for word in any one stored review: never invent, combine or polish a quote"})
    return findings


def attribution(review):
    """The line under a quote: who said it, when, where and how many stars."""
    return f"{review['reviewer'] or 'a customer'}, {review['date'] or 'no date'} ({review.get('source') or 'review'}, {review['rating']} stars, {review['id']})"
