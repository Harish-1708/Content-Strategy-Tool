"""A second reader for Meta Ad Library pages: the same page our own collector already reads, parsed a different way.

The parser is the one in facebook-ads-library-mcp (https://github.com/RamsesAguirre777/facebook-ads-library-mcp, MIT licence, copyright 2025 Ramses Aguirre), re-written here: it reads the rendered page as
markdown (cards that start at "Library ID:"), and gives, for every ad, the Library ID, the day it started, how many ads use the same creative, the advertiser and its address name, the landing address
(decoded from Facebook's l.facebook.com redirect), the call-to-action button, the headline strip and the body copy. Our own collector reads the structured data Facebook embeds in the page; this one reads what
is drawn on it. They are independent, so:

  * as a SECONDARY it adds what the first did not give for an ad both found (the Library ID, the headline strip, the landing domain) and counts the advertisers on a page, and
  * as a SAFETY NET it supplies ads when the structured read finds none on a page whose text clearly shows ads.

It never replaces a field the first reader filled, and it reads text the platform's own browser already fetched: it opens no page itself. The connection `fb_ads_library_mcp` (the repository's own checkout) is
run first when its checkout is present (the repository's own function, loaded as it is), with this copy as the backup; the health of the repository is recorded and the owner can switch it off. HTML is turned into the markdown shape the parser expects by html_to_markdown() below.
"""
import html
import re
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

_BLOCK = {"div", "p", "section", "article", "li", "ul", "ol", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "span"}
_NEWLINE = {"div", "p", "section", "article", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer"}


class _Md(HTMLParser):
    """Just enough HTML to markdown for the parser: links, images, bold, line breaks. Scripts and styles are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.href, self.strong = [], 0, [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "a" and a.get("href"):
            self.href.append(a["href"])
            self.out.append("[")
        elif tag == "a":
            self.href.append("")
        elif tag == "img" and a.get("src"):
            self.out.append(f"![{a.get('alt', '')}]({a['src']})")
        elif tag in ("strong", "b"):
            self.strong += 1
            self.out.append("**")
        elif tag in _NEWLINE:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            href = self.href.pop()
            self.out.append(f"]({href})" if href else "")
        elif tag in ("strong", "b"):
            self.strong = max(0, self.strong - 1)
            self.out.append("** ")
        elif tag in _NEWLINE:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip() if self.strong else " " + data.strip() + " ")


def html_to_markdown(page_html):
    p = _Md()
    p.feed(str(page_html or ""))
    text = "".join(p.out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n\s*\n+", "\n", text)


def decode_landing_url(fb_link):
    """l.facebook.com/l.php?u=<address> becomes <address>."""
    try:
        q = parse_qs(urlparse(fb_link).query)
        if "u" in q:
            return unquote(q["u"][0])
    except ValueError:
        pass
    return fb_link


CTA_LABELS = ("Learn more", "Sign up", "Shop now", "Book now", "Send message", "Send WhatsApp message", "Contact us", "Subscribe", "Get offer", "Get quote", "Apply now", "Download", "Watch more", "See menu",
              "Order now", "Donate now", "Play game", "Listen now", "Get showtimes", "Save", "Open link", "Message page", "Call now", "Get directions", "Follow page", "Use app", "Install now", "Buy tickets",
              "Request time", "Try in camera")
_CTA = re.compile(r"\s(" + "|".join(re.escape(c) for c in CTA_LABELS) + r")\s*\]\(https://l\.facebook\.com/l\.php", re.I)


def parse(markdown):
    """Ad cards from the rendered page, one dict each (keys may be missing when the page did not show them). Duplicates (a card drawn twice while scrolling) are kept once."""
    ads, seen = [], set()
    for chunk in re.split(r"\nLibrary ID:\s*", "\n" + str(markdown or ""))[1:]:
        m = re.match(r"(\d+)", chunk)
        if not m or m.group(1) in seen:
            continue
        lid = m.group(1)
        seen.add(lid)
        ad = {"library_id": lid, "ad_details_url": f"https://www.facebook.com/ads/library/?id={lid}"}
        s = re.search(r"Started running on ([A-Za-z]{3} \d{1,2}, \d{4})", chunk)
        ad["started_running"] = s.group(1) if s else None
        s = re.search(r"\*\*(\d+)\s+ads?\*\*\s+use this creative", chunk)
        ad["ads_using_creative"] = int(s.group(1)) if s else 1
        s = re.search(r"\[([^\]]+)\]\(https://www\.facebook\.com/([^/)]+)/?\)", chunk)
        if s:
            ad["advertiser"], ad["advertiser_handle"] = s.group(1).strip(), s.group(2)
        s = re.search(r"\(https://l\.facebook\.com/l\.php\?u=([^)&]+)", chunk)
        if s:
            real = decode_landing_url("https://l.facebook.com/l.php?u=" + s.group(1))
            ad["landing_url"], ad["landing_domain"] = real, urlparse(real).netloc
        s = _CTA.search(chunk)
        ad["cta"] = s.group(1).strip() if s else None
        s = re.search(r"\((https://scontent[^)]+?\.(?:jpe?g|png|webp)[^)]*)\)", chunk)
        ad["creative_image"] = s.group(1) if s else None
        if ad["cta"]:
            s = re.search(r"\[!\[[^\]]*\]\(https?://scontent[^)]+\)\s+(.+?)\s+" + re.escape(ad["cta"]) + r"\s*\]\(https://l\.facebook\.com", chunk, re.S)
            if s:
                text = re.sub(r"\s+", " ", s.group(1)).strip()
                ad["link_text"] = re.sub(r"^[A-Z0-9][A-Z0-9.\-]{2,}\s+", "", text)[:400]
        s = re.search(r"\*\*Sponsored\*\*\s*\n(.+?)(?:\n\[!\[|\nActive\n|\nInactive\n|$)", chunk, re.S)
        if s:
            body = re.sub(r"\s+\n", "\n", s.group(1)).strip()
            body = re.sub(r"\[([^\]]+)\]\(https?://l\.facebook\.com[^)]*\)", r"\1", body)
            ad["body"] = re.sub(r"\s*\[[^\]]*\]\([^)]*$", "", body).strip()[:2000]
        ad["platforms"] = [p for p in ("Facebook", "Instagram", "Audience Network", "Messenger") if p in chunk]
        ads.append(ad)
    return ads


def advertisers(ads):
    """{name: number of ads} across the page's cards, busiest first."""
    out = {}
    for a in ads:
        if a.get("advertiser"):
            out[a["advertiser"]] = out.get(a["advertiser"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def read_page(page_html):
    return parse(html_to_markdown(html.unescape(page_html) if "&lt;" in str(page_html)[:2000] else page_html))


def to_row(ad):
    """The platform's own collected-row fields for a secondary ad (the shape _row() in collectors.py gives meta ads): only what this reader actually saw."""
    body = ad.get("body") or ad.get("link_text") or ""
    details = ["read by the second Ad Library reader"]
    if ad.get("cta"):
        details.append(f"CTA: {ad['cta']}")
    if ad.get("landing_url"):
        details.append(f"landing: {ad['landing_url']}")
    if ad.get("platforms"):
        details.append("platforms: " + ", ".join(ad["platforms"]))
    return {"source": "meta_ads", "item_id": ad["library_id"], "handle": ad.get("advertiser") or "", "title": (ad.get("link_text") or body)[:120], "text": body, "url": ad["ad_details_url"],
            "posted_at": ad.get("started_running") or "", "variants": ad.get("ads_using_creative") or 1, "media_url": ad.get("creative_image") or "", "details": "; ".join(details), "via": "secondary reader"}


def fill_missing(row, ad):
    """Add to an already-collected ad row only what it lacks (never overwrite). Returns the names of the fields filled."""
    filled = []
    extra = []
    if ad.get("link_text") and ad["link_text"] not in (row.get("details") or "") and "headline:" not in (row.get("details") or ""):
        extra.append(f"headline: {ad['link_text']}")
    if ad.get("landing_domain") and "landing" not in (row.get("details") or ""):
        extra.append(f"landing: {ad['landing_url']}")
    if ad.get("cta") and "CTA:" not in (row.get("details") or ""):
        extra.append(f"CTA: {ad['cta']}")
    if extra:
        row["details"] = "; ".join(x for x in [row.get("details") or "", *extra] if x)
        filled.append("details")
    if not row.get("media_url") and ad.get("creative_image"):
        row["media_url"] = ad["creative_image"]
        filled.append("media_url")
    if not row.get("variants") and ad.get("ads_using_creative"):
        row["variants"] = ad["ads_using_creative"]
        filled.append("variants")
    return filled


PARSER_FILE = "facebook_ads_mcp_complete.py"
_STUBBED = ("fastmcp", "crawl4ai")


def checkout_parser(root=None):
    """The repository's own `_parse_ad_library_markdown`, loaded from its checkout (connection `fb_ads_library_mcp`), or None when the checkout or the function is missing. The repository's server libraries
    (fastmcp, crawl4ai) are not needed for parsing, so empty stand-ins are put in place only while its file loads and are removed again."""
    import importlib.util
    import sys
    import types
    from . import connections as CONN
    path = Path(root) / PARSER_FILE if root else CONN.external_dir() / "fb_ads_library_mcp" / PARSER_FILE
    if not path.is_file():
        return None

    class _Stub:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

        def __getattr__(self, name):
            return _Stub()

        def __call__(self, *a, **k):
            return _Stub()
    saved = {m: sys.modules.get(m) for m in _STUBBED}
    try:
        for m in _STUBBED:
            mod = types.ModuleType(m)
            mod.__getattr__ = lambda name: _Stub()                                    # any name the repository imports from them
            sys.modules[m] = mod
        spec = importlib.util.spec_from_file_location("fb_ads_library_mcp_checkout", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "_parse_ad_library_markdown", None)
        return fn if callable(fn) else None
    except Exception:                                                                 # noqa: BLE001 - a repository that does not load is the platform's copy's turn
        return None
    finally:
        for m, old in saved.items():
            if old is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = old


def apply(page_html, rows, *, row_factory=None, parse_md=None):
    """Use the second parse on a page the collector has already read. Existing rows only gain what they lack; when the structured read found NO ads on the page (rows is empty) and the page text shows ads, those ads
    are added through `row_factory(source, ident, **fields)` (collectors._row). Returns (new rows, facts about what the second reader saw)."""
    ads = parse_md(html_to_markdown(html.unescape(page_html) if "&lt;" in str(page_html)[:2000] else page_html)) if parse_md else read_page(page_html)
    by_id = {str(r.get("item_id", "")).split(":", 1)[-1]: r for r in rows}
    filled, new = 0, []
    for ad in ads:
        row = by_id.get(ad["library_id"])
        if row is not None:
            filled += 1 if fill_missing(row, ad) else 0
        elif not rows and row_factory is not None:
            fields = to_row(ad)
            new.append(row_factory("meta_ads", ad["library_id"], **{k: v for k, v in fields.items() if k not in ("source", "item_id", "via")}, found_via="secondary reader"))
    return new, {"seen": len(ads), "filled": filled, "added": len(new), "advertisers": advertisers(ads)}


def _as_list(value):
    if not isinstance(value, list):
        raise ValueError("the repository's parser did not return a list of ads")
    return value


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_reader(settings, row_factory):
    """The reader the collector is given, or None when the owner has switched the connection off: (html, rows) -> (new rows, facts). Failures of this reader never reach the collection."""
    from . import connections as CONN
    if not CONN.decide(settings, "fb_ads_library_mcp")["on"]:
        return None

    def reader(page_html, rows):
        try:
            md = html_to_markdown(html.unescape(page_html) if "&lt;" in str(page_html)[:2000] else page_html)
            external = checkout_parser()
            ads, _ = CONN.run_connection(settings, "fb_ads_library_mcp", (lambda: _as_list(external(md))) if external else None, lambda: parse(md), now=_now())
            return apply(page_html, rows, row_factory=row_factory, parse_md=lambda _md: ads)
        except Exception:                                                      # noqa: BLE001 - a secondary reader must never stop the first
            return [], {"seen": 0, "filled": 0, "added": 0, "advertisers": {}}
    return reader
