import pytest

from contentintel import browser as B
from contentintel import claude_client as CC
from contentintel import collectors as C
from contentintel import providers as P
from contentintel import research as R
from contentintel.store import Store

from fake_browser import REPORT_TEXT, AskingGemini, FakeClaude, GeminiFake
from fake_http import FakeHttp
from test_collectors import CREDS, NOW, site, statuses, used, world, write_research

CLAUDE_URL = "https://api.anthropic.com/v1/messages"


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return C.load_collector_settings("does-not-exist.yaml")


def ask(cfg, gem, claude=None, **kw):
    kw.setdefault("claude_budget", 10)
    return C.collect_deep_research(gem, "Who are the competitors?", site(cfg, "gemini"), claude=claude, context="ctx", **kw)


# --- the Claude client ------------------------------------------------------------------------------------------------

def claude_http(*responses):
    http = FakeHttp()
    http.on("POST", CLAUDE_URL, *(responses or [(200, {"content": [{"type": "text", "text": "OK"}]})]))
    return http, CC.Claude("sk-ant-secret-123", http=http, sleep=lambda s: None)


def test_claude_request_shape_and_answer():
    http, claude = claude_http((200, {"content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": " Hello "}, {"type": "text", "text": "there "}]}))
    assert claude.complete("be brief", "hi") == "Hello there"
    (call,) = http.calls
    assert call["method"] == "POST" and call["url"] == CLAUDE_URL
    assert call["headers"] == {"x-api-key": "sk-ant-secret-123", "anthropic-version": "2023-06-01", "content-type": "application/json"}
    assert call["json"] == {"model": "claude-sonnet-5-5", "max_tokens": 700, "system": "be brief", "messages": [{"role": "user", "content": "hi"}]}


def test_claude_model_and_size_come_from_the_settings():
    http = FakeHttp()
    http.on("POST", CLAUDE_URL, (200, {"content": [{"type": "text", "text": "x"}]}))
    CC.Claude("k", http=http, model="claude-haiku-5-5", max_tokens=123).complete("s", "u")
    assert http.calls[0]["json"]["model"] == "claude-haiku-5-5" and http.calls[0]["json"]["max_tokens"] == 123


@pytest.mark.parametrize("response,error", [
    ((401, {"error": {"message": "invalid x-api-key"}}), P.AuthError), ((403, {"error": {}}), P.AuthError),
    ((400, {"error": {"message": "model: not found"}}), P.ProviderError), ((200, {"content": []}), P.ProviderError),
    ((200, {"content": [{"type": "text", "text": "   "}]}), P.ProviderError), ((200, ["not", "a", "dict"]), P.ProviderError)])
def test_claude_failures_are_clear_errors(response, error):
    http, claude = claude_http(response)
    with pytest.raises(error):
        claude.complete("s", "u")


def test_the_claude_key_never_appears_in_an_error():
    http, claude = claude_http((400, "bad request sk-ant-secret-123"))
    with pytest.raises(P.ProviderError) as info:
        claude.complete("s", "u")
    assert "sk-ant-secret-123" not in str(info.value)


def test_claude_retries_a_busy_server_once_then_succeeds():
    http, claude = claude_http((529, "overloaded"), (200, {"content": [{"type": "text", "text": "fine"}]}))
    assert claude.complete("s", "u") == "fine" and len(http.calls) == 2


def test_the_prompt_is_prepared_from_the_question_and_the_brand_context():
    http, claude = claude_http((200, {"content": [{"type": "text", "text": "FULL PROMPT"}]}))
    assert claude.prepare_prompt("Who are the rivals?", "Swoveralls sells overalls") == "FULL PROMPT"
    body = http.calls[0]["json"]
    assert body["system"] == CC.PREPARE_SYSTEM and "Swoveralls sells overalls" in body["messages"][0]["content"]
    assert "Who are the rivals?" in body["messages"][0]["content"]


def test_a_missing_brand_context_is_said_plainly_to_claude():
    http, claude = claude_http((200, {"content": [{"type": "text", "text": "P"}]}), (200, {"content": [{"type": "text", "text": "NO_QUESTION"}]}))
    claude.prepare_prompt("Q?", "")
    claude.answer("prompt", "", "msg")
    assert all("(none given)" in c["json"]["messages"][0]["content"] for c in http.calls)


@pytest.mark.parametrize("reply,expected", [
    ("NO_QUESTION", ""), ("  no_question.  ", ""), ("No_Question - this is just the plan", ""),
    ("US women aged 18-34.", "US women aged 18-34."), ("There is no question here, but go on.", "There is no question here, but go on.")])
def test_a_reply_that_says_there_was_no_question_is_not_sent_to_gemini(reply, expected):
    http, claude = claude_http((200, {"content": [{"type": "text", "text": reply}]}))
    assert claude.answer("the prompt", "the context", "Which audience?") == expected
    body = http.calls[0]["json"]
    assert body["system"] == CC.ANSWER_SYSTEM and CC.NO_QUESTION in CC.ANSWER_SYSTEM
    assert all(s in body["messages"][0]["content"] for s in ("the prompt", "the context", "Which audience?"))


def test_the_check_is_a_tiny_real_call():
    http, claude = claude_http()
    assert claude.check() is True and http.calls[0]["json"]["messages"][0]["content"] == "ping"


# --- Claude helping Gemini ---------------------------------------------------------------------------------------------

def test_the_prompt_is_prepared_by_claude_and_that_is_what_gemini_receives(cfg):
    gem, claude = GeminiFake(site(cfg, "gemini")), FakeClaude(prepared="A COMPLETE PROMPT")
    out = ask(cfg, gem, claude)
    assert gem.typed == ["A COMPLETE PROMPT"] and out["prompt"] == "A COMPLETE PROMPT" and out["claude_calls"] == 1
    assert claude.prepare_calls == [("Who are the competitors?", "ctx")] and out["text"] == REPORT_TEXT


@pytest.mark.parametrize("claude,kw", [(None, {}), (FakeClaude(), {"prepare_prompt": False}), (FakeClaude(), {"claude_budget": 0})])
def test_without_claude_or_when_told_not_to_the_question_is_sent_as_written(cfg, claude, kw):
    gem = GeminiFake(site(cfg, "gemini"))
    out = ask(cfg, gem, claude, **kw)
    assert gem.typed == ["Who are the competitors?"] and out["claude_calls"] == 0 and out["exchanges"] == []


def test_a_prompt_claude_cannot_prepare_falls_back_to_the_question_with_a_note(cfg):
    gem = GeminiFake(site(cfg, "gemini"))
    out = ask(cfg, gem, FakeClaude(fail_prepare=P.ProviderError("Claude: busy")))
    assert gem.typed == ["Who are the competitors?"] and out["claude_calls"] == 0
    assert "could not prepare the prompt (Claude: busy)" in out["notes"][0]


def test_a_clarifying_question_from_gemini_is_answered_by_claude(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Which audience should I focus on?"])
    claude = FakeClaude(prepared="PROMPT", answers=["US women aged 18-34."])
    out = ask(cfg, gem, claude)
    assert gem.typed == ["PROMPT", "US women aged 18-34."] and out["text"] == REPORT_TEXT
    assert out["exchanges"] == [{"gemini": "Which audience should I focus on?", "reply": "US women aged 18-34."}]
    assert claude.answer_calls == [("PROMPT", "ctx", "Which audience should I focus on?")] and out["claude_calls"] == 2
    assert gem.clicks.count(gem.cfg["send"][0]) == 2 and gem.shots == ["gemini-report"]


def test_several_questions_are_answered_in_turn_up_to_the_round_limit(cfg):
    questions, answers = ["Q1?", "Q2?"], ["A1", "A2"]
    ok = AskingGemini(site(cfg, "gemini"), questions)
    out = ask(cfg, ok, FakeClaude(answers=list(answers)), max_rounds=2)
    assert [e["reply"] for e in out["exchanges"]] == ["A1", "A2"]
    stuck = AskingGemini(site(cfg, "gemini"), questions)
    with pytest.raises(B.BrowserError, match="answer limit was reached.*Gemini said: Q2"):
        ask(cfg, stuck, FakeClaude(answers=list(answers)), max_rounds=1)
    assert stuck.shots == ["gemini-question-unanswered"]


def test_the_monthly_claude_allowance_limits_the_answers_too(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Q1?", "Q2?"])
    with pytest.raises(B.BrowserError, match="answer limit was reached"):
        ask(cfg, gem, FakeClaude(answers=["A1", "A2"]), claude_budget=2)          # one call to prepare, one to answer


def test_a_message_that_is_not_a_question_is_left_alone_and_asked_about_only_once(cfg):
    gem = AskingGemini(site(cfg, "gemini"), [], start_after=60)
    claude = FakeClaude()                                                         # always says: no question
    out = ask(cfg, gem, claude)
    assert len(claude.answer_calls) == 1 and out["exchanges"] == [] and gem.typed == ["PREPARED PROMPT"] and out["text"] == REPORT_TEXT


def test_a_question_that_lingers_on_screen_is_not_answered_twice(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Which audience?"], linger=60, start_after=90)
    claude = FakeClaude(answers=["US women.", "SECOND ANSWER"])
    out = ask(cfg, gem, claude)
    assert len(claude.answer_calls) == 1 and gem.typed == ["PREPARED PROMPT", "US women."] and len(out["exchanges"]) == 1


def test_claude_failing_to_answer_names_what_gemini_asked(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Which audience?"])
    with pytest.raises(B.BrowserError, match="Claude could not answer it \\(Claude: down\\).*Gemini said: Which audience"):
        ask(cfg, gem, FakeClaude(fail_answer=P.ProviderError("Claude: down")))
    assert gem.shots == ["gemini-question-unanswered"]


def test_an_answer_that_cannot_be_typed_is_reported(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Which audience?"])
    gem.on_type = lambda text: setattr(gem, "typeable", set())                     # the box vanishes after the first message
    with pytest.raises(B.BrowserError, match="could not type the answer"):
        ask(cfg, gem, FakeClaude(answers=["US women."]))
    assert gem.shots == ["gemini-answer-not-sent"]


def test_without_claude_a_question_cannot_be_answered_and_the_timeout_shows_what_gemini_said(cfg):
    gem = AskingGemini(site(cfg, "gemini"), ["Which audience?"])
    with pytest.raises(B.BrowserError, match="not finished after 25 minutes.*Last thing Gemini showed: Which audience"):
        ask(cfg, gem, None)


# --- through a whole run -------------------------------------------------------------------------------------------------

def seeds(context=""):
    return {"deep_research": {"questions": ["Who are the competitors?"], "context": context}}


def asking_world(cfg, questions):
    return world(cfg, gem=AskingGemini(site(cfg, "gemini"), questions))


def test_a_run_uses_claude_and_records_the_conversation_and_the_calls(tmp_path, store, swov, cfg, settings):
    claude = FakeClaude(prepared="PROMPT", answers=["US women."])
    f = asking_world(cfg, ["Which audience?"])
    result = collect_with_claude(store, tmp_path, swov, seeds("Sweatpant overalls for young adults."), f, cfg, settings, claude)
    assert statuses(result) == [("deep_research", "ran")] and "1 question(s) from Gemini answered by Claude" in result.brands["swoveralls"][0].detail
    (rec,) = store.read_collected("swoveralls")
    assert rec["deep"]["prompt"] == "PROMPT" and rec["deep"]["exchanges"][0]["reply"] == "US women." and rec["deep"]["claude_calls"] == 2
    assert used(store) == {"deep_research_tasks": 1, "claude_calls": 2}
    context = claude.prepare_calls[0][1]
    assert "Swoveralls" in context and "swoveralls.com" in context and "Sweatpant overalls for young adults." in context


def collect_with_claude(store, tmp_path, brand, data, factory, cfg, settings, claude, **kw):
    write_research(tmp_path, brand, data)
    return C.run_collect([brand], store, browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=["deep_research"], now=NOW, run_id="run1", root=str(tmp_path / "brands"), claude=claude, **kw)


def test_the_monthly_claude_allowance_is_respected_and_used_up_calls_are_counted(tmp_path, store, swov, cfg, settings):
    from dataclasses import replace
    store.save_usage({"months": {"2026-10": {"claude_calls": 58}}, "lifetime": {}})
    claude = FakeClaude()
    f = world(cfg)
    collect_with_claude(store, tmp_path, swov, seeds(), f, cfg, replace(settings, claude_monthly_calls=60), claude)
    assert len(claude.prepare_calls) == 1 and used(store)["claude_calls"] == 59
    other = Store(tmp_path / "other-data")
    unused = FakeClaude()
    none_left = collect_with_claude(other, tmp_path / "b", swov, seeds(), world(cfg), cfg, replace(settings, claude_monthly_calls=0), unused)
    assert statuses(none_left) == [("deep_research", "ran")] and unused.prepare_calls == []


def test_prompt_preparation_can_be_switched_off(tmp_path, store, swov, cfg, settings):
    from dataclasses import replace
    claude, f = FakeClaude(), world(cfg)
    collect_with_claude(store, tmp_path, swov, seeds(), f, cfg, replace(settings, claude_prepare_prompt=False), claude)
    assert claude.prepare_calls == [] and f.browsers["gemini"].typed == ["Who are the competitors?"]


def test_without_claude_a_question_makes_the_report_fail_with_gemini_s_words_in_the_message(tmp_path, store, swov, cfg, settings):
    f = asking_world(cfg, ["Which audience?"])
    result = collect_with_claude(store, tmp_path, swov, seeds(), f, cfg, settings, None)
    assert statuses(result) == [("deep_research", "failed")] and "Which audience" in result.brands["swoveralls"][0].detail
    assert store.read_collected("swoveralls") == [] and used(store) == {}


def test_the_dry_run_says_whether_claude_will_help(tmp_path, store, swov, cfg, settings):
    with_claude = collect_with_claude(store, tmp_path, swov, seeds(), world(cfg), cfg, settings, FakeClaude(), dry_run=True)
    assert "Claude prepares the prompt and answers Gemini's questions" in with_claude.brands["swoveralls"][0].detail
    without = collect_with_claude(store, tmp_path / "b", swov, seeds(), world(cfg), cfg, settings, None, dry_run=True)
    assert "no Claude" in without.brands["swoveralls"][0].detail


def test_the_brand_context_is_validated_and_tidied(tmp_path, swov):
    root = write_research(tmp_path, swov, {"deep_research": {"questions": ["Q?"], "context": "  a\n\n  b  " + "x" * 3000}})
    cfg = R.load_brand_research(swov, root)["collect"]["deep_research"]
    assert cfg["context"].startswith("a b xxx") and len(cfg["context"]) == 2000
    bad = write_research(tmp_path / "bad", swov, {"deep_research": {"questions": ["Q?"], "context": 5}})
    with pytest.raises(R.ResearchError, match="context must be text"):
        R.load_brand_research(swov, bad)


# --- the one-off test of the whole Deep Research path ---------------------------------------------------------------

def run_test(tmp_path, store, cfg, settings, f, claude=None, creds=CREDS):
    return C.run_deep_research_test(f, creds, cfg, settings, store, R.Ledger, NOW, claude, tmp_path / "debug")


def test_the_deep_research_test_runs_one_report_and_saves_it_for_reading_but_not_as_data(tmp_path, store, cfg, settings):
    f = asking_world(cfg, ["Which audience?"])
    claude = FakeClaude(prepared="TEST PROMPT", answers=["US women."])
    outcome = run_test(tmp_path, store, cfg, settings, f, claude)
    assert outcome["ok"] and "Claude made 2 call(s); 1 Gemini question(s) answered" in outcome["detail"]
    text = (tmp_path / "debug" / "test-deep-research.md").read_text(encoding="utf-8")
    assert "TEST PROMPT" in text and "Which audience?" in text and "US women." in text and REPORT_TEXT in text
    assert used(store) == {"claude_calls": 2} and store.read_collected("swoveralls") == []        # nothing counted as collected data
    assert C.TEST_QUESTION in claude.prepare_calls[0][0]


def test_the_deep_research_test_reports_what_went_wrong(tmp_path, store, cfg, settings):
    expired = run_test(tmp_path, store, cfg, settings, world(cfg, gem=GeminiFake(site(cfg, "gemini"), logged_in=False)))
    assert not expired["ok"] and "GEMINI_COOKIES" in expired["detail"]
    missing = run_test(tmp_path, store, cfg, settings, world(cfg), creds={"gemini": {}})
    assert missing == {"ok": False, "detail": "no login: set GEMINI_COOKIES"}
    stuck = run_test(tmp_path, store, cfg, settings, world(cfg, gem=GeminiFake(site(cfg, "gemini"), report="short")))
    assert not stuck["ok"] and "not finished" in stuck["detail"] and not (tmp_path / "debug" / "test-deep-research.md").exists()
