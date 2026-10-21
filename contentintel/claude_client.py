"""Claude, used to help Gemini Deep Research: it writes the research prompt, and answers any
clarifying question Gemini asks before it starts. Needs ANTHROPIC_API_KEY."""
from .providers import Provider, ProviderError

NO_QUESTION = "NO_QUESTION"

PREPARE_SYSTEM = (
    "You write prompts for Gemini Deep Research on behalf of a marketing agency. Turn the research question "
    "and the brand context into ONE clear, self-contained prompt that leaves no reason for clarifying "
    "questions: state the goal, the brand and its market, what to compare, the time frame, which public "
    "sources to prefer (ad libraries, social posts, reviews, news), and the output wanted (sections, ranked "
    "findings, examples with links). Do not invent facts about the brand beyond the context given. "
    "Reply with the prompt only.")
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

    def __init__(self, api_key, http=None, sleep=None, model="claude-sonnet-5-5", max_tokens=700):
        super().__init__(api_key, http=http, sleep=sleep)
        self.model, self.max_tokens = model, max_tokens

    def complete(self, system, user, max_tokens=None):
        body = self._call("POST", self.url, headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                                      "content-type": "application/json"},
                          json_body={"model": self.model, "max_tokens": max_tokens or self.max_tokens, "system": system,
                                     "messages": [{"role": "user", "content": user}]})
        parts = body.get("content") if isinstance(body, dict) else None
        text = "".join(p.get("text", "") for p in (parts or []) if isinstance(p, dict) and p.get("type") == "text").strip()
        if not text:
            raise ProviderError(f"{self.label}: the answer had no text")
        return text

    def check(self):
        """A tiny real call that proves the key and the model name are accepted."""
        self.complete("Reply with the single word OK.", "ping")
        return True

    def prepare_prompt(self, question, context):
        return self.complete(PREPARE_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nResearch question:\n{question}")

    def answer(self, original_prompt, context, gemini_message):
        """The reply to send Gemini, or "" when its message was not asking anything."""
        reply = self.complete(ANSWER_SYSTEM, f"Brand context:\n{context or '(none given)'}\n\nOur research request:\n"
                                             f"{original_prompt}\n\nGemini's message:\n{gemini_message}")
        return "" if reply.strip().upper().startswith(NO_QUESTION) else reply
