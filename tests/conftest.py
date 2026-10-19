import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contentintel.brands import load_all  # noqa: E402
from contentintel.normalize import normalize_all  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BRANDS_DIR = ROOT / "brands"
BRIEFS_DIR = ROOT / "briefs"

_CACHE = {}


def load_json(name):
    if name not in _CACHE:
        with open(FIXTURES / name, encoding="utf-8") as fh:
            _CACHE[name] = json.load(fh)
    return copy.deepcopy(_CACHE[name])


def raw_products(name):
    """Fresh copy of the products list from a saved catalogue fixture."""
    return load_json(name)["products"]


@pytest.fixture(scope="session")
def brands():
    return load_all(BRANDS_DIR)


@pytest.fixture
def swov(brands):
    return brands["swoveralls"]


@pytest.fixture
def dude(brands):
    return brands["duderobe"]


@pytest.fixture
def defi(brands):
    return brands["defi_snacks"]


@pytest.fixture
def swov_records(swov):
    records, problems = normalize_all(raw_products("swoveralls_full_72.json"), swov)
    assert not problems
    return records


def by_title(records, title):
    matches = [r for r in records.values() if r["title"] == title]
    assert len(matches) == 1, f"{title!r}: {len(matches)} matches"
    return matches[0]


def by_handle(records, handle):
    matches = [r for r in records.values() if r["handle"] == handle]
    assert len(matches) == 1, f"{handle!r}: {len(matches)} matches"
    return matches[0]


class FakeServer:
    """Pretends to be /products.json. `page_size` caps each page like Shopify's default."""

    def __init__(self, products, page_size=30, repeat_first_page=False):
        self.products = products
        self.page_size = page_size
        self.repeat_first_page = repeat_first_page
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        page = params["page"]
        size = min(params["limit"], self.page_size)
        if self.repeat_first_page:
            page = 1
        start = (page - 1) * size
        return {"products": self.products[start:start + size]}
