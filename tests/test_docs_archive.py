"""Deep Research reports in one Google Doc, a new tab for each. Nothing here reaches Google: a fake Google answers like
the real Docs API (it issues tab ids, refuses text outside a tab, and can send the real error shapes), and the report
used is the real one from the 2026-10-21 Deep Research test.
"""
import json
from pathlib import Path

import pytest

from contentintel import cli
from contentintel import docs_archive as D
from contentintel.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
RAW_REPORT = (FIXTURES / "deep_research_doubled_report.txt").read_text(encoding="utf-8")


def _cleaned():
    from contentintel import collectors as C
    return C.single_copy(RAW_REPORT)[0]            # what the run saves, and so what is archived


REAL_REPORT = _cleaned()
DOC = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
EMAIL = "robot@project.iam.gserviceaccount.com"
NOW = "2026-10-22T08:00:00Z"


# ---- a Google that behaves like the real one -----------------------------------------------------------------------

class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeGoogle:
    """The Docs API as far as this tool uses it. `script` is a queue of (status, body) answers that override the real
    behaviour, one per call, for testing failures. Every filled tab is checked: ranges must lie inside its text."""

    def __init__(self, title="Deep Research reports", tabs=("Tab 1",), script=None):
        self.title, self.tabs, self.script = title, [{"tabId": f"t.old{i}", "title": t} for i, t in enumerate(tabs)], list(script or [])
        self.calls, self.text, self.counter = [], {}, 0

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params})
        assert timeout == 60
        if self.script:
            status, body = self.script.pop(0)
            return Resp(status, body)
        if method == "GET":
            return Resp(200, {"title": self.title, "tabs": [{"tabProperties": {"tabId": t["tabId"], "title": t["title"]}} for t in self.tabs]})
        replies = []
        for req in json["requests"]:
            replies.append(self._apply(req))
        return Resp(200, {"replies": replies})

    def _apply(self, req):
        (kind, body), = req.items()
        if kind == "addDocumentTab":
            self.counter += 1
            props = body["tabProperties"]
            tab = {"tabId": f"t.new{self.counter}", "title": props["title"]}
            self.tabs.insert(props.get("index", 0), tab)
            return {"addDocumentTab": {"tabProperties": {"tabId": tab["tabId"], "title": tab["title"], "index": props.get("index", 0)}}}
        if kind == "deleteTab":
            self.tabs = [t for t in self.tabs if t["tabId"] != body["tabId"]]
            self.text.pop(body["tabId"], None)
            return {}
        if kind == "insertText":
            assert body["location"]["index"] == 1 and body["location"]["tabId"] in {t["tabId"] for t in self.tabs}
            self.text[body["location"]["tabId"]] = body["text"]
            return {}
        rng = body["range"]
        text = self.text[rng["tabId"]]
        assert 1 <= rng["startIndex"] < rng["endIndex"] <= D.utf16_len(text) + 1, (kind, rng, D.utf16_len(text))
        return {}

    @property
    def titles(self):
        return [t["title"] for t in self.tabs]


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def client(google):
    return D.DocClient(google, DOC, EMAIL, sleep=lambda s: None)


# ---- the address of the Doc ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (DOC, DOC), (f"  {DOC}\n", DOC), (f"https://docs.google.com/document/d/{DOC}/edit", DOC),
    (f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0", DOC), (f"https://docs.google.com/document/u/0/d/{DOC}/edit".replace("/u/0/d/", "/d/"), DOC),
    ("", None), ("   ", None), (None, None)])
def test_the_doc_is_found_from_its_address_or_its_bare_id(value, expected):
    assert D.parse_doc_id(value) == expected


@pytest.mark.parametrize("bad", ["short", "https://example.com/not-a-doc", "has spaces in it which is long enough x",
                                 "https://docs.google.com/document/d/short/edit",
                                 f"https://docs.google.com/spreadsheets/d/{DOC}/edit"])
def test_anything_else_is_refused_with_the_fix(bad):
    with pytest.raises(D.DocsError, match="DEEP_RESEARCH_DOC_ID is not a Google Doc address or id"):
        D.parse_doc_id(bad)


def test_the_connection_asks_only_for_the_docs_scope_and_never_echoes_the_key(monkeypatch):
    seen = {}

    class Creds:
        @staticmethod
        def from_service_account_info(info, scopes):
            seen["scopes"], seen["email"] = scopes, info["client_email"]
            return "creds"

    class Session:
        def __init__(self, creds):
            seen["creds"] = creds

    import google.auth.transport.requests as gr
    import google.oauth2.service_account as sa
    monkeypatch.setattr(sa, "Credentials", Creds)
    monkeypatch.setattr(gr, "AuthorizedSession", Session)
    session, email = D.connect(json.dumps({"client_email": EMAIL, "private_key": "SECRET-PEM"}))
    assert seen == {"scopes": ["https://www.googleapis.com/auth/documents"], "email": EMAIL, "creds": "creds"} and email == EMAIL
    assert isinstance(session, Session)


@pytest.mark.parametrize("key,match", [("not json", "not valid JSON"), ("[]", "not a service account key"),
                                       (json.dumps({"client_email": EMAIL}), "not a service account key"), (None, "not valid JSON")])
def test_a_bad_key_is_explained(key, match):
    with pytest.raises(D.DocsError, match=match):
        D.connect(key)


# ---- turning a report into text ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [("abc", 3), ("café", 4), ("🤠", 2), ("a🤠b", 4), ("", 0), ("日本", 2)])
def test_google_counts_positions_in_utf16_units_so_an_emoji_is_two(text, expected):
    assert D.utf16_len(text) == expected


def test_a_tab_is_named_by_date_brand_and_the_start_of_the_question():
    assert D.tab_title("2026-10-22", "swoveralls", "How are overalls marketed?") == "2026-10-22 swoveralls - How are overalls marketed?"


@pytest.mark.parametrize("question,expected_tail", [
    ("**Task:** Produce a brief research report on loungewear", "Produce a brief research report on loungewear"),
    ("Question: who are the rivals?", "who are the rivals?"),
    ("  lots   of\n\n whitespace\there ", "lots of whitespace here"),
    ("", ""), (None, "")])
def test_the_question_is_tidied_for_a_tab_name(question, expected_tail):
    assert D.tab_title("2026-10-22", "x", question) == "2026-10-22 x - " + expected_tail


def test_a_long_question_is_shortened_with_an_ellipsis_and_the_name_never_exceeds_the_limit():
    title = D.tab_title("2026-10-22", "swoveralls", "word " * 80)
    assert len(title) <= D.TITLE_LIMIT and title.endswith("…") and title.startswith("2026-10-22 swoveralls - word word")
    assert len(D.tab_title("2026-10-22", "a-very-long-brand-name-indeed-yes-it-is", "q" * 100)) <= D.TITLE_LIMIT + 5


def test_headings_in_the_real_report_are_found_by_their_shape_and_only_those():
    blocks = D.report_blocks(REAL_REPORT)
    headings = [(s, t) for s, t in blocks if s.startswith("heading")]
    assert headings == [
        ("heading1", "Category Study: Social Media Marketing Strategies for Loungewear Overalls on TikTok and Instagram"),
        ("heading2", "Core Messaging, Materiality, and Positioning Strategies"),
        ("heading2", "Platform Execution Dynamics: TikTok Direct-Response versus Instagram Lifestyle"),
        ("heading2", "Brand Ecosystem Archetypes and the Viral Dupe Phenomenon"),
        ("heading2", "Comparative Brand Landscape"),
        ("heading2", "Strategic Implications for Content Strategy")]


def test_the_real_tables_split_rows_are_put_back_together_one_line_per_row():
    rows = [t for s, t in D.report_blocks(REAL_REPORT) if " | " in t]
    assert len(rows) == 7                                                      # the header and six brands
    assert all(r.count(" | ") == 5 for r in rows)                              # six cells each, none lost or run together
    assert rows[0].startswith("Brand Name | Brand Scale & Type | Product Example")
    assert rows[1].startswith("Free People (FP Movement) | Enterprise Retailer | Hot Shot Onesie / Strappy Onesie | Cotton / Elastane jersey | TikTok, Instagram | Slouchy")
    assert rows[2].startswith("Halara | Fast-Growing Volume DTC |") and "TikTok (Shop & Ads), Instagram" in rows[2]
    assert rows[6].startswith("Aerie (OFFLINE) | Enterprise Retailer |") and rows[6].endswith("accessible across diverse sizes.")


def test_no_table_cell_is_ever_taken_for_a_heading():
    heads = {t for s, t in D.report_blocks(REAL_REPORT) if s.startswith("heading")}
    for cell in ("TikTok, Instagram", "Instagram", "Instagram, TikTok", "TikTok (Shop & Ads), Instagram", "Cotton / Elastane jersey", "Enterprise Retailer"):
        assert cell not in heads, cell


def test_paragraphs_stay_paragraphs():
    normal = [t for s, t in D.report_blocks(REAL_REPORT) if s == "normal" and " | " not in t]
    assert len(normal) == 9 and all(len(t) > 150 for t in normal)
    assert normal[0].startswith("The market for comfortable, casual loungewear overalls")


def test_a_table_that_does_not_fit_the_split_pattern_is_left_as_it_was():
    broken = "Heading words here\nA\tB\tC\tD\nOnly\tone\n"                  # a row with too few cells following it
    assert [t for s, t in D.report_blocks(broken)] == ["Heading words here", "A | B | C | D", "Only | one"]


def test_a_short_table_line_is_never_a_heading_even_before_a_long_paragraph():
    blocks = D.report_blocks("Brand | Scale\n" + "A long paragraph that goes on and on about the market and the brands in it. " * 3)
    assert blocks[0] == ("normal", "Brand | Scale")


def test_a_title_before_a_short_table_is_a_heading():
    assert D.report_blocks("Table of results\nA\tB\tC\tD\n")[0] == ("heading2", "Table of results")


def test_a_row_that_arrives_whole_on_one_line_is_just_joined():
    whole = "A\tB\tC\tD\nE\tF\tG\tH\n"
    assert [t for s, t in D.report_blocks(whole)] == ["A | B | C | D", "E | F | G | H"]


def test_table_lines_become_pipe_separated_text():
    blocks = D.report_blocks("Brand Name\tScale\tHook\nHalara\tVolume DTC\tWorkleisure comfort")
    assert blocks == [("normal", "Brand Name | Scale | Hook"), ("normal", "Halara | Volume DTC | Workleisure comfort")]


def test_markdown_marks_are_honoured_and_removed():
    assert D.report_blocks("# Title\n## Section\n### Sub\nBody text.") == [
        ("heading1", "Title"), ("heading2", "Section"), ("heading2", "Sub"), ("normal", "Body text.")]


def test_blank_lines_and_odd_spaces_are_dropped():
    assert D.report_blocks("\n\n  one\xa0\xa0 \n\n\n two  \n") == [("normal", "one"), ("normal", "two")]
    assert D.report_blocks("") == [] and D.report_blocks(None) == []


# ---- the tab's contents --------------------------------------------------------------------------------------------------

def entry(**kw):
    base = {"key": "run1-abc.md", "brand": "swoveralls", "at": "2026-10-22T07:41:00Z", "run": "run1", "question": "Who are the competitors?",
            "prompt": "Line one of the prompt.\nLine two.", "exchanges": [], "notes": [], "diagnostics": {}, "claude_calls": 1,
            "text": "Overview\n" + "A long paragraph about the market. " * 5, "sources": ["https://a.example.com/x"]}
    base.update(kw)
    return base


def test_a_tab_has_the_question_details_prompt_report_and_sources_in_that_order():
    styles = [(s, t) for s, t, _ in D.build_blocks(entry())]
    order = [t for s, t in styles if s in ("title", "heading2")]
    assert order == ["Who are the competitors?", "Details", "Prompt sent to Gemini", "Report", "Overview", "Sources"]
    assert styles[0][0] == "title"


def test_the_details_say_how_the_run_went_when_it_is_known():
    facts = [t for s, t, _ in D.build_blocks(entry(diagnostics={"mode_confirmed": False, "start_clicked": True, "waited_seconds": 125}))
             if s == "fact"]
    assert "Brand: swoveralls" in facts and "Collected: 2026-10-22T07:41:00Z" in facts and "Run: run1" in facts
    assert "Deep Research confirmed on: NOT CONFIRMED" in facts and "Start research pressed: yes" in facts
    assert "Waited: 2 min 5 s" in facts and "Claude calls: 1" in facts


def test_notes_and_exchanges_appear_only_when_there_are_some():
    plain = [t for _, t, _ in D.build_blocks(entry())]
    assert not any(t.startswith("Note:") or "questions and Claude" in t for t in plain)
    rich = [t for _, t, _ in D.build_blocks(entry(notes=["the page returned the report twice"],
                                                  exchanges=[{"gemini": "Which market?", "reply": "The US."}]))]
    assert "Note: the page returned the report twice" in rich and "Gemini's questions and Claude's answers" in rich
    assert "Gemini: Which market?" in rich and "Claude: The US." in rich


def test_sources_are_links_and_a_missing_list_is_said_not_hidden():
    blocks = D.build_blocks(entry(sources=["https://a.example.com/x", "https://b.example.com/y"]))
    assert [(s, e.get("url")) for s, _, e in blocks if s == "link"] == [("link", "https://a.example.com/x"), ("link", "https://b.example.com/y")]
    assert D.build_blocks(entry(sources=[]))[-1][1] == "(no source links were found in the report)"


def test_a_missing_question_still_gets_a_title():
    assert D.build_blocks(entry(question=""))[0][1] == "Deep Research report"


# ---- the requests, by position -------------------------------------------------------------------------------------------

def test_one_insert_then_styles_at_the_exact_positions_with_emoji_counted_as_two():
    blocks = [("title", "A🤠", {}), ("fact", "Brand: x", {"bold_to": 6}), ("link", "https://a.example.com", {"url": "https://a.example.com"})]
    reqs = D.build_requests("t.9", blocks)
    assert reqs[0] == {"insertText": {"location": {"tabId": "t.9", "index": 1}, "text": "A🤠\nBrand: x\nhttps://a.example.com\n"}}
    title = reqs[1]["updateParagraphStyle"]
    assert title["range"] == {"tabId": "t.9", "startIndex": 1, "endIndex": 5} and title["paragraphStyle"] == {"namedStyleType": "TITLE"}
    bold = reqs[2]["updateTextStyle"]
    assert bold["range"] == {"tabId": "t.9", "startIndex": 5, "endIndex": 11} and bold["textStyle"] == {"bold": True} and bold["fields"] == "bold"
    link = reqs[3]["updateTextStyle"]
    assert link["range"] == {"tabId": "t.9", "startIndex": 14, "endIndex": 35}          # the address itself, not the line break
    assert link["textStyle"] == {"link": {"url": "https://a.example.com"}} and link["fields"] == "link"
    assert len(reqs) == 4


def test_headings_get_named_styles_and_normal_text_gets_none():
    reqs = D.build_requests("t.1", [("heading1", "H1", {}), ("heading2", "H2", {}), ("normal", "plain", {})])
    assert [r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] for r in reqs[1:]] == ["HEADING_1", "HEADING_2"]
    assert len(reqs) == 3


def test_every_request_names_its_tab():
    for r in D.build_requests("t.7", D.build_blocks(entry())):
        (body,) = r.values()
        assert (body.get("location") or body.get("range"))["tabId"] == "t.7"


# ---- talking to Google --------------------------------------------------------------------------------------------------

def test_the_documents_title_and_tabs_are_read_with_a_small_field_list(client, google):
    assert client.info() == ("Deep Research reports", ["Tab 1"])
    call = google.calls[0]
    assert call["method"] == "GET" and call["url"] == f"https://docs.googleapis.com/v1/documents/{DOC}"
    assert call["params"] == {"includeTabsContent": "true", "fields": "title,tabs(tabProperties)"}


def test_a_report_becomes_a_new_first_tab_and_the_address_points_at_it(client, google):
    tab_id, url = client.add_report("2026-10-22 swoveralls - q", D.build_blocks(entry()))
    assert google.titles == ["2026-10-22 swoveralls - q", "Tab 1"]
    assert url == f"https://docs.google.com/document/d/{DOC}/edit?tab=t.new1" and tab_id == "t.new1"
    first, second = google.calls
    assert first["json"] == {"requests": [{"addDocumentTab": {"tabProperties": {"title": "2026-10-22 swoveralls - q", "index": 0}}}]}
    assert first["url"].endswith(":batchUpdate") and second["url"].endswith(":batchUpdate")
    assert "Who are the competitors?" in google.text["t.new1"] and "https://a.example.com/x" in google.text["t.new1"]


def test_the_real_report_fits_in_a_tab_with_every_range_inside_its_text(client, google):
    client.add_report("t", D.build_blocks(entry(text=REAL_REPORT, prompt="p " * 400)))
    assert len(google.text["t.new1"]) > 8000 and google.text["t.new1"].count("Comparative Brand Landscape") == 1


def test_a_tab_that_could_not_be_filled_is_removed_and_the_error_raised(google):
    google.script = [(200, {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.x", "title": "t"}}}]}),
                     (400, {"error": {"message": "Invalid requests[1]"}}), (200, {})]
    client = D.DocClient(google, DOC, EMAIL, sleep=lambda s: None)
    with pytest.raises(D.DocsError, match="Google answered 400: Invalid requests"):
        client.add_report("t", [("normal", "x", {})])
    assert google.calls[-1]["json"] == {"requests": [{"deleteTab": {"tabId": "t.x"}}]}


def test_a_failed_clean_up_does_not_hide_the_first_error(google):
    google.script = [(200, {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.x", "title": "t"}}}]}),
                     (400, {"error": {"message": "bad fill"}}), (500, {}), (500, {}), (500, {})]
    client = D.DocClient(google, DOC, EMAIL, sleep=lambda s: None)
    with pytest.raises(D.DocsError, match="bad fill"):
        client.add_report("t", [("normal", "x", {})])


def test_google_not_naming_the_new_tab_writes_nothing(google):
    google.script = [(200, {"replies": [{}]})]
    with pytest.raises(D.DocsError, match="did not say which tab it created"):
        D.DocClient(google, DOC, EMAIL).add_tab("t")
    assert len(google.calls) == 1


def test_the_check_reads_adds_and_removes_a_tab_leaving_nothing_behind(client, google):
    assert client.check() == {"title": "Deep Research reports", "tabs": 1}
    assert google.titles == ["Tab 1"]
    kinds = [list(c["json"]["requests"][0])[0] for c in google.calls if c["method"] == "POST"]
    assert kinds == ["addDocumentTab", "deleteTab"]


def test_a_check_that_cannot_remove_its_test_tab_says_which_tab_to_delete(google):
    google.script = [(200, {"title": "T", "tabs": []}), (200, {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.z"}}}]}),
                     (403, {"error": {"message": "no"}})]
    with pytest.raises(D.DocsError, match="delete the tab called 'connection test' by hand"):
        D.DocClient(google, DOC, EMAIL).check()


# ---- Google's complaints, as fixes ---------------------------------------------------------------------------------------

def fail(status, body, retries=0):
    google = FakeGoogle(script=[(status, body)] * (retries + 1))
    with pytest.raises(D.DocsError) as err:
        D.DocClient(google, DOC, EMAIL, sleep=lambda s: None).info()
    return str(err.value)


def test_the_docs_api_switched_off_names_the_link_to_switch_it_on():
    link = "https://console.developers.google.com/apis/api/docs.googleapis.com/overview?project=123456"
    msg = fail(403, {"error": {"status": "PERMISSION_DENIED", "message": f"Google Docs API has not been used in project 123456 before or it is disabled. Enable it by visiting {link} then retry.",
                               "details": [{"reason": "SERVICE_DISABLED"}]}})
    assert "Google Docs API is not switched on" in msg and link in msg and "press Enable" in msg


def test_the_docs_api_switched_off_without_a_link_still_points_at_the_library_page():
    msg = fail(403, {"error": {"message": "x", "details": [{"reason": "SERVICE_DISABLED"}]}})
    assert "https://console.cloud.google.com/apis/library/docs.googleapis.com" in msg


def test_a_doc_not_shared_with_the_robot_names_the_address_to_share_it_with():
    msg = fail(403, {"error": {"status": "PERMISSION_DENIED", "message": "The caller does not have permission"}})
    assert EMAIL in msg and "press Share" in msg and "Editor" in msg and "not switched on" not in msg


def test_a_wrong_id_says_to_check_the_secret():
    msg = fail(404, {"error": {"message": "Requested entity was not found."}})
    assert "DEEP_RESEARCH_DOC_ID" in msg and "not a Sheet" in msg


def test_a_rejected_key_says_to_make_a_new_one():
    assert "Create a new key" in fail(401, {"error": {"message": "invalid"}})


@pytest.mark.parametrize("status,body,expect", [(400, {"error": {"message": "Invalid value"}}, "Google answered 400: Invalid value."),
                                                (418, {}, "Google answered 418."), (400, None, "Google answered 400.")])
def test_other_complaints_are_passed_on_plainly(status, body, expect):
    assert expect in fail(status, body)


def test_a_busy_google_is_retried_twice_with_a_growing_pause_then_succeeds():
    pauses = []
    google = FakeGoogle(script=[(429, {}), (503, {})])
    client = D.DocClient(google, DOC, EMAIL, sleep=pauses.append)
    assert client.info() == ("Deep Research reports", ["Tab 1"])
    assert pauses == [2, 4] and len(google.calls) == 3


def test_a_google_that_stays_busy_is_given_up_on_after_three_tries():
    pauses = []
    google = FakeGoogle(script=[(503, {})] * 3)
    with pytest.raises(D.DocsError, match="Google answered 503"):
        D.DocClient(google, DOC, EMAIL, sleep=pauses.append).info()
    assert len(google.calls) == 3 and pauses == [2, 4]


def test_real_errors_are_not_retried():
    google = FakeGoogle(script=[(403, {"error": {}}), (200, {})])
    with pytest.raises(D.DocsError):
        D.DocClient(google, DOC, EMAIL, sleep=lambda s: pytest.fail("slept")).info()
    assert len(google.calls) == 1


# ---- which reports still need a tab ---------------------------------------------------------------------------------------

def record(name, text="A long report. " * 30, source="deep_research", at="2026-10-22T07:41:00Z", question="Who are the rivals?", **deep):
    item = {"url": f"data/swoveralls/research/deep/{name}", "title": question, "text": text, "extra_sources": ["https://s.example.com"]}
    return {"at": at, "run": "run1", "source": source, "items": [item], "deep": {"prompt": "P", "claude_calls": 1, **deep}}


def test_every_unarchived_report_is_listed_oldest_first_with_everything_the_tab_needs():
    recs = [record("run1-a.md", at="2026-10-20T07:00:00Z"), record("run2-b.md", at="2026-10-21T07:00:00Z", notes=["n"], exchanges=[{"gemini": "g", "reply": "r"}])]
    found = D.entries_to_archive("swoveralls", recs, None)
    assert [e["key"] for e in found] == ["run1-a.md", "run2-b.md"]
    second = found[1]
    assert (second["brand"], second["at"], second["run"], second["question"], second["prompt"]) == ("swoveralls", "2026-10-21T07:00:00Z", "run1", "Who are the rivals?", "P")
    assert second["notes"] == ["n"] and second["exchanges"] == [{"gemini": "g", "reply": "r"}] and second["sources"] == ["https://s.example.com"]
    assert second["claude_calls"] == 1 and second["text"].startswith("A long report.")


def test_reports_that_already_have_a_tab_other_sources_and_empty_reports_are_skipped():
    recs = [record("done.md"), record("new.md"), record("tiktok.md", source="tiktok"), record("empty.md", text="   "),
            {"source": "deep_research", "items": [{"url": "no-md-here", "text": "x"}]}, {"source": "deep_research", "items": []}]
    state = {"items": {"done.md": {"tab_id": "t.1"}}}
    assert [e["key"] for e in D.entries_to_archive("swoveralls", recs, state)] == ["new.md"]


def test_the_report_key_is_the_saved_file_name():
    assert D.report_key({"url": "data/swoveralls/research/deep/run1-abc.md"}) == "run1-abc.md"
    assert D.report_key({"url": "https://example.com/page"}) == "" and D.report_key({}) == "" and D.report_key(None) == ""


# ---- archiving ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    for r in (record("run1-a.md", at="2026-10-20T07:00:00Z", question="First question"),
              record("run2-b.md", at="2026-10-21T07:00:00Z", question="Second question")):
        s.append_collected("swoveralls", r)
    return s


def test_each_new_report_gets_a_tab_and_is_remembered(store, client, google):
    lines, added, failed = D.archive_reports(store, ["swoveralls"], client, NOW)
    assert (added, failed) == (2, 0) and len(google.titles) == 3
    assert google.titles[:2] == ["2026-10-21 swoveralls - Second question", "2026-10-20 swoveralls - First question"]
    state = store.read_deep_archive("swoveralls")
    assert sorted(state["items"]) == ["run1-a.md", "run2-b.md"]
    entry_ = state["items"]["run1-a.md"]
    assert entry_["archived_at"] == NOW and entry_["tab_id"].startswith("t.new") and entry_["url"].startswith(f"https://docs.google.com/document/d/{DOC}/edit?tab=")
    assert all("added the tab" in line for line in lines) and len(lines) == 2


def test_running_again_adds_nothing_and_says_so(store, client, google):
    D.archive_reports(store, ["swoveralls"], client, NOW)
    calls = len(google.calls)
    lines, added, failed = D.archive_reports(store, ["swoveralls"], client, NOW)
    assert (added, failed) == (0, 0) and lines == ["swoveralls: no new Deep Research reports to add"] and len(google.calls) == calls


def test_a_dry_run_lists_the_tabs_and_changes_nothing(store, client, google):
    lines, added, failed = D.archive_reports(store, ["swoveralls"], client, NOW, dry_run=True)
    assert (added, failed) == (0, 0) and len(lines) == 2 and all(l.startswith("swoveralls: would add a tab") for l in lines)
    assert google.calls == [] and store.read_deep_archive("swoveralls") is None


def test_one_failing_report_does_not_stop_the_others_and_is_retried_next_time(store, google):
    google.script = [(503, {})] * 3                              # the first report's first call fails for good
    client = D.DocClient(google, DOC, EMAIL, sleep=lambda s: None)
    lines, added, failed = D.archive_reports(store, ["swoveralls"], client, NOW)
    assert (added, failed) == (1, 1)
    assert "could not add" in lines[0] and "First question" in lines[0] and "added the tab" in lines[1]
    assert sorted(store.read_deep_archive("swoveralls")["items"]) == ["run2-b.md"]          # only the success is remembered
    lines, added, failed = D.archive_reports(store, ["swoveralls"], client, NOW)             # Google recovers
    assert (added, failed) == (1, 0) and sorted(store.read_deep_archive("swoveralls")["items"]) == ["run1-a.md", "run2-b.md"]


def test_the_memory_is_saved_after_every_tab_so_a_crash_never_repeats_one(store, google):
    class Crash(Exception):
        pass

    class CrashAfterOne(D.DocClient):
        def add_report(self, title, blocks):
            if len(google.titles) > 1:
                raise Crash()
            return super().add_report(title, blocks)

    with pytest.raises(Crash):
        D.archive_reports(store, ["swoveralls"], CrashAfterOne(google, DOC, EMAIL, sleep=lambda s: None), NOW)
    assert sorted(store.read_deep_archive("swoveralls")["items"]) == ["run1-a.md"]


def test_a_brand_with_no_reports_is_reported_not_ignored(tmp_path, client):
    lines, added, failed = D.archive_reports(Store(tmp_path / "d"), ["defi_snacks"], client, NOW)
    assert lines == ["defi_snacks: no new Deep Research reports to add"] and (added, failed) == (0, 0)


# ---- the memory file -----------------------------------------------------------------------------------------------------

def test_reading_the_memory_creates_nothing_and_a_round_trip_keeps_everything(tmp_path):
    s = Store(tmp_path / "data")
    assert s.read_deep_archive("swoveralls") is None and not (tmp_path / "data").exists()
    state = {"items": {"a.md": {"tab_id": "t.1", "url": "u", "title": "t", "archived_at": NOW}}}
    s.write_deep_archive("swoveralls", state)
    assert s.read_deep_archive("swoveralls") == state


@pytest.mark.parametrize("content", ["{ not json", "[]", '{"items": []}', '{"items": "x"}', "null"])
def test_a_damaged_memory_reads_as_nothing_archived_never_a_crash(tmp_path, content):
    s = Store(tmp_path / "data")
    path = s._research_file("swoveralls", "deep_archive.json")
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert s.read_deep_archive("swoveralls") is None


# ---- the command ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def cmd(tmp_path, monkeypatch, store):
    google = FakeGoogle()
    monkeypatch.setattr(cli, "_DOCS_CONNECT", lambda key: (google, EMAIL))
    for name in ("GOOGLE_SERVICE_ACCOUNT_JSON", "DEEP_RESEARCH_DOC_ID"):
        monkeypatch.delenv(name, raising=False)

    def run(*args, key='{"client_email": "x", "private_key": "y"}', doc=DOC):
        if key is not None:
            monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", key)
        if doc is not None:
            monkeypatch.setenv("DEEP_RESEARCH_DOC_ID", doc)
        return cli.main(["docs", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", *args])
    return run, google


def test_the_command_adds_tabs_and_exits_cleanly(cmd, capsys):
    run, google = cmd
    assert run("--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert out.count("added the tab") == 2 and len(google.titles) == 3
    assert run("--brand", "swoveralls") == 0 and "no new Deep Research reports to add" in capsys.readouterr().out


def test_the_command_is_skipped_politely_without_the_doc_secret(cmd, capsys):
    run, google = cmd
    assert run("--brand", "swoveralls", doc=None) == 0
    out = capsys.readouterr().out
    assert "skipped: the DEEP_RESEARCH_DOC_ID secret is not set" in out and "::notice::" in out and google.calls == []


def test_the_command_is_skipped_politely_without_the_google_key(cmd, capsys):
    run, google = cmd
    assert run("--brand", "swoveralls", key=None) == 0
    assert "skipped: the GOOGLE_SERVICE_ACCOUNT_JSON secret is not set" in capsys.readouterr().out and google.calls == []


def test_a_blank_doc_secret_counts_as_not_set(cmd, capsys):
    run, google = cmd
    assert run("--brand", "swoveralls", doc="   ") == 0 and "DEEP_RESEARCH_DOC_ID secret is not set" in capsys.readouterr().out


def test_a_pasted_address_works_as_well_as_the_id(cmd):
    run, google = cmd
    assert run("--brand", "swoveralls", doc=f"https://docs.google.com/document/d/{DOC}/edit") == 0
    assert all(DOC in c["url"] for c in google.calls)


def test_a_nonsense_doc_secret_is_an_error_with_the_fix(cmd, capsys):
    run, google = cmd
    assert run("--brand", "swoveralls", doc="oops") == 1
    assert "Error: DEEP_RESEARCH_DOC_ID is not a Google Doc address or id" in capsys.readouterr().out


def test_the_check_proves_the_doc_and_says_who_it_is_shared_with(cmd, capsys):
    run, google = cmd
    assert run("--all", "--check") == 0
    out = capsys.readouterr().out
    assert "Google Doc archive works: 'Deep Research reports' has 1 tab(s)" in out and f"Shared with {EMAIL}" in out
    assert google.titles == ["Tab 1"]


def test_a_failed_check_exits_with_the_fix(cmd, capsys, monkeypatch):
    run, google = cmd
    google.script = [(403, {"error": {"message": "no"}})]
    assert run("--all", "--check") == 1
    out = capsys.readouterr().out
    assert f"Error: The Google Doc is not shared with {EMAIL}" in out


def test_a_report_that_cannot_be_added_fails_the_step_so_it_is_noticed_and_is_not_lost(cmd, capsys):
    run, google = cmd
    google.script = [(503, {})] * 3 + [(200, {})] * 0
    assert run("--brand", "swoveralls") == 1
    out = capsys.readouterr().out
    assert "could not add" in out and "::error::1 Deep Research report(s) could not be added" in out and "tried again next run" in out


def test_a_dry_run_through_the_command_writes_nothing(cmd, capsys, tmp_path):
    run, google = cmd
    assert run("--brand", "swoveralls", "--dry-run") == 0
    assert "would add a tab" in capsys.readouterr().out and google.calls == []
    assert not (tmp_path / "data" / "swoveralls" / "research" / "deep_archive.json").exists()
