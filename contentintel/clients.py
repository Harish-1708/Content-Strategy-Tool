"""Clients: how a call actually reaches a model. The gateway (a localhost FreeLLMAPI), Claude, and a scripted mock for tests.

Every client returns a Completion or raises; nothing here decides routing. The gateway client also checks which provider the
gateway really used: if it answered through a provider other than the one the route names, the answer is refused, because
the data may then have reached a provider nobody approved.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import contract
from ..contract import Failure


@dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    model: str = ""
    estimated: bool = False


def default_http(method, url, *, headers, body, timeout):
    """(status, parsed body, response headers). Network errors raise; an HTTP error status is returned, not raised."""
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"error": {"message": raw[:200].decode("utf-8", "replace")}}
        return exc.code, parsed, dict(exc.headers)


def _estimate(text):
    return max(1, len(text or "") // 4)


class GatewayClient:
    """Talks to the gateway on localhost with its unified key. It never sees provider keys."""

    def __init__(self, base_url, api_key, http=None, clock=time.monotonic):
        if not str(base_url).startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            raise ValueError("the gateway must be reached on localhost; it is never exposed as a public address")
        self.base_url, self.api_key, self.http, self.clock = str(base_url).rstrip("/"), api_key, http or default_http, clock

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        t0 = self.clock()
        try:
            status, body, headers = self.http("POST", self.base_url + "/v1/chat/completions", headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                                              body={"model": route["gateway_model"], "messages": messages, "max_tokens": max_tokens, "temperature": 0}, timeout=timeout)
        except Exception as exc:          # a network error, a timeout, a refused connection: classified, never raised raw
            raise Failure(contract.classify_exception(exc)["code"], f"the gateway could not be reached: {exc}") from None
        ms = int((self.clock() - t0) * 1000)
        text_error = str((body.get("error") or {}).get("message", "")) if isinstance(body, dict) else ""
        if status in (401, 403):
            raise Failure(contract.NEED_SETUP, "the gateway rejected its key")
        if status in (402, 429):
            retry = headers.get("Retry-After") or headers.get("retry-after") or ""
            raise Failure(contract.RATE_LIMITED, text_error or f"the gateway answered {status}", retry_after=int(retry) if str(retry).isdigit() else None)
        if status == 400:
            raise Failure(contract.BAD_INPUT, text_error or "the gateway refused the request")
        if status >= 500 or status != 200:
            raise Failure(contract.UPSTREAM_BROKEN, text_error or f"the gateway answered {status}")
        if expect_platform:
            used = (body.get("_routed_via") or {}).get("platform")
            if used != expect_platform:
                raise Failure(contract.BAD_INPUT, f"the gateway routed through {used!r}, not the approved {expect_platform!r}; the answer is refused")
        try:
            choice = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise Failure(contract.UPSTREAM_BROKEN, "the gateway's answer had no message") from None
        usage = body.get("usage") or {}
        return Completion(text=choice or "", tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)),
                          latency_ms=ms, model=str(body.get("model") or route["model"]))


class AnthropicClient:
    """Claude through Anthropic's Messages API directly, so every call reports its real token counts (the cost ledger never guesses).

    It needs a funded API key (ANTHROPIC_API_KEY): a Claude.ai subscription does not include API use. Spending is capped by the
    router before each call; this client only makes the call.
    """

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key, http=None, clock=time.monotonic):
        self.api_key, self.http, self.clock = api_key, http or default_http, clock

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        t0 = self.clock()
        body = {"model": route["gateway_model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        try:
            status, data, headers = self.http("POST", self.URL, headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                              body=body, timeout=timeout)
        except Exception as exc:
            raise Failure(contract.classify_exception(exc)["code"], f"Anthropic could not be reached: {exc}") from None
        message = str((data.get("error") or {}).get("message", "")) if isinstance(data, dict) else ""
        if status in (401, 403):
            raise Failure(contract.NEED_SETUP, "Anthropic rejected the API key")
        if status == 402 or (status == 400 and "credit balance" in message.lower()):
            raise Failure(contract.RATE_LIMITED, "the Anthropic credit balance is exhausted")
        if status in (429, 529):
            retry = headers.get("retry-after") or headers.get("Retry-After") or ""
            raise Failure(contract.RATE_LIMITED, message or "Anthropic rate limit", retry_after=int(retry) if str(retry).isdigit() else None)
        if status == 400:
            raise Failure(contract.BAD_INPUT, message or "Anthropic refused the request")
        if status != 200:
            raise Failure(contract.UPSTREAM_BROKEN, message or f"Anthropic answered {status}")
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise Failure(contract.UPSTREAM_BROKEN, "Anthropic's answer had no text") from None
        usage = data.get("usage") or {}
        return Completion(text=text, tokens_in=int(usage.get("input_tokens", 0)), tokens_out=int(usage.get("output_tokens", 0)),
                          latency_ms=int((self.clock() - t0) * 1000), model=str(data.get("model") or route["model"]))


class ClaudeAdapter:
    """The existing Claude client behind the same interface. It needs a funded API key; the mock stands in until one is approved."""

    def __init__(self, claude, clock=time.monotonic):
        self.claude, self.clock = claude, clock

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        t0 = self.clock()
        text = self.claude.complete(system, prompt, max_tokens)
        return Completion(text=text, tokens_in=_estimate(system + prompt), tokens_out=_estimate(text), latency_ms=int((self.clock() - t0) * 1000),
                          model=getattr(self.claude, "model", route["model"]), estimated=True)


class MockClient:
    """A scripted stand-in. Each item is a string, a Completion, or an exception to raise. Calls are recorded for assertions."""

    def __init__(self, script, name="mock"):
        self.script, self.calls, self.name = list(script), [], name

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        self.calls.append({"route": route, "system": system, "prompt": prompt, "max_tokens": max_tokens, "expect_platform": expect_platform, "lane": lane})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item) and not isinstance(item, (BaseException, Completion)):
            item = item(route, prompt, lane)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, Completion):
            return item
        return Completion(text=str(item), tokens_in=_estimate(system + prompt), tokens_out=_estimate(str(item)), latency_ms=5, model=route["model"])
