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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is returned as an answer, never followed behind the caller's back (a followed POST silently becomes a GET that answers something else)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def default_http(method, url, *, headers, body, timeout):
    """(status, parsed body, response headers). Network errors raise; an HTTP error status, including a redirect, is returned, not raised."""
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read() or b"{}"
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except ValueError:                                           # an answer that is not JSON: keep a short look at it, so the failure can say what the server actually returned
                return r.status, {"_not_json": raw[:300].decode("utf-8", "replace"), "_content_type": r.headers.get("Content-Type", "")}, dict(r.headers)
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
        if isinstance(body, dict) and "_not_json" in body:
            raise Failure(contract.UPSTREAM_BROKEN, f"the gateway answered {status} with something that is not JSON ({body.get('_content_type') or 'no content type'}): {body['_not_json'][:200]!r}")
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


USER_AGENT = "KelsonResearchBot/1.0"

RETIRED_HOSTS = {"models.github.ai": "GitHub Models was retired by GitHub on 30 July 2026: its inference API and model catalogue no longer exist for any customer, so no token, permission or request change can bring it back. "
                                     "It is not called. The other configured providers (Groq, OpenRouter, Google, Cloudflare, Claude) take its place."}


class DirectClient:
    """A provider with an OpenAI-style endpoint called straight from here (OpenRouter). The token is sent as a header and never appears in a message or a result. A redirect is never followed behind the
    scenes: a 307/308 to the same https host is followed once, anything else is reported with where it pointed. An endpoint of a provider that has been retired is refused before any request is made."""

    def __init__(self, base_url, token, http=None, clock=time.monotonic):
        from urllib.parse import urlsplit
        if not str(base_url).startswith("https://"):
            raise ValueError("a direct provider is only reached over https")
        host = urlsplit(str(base_url)).netloc.lower()
        for retired_host, why in RETIRED_HOSTS.items():
            if host == retired_host or host.endswith("." + retired_host):
                raise Failure(contract.PROVIDER_RETIRED, why)
        self.base_url, self.token, self.http, self.clock = str(base_url).rstrip("/"), token, http or default_http, clock

    def _clean(self, text):
        return str(text).replace(self.token, "***") if self.token else str(text)

    def _same_host(self, url):
        from urllib.parse import urlsplit
        return str(url).startswith("https://") and urlsplit(str(url)).netloc.lower() == urlsplit(self.base_url).netloc.lower()

    def _headers(self):
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}

    def _body(self, route, messages, max_tokens):
        return {"model": route["model"], "messages": messages, "max_tokens": max_tokens, "temperature": 0}

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        headers = self._headers()
        body = self._body(route, messages, max_tokens)
        url = self.base_url + "/chat/completions"
        t0 = self.clock()
        try:
            for hop in range(2):
                status, answer, reply = self.http("POST", url, headers=headers, body=body, timeout=timeout)
                where = reply.get("Location") or reply.get("location") or ""
                if status in (307, 308) and hop == 0 and self._same_host(where):          # the same request, to the same host: safe to repeat once
                    url = where
                    continue
                break
        except Exception as exc:
            raise Failure(contract.classify_exception(exc)["code"], self._clean(f"the provider could not be reached: {exc}")) from None
        ms = int((self.clock() - t0) * 1000)
        if 300 <= status < 400:
            raise Failure(contract.UPSTREAM_BROKEN, self._clean(f"the provider redirected the request ({status}) to {where or 'an unknown address'}"))
        err = answer.get("error") if isinstance(answer, dict) else None
        text_error = self._clean((err.get("message") if isinstance(err, dict) else err) or "")[:200] if err else ""
        if status in (401, 403):
            raise Failure(contract.NEED_SETUP, "the provider rejected the token")
        if status in (402, 429):
            retry = reply.get("Retry-After") or reply.get("retry-after") or ""
            raise Failure(contract.RATE_LIMITED, text_error or f"the provider answered {status}: its free limit was reached", retry_after=int(retry) if str(retry).isdigit() else None)
        if status in (400, 413, 422):
            raise Failure(contract.BAD_INPUT, text_error or "the provider refused the request (often: more than its per-request token limit)")
        if status == 404 and "free" in text_error.lower():
            raise Failure(contract.UPSTREAM_BROKEN, "Every routed provider reports the model as not found or removed upstream (1 attempt(s)). " + text_error)
        if status != 200:
            raise Failure(contract.UPSTREAM_BROKEN, text_error or f"the provider answered {status}")
        if isinstance(answer, dict) and "_not_json" in answer:                 # what the provider really sent back, with the token hidden
            raise Failure(contract.UPSTREAM_BROKEN, self._clean(f"the provider answered {status} with something that is not JSON ({answer.get('_content_type') or 'no content type'}): {answer['_not_json'][:200]!r}"))
        try:
            choice = answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise Failure(contract.UPSTREAM_BROKEN, "the provider's answer had no message") from None
        usage = answer.get("usage") or {}
        return Completion(text=choice or "", tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)), latency_ms=ms,
                          model=str(answer.get("model") or route["model"]))


class OpenRouterClient(DirectClient):
    """OpenRouter's free tier, called directly. Free models are added and withdrawn every few days, so a fixed model name keeps going stale (both gpt-oss-120b:free and the replacements were withdrawn
    within days). Before the first call it reads OpenRouter's public model list and asks for the models that are free today, newest and largest first, with OpenRouter's own free router as the last
    choice; OpenRouter itself moves on to the next one if one is unavailable. Nothing paid is ever requested: only models whose prompt and completion prices are both zero."""

    ROUTER = "openrouter/free"
    MAX_MODELS = 3                       # the most OpenRouter accepts in one request's `models` list

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._free = None

    def free_models(self, timeout=20):
        if self._free is not None:
            return self._free
        found = []
        try:
            status, data, _ = self.http("GET", self.base_url + "/models", headers={"Accept": "application/json", "User-Agent": USER_AGENT}, body=None, timeout=timeout)
            for m in (data.get("data") if status == 200 and isinstance(data, dict) else None) or []:
                price = m.get("pricing") or {}
                modalities = (m.get("architecture") or {}).get("output_modalities") or ["text"]
                if str(m.get("id", "")).endswith(":free") and str(price.get("prompt", "1")) in ("0", "0.0") and str(price.get("completion", "1")) in ("0", "0.0") and "text" in modalities:
                    found.append((int(m.get("context_length") or 0), str(m["id"])))
        except Exception:                                                  # the list is a convenience: without it the free router is still asked
            found = []
        self._free = [mid for _, mid in sorted(found, reverse=True)[:3]]
        return self._free

    def _body(self, route, messages, max_tokens):
        body = super()._body(route, messages, max_tokens)
        wanted = route["model"]
        lead = ([] if wanted == self.ROUTER else [wanted]) + [m for m in self.free_models() if m != wanted]
        chain = lead[: self.MAX_MODELS - 1] + [self.ROUTER]                       # OpenRouter refuses a models list longer than three ("must have 3 items or fewer"); the free router is always the last of them
        body["model"], body["models"] = chain[0], chain
        return body


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
