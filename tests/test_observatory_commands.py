"""The observatory commands through the real command line, and the sheet tabs through the fake spreadsheet."""
import json
import shutil

import pytest
import yaml

from contentintel import cli, sheets
from contentintel.observatory import commands as OCMD
from contentintel.observatory import report as RP
from contentintel.store import Store
from conftest import ROOT
from fake_sheets import FakeSpreadsheet, read
from observatory_helpers import Web, client, reply, settings
from test_observatory_corpus import Yt, item as yt_item
from test_observatory_monitor import BASE, T1, T2, world

SLUG = "duderobe"
EMAIL = "research@kelson.example"


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    brands = tmp_path / "brands"
    shutil.copytree(ROOT / "brands", brands)
    (brands / SLUG / "observatory.yaml").write_text(yaml.safe_dump({"competitors": [{"name": "Shop", "domain": "shop.example", "pages": [BASE + "/pages/about"]}],
                                                                  "youtube": {"channels": ["@chan"], "queries": ["protein snacks"]}}), encoding="utf-8")
    monkeypatch.setenv("OBSERVATORY_CONTACT", EMAIL)
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIza-SECRET-KEY-0123456789")
    data = tmp_path / "data"
    state = {"web": world(1)}
    monkeypatch.setattr(OCMD, "CLIENT_FACTORY", lambda: client(state["web"], settings()))
    monkeypatch.setattr(OCMD, "YOUTUBE_GET", Yt(uploads=["vid1", "vid2", "vid3", "vid4", "vid5"], views="1000"))

    def go(action, *extra, now=T1):
        monkeypatch.setattr("contentintel.sync.utc_now", lambda: now)
        code = cli.main(["observatory", action, "--brand", SLUG, "--data-dir", str(data), "--brands-dir", str(brands), "--sources-config", str(ROOT / "sources.yaml"),
                         "--settings", str(tmp_path / "none.yaml"), "--research-config", str(ROOT / "research.yaml"), *extra])
        return code, capsys.readouterr().out
    go.data, go.store, go.state, go.tmp = data, Store(data), state, tmp_path
    return go


def test_plan_lists_what_would_be_read_and_makes_no_request(env):
    code, out = env("plan")
    assert code == 0 and "1 site(s) for 1 brand(s)" in out and "shop.example" in out and "contact address (set)" in out and "robots.txt always obeyed" in out
    assert env.state["web"].log == []


def test_monitor_reads_every_part_writes_the_report_and_records_the_run(env):
    code, out = env("monitor")
    assert code == 0 and "shop.example: 5 part(s) read, 0 failed, 0 change(s)" in out and "Observatory run: 1 site(s), 5 event(s), 0 site(s) with problems" in out
    text = (env.store.dir(SLUG) / "catalogue" / "observatory-report.md").read_text()
    assert text.startswith("# Competitor observatory:") and "Shop (shop.example)" in text and "Catalogue: 2 products in 2 collections" in text
    assert [s["domain"] for s in RP.read_brand_sites(env.store, SLUG)] == ["shop.example"]
    env.state["web"] = world(2)
    code, out = env("monitor", now=T2)
    assert code == 0 and "[major]" in out and "Fresh Bar" in out
    report = (env.store.dir(SLUG) / "catalogue" / "observatory-report.md").read_text()
    assert "## Alerts (" in report and "Next step:" in report and "Fresh Bar" in report


def test_monitor_needs_a_contact_and_knows_dry_runs_and_unknown_parts(env, monkeypatch):
    monkeypatch.delenv("OBSERVATORY_CONTACT")
    code, out = env("monitor")
    assert code == 1 and "OBSERVATORY_CONTACT" in out and env.state["web"].log == []
    monkeypatch.setenv("OBSERVATORY_CONTACT", EMAIL)
    code, out = env("monitor", "--dry-run")
    assert code == 0 and "No request is made" in out and env.state["web"].log == []
    code, out = env("monitor", "--only", "magic")
    assert code == 1 and "unknown part 'magic'" in out
    code, out = env("monitor", "--competitor", "someone-else")
    assert code == 0 and "0 site(s)" in out


def test_history_status_prune_and_report_work_on_what_was_stored(env):
    env("monitor")
    env.state["web"] = world(2)
    env("monitor", now=T2)
    code, out = env("history", "--site", "shop-example", "--product", "dark-bar")
    assert code == 0 and "24.0" in out and "16.0" in out and "on sale" in out
    code, out = env("history", "--site", "shop-example")
    assert code == 0 and out.count("complete") == 2 and "INCOMPLETE" not in out
    code, out = env("history", "--site", "shop-example", "--product", "nope")
    assert code == 1 and "No reading of 'nope'" in out
    assert env("history")[0] == 1
    code, out = env("status", now=T2)
    assert code == 0 and "1 site(s) watched" in out and "all fresh" in out and "No site is resting" in out
    code, out = env("status", now="2026-12-30T00:00:00Z")
    assert "STALE" in out
    code, out = env("prune", now=T2)
    assert code == 0 and "removed 0 old reading(s)" in out
    code, out = env("report", now=T2)
    assert code == 0 and "Competitor observatory" in out and "Black Friday" in out
    code, out = env("report", "--json", now=T2)
    assert any(e["type"] == "product_new" for e in json.loads(out[:out.rindex("]") + 1]))


def test_a_site_that_blocks_us_is_reported_with_a_warning_and_the_exit_code_says_so(env):
    web = world(1)
    web.on(BASE + "/", reply(403, "no"))
    env.state["web"] = web
    code, out = env("monitor")
    assert code == 1 and "stopped: PLATFORM_BLOCKED" in out and "::warning::shop.example pages: PLATFORM_BLOCKED" in out


# ---- the corpus -------------------------------------------------------------------------------------------------------------------------------

def test_the_youtube_corpus_is_read_ranked_stored_and_the_key_stays_out_of_every_output(env):
    class Spread(Yt):
        def __call__(self, url, params, timeout):
            status, body = super().__call__(url, params, timeout)
            if url.endswith("/videos"):
                views = {"vid1": "100", "vid2": "110", "vid3": "90", "vid4": "105", "vid5": "900"}
                body = {"items": [yt_item(int(i[3:]), views=views[i], title="97% of people quit") for i in params["id"].split(",")]}
            return status, body
    OCMD.YOUTUBE_GET = Spread(uploads=["vid1", "vid2", "vid3", "vid4", "vid5"])
    code, out = env("corpus", "--what", "youtube")
    assert code == 0 and "1 channel(s), 5 video(s) read (5 new)" in out and "8.57x" in out and "The Statistic" in out and "AIza" not in out and "SECRET" not in out
    assert (env.data / "_observatory" / "youtube_quota.json").is_file()
    assert (env.store.dir(SLUG) / "catalogue" / "youtube-outliers.md").read_text().startswith("YouTube outliers for")
    code, out = env("corpus", "--what", "outliers")
    assert "8.57x" in out
    code, out = env("corpus", "--what", "stats")
    assert "1 channel(s), 5 video(s)" in out and "shorts 5" in out
    code, out = env("corpus", "--what", "search", "--query", "people quit")
    assert code == 0 and "[youtube_public]" in out and "97% of people quit" in out
    assert env("corpus", "--what", "search")[0] == 1
    rows = {(r["source"], r["route"]): r for r in __import__("contentintel.health", fromlist=["x"]).HealthLedger.load(env.store, __import__("contentintel.sources", fromlist=["x"]).load_sources(ROOT / "sources.yaml")).rows(T1)}
    assert rows[("youtube_public", "data_api")]["last_success"] == T1


def test_the_youtube_corpus_explains_a_missing_key_a_used_up_quota_and_no_channels(env, monkeypatch):
    OCMD.YOUTUBE_GET = Yt(error=(403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}}))
    code, out = env("corpus", "--what", "youtube")
    assert code == 1 and "RATE_LIMITED" in out
    monkeypatch.delenv("YOUTUBE_API_KEY")
    code, out = env("corpus", "--what", "youtube")
    assert code == 1 and "YOUTUBE_API_KEY" in out
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    (env.tmp / "brands" / SLUG / "observatory.yaml").write_text(yaml.safe_dump({"competitors": []}))
    code, out = env("corpus", "--what", "youtube")
    assert "no YouTube channels to read" in out


def test_finding_channels_costs_searches_and_is_off_unless_asked(env):
    yt = Yt(uploads=["vid1", "vid2"])
    OCMD.YOUTUBE_GET = yt
    env("corpus", "--what", "youtube")
    assert not [c for c in yt.calls if c[0] == "search"]
    env("corpus", "--what", "youtube", "--find-channels")
    assert len([c for c in yt.calls if c[0] == "search"]) == 1                                                    # youtube.search_calls_per_run defaults to 2, one query is listed


def test_reading_a_page_through_the_reader_saves_it_notes_robots_and_makes_it_searchable(env, monkeypatch):
    jina = "Title: Snack trends\nMarkdown Content:\n# Snack trends\n\n" + "Protein snacks are growing quickly this season. " * 10
    OCMD.READER_HTTP = lambda m, u, h, t, mb: reply(200, jina)
    web = Web(robots="User-agent: *\nDisallow: /blog\n")
    env.state["web"] = web
    code, out = env("corpus", "--what", "read", "--url", "https://shop.example/blog/trends")
    assert code == 0 and "Snack trends" in out and "NOTE: robots.txt asks crawlers not to read this address" in out
    code, out = env("corpus", "--what", "search", "--query", "protein snacks growing")
    assert "Snack trends" in out
    OCMD.READER_HTTP = lambda m, u, h, t, mb: reply(200, "Title: Just a moment...\nMarkdown Content:\nChecking")
    code, out = env("corpus", "--what", "read", "--url", "https://shop.example/blog/trends")
    assert code == 1 and "PLATFORM_BLOCKED" in out
    assert env("corpus", "--what", "read")[0] == 1


# ---- the sheet --------------------------------------------------------------------------------------------------------------------------------

def test_the_competitor_tabs_are_written_typed_and_keep_what_a_person_adds(env, brands, store=None):
    env("monitor")
    env.state["web"] = world(2)
    env("monitor", now=T2)
    OCMD.YOUTUBE_GET = Yt(uploads=["vid1", "vid2", "vid3", "vid4", "vid5"])
    env("corpus", "--what", "youtube", now=T2)
    brand = brands[SLUG]
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brand, env.store, sp, T2)
    titles = [ws.title for ws in sp.worksheets()]
    assert {"Competitors", "Observatory Events", "Competitor Products"} <= set(titles)
    header, comps = read(sp.worksheet("Competitors"))
    assert comps[0]["site_id"] == "shop-example" and comps[0]["products"] == 3 and comps[0]["status"] == "monitoring" and comps[0]["platform"] == "shopify"
    assert "Decision" in header and "Notes" in header and "last_updated" in header
    _, events = read(sp.worksheet("Observatory Events"))
    assert {e["type"] for e in events} >= {"product_new", "sale_started", "offer_change"} and all(e["event_id"].startswith("ob-") for e in events)
    assert next(e for e in events if e["type"] == "product_new")["suggested_action"].startswith("A rival added a product")
    assert not any(e["type"] == "baseline_recorded" for e in events)
    _, products = read(sp.worksheet("Competitor Products"))
    dark = next(p for p in products if p["title"] == "Dark Bar")
    assert dark["price"] == 16.0 and dark["compare_at_price"] == 24.0 and dark["on_sale"] is True and dark["available"] is True and "best-sellers" in dark["collections"]
    milk = next(p for p in products if p["title"] == "Milk Bar")
    assert milk["available"] is False
    ws = sp.worksheet("Competitors")
    col = read(ws)[0].index("Decision") + 1
    ws.cells[(2, col)] = "Keep"
    sheets.update_brand_sheet(brand, env.store, sp, T2)
    assert read(sp.worksheet("Competitors"))[1][0]["Decision"] == "Keep"                              # the tool never overwrites a person's column


def test_a_problem_in_the_competitor_tabs_never_stops_the_rest_of_the_sheet(env, brands, monkeypatch):
    env("monitor")
    from contentintel.observatory import report as obs_report
    monkeypatch.setattr(obs_report, "sheet_tabs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sp = FakeSpreadsheet("sid")
    rep = sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert any("Competitor tabs skipped: boom" in n for n in rep.notes) and "Products" in [ws.title for ws in sp.worksheets()]


def test_without_any_observatory_data_the_sheet_gets_no_competitor_tabs(env, brands):
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert not ({"Competitors", "Observatory Events", "Competitor Products", "Public Corpus"} & {ws.title for ws in sp.worksheets()})



# ---- a person's decisions about discovered competitors ----------------------------------------------------------------------------------------

def candidate(key, name, domain, status="candidate", score=70, **kw):
    return {"key": key, "name": name, "domain": domain, "status": status, "decided_by": "", "first_seen": T1, "last_seen": T1, "sources": ["meta_ads"], "reasons": ["ran ads for 120 days", "4 ad variants"],
            "signals": {}, "instagram": "", "tiktok": "", "shopify": None, "shopify_note": "", "ai": None, "score": score, **kw}


@pytest.fixture
def with_candidates(env):
    book = {"version": 1, "candidates": [candidate("howdysnax", "Howdy Snax", "howdysnax.com"), candidate("mysteryname", "Mystery Name", "", status="weak", score=40),
                                         candidate("grazaco", "Graza", "graza.co", status="monitoring")]}
    RP.write_discovery(env.store, SLUG, book, T1)
    return env


def book_status(env):
    return {c["key"]: (c["status"], c.get("decided_by", "")) for c in RP.read_discovery(env.store, SLUG)["candidates"]}


def test_decide_records_a_permanent_choice_by_key_or_website_and_can_be_reset(with_candidates):
    env = with_candidates
    code, out = env("decide", "--competitor", "howdysnax", "--decision", "stop")
    assert code == 0 and "howdysnax -> stop" in out and book_status(env)["howdysnax"] == ("rejected", "person")
    code, out = env("decide", "--competitor", "https://www.howdysnax.com/", "--decision", "stop")
    assert "(already so)" in out
    env("decide", "--competitor", "howdysnax.com", "--decision", "monitor")
    assert book_status(env)["howdysnax"] == ("monitoring", "person")
    env("decide", "--competitor", "howdysnax", "--decision", "reset")
    assert book_status(env)["howdysnax"] == ("candidate", "")
    code, out = env("decide", "--competitor", "nobody", "--decision", "stop")
    assert code == 1 and "no discovered competitor matches 'nobody'" in out and "howdysnax" in out
    assert env("decide", "--competitor", "howdysnax")[0] == 1 and env("decide", "--decision", "stop")[0] == 1


def test_a_person_decision_survives_a_later_discovery_run(with_candidates):
    env = with_candidates
    env("decide", "--competitor", "howdysnax", "--decision", "stop")
    book = RP.read_discovery(env.store, SLUG)
    scores = {"howdysnax": (95, ["ran ads for 300 days"])}
    ents = {"howdysnax": {"name": "Howdy Snax", "domain": "howdysnax.com", "sources": {"meta_ads"}, "signals": {"ads": 9, "max_days": 300, "max_variants": 9, "posts": 0, "engagement": 0},
                                "instagram": "", "tiktok": "", "texts": [], "last_seen": T2}}
    from contentintel.observatory import discovery as D
    D.merge(book, ents, scores, settings(), T2, probes={"howdysnax": (True, "a Shopify store")})
    assert next(c for c in book["candidates"] if c["key"] == "howdysnax")["status"] == "rejected"


def test_undecided_candidates_are_on_the_competitors_tab_with_their_reasons_and_nothing_else_is_lost(with_candidates, brands):
    env = with_candidates
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T1)
    header, rows = read(sp.worksheet("Competitors"))
    by = {r["site_id"]: r for r in rows}
    assert set(by) == {"candidate:howdysnax", "candidate:mysteryname"} and "Decision" in header and "Notes" in header
    assert by["candidate:howdysnax"]["status"] == "candidate" and by["candidate:howdysnax"]["note"] == "score 70: ran ads for 120 days; 4 ad variants"
    assert "its website is not known yet" in by["candidate:mysteryname"]["note"]
    assert "Competitor Products" not in [w.title for w in sp.worksheets()]                      # nothing is monitored yet, so no empty product tab


def test_what_a_person_types_in_the_decision_column_is_read_back_applied_and_shown(with_candidates, brands):
    env = with_candidates
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T1)
    ws = sp.worksheet("Competitors")
    header, rows = read(ws)
    col = header.index("Decision") + 1
    for n, r in enumerate(rows, start=2):
        ws.cells[(n, col)] = {"candidate:howdysnax": "Monitor", "candidate:mysteryname": "stop"}[r["site_id"]]
    rep = sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert any("Competitor decisions read from the sheet: 2" in n for n in rep.notes)
    assert book_status(env) == {"howdysnax": ("monitoring", "person"), "mysteryname": ("rejected", "person"), "grazaco": ("monitoring", "")}
    assert {c["domain"] for c in RP.discovered_competitors(env.store, SLUG)} >= {"howdysnax.com", "graza.co"}      # a "monitor" choice is picked up by the next monitor run
    _, again = read(sp.worksheet("Competitors"))
    assert {r["site_id"]: r["status"] for r in again}["candidate:mysteryname"] == "rejected (your decision)"
    rep = sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert not any("decisions read" in n for n in rep.notes)                                    # nothing new typed, nothing re-applied


def test_a_dry_run_reads_decisions_but_saves_nothing_and_unknown_words_are_ignored(with_candidates, brands):
    env = with_candidates
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T1)
    ws = sp.worksheet("Competitors")
    header, rows = read(ws)
    col = header.index("Decision") + 1
    ws.cells[(2, col)], ws.cells[(3, col)] = "maybe later", "stop"
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2, dry_run=True)
    assert book_status(env)["howdysnax"] == ("candidate", "") and book_status(env)["mysteryname"] == ("weak", "")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert book_status(env)["howdysnax"] == ("candidate", "") and book_status(env)["mysteryname"] == ("rejected", "person")


def test_a_competitor_listed_in_the_brand_file_is_controlled_by_the_file_not_the_sheet(env, brands):
    env("monitor")
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands[SLUG], env.store, sp, T1)
    ws = sp.worksheet("Competitors")
    header, rows = read(ws)
    ws.cells[(2, header.index("Decision") + 1)] = "stop"
    rep = sheets.update_brand_sheet(brands[SLUG], env.store, sp, T2)
    assert not any("decisions read" in n for n in rep.notes) and [s["domain"] for s in RP.read_brand_sites(env.store, SLUG)] == ["shop.example"]
