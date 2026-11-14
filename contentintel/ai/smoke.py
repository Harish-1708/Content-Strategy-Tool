"""A real-gateway smoke test that uses NO provider account: the pinned gateway, a local fake model behind it, and our own client.

It proves the plumbing (bootstrap, localhost-only binding, authentication, the model allowlist, routing, answer-provider check,
token counts). It is a MOCK-PROVIDER result: it says nothing about whether any live provider works.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import gateway as gw
from .clients import GatewayClient, default_http


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "smoke-model", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.server.seen.append(body.get("model"))
        self._send({"id": "s", "object": "chat.completion", "created": 1, "model": body.get("model", "smoke-model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "SMOKE-OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}})


def expected_enabled(registry, environ):
    """{(platform, model id)} the gateway is MEANT to have switched on for this configuration: the approved models of enabled gateway providers that have their credential, or need none (Kilo). Claude and a
    provider called directly are never sent through the gateway. Every other model must be off."""
    out = set()
    for p in registry.providers.values():
        if p.id == "claude" or p.direct or not p.enabled:
            continue
        if p.credential_env and gw._credential(p, environ) is None:
            continue
        out |= {(p.gateway_platform, m.id) for m in p.models if m.approved}
    return out


def switch_check(rows, registry, environ):
    """(ok, detail): the gateway's model rows against the allowlist. A model that is on but not meant to be (not approved, or its provider has no credential) or an approved model that is off, is named."""
    want = expected_enabled(registry, environ)
    seen = {(r.get("platform"), r.get("modelId") or r.get("model_id")): bool(r.get("enabled")) for r in rows if r.get("platform") != "custom"}
    on = sorted(k for k, v in seen.items() if v and k not in want)
    off = sorted(k for k in want if k in seen and not seen[k])
    name = lambda keys: (": " + ", ".join(f"{p}/{m}" for p, m in keys[:6])) if keys else ""      # noqa: E731
    return bool(seen) and not on and not off, f"{len(rows)} rows, {len(on)} wrongly enabled{name(on)}, {len(off)} approved but off{name(off)}, {sum(1 for k in want if seen.get(k))} approved and on"


def keyless_check(rows, registry):
    """(ok, detail): every approved model of an enabled key-less provider (Kilo) must sit on a platform that holds the gateway's stored 'no-key' row, or the gateway answers 'no key configured for their
    platform' (live check #21, Kilo). Rows the gateway does not list are not judged."""
    seen = {(r.get("platform"), r.get("modelId") or r.get("model_id")): int(r.get("keyCount") or 0) for r in rows if r.get("platform") != "custom"}
    wanted = [(p.gateway_platform, m.id) for p in registry.providers.values() if p.enabled and p.id != "claude" and not p.direct and not p.credential_env for m in p.models if m.approved]
    missing = sorted(k for k in wanted if k in seen and seen[k] < 1)
    return not missing, f"{sum(1 for k in wanted if seen.get(k, 0) >= 1)} key-less model(s) have their stored row" + (": missing for " + ", ".join(f"{p}/{m}" for p, m in missing[:4]) if missing else "")


def run_smoke(gateway_dir, registry, lock, workdir, baseline):
    """Returns a list of (check, passed, detail). Never raises for a failed check; raises GatewayError only if the gateway cannot start."""
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), str(detail)))

    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    up_port, port = gw.free_port(), gw.free_port()
    upstream = HTTPServer(("127.0.0.1", up_port), _Upstream)
    upstream.seen = []
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    password, encryption = "smoke-" + os.urandom(6).hex(), os.urandom(32).hex()
    environ = dict(os.environ, FREEAPI_ADMIN_PASSWORD=password, FREEAPI_ENCRYPTION_KEY=encryption)
    config, report = gw.build_config(registry, environ, baseline)
    config["customProviders"] = [{"baseUrl": f"http://127.0.0.1:{up_port}/v1", "apiKey": "smoke-upstream-key", "label": "Smoke", "models": [{"model": "smoke-model", "displayName": "Smoke"}]}]
    env = gw.gateway_env(lock, environ, port=port, db_path=work / "gateway.db", config=config)
    for suffix in ("", "-wal", "-shm"):
        (work / f"gateway.db{suffix}").unlink(missing_ok=True)
    side = gw.Sidecar(gateway_dir, lock)
    try:
        side.start(env, port, work / "gateway.log")
        side.wait_ready()
        check("gateway starts and answers", True, side.base_url)
        status, _, _ = default_http("GET", side.base_url + "/v1/models", headers={}, body=None, timeout=10)
        check("a call with no key is refused (401)", status == 401, status)
        key = side.bootstrap(password)
        check("admin login and key retrieval work", bool(key), "key length %d (not shown)" % len(key))
        rows = side.admin_models(password)
        ok, detail = switch_check(rows, registry, environ)
        check("every baseline model outside the allowlist is switched off", ok, detail)
        ok, detail = keyless_check(rows, registry)
        check("a key-less provider has the gateway's stored no-key row", ok, detail)
        client = GatewayClient(side.base_url, key)
        done = client.complete({"provider": "custom", "model": "smoke-model", "gateway_model": "smoke-model"}, "", "say OK", 16, expect_platform="custom")
        check("a chat call is routed to the expected provider", done.text == "SMOKE-OK" and upstream.seen == ["smoke-model"], done.text)
        check("token counts come back", (done.tokens_in, done.tokens_out) == (9, 2), (done.tokens_in, done.tokens_out))
        try:
            client.complete({"provider": "groq", "model": "smoke-model", "gateway_model": "smoke-model"}, "", "x", 16, expect_platform="groq")
            check("an answer from an unexpected provider is refused", False, "it was accepted")
        except Exception as exc:
            check("an answer from an unexpected provider is refused", "not the approved" in str(exc), str(exc)[:120])
        log = side.log_tail(60)
        check("the log shows no unified key and no setup code", "freellmapi-" not in log and "setup code" not in log.lower().replace("***", ""), "")
    finally:
        side.stop()
        upstream.shutdown()
    return results, report
