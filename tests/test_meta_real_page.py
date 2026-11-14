"""Tests built on four real ads taken from a real Meta Ad Library page (tests/fixtures/meta_ad_library_sample.html)."""
import re
import shutil
from pathlib import Path

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import research as R
from contentintel import sheets
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeBrowser
from test_collectors import BROWSER_YAML, CREDS, NOW, site, statuses, world, write_research

HTML = Path("tests/fixtures/meta_ad_library_sample.html").read_text(encoding="utf-8")
RATE_LIMIT = {"errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL", "code": 1675004}], "extensions": {"is_final": True}}
ADS_URL = C.ad_library_url("overalls", "us", "active")
TASK = C.Task("meta_ads", "t", {"urls": [ADS_URL]}, "k")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return C.load_collector_settings("does-not-exist.yaml")


def meta_browser(cfg, **spec):
    return FakeBrowser({"https://www.facebook.com/ads/library": spec}, clickable=set(site(cfg, "meta_ads")["consent_any"]))


def run_meta(cfg, browser, n=30):
    return C.run_browser_task(browser, TASK, n, site(cfg, "meta_ads"), cfg, {})


# --- reading the data inside a page ------------------------------------------------------------------------------

def test_only_the_json_blocks_inside_a_page_are_read():
    blocks = B.parse_embedded_json(HTML)
    assert len(blocks) == 2 and "require" in blocks[0] and "require" in blocks[1]        # not the decoy script, not the broken block


@pytest.mark.parametrize("html,expected", [
    ("<script type=\"application/json\">{\"a\": 1}</script>", [{"a": 1}]),
    ("<script data-sjs type='application/json' nonce=\"\">\n {\"a\": 1}\n</script>", [{"a": 1}]),
    ("<SCRIPT TYPE=\"Application/JSON\">[1, 2]</SCRIPT>", [[1, 2]]),
    ("<script type=\"application/ld+json\">{\"a\": 1}</script>", []),
    ("<script type=\"text/javascript\">{\"a\": 1}</script>", []),
    ("<script type=\"application/json\">not json</script><script type=\"application/json\">{\"b\": 2}</script>", [{"b": 2}]),
    ("<script type=\"application/json\">   </script>", []),
    ("", []), (None, [])])
def test_embedded_json_is_found_however_the_tag_is_written(html, expected):
    assert B.parse_embedded_json(html) == expected


def test_a_huge_block_is_skipped_rather_than_read():
    assert B.parse_embedded_json('<script type="application/json">{"a": "' + "x" * 8_100_000 + '"}</script>') == []


# --- the real ads ----------------------------------------------------------------------------------------------------

def real_rows():
    return list(C.extract_json_items("meta_ads", B.parse_embedded_json(HTML)).values())


def test_all_four_real_ads_are_read_with_nothing_blank():
    rows = real_rows()
    assert [(r["handle"], r["item_id"].startswith("meta_ads:")) for r in rows] == [
        ("SHEIN", True), ("SHEIN CURVE", True), ("Madepants", True), ("Sarah Randall", True)]
    for r in rows:
        assert r["title"] and r["media_url"].startswith("https://") and re.match(r"20\d\d-\d\d-\d\dT", r["posted_at"])
        assert r["url"] == f"https://www.facebook.com/ads/library/?id={r['item_id'].split(':')[1]}"
    # The fourth ad's whole text is the template token {{product.brand}}; that is not real text, and "Sarah Randall"
    # selling through an Amazon link cannot be verified as the product's brand, so the text is left blank.
    assert [bool(r["text"]) for r in rows] == [True, True, True, False]
    assert not any("{{" in r["text"] for r in rows)


def test_placeholder_titles_from_dynamic_product_ads_are_replaced_by_the_product_name():
    rows = real_rows()
    assert rows[0]["title"].startswith("Elenzga Blue Elegant Tie-Waist Romper")
    assert not any("{{" in r["title"] or "{{" in r["details"] for r in rows)


def test_the_details_line_says_where_it_runs_what_it_asks_and_where_it_lands():
    first = real_rows()[0]["details"]
    assert first.startswith("Facebook/Instagram | CTA: Shop now | format: DPA | active | ends 2026-10-")
    assert "landing: https://us.shein.com/" in first
    assert "format: IMAGE" in real_rows()[2]["details"] and "format: DCO" in real_rows()[3]["details"]


def test_a_placeholder_title_with_no_card_to_borrow_from_is_left_blank_and_a_real_title_is_kept():
    base = {"ad_archive_id": "1", "snapshot": {"body": {"text": "t"}}}
    assert C.normalize_meta_ad({**base, "snapshot": {**base["snapshot"], "title": "{{product.name}}"}})["title"] == ""
    assert C.normalize_meta_ad({**base, "snapshot": {**base["snapshot"], "title": "Real title"}})["title"] == "Real title"
    both = {**base, "snapshot": {**base["snapshot"], "title": "{{product.name}}", "cards": [{"title": "Card title"}]}}
    assert C.normalize_meta_ad(both)["title"] == "Card title"


def test_details_cope_with_every_shape_of_platform_status_and_link():
    ad = {"ad_archive_id": "1", "publisher_platform": "FACEBOOK", "is_active": False, "end_date": 1792479600,
          "snapshot": {"link_url": "https://x.example/a", "cta_text": "Learn more", "display_format": "VIDEO"}}
    assert C.normalize_meta_ad(ad)["details"] == "Facebook | CTA: Learn more | format: VIDEO | inactive | ends 2026-10-20 | landing: https://x.example/a"
    assert C.normalize_meta_ad({"ad_archive_id": "2", "snapshot": {}})["details"] == ""
    template = {"ad_archive_id": "3", "snapshot": {"link_url": "https://x/{{placement}}", "cards": [{"link_url": "https://real.example/p", "cta_text": "Buy"}]}}
    assert C.normalize_meta_ad(template)["details"] == "CTA: Buy | landing: https://real.example/p"


# --- finding them without scrolling, and being rate-limited --------------------------------------------------------------------

def test_ads_that_are_inside_the_page_are_read_without_scrolling_at_all(cfg):
    b = meta_browser(cfg, embedded=B.parse_embedded_json(HTML))
    rows, stats = run_meta(cfg, b, n=4)
    assert len(rows) == 4 and b.scrolls == 0 and (stats["json"], stats["links"], stats["rate_limited"]) == (4, 0, False)


def test_a_page_with_fewer_ads_than_wanted_is_scrolled_as_far_as_the_limit_allows(cfg):
    b = meta_browser(cfg, embedded=B.parse_embedded_json(HTML))
    rows, stats = run_meta(cfg, b, n=30)
    assert len(rows) == 4 and b.scrolls == cfg["limits"]["max_scrolls_per_page"] and stats["rate_limited"] is False


@pytest.mark.parametrize("responses,expected", [
    ([RATE_LIMIT], True),
    ([{"errors": [{"message": "RATE LIMIT EXCEEDED"}]}], True),
    ([{"errors": [{"message": "x", "code": 1675004}]}], True),
    ([{"errors": [{"message": "Something went wrong"}]}], False),
    ([{"data": {}}, [1, 2], "text", None], False), ([], False)])
def test_a_rate_limit_answer_is_recognised(responses, expected):
    assert C.rate_limited(responses) is expected


def test_a_rate_limited_page_is_not_scrolled_again_and_the_report_says_so(cfg, tmp_path, store, swov, settings):
    page = {"json": [RATE_LIMIT], "embedded": B.parse_embedded_json(HTML)[1:]}
    b = meta_browser(cfg, **page)
    rows, stats = run_meta(cfg, b, n=10)
    assert len(rows) == 4 and b.scrolls == 0 and stats["rate_limited"] is True
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, **page)
    write_research(tmp_path, swov, {"track_own_brand": False, "ads": {"keywords": ["overalls"]}})
    result = C.run_collect([swov], store, browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                           sources=["meta_ads"], now=NOW, run_id="run1", root=str(tmp_path / "brands"))
    assert statuses(result) == [("meta_ads", "ran")] and "rate-limited further scrolling, so this is the first page of results only" in result.brands["swoveralls"][0].detail


def test_having_enough_ads_means_a_rate_limit_is_not_worth_mentioning(cfg):
    rows, stats = run_meta(cfg, meta_browser(cfg, json=[RATE_LIMIT], embedded=B.parse_embedded_json(HTML)), n=4)
    assert len(rows) == 4 and stats["rate_limited"] is False


def test_scrolling_still_happens_when_the_page_has_too_few_and_the_site_is_not_limiting(cfg):
    b = meta_browser(cfg, embedded=B.parse_embedded_json(HTML)[1:], scroll_json=[[RATE_LIMIT]])
    rows, stats = run_meta(cfg, b, n=10)
    assert b.scrolls == 1 and stats["rate_limited"] is True                 # it tried once, was told no, and stopped


# --- recon and the check, on the real page -----------------------------------------------------------------------------------

def test_recon_says_where_the_ads_were_found_and_saves_what_is_inside_the_page(tmp_path, cfg):
    import json
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, json=[RATE_LIMIT], embedded=B.parse_embedded_json(HTML))
    lines = C.recon(f, CREDS, cfg, ["meta_ads"], tmp_path / "debug")
    assert any("1 data responses and 2 data blocks inside the page saved; readable items: 4 (0 in the responses, 4 in the page); "
               "the site rate-limited a data request" in l for l in lines)
    blocks = json.loads((tmp_path / "debug" / "recon-meta_ads-page-data.json").read_text())
    assert [b["readable_items"] for b in blocks] == [0, 4] and all(len(b["sample"]) <= 800 for b in blocks)


def test_recon_flags_a_page_it_could_read_nothing_from(tmp_path, cfg):
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, json=[RATE_LIMIT])
    (line,) = [l for l in C.recon(f, CREDS, cfg, ["meta_ads"], tmp_path / "debug") if l.startswith("  meta_ads:")]
    assert "readable items: 0" in line and line.endswith("<-- nothing readable: open the saved screenshot and HTML")


def test_recon_also_saves_what_metas_own_page_search_sends(tmp_path, cfg):
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, embedded=B.parse_embedded_json(HTML))
    lines = C.recon(f, CREDS, cfg, ["meta_ads"], tmp_path / "debug")
    assert any(l.startswith("  meta_ads (page-search):") for l in lines)
    assert "search_type=page" in f.browsers["meta_ads"].opened[-1] and "q=Gymshark" in f.browsers["meta_ads"].opened[-1]
    for name in ("recon-meta_ads-page-search-network.json", "recon-meta_ads-page-search-page-data.json"):
        assert (tmp_path / "debug" / name).is_file(), name


def test_the_check_reads_the_real_ads_from_the_page(store, cfg, settings):
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, embedded=B.parse_embedded_json(HTML))
    (row,) = C.check_collectors(f, CREDS, settings, cfg, store, R.Ledger, NOW, ["meta_ads"])
    assert row["ok"] and row["detail"].startswith("login ok; 3 items (3 from the site's data, 0 only from page links): SHEIN")


def test_the_command_line_raises_a_warning_when_recon_reads_nothing(tmp_path, monkeypatch, capsys):
    cfg = B.load_browser_settings("browser.yaml")
    f = world(cfg)
    f.browsers["meta_ads"] = meta_browser(cfg, json=[RATE_LIMIT])
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda *_: f)
    monkeypatch.chdir(tmp_path)
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")
    code = cli.main(["collect", "--recon", "--sources", "meta_ads", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands"),
                     "--research-config", "research.yaml", "--browser-config", BROWSER_YAML, "--debug-dir", str(tmp_path / "debug")])
    assert code == 0 and "::warning::Recon read nothing from meta_ads" in capsys.readouterr().out


# --- the sheet and the workflow -------------------------------------------------------------------------------------------------

def test_the_details_travel_to_the_collected_rows_and_update_in_place():
    rows = real_rows()
    record = lambda at, items: {"at": at, "run": "r", "source": "meta_ads", "key": "k", "seeds": "s", "items": items}   # noqa: E731
    changed = {**rows[0], "details": "Facebook | active"}
    (row, *_), = [list(C.collected_rows([record("2026-10-01T00:00:00Z", rows[:1]), record("2026-10-08T00:00:00Z", [changed])]).values())]
    assert row["details"] == "Facebook | active" and row["times_seen"] == 2


def test_the_browser_install_is_pinned_cached_and_retried():
    text = open(".github/workflows/collect.yml", encoding="utf-8").read()
    assert re.fullmatch(r"playwright==\d+\.\d+\.\d+\n", open("requirements-browser.txt").read())
    assert "actions/cache@v4" in text and "~/.cache/ms-playwright" in text and "hashFiles('requirements-browser.txt')" in text
    assert "for attempt in 1 2 3" in text and "playwright install --with-deps chromium" in text and "sleep 20" in text
    assert "could not be downloaded after 3 tries" in text
    data = yaml.safe_load(text)
    steps = {s.get("name"): s for s in data["jobs"]["collect"]["steps"]}
    assert steps["Install the browser"]["if"] == steps["Remember the downloaded browser"]["if"] == "inputs.mode != 'plan' && inputs.mode != 'seeds' && inputs.mode != 'benchmarks'"             # the browser is not installed for the modes that open none


def test_the_sheet_has_a_details_column_right_after_the_media_link():
    cols = sheets.COLLECTED_COLUMNS
    assert cols[cols.index("media_url") + 1] == "details" and cols.count("details") == 1
