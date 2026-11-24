"""Clients: how a call actually reaches a model. The gateway (a localhost FreeLLMAPI), Claude, and a scripted mock for tests.

Every client returns a Completion or raises; nothing here decides routing. The gateway client also checks which provider the
gateway really used: if it answered through a provider other than the one the route names, the answer is refused, because
the data may then have reached a provider nobody approved.
"""
import json
import re
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
                                              body={"model": route.get("gateway_model") or route["model"], "messages": messages, "max_tokens": max_tokens, "temperature": 0}, timeout=timeout)
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
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:                                                      # a keyless provider (OVH's free tier) is sent NO Authorization header: a key there would bill
            h["Authorization"] = "Bearer " + self.token
        return h

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


class FreeDiscoveryClient(DirectClient):
    """A direct provider whose FREE model ids are not published anywhere but its own live list (AnyAPI): the ids pinned in the registry are only the intent. Before a call, GET {base}/models is read once; a pinned id that
    is in the live list is used as it is, and one that is not (the provider test of 2026-11-23 found qwen/qwen3.8-27b:free does not exist) is replaced by a LIVE id that ends in :free from the same vendor and model family.
    Only a :free id is ever substituted, never a paid one; with none left the route fails with the live free ids named, so the registry can be corrected from the log."""
    _live = None

    def _free_ids(self):
        if self._live is None:
            try:
                status, answer, _r = self.http("GET", self.base_url + "/models", headers=self._headers(), body=None, timeout=30)
                rows = answer.get("data") if status == 200 and isinstance(answer, dict) else []
                self._live = [str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]
            except Exception:                                                                    # noqa: BLE001 - no list: the pinned id is tried as it is
                self._live = []
        return self._live

    @staticmethod
    def _family(model):
        vendor, _s, name = model.partition("/")
        stem = re.match(r"[a-z]+", name.lower()) if name else None
        return vendor.lower(), (stem.group(0) if stem else "")

    def resolve(self, wanted):
        live = self._free_ids()
        if not live or wanted in live:
            return wanted
        family = self._family(wanted)
        for cand in sorted(i for i in live if i.endswith(":free")):
            if self._family(cand) == family:
                return cand
        raise Failure(contract.UPSTREAM_BROKEN, f"Every routed provider reports the model as not found or removed upstream (1 attempt(s)). Model {wanted!r} is not in the live list and no :free model of the same family is; live free ids: " + ", ".join(sorted(i for i in live if i.endswith(":free"))[:14]))

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        actual = self.resolve(route["model"])
        return super().complete({**route, "model": actual}, system, prompt, max_tokens, timeout=timeout, expect_platform=expect_platform, lane=lane)


class HordeClient(DirectClient):
    """AI Horde (https://aihorde.net), a community-run network of volunteer workers. Anonymous use: the public key 0000000000, the lowest queue priority. It is asynchronous: the prompt is submitted, then polled until a
    worker has answered. Volunteer workers can technically see prompts, so only the data classes the owner approved for it are ever sent. A job that has not finished within the timeout is cancelled and reported, never left queued."""
    ANON_KEY = "0000000000"
    POLL_SECONDS = 3

    def __init__(self, base_url, token="", http=None, clock=time.monotonic, sleep=time.sleep):
        super().__init__(base_url, "", http=http, clock=clock)
        self.sleep = sleep

    def _horde_headers(self):
        return {"apikey": self.ANON_KEY, "Client-Agent": "contentintel:1:owner", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        text = (system + "\n\n" if system else "") + prompt
        body = {"prompt": text, "params": {"max_length": max(16, min(int(max_tokens), 512)), "max_context_length": 2048, "temperature": 0.2}}
        model = str(route.get("model") or "")
        if model and not model.endswith("/any"):                           # `aihorde/any` lets any worker answer; a named model is asked for by name
            body["models"] = [model.split("/", 1)[1] if model.startswith("aihorde/") else model]
        t0 = self.clock()
        try:
            status, answer, _reply = self.http("POST", self.base_url + "/generate/text/async", headers=self._horde_headers(), body=body, timeout=30)
        except Exception as exc:                                           # noqa: BLE001
            raise Failure(contract.classify_exception(exc)["code"], f"AI Horde could not be reached: {exc}") from None
        if status in (401, 403):
            raise Failure(contract.NEED_SETUP, "AI Horde rejected the anonymous key")
        if status == 429:
            raise Failure(contract.RATE_LIMITED, "AI Horde's anonymous limit was reached")
        if status not in (200, 202) or not isinstance(answer, dict) or not answer.get("id"):
            raise Failure(contract.UPSTREAM_BROKEN, f"AI Horde did not accept the job (HTTP {status}): {str(answer)[:160]}")
        job = str(answer["id"])
        while True:
            if self.clock() - t0 > timeout:
                try:
                    self.http("DELETE", f"{self.base_url}/generate/text/status/{job}", headers=self._horde_headers(), body=None, timeout=15)
                except Exception:                                          # noqa: BLE001 - cancelling is best effort
                    pass
                raise Failure(contract.TIMEOUT if hasattr(contract, "TIMEOUT") else contract.UPSTREAM_BROKEN, f"AI Horde had no worker answer within {timeout} seconds (the job was cancelled)")
            try:
                status, answer, _reply = self.http("GET", f"{self.base_url}/generate/text/status/{job}", headers=self._horde_headers(), body=None, timeout=30)
            except Exception as exc:                                       # noqa: BLE001
                raise Failure(contract.classify_exception(exc)["code"], f"AI Horde could not be polled: {exc}") from None
            if status != 200 or not isinstance(answer, dict):
                raise Failure(contract.UPSTREAM_BROKEN, f"AI Horde's status answer was HTTP {status}")
            if answer.get("faulted"):
                raise Failure(contract.UPSTREAM_BROKEN, "AI Horde reported the job faulted")
            if answer.get("done"):
                gens = answer.get("generations") or []
                out = str((gens[0] or {}).get("text") or "") if gens else ""
                if not out.strip():
                    raise Failure(contract.UPSTREAM_BROKEN, "AI Horde finished the job with no text")
                return Completion(text=out, tokens_in=_estimate(text), tokens_out=_estimate(out), latency_ms=int((self.clock() - t0) * 1000), model=str((gens[0] or {}).get("model") or model), estimated=True)
            self.sleep(self.POLL_SECONDS)


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
        body = {"model": route.get("gateway_model") or route["model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
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
