"""The FreeLLMAPI gateway as a sidecar: build its configuration, start it on localhost, get its key, stop it, mask its logs.

Pinned by ai/gateway.lock.json (tag and full commit). The gateway never receives a secret that is not needed: only enabled
providers get a key. Every baseline model that is not on the approved-model allowlist is switched off in its configuration,
and its remote catalogue is disabled, so nothing outside version control can change what is routable.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..safety import scrub
from .clients import default_http

ADMIN_EMAIL = "sidecar@localhost.invalid"
_SETUP_CODE = re.compile(r"(?i)(setup code:?\s*)[A-Z0-9]{6,}")
_UNIFIED_KEY = re.compile(r"freellmapi-[A-Za-z0-9_\-]{8,}")


class GatewayError(Exception):
    pass


def load_lock(path="ai/gateway.lock.json"):
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    commit = lock.get("gateway", {}).get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise GatewayError("the gateway pin must be a full 40-character commit hash")
    return lock


def _credential(provider, environ):
    values = [environ.get(name, "").strip() for name in provider.credential_env]
    if not provider.credential_env or not all(values):
        return None
    return ":".join(values)         # Cloudflare's adapter reads account_id:token


def build_config(registry, environ, baseline_models):
    """(config, report). baseline_models: {platform: [modelId, ...]} as shipped with the pinned gateway (docs/ai/provider_inventory.json)."""
    password = environ.get("FREEAPI_ADMIN_PASSWORD", "")
    if len(password) < 8:
        raise GatewayError("FREEAPI_ADMIN_PASSWORD (at least 8 characters) is required: without an admin the gateway prints an open setup code")
    keys, skipped, models = [], [], []
    for p in registry.providers.values():
        if p.id == "claude" or p.direct:
            continue                                    # Claude, and providers with a direct client, are never sent through the gateway
        if not p.enabled:
            continue
        cred = _credential(p, environ)
        if cred is None and p.credential_env:
            skipped.append({"provider": p.id, "reason": "enabled, but no credential in the environment: " + ", ".join(p.credential_env)})
            continue
        if p.credential_env:
            keys.append({"platform": p.gateway_platform, "key": cred, "label": "actions", "enabled": True})
    live = {k["platform"] for k in keys} | {p.gateway_platform for p in registry.providers.values() if p.enabled and not p.credential_env and p.id != "claude" and not p.direct}
    for platform, ids in sorted(baseline_models.items()):
        provider = next((p for p in registry.providers.values() if p.gateway_platform == platform), None)
        for model_id in ids:
            on = bool(provider and provider.enabled and platform in live and registry.model_ok(provider.id, model_id))
            models.append({"platform": platform, "modelId": model_id, "enabled": on, "fallbackEnabled": on})
    config = {"admin": {"email": ADMIN_EMAIL, "password": password}, "keys": keys, "models": models}
    return config, {"keys": [k["platform"] for k in keys], "skipped": skipped, "models_on": sum(1 for m in models if m["enabled"]), "models_off": sum(1 for m in models if not m["enabled"])}


def gateway_env(lock, environ, *, port, db_path, config):
    """The environment for the gateway process. Settings that must stay unset are removed, never inherited."""
    env = {k: v for k, v in environ.items() if k not in lock.get("must_stay_unset", [])}
    env.update(lock["required_env"])
    encryption = environ.get("FREEAPI_ENCRYPTION_KEY", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", encryption):
        raise GatewayError("FREEAPI_ENCRYPTION_KEY must be 64 hex characters")
    env.update({"PORT": str(port), "ENCRYPTION_KEY": encryption, "FREEAPI_DB_PATH": str(Path(db_path).resolve()), "FREEAPI_CONFIG_JSON": json.dumps(config)})
    return env


def redact_log(text):
    """A gateway log line with the first-run setup code, its own key and any provider secret hidden."""
    return scrub(_UNIFIED_KEY.sub("***", _SETUP_CODE.sub(lambda m: m.group(1) + "***", text)))


class Sidecar:
    def __init__(self, gateway_dir, lock, *, popen=subprocess.Popen, http=default_http, sleep=time.sleep, clock=time.monotonic):
        self.dir, self.lock, self.popen, self.http, self.sleep, self.clock = gateway_dir, lock, popen, http, sleep, clock
        self.proc, self.base_url, self.log_path = None, "", None

    def start(self, env, port, log_path):
        self.base_url, self.log_path = f"http://127.0.0.1:{port}", Path(log_path)
        self.proc = self.popen(["node", "server/dist/index.js"], cwd=self.dir, env=env, stdout=open(self.log_path, "w"), stderr=subprocess.STDOUT)
        return self

    def wait_ready(self, timeout=40):
        end = self.clock() + timeout
        while self.clock() < end:
            if self.proc.poll() is not None:
                raise GatewayError("the gateway exited while starting: " + self.log_tail())
            try:
                status, _, _ = self.http("GET", self.base_url + "/v1/models", headers={}, body=None, timeout=2)
                if status in (200, 401):
                    return
            except Exception:
                pass
            self.sleep(0.5)
        raise GatewayError("the gateway did not become ready in time: " + self.log_tail())

    def bootstrap(self, password):
        """Log in as the provisioned admin and read the unified key. Returns the key (never logged)."""
        status, body, _ = self.http("POST", self.base_url + "/api/auth/login", headers={"Content-Type": "application/json"},
                                    body={"email": ADMIN_EMAIL, "password": password}, timeout=20)
        token = body.get("token") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise GatewayError(f"the gateway admin login failed ({status})")
        status, body, _ = self.http("GET", self.base_url + "/api/settings/api-key", headers={"Authorization": "Bearer " + token}, body=None, timeout=20)
        key = body.get("apiKey") if isinstance(body, dict) else None
        if status != 200 or not key:
            raise GatewayError(f"the gateway did not give its key ({status})")
        return key

    def admin_models(self, password):
        status, body, _ = self.http("POST", self.base_url + "/api/auth/login", headers={"Content-Type": "application/json"}, body={"email": ADMIN_EMAIL, "password": password}, timeout=20)
        status, rows, _ = self.http("GET", self.base_url + "/api/models", headers={"Authorization": "Bearer " + body["token"]}, body=None, timeout=20)
        return rows if isinstance(rows, list) else []

    def log_tail(self, lines=12):
        try:
            return redact_log("".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-lines:]))[:1500]
        except Exception:
            return ""

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()


def baseline_from_inventory(path="docs/ai/provider_inventory.json"):
    inv = json.loads(Path(path).read_text(encoding="utf-8"))
    return {p["id"]: [m["modelId"] for m in p["models"]] for p in inv["platforms"] if p["models"]}


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def env_ready(environ=None):
    environ = environ if environ is not None else os.environ
    return bool(environ.get("FREEAPI_ENCRYPTION_KEY") and environ.get("FREEAPI_ADMIN_PASSWORD"))
