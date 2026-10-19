import json

import pytest

from contentintel.shopify_fetch import (FetchError, fetch_products, fetch_sitemap_handles,
                                        handles_from_sitemap_xml, http_get,
                                        make_http_json_getter)

from conftest import FakeServer, raw_products

NO_SLEEP = lambda s: None  # noqa: E731


def fetch(server, **kw):
    return fetch_products("swoveralls.com", server, sleep=NO_SLEEP, **kw)


def test_pages_until_an_empty_page():
    products = raw_products("swoveralls_full_72.json")
    server = FakeServer(products, page_size=30)
    result = fetch(server)
    assert len(result.products) == 72
    assert result.pages == 3
    assert result.terminal_empty_page is True
    assert result.requests_made == 4          # three full/partial pages plus the empty one
    assert [c[1]["page"] for c in server.calls] == [1, 2, 3, 4]


def test_does_not_stop_after_the_first_page():
    # The original bug: exactly 30 products came back because paging stopped at page 1.
    products = raw_products("swoveralls_full_72.json")
    result = fetch(FakeServer(products, page_size=30))
    assert len(result.products) != 30
    assert {p["id"] for p in result.products} == {p["id"] for p in products}


def test_catalogue_that_is_an_exact_multiple_of_the_page_size():
    products = raw_products("swoveralls_full_72.json")[:60]
    result = fetch(FakeServer(products, page_size=30))
    assert len(result.products) == 60
    assert result.terminal_empty_page is True


def test_small_catalogue_ends_on_the_empty_page_it_requested():
    products = raw_products("duderobe_fetch_a.json")
    server = FakeServer(products, page_size=250)
    result = fetch(server)
    assert len(result.products) == 25
    assert result.requests_made == 2           # page 1 (25 products), page 2 (empty)
    assert result.terminal_empty_page is True


def test_empty_catalogue_is_reported_not_hidden():
    result = fetch(FakeServer([]))
    assert result.products == []
    assert result.terminal_empty_page is True


def test_duplicate_ids_across_pages_are_recorded_once():
    products = raw_products("swoveralls_full_72.json")[:40]
    server = FakeServer(products, page_size=30)
    original = server.__call__

    def with_repeat(url, params):
        data = original(url, params)
        if params["page"] == 2:
            data["products"] = data["products"] + [products[0]]
        return data
    result = fetch(with_repeat)
    assert len(result.products) == 40
    assert result.duplicate_ids == [products[0]["id"]]


def test_a_server_that_repeats_page_one_forever_is_detected():
    products = raw_products("swoveralls_full_72.json")
    result = fetch(FakeServer(products, page_size=30, repeat_first_page=True))
    assert result.loop_detected is True
    assert result.terminal_empty_page is False
    assert len(result.products) == 30
    assert result.requests_made == 2


def test_page_cap_is_reported_when_no_empty_page_is_ever_seen():
    endless = [{"id": i, "handle": f"p{i}", "title": "x", "variants": []} for i in range(1000)]
    result = fetch(FakeServer(endless, page_size=10), max_pages=5)
    assert result.max_pages_hit is True
    assert result.terminal_empty_page is False
    assert result.requests_made == 5


def test_request_uses_the_plain_products_json_url():
    server = FakeServer(raw_products("defi_snacks.json"), page_size=250)
    fetch_products("defi-snacks.com", server, sleep=NO_SLEEP)
    url, params = server.calls[0]
    assert url == "https://defi-snacks.com/products.json"      # no trailing slash
    assert params == {"limit": 250, "page": 1}


@pytest.mark.parametrize("payload", [{}, {"products": "nope"}, [], None])
def test_unexpected_response_shapes_raise(payload):
    with pytest.raises(FetchError):
        fetch(lambda url, params: payload)


def test_product_without_id_raises():
    with pytest.raises(FetchError):
        fetch(lambda url, params: {"products": [{"handle": "x"}]})


@pytest.mark.parametrize("domain", ["https://swoveralls.com", "swoveralls.com/", "", "a b.com"])
def test_domain_must_be_a_bare_hostname(domain):
    with pytest.raises(FetchError):
        fetch_products(domain, lambda u, p: {"products": []}, sleep=NO_SLEEP)


class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_retries_rate_limits_then_succeeds():
    sleeps = []
    session = FakeSession([FakeResponse(429), FakeResponse(503), FakeResponse(200, "{}")])
    assert http_get("https://x.com/products.json", session=session, sleep=sleeps.append) == "{}"
    assert session.calls == 3
    assert sleeps == [1, 2]


def test_honours_retry_after_header():
    sleeps = []
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, "ok")])
    http_get("https://x.com/p", session=session, sleep=sleeps.append)
    assert sleeps == [7]


def test_gives_up_after_the_retry_limit():
    session = FakeSession([FakeResponse(500)] * 5)
    with pytest.raises(FetchError, match="Giving up"):
        http_get("https://x.com/p", session=session, sleep=NO_SLEEP, retries=4)
    assert session.calls == 5


def test_network_errors_are_retried():
    session = FakeSession([ConnectionError("down"), FakeResponse(200, "fine")])
    assert http_get("https://x.com/p", session=session, sleep=NO_SLEEP) == "fine"


def test_404_explains_the_likely_causes_and_does_not_retry():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(FetchError) as exc:
        http_get("https://defi.com/products.json", session=session, sleep=NO_SLEEP)
    assert "trailing slash" in str(exc.value)
    assert session.calls == 1


def test_html_instead_of_json_is_a_clear_error():
    session = FakeSession([FakeResponse(200, "<html>shop</html>")])
    getter = make_http_json_getter(session=session, sleep=NO_SLEEP)
    with pytest.raises(FetchError, match="did not return JSON"):
        getter("https://x.com/products.json", {"limit": 250, "page": 1})


def test_json_getter_parses_valid_json():
    session = FakeSession([FakeResponse(200, json.dumps({"products": []}))])
    getter = make_http_json_getter(session=session, sleep=NO_SLEEP)
    assert getter("https://x.com/products.json", {}) == {"products": []}


# Synthetic sitemap XML: written for these tests, not copied from a real store.
INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://shop.test/sitemap_products_1.xml?from=1&amp;to=9</loc></sitemap>
<sitemap><loc>https://shop.test/sitemap_pages_1.xml</loc></sitemap></sitemapindex>"""
CHILD = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://shop.test/products/alpha</loc></url>
<url><loc>https://shop.test/products/beta?variant=1</loc></url>
<url><loc>https://shop.test/pages/about</loc></url></urlset>"""


def test_sitemap_handles_come_from_product_sitemaps_only():
    seen = []

    def get_text(url):
        seen.append(url)
        return INDEX if url.endswith("/sitemap.xml") else CHILD
    assert fetch_sitemap_handles("shop.test", get_text) == ["alpha", "beta"]
    assert not any("sitemap_pages" in u for u in seen)


def test_sitemap_without_children_is_read_directly():
    assert fetch_sitemap_handles("shop.test", lambda url: CHILD) == ["alpha", "beta"]


def test_invalid_sitemap_xml_raises():
    with pytest.raises(FetchError):
        handles_from_sitemap_xml("<not xml")
