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
        wrong = [r for r in rows if r.get("platform") != "custom" and r.get("enabled")]
        check("every baseline model outside the allowlist is switched off", not wrong and any(r.get("platform") != "custom" for r in rows),
              f"{len(rows)} rows, {len(wrong)} wrongly enabled")
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
