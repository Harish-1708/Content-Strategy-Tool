"""GA4 and Search Console, ready for the day access is granted; public Serper ranks; and never an invented number."""
from datetime import date
from pathlib import Path


from contentintel import analytics as AN
from contentintel import cli
from contentintel import research_tools as RT
from contentintel.store import Store


class Reply:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Session:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def post(self, url, json=None, timeout=0):
        self.calls.append((url, json))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _brand(tmp_path, text):
    d = tmp_path / "brands" / "shop"
    d.mkdir(parents=True)
    (d / "analytics.yaml").write_text(text, encoding="utf-8")
    return str(tmp_path / "brands")


def test_the_brand_file_is_read_and_a_missing_or_damaged_one_means_not_configured(tmp_path):
    root = _brand(tmp_path, "ga4_property_id: ' 12-3456 '\nsearch_console_site: sc-domain:example.com\ndomain: Example.com\nrank_keywords: [bib overalls, '', sweatpant overalls]\n")
    assert AN.load_config(root, "shop") == {"ga4_property_id": "123456", "search_console_site": "sc-domain:example.com", "domain": "example.com", "rank_keywords": ["bib overalls", "sweatpant overalls"]}
    assert AN.load_config(root, "nobody") == {"ga4_property_id": "", "search_console_site": "", "domain": "", "rank_keywords": []}
    broken = _brand(tmp_path / "b", "ga4_property_id: [unclosed\n")
    assert AN.load_config(broken, "shop")["ga4_property_id"] == ""
    for shipped in ("swoveralls", "duderobe", "defi_snacks"):
        cfg = AN.load_config("brands", shipped)
        assert cfg == {"ga4_property_id": "", "search_console_site": "", "domain": "", "rank_keywords": []}          # nothing is guessed: the owner fills these in when access exists


def test_without_access_google_says_403_and_the_report_has_no_numbers_only_needs_input_lines():
    session = Session(Reply(403, {"error": {"message": "forbidden"}}))
    ga4 = AN.ga4_report(session, "123456", "tool@project.iam.gserviceaccount.com")
    gsc = AN.search_console_report(session, "sc-domain:example.com", "tool@project.iam.gserviceaccount.com", today=date(2026, 11, 16))
    for part in (ga4, gsc):
        assert part["status"] == AN.NO_ACCESS and part["rows"] is None and "tool@project.iam.gserviceaccount.com" in part["reason"] and "must add it as a Viewer (GA4) or a user (Search Console)" in part["reason"]
    report = {"ga4": ga4, "search_console": gsc}
    lines = AN.needs_input(report)
    assert len(lines) == 2 and lines[0].startswith("NEEDS_INPUT: GA4 numbers") and lines[1].startswith("NEEDS_INPUT: Search Console queries")
    assert AN.needs_input({}) == AN.needs_input(None) and len(AN.needs_input(None)) == 2                                   # nothing read: nothing is known


def test_with_access_the_rows_come_back_and_nothing_needs_input():
    ga4_session = Session(Reply(200, {"rows": [{"dimensionValues": [{"value": "Organic Search"}], "metricValues": [{"value": "120"}]}]}))
    ga4 = AN.ga4_report(ga4_session, "123456", days=14)
    assert ga4["status"] == AN.OK and len(ga4["rows"]) == 1
    url, body = ga4_session.calls[0]
    assert url == "https://analyticsdata.googleapis.com/v1beta/properties/123456:runReport" and body["dateRanges"] == [{"startDate": "14daysAgo", "endDate": "today"}]
    gsc_session = Session(Reply(200, {"rows": [{"keys": ["bib overalls"], "clicks": 4, "impressions": 90}]}))
    gsc = AN.search_console_report(gsc_session, "sc-domain:example.com", today=date(2026, 11, 16))
    url, body = gsc_session.calls[0]
    assert url == "https://www.googleapis.com/webmasters/v3/sites/sc-domain%3Aexample.com/searchAnalytics/query" and body["startDate"] == "2026-10-19" and body["endDate"] == "2026-11-16"
    assert gsc["status"] == AN.OK and AN.needs_input({"ga4": ga4, "search_console": gsc}) == []
    assert AN.ga4_report(Session(Reply(200, {})), "1")["rows"] == []                                                         # an empty answer is an empty answer, not an error


def test_other_failures_are_reported_in_words_and_never_raised():
    assert AN.ga4_report(Session(Reply(500)), "1")["status"] == AN.ERROR and "HTTP 500" in AN.ga4_report(Session(Reply(500)), "1")["reason"]
    assert "reply was not readable" in AN.ga4_report(Session(Reply(200, None)), "1")["reason"]
    down = AN.search_console_report(Session(TimeoutError("slow")), "sc-domain:x.com")
    assert down["status"] == AN.ERROR and "TimeoutError" in down["reason"] and down["rows"] is None
    assert AN.ga4_report(Session(Reply(200, {})), "")["status"] == AN.NOT_CONFIGURED and AN.search_console_report(Session(Reply(200, {})), "")["status"] == AN.NOT_CONFIGURED


def test_a_domains_public_rank_comes_from_the_search_results_and_none_means_not_in_the_first_results():
    results = [{"url": "https://www.other.com/a"}, {"url": "https://shop.example.com/overalls"}, {"url": "https://example.com.evil.net/x"}, {"url": "https://www.example.com/bib"}]
    assert AN.serper_rank(lambda q: results, "bib overalls", "example.com") == {"keyword": "bib overalls", "position": 2, "url": "https://shop.example.com/overalls"}
    assert AN.serper_rank(lambda q: results, "bib overalls", "www.Example.com")["position"] == 2
    assert AN.serper_rank(lambda q: results, "x", "nowhere.org") == {"keyword": "x", "position": None, "url": ""}
    assert AN.serper_rank(lambda q: results, "x", "example.com", depth=1)["position"] is None                                    # outside the depth asked
    assert AN.serper_rank(lambda q: results, "x", "")["position"] is None and AN.serper_rank(lambda q: None, "x", "a.com")["position"] is None


def test_the_full_report_runs_on_public_signals_alone_when_no_key_is_set_and_says_what_is_missing(tmp_path):
    root = _brand(tmp_path, "ga4_property_id: '123'\nsearch_console_site: sc-domain:example.com\ndomain: example.com\nrank_keywords: [bib overalls]\n")
    report = AN.build_report(root, "shop", "", search=lambda q: [{"url": "https://example.com/p"}])
    assert report["ga4"]["status"] == AN.NO_ACCESS and "GOOGLE_SERVICE_ACCOUNT_JSON is not set" in report["ga4"]["reason"] and report["ga4"]["rows"] is None
    assert report["serper_ranks"] == [{"keyword": "bib overalls", "position": 1, "url": "https://example.com/p"}] and report["public_signals_only"] is True and len(report["needs_input"]) == 2
    bare = AN.build_report(_brand(tmp_path / "b", "{}\n"), "shop", "", search=None)
    assert bare["ga4"]["status"] == AN.NOT_CONFIGURED and bare["serper_ranks"] == [] and len(bare["needs_input"]) == 2
    ok = AN.build_report(root, "shop", "", search=None, fetch=(Session(Reply(200, {"rows": [1]})), "tool@x.iam.gserviceaccount.com"))
    assert ok["ga4"]["status"] == AN.OK and ok["search_console"]["status"] == AN.OK and ok["public_signals_only"] is False and ok["needs_input"] == []


def test_the_analytics_command_prints_each_source_saves_the_report_and_invents_nothing(tmp_path, capsys):
    root = _brand(tmp_path, "ga4_property_id: '123'\nsearch_console_site: sc-domain:example.com\ndomain: example.com\nrank_keywords: [bib overalls]\n")
    store = Store(tmp_path / "data")
    args = cli.build_parser().parse_args(["analytics", "--brand", "shop", "--brands-dir", root, "--data-dir", str(tmp_path / "data")])
    assert RT.run_analytics(args, store, "2026-11-16T00:00:00Z", environ={}, search=lambda q: [{"url": "https://www.example.com/p"}]) == 0
    out = capsys.readouterr().out
    assert "shop: GA4: no_access (GOOGLE_SERVICE_ACCOUNT_JSON is not set in this run)" in out and "shop: public signal (Serper): 'bib overalls': position 1 (https://www.example.com/p)" in out
    assert out.count("shop: NEEDS_INPUT:") == 2
    saved = store.read_extra("shop", "analytics")
    assert saved["ga4"]["rows"] is None and saved["search_console"]["rows"] is None and saved["at"] == "2026-11-16T00:00:00Z" and len(saved["needs_input"]) == 2
    body = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    assert "analytics) args=(analytics --brand \"$BRAND\") ;;" in body and "analytics, install]" in body
