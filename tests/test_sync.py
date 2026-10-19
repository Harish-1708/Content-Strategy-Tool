import json
from dataclasses import replace

import pytest

from contentintel import cli
from contentintel.brands import BrandError
from contentintel.shopify_fetch import FetchError, FetchResult
from contentintel.store import Store, StoreError
from contentintel.sync import (BaselineError, catalogue_state, confirm_baseline,
                               confirm_removed, format_report, reset_baseline, run_sync)

from conftest import FIXTURES, raw_products

T0, T1, T2, T3 = ("2026-10-19T08:00:00Z", "2026-10-20T08:00:00Z",
                  "2026-10-21T08:00:00Z", "2026-10-25T08:00:00Z")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def sync(brand, store, fixture, now, **kw):
    return run_sync(brand, store, raw_products=raw_products(fixture), now=now, **kw)


def baselined(brand, store, fixture="swoveralls_full_72.json", count=72):
    first = sync(brand, store, fixture, T0)
    assert first.state == "needs_baseline"
    confirm_baseline(brand.slug, store, count, "Harish", T0)


def test_first_run_needs_a_person_to_confirm_the_count(swov, store):
    result = sync(swov, store, "swoveralls_full_72.json", T0)
    assert result.state == "needs_baseline" and result.generation_allowed is False
    assert result.count == 72 and result.snapshot_path
    assert store.load_latest("swoveralls") is None            # not trusted yet
    assert store.load_pending("swoveralls")["count"] == 72
    state = catalogue_state("swoveralls", store, T0)
    assert state["usable"] is False


def test_confirming_the_wrong_count_saves_nothing(swov, store):
    sync(swov, store, "swoveralls_full_72.json", T0)
    with pytest.raises(BaselineError, match="30"):
        confirm_baseline("swoveralls", store, 30, "Harish", T0)
    assert store.load_baseline("swoveralls") is None and store.load_latest("swoveralls") is None
    assert store.load_pending("swoveralls") is not None


def test_confirming_needs_a_name_and_a_pending_snapshot(swov, store):
    with pytest.raises(BaselineError, match="Nothing"):
        confirm_baseline("swoveralls", store, 72, "Harish")
    sync(swov, store, "swoveralls_full_72.json", T0)
    with pytest.raises(BaselineError, match="who"):
        confirm_baseline("swoveralls", store, 72, "")


def test_confirmation_starts_the_baseline_and_the_registry(swov, store):
    baselined(swov, store)
    baseline = store.load_baseline("swoveralls")
    assert baseline["count"] == 72 and baseline["confirmed_by"] == "Harish"
    assert store.load_latest("swoveralls")["count"] == 72
    assert store.load_pending("swoveralls") is None
    assert len(store.load_registry("swoveralls")) == 72
    assert catalogue_state("swoveralls", store, T0)["usable"] is True


def test_a_baseline_cannot_be_confirmed_twice(swov, store):
    baselined(swov, store)
    sync(swov, store, "swoveralls_full_72.json", T1)
    with pytest.raises(BaselineError, match="already"):
        confirm_baseline("swoveralls", store, 72, "Harish", T1)


def test_an_unchanged_catalogue_syncs_cleanly_with_no_events(swov, store):
    baselined(swov, store)
    result = sync(swov, store, "swoveralls_full_72.json", T1)
    assert result.state == "ok" and result.events == [] and result.generation_allowed


def test_the_truncated_file_is_blocked_and_leaves_everything_untouched(swov, store):
    baselined(swov, store)
    before_latest = store.load_latest("swoveralls")
    before_registry = store.load_registry("swoveralls")
    result = sync(swov, store, "swoveralls_truncated_30.json", T1)
    assert result.state == "blocked" and not result.generation_allowed
    assert store.load_latest("swoveralls") == before_latest
    assert store.load_registry("swoveralls") == before_registry          # no false removals
    assert result.events == [] and result.pending_removals == []
    assert len(store.read_failed("swoveralls")) == 1
    assert len(list((store.dir("swoveralls") / "snapshots").glob("*.json"))) == 1   # rejected data not kept


def test_catalogue_state_turns_unusable_after_a_failed_run_even_within_the_same_second(swov, store):
    baselined(swov, store)
    sync(swov, store, "swoveralls_truncated_30.json", T0)               # same timestamp as the snapshot
    state = catalogue_state("swoveralls", store, T0)
    assert state["usable"] is False and "failed" in state["reasons"][0]
    sync(swov, store, "swoveralls_full_72.json", T1)                    # a good run clears it
    assert catalogue_state("swoveralls", store, T1)["usable"] is True


def test_a_stale_snapshot_warns_but_stays_usable(swov, store):
    baselined(swov, store)
    state = catalogue_state("swoveralls", store, T3)                    # six days later
    assert state["usable"] is True and "hours old" in state["reasons"][0]


def test_a_failed_fetch_is_blocked_and_logged(swov, store):
    def broken(domain):
        raise FetchError("404 Not Found")
    result = run_sync(swov, store, fetcher=broken, now=T0)
    assert result.state == "blocked" and result.gate.codes("fail") == ["fetch_error"]
    assert store.read_failed("swoveralls")[0]["reasons"][0]["message"] == "404 Not Found"


def test_a_live_style_fetch_is_judged_on_its_own_metadata(swov, store):
    products = raw_products("swoveralls_full_72.json")

    def fetcher(domain):
        return FetchResult(domain=domain, products=products, pages=1, terminal_empty_page=True,
                           requests_made=2, fetched_at=T0)
    result = run_sync(swov, store, fetcher=fetcher, now=T0)
    assert result.state == "needs_baseline"
    assert store.load_pending("swoveralls") is not None

    def never_ends(domain):
        return FetchResult(domain=domain, products=products, pages=40, terminal_empty_page=False,
                           max_pages_hit=True, requests_made=40, fetched_at=T1)
    assert run_sync(swov, store, fetcher=never_ends, now=T1).state == "blocked"


def test_a_moderate_drop_is_accepted_with_a_warning_and_suspected_removals(swov, store):
    baselined(swov, store)
    raw = raw_products("swoveralls_full_72.json")
    gone = {p["id"] for p in raw[:8]}                                    # 8 of 72 = 11%
    result = run_sync(swov, store, raw_products=[p for p in raw if p["id"] not in gone], now=T1)
    assert result.state == "warn" and result.generation_allowed
    assert {e["type"] for e in result.events} == {"suspected_removed"}
    assert len(result.pending_removals) == 8
    assert "Suspected removals awaiting confirmation" in format_report(result)


def test_removals_can_be_confirmed_through_the_sync_layer(swov, store):
    baselined(swov, store)
    raw = raw_products("swoveralls_full_72.json")
    run_sync(swov, store, raw_products=raw[1:] , now=T1)                 # one product disappears
    pid = str(raw[0]["id"])
    entry = confirm_removed("swoveralls", store, pid, "Harish", T2)
    assert entry["presence"] == "confirmed_removed"
    with pytest.raises(ValueError):
        confirm_removed("swoveralls", store, pid, "", T2)


def test_new_and_changed_products_flow_through_to_the_report(swov, store):
    baselined(swov, store)
    raw = raw_products("swoveralls_full_72.json")
    next(p for p in raw if p["handle"] == "swoveralls-navy")["variants"][0]["price"] = "89.00"
    result = run_sync(swov, store, raw_products=raw, now=T1)
    assert [(e["type"], e["handle"]) for e in result.events] == [("changed", "swoveralls-navy")]
    report = format_report(result)
    assert "Catalogue sync: OK" in report and "Swoveralls - Navy: changed: price" in report


def test_the_report_lists_products_that_need_facts_and_unknown_manual_handles(dude, store):
    ghost = replace(dude, manual_facts={**dude.manual_facts, "no-such-product": {"audience": "x"}})
    result = run_sync(ghost, store, raw_products=raw_products("duderobe_fetch_a.json"), now=T0)
    assert result.unknown_manual_handles == ["no-such-product"]
    assert sorted(result.needs_facts) == ["The BroThrow", "The Couple Bundle", "The SheRobe"]
    report = format_report(result)
    assert "needs facts: The SheRobe" in report and "unknown product handle: no-such-product" in report
    assert "ACTION" in report


def test_manual_facts_that_disagree_with_the_website_are_reported(swov, store):
    clash = replace(swov, manual_facts={"swoveralls-midnight-black": {
        "pocket_claims": [{"count": 7, "phrase": "seven pockets"}]}})
    result = run_sync(clash, store, raw_products=raw_products("swoveralls_full_72.json"), now=T0)
    assert len(result.fact_conflicts) == 1
    assert result.fact_conflicts[0]["fact"] == "pocket_claims"
    assert "differs from the website" in format_report(result)


def test_resetting_the_baseline_is_deliberate_and_requires_a_new_confirmation(swov, store):
    baselined(swov, store)
    with pytest.raises(BaselineError, match="who"):
        reset_baseline("swoveralls", store, "")
    reset_baseline("swoveralls", store, "Harish", T1)
    assert store.load_baseline("swoveralls") is None
    result = sync(swov, store, "swoveralls_full_72.json", T2)
    assert result.state == "needs_baseline"
    baseline, events = confirm_baseline("swoveralls", store, 72, "Harish", T2)
    assert baseline["count"] == 72 and events == []
    assert catalogue_state("swoveralls", store, T2)["usable"] is True


def test_reset_without_a_baseline_is_an_error(store):
    with pytest.raises(BaselineError):
        reset_baseline("swoveralls", store, "Harish")


# --- brands never mix -------------------------------------------------------------------

def test_two_brands_sync_side_by_side_without_sharing_anything(swov, dude, store):
    baselined(swov, store)
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sw, dr = store.load_latest("swoveralls"), store.load_latest("duderobe")
    assert (sw["count"], dr["count"]) == (72, 25)
    assert {p["brand"] for p in sw["products"].values()} == {"swoveralls"}
    assert {p["brand"] for p in dr["products"].values()} == {"duderobe"}
    assert not set(sw["products"]) & set(dr["products"])
    assert set(store.load_registry("swoveralls")) == set(sw["products"])
    assert store.dir("swoveralls") != store.dir("duderobe")


def test_a_failed_sync_for_one_brand_does_not_affect_the_other(swov, dude, store):
    baselined(swov, store)
    baselined(dude, store, "duderobe_fetch_a.json", 25)
    sync(swov, store, "swoveralls_truncated_30.json", T1)
    assert catalogue_state("swoveralls", store, T1)["usable"] is False
    assert catalogue_state("duderobe", store, T1)["usable"] is True
    assert store.read_failed("duderobe") == []


def test_confirming_one_brand_does_not_confirm_another(swov, dude, store):
    sync(swov, store, "swoveralls_full_72.json", T0)
    sync(dude, store, "duderobe_fetch_a.json", T0)
    confirm_baseline("swoveralls", store, 72, "Harish", T0)
    assert store.load_baseline("duderobe") is None
    assert catalogue_state("duderobe", store, T0)["usable"] is False
    with pytest.raises(BaselineError, match="25"):
        confirm_baseline("duderobe", store, 72, "Harish", T0)


def test_the_store_refuses_to_save_one_brands_snapshot_under_another(swov, store):
    sync(swov, store, "swoveralls_full_72.json", T0)
    snap = store.load_snapshot(store.load_pending("swoveralls")["snapshot"])
    with pytest.raises(StoreError):
        store.save_snapshot("duderobe", snap)
    with pytest.raises(StoreError):
        store.promote_latest("duderobe", snap)


def test_the_store_refuses_unsafe_brand_names(store):
    for bad in ("../x", "a/b", ""):
        with pytest.raises(BrandError):
            store.dir(bad)


def test_two_snapshots_saved_in_the_same_second_do_not_overwrite_each_other(swov, store):
    sync(swov, store, "swoveralls_full_72.json", T0)
    snap = store.load_snapshot(store.load_pending("swoveralls")["snapshot"])
    store.save_snapshot("swoveralls", snap)
    store.save_snapshot("swoveralls", snap)
    names = sorted(p.name for p in (store.dir("swoveralls") / "snapshots").glob("*.json"))
    assert names == ["20261019T080000Z-1.json", "20261019T080000Z-2.json", "20261019T080000Z.json"]


# --- command line -----------------------------------------------------------------------

def run_cli(tmp_path, *args):
    return cli.main([*args, "--data-dir", str(tmp_path / "cli-data")])


def test_cli_full_journey_and_exit_codes(tmp_path, capsys):
    full = str(FIXTURES / "swoveralls_full_72.json")
    trunc = str(FIXTURES / "swoveralls_truncated_30.json")
    assert run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file", full) == 3
    assert run_cli(tmp_path, "status", "--brand", "swoveralls") == 2
    assert run_cli(tmp_path, "confirm-baseline", "--brand", "swoveralls", "--count", "30", "--by", "H") == 1
    assert run_cli(tmp_path, "confirm-baseline", "--brand", "swoveralls", "--count", "72", "--by", "H") == 0
    assert run_cli(tmp_path, "status", "--brand", "swoveralls") == 0
    assert run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file", full) == 0
    assert run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file", trunc) == 2
    assert run_cli(tmp_path, "status", "--brand", "swoveralls") == 2
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "NEEDS_BASELINE" in out


def test_cli_warn_prints_a_github_annotation_and_exits_zero(tmp_path, capsys):
    full = raw_products("swoveralls_full_72.json")
    short = tmp_path / "short.json"
    short.write_text(json.dumps({"products": full[8:]}), encoding="utf-8")
    run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file", str(FIXTURES / "swoveralls_full_72.json"))
    run_cli(tmp_path, "confirm-baseline", "--brand", "swoveralls", "--count", "72", "--by", "H")
    assert run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file", str(short)) == 0
    assert "::warning::swoveralls" in capsys.readouterr().out


def test_cli_reports_errors_instead_of_crashing(tmp_path, capsys):
    assert run_cli(tmp_path, "sync", "--brand", "nonexistent", "--from-file", "x.json") == 1
    assert run_cli(tmp_path, "confirm-removed", "--brand", "swoveralls", "--product-id", "1", "--by", "H") == 1
    assert "Error" in capsys.readouterr().out


def test_cli_all_with_a_file_is_refused(tmp_path):
    assert run_cli(tmp_path, "sync", "--all", "--from-file", str(FIXTURES / "defi_snacks.json")) == 1


def test_cli_reads_a_sitemap_file_for_the_cross_check(tmp_path):
    sitemap = tmp_path / "handles.txt"
    handles = [p["handle"] for p in raw_products("swoveralls_full_72.json")]
    sitemap.write_text("\n".join(handles + [f"ghost-{i}" for i in range(30)]), encoding="utf-8")
    code = run_cli(tmp_path, "sync", "--brand", "swoveralls", "--from-file",
                   str(FIXTURES / "swoveralls_full_72.json"), "--sitemap-file", str(sitemap))
    assert code == 2                                                      # 30 of 102 handles missing from the feed
