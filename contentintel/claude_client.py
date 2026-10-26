"""Claude, used to help Gemini Deep Research: it writes the research prompt, and answers any
clarifying question Gemini asks before it starts. Needs ANTHROPIC_API_KEY."""
from .providers import Provider, ProviderError

NO_QUESTION = "NO_QUESTION"


class TruncatedError(ProviderError):
    """Claude stopped because it ran out of room, so the text ends in the middle of a sentence."""

PREPARE_SYSTEM = (
    "You write prompts for Gemini Deep Research on behalf of a marketing agency. Turn the research question "
    "and the brand context into ONE complete, self-contained prompt, written so that Gemini never needs to ask a "
    "clarifying question (if something is unclear, the prompt says which sensible assumption to make and to state it "
    "at the top of the report).\n"
    "The prompt must contain, in this order: (1) the goal, and the decision the findings will inform; (2) the scope: "
    "product category, audience, platforms, time frame (default: the last 12 months) and market (default: US, "
    "English-language, unless the context says otherwise); (3) a numbered list of what to examine, for advertising "
    "and creative research including: the hook in the first seconds, the offer and promotion, the claims and proof "
    "used, the format and length, who is shown or speaking, objections handled, calls to action and seasonality; "
    "(4) the evidence rules: every example and every number must carry a working source link and a date; say "
    "'not found' instead of guessing; never state engagement or sales figures unless the source shows them; label each "
    "finding 'observed' (seen at a source) or 'inferred' (a conclusion drawn from what was seen); "
    "(5) the output: short headed sections, a ranked list of the top findings, one table of examples with columns "
    "brand, platform, format, hook, offer, date and link, a section on what this means for the brand's ads (only if a "
    "brand is given), a short list of gaps and how confident each finding is, and a final 'Sources' list.\n"
    "Do not invent facts about the brand beyond the context given. Keep the whole prompt under 450 words and always "
    "finish it: a prompt that stops in the middle is worse than a shorter one. Reply with the prompt text only: no "
    "preamble, no code fences.")
ANSWER_SYSTEM = (
    "You answer on behalf of a marketing agency. Gemini Deep Research has replied to our research request. "
    "If it asks clarifying questions, answer them concisely and decisively using only the brand context and "
    "the original request; where information is missing, make a sensible choice and say so. If its message "
    f"is not asking us anything (a plan, a status), reply with exactly {NO_QUESTION}. Reply with the answer only.")


class Claude(Provider):
    name = "claude"
    label = "Claude"
    env_var = "ANTHROPIC_API_KEY"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key, http=None, sleep=None, model="claude-sonnet-5-5", max_tokens=700, prompt_max_tokens=2000):
        super().__init__(api_key, http=http, sleep=sleep)
        self.model, self.max_tokens, self.prompt_max_tokens = model, max_tokens, prompt_max_tokens

    def complete(self, system, user, max_tokens=None):
        body = self._call("POST", self.url, headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                                      "content-type": "application/json"},
                          json_body={"model": self.model, "max_tokens": max_tokens or self.max_tokens, "system": system,
                                     "messages": [{"role": "user", "content": user}]})
        parts = body.get("content") if isinstance(body, dict) else None
        text = "".join(p.get("text", "") for p in (parts or []) if isinstance(p, dict) and p.get("type") == "text").strip()
        if not text:
            raise ProviderError(f"{self.label}: the answer had no text")
        if isinstance(body, dict) and body.get("stop_reason") == "max_tokens":
            raise TruncatedError(f"{self.label}: the answer was cut off after {max_tokens or self.max_tokens} tokens")
        return text

    def _complete_whole(self, system, user, first_limit):
        """The answer, never a cut-off one: if it runs out of room it is asked again with twice the room, once."""
        try:
            return self.complete(system, user, max_tokens=first_limit)
        except TruncatedError:
            return self.complete(system, user, max_tokens=first_limit * 2)

    def check(self):
        """A tiny real call that proves the key and the model name are accepted."""
        self.complete("Reply with the single word OK.", "ping")
        return True

    def prepare_prompt(self, question, context):
        return self._complete_whole(PREPARE_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nResearch question:\n{question}",
                                    self.prompt_max_tokens)

    def answer(self, original_prompt, context, gemini_message):
        """The reply to send Gemini, or "" when its message was not asking anything."""
        reply = self._complete_whole(ANSWER_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nOur research request:\n"
                                                    f"{original_prompt}\n\nGemini's message:\n{gemini_message}", self.max_tokens)
        return "" if reply.strip().upper().startswith(NO_QUESTION) else reply
