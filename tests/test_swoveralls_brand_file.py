"""The Swoveralls research file as shipped: the benchmark brands that are not loungewear are never followed, and nothing else about it broke."""
import pytest

from contentintel import benchmarks as bench
from contentintel import research as R

from conftest import BRANDS_DIR

SKIPPED = ["Allbirds", "Liquid Death", "Cotopaxi", "Dollar Shave Club"]
NAMES = ["The Oodie", "Madepants", "Gymshark", "Skims", "Allbirds", "Liquid Death", "Cotopaxi", "Dollar Shave Club"]


@pytest.fixture
def cfg(swov):
    return R.load_brand_research(swov, root=str(BRANDS_DIR))


def resolved(*names):
    return {"items": {bench.norm_name(n): {"status": "resolved", "page_id": str(1000 + i), "page_name": n, "page_likes": 1}
                      for i, n in enumerate(names)}}


def plan(cfg):
    pages = cfg["collect"]["ads"]["benchmark_pages"]
    return bench.plan_follow("Swoveralls", NAMES, pages, resolved("Swoveralls", *NAMES), "2026-10-28T00:00:00Z",
                             max_pages=20, discovery_per_run=20, retry_days=7)


def test_the_four_unwanted_brands_are_marked_skip_in_the_shipped_file(cfg):
    assert cfg["collect"]["ads"]["benchmark_pages"] == {name: "skip" for name in SKIPPED}


def test_the_product_keyword_is_still_there(cfg):
    assert cfg["collect"]["ads"]["keywords"] == ["sweatpant overalls"]


def test_skipped_brands_are_not_followed_not_looked_up_and_not_asked_about(cfg):
    p = plan(cfg)
    followed = {page["name"] for page in p.pages}
    assert followed == {"Swoveralls", "The Oodie", "Madepants", "Gymshark", "Skims"}
    assert sorted(p.skipped) == sorted(SKIPPED) and p.review == [] and p.discover == [] and p.held == [] and p.waiting == []


def test_without_the_skip_lines_those_brands_would_be_followed(cfg):
    pages = bench.plan_follow("Swoveralls", NAMES, {}, resolved("Swoveralls", *NAMES), "2026-10-28T00:00:00Z", max_pages=20, discovery_per_run=20, retry_days=7).pages
    assert {"Allbirds", "Liquid Death", "Cotopaxi", "Dollar Shave Club"} <= {p["name"] for p in pages}


def test_skip_matches_however_the_brand_is_spelled(cfg):
    pages = cfg["collect"]["ads"]["benchmark_pages"]
    p = bench.plan_follow("Swoveralls", ["liquid-death", "COTOPAXI", "Dollar Shave Club Inc"], pages, {"items": {}}, "2026-10-28T00:00:00Z",
                          max_pages=5, discovery_per_run=5, retry_days=7)
    assert len(p.skipped) >= 2 and "Cotopaxi" not in [d["name"] for d in p.discover] and "liquid-death" not in [d["name"] for d in p.discover]
