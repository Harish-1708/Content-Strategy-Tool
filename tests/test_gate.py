import pytest

from contentintel.brands import DEFAULT_GATE
from contentintel.gate import check_completeness

CFG = dict(DEFAULT_GATE)
CONFIRMED = {"terminal_empty_page": True}


def gate(count, **kw):
    kw.setdefault("cfg", CFG)
    kw.setdefault("meta", CONFIRMED)
    return check_completeness(count, **kw)


def test_the_truncated_30_product_file_is_rejected_against_a_72_product_baseline():
    result = gate(30, meta={"source": "file", "terminal_empty_page": None},
                  reference_count=72, baseline_count=72)
    assert result.status == "fail" and not result.can_generate
    assert {"drop_vs_last_run", "below_baseline_floor", "default_page_size"} <= set(result.codes())


def test_first_clean_run_is_unbaselined_and_blocks_generation():
    result = gate(72)
    assert result.status == "unbaselined" and not result.can_generate
    assert "needs_baseline" in result.codes("info")


def test_confirmed_baseline_and_stable_count_passes():
    result = gate(72, reference_count=72, baseline_count=72)
    assert result.status == "pass" and result.can_generate and result.reasons == []


def test_moderate_drop_warns_but_still_allows_generation():
    result = gate(64, reference_count=72, baseline_count=72)      # 11% drop
    assert result.status == "warn" and result.can_generate
    assert result.codes("warn") == ["drop_vs_last_run"]


def test_large_drop_fails():
    result = gate(50, reference_count=72, baseline_count=50)      # 31% drop
    assert result.status == "fail" and "drop_vs_last_run" in result.codes("fail")


def test_drop_exactly_at_the_warn_threshold_does_not_warn():
    result = gate(90, reference_count=100, baseline_count=100)    # exactly 10%
    assert result.status == "pass"


def test_tiny_catalogues_ignore_a_one_product_move():
    assert gate(6, reference_count=7, baseline_count=7).status == "pass"
    assert gate(5, reference_count=7, baseline_count=7).status == "fail"     # 2 products, 29%


def test_big_growth_warns():
    result = gate(120, reference_count=72, baseline_count=72)
    assert result.status == "warn" and result.codes("warn") == ["growth_vs_last_run"]
    assert gate(100, reference_count=72, baseline_count=72).status == "pass"   # 39% growth


def test_stepwise_erosion_is_caught_by_the_baseline_floor():
    # Each step looks small against the previous run, but the catalogue is leaking away.
    result = gate(55, reference_count=60, baseline_count=100)
    assert result.status == "fail" and result.codes("fail") == ["below_baseline_floor"]


def test_empty_catalogue_fails_even_without_any_reference():
    result = gate(0)
    assert result.status == "fail" and "empty_catalogue" in result.codes("fail")


@pytest.mark.parametrize("meta,code", [
    ({"duplicate_ids": [1]}, "duplicate_ids"),
    ({"loop_detected": True}, "paging_loop"),
    ({"max_pages_hit": True}, "no_final_page"),
])
def test_fetch_problems_fail_the_gate(meta, code):
    result = gate(72, meta=meta, reference_count=72, baseline_count=72)
    assert result.status == "fail" and code in result.codes("fail")


def test_unreadable_products_fail_the_gate():
    result = gate(70, reference_count=72, baseline_count=72,
                  problems=[{"product_id": "9", "problem": "product missing 'title'"}])
    assert result.status == "fail" and "malformed_products" in result.codes("fail")


@pytest.mark.parametrize("size", [30, 50, 250])
def test_a_count_equal_to_a_default_page_size_warns_unless_the_end_was_confirmed(size):
    unconfirmed = gate(size, meta={"terminal_empty_page": None}, reference_count=size, baseline_count=size)
    assert unconfirmed.status == "warn" and "default_page_size" in unconfirmed.codes("warn")
    confirmed = gate(size, meta={"terminal_empty_page": True}, reference_count=size, baseline_count=size)
    assert confirmed.status == "pass"


def test_sitemap_agreement_passes():
    handles = [f"p{i}" for i in range(20)]
    assert gate(20, reference_count=20, baseline_count=20,
                feed_handles=handles, sitemap_handles=handles).status == "pass"


def test_sitemap_missing_a_few_products_warns_and_names_them():
    sitemap = [f"p{i}" for i in range(20)]
    result = gate(17, reference_count=17, baseline_count=17,
                  feed_handles=sitemap[:17], sitemap_handles=sitemap)   # 15% missing
    assert result.status == "warn"
    assert "sitemap_mismatch" in result.codes("warn")
    assert "p17" in result.reasons[0]["message"]


def test_sitemap_missing_many_products_fails():
    sitemap = [f"p{i}" for i in range(20)]
    result = gate(12, reference_count=12, baseline_count=12,
                  feed_handles=sitemap[:12], sitemap_handles=sitemap)   # 40% missing
    assert result.status == "fail" and "sitemap_mismatch" in result.codes("fail")


def test_feed_products_missing_from_the_sitemap_are_not_a_problem():
    # Hidden or unpublished products can be in the feed without being in the sitemap.
    feed = [f"p{i}" for i in range(20)]
    result = gate(20, reference_count=20, baseline_count=20,
                  feed_handles=feed, sitemap_handles=feed[:15])
    assert result.status == "pass"


def test_a_single_missing_product_on_a_small_store_is_ignored():
    feed = ["a", "b", "c", "d", "e", "f"]
    assert gate(6, reference_count=6, baseline_count=6,
                feed_handles=feed, sitemap_handles=feed + ["g"]).status == "pass"


def test_an_empty_sitemap_warns_instead_of_silently_passing():
    result = gate(20, reference_count=20, baseline_count=20, feed_handles=["a"], sitemap_handles=[])
    assert result.status == "warn" and "sitemap_empty" in result.codes("warn")


def test_fail_outranks_unbaselined_and_warn():
    result = gate(0, meta={"terminal_empty_page": None, "max_pages_hit": True})
    assert result.status == "fail"


def test_per_brand_thresholds_are_respected():
    strict = dict(CFG, warn_change=0.02, fail_drop=0.05)
    assert gate(97, reference_count=100, baseline_count=100, cfg=strict).status == "warn"
    assert gate(90, reference_count=100, baseline_count=100, cfg=strict).status == "fail"
