"""A second look at a Deep Research report: is it what was asked for?

After Gemini's report arrives, Claude reads it against the original question and says either that it is good enough or what is
missing. If something important is missing, ONE follow-up message is sent to Gemini in the same chat, asking for exactly that, and
the answer is added to the report under its own heading. How many rounds are allowed is a setting (collectors.claude.review_rounds,
0 switches the check off); each round costs one Claude call, and a follow-up may use up part of Gemini's own Deep Research allowance.

Claude judges completeness and checkability (does it answer every part, are the examples dated and linked); it cannot confirm that
the facts are true, and the report says so rather than implying it can.
"""
from .providers import ProviderError

VERDICTS = ("ok", "follow_up")

REVIEW_SYSTEM = (
    "You check a research report written by Gemini Deep Research for a marketing agency against what was asked. Decide whether it "
    "answers every part of the request, whether its examples are concrete (named brands or creators, dates, links), and whether it "
    "is organised so a creative team can use it. You cannot verify facts, so judge completeness and checkability only. Reply with "
    "JSON only: {\"verdict\": \"ok\" or \"follow_up\", \"problems\": [\"up to 4 short problems, most important first\"], "
    "\"follow_up\": \"if the verdict is follow_up: ONE message to send Gemini, under 120 words, asking only for what is missing or "
    "unsupported (for example the examples that have no link or date, or the part of the request that was not answered); empty if "
    "the verdict is ok\"}. Say ok if the report is good enough to use even though it could be longer. Never ask for something the "
    "request did not ask for, and never ask Gemini to start the whole research again.")


def clean_review(raw):
    """A valid review from a model's answer, or ProviderError. An unusable follow_up becomes ok, with the reason in problems."""
    if not isinstance(raw, dict) or raw.get("verdict") not in VERDICTS:
        raise ProviderError("Claude's check of the report was not in the expected shape")
    problems = [" ".join(p.split())[:160] for p in raw.get("problems") or [] if isinstance(p, str) and p.strip()][:4]
    follow = " ".join(str(raw.get("follow_up") or "").split())[:800]
    verdict = raw["verdict"]
    if verdict == "follow_up" and not follow:
        verdict = "ok"
        problems.append("Claude wanted a follow-up but wrote none, so the report stands as it is")
    return {"verdict": verdict, "problems": problems, "follow_up": follow if verdict == "follow_up" else ""}


def review_report(claude, question, prompt, report, sources, round_no):
    """Claude's check of one report. One call."""
    user = (f"Original question:\n{question}\n\nPrompt that was sent to Gemini:\n{prompt[:3000]}\n\n"
            f"Report (check number {round_no}):\n{report[:12000]}\n\nSource links found in the report: {len(sources)}")
    return clean_review(claude.json_task(REVIEW_SYSTEM, user, max_tokens=900))


def send_followup(browser, site_cfg, message):
    """Type the follow-up into the same chat and send it. False if the box or the button could not be found."""
    return bool(browser.type_text(site_cfg["prompt_box"], message) and browser.click(site_cfg["send"]))


def wait_for_followup(browser, site_cfg, before, seconds_allowed):
    """Wait for the answer to a follow-up: text that is new, long enough and no longer growing.
    Returns (text or "", seconds waited, finished)."""
    wait = site_cfg["wait"]
    poll, waited, stable, last = wait["poll_seconds"], 0, 0, ""
    while waited < seconds_allowed:
        browser.wait(poll)
        waited += poll
        text = browser.text(site_cfg["response"])
        stable = stable + poll if text and text == last else 0
        last = text
        working = browser.exists(site_cfg.get("still_working_any", []))
        if text and text != before and len(text) >= wait["min_chars"] // 4 and not working and stable >= wait["settle_seconds"]:
            return text, waited, True
    return (last if last != before else ""), waited, False


def join_report(report, followups):
    """The report followed by each follow-up's question and answer, under their own headings."""
    parts = [report]
    for n, f in enumerate(followups, start=1):
        if f.get("answer"):
            parts.append(f"## Follow-up {n}: what Claude asked Gemini to add\n{f['message']}\n\n## Gemini's answer to follow-up {n}\n{f['answer']}")
    return "\n\n".join(parts)
