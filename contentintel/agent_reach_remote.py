"""Agent-Reach's seven OpenCLI channels (instagram, reddit, twitter, facebook, linkedin, xiaohongshu, boss), run on Google Cloud Run and called from here over HTTPS.

OpenCLI needs Node, Chrome and a browser logged in to the site, on a machine we control, never a GitHub-hosted runner. So Agent-Reach runs in a container on our own Cloud Run (cloudrun/agent-reach/), with a small
HTTP wrapper around its command line, and this module is how an Actions run asks it: `RemoteAgentReach.call_action(channel, action, values)` has the same shape as the local runner (agent_reach_external.ExternalAgentReach),
sends only the declared inputs, and turns the answer into the platform's own result or Failure with the same error codes. The owner's switches for each channel are not touched: they are checked before a client is made.

Nothing here is live-tested: the service is built but not used yet (docs/deferred-live-checks.md)."""
from __future__ import annotations

import json
import socket
from urllib.parse import urlsplit

from . import agent_reach_catalog as CAT
from . import contract as K
from .ai.clients import default_http

OPENCLI_CHANNELS = tuple(sorted(name for name, spec in CAT.CHANNELS.items() if spec["integration"] == "opencli"))
MAX_VALUE = 300
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def check_url(url):
    """The service address: https only (a local test server may use http). Returns the address without a trailing slash, or raises NEED_SETUP."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme == "https" and parts.hostname or parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return str(url).strip().rstrip("/")
    raise K.Failure(K.NEED_SETUP, "AGENT_REACH_URL must be the https address of the Agent-Reach service on Cloud Run")


class RemoteAgentReach:
    needs_browser = False

    def __init__(self, settings, *, url, token, id_token="", http=default_http, timeout=90):
        self.settings, self.token, self.id_token, self.http, self.timeout = settings, str(token or "").strip(), str(id_token or "").strip(), http, timeout
        self.url = check_url(url)
        if len(self.token) < 24:
            raise K.Failure(K.NEED_SETUP, "AGENT_REACH_TOKEN must be set (at least 24 characters) to call the Agent-Reach service")

    def payload(self, channel, action, values):
        if channel not in OPENCLI_CHANNELS:
            raise K.Failure(K.BAD_INPUT, f"the Agent-Reach service runs only the OpenCLI channels ({', '.join(OPENCLI_CHANNELS)}), not {channel}")
        declared = CAT.DECLARED.get(channel, {}).get(action)
        if declared is None:
            raise K.Failure(K.BAD_INPUT, f"Agent-Reach has no {channel} {action} action")
        return {"channel": channel, "action": action, "timeout": self.timeout,
                "values": {name: str(values[name])[:MAX_VALUE] for name in declared if values.get(name) not in (None, "")}}

    def headers(self):
        headers = {"Content-Type": "application/json", "X-Agent-Reach-Token": self.token}
        if self.id_token:
            headers["Authorization"] = "Bearer " + self.id_token                    # Cloud Run's own sign-in, when the service is deployed to require it
        return headers

    def call_action(self, channel, action, values, argv=None, neutral=None):
        body = self.payload(channel, action, values)
        try:
            status, parsed, _ = self.http("POST", self.url + "/run", headers=self.headers(), body=body, timeout=self.timeout + 60)
        except Exception as exc:                                                 # only the kind of error is said: its text can hold an address or a token
            if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
                raise K.Failure(K.TIMEOUT, f"the Agent-Reach service did not answer within {self.timeout + 60} seconds") from None
            raise K.Failure(K.UPSTREAM_BROKEN, f"the Agent-Reach service could not be reached ({type(exc).__name__})") from None
        if status in (401, 403):
            raise K.Failure(K.NEED_SETUP, f"the Agent-Reach service refused the call (HTTP {status}): check the AGENT_REACH_TOKEN secret, and AGENT_REACH_ID_TOKEN if the service requires Google sign-in")
        if status == 429:
            raise K.Failure(K.RATE_LIMITED, "the Agent-Reach service asked for a pause (HTTP 429)")
        if status != 200 or not isinstance(parsed, dict):
            raise K.Failure(K.UPSTREAM_BROKEN, f"the Agent-Reach service answered HTTP {status} without a readable result")
        if not parsed.get("ok"):
            err = parsed.get("error") or {}
            code = err.get("code")
            raise K.Failure(code if code in K.CODES else K.UPSTREAM_BROKEN, str(err.get("message") or "the Agent-Reach service reported an error")[:300])
        return list(parsed.get("items") or [])


def configured(environ):
    """(url, token) when the service is set up for this run, else ('', '')."""
    url, token = str((environ or {}).get("AGENT_REACH_URL") or "").strip(), str((environ or {}).get("AGENT_REACH_TOKEN") or "").strip()
    return (url, token) if url and token else ("", "")


def dumps(envelope):
    return json.dumps(envelope, ensure_ascii=False)
