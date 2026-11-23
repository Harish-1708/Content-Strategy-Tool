"""assets: results are scored against the query and off-target ones dropped, other queries are derived from the brand, the brand's own site is the fallback, and the run says what happened."""
import json

from contentintel import assets_find as AF
from contentintel import cli
from contentintel import research_tools as RT
from contentintel.brands import load_brand
from contentintel.store import Store

NOW = "2026-11-19T12:00:00Z"


def ov_item(title, n=1):
    return {"id": f"id{n}", "title": title, "url": f"https://img.example/{n}.jpg", "thumbnail": "", "foreign_landing_url": f"https://p.example/{n}", "creator": "c", "license": "by", "license_version": "2.0",
            "license_url": "", "attribution": f'"{title}" by c (CC BY 2.0)', "source": "flickr", "width": 800, "height": 600}


class World:
    """A scripted internet: Openverse answers per query (looked up in `by_query`), Commons answers empty, the brand's front page is `site`."""
    def __init__(self, by_query=None, site=None):
        self.by_query, self.site, self.urls = by_query or {}, site, []

    def __call__(self, url, *a, **kw):
        self.urls.append(url)
        if "api.openverse" in url:
            import urllib.parse as up
            q = up.parse_qs(up.urlparse(url).query).get("q", [""])[0]
            return 200, json.dumps({"results": [ov_item(t, i) for i, t in enumerate(self.by_query.get(q, []))]})
        if "commons.wikimedia" in url:
            return 200, json.dumps({"query": {"pages": {}}})
        if self.site is not None and url.startswith("https://swoveralls"):
            return 200, self.site
        return 404, ""


def run(tmp_path, query, world, *extra, brand="swoveralls"):
    args = cli.build_parser().parse_args(["assets", "--query", query, *(["--brand", brand] if brand else []), "--data-dir", str(tmp_path), *extra])
    store = Store(tmp_path)
    code = RT.run_assets(args, store, NOW, get=world, post=lambda u, d: (500, ""), environ={})
    return code, store


def test_a_picture_is_kept_only_when_its_own_title_mentions_the_words_of_the_query():
    assets = [{"title": "Sweatpant overalls on a rack", "attribution": ""}, {"title": "A bridge in Indonesia", "attribution": "by someone"}, {"title": "Overalls", "attribution": ""}, {"title": "", "attribution": ""}]
    kept, dropped = AF.filter_relevant(assets, "sweatpant overalls")
    assert [a["title"] for a in kept] == ["Sweatpant overalls on a rack", "Overalls"] and [d[0] for d in dropped][0] == "A bridge in Indonesia" and len(dropped) == 2
    assert "off-target: its title and credit line mention 0% of the words of 'sweatpant overalls'" in dropped[0][1] and kept[0]["relevance"] == 1.0 and kept[1]["relevance"] == 0.5
    assert AF.relevance({"title": "anything"}, "the and") == 0.0 and AF.words("The Sweatpant-Overalls, new!") == ["sweatpant", "overalls"]


def test_off_target_results_are_dropped_not_saved_and_the_report_says_why(tmp_path, capsys):
    world = World({"sweatpant overalls": ["Drag look with sweatpants", "A bridge in Indonesia"]})
    code, store = run(tmp_path, "sweatpant overalls", world, brand="")
    out = capsys.readouterr().out
    assert code == 0 and "tried 'sweatpant overalls' on openverse: 2 returned, 0 kept, 2 dropped" in out and "dropped 'A bridge in Indonesia': off-target" in out
    assert "none usable found for 'sweatpant overalls'" in out and "nothing was saved" in out
    assert store.read_extra("swoveralls", "assets") is None


def test_when_the_brand_name_gives_nothing_other_queries_come_from_its_own_catalogue_and_competitors(tmp_path, capsys):
    brand = load_brand("swoveralls", "brands")
    research = {"ads": {"keywords": ["sweatpant overalls"], "benchmark_pages": {"Allbirds": "skip", "Cotopaxi": "skip"}}, "topics": ["lounge wear"], "benchmark_pages": ["Madepants", {"name": "Swoveralls"}], "competitors": ["Liquid Death"]}
    queries = AF.derived_queries("swoveralls", brand, Store(tmp_path), research)
    assert queries == ["sweatpant overalls", "lounge wear", "Allbirds", "Cotopaxi"] and "Swoveralls" not in queries                                   # the brand's own words first, then competitors, at most four, never its own name
    assert AF.derived_queries("sweatpant overalls", brand, Store(tmp_path), research)[0] == "lounge wear"                                              # the query itself is never tried twice
    world = World({"swoveralls": [], "sweatpant overalls": ["Sweatpant overalls navy on a model"]})
    (tmp_path / "brands").mkdir()
    code, store = run(tmp_path, "swoveralls", world)
    out = capsys.readouterr().out
    assert code == 0 and "tried 'swoveralls' on openverse: 0 returned" in out and "found through 'sweatpant overalls'" in out
    kept = store.read_extra("swoveralls", "assets")["assets"]
    assert kept[0]["title"] == "Sweatpant overalls navy on a model" and kept[0]["query"] == "swoveralls"


PAGE = ('<html><head><meta property="og:image" content="/cdn/hero.jpg"></head><body><img src="/cdn/sweatpant-overalls-navy.jpg" alt="Navy sweatpant overalls"><img src="/cdn/sprite.png" alt="">'
        '<img src="data:image/gif;base64,AAAA" alt="x"><img src="/cdn/unrelated.jpg" alt="A cat"><img src="/cdn/logo.svg" alt="Swoveralls"></body></html>')


def test_the_brands_own_site_is_the_fallback_and_its_pictures_are_marked_brand_owned(tmp_path, capsys):
    own = AF.site_media(lambda url: PAGE, "swoveralls.com", "sweatpant overalls")
    assert [a["url"] for a in own] == ["https://swoveralls.com/cdn/hero.jpg", "https://swoveralls.com/cdn/sweatpant-overalls-navy.jpg"] and all(a["license"] == "brand-own" and a["via"] == "brand_site" for a in own)
    world = World({}, site=PAGE)
    code, store = run(tmp_path, "swoveralls", world)
    out = capsys.readouterr().out
    assert code == 0 and "tried 'swoveralls' on brand site:" in out and "from brand site" in out
    saved = store.read_extra("swoveralls", "assets")["assets"]
    assert saved and all(a["license"] == "brand-own" for a in saved)
    broken = World({}, site=None)
    code2, store2 = run(tmp_path / "x", "swoveralls", broken)
    assert "none usable found" in capsys.readouterr().out and store2.read_extra("swoveralls", "assets") is None                    # no site either: it says so and saves nothing


def test_an_empty_topic_is_refused_instead_of_fetching_random_pictures(tmp_path, capsys):
    world = World({"": ["Random thing"]})
    code, store = run(tmp_path, "  ", world)
    assert code == 1 and "the topic is empty" in capsys.readouterr().out and world.urls == [] and store.read_extra("swoveralls", "assets") is None


def test_a_relevant_first_result_is_used_directly_and_audio_is_not_scored_by_words(tmp_path, capsys):
    world = World({"cotton overalls": ["Organic cotton overalls hanging", "Cotton field"]})
    code, store = run(tmp_path, "cotton overalls", world)
    out = capsys.readouterr().out
    assert code == 0 and "2 returned, 2 kept, 0 dropped" in out and "found through" not in out and len(store.read_extra("swoveralls", "assets")["assets"]) == 2
    kept, report = AF.find_usable(World({"sound": ["Whoosh"]}), "sound", "audio")
    assert kept == [] or all(a["kind"] == "audio" for a in kept)
