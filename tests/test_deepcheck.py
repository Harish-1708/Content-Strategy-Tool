"""Claude's check of a Deep Research report, and the one follow-up it may send to Gemini."""
import pytest
import yaml

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import deepcheck as DC
from contentintel.providers import ProviderError

from fake_browser import FakeBrowser, FakeClaude, GeminiFake
from test_collectors import site
from test_deep_research_claude import collect_with_claude, seeds
from test_collectors import world as make_world

ANSWER = "Here are the missing examples. " * 40
OK = {"verdict": "ok", "problems": [], "follow_up": ""}
ASK = {"verdict": "follow_up", "problems": ["no links", "no dates"], "follow_up": "Please add a link and a date for every example brand."}


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


class FollowUpGemini(GeminiFake):
    """Gemini that answers a second message in the same chat: the answer grows, then stops (or never stops, if `stall`)."""

    def __init__(self, cfg, *, answer=ANSWER, answer_after=60, no_box=False, stall=False, **kw):
        super().__init__(cfg["sites"]["gemini"], **kw)
        self.cfg = cfg["sites"]["gemini"]
        self.answer, self.answer_after, self.no_box, self.stall = answer, answer_after, no_box, stall
        self.followup_at, self.followups = None, []

    def type_text(self, selectors, text, submit=False):
        if self.started_at is not None and self.no_box:
            return False
        return super().type_text(selectors, text, submit)

    def on_click(self, selector):
        super().on_click(selector)
        if selector in self.cfg["send"] and self.started_at is not None and self.clock - self.started_at >= self.done_after:
            self.followup_at = self.clock
            self.followups.append(self.typed[-1])

    def dynamic_present(self):
        out = super().dynamic_present()
        if self.followup_at is not None:
            out.discard(self.cfg["done_any"][0])
            out.discard(self.cfg["still_working_any"][0])
            if self.stall or self.clock - self.followup_at < self.answer_after:
                out.add(self.cfg["still_working_any"][0])
        return out

    def dynamic_text(self):
        if self.followup_at is None:
            return super().dynamic_text()
        share = min(0.5 if self.stall else 1.0, (self.clock - self.followup_at) / self.answer_after)
        return self.answer[:int(len(self.answer) * share)]


def run(cfg, gem, claude, rounds=1, budget=10, **kw):
    return C.collect_deep_research(gem, "Who are the competitors?", site(cfg, "gemini"), claude=claude, claude_budget=budget, review_rounds=rounds, **kw)


def claude_with(*verdicts):
    claude = FakeClaude()
    claude.json_answers = list(verdicts)
    return claude


# ---- reading Claude's verdict ---------------------------------------------------------------------------------------------------

def test_a_good_verdict_is_kept_and_tidied():
    assert DC.clean_review({"verdict": "ok", "problems": ["  a   b ", "", 5, "c"], "follow_up": "ignored"}) == {"verdict": "ok", "problems": ["a b", "c"], "follow_up": ""}
    assert DC.clean_review(ASK) == {"verdict": "follow_up", "problems": ["no links", "no dates"], "follow_up": "Please add a link and a date for every example brand."}


def test_problems_and_the_follow_up_are_limited_in_number_and_length():
    got = DC.clean_review({"verdict": "follow_up", "problems": [f"p{n}" + "x" * 300 for n in range(9)], "follow_up": "w " * 900})
    assert len(got["problems"]) == 4 and all(len(p) <= 160 for p in got["problems"]) and len(got["follow_up"]) == 800


def test_a_follow_up_with_no_message_becomes_ok_and_says_why():
    got = DC.clean_review({"verdict": "follow_up", "problems": ["thin"], "follow_up": "  "})
    assert got["verdict"] == "ok" and got["follow_up"] == "" and got["problems"][-1] == "Claude wanted a follow-up but wrote none, so the report stands as it is"


@pytest.mark.parametrize("raw", [None, [], "ok", {}, {"verdict": "maybe"}, {"verdict": None}, {"problems": []}])
def test_an_unusable_verdict_is_an_error(raw):
    with pytest.raises(ProviderError, match="not in the expected shape"):
        DC.clean_review(raw)


def test_the_review_sends_the_question_the_prompt_the_report_and_the_source_count():
    claude = claude_with(OK)
    out = DC.review_report(claude, "Who are the rivals?", "THE PROMPT", "R" * 20000, ["https://a", "https://b"], 2)
    system, user, limit = claude.json_calls[0]
    assert out == OK and limit == 900
    assert "Original question:\nWho are the rivals?" in user and "Prompt that was sent to Gemini:\nTHE PROMPT" in user
    assert "Report (check number 2):\n" + "R" * 12000 in user and "R" * 12001 not in user and "Source links found in the report: 2" in user
    assert "cannot verify facts" in system and "never ask Gemini to start the whole research again" in system and "ONE message" in system


def test_a_claude_failure_in_the_review_reaches_the_caller():
    with pytest.raises(ProviderError, match="down"):
        DC.review_report(claude_with(ProviderError("Claude: down")), "q", "p", "r", [], 1)


# ---- sending and waiting ------------------------------------------------------------------------------------------------------------

def test_a_follow_up_is_typed_into_the_chat_and_sent(cfg):
    g = site(cfg, "gemini")
    gem = GeminiFake(g)
    assert DC.send_followup(gem, g, "Add dates please") is True
    assert gem.typed == ["Add dates please"] and gem.clicks[-1] in g["send"]


def test_a_follow_up_that_cannot_be_typed_or_sent_says_so(cfg):
    g = site(cfg, "gemini")
    assert DC.send_followup(FakeBrowser({}), g, "x") is False
    no_send = FakeBrowser({}, typeable=set(g["prompt_box"]))
    assert DC.send_followup(no_send, g, "x") is False


class Scripted(FakeBrowser):
    """A page whose response text, poll by poll, is scripted."""

    def __init__(self, texts, working=()):
        super().__init__({})
        self.texts, self.working, self.polls = list(texts), set(working), 0

    def text(self, selectors):
        value = self.texts[min(self.polls, len(self.texts) - 1)]
        self.polls += 1
        return value

    def exists(self, selectors):
        return bool(self.working & set(selectors)) and self.polls - 1 in range(0, 100) and True


def waiting(cfg, texts, before="OLD REPORT", seconds=600, working=False):
    g = site(cfg, "gemini")
    b = Scripted(texts)
    if working:
        b.exists = lambda selectors: True
    return DC.wait_for_followup(b, g, before, seconds), g, b


def test_the_answer_is_what_is_new_long_enough_and_no_longer_growing(cfg):
    g = site(cfg, "gemini")
    full = "A" * (g["wait"]["min_chars"] // 4 + 50)
    (text, waited, finished), _, _ = waiting(cfg, ["OLD REPORT", full[:50], full, full, full, full, full, full, full, full])
    assert (text, finished) == (full, True) and waited >= g["wait"]["settle_seconds"] and waited % g["wait"]["poll_seconds"] == 0


def test_the_old_report_is_never_mistaken_for_the_answer(cfg):
    (text, waited, finished), _, _ = waiting(cfg, ["OLD REPORT"], seconds=120)
    assert (text, finished) == ("", False)


def test_an_answer_that_is_too_short_is_not_accepted(cfg):
    (text, waited, finished), _, _ = waiting(cfg, ["tiny"], seconds=300)
    assert finished is False and text == "tiny"


def test_an_answer_that_is_still_being_written_is_not_accepted(cfg):
    g = site(cfg, "gemini")
    full = "A" * (g["wait"]["min_chars"] // 4 + 50)
    (text, waited, finished), _, _ = waiting(cfg, [full], seconds=300, working=True)
    assert finished is False and waited == 300


def test_waiting_stops_at_the_time_allowed_and_hands_back_what_arrived(cfg):
    (text, waited, finished), g, _ = waiting(cfg, ["OLD REPORT", "part one", "part one and two"], seconds=45)
    assert (text, finished) == ("part one and two", False) and waited == 45 // g["wait"]["poll_seconds"] * g["wait"]["poll_seconds"] or waited >= 45


def test_no_time_means_no_waiting(cfg):
    (text, waited, finished), _, b = waiting(cfg, ["x"], seconds=0)
    assert (text, waited, finished) == ("x" if False else "", 0, False) and b.polls == 0


def test_the_report_and_its_follow_ups_are_joined_under_their_own_headings():
    out = DC.join_report("THE REPORT", [{"message": "add dates", "answer": "Dated list."}, {"message": "skipped", "answer": ""}, {"message": "add links", "answer": "Linked list."}])
    assert out == ("THE REPORT\n\n## Follow-up 1: what Claude asked Gemini to add\nadd dates\n\n## Gemini's answer to follow-up 1\nDated list.\n\n"
                   "## Follow-up 3: what Claude asked Gemini to add\nadd links\n\n## Gemini's answer to follow-up 3\nLinked list.")
    assert DC.join_report("ONLY", []) == "ONLY" and DC.join_report("ONLY", [{"message": "m", "answer": ""}]) == "ONLY"


# ---- in a Deep Research run ----------------------------------------------------------------------------------------------------------

def test_a_report_that_is_good_enough_is_left_alone_and_costs_one_check(cfg):
    gem, claude = FollowUpGemini(cfg), claude_with(OK)
    out = run(cfg, gem, claude)
    assert gem.followups == [] and out["followups"] == [] and out["reviews"] == [{"round": 1, **OK}] and out["text"] == out["raw_text"]
    assert out["claude_calls"] == 2 and out["diagnostics"]["review_rounds_used"] == 1                  # preparing the prompt, and the check
    assert any("cannot confirm that the facts are true" in n for n in out["notes"])


def test_a_report_that_falls_short_gets_one_follow_up_and_the_answer_is_added(cfg):
    gem, claude = FollowUpGemini(cfg), claude_with(ASK)
    out = run(cfg, gem, claude)
    assert gem.followups == ["Please add a link and a date for every example brand."]
    assert out["followups"] == [{"message": "Please add a link and a date for every example brand.", "answer": ANSWER.strip(), "finished": True}]
    assert out["text"].startswith(out["raw_text"]) and out["text"] != out["raw_text"] and "## Follow-up 1: what Claude asked Gemini to add" in out["text"] and ANSWER.strip() in out["text"]
    assert out["reviews"][0]["verdict"] == "follow_up" and out["claude_calls"] == 2 and out["diagnostics"]["waited_seconds"] == out["waited_seconds"]


def test_the_follow_up_waiting_time_counts_in_the_total(cfg):
    gem = FollowUpGemini(cfg, answer_after=120)
    plain = run(cfg, FollowUpGemini(cfg), claude_with(OK))
    asked = run(cfg, gem, claude_with(ASK))
    assert asked["waited_seconds"] >= plain["waited_seconds"] + 120


def test_with_two_rounds_a_second_check_sees_the_first_answer(cfg):
    gem, claude = FollowUpGemini(cfg), claude_with(ASK, OK)
    out = run(cfg, gem, claude, rounds=2)
    assert len(out["reviews"]) == 2 and [r["verdict"] for r in out["reviews"]] == ["follow_up", "ok"] and len(gem.followups) == 1
    assert "## Gemini's answer to follow-up 1" in claude.json_calls[1][1] and "Report (check number 2)" in claude.json_calls[1][1]


def test_rounds_stop_at_the_limit_even_if_claude_keeps_asking(cfg):
    gem, claude = FollowUpGemini(cfg), claude_with(ASK, ASK, ASK)
    out = run(cfg, gem, claude, rounds=1)
    assert len(out["reviews"]) == 1 and len(gem.followups) == 1 and len(claude.json_calls) == 1


def test_with_the_check_switched_off_nothing_is_asked(cfg):
    gem, claude = FollowUpGemini(cfg), claude_with()
    out = run(cfg, gem, claude, rounds=0)
    assert claude.json_calls == [] and out["reviews"] == [] and out["followups"] == [] and out["claude_calls"] == 1
    assert not any("cannot confirm" in n for n in out["notes"])


def test_without_claude_there_is_no_check(cfg):
    out = C.collect_deep_research(FollowUpGemini(cfg), "q", site(cfg, "gemini"), claude=None, review_rounds=1)
    assert out["reviews"] == [] and out["claude_calls"] == 0


def test_when_the_call_limit_is_reached_the_check_is_skipped_with_a_note(cfg):
    out = run(cfg, FollowUpGemini(cfg), claude_with(OK), budget=1)                  # the one call goes on preparing the prompt
    assert out["reviews"] == [] and "the check of the report was skipped: Claude's call limit is reached" in out["notes"]


def test_a_failing_check_keeps_the_report_and_says_so(cfg):
    out = run(cfg, FollowUpGemini(cfg), claude_with(ProviderError("Claude: overloaded")))
    assert out["reviews"] == [] and out["followups"] == [] and any("Claude could not check the report (Claude: overloaded); it stands as it is" in n for n in out["notes"])


def test_a_follow_up_that_cannot_be_sent_keeps_the_report_with_a_picture(cfg):
    gem = FollowUpGemini(cfg, no_box=True)
    out = run(cfg, gem, claude_with(ASK))
    assert out["followups"] == [] and out["text"] == out["raw_text"] and "gemini-followup-not-sent" in gem.shots
    assert any("could not be sent (prompt box or send button not found); the report stands as it is" in n for n in out["notes"])


def test_an_answer_that_never_finishes_is_included_as_far_as_it_got_and_flagged(cfg):
    gem = FollowUpGemini(cfg, stall=True)
    out = run(cfg, gem, claude_with(ASK))
    (follow,) = out["followups"]
    assert follow["finished"] is False and 0 < len(follow["answer"]) < len(ANSWER) and "gemini-followup-timeout" in gem.shots
    assert any("the answer to follow-up 1 was not finished in time; what had arrived is included" in n for n in out["notes"])


def test_an_answer_that_never_starts_adds_nothing_and_ends_the_rounds(cfg):
    gem = FollowUpGemini(cfg, answer="")
    out = run(cfg, gem, claude_with(ASK, ASK), rounds=2)
    assert out["text"] == out["raw_text"] and len(out["reviews"]) == 1 and any("nothing arrived" in n for n in out["notes"])


def test_a_doubled_follow_up_answer_is_cleaned_like_a_doubled_report(cfg):
    from test_deep_research_quality import DOUBLED
    gem = FollowUpGemini(cfg, answer=DOUBLED, answer_after=30)
    out = run(cfg, gem, claude_with(ASK))
    answer = out["followups"][0]["answer"]
    assert answer.startswith("# Category Study") and "Marketing Strategy Analysis" not in answer


def test_links_in_the_follow_up_answer_join_the_sources(cfg):
    gem = FollowUpGemini(cfg, answer="More examples at https://new.example.com/page and https://a.example.com/x. " * 20)
    out = run(cfg, gem, claude_with(ASK))
    assert "https://new.example.com/page" in out["sources"] and out["sources"].count("https://a.example.com/x") == 1


def test_a_first_report_that_was_doubled_is_cleaned_before_it_is_checked(cfg):
    from test_deep_research_quality import DOUBLED
    claude = claude_with(OK)
    run(cfg, FollowUpGemini(cfg, report=DOUBLED), claude)
    assert "Marketing Strategy Analysis" not in claude.json_calls[0][1] and "# Category Study" in claude.json_calls[0][1]


# ---- the setting and the saved record ------------------------------------------------------------------------------------------------

def test_the_check_is_on_by_default_for_one_round_and_can_be_switched_off(tmp_path):
    assert C.load_collector_settings("does-not-exist.yaml").claude_review_rounds == 1 == C.load_collector_settings("research.yaml").claude_review_rounds
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"claude": {"review_rounds": 0}}}))
    assert C.load_collector_settings(path).claude_review_rounds == 0
    path.write_text(yaml.safe_dump({"collectors": {"claude": {"review_rounds": 3}}}))
    assert C.load_collector_settings(path).claude_review_rounds == 3


@pytest.mark.parametrize("value", [-1, 1.5, "one", True])
def test_a_bad_number_of_rounds_is_refused(tmp_path, value):
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"claude": {"review_rounds": value}}}))
    with pytest.raises(C.CollectError, match="review_rounds"):
        C.load_collector_settings(path)


def test_a_collect_run_saves_the_checks_and_the_follow_ups(tmp_path, cfg):
    from contentintel.store import Store
    store = Store(tmp_path / "data")
    brand = __import__("conftest").load_all(__import__("conftest").BRANDS_DIR)["swoveralls"]
    f = make_world(cfg, gem=FollowUpGemini(cfg))
    claude = claude_with(ASK)
    result = collect_with_claude(store, tmp_path, brand, seeds(), f, cfg, C.load_collector_settings("does-not-exist.yaml"), claude)
    (rec,) = [r for r in store.read_collected("swoveralls") if r["source"] == "deep_research"]
    assert rec["deep"]["reviews"][0]["verdict"] == "follow_up" and rec["deep"]["followups"] == [{"message": ASK["follow_up"], "finished": True}]
    full = store.read_deep_report("swoveralls", rec["items"][0]["url"].rsplit("/", 1)[-1])
    assert "## Gemini's answer to follow-up 1" in full and ANSWER.strip() in full            # the row keeps only the start; the saved file has it all
    assert rec["deep"]["diagnostics"]["review_rounds_used"] == 1
    assert [r for r in result.brands["swoveralls"] if r.source == "deep_research"][0].status == "ran"


def test_an_old_report_that_is_long_enough_is_still_never_taken_for_the_answer(cfg):
    g = site(cfg, "gemini")
    old = "OLD REPORT TEXT " * (g["wait"]["min_chars"] // 8)
    assert len(old) >= g["wait"]["min_chars"] // 4
    (text, waited, finished), _, _ = waiting(cfg, [old], before=old, seconds=300)
    assert (text, finished) == ("", False) and waited == 300
