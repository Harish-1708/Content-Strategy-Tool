"""Competitor discovery: finding, scoring, resolving websites, deciding, and never overriding a person."""
import types

import pytest

from contentintel import cli
from contentintel import providers as PV
from contentintel.observatory import commands as OCMD
from contentintel.observatory import discovery as D
from contentintel.observatory import report as RP
from contentintel.store import Store
from observatory_helpers import Web, client, register_store, product, settings
from conftest import ROOT

NOW = "2026-11-03T12:00:00Z"
CFG = settings()
BRAND = types.SimpleNamespace(slug="defi_snacks", name="Defi Snacks", domain="https://www.defi-snacks.com")


def item(handle, platform="meta_ads", state="kept", relevance=80, title="", text="", kind="ad", own=False):
    return {"handle": handle, "platform": platform, "state": state, "relevance": relevance, "title": title, "text": text, "kind": kind, "own_brand": own}


def fill(store, bank=None, collected=None, searches=None, benchmarks=None, seeds=None):
    store.write_ideabank(BRAND.slug, {"version": 1, "items": {f"IB-{n}": it for n, it in enumerate(bank or [], 1)}})
    for n, rec in enumerate(collected or [], 1):                      # collected data is saved in batches, as the collectors write it
        store.append_collected(BRAND.slug, {"at": NOW, "source": rec["source"], "via": "browser", "items": [{"item_id": f"c{n}", **rec}]})
    if searches:
        store.append_searches(BRAND.slug, searches)
    if benchmarks:
        store.write_benchmarks(BRAND.slug, {"version": 1, "items": {n: {"name": n, "status": "resolved", "page_name": n, "page_id": "123456789012"} for n in benchmarks}})
    if seeds:
        store.write_auto_seeds(BRAND.slug, seeds)


def search_rec(query, results, tag="competitor:x"):
    return {"provider": "serper_dev", "query": query, "tag": tag, "at": NOW, "results": [{"rank": r, "title": t, "url": u, "snippet": s} for r, (t, u, s) in enumerate(results, 1)]}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_noise_and_own_sites_are_never_candidates_and_real_ones_are_found(store):
    fill(store, bank=[item("Elev8 Foods", relevance=82, title="protein snack"), item("Elev8 Foods", relevance=78), item("Facebook"), item("Defi Snacks", own=True)],
         searches=[search_rec("best protein snacks", [("Amazon", "https://www.amazon.com/x", ""), ("Graza Olive Oil", "https://graza.co/", "squeeze bottle"),
                                                      ("Defi", "https://www.defi-snacks.com/", ""), ("Reddit", "https://reddit.com/r/snacks", "")])])
    ents = D.extract_entities(store, BRAND, NOW)
    assert set(ents) == {"elev8foods", "graza"} and ents["graza"]["domain"] == "graza.co" and ents["elev8foods"]["domain"] == ""
    assert D.is_noise("www.amazon.com") and D.is_noise("shop.amazon.com") and not D.is_noise("amazonia.example") and D.is_noise("defi-snacks.com", {"defi-snacks.com"})


def test_scores_follow_the_rubric_and_say_why():
    def ent(**sig):
        return {"key": "k", "name": "K", "domain": "", "sources": set(sig.pop("sources", [])), "signals": dict(sig), "texts": sig.pop("texts", []) if False else []}
    rel = D.score_entity(ent(kept_items=2, relevance_sum=160, sources=["meta_ads", "instagram"]), set())
    assert rel[0] == 56 and any("2 of its items were judged relevant" in w for w in rel[1]) and any("several places" in w for w in rel[1])
    assert D.score_entity(ent(removed_items=3), set())[0] == 0
    long_ad = D.score_entity(ent(max_days=120, max_variants=4), set())
    assert long_ad[0] == 20 and any("proxy" in w for w in long_ad[1])
    assert D.score_entity(ent(benchmark=1), set())[0] == 25
    assert D.score_entity(ent(search_queries=3), set())[0] == 15 and D.score_entity(ent(search_queries=1), set())[0] == 8
    e = {"key": "k", "name": "K", "domain": "", "sources": set(), "signals": {}, "texts": ["high protein snack bars with oats"]}
    s, why = D.score_entity(e, {"protein", "snack", "oats", "gluten"})
    assert s > 0 and any("market words" in w for w in why)
    assert D.score_entity({"key": "k", "name": "K", "domain": "", "sources": set(), "signals": {"kept_items": 9, "relevance_sum": 900, "benchmark": 1, "max_days": 200, "search_queries": 5, "posts": 3, "engagement": 5},
                           "texts": []}, set())[0] == 100


def test_advertisers_judged_irrelevant_by_claude_stay_out_even_if_they_advertise_a_lot(store):
    fill(store, bank=[item("Kinective Fitness Club", state="removed", relevance=5)], collected=[{"source": "meta_ads", "handle": "Kinective Fitness Club", "days_running": "184", "variants": "5"}])
    e = D.extract_entities(store, BRAND, NOW)["kinectivefitnessclub"]
    assert D.score_entity(e, set())[0] <= 20


def test_instagram_tiktok_benchmarks_and_ad_longevity_feed_the_signals(store):
    fill(store, bank=[item("@grazaolive", platform="instagram", relevance=70)],
         collected=[{"source": "instagram", "handle": "@grazaolive", "engagement_pct": "4.2", "title": "squeeze"}, {"source": "tiktok", "handle": "snackhacks", "engagement_pct": "9"},
                    {"source": "meta_ads", "handle": "Howdy Snax", "days_running": "95", "variants": "4"}], benchmarks=["Graza"],
         seeds={"keywords": ["protein snack bars", "healthy snacks"], "benchmarks": ["Graza"]})
    ents = D.extract_entities(store, BRAND, NOW)
    assert ents["grazaolive"]["instagram"] == "grazaolive" and ents["snackhacks"]["tiktok"] == "snackhacks"
    assert ents["howdysnax"]["signals"]["max_days"] == 95 and ents["graza"]["signals"]["benchmark"] == 1
    assert {"protein", "snack", "bars", "healthy", "snacks"} <= D.brand_keywords(store, BRAND)


def test_a_damaged_or_missing_data_file_does_not_crash_discovery(store, tmp_path):
    assert D.extract_entities(store, BRAND, NOW) == {}
    (tmp_path / "defi_snacks" / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "defi_snacks" / "research" / "benchmarks.json").write_text("{broken")
    assert D.extract_entities(store, BRAND, NOW) == {}


# ---- names to websites ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, domain, title, ok", [
    ("Elev8 Foods", "elev8foods.com", "", True), ("Graza", "graza.co", "Graza | olive oil", True), ("Howdy Snax", "www.howdysnax.com", "", True),
    ("Stars + Honey", "starsandhoney.com", "Stars and Honey: raw honey stars", True), ("Bar", "barista.com", "Barista beans", False),
    ("Graza", "grazing-tray.com", "Grazing trays", False), ("Defi", "defi-finance.xyz", "DeFi lending protocol", False), ("Kinective Fitness Club", "kinective.com", "Kinective Fitness Club", True)])
def test_a_website_is_only_accepted_when_the_name_matches(name, domain, title, ok):
    assert D.name_matches(name, domain, title) is ok


def test_the_first_plausible_result_wins_and_noise_is_skipped():
    results = [{"url": "https://www.amazon.com/Elev8", "title": "Elev8 Foods on Amazon"}, {"url": "https://yelp.com/elev8foods", "title": "Elev8 Foods"},
               {"url": "https://www.elev8foods.com/products", "title": "Elev8 Foods | protein"}, {"url": "https://elev8foods.co/", "title": "Elev8"}]
    assert D.pick_domain("Elev8 Foods", results) == ("elev8foods.com", "Elev8 Foods | protein")
    assert D.pick_domain("Elev8 Foods", [{"url": "https://other.com", "title": "Something else"}]) is None
    assert D.pick_domain("Elev8 Foods", [{"url": "https://elev8foods.com", "title": ""}], own={"elev8foods.com"}) is None


# ---- the book and the rules -------------------------------------------------------------------------------------------------------------------

def ents(**spec):
    out = {}
    for key, (domain, score) in spec.items():
        out[key] = {"key": key, "name": key.title(), "domain": domain, "sources": {"meta_ads"}, "signals": {}, "texts": [], "instagram": "", "tiktok": ""}
    return out


def merged(book, spec, probes=None, judged=None, cfg=CFG, elsewhere=0, now=NOW):
    e = ents(**spec)
    scores = {k: (v[1], [f"score {v[1]}"]) for k, v in spec.items()}
    return D.merge(book, e, scores, cfg, now, probes=probes, judged=judged, monitored_elsewhere=elsewhere)


def status(book):
    return {c["key"]: c["status"] for c in book["candidates"]}


def test_scores_decide_weak_candidate_or_automatic_monitoring_and_a_probe_is_required():
    book = {"candidates": []}
    notes = merged(book, {"high": ("high.com", 80), "mid": ("mid.com", 50), "low": ("low.com", 10), "nosite": ("", 90), "unprobed": ("u.com", 90)},
                   probes={"high": (True, "meta.json says it is a Shopify store"), "mid": (True, ""), "nosite": (None, "")} if False else {"high": (True, "ok"), "mid": (True, "ok")})
    assert status(book) == {"high": "monitoring", "mid": "candidate", "low": "weak", "nosite": "candidate", "unprobed": "candidate"}
    assert any("high" in n.lower() and "monitored automatically" in n for n in notes)
    c = next(c for c in book["candidates"] if c["key"] == "high")
    assert c["decided_by"] == "auto" and c["shopify"] is True and c["score"] == 80


def test_automatic_monitoring_respects_both_caps_and_the_ai_judgement():
    spec = {f"c{n}": (f"c{n}.com", 90 - n) for n in range(10)}
    probes = {k: (True, "") for k in spec}
    book = {"candidates": []}
    merged(book, spec, probes=probes)
    assert sum(s == "monitoring" for s in status(book).values()) == 6                         # auto_monitor_max
    book = {"candidates": []}
    merged(book, spec, probes=probes, elsewhere=7)
    assert sum(s == "monitoring" for s in status(book).values()) == 3                         # max_monitored 10 minus 7 already watched
    book = {"candidates": []}
    merged(book, {"x": ("x.com", 95)}, probes={"x": (True, "")}, judged={"x": {"relevant": False, "reason": "a different market", "model": "m"}})
    assert status(book) == {"x": "weak"}
    book = {"candidates": []}
    merged(book, {"x": ("x.com", 95)}, probes={"x": (True, "")}, cfg=settings(discovery__auto_monitor_max=0))
    assert status(book) == {"x": "candidate"}                                                  # automatic monitoring switched off


def test_a_persons_decision_is_permanent_and_automatic_choices_stand_until_a_person_changes_them():
    book = {"candidates": []}
    merged(book, {"a": ("a.com", 90), "b": ("b.com", 50)}, probes={"a": (True, ""), "b": (True, "")})
    assert D.apply_decisions(book, {"a": "stop", "b.com": "Monitor", "zzz": "stop", "a ": "bogus"}, NOW) == 2
    assert status(book) == {"a": "rejected", "b": "monitoring"} and {c["key"]: c["decided_by"] for c in book["candidates"]} == {"a": "person", "b": "person"}
    merged(book, {"a": ("a.com", 99), "b": ("b.com", 5)}, probes={"a": (True, ""), "b": (True, "")}, now="2026-11-10T00:00:00Z")
    assert status(book) == {"a": "rejected", "b": "monitoring"}                                # neither a great score nor a terrible one changes it
    assert D.apply_decisions(book, {"a": "monitor"}, NOW) == 1 and status(book)["a"] == "monitoring"
    assert D.apply_decisions(book, {"a": "monitor"}, NOW) == 0                                  # saying the same thing again changes nothing
    merged(book, {"x": ("x.com", 90)}, probes={"x": (True, "")})
    first_auto = next(c for c in book["candidates"] if c["key"] == "x")
    assert first_auto["status"] == "monitoring"
    merged(book, {"x": ("x.com", 20)}, probes={"x": (True, "")}, now="2026-11-20T00:00:00Z")
    assert next(c for c in book["candidates"] if c["key"] == "x")["status"] == "monitoring"     # an earlier automatic choice stands


def test_sheet_rows_hide_weak_candidates_and_flag_a_missing_website():
    book = {"candidates": []}
    merged(book, {"a": ("", 70), "b": ("b.com", 10), "c": ("c.com", 60)}, probes={"c": (False, "no public product data")})
    rows = D.rows_for_sheet(book)
    assert set(rows) == {"a", "c"} and rows["a"]["domain"] == "(needs a website)" and rows["c"]["platform"] == "website" and rows["a"]["note"].startswith("score 70")


def test_lookups_pick_the_strongest_names_without_a_website_and_never_repeat_a_failed_one():
    e = ents(**{"a": ("", 0), "b": ("b.com", 0), "c": ("", 0)})
    book = {"candidates": [{"key": "c", "lookup_failed_at": NOW}]}
    assert D.needs_lookup(book, e, CFG, 5) == [("a", "A")] and D.needs_lookup(book, e, CFG, 0) == []


# ---- the command, end to end ------------------------------------------------------------------------------------------------------------------

class Searcher(PV.Provider):
    name, label, env_var = "serper_dev", "Serper.dev", "SERPER_API_KEY"

    def __init__(self, answers):
        super().__init__("k", http=lambda *a, **k: (200, {}, None))
        self.answers, self.asked = answers, []

    def search(self, query, *, kind="web", num=10, country=None, recency=None, depth="basic"):
        self.asked.append(query)
        return [PV.Result(rank=n, title=t, url=u, snippet="") for n, (t, u) in enumerate(self.answers.get(query, []), 1)]


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OBSERVATORY_CONTACT", "research@kelson.example")
    store = Store(tmp_path)
    fill(store, bank=[item("Elev8 Foods", relevance=85, title="protein"), item("Elev8 Foods", relevance=80), item("Mystery Name", relevance=75)],
         benchmarks=["Elev8 Foods"], collected=[{"source": "meta_ads", "handle": "Elev8 Foods", "days_running": "120", "variants": "4"}])
    searcher = Searcher({"Elev8 Foods official website": [("Elev8 Foods on Amazon", "https://amazon.com/e8"), ("Elev8 Foods | Protein snacks", "https://www.elev8foods.com/")],
                         "Mystery Name official website": [("Unrelated", "https://unrelated.example/")]})
    web = Web()
    register_store(web, "elev8foods.com", [product(1, "Bar", "3.00")], {"all": [1]})
    monkeypatch.setattr(OCMD, "PROVIDERS_FACTORY", lambda: {"serper_dev": searcher})
    monkeypatch.setattr(OCMD, "CLIENT_FACTORY", lambda: client(web, settings()))

    def go(*extra):
        code = cli.main(["observatory", "discover", "--brand", "defi_snacks", "--data-dir", str(tmp_path), "--brands-dir", str(ROOT / "brands"), "--sources-config", str(ROOT / "sources.yaml"),
                         "--research-config", str(ROOT / "research.yaml"), "--settings", str(tmp_path / "none.yaml"), *extra])
        return code, capsys.readouterr().out
    go.store, go.searcher, go.web = store, searcher, web
    return go


def test_the_discover_command_finds_resolves_probes_and_starts_monitoring(run):
    code, out = run()
    assert code == 0 and "possible competitor(s)" in out
    book = RP.read_discovery(run.store, "defi_snacks")
    got = {c["key"]: c for c in book["candidates"]}
    e8 = got["elev8foods"]
    assert e8["domain"] == "elev8foods.com" and e8["shopify"] is True and e8["status"] == "monitoring" and e8["decided_by"] == "auto" and e8["score"] >= 65
    assert run.searcher.asked == ["Elev8 Foods official website", "Mystery Name official website"] or run.searcher.asked == ["Mystery Name official website", "Elev8 Foods official website"] or "Elev8 Foods official website" in run.searcher.asked
    assert got["mysteryname"]["domain"] == "" and got["mysteryname"]["lookup_failed_at"] and got["mysteryname"]["status"] in ("candidate", "weak")
    assert [c["id"] for c in RP.discovered_competitors(run.store, "defi_snacks")] == ["elev8foods-com"]


def test_a_second_run_does_not_search_or_probe_again_and_keeps_decisions(run):
    run()
    asked, probed = list(run.searcher.asked), [u for u in run.web.log if "meta.json" in u]
    code, out = run()
    assert run.searcher.asked == asked and [u for u in run.web.log if "meta.json" in u] == probed       # nothing is re-spent
    book = RP.read_discovery(run.store, "defi_snacks")
    D.apply_decisions(book, {"elev8foods": "stop"}, NOW)
    RP.write_discovery(run.store, "defi_snacks", book, NOW)
    run()
    assert {c["key"]: c["status"] for c in RP.read_discovery(run.store, "defi_snacks")["candidates"]}["elev8foods"] == "rejected"
    assert RP.discovered_competitors(run.store, "defi_snacks") == []


def test_without_search_keys_or_a_contact_discovery_still_scores_and_says_what_it_skipped(run, monkeypatch):
    monkeypatch.setattr(OCMD, "PROVIDERS_FACTORY", lambda: {})
    monkeypatch.delenv("OBSERVATORY_CONTACT")
    code, out = run()
    assert code == 0 and "no search service key" in out and not run.web.log
    got = {c["key"]: c for c in RP.read_discovery(run.store, "defi_snacks")["candidates"]}
    assert got["elev8foods"]["status"] == "candidate" and got["elev8foods"]["domain"] == "" and "mysteryname" in got
    run.store.append_searches("defi_snacks", [search_rec("elev8 foods", [("Elev8 Foods", "https://www.elev8foods.com/", "protein")])])
    code, out = run()
    assert "OBSERVATORY_CONTACT" in out and not run.web.log                                    # a site is now known but may not be read without a contact address


def test_no_lookup_and_no_probe_flags_make_no_outside_request(run):
    code, out = run("--no-lookup", "--no-probe")
    assert code == 0 and run.searcher.asked == [] and run.web.log == []


def test_candidates_without_a_website_survive_saving_and_are_not_looked_up_again(store):
    book = {"candidates": [{"key": "nosite", "name": "No Site", "domain": "", "status": "candidate", "lookup_failed_at": NOW, "score": 50, "reasons": [], "sources": [], "signals": {}}]}
    RP.write_discovery(store, "defi_snacks", book, NOW)
    again = RP.read_discovery(store, "defi_snacks")
    assert [c["key"] for c in again["candidates"]] == ["nosite"] and RP.discovered_competitors(store, "defi_snacks") == []
    assert D.needs_lookup(again, ents(nosite=("", 0)), CFG, 5) == []


def test_discovery_reads_the_real_batch_shape_and_tolerates_a_flat_record(store):
    """Regression: collected data is saved as batches. Reading it as flat records found no advertiser at all."""
    store.append_collected(BRAND.slug, {"at": NOW, "source": "meta_ads", "via": "browser", "items": [
        {"item_id": "1", "source": "meta_ads", "handle": "Elev8 Foods", "days_running": "120", "variants": "4", "title": "protein"},
        {"item_id": "2", "handle": "Howdy Snax", "days_running": "40"}, {"item_id": "3", "title": "no handle"}]})
    store.append_collected(BRAND.slug, {"at": NOW, "source": "instagram", "via": "browser", "items": [{"item_id": "4", "handle": "@graza", "engagement_pct": "3.5"}]})
    store.append_collected(BRAND.slug, {"source": "meta_ads", "handle": "Flat Brand", "days_running": "10"})
    ents = D.extract_entities(store, BRAND, NOW)
    assert set(ents) == {"elev8foods", "howdysnax", "graza", "flatbrand"}
    assert ents["elev8foods"]["signals"]["max_days"] == 120 and ents["elev8foods"]["signals"]["max_variants"] == 4 and ents["howdysnax"]["sources"] == {"meta_ads"}
    assert ents["graza"]["instagram"] == "graza" and ents["graza"]["signals"]["engagement"] == 3.5
