"""Feeds: blog and news feeds (RSS, Atom, JSON Feed) and product feeds (Google Merchant XML, CSV and TSV).

Whatever the format, an item comes out in one shape, with its text cleaned of hidden and invisible content and its address checked.
"""
import csv
import io
import json
import re
from urllib.parse import urljoin

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from .. import contract as K
from .. import safety
from ..safety import clean_text, visible_text

_G = "{http://base.google.com/ns/1.0}"
_MONEY = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(node, *names):
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return " ".join(clean_text(child.text).split())
    return ""


def _gtext(node, name):
    for child in node:
        if child.tag in (_G + name, name) and (child.text or "").strip():
            return " ".join(clean_text(child.text).split())
    return ""


def _summary(raw, limit):
    return " ".join(visible_text(raw).split())[:limit] if raw else ""


def _price(text):
    match = _MONEY.search(str(text or ""))
    return round(float(match.group(1).replace(",", "")), 2) if match else None


def _item(**fields):
    base = {"id": "", "title": "", "link": "", "published": "", "updated": "", "summary": "", "author": "", "categories": [], "price": None, "sale_price": None,
            "currency": "", "availability": "", "image": "", "brand": "", "gtin": ""}
    base.update(fields)
    base["id"] = base["id"] or base["link"] or base["title"]
    return base


def _link_ok(base, link):
    if not link:
        return ""
    full = urljoin(base, link)
    return full if safety.is_public_url(full) else ""


def parse_feed(body, url="", max_items=500, summary_chars=500):
    """{'format', 'title', 'items'} from a feed file. Raises UPSTREAM_BROKEN when it is not a readable feed."""
    head = body.lstrip()[:300]
    if head.startswith(b"{"):
        try:
            data = json.loads(body.decode("utf-8-sig", errors="replace"))
        except ValueError:
            raise K.Failure(K.UPSTREAM_BROKEN, "the feed looks like JSON but cannot be read") from None
        if not isinstance(data, dict) or "items" not in data:
            raise K.Failure(K.UPSTREAM_BROKEN, "the JSON file is not a JSON Feed")
        items = [_item(id=str(i.get("id") or ""), title=clean_text(str(i.get("title") or "")).strip(), link=_link_ok(url, i.get("url") or i.get("external_url") or ""),
                       published=str(i.get("date_published") or ""), updated=str(i.get("date_modified") or ""),
                       summary=_summary(i.get("content_html") or i.get("content_text") or i.get("summary") or "", summary_chars),
                       author=clean_text(str((i.get("author") or {}).get("name", ""))) if isinstance(i.get("author"), dict) else "",
                       image=_link_ok(url, i.get("image") or "")) for i in data["items"][:max_items] if isinstance(i, dict)]
        return {"format": "json", "title": clean_text(str(data.get("title", ""))).strip(), "items": items}
    if not head.startswith(b"<"):
        return _parse_table(body, url, max_items)
    try:
        root = DET.fromstring(body, forbid_dtd=True)
    except (DET.ParseError, DefusedXmlException, ValueError):
        raise K.Failure(K.UPSTREAM_BROKEN, "the feed is not valid XML (or uses features that are refused for safety)") from None
    kind = _local(root.tag)
    if kind == "feed":
        entries = [n for n in root if _local(n.tag) == "entry"]
        items = []
        for e in entries[:max_items]:
            link = ""
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                    link = c.get("href")
                    break
            author = next((_text(a, "name") for a in e if _local(a.tag) == "author"), "")
            items.append(_item(id=_text(e, "id"), title=_text(e, "title"), link=_link_ok(url, link), published=_text(e, "published"), updated=_text(e, "updated"),
                               summary=_summary(next((c.text for c in e if _local(c.tag) in ("summary", "content") and c.text), ""), summary_chars), author=author,
                               categories=[c.get("term", "") for c in e if _local(c.tag) == "category" and c.get("term")]))
        return {"format": "atom", "title": _text(root, "title"), "items": items}
    if kind in ("rss", "RDF"):
        channel = next((n for n in root if _local(n.tag) == "channel"), root)
        merchant = any(c.tag.startswith(_G) for i in channel if _local(i.tag) == "item" for c in i)
        items = []
        for node in [n for n in (channel if kind == "rss" else root) if _local(n.tag) == "item"][:max_items]:
            if merchant:
                price, sale = _gtext(node, "price"), _gtext(node, "sale_price")
                items.append(_item(id=_gtext(node, "id") or _text(node, "guid"), title=_gtext(node, "title") or _text(node, "title"),
                                   link=_link_ok(url, _gtext(node, "link") or _text(node, "link")), price=_price(price), sale_price=_price(sale),
                                   currency=(price.split()[-1] if " " in price else ""), availability=_gtext(node, "availability"),
                                   image=_link_ok(url, _gtext(node, "image_link")), brand=_gtext(node, "brand"), gtin=_gtext(node, "gtin"),
                                   summary=_summary(_gtext(node, "description") or _text(node, "description"), summary_chars),
                                   categories=[_gtext(node, "product_type")] if _gtext(node, "product_type") else []))
            else:
                items.append(_item(id=_text(node, "guid"), title=_text(node, "title"), link=_link_ok(url, _text(node, "link")), published=_text(node, "pubDate", "date"),
                                   summary=_summary(next((c.text for c in node if _local(c.tag) in ("description", "encoded") and c.text), ""), summary_chars),
                                   author=_text(node, "creator", "author"), categories=[_text(node, "category")] if _text(node, "category") else []))
        return {"format": "merchant" if merchant else "rss", "title": _text(channel, "title"), "items": items}
    raise K.Failure(K.UPSTREAM_BROKEN, f"unexpected feed root <{kind}>")


def _parse_table(body, url, max_items):
    text = body.decode("utf-8-sig", errors="replace")
    sample = text[:4000]
    dialect = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=dialect)
    names = {(n or "").strip().lower().replace("g:", ""): n for n in (reader.fieldnames or [])}
    if not ("id" in names and "title" in names):
        raise K.Failure(K.UPSTREAM_BROKEN, "the file is neither a feed nor a product table with id and title columns")
    items = []
    for row in reader:
        if len(items) >= max_items:
            break

        def col(name):
            return clean_text(row.get(names.get(name, ""), "") or "").strip()
        price = col("price")
        items.append(_item(id=col("id"), title=col("title"), link=_link_ok(url, col("link")), price=_price(price), sale_price=_price(col("sale_price")),
                           currency=(price.split()[-1] if " " in price else ""), availability=col("availability"), image=_link_ok(url, col("image_link")),
                           brand=col("brand"), gtin=col("gtin"), summary=_summary(col("description"), 500),
                           categories=[col("product_type")] if col("product_type") else []))
    return {"format": "csv" if dialect == "," else "tsv", "title": "", "items": items}


def guess_feed_urls(base_url, blog_url=""):
    """Where a Shopify-style site usually publishes blog feeds, when none is advertised."""
    base = base_url.rstrip("/")
    guesses = []
    if blog_url:
        guesses.append(blog_url.rstrip("/") + ".atom" if "/blogs/" in blog_url else blog_url)
    guesses += [base + "/blogs/news.atom", base + "/blogs/journal.atom", base + "/feed", base + "/feed.xml", base + "/rss.xml", base + "/blog/feed"]
    seen, out = set(), []
    for g in guesses:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out
