"""Fetch a Shopify storefront catalogue (/products.json) completely.

The first Swoveralls file held exactly 30 products because Shopify returns 30 per
page by default. This module therefore keeps requesting pages until one comes
back empty, and reports how it stopped so the completeness gate can judge it.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
USER_AGENT = "kelson-content-strategy-tool/0.1 (+catalogue sync)"


class FetchError(Exception):
    """Raised when a catalogue cannot be fetched or is not what we expect."""


@dataclass
class FetchResult:
    domain: str
    products: list
    pages: int                      # non-empty pages received
    terminal_empty_page: bool       # True only if an empty page ended the loop
    duplicate_ids: list = field(default_factory=list)
    loop_detected: bool = False     # a page returned nothing but already-seen ids
    max_pages_hit: bool = False     # stopped by the safety cap, not by an empty page
    requests_made: int = 0
    fetched_at: str = ""

    def meta(self):
        return {
            "source": "live",
            "domain": self.domain,
            "pages": self.pages,
            "terminal_empty_page": self.terminal_empty_page,
            "duplicate_ids": list(self.duplicate_ids),
            "loop_detected": self.loop_detected,
            "max_pages_hit": self.max_pages_hit,
            "requests_made": self.requests_made,
            "fetched_at": self.fetched_at,
        }


def _check_domain(domain):
    domain = (domain or "").strip().lower()
    if not DOMAIN_RE.match(domain) or "/" in domain:
        raise FetchError(f"Domain must be a bare hostname such as example.com, got {domain!r}")
    return domain


def http_get(url, params=None, *, session=None, sleep=time.sleep, retries=4, timeout=30):
    """GET with retries on 429 and 5xx. Returns the response body as text."""
    if session is None:
        import requests
        session = requests.Session()
    last = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout,
                               headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
        except Exception as exc:  # network error
            last = f"network error: {exc}"
        else:
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                raise FetchError(f"404 Not Found for {url}. Check the domain; use the URL "
                                 f"without a trailing slash; the store may not be on Shopify.")
            if resp.status_code in (429,) or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    sleep(min(int(retry_after), 60))
                    continue
            else:
                raise FetchError(f"HTTP {resp.status_code} for {url}")
        if attempt < retries:
            sleep(min(2 ** attempt, 30))
    raise FetchError(f"Giving up on {url} after {retries + 1} attempts ({last})")


def make_http_json_getter(session=None, sleep=time.sleep):
    def getter(url, params):
        body = http_get(url, params, session=session, sleep=sleep)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise FetchError(f"{url} did not return JSON (not a Shopify products.json?)") from exc
        return data
    return getter


def fetch_products(domain, getter=None, *, limit=250, max_pages=40, delay=0.4,
                   sleep=time.sleep, now=None):
    """Return every product, paging until an empty page. `getter(url, params)` -> dict."""
    domain = _check_domain(domain)
    getter = getter or make_http_json_getter(sleep=sleep)
    url = f"https://{domain}/products.json"
    seen = {}
    duplicates = []
    pages = 0
    requests_made = 0
    terminal = False
    loop = False
    for page in range(1, max_pages + 1):
        data = getter(url, {"limit": limit, "page": page})
        requests_made += 1
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise FetchError(f"{url} page {page}: response has no 'products' list")
        batch = data["products"]
        if not batch:
            terminal = True
            break
        pages += 1
        fresh = 0
        for item in batch:
            pid = item.get("id") if isinstance(item, dict) else None
            if pid is None:
                raise FetchError(f"{url} page {page}: product without an id")
            if pid in seen:
                duplicates.append(pid)
            else:
                seen[pid] = item
                fresh += 1
        if fresh == 0:
            loop = True
            break
        sleep(delay)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return FetchResult(
        domain=domain, products=list(seen.values()), pages=pages,
        terminal_empty_page=terminal, duplicate_ids=duplicates, loop_detected=loop,
        max_pages_hit=(not terminal and not loop), requests_made=requests_made,
        fetched_at=stamp,
    )


def _locs(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"Sitemap is not valid XML: {exc}") from exc
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def handles_from_sitemap_xml(xml_text):
    handles = []
    for loc in _locs(xml_text):
        match = re.search(r"/products/([^/?#]+)", loc)
        if match:
            handles.append(match.group(1))
    return handles


def fetch_sitemap_handles(domain, get_text=None, *, sleep=time.sleep):
    """Collect product handles from the public sitemap (an independent completeness check)."""
    domain = _check_domain(domain)
    if get_text is None:
        def get_text(url):
            return http_get(url, None, sleep=sleep)
    index_url = f"https://{domain}/sitemap.xml"
    index_text = get_text(index_url)
    children = [loc for loc in _locs(index_text) if "sitemap_products" in loc]
    handles = []
    if not children:
        handles.extend(handles_from_sitemap_xml(index_text))
    for child in children:
        handles.extend(handles_from_sitemap_xml(get_text(child)))
    return sorted(set(handles))
