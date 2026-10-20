import copy

import pytest

from contentintel.diff import (confirm_removed, diff_availability, diff_records,
                               diff_snapshots, pending_removals)
from contentintel.normalize import normalize_all

from conftest import raw_products

T0, T1, T2, T3 = ("2026-10-19T08:00:00Z", "2026-10-20T08:00:00Z",
                  "2026-10-21T08:00:00Z", "2026-10-22T08:00:00Z")


def records(brand, raw):
    recs, problems = normalize_all(raw, brand)
    assert problems == []
    return recs


@pytest.fixture
def base(dude):
    """DudeRobe records plus a registry as it stands after the first confirmed baseline."""
    recs = records(dude, raw_products("duderobe_fetch_a.json"))
    _, registry = diff_snapshots({}, recs, {}, T0, initial=True)
    return recs, registry


def mutate(brand, handle, fn):
    raw = raw_products("duderobe_fetch_a.json")
    fn(next(p for p in raw if p["handle"] == handle))
    return records(brand, raw)


def run(prev, cur, registry, now=T1):
    return diff_snapshots(prev, cur, registry, now)


def only(events, kind):
    return [e for e in events if e["type"] == kind]


def test_two_real_fetches_that_differ_only_in_updated_at_produce_no_events(dude, base):
    recs, registry = base
    later = records(dude, raw_products("duderobe_fetch_b.json"))
    events, _ = run(recs, later, registry)
    assert events == []


def test_the_first_load_reports_nothing_as_new(base):
    recs, registry = base
    assert set(registry) == set(recs)
    assert all(e["presence"] == "present" and e["first_seen"] == T0 for e in registry.values())


def test_a_new_product_is_reported_once(dude, base):
    recs, registry = base
    raw = raw_products("duderobe_fetch_a.json")
    extra = copy.deepcopy(raw[0])
    extra.update(id=777, handle="the-new-robe", title="The New Robe")
    for v in extra["variants"]:
        v["id"] += 10_000_000
    raw.append(extra)
    cur = records(dude, raw)
    events, registry2 = run(recs, cur, registry)
    assert [(e["type"], e["title"]) for e in events] == [("new", "The New Robe")]
    again, _ = run(cur, cur, registry2, T2)
    assert again == []


def test_price_change_is_reported_with_old_and_new_values(dude, base):
    recs, registry = base
    cur = mutate(dude, "the-sherobe", lambda p: p["variants"][0].update(price="1.00"))
    events, _ = run(recs, cur, registry)
    (ev,) = only(events, "changed")
    assert ev["title"] == "The SheRobe"
    (change,) = ev["details"]["price"]
    assert change["new"] == 1.0 and change["old"] != 1.0
    assert set(ev["details"]) == {"price"}


def test_compare_at_price_change_marks_a_sale(dude, base):
    recs, registry = base
    cur = mutate(dude, "the-sherobe", lambda p: p["variants"][0].update(compare_at_price="999.00"))
    events, _ = run(recs, cur, registry)
    (ev,) = only(events, "changed")
    assert "compare_at_price" in ev["details"]
    assert ev["details"]["flags"]["added"] == ["on_sale"]


def test_a_new_description_is_reported_with_an_excerpt(dude, base):
    recs, registry = base
    cur = mutate(dude, "the-sherobe", lambda p: p.update(body_html="<p>Finally a description</p>"))
    events, _ = run(recs, cur, registry)
    (ev,) = only(events, "changed")
    assert "Finally a description" in ev["details"]["body_text"]["new"]
    assert ev["details"]["facts"]["added"] == ["description"]


def test_tag_changes_list_added_and_removed_tags(dude, base):
    recs, registry = base
    def fn(p):
        p["tags"] = [t for t in p["tags"] if t != "igg26_for_her"] + ["fresh"]
    events, _ = run(recs, mutate(dude, "the-sherobe", fn), registry)
    (ev,) = only(events, "changed")
    assert ev["details"]["tags"] == {"added": ["fresh"], "removed": ["igg26_for_her"]}


def test_title_change_is_reported(dude, base):
    recs, registry = base
    events, registry2 = run(recs, mutate(dude, "the-sherobe", lambda p: p.update(title="SheRobe 2")), registry)
    (ev,) = only(events, "changed")
    assert ev["details"]["title"]["new"] == "SheRobe 2"
    assert registry2[ev["product_id"]]["title"] == "SheRobe 2"


def test_a_variant_removed_is_reported(dude, base):
    recs, registry = base
    events, _ = run(recs, mutate(dude, "the-sherobe", lambda p: p["variants"].pop()), registry)
    (ev,) = only(events, "changed")
    assert len(ev["details"]["variants"]["removed"]) == 1


def test_image_replacement_is_reported_but_a_cdn_version_change_is_not(dude, base):
    recs, registry = base
    cur = mutate(dude, "the-sherobe", lambda p: p["images"][0].update(id=42, src="https://cdn.x/new.jpg"))
    (ev,) = only(run(recs, cur, registry)[0], "changed")
    assert ev["details"]["images"] == {"added": 1, "removed": 1}
    quiet = mutate(dude, "the-sherobe", lambda p: p["images"][0].update(src=p["images"][0]["src"] + "?v=1"))
    assert run(recs, quiet, registry)[0] == []


def test_stock_flips_are_a_separate_event_not_a_content_change(dude, base):
    recs, registry = base
    def sell_out(p):
        for v in p["variants"]:
            v["available"] = False
    sold = mutate(dude, "the-sherobe", sell_out)
    events, _ = run(recs, sold, registry)
    assert only(events, "changed") == []
    (ev,) = only(events, "availability")
    assert ev["details"]["product"] == "sold_out"
    assert ev["details"]["variants_sold_out"] and not ev["details"]["variants_restocked"]
    back, _ = run(sold, recs, registry, T2)
    (ev2,) = only(back, "availability")
    assert ev2["details"]["product"] == "back_in_stock"


def test_one_variant_selling_out_does_not_make_the_product_sold_out(dude, base):
    recs, registry = base
    sherobe = next(r for r in recs.values() if r["handle"] == "the-sherobe")
    first = sherobe["variants"][0]
    assert first["available"] and len(sherobe["variants"]) > 1
    cur = mutate(dude, "the-sherobe", lambda p: p["variants"][0].update(available=False))
    events, _ = run(recs, cur, registry)
    (ev,) = only(events, "availability")
    assert ev["details"]["product"] is None
    assert ev["details"]["variants_sold_out"] == [first["title"]]
    assert only(events, "changed") == []


def test_a_missing_product_is_suspected_never_deleted(dude, base):
    recs, registry = base
    raw = [p for p in raw_products("duderobe_fetch_a.json") if p["handle"] != "the-brothrow"]
    cur = records(dude, raw)
    events, registry2 = run(recs, cur, registry)
    assert [(e["type"], e["title"]) for e in events] == [("suspected_removed", "The BroThrow")]
    pid = events[0]["product_id"]
    assert registry2[pid]["presence"] == "suspected_removed"
    assert registry2[pid]["suspected_since"] == T1
    assert registry2[pid]["last_record"]["title"] == "The BroThrow"      # history kept
    assert pending_removals(registry2) == [
        {"product_id": pid, "title": "The BroThrow", "suspected_since": T1}]


def test_a_still_missing_product_is_not_reported_again(dude, base):
    recs, registry = base
    cur = records(dude, [p for p in raw_products("duderobe_fetch_a.json") if p["handle"] != "the-brothrow"])
    _, registry2 = run(recs, cur, registry)
    events, registry3 = run(cur, cur, registry2, T2)
    assert events == []
    assert registry3[next(iter(pending_removals(registry3)))["product_id"]]["suspected_since"] == T1


def test_a_person_confirms_a_removal(dude, base):
    recs, registry = base
    cur = records(dude, [p for p in raw_products("duderobe_fetch_a.json") if p["handle"] != "the-brothrow"])
    _, registry2 = run(recs, cur, registry)
    pid = pending_removals(registry2)[0]["product_id"]
    registry3 = confirm_removed(registry2, pid, "Harish", T2)
    assert registry3[pid]["presence"] == "confirmed_removed"
    assert registry3[pid]["confirmed_by"] == "Harish"
    assert pending_removals(registry3) == []
    assert registry2[pid]["presence"] == "suspected_removed"            # input untouched


def test_only_suspected_removals_can_be_confirmed(base):
    recs, registry = base
    pid = next(iter(registry))
    with pytest.raises(ValueError):
        confirm_removed(registry, pid, "Harish", T1)
    with pytest.raises(KeyError):
        confirm_removed(registry, "000", "Harish", T1)


def test_a_returning_product_is_reported_with_what_changed_while_it_was_away(dude, base):
    recs, registry = base
    gone = records(dude, [p for p in raw_products("duderobe_fetch_a.json") if p["handle"] != "the-brothrow"])
    _, registry2 = run(recs, gone, registry)
    back = mutate(dude, "the-brothrow", lambda p: p["variants"][0].update(price="5.00"))
    events, registry3 = run(gone, back, registry2, T2)
    (ev,) = only(events, "returned")
    assert ev["details"]["was"] == "suspected_removed"
    assert "price" in ev["details"]["changes"]
    entry = registry3[ev["product_id"]]
    assert entry["presence"] == "present" and "last_record" not in entry and "suspected_since" not in entry


def test_a_product_that_returns_after_confirmed_removal_is_flagged_too(dude, base):
    recs, registry = base
    gone = records(dude, [p for p in raw_products("duderobe_fetch_a.json") if p["handle"] != "the-brothrow"])
    _, registry2 = run(recs, gone, registry)
    registry3 = confirm_removed(registry2, pending_removals(registry2)[0]["product_id"], "Harish", T2)
    events, _ = run(gone, recs, registry3, T3)
    assert only(events, "returned")[0]["details"]["was"] == "confirmed_removed"


def test_inputs_are_not_modified(dude, base):
    recs, registry = base
    before_recs, before_registry = copy.deepcopy(recs), copy.deepcopy(registry)
    cur = mutate(dude, "the-sherobe", lambda p: p["variants"][0].update(price="1.00"))
    before_cur = copy.deepcopy(cur)
    run(recs, cur, registry)
    assert recs == before_recs and registry == before_registry and cur == before_cur


def test_diff_records_and_availability_are_empty_for_identical_records(base):
    recs, _ = base
    rec = next(iter(recs.values()))
    assert diff_records(rec, copy.deepcopy(rec)) == {}
    assert diff_availability(rec, copy.deepcopy(rec)) == {}


def test_swoveralls_scale_run_reports_only_the_real_change(swov, swov_records):
    from contentintel.diff import diff_snapshots as ds
    _, registry = ds({}, swov_records, {}, T0, initial=True)
    raw = raw_products("swoveralls_full_72.json")
    target = next(p for p in raw if p["handle"] == "swoveralls-navy")
    target["variants"][0]["price"] = "89.00"
    for p in raw:                                             # noise: every product touched
        p["updated_at"] = "2030-01-01T00:00:00-00:00"
    cur = records(swov, raw)
    events, _ = ds(swov_records, cur, registry, T1)
    assert [(e["type"], e["handle"]) for e in events] == [("changed", "swoveralls-navy")]


def test_a_rules_edit_that_reclassifies_a_product_is_reported_as_a_change(swov, swov_records):
    from dataclasses import replace
    _, registry = diff_snapshots({}, swov_records, {}, T0, initial=True)
    edited = replace(swov, catalogue={**swov.catalogue, "exclude": {
        **swov.catalogue["exclude"],
        "title_patterns": swov.catalogue["exclude"]["title_patterns"] + ["midnight black"]}})
    after = records(edited, raw_products("swoveralls_full_72.json"))
    events, _ = diff_snapshots(swov_records, after, registry, T1)
    assert events and {e["type"] for e in events} == {"changed"}
    assert all("Midnight Black" in e["title"] for e in events)
    assert all(e["details"]["status"]["new"] == "excluded" and e["details"]["excluded"]["new"] is True
               for e in events)


def test_the_registry_does_not_change_on_a_quiet_run(base):
    recs, registry = base
    events, registry2 = diff_snapshots(recs, recs, registry, T3)
    assert events == [] and registry2 == registry


def test_a_stated_status_is_reported_as_a_change(swov, swov_records):
    from dataclasses import replace
    _, registry = diff_snapshots({}, swov_records, {}, T0, initial=True)
    brand = replace(swov, manual_facts={"sidekick-crew-swoveralls-olive-branch": {"status": "active"}})
    after = records(brand, raw_products("swoveralls_full_72.json"))
    events, _ = diff_snapshots(swov_records, after, registry, T1)
    (ev,) = events
    assert ev["type"] == "changed" and ev["handle"] == "sidekick-crew-swoveralls-olive-branch"
    assert ev["details"]["status"] == {"old": "inactive", "new": "active"}
    assert ev["details"]["readiness"] == {"old": "inactive", "new": "ready"}
