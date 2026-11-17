"""OmniRoute as the second gateway beside FreeLLMAPI.

OmniRoute (https://github.com/diegosouzapw/OmniRoute, MIT, installed from npm at a PINNED version) is run on localhost for the length of one job. This tool starts it, logs in with a password it makes up for
this run, creates one client key, and registers each enabled provider's key with it; the router then calls it like any OpenAI-style endpoint. The ORDER of providers, the failover between them and the
fallback to Claude are the tool's own ranked lanes (ai_lanes.yaml), never OmniRoute's: OmniRoute only holds the connections. It is never exposed beyond localhost, no key is written to a file in the
repository or printed, and its data folder is scratch space that is thrown away with the run.
"""
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from . import clients as cl

PINNED_VERSION = "3.8.53"                                                            # the version the live proof was made on (2026-11-17); the workflow installs exactly this one


class OmniRouteError(Exception):
    pass


def entry_point(omni_dir):
    """The launcher inside an `npm install omniroute` folder (or the package folder itself), or None."""
    for rel in ("node_modules/omniroute/bin/omniroute.mjs", "bin/omniroute.mjs"):
        if omni_dir and Path(omni_dir, rel).is_file():
            return Path(omni_dir, rel)
    return None


class OmniRouteSidecar:
    def __init__(self, omni_dir, *, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        self.dir, self.popen, self.clock, self.sleep = omni_dir, popen, clock, sleep
        self.proc, self.base_url, self.log_path = None, "", None
        self.password = secrets.token_urlsafe(24)                                       # made up for this run; never stored
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def start(self, port, data_dir, log_path):
        launcher = entry_point(self.dir)
        if launcher is None:
            raise OmniRouteError(f"OmniRoute is not installed at {self.dir} (the workflow runs `npm install omniroute@{PINNED_VERSION}` there)")
        self.base_url, self.log_path = f"http://127.0.0.1:{port}", Path(log_path)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "NODE_OPTIONS")}
        env.update({"DATA_DIR": str(data_dir), "PORT": str(port), "INITIAL_PASSWORD": self.password, "JWT_SECRET": secrets.token_hex(24), "API_KEY_SECRET": secrets.token_hex(24),
                    "STORAGE_ENCRYPTION_KEY": secrets.token_hex(16), "OMNIROUTE_HEADLESS": "1", "NEXT_TELEMETRY_DISABLED": "1"})
        self.proc = self.popen(["node", str(launcher), "serve", "--no-open", "--headless"], env=env, stdout=open(self.log_path, "w"), stderr=subprocess.STDOUT)
        return self

    def _request(self, method, path, body=None, headers=None, timeout=20):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {"_text": raw[:300]})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"_text": raw[:300]}

    def wait_ready(self, timeout=90):
        end = self.clock() + timeout
        while self.clock() < end:
            if self.proc is not None and self.proc.poll() is not None:
                raise OmniRouteError(f"OmniRoute stopped while starting (exit {self.proc.returncode}); see its log")
            try:
                if self._request("GET", "/api/health", timeout=3)[0] == 200:
                    return self
            except (urllib.error.URLError, OSError, ValueError):
                pass
            self.sleep(1)
        raise OmniRouteError(f"OmniRoute did not answer on {self.base_url} within {timeout} seconds")

    def bootstrap(self, key_name="contentintel"):
        """Log in with this run's password and create the one client key the tool uses. Returns the key."""
        status, body = self._request("POST", "/api/auth/login", {"password": self.password})
        if status != 200 or not body.get("success"):
            raise OmniRouteError(f"OmniRoute refused the login it was started with (HTTP {status})")
        status, body = self._request("POST", "/api/keys", {"name": key_name})
        if status not in (200, 201) or not body.get("key"):
            raise OmniRouteError(f"OmniRoute did not create a client key (HTTP {status})")
        return str(body["key"])

    def add_provider(self, provider_id, name, api_key):
        """Register one provider's key under OmniRoute's own provider id (or a custom node's id). Returns the connection id."""
        status, body = self._request("POST", "/api/providers", {"provider": provider_id, "name": name, "apiKey": api_key, "isActive": True})
        connection = body.get("connection") if isinstance(body, dict) else None
        if status not in (200, 201) or not connection:
            raise OmniRouteError(f"OmniRoute did not accept the {provider_id} connection (HTTP {status})")
        connection_id = str(connection.get("id", ""))
        if not connection.get("isActive"):                                                  # a new connection starts inactive ("No active credentials for provider"): it is switched on here
            status, body = self._request("PUT", f"/api/providers/{connection_id}", {"isActive": True})
            if status != 200:
                raise OmniRouteError(f"OmniRoute did not switch the {provider_id} connection on (HTTP {status})")
        return connection_id

    def add_custom_node(self, name, prefix, base_url):
        """An OpenAI-compatible endpoint of your own (used to prove the wiring against a local stand-in). Returns the node's provider id."""
        status, body = self._request("POST", "/api/provider-nodes", {"name": name, "prefix": prefix, "baseUrl": base_url, "type": "openai-compatible", "apiType": "chat"})
        node = body.get("node") if isinstance(body, dict) else None
        if status not in (200, 201) or not node:
            raise OmniRouteError(f"OmniRoute did not accept the custom node (HTTP {status})")
        return str(node["id"])

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:                                                              # noqa: BLE001
                self.proc.kill()


class OmniRouteClient(cl.GatewayClient):
    """Calls OmniRoute's OpenAI-style endpoint on localhost. The approved model id of an OmniRoute provider is the id OmniRoute routes by (`provider/model`), so the route's own model is what is sent."""

    def complete(self, route, system, prompt, max_tokens, timeout=90, expect_platform=None, lane=None):
        return super().complete({**route, "gateway_model": route["model"]}, system, prompt, max_tokens, timeout=timeout, lane=lane)


class Sidecars:
    """More than one local gateway in one run (FreeLLMAPI and OmniRoute): stop() stops them all, and anything else asked of it is the first one's."""

    def __init__(self, *sides):
        self.sides = [s for s in sides if s is not None]

    def stop(self):
        for side in self.sides:
            try:
                side.stop()
            except Exception:                                                              # noqa: BLE001 - one that will not stop must not keep the others running
                pass

    def __getattr__(self, name):
        return getattr(self.sides[0], name)
