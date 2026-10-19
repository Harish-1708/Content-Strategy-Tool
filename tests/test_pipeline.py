"""A pretend store, run for several 'days' through the real command line.

This is what the scheduled GitHub job does, minus the network: the catalogue is
served by a fake that can be edited between runs. It checks that a quiet day writes
almost nothing, that real changes land in the exports and change log, and that a
bad day is blocked without damaging anything.
"""
import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from contentintel import cli
from contentintel import sync as sync_mod
from contentintel.shopify_fetch import fetch_products

from conftest import FakeServer, raw_products

NO_SLEEP = lambda s: None  # noqa: E731


class Site:
    """A pretend live store whose catalogue can be edited between runs."""

    def __init__(self, fixture):
        self.products = raw_products(fixture)

    def fetch(self, domain):
        return fetch_products(domain, FakeServer(self.products, page_size=30), sleep=NO_SLEEP)

    def product(self, handle):
        return next(p for p in self.products if p["handle"] == handle)


class Clock:
    def __init__(self):
        self.day = -1

    def next_day(self):
        self.day += 1

    def __call__(self):
        moment = datetime(2026, 10, 19, 5, 17, tzinfo=timezone.utc) + timedelta(days=self.day)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def tree(root, skip=()):
    """{relative path: content hash} for every file under root, minus the skipped names."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in skip:
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def world(tmp_path, monkeypatch):
    site, clock = Site("swoveralls_full_72.json"), Clock()
    monkeypatch.setattr(sync_mod, "fetch_products", site.fetch)
    monkeypatch.setattr(sync_mod, "utc_now", clock)
    monkeypatch.setattr(cli, "fetch_sitemap_handles",
                        lambda domain: [p["handle"] for p in site.products])
    data = tmp_path / "data"

    def run(*args):
        return cli.main([*args, "--data-dir", str(data)])
    return site, clock, data, run


def sync_swov(run):
    return run("sync", "--brand", "swoveralls", "--sitemap")


def test_a_week_in_the_life_of_the_scheduled_sync(world, capsys):
    site, clock, data, run = world
    root = data / "swoveralls"

    # Day 0: first run stops for a person to confirm the count.
    clock.next_day()
    assert sync_swov(run) == 3
    assert (root / "pending.json").is_file()
    assert not (root / "latest.json").exists() and not (root / "catalogue").exists()
    assert run("status", "--brand", "swoveralls") == 2
    assert run("sync", "--brand", "swoveralls", "--sitemap") == 3          # asking again changes nothing
    assert len(list((root / "snapshots").glob("*.json"))) == 1

    # The person confirms 72. Exports appear.
    assert run("confirm-baseline", "--brand", "swoveralls", "--count", "72", "--by", "alice") == 0
    assert len(read_csv(root / "catalogue" / "products.csv")) == 72
    assert len(read_csv(root / "catalogue" / "variants.csv")) == 462
    assert json.loads((root / "last_checked.json").read_text())["count"] == 72
    assert not (root / "changes.jsonl").exists()
    assert run("status", "--brand", "swoveralls") == 0

    # Day 1: nothing changed. Only last_checked.json is touched.
    before = tree(root, skip={"last_checked.json"})
    stamp = json.loads((root / "last_checked.json").read_text())["run_at"]
    clock.next_day()
    capsys.readouterr()
    assert sync_swov(run) == 0
    assert "nothing new" in capsys.readouterr().out
    assert tree(root, skip={"last_checked.json"}) == before
    assert json.loads((root / "last_checked.json").read_text())["run_at"] != stamp

    # Day 1b: Shopify touches updated_at on every product. Still nothing is written.
    for p in site.products:
        p["updated_at"] = "2031-01-01T00:00:00-00:00"
        for v in p["variants"]:
            v["updated_at"] = "2031-01-01T00:00:00-00:00"
    clock.next_day()
    assert sync_swov(run) == 0
    assert tree(root, skip={"last_checked.json"}) == before

    # Day 2: a real price change. Snapshot, change log and exports all pick it up.
    site.product("swoveralls-navy")["variants"][0]["price"] = "89.00"
    clock.next_day()
    assert sync_swov(run) == 0
    assert len(list((root / "snapshots").glob("*.json"))) == 2
    changes = [json.loads(line) for line in (root / "changes.jsonl").read_text().splitlines()]
    assert [(c["type"], c["handle"]) for c in changes] == [("changed", "swoveralls-navy")]
    navy = next(r for r in read_csv(root / "catalogue" / "products.csv") if r["handle"] == "swoveralls-navy")
    assert (navy["price_min"], navy["price_max"]) == ("89.00", "99.00")

    # Day 3: a stock-only move. No new snapshot, but latest, the log and variants.csv follow.
    site.product("swoveralls-midnight-black")["variants"][0]["available"] = False
    clock.next_day()
    assert sync_swov(run) == 0
    assert len(list((root / "snapshots").glob("*.json"))) == 2
    changes = [json.loads(line) for line in (root / "changes.jsonl").read_text().splitlines()]
    assert changes[-1]["type"] == "availability" and changes[-1]["handle"] == "swoveralls-midnight-black"
    sold_out = [r for r in read_csv(root / "catalogue" / "variants.csv")
                if r["product_title"] == "Swoveralls - Midnight Black" and r["available"] == "false"]
    assert len(sold_out) == 1
    latest = json.loads((root / "latest.json").read_text())
    black = next(p for p in latest["products"].values() if p["handle"] == "swoveralls-midnight-black")
    assert black["variants"][0]["available"] is False

    # Day 4: the store returns only its first page. Blocked, and nothing else is touched.
    full = list(site.products)
    site.products = full[:30]
    keep = tree(root, skip={"failed_runs.jsonl"})
    clock.next_day()
    assert sync_swov(run) == 2
    assert tree(root, skip={"failed_runs.jsonl"}) == keep
    assert len((root / "failed_runs.jsonl").read_text().splitlines()) == 1
    assert run("status", "--brand", "swoveralls") == 2

    # Day 5: recovery.
    site.products = full
    clock.next_day()
    assert sync_swov(run) == 0
    assert run("status", "--brand", "swoveralls") == 0
    assert len((root / "failed_runs.jsonl").read_text().splitlines()) == 1     # history kept

    # Day 6: one product disappears. Suspected, never silently deleted.
    navy_raw = site.product("swoveralls-navy")
    navy_id = str(navy_raw["id"])
    site.products = [p for p in full if p["handle"] != "swoveralls-navy"]
    clock.next_day()
    assert sync_swov(run) == 0
    registry = json.loads((root / "registry.json").read_text())
    assert registry[navy_id]["presence"] == "suspected_removed"
    assert registry[navy_id]["last_record"]["title"] == "Swoveralls - Navy"
    assert len(read_csv(root / "catalogue" / "products.csv")) == 71
    assert run("confirm-removed", "--brand", "swoveralls", "--product-id", navy_id, "--by", "alice") == 0
    assert json.loads((root / "registry.json").read_text())[navy_id]["presence"] == "confirmed_removed"

    # Day 7: it comes back.
    site.products = full
    clock.next_day()
    assert sync_swov(run) == 0
    last = [json.loads(line) for line in (root / "changes.jsonl").read_text().splitlines()][-1]
    assert last["type"] == "returned" and last["details"]["was"] == "confirmed_removed"
    assert len(read_csv(root / "catalogue" / "products.csv")) == 72


def test_sync_all_keeps_each_brand_in_its_own_folder_and_isolates_failures(tmp_path, monkeypatch, brands):
    fixtures = {"swoveralls": "swoveralls_full_72.json", "duderobe": "duderobe_fetch_a.json",
                "defi_snacks": "defi_snacks.json"}
    sites = {brands[slug].domain: Site(fixture) for slug, fixture in fixtures.items()}
    clock = Clock()
    monkeypatch.setattr(sync_mod, "fetch_products", lambda domain: sites[domain].fetch(domain))
    monkeypatch.setattr(sync_mod, "utc_now", clock)
    data = tmp_path / "data"

    def run(*args):
        return cli.main([*args, "--data-dir", str(data)])

    clock.next_day()
    assert run("sync", "--all") == 3
    for slug, count in (("swoveralls", 72), ("duderobe", 25), ("defi_snacks", 7)):
        assert run("confirm-baseline", "--brand", slug, "--count", str(count), "--by", "alice") == 0
    folders = sorted(p.name for p in data.iterdir())
    assert folders == ["defi_snacks", "duderobe", "swoveralls"]
    counts = {slug: len(read_csv(data / slug / "catalogue" / "products.csv")) for slug in folders}
    assert counts == {"defi_snacks": 7, "duderobe": 25, "swoveralls": 72}

    clock.next_day()
    assert run("sync", "--all") == 0
    before = {slug: tree(data / slug, skip={"last_checked.json"}) for slug in folders}

    sites[brands["swoveralls"].domain].products = sites[brands["swoveralls"].domain].products[:30]
    sites[brands["duderobe"].domain].product("the-sherobe")["variants"][0]["price"] = "1.00"
    checked_before = (data / "swoveralls" / "last_checked.json").read_text()
    clock.next_day()
    assert run("sync", "--all") == 2                                        # swoveralls blocked
    assert tree(data / "swoveralls", skip={"failed_runs.jsonl", "last_checked.json"}) == \
        before["swoveralls"]                                                # nothing else touched
    assert (data / "swoveralls" / "last_checked.json").read_text() == checked_before
    assert tree(data / "defi_snacks", skip={"last_checked.json"}) == before["defi_snacks"]
    dude_rows = read_csv(data / "duderobe" / "catalogue" / "products.csv")
    assert next(r for r in dude_rows if r["title"] == "The SheRobe")["price_min"] == "1.00"
    assert run("status", "--brand", "swoveralls") == 2
    assert run("status", "--brand", "duderobe") == 0
    assert run("status", "--brand", "defi_snacks") == 0


def test_a_quiet_day_before_confirmation_adds_nothing(world):
    site, clock, data, run = world
    root = data / "swoveralls"
    clock.next_day()
    sync_swov(run)
    first = tree(root)
    clock.next_day()
    assert sync_swov(run) == 3
    assert tree(root) == first


def test_a_failed_fetch_is_blocked_and_visible(world, monkeypatch):
    site, clock, data, run = world
    clock.next_day()
    sync_swov(run)
    run("confirm-baseline", "--brand", "swoveralls", "--count", "72", "--by", "alice")

    def broken(domain):
        raise sync_mod.FetchError("HTTP 503 for https://swoveralls.com/products.json")
    monkeypatch.setattr(sync_mod, "fetch_products", broken)
    clock.next_day()
    assert sync_swov(run) == 2
    failed = json.loads((data / "swoveralls" / "failed_runs.jsonl").read_text().splitlines()[0])
    assert failed["reasons"][0]["code"] == "fetch_error"
    assert run("status", "--brand", "swoveralls") == 2
