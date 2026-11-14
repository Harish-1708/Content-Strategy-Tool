"""Update 63: why the benchmark page finder did nothing in collect #56 (Serper's key never reached the collect run, and nothing said so), a mode that runs only the finder, and a sturdier page-id reader."""
import re

import pytest
import yaml

from contentintel import cli
from contentintel import main_page as MP
from contentintel.store import Store

from conftest import ROOT
from test_update62 import DUDE_RESULTS, reviewer, serp

WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)


# ---- the collect workflow -------------------------------------------------------------------------------------------------------------------------------

def test_the_collect_run_is_given_serpers_key_and_reports_it_among_the_secrets_it_can_see():
    step = next(s for s in WORKFLOW["jobs"]["collect"]["steps"] if s.get("id") == "collect")
    assert step["env"]["SERPER_API_KEY"] == "${{ secrets.SERPER_API_KEY }}"                                   # under its own name; without it the finder had no search and did nothing
    assert "SERPER_API_KEY" in cli.EXTRA_SECRETS


def test_a_benchmarks_mode_runs_only_the_finder_and_needs_no_browser_and_no_ticked_source():
    inputs = WORKFLOW[True]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"][:3] == ["plan", "seeds", "benchmarks"]
    assert re.search(r"benchmarks\)\s+args=\(--find-pages\) ;;", WORKFLOW_TEXT)
    assert '[ "$MODE" != "benchmarks" ]' in WORKFLOW_TEXT                                                      # no 'No collector was ticked' stop for it
    assert WORKFLOW_TEXT.count("inputs.mode != 'plan' && inputs.mode != 'seeds' && inputs.mode != 'benchmarks'") == 2      # the browser is not installed for it
    assert cli.build_parser().parse_args(["collect", "--find-pages"]).find_pages is True and cli.build_parser().parse_args(["collect"]).find_pages is False


# ---- the finder says whether it is on ------------------------------------------------------------------------------------------------------------------

class Claude:
    def json_task(self, *a, **k):
        return {}


class Serper:
    def search(self, *a, **k):
        return []


def test_the_finder_says_why_it_is_off_and_is_on_only_with_a_search_and_an_ai_client(monkeypatch):
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: {})
    finder, why = cli._page_finder_status(Claude(), None)
    assert finder is None and "SERPER_API_KEY is not set in this workflow, or search is switched off for discovery" in why
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: {"serper_dev": Serper()})
    finder, why = cli._page_finder_status(None, None)
    assert finder is None and "ANTHROPIC_API_KEY is not set here" in why and cli._page_finder_status(object(), None)[0] is None
    finder, why = cli._page_finder_status(Claude(), None)
    assert callable(finder) and why == "" and callable(cli._page_finder(Claude(), None))


def run_cli(monkeypatch, capsys, tmp_path, *extra):
    (tmp_path / "s.yaml").write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    code = cli.main(["collect", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(ROOT / "brands"), "--sources-config", str(tmp_path / "s.yaml"), *extra])
    return code, capsys.readouterr().out


def test_a_real_collect_prints_whether_the_finder_is_on_before_it_starts(monkeypatch, capsys, tmp_path):
    from contentintel import collectors as C
    monkeypatch.setattr(C, "run_collect", lambda brands, store, **kw: C.CollectResult(run_id="r", now="2026-11-15T10:00:00Z", dry_run=True))
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _, out = run_cli(monkeypatch, capsys, tmp_path, "--sources", "meta_ads")
    assert "Benchmark page finder: OFF: no Serper search is available to this run" in out
    monkeypatch.setattr(cli, "_search_providers", lambda args, purpose: {"serper_dev": Serper()})
    _, out = run_cli(monkeypatch, capsys, tmp_path, "--sources", "meta_ads")
    assert "Benchmark page finder: ON (Serper search, then a reviewer" in out


# ---- the benchmarks mode ---------------------------------------------------------------------------------------------------------------------------------

def seed_state(tmp_path):
    from test_collectors import _bench_state
    Store(tmp_path / "data").write_benchmarks("swoveralls", _bench_state(**{"Chobani": ("ambiguous", "auto", "2026-11-04T10:00:00Z"), "Dude Wipes": ("unresolved", "auto", "2026-11-14T10:00:00Z")}))
    return Store(tmp_path / "data")


def settled(name):
    return {"status": "resolved", "page_id": "424242424", "page_name": name, "page_likes": 0, "candidates": [], "reason": "found with Serper and confirmed as the main official page by the AI reviewer: the brand's own page"}


def test_the_benchmarks_mode_looks_for_the_unsettled_pages_files_what_it_finds_and_collects_nothing(monkeypatch, capsys, tmp_path):
    from contentintel import collectors as C
    store, asked = seed_state(tmp_path), []
    monkeypatch.setattr(C, "run_collect", lambda *a, **k: pytest.fail("the benchmarks mode must not collect"))
    monkeypatch.setattr(cli, "_page_finder_status", lambda claude, args: (lambda name: asked.append(name) or settled(name), ""))
    code, out = run_cli(monkeypatch, capsys, tmp_path, "--find-pages")
    assert code == 0 and sorted(asked) == ["Chobani", "Dude Wipes"] and "Benchmark page finder: ON" in out
    assert "--- swoveralls: benchmark pages looked for ---" in out and "Chobani: resolved - found with Serper and confirmed as the main official page" in out and "Dude Wipes: resolved" in out
    state = store.read_benchmarks("swoveralls")
    assert state["items"]["chobani"]["status"] == "resolved" and state["items"]["chobani"]["page_id"] == "424242424" and state["items"]["chobani"]["source"] == "serper_review"
    code, out = run_cli(monkeypatch, capsys, tmp_path, "--find-pages")
    assert asked == ["Chobani", "Dude Wipes"] and "nothing was looked for: every benchmark is settled" in out                         # a second run has nothing left to look for


def test_the_benchmarks_mode_with_the_finder_off_says_why_leaves_the_state_alone_and_warns(monkeypatch, capsys, tmp_path):
    store = seed_state(tmp_path)
    before = store.read_benchmarks("swoveralls")
    monkeypatch.setattr(cli, "_page_finder_status", lambda claude, args: (None, "no Serper search is available to this run (SERPER_API_KEY is not set in this workflow, or search is switched off for discovery)"))
    code, out = run_cli(monkeypatch, capsys, tmp_path, "--find-pages")
    assert code == 0 and "Benchmark page finder: OFF: no Serper search is available" in out and "nothing was looked for: the finder is off" in out
    assert "::warning::The benchmark page finder is off: no Serper search is available" in out and store.read_benchmarks("swoveralls") == before


# ---- the page id ------------------------------------------------------------------------------------------------------------------------------------------

def test_an_address_that_already_holds_the_page_id_needs_no_page_read():
    results = serp(("https://www.facebook.com/people/DUDE-Wipes/100064123456789/", "DUDE Wipes", "Flushable wipes"), ("https://www.facebook.com/profile.php?id=100064123456789&sk=about", "DUDE Wipes", "dup"),
                   ("https://www.facebook.com/profile.php?id=5", "too short an id", "x"))
    got = MP.candidates_from(results)
    assert len(got) == 1 and got[0]["id"] == "100064123456789" and got[0]["url"] == "https://www.facebook.com/profile.php?id=100064123456789"
    reads = []
    out = MP.lookup_main_page("DUDE Wipes", lambda q: results, reviewer("DUDEWipes", reason="the brand's own page"), lambda url: reads.append(url) or "")
    assert out["status"] == "resolved" and out["page_id"] == "100064123456789" and reads == []


def test_the_reader_tries_the_page_its_mobile_address_and_its_basic_address_and_says_what_each_did():
    asked = []

    def get_html(url):
        asked.append(url)
        if "m.facebook.com" in url:
            return '<meta property="al:android:url" content="fb://page/?id=229927120383343">'
        raise ConnectionError(f"blocked {url}")

    class Serper:
        def search(self, query, num=10):
            return [MP.Result(rank=n, title=r["title"], url=r["url"], snippet=r["snippet"]) for n, r in enumerate(DUDE_RESULTS, 1)] if hasattr(MP, "Result") else []

    class Claude:
        def json_task(self, system, user, max_tokens=0):
            return {"choice": "https://www.facebook.com/dudewipes", "is_main": True, "regional": False, "reason": "the brand's own page"}
    from contentintel.providers import Result
    Serper.search = lambda self, query, num=10: [Result(rank=n, title=r["title"], url=r["url"], snippet=r["snippet"]) for n, r in enumerate(DUDE_RESULTS, 1)]
    out = MP.make_finder(Serper(), Claude(), get_html)("Dude Wipes")
    assert out["status"] == "resolved" and out["page_id"] == "229927120383343" and asked == ["https://www.facebook.com/dudewipes", "https://m.facebook.com/dudewipes"]
    asked.clear()

    def blocked(url):
        asked.append(url)
        raise ConnectionError("blocked")
    out = MP.make_finder(Serper(), Claude(), blocked)("Dude Wipes")
    assert out["status"] == "ambiguous" and "page id could not be read" in out["reason"]
    assert "tried www.facebook.com: ConnectionError; m.facebook.com: ConnectionError; mbasic.facebook.com: ConnectionError" in out["reason"] and len(asked) == 3
    empty = MP.make_finder(Serper(), Claude(), lambda url: "<html>login</html>")("Dude Wipes")
    assert "www.facebook.com: no page id in the page" in empty["reason"]


def test_every_form_the_page_id_takes_in_a_pages_html_is_read():
    for html in ('{"delegate_page_id":"229927120383343"}', '{"profile_id":"229927120383343"}', '<a href="https://www.facebook.com/profile.php?id=229927120383343">'):
        assert MP.read_page_id(html) == "229927120383343"


def test_the_documents_name_the_mode_and_the_key():
    guide = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    for phrase in ("## Update 63", "Benchmark page finder: ON", "SERPER_API_KEY", "mode `benchmarks`", "collect #56"):
        assert phrase in guide, phrase
