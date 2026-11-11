"""Update 47: the page-read summary, Sheets reads that retry, Scrapling's health, the AI judge's log folder, 'cannot run here', and the acceptance GitHub check."""
import json

import gspread
import pytest

from contentintel import connections as CONN
from contentintel import contract as K
from contentintel import health as HL
from contentintel import services as SV
from contentintel import sheets
from contentintel import sources as SRC
from contentintel.ai import gateway as G
from contentintel.observatory import acceptance_checks as AK
from contentintel.observatory import commands as OCMD
from conftest import ROOT
from observatory_helpers import Web, client, json_reply, settings as obs_settings

MADEPANTS = "\n".join([
    "Stunning and Unique Clothing Boutique",
    "[New](https://www.madepants.com/collections/new)", "* [Women](https://www.madepants.com/collections/women)", "- [Overalls](https://www.madepants.com/collections/womens-overalls)",
    "[Jumpsuits & Rompers](https://www.madepants.com/collections/womensjumpsuits)",
    "$0.00 USD [Quick view ![Image 40](https://www.madepants.com/cdn/shop/files/Mens-Canvas_{width}x.jpg?v=1)",
    "[Quick view ![Image 43](https://www.madepants.com/cdn/shop/files/Men_s-Crafter-Overalls_{width}x.jpg?v=2)"])


def test_the_menu_picture_links_and_placeholder_price_of_a_shopify_page_are_not_shown_as_content_or_prices():
    assert OCMD._main_preview(MADEPANTS) == ""                                                          # nothing here is running text
    assert OCMD._price_lines(MADEPANTS) == []                                                           # "$0.00 USD" is a placeholder and the Quick view lines are pictures


def test_real_text_and_real_prices_are_found_whether_or_not_they_are_written_as_markdown():
    text = "\n".join(["* [Shop](https://x/shop)", "## Our story",
                      "We make soft overalls from recycled cotton, cut loose so you can wear them all day long.",
                      "* Sweatpant Overalls - $89.00", "[Denim Overall Shorts](https://x/p/1) $1,249.50", "Free shipping over $75", "$12.00", "Placeholder gift item $0.00", "Supercalifragilisticexpialidocious-wearing-outstanding-comfortable-overalls",
                      "[![Image](https://x/a.jpg?price=$99)](https://x/p/2)",
                      "![Soft recycled cotton overalls made to be worn all day long](https://x/hero.jpg)", "![Overalls on sale for only $45 today](https://x/sale.jpg)",
                      "[Shop](https://x/1) [Women](https://x/2) [Men](https://x/3) [Kids](https://x/4) [Sale](https://x/5) [Gift cards](https://x/6) [About us](https://x/7) [Contact us](https://x/8)"])
    assert OCMD._main_preview(text) == "We make soft overalls from recycled cotton, cut loose so you can wear them all day long."
    assert OCMD._price_lines(text) == ["Sweatpant Overalls $89.00", "Denim Overall Shorts $1,249.50", "Free shipping over $75"]      # a bare price has no name, $0.00 is a placeholder, a price inside an address is not a price


def page(url="https://madepants.com/"):
    return {"title": "Madepants", "url": url, "engine": "core", "robots_note": "", "text": MADEPANTS}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import shutil

    from contentintel import cli
    monkeypatch.chdir(tmp_path)
    for name in ("observatory.yaml", "research.yaml", "sources.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "brands", tmp_path / "brands")
    monkeypatch.setenv("OBSERVATORY_CONTACT", "research@kelson.example")

    def run(*args):
        return cli.main(["observatory", *args, "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands")])
    return run


def test_a_theme_that_fills_prices_in_with_javascript_gets_its_real_prices_from_the_stores_public_product_list(world, monkeypatch, capsys):
    web = Web()
    web.on("https://madepants.com/products.json?limit=8", json_reply({"products": [
        {"id": 1, "handle": "canvas-overalls", "title": "Men's Canvas Overalls", "variants": [{"id": 11, "title": "S", "price": "89.00", "available": True}, {"id": 12, "title": "XL", "price": "99.00", "available": True}]},
        {"id": 2, "handle": "denim-dungarees", "title": "Distressed Denim Dungarees", "variants": [{"id": 21, "title": "M", "price": "120.00", "available": True}]},
        {"id": 3, "handle": "free-gift", "title": "Gift", "variants": []}]}))
    monkeypatch.setattr(OCMD, "CLIENT_FACTORY", lambda: client(web, obs_settings()))
    monkeypatch.setattr(OCMD, "_read_url", lambda *a, **k: (page(), ""))
    assert world("corpus", "--what", "read", "--url", "https://madepants.com/", "--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "no running text" in out and "$0.00" not in out and "Quick view" not in out
    assert "products and prices from the store's public product list" in out and "Men's Canvas Overalls 89.00-99.00" in out and "Distressed Denim Dungarees 120.00" in out and "Gift" not in out


def test_when_the_product_list_cannot_be_read_the_summary_says_so_and_the_read_still_succeeds(world, monkeypatch, capsys):
    monkeypatch.setattr(OCMD, "CLIENT_FACTORY", lambda: client(Web(), obs_settings()))
    monkeypatch.setattr(OCMD, "_read_url", lambda *a, **k: (page(), ""))
    assert world("corpus", "--what", "read", "--url", "https://madepants.com/", "--brand", "swoveralls") == 0
    assert "no prices in the page text (the theme fills them in with JavaScript) and the store's public product list could not be read" in capsys.readouterr().out


def test_a_page_read_for_several_brands_is_described_once(world, monkeypatch, capsys):
    monkeypatch.setattr(OCMD, "CLIENT_FACTORY", lambda: client(Web(), obs_settings()))
    monkeypatch.setattr(OCMD, "_read_url", lambda *a, **k: (page(), ""))
    assert world("corpus", "--what", "read", "--url", "https://madepants.com/") == 0
    out = capsys.readouterr().out
    assert out.count("main content (menus and short labels skipped)") == 1 and out.count("characters read through") == 3
    assert out.count("(the same page as above; recorded for") == 2


# ---- Sheets reads that wait out a 429 -----------------------------------------------------------------------------------------------------------

def api_error(code):
    class Response:
        status_code = code
        text = json.dumps({"error": {"code": code, "message": "Quota exceeded for quota metric 'Read requests'", "status": "RESOURCE_EXHAUSTED"}})

        def json(self):
            return json.loads(self.text)
    return gspread.exceptions.APIError(Response())


class Flaky:
    """A worksheet or spreadsheet call that is refused with 429 a few times and then answers."""
    def __init__(self, failures, answer):
        self.failures, self.answer, self.calls = failures, answer, 0

    def __call__(self, *args):
        self.calls += 1
        if self.calls <= self.failures:
            raise api_error(429)
        return self.answer


class Sheet:
    def __init__(self, failures):
        self.title = "Competitors"
        self.listing, self.values = Flaky(failures, None), Flaky(failures, [["Name", "Decision"], ["Madepants", "monitor"]])
        self.listing.answer = [self]

    def worksheets(self):
        return self.listing()

    def worksheet(self, name):
        return self

    def get_all_values(self):
        return self.values()


class Notes:
    def __init__(self):
        self.notes = []


def test_the_competitor_decisions_are_read_even_when_google_refuses_the_first_reads(monkeypatch):
    from contentintel.observatory import report as obs_report
    waits, got = [], {}
    monkeypatch.setattr(sheets, "_SLEEP", waits.append)
    monkeypatch.setattr(obs_report, "apply_sheet_decisions", lambda values, store, slug, now, save: got.update(values=values) or 1)
    sheet, notes = Sheet(2), Notes()
    sheets._read_competitor_decisions(sheet, type("B", (), {"slug": "swoveralls"})(), None, "2026-11-11T10:00:00Z", notes, dry_run=False)
    assert got["values"] == [["Name", "Decision"], ["Madepants", "monitor"]] and notes.notes == ["Competitor decisions read from the sheet: 1"]      # no "not read" note
    assert waits[:2] == [20, 65] and sheet.listing.calls == 3 and sheet.values.calls == 3                                                          # waited, then read


def test_the_controls_are_read_even_when_google_refuses_the_first_reads(monkeypatch, tmp_path):
    waits, got = [], {}
    monkeypatch.setattr(sheets, "_SLEEP", waits.append)
    monkeypatch.setattr(SV, "apply_sheet_controls", lambda values, settings, data_dir, now, save, sheet_id: got.update(values=values) or ([], []))
    monkeypatch.setattr(SV, "load_settings", lambda *a, **k: {})
    sheet, notes = Sheet(1), Notes()
    sheet.title = SV.CONTROLS_TAB
    sheets._read_controls(sheet, type("B", (), {"slug": "swoveralls"})(), type("S", (), {"data_dir": str(tmp_path)})(), "2026-11-11T10:00:00Z", notes, dry_run=False)
    assert got["values"] and not notes.notes and waits == [20, 20]                               # the tab list and the tab itself were each refused once


def test_a_read_that_stays_refused_is_still_reported_not_raised(monkeypatch):
    monkeypatch.setattr(sheets, "_SLEEP", lambda s: None)
    sheet, notes = Sheet(99), Notes()
    sheets._read_competitor_decisions(sheet, type("B", (), {"slug": "swoveralls"})(), None, "2026-11-11T10:00:00Z", notes, dry_run=False)
    assert len(notes.notes) == 1 and notes.notes[0].startswith("Competitor decisions not read:")


# ---- the AI judge's gateway log, Scrapling's health ---------------------------------------------------------------------------------------------

def test_the_gateway_log_folder_is_made_when_it_does_not_exist(tmp_path):
    class Proc:
        def poll(self):
            return None
    side = G.Sidecar(str(tmp_path / "gw"), {}, popen=lambda *a, **k: Proc(), http=None, sleep=lambda s: None)
    side.start({}, 3001, tmp_path / "ai-work" / "deeper" / "gateway.log")
    assert (tmp_path / "ai-work" / "deeper" / "gateway.log").is_file()                                   # discover starts the gateway before `ai check` has made ai-work


def crawl_world(monkeypatch, tmp_path, outcome):
    from contentintel.observatory import worker_client as wc
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "data"))                                    # the health file is this test's own, whatever an earlier test set

    def fake(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(wc, "crawl_site", fake)
    monkeypatch.setattr(wc, "discover_feeds", fake)
    return SV.load_settings(str(ROOT / "observatory.yaml"), str(tmp_path))


def scrapling_score():
    return CONN.ConnectionHealth().summary("scrapling")


def test_a_crawl_that_returns_pages_is_recorded_as_scrapling_working_and_a_feed_search_too(monkeypatch, tmp_path):
    st = crawl_world(monkeypatch, tmp_path, {"pages": [{"url": "https://madepants.com/"}], "skipped": [], "stopped": "page limit reached"})
    SV.scrapling_crawl(st, "https://madepants.com/", "content_research", contact="a@b.example", python="/x/python", now="2026-11-11T10:00:00Z")
    assert scrapling_score()["attempts"] == 1 and scrapling_score()["score"] == 100
    crawl_world(monkeypatch, tmp_path, [{"url": "https://madepants.com/feed", "how": "guess"}])
    SV.discover_feeds(st, "https://madepants.com/", "content_research", contact="a@b.example", python="/x/python", now="2026-11-11T10:00:00Z")
    assert scrapling_score()["attempts"] == 2


def test_a_crawl_that_got_no_page_because_of_the_site_is_not_counted_against_scrapling(monkeypatch, tmp_path):
    st = crawl_world(monkeypatch, tmp_path, {"pages": [], "skipped": [], "stopped": "the site refused"})
    SV.scrapling_crawl(st, "https://madepants.com/", "content_research", contact="a@b.example", python="/x/python", now="2026-11-11T10:00:00Z")
    assert scrapling_score()["attempts"] == 0


@pytest.mark.parametrize("code, counted", [(K.PLATFORM_BLOCKED, 0), (K.RATE_LIMITED, 0), (K.NOT_FOUND, 0), (K.NEED_SETUP, 0), (K.UPSTREAM_BROKEN, 1), (K.TIMEOUT, 1)])
def test_only_scrapling_actually_failing_lowers_its_health_not_a_sites_refusal_or_a_missing_environment(monkeypatch, tmp_path, code, counted):
    st = crawl_world(monkeypatch, tmp_path, K.Failure(code, "x"))
    for call in (lambda: SV.scrapling_crawl(st, "https://madepants.com/", "content_research", contact="a@b.example", python="/x/python", now="n"),
                 lambda: SV.discover_feeds(st, "https://madepants.com/", "content_research", contact="a@b.example", python="/x/python", now="n")):
        with pytest.raises(K.Failure):
            call()
    assert scrapling_score()["attempts"] == counted * 2


# ---- 'cannot run here' ----------------------------------------------------------------------------------------------------------------------------

NO_OPENCLI, HAS_OPENCLI = (lambda b: None), (lambda b: "/bin/opencli")


def test_a_route_that_needs_opencli_is_cannot_run_here_not_unverified_where_opencli_is_not_installed():
    assert HL.cannot_run_here("agent_reach_research", "instagram", {}, NO_OPENCLI) and not HL.cannot_run_here("agent_reach_research", "instagram", {}, HAS_OPENCLI)
    assert not HL.cannot_run_here("agent_reach_research", "github", {}, NO_OPENCLI) and not HL.cannot_run_here("meta_ads", "browser", {}, NO_OPENCLI)
    assert len(HL.NEEDS_YOUR_MACHINE) == 10


def test_the_health_report_marks_those_routes_cannot_run_here_and_leaves_every_other_route_as_it_was(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLI_BIN", "/no/such/opencli")
    reg = SRC.load_sources(ROOT / "sources.yaml")
    from contentintel.store import Store
    led = HL.HealthLedger.load(Store(str(tmp_path)), reg)
    rows = {(r["source"], r["route"]): r for r in led.rows("2026-11-11T10:00:00Z")}
    for key in HL.NEEDS_YOUR_MACHINE:
        assert rows[key]["status"] == "cannot run here" and HL.NEEDS_YOUR_MACHINE_NOTE in rows[key]["limits"], key
    assert rows[("meta_ads", "browser")]["status"] == "unverified" and rows[("agent_reach_research", "github")]["status"] == "unverified"
    assert "cannot run here" in HL.format_report(list(rows.values())) and "agent_reach_research / instagram" in HL.format_report(list(rows.values()))
    monkeypatch.setenv("OPENCLI_BIN", "/bin/sh")                                                         # on a machine that has OpenCLI nothing is hidden
    rows = {(r["source"], r["route"]): r for r in led.rows("2026-11-11T10:00:00Z")}
    assert rows[("agent_reach_research", "instagram")]["status"] == "unverified"


# ---- the acceptance GitHub check ------------------------------------------------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, *answers):
        self.answers, self.queries = list(answers), []

    def call(self, argv):
        self.queries.append(argv[1])
        return self.answers.pop(0) if self.answers else []


def github_check(client_):
    return AK.agent_reach_checks(SV.load_settings(str(ROOT / "observatory.yaml"), "data"), "a@b.example", "2026-11-11T10:00:00Z", environ={}, cli=lambda ch: type("C", (), {"available": lambda s: False})(),
                                 github=client_, web_client=None)[("agent_reach_research", "github")]


def test_the_acceptance_github_check_tries_a_second_broad_query_before_calling_the_route_broken():
    rows = [{"title": "acme/widgets", "url": "https://github.com/acme/widgets"}]
    c = GitHubClient([], rows)
    items, detail = github_check(c)()
    assert items == 1 and c.queries == ["agent reach", "python"] and "'python'" in detail
    c = GitHubClient(rows)
    github_check(c)()
    assert c.queries == ["agent reach"]                                                                  # a first answer is enough


def test_the_acceptance_github_check_says_why_when_two_broad_queries_find_nothing():
    c = GitHubClient([], [])
    with pytest.raises(K.Failure) as seen:
        github_check(c)()
    assert seen.value.code == K.UPSTREAM_BROKEN and "found no repository for 'agent reach' or 'python'" in str(seen.value) and "GITHUB_TOKEN" in str(seen.value)


def test_the_acceptance_workflow_passes_the_jobs_own_github_token():
    import yaml
    wf = yaml.safe_load(open(ROOT / ".github" / "workflows" / "acceptance.yml", encoding="utf-8"))
    step = next(s for job in wf["jobs"].values() for s in job["steps"] if s.get("name") == "Check each route")
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_the_ai_workflow_prints_the_raw_github_models_answer_before_the_live_check_and_never_fails_the_run_for_it():
    import yaml
    wf = yaml.safe_load(open(ROOT / ".github" / "workflows" / "ai-gateway.yml", encoding="utf-8"))
    steps = wf["jobs"]["live"]["steps"]
    names = [s.get("name", "") for s in steps]
    probe = steps[names.index("GitHub Models raw probe (diagnostic, shows exactly what GitHub answers)")]
    assert names.index(probe["name"]) < names.index("Live check (LIVE result)") and probe["continue-on-error"] is True
    assert "'github'" in probe["if"] and probe["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    run = probe["run"]
    assert "curl -sS -i" in run and "https://models.github.ai/inference/chat/completions" in run and "2026-03-10 2022-11-28" in run and "https://models.github.ai/catalog/models" in run and "openai/gpt-4.1-mini" in run


def test_a_route_that_needs_opencli_and_has_really_passed_acceptance_is_healthy_even_here(monkeypatch, tmp_path):
    from test_health import ledger, passing
    monkeypatch.setenv("OPENCLI_BIN", "/no/such/opencli")
    led = ledger(tmp_path)
    assert led.status("agent_reach_research", "instagram", "2026-11-02T10:00:00Z") == "cannot run here"
    passing(led, source="agent_reach_research", route="instagram")                                       # a run on the owner's own machine proved it
    assert led.status("agent_reach_research", "instagram", "2026-11-02T10:00:00Z") == "healthy"
