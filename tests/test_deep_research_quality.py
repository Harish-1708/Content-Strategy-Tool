"""The Deep Research test of 2026-10-21 passed, but its file showed three real problems:

  * the prompt Claude wrote ended in the middle of a word ("comfort, vers"): it hit the 700-token limit and nothing
    noticed, so Gemini was sent half a brief;
  * the report appeared twice in the text read from the page (a cut-off copy, then the complete one);
  * nothing said whether Deep Research had really run, or where the report's sources were.

These tests pin the fixes. The doubled report below is the real one from that run.
"""
from pathlib import Path

import pytest

from contentintel import browser as B
from contentintel import claude_client as CC
from contentintel import collectors as C
from contentintel import providers as P

from fake_browser import GeminiFake
from fake_http import FakeHttp
from test_collectors import site

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
DOUBLED = (Path(__file__).parent / "fixtures" / "deep_research_doubled_report.txt").read_text(encoding="utf-8")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


def reply(text, stop="end_turn"):
    return (200, {"content": [{"type": "text", "text": text}], "stop_reason": stop})


def claude_with(*responses, **kw):
    http = FakeHttp()
    http.on("POST", CLAUDE_URL, *responses)
    return http, CC.Claude("k", http=http, sleep=lambda s: None, **kw)


# ---- a prompt that is cut off is never sent ----------------------------------------------------------------------------

def test_the_prompt_gets_its_own_larger_limit_not_the_small_one_for_answers():
    http, claude = claude_with(reply("A whole prompt."))
    assert claude.prepare_prompt("q", "ctx") == "A whole prompt."
    assert http.calls[0]["json"]["max_tokens"] == 2000 and claude.max_tokens == 700


def test_the_prompt_limit_is_a_setting():
    http, claude = claude_with(reply("ok"), prompt_max_tokens=1234)
    claude.prepare_prompt("q", "c")
    assert http.calls[0]["json"]["max_tokens"] == 1234


def test_a_prompt_that_runs_out_of_room_is_asked_again_with_twice_the_room():
    http, claude = claude_with(reply("**Task:** a brief that stops in the mid", "max_tokens"), reply("The complete prompt."))
    assert claude.prepare_prompt("q", "c") == "The complete prompt."
    assert [c["json"]["max_tokens"] for c in http.calls] == [2000, 4000]


def test_a_prompt_that_is_cut_off_twice_is_an_error_never_a_half_prompt():
    http, claude = claude_with(reply("half", "max_tokens"), reply("half again", "max_tokens"))
    with pytest.raises(CC.TruncatedError, match="cut off after 4000 tokens"):
        claude.prepare_prompt("q", "c")
    assert len(http.calls) == 2 and issubclass(CC.TruncatedError, P.ProviderError)


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence", None])
def test_a_normal_ending_is_never_mistaken_for_a_cut_off(stop):
    body = {"content": [{"type": "text", "text": "fine"}]}
    if stop:
        body["stop_reason"] = stop
    http, claude = claude_with((200, body))
    assert claude.complete("s", "u") == "fine" and len(http.calls) == 1


def test_an_answer_to_a_gemini_question_is_also_never_cut_off():
    http, claude = claude_with(reply("Target audience: young adults, and", "max_tokens"), reply("Target audience: young adults."))
    assert claude.answer("prompt", "ctx", "Who is the audience?") == "Target audience: young adults."
    assert [c["json"]["max_tokens"] for c in http.calls] == [700, 1400]


def test_seeds_use_the_same_check(tmp_path):
    http, claude = claude_with(reply('{"context": "cut', "max_tokens"))
    with pytest.raises(CC.TruncatedError):
        claude.complete("s", "u", max_tokens=100)


def test_when_the_prompt_cannot_be_completed_the_question_is_sent_as_written_and_it_says_so(cfg):
    from fake_browser import FakeClaude
    claude = FakeClaude(fail_prepare=CC.TruncatedError("Claude: the answer was cut off after 4000 tokens"))
    g = site(cfg, "gemini")
    out = C.collect_deep_research(GeminiFake(g), "Who are the competitors?", g, claude=claude, claude_budget=5)
    assert out["prompt"] == "Who are the competitors?" and out["claude_calls"] == 0
    assert any("Claude could not prepare the prompt" in n and "cut off" in n for n in out["notes"])


# ---- what the prompt tells Gemini to do ---------------------------------------------------------------------------------

@pytest.mark.parametrize("needle", [
    "the goal, and the decision the findings will inform",          # why the research is wanted
    "time frame (default: the last 12 months)",                      # a default instead of a question
    "Gemini never needs to ask a clarifying question", "state it at the top of the report",
    "hook in the first seconds", "the offer and promotion", "the claims and proof",
    "every example and every number must carry a working source link and a date",
    "say 'not found' instead of guessing", "never state engagement or sales figures unless the source shows them",
    "'observed'", "'inferred'", "a ranked list of the top findings", "brand, platform, format, hook, offer, date and link",
    "gaps and how confident", "final 'Sources' list", "under 450 words", "always finish it",
    "Reply with the prompt text only"])
def test_the_prompt_instructions_ask_for_what_makes_a_report_checkable(needle):
    assert needle in CC.PREPARE_SYSTEM


def test_the_instructions_do_not_invent_facts_about_a_brand():
    assert "Do not invent facts about the brand beyond the context given" in CC.PREPARE_SYSTEM


# ---- a doubled report -----------------------------------------------------------------------------------------------------

def test_the_real_doubled_report_is_reduced_to_its_complete_copy():
    text, doubled = C.single_copy(DOUBLED)
    assert doubled is True
    assert text.startswith("# Category Study: Social Media Marketing Strategies for Loungewear Overalls")
    assert text.rstrip().endswith("enduring brand equity.")
    assert "Marketing Strategy Analysis" not in text and len(text) > 7000 and len(text) < len(DOUBLED) * 0.65
    assert text.count("Comparative Brand Landscape") == 1                 # the table now appears once


def test_a_normal_report_is_returned_exactly_as_it_came():
    single = DOUBLED[DOUBLED.index("# Category Study"):]
    assert C.single_copy(single) == (single, False)
    assert C.single_copy("Short answer.") == ("Short answer.", False)
    assert C.single_copy("") == ("", False)


def test_a_report_with_several_headed_sections_is_not_mistaken_for_a_repeat():
    """Two sections of one real report share about 40% of their distinctive words, not the 60% a repeat does."""
    one = DOUBLED[DOUBLED.index("# Category Study"):]
    cut = one.index("Platform Execution Dynamics")
    sectioned = one[:cut] + "\n# Platform Execution Dynamics: TikTok versus Instagram\n" + one[cut:]
    assert C.single_copy(sectioned) == (sectioned, False)


def test_two_unrelated_documents_are_not_treated_as_a_repeat():
    a = "Introduction\n" + "Quarterly logistics review covering warehouse throughput, carrier performance and freight costs. " * 30
    b = "\n# Pricing Study\n" + "Subscription pricing experiments across tiers, discounts, churn cohorts and renewal timing. " * 30
    assert C.single_copy(a + b) == (a + b, False)


@pytest.mark.parametrize("restart,is_a_repeat", [
    ("# Category Study", True),             # the real one: a top-level heading with a capital
    ("## Category Study", False),           # a second-level heading is just a section
    ("# category Study", False),            # no capital letter
    ("#Category Study", False),             # not a markdown heading at all
])
def test_only_a_top_level_heading_with_a_capital_marks_a_restart(restart, is_a_repeat):
    text = DOUBLED.replace("# Category Study", restart)
    assert text != DOUBLED or is_a_repeat
    assert C.single_copy(text)[1] is is_a_repeat


def test_the_longer_copy_is_kept_even_if_it_came_first():
    body = DOUBLED[DOUBLED.index("# Category Study"):]
    swapped = body + "\n# Category Study (draft): " + body[:int(len(body) * 0.55)]
    kept, doubled = C.single_copy(swapped)
    assert doubled is True and kept == body.strip()


def test_a_later_part_that_is_too_short_to_be_a_copy_is_ignored():
    body = DOUBLED[DOUBLED.index("# Category Study"):]
    assert C.single_copy(body + "\n# Notes\n" + body[:200])[1] is False


# ---- what the run records ---------------------------------------------------------------------------------------------------

def run(cfg, gem, **kw):
    g = site(cfg, "gemini")
    return C.collect_deep_research(gem, "Who are the competitors?", g, **kw)


def test_a_doubled_page_text_is_cleaned_with_the_raw_text_kept_and_a_note(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini"), report=DOUBLED))
    assert out["doubled"] is True and out["raw_text"] == DOUBLED
    assert out["text"].startswith("# Category Study") and len(out["text"]) < len(out["raw_text"])
    assert any("returned the report twice" in n for n in out["notes"])


def test_a_single_report_is_untouched_and_not_flagged(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    assert out["doubled"] is False and out["text"] == out["raw_text"]
    assert not any("twice" in n for n in out["notes"])


def test_a_normal_run_says_the_research_was_started_and_finished(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    d = out["diagnostics"]
    assert d["mode_confirmed"] is True and d["start_clicked"] is True and d["done_button_seen"] is True
    assert d["waited_seconds"] == out["waited_seconds"] and d["plan_seconds"] <= d["waited_seconds"]
    assert not any("ordinary chat answer" in n or "on its own" in n for n in out["notes"])


class ChatLikeGemini(GeminiFake):
    """Gemini answering at once in an ordinary chat: the text is there from the start and there is no Start research button."""

    def __init__(self, cfg, **kw):
        super().__init__(cfg, start_after=10 ** 6, **kw)

    def dynamic_text(self):
        return self.report

    def dynamic_present(self):
        return set()


def test_a_run_that_never_started_research_and_never_showed_a_finished_button_is_flagged_as_maybe_just_chat(cfg):
    out = run(cfg, ChatLikeGemini(site(cfg, "gemini")))
    d = out["diagnostics"]
    assert d["start_clicked"] is False and d["done_button_seen"] is False
    assert any("may be an ordinary chat answer and not a Deep Research report" in n for n in out["notes"])


def test_gemini_starting_on_its_own_is_noted_but_not_called_suspicious(cfg):
    g = site(cfg, "gemini")
    gem = ChatLikeGemini(g)
    gem.present_always.add(g["done_any"][0])
    out = run(cfg, gem)
    assert out["diagnostics"]["start_clicked"] is False and out["diagnostics"]["done_button_seen"] is True
    assert any("started the research on its own" in n for n in out["notes"])
    assert not any("ordinary chat answer" in n for n in out["notes"])


def test_an_unconfirmed_switch_is_recorded_in_the_diagnostics(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini"), mode_confirmed=False))
    assert out["diagnostics"]["mode_confirmed"] is False


def test_a_report_with_no_links_says_so(cfg):
    gem = GeminiFake(site(cfg, "gemini"), report="A long report with no web addresses in it. " * 60)
    gem.response_links = lambda selectors: []
    out = run(cfg, gem)
    assert out["sources"] == [] and any("no source links were found" in n for n in out["notes"])


def test_the_finished_page_is_pictured_for_next_time(cfg):
    gem = GeminiFake(site(cfg, "gemini"))
    run(cfg, gem)
    assert gem.shots == ["gemini-report"]


# ---- the saved test file -----------------------------------------------------------------------------------------------------

def test_the_test_file_holds_everything_a_person_needs_to_judge_the_run(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    text = C.format_test_file(out)
    for heading in ("# Deep Research test", "## Prompt sent to Gemini", "## Questions from Gemini and Claude's answers",
                    "## What happened", "## Report", "## Sources found"):
        assert heading in text, heading
    assert text.index("## What happened") < text.index("## Report") < text.index("## Sources found")
    assert "- Deep Research switched on (the prompt box asked what to research): yes" in text
    assert "- Start research button pressed: yes" in text and "- finished-report button seen: yes" in text
    assert "https://a.example.com/x" in text and "https://c.example.com/z" in text
    assert "(Gemini asked nothing)" in text


def test_the_test_file_shouts_when_deep_research_was_not_confirmed(cfg):
    text = C.format_test_file(run(cfg, GeminiFake(site(cfg, "gemini"), mode_confirmed=False)))
    assert "NOT CONFIRMED" in text and "- NOTE: the buttons were pressed but the page did not show Deep Research" in text


def test_the_test_file_lists_the_questions_gemini_asked(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    out["exchanges"] = [{"gemini": "Which market?", "reply": "The US."}]
    assert "- Gemini: Which market?\n  Claude: The US." in C.format_test_file(out)


def test_the_test_file_says_when_no_sources_were_found(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    out["sources"] = []
    assert "(none found)" in C.format_test_file(out)


# ---- the collect run and the test run keep the cleaned report AND the raw one ------------------------------------------------

def _deep_collect(tmp_path, cfg, report):
    from contentintel.store import Store
    from test_collectors import DEEP, collect, world
    store = Store(tmp_path / "data")
    brand = __import__("conftest").load_all(__import__("conftest").BRANDS_DIR)["swoveralls"]
    gem = GeminiFake(site(cfg, "gemini"), report=report)
    result = collect(store, tmp_path, [brand], DEEP, world(cfg, gem=gem), cfg,
                     C.load_collector_settings("does-not-exist.yaml"), sources=("deep_research",))
    return store, result


def test_a_collect_run_saves_the_cleaned_report_and_beside_it_the_raw_one(tmp_path, cfg):
    store, result = _deep_collect(tmp_path, cfg, DOUBLED)
    assert result.brands["swoveralls"][0].status == "ran"
    folder = tmp_path / "data" / "swoveralls" / "research" / "deep"
    cleaned = [p for p in folder.glob("*.md") if not p.name.endswith("-raw.md")]
    raw = list(folder.glob("*-raw.md"))
    assert len(cleaned) == 1 and len(raw) == 1 and raw[0].name == cleaned[0].name[:-3] + "-raw.md"
    assert cleaned[0].read_text(encoding="utf-8").startswith("# Category Study") and raw[0].read_text(encoding="utf-8") == DOUBLED


def test_a_normal_report_leaves_no_raw_file(tmp_path, cfg):
    store, result = _deep_collect(tmp_path, cfg, "A single, ordinary report. " * 80)
    assert list((tmp_path / "data" / "swoveralls" / "research" / "deep").glob("*-raw.md")) == []


def test_the_saved_record_carries_what_the_archive_and_the_sheet_need(tmp_path, cfg):
    store, result = _deep_collect(tmp_path, cfg, DOUBLED)
    (rec,) = [r for r in store.read_collected("swoveralls") if r["source"] == "deep_research"]
    d = rec["deep"]["diagnostics"]
    assert d["mode_confirmed"] is True and d["start_clicked"] is True and d["done_button_seen"] is True
    assert any("returned the report twice" in n for n in rec["deep"]["notes"])
    (item,) = rec["items"]
    assert item["text"].startswith("# Category Study") and "Start research pressed: yes" in item["details"]
    assert "Deep Research confirmed on: yes" in item["details"] and "finished-report button seen: yes" in item["details"]
    assert item["details"].endswith(f"waited {d['waited_seconds']} s")


def test_the_deep_research_test_also_keeps_the_raw_text_when_the_page_doubled_it(tmp_path, cfg):
    from contentintel import research as R
    from contentintel.store import Store
    from test_collectors import CREDS, NOW, world
    f = world(cfg, gem=GeminiFake(site(cfg, "gemini"), report=DOUBLED))
    out = C.run_deep_research_test(f, CREDS, cfg, C.load_collector_settings("does-not-exist.yaml"), Store(tmp_path / "data"),
                                   R.Ledger, NOW, None, tmp_path / "debug")
    assert out["ok"] is True
    assert (tmp_path / "debug" / "test-deep-research-raw.txt").read_text(encoding="utf-8") == DOUBLED
    assert (tmp_path / "debug" / "test-deep-research.md").read_text(encoding="utf-8").count("Comparative Brand Landscape") == 1


def test_the_deep_research_test_leaves_no_raw_file_for_a_normal_report(tmp_path, cfg):
    from contentintel import research as R
    from contentintel.store import Store
    from test_collectors import CREDS, NOW, world
    f = world(cfg)
    C.run_deep_research_test(f, CREDS, cfg, C.load_collector_settings("does-not-exist.yaml"), Store(tmp_path / "data"),
                             R.Ledger, NOW, None, tmp_path / "debug")
    assert not (tmp_path / "debug" / "test-deep-research-raw.txt").exists()


# ---- the repeat rule needs BOTH similarity measures -----------------------------------------------------------------------------

def test_a_later_part_that_merely_contains_the_first_parts_words_among_many_new_ones_is_not_a_repeat():
    first = "Overview\n" + ("alpha bravo charlie delta foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango "
                            "uniform victor whiskey xray yankee zulu " * 12)
    new_words = " ".join(f"unique{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}word" for i in range(400))
    second = "\n# Another Document\n" + first.split("\n", 1)[1][:400] + " " + new_words
    assert len(first) >= 0.25 * (len(first) + len(second)) and len(second) >= 0.5 * len(first)
    assert C.single_copy(first + second)[1] is False


# ---- the note about typed or addressed searches ----------------------------------------------------------------------------------

def test_the_note_says_how_the_tag_was_searched():
    typed = C._page_notes({"fallbacks": ["#a"], "fallback_how": ["typed"]})
    assert typed.endswith("(typed into the search box, as a person would)")
    addressed = C._page_notes({"fallbacks": ["#a"], "fallback_how": ["address"]})
    assert addressed.endswith("(the search address)")
    both = C._page_notes({"fallbacks": ["#a", "#b"], "fallback_how": ["typed", "address"]})
    assert "#a, #b" in both and "the search address and typed into the search box, as a person would" in both
    assert C._page_notes({"fallbacks": ["#a"]}).endswith("(searched)")


# ---- the 60% bar on its own: similar enough on the second measure, but not a repeat on the first ----------------------------------

def _vocab(start, count):
    """Distinct made-up words of 5+ letters only (the rule ignores digits and short words)."""
    out = []
    for i in range(start, start + count):
        out.append("vocab" + chr(97 + i % 26) + chr(97 + (i // 26) % 26) + chr(97 + (i // 676) % 26))
    return out


def _two_parts(shared):
    """Two parts of equal size with `shared` words in common out of 100 each."""
    first_words = _vocab(0, 100)
    second_words = first_words[:shared] + _vocab(1000, 100 - shared)
    return "Intro\n" + " ".join(first_words), "\n# Next Section\n" + " ".join(second_words)


def test_half_the_words_in_common_is_a_new_section_not_a_repeat():
    first, second = _two_parts(50)                     # 50% of the first part's words recur, and 33% overall: only the 60% bar says no
    assert len(first) >= 0.25 * (len(first) + len(second)) and len(second) >= 0.5 * len(first)
    assert C.single_copy(first + second)[1] is False


def test_seventy_percent_in_common_is_a_repeat():
    first, second = _two_parts(70)
    kept, doubled = C.single_copy(first + second)
    assert doubled is True and kept.startswith("# Next Section")


def test_the_bar_sits_between_the_two():
    for shared, expected in ((55, False), (65, True)):
        first, second = _two_parts(shared)
        assert C.single_copy(first + second)[1] is expected, shared


# ---- every line of "What happened" can say no ----------------------------------------------------------------------------------------

def test_the_test_file_says_no_when_research_was_not_started_and_no_finished_button_came(cfg):
    out = run(cfg, ChatLikeGemini(site(cfg, "gemini")))
    text = C.format_test_file(out)
    assert "- Start research button pressed: no" in text and "- finished-report button seen: no" in text
    assert "- NOTE: no Start research button was pressed and no finished-report button appeared" in text
    assert "Start research button pressed: yes" not in text


def test_each_line_of_what_happened_follows_the_facts_it_reports(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    for flag, line in (("mode_confirmed", "- Deep Research switched on (the prompt box asked what to research): "),
                       ("start_clicked", "- Start research button pressed: "), ("done_button_seen", "- finished-report button seen: ")):
        for value, word in ((True, "NOT CONFIRMED" if flag == "mode_confirmed" else "no"), (False, None)):
            out["diagnostics"][flag] = value
            text = C.format_test_file(out)
            wanted = "yes" if value else ("NOT CONFIRMED" if flag == "mode_confirmed" else "no")
            assert line + wanted in text, (flag, value)


def test_the_waiting_line_reports_the_real_numbers(cfg):
    out = run(cfg, GeminiFake(site(cfg, "gemini")))
    d = out["diagnostics"]
    assert f"- waited {d['waited_seconds']} seconds in all, {d['plan_seconds']} of them for the plan" in C.format_test_file(out)
    assert f"- report length: {len(out['text'])} characters; claude calls: {out['claude_calls']}" in C.format_test_file(out)
