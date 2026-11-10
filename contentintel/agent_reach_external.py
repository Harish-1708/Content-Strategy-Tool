"""Agent-Reach, run as Agent-Reach itself: its own `agent-reach` command line, installed from its repository (the newest version by default), with the platform's own adapters as the backup.

`ExternalAgentReach` runs `agent-reach <channel> <action> <inputs> --timeout N` in an environment that holds only what the connection declares it needs (contentintel/connections.py), reads the JSON
result Agent-Reach prints ({"ok", "items", "error": {"code", "message"}}) and returns the items or raises the platform's own Failure with the same error code.
`RoutedAgentReach` puts the two together under the owner's route for this connection and records the health of every external attempt.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from . import agent_reach_catalog as CAT
from . import connections as CONN
from . import contract as K

OPTIONAL = ("limit", "location", "city", "lang")          # the inputs Agent-Reach takes as --name value; every other input is given in order


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ExternalAgentReach:
    needs_browser = False

    def __init__(self, settings, *, binary, environ, runner=subprocess.run, timeout=90):
        self.settings, self.binary, self.environ, self.runner, self.timeout = settings, binary, environ, runner, timeout

    def command(self, channel, action, values):
        declared = CAT.DECLARED.get(channel, {}).get(action)
        if declared is None:
            raise K.Failure(K.BAD_INPUT, f"Agent-Reach has no {channel} {action} action")
        cmd = [self.binary, channel, action]
        cmd += [str(values[name]) for name in declared if name not in OPTIONAL and values.get(name)]
        for name in declared:
            if name in OPTIONAL and values.get(name):
                cmd += [f"--{name}", str(values[name])]
        return cmd + ["--timeout", str(self.timeout)]

    def call_action(self, channel, action, values, argv=None, neutral=None):
        cmd = self.command(channel, action, values)
        try:
            done = self.runner(cmd, env=CONN.external_env(self.settings, "agent_reach", self.environ), capture_output=True, text=True, timeout=self.timeout + 30)
        except FileNotFoundError:
            raise K.Failure(K.NEED_SETUP, "Agent-Reach is not installed here (run the connections install action)") from None
        except subprocess.TimeoutExpired:
            raise K.Failure(K.TIMEOUT, f"Agent-Reach did not answer within {self.timeout + 30} seconds") from None
        lines = [line for line in (done.stdout or "").splitlines() if line.strip().startswith("{")]
        try:
            envelope = json.loads(lines[-1])
        except (IndexError, ValueError):
            tail = " ".join((done.stderr or done.stdout or "").split())[-200:]
            raise K.Failure(K.UPSTREAM_BROKEN, f"Agent-Reach gave no readable result ({tail or 'no output'})") from None
        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            code = err.get("code")
            raise K.Failure(code if code in K.CODES else K.UPSTREAM_BROKEN, str(err.get("message") or "Agent-Reach reported an error")[:300])
        return list(envelope.get("items") or [])


class RoutedAgentReach:
    """What the shared interface hands to a module: the repository's own Agent-Reach and the platform's adapter, run in the order the owner chose."""

    def __init__(self, settings, external, builtin, now_fn=_now):
        self.settings, self.external, self.builtin, self.now_fn = settings, external, builtin, now_fn
        self.last_route = ""

    @property
    def needs_browser(self):
        return getattr(self.builtin, "needs_browser", True)

    @property
    def environ(self):
        return getattr(self.builtin, "environ", None)

    def available(self):
        return self.builtin.available() if hasattr(self.builtin, "available") else True

    def call(self, args):                                                       # the older call by command words: the platform's own adapter
        return self.builtin.call(args)

    def call_action(self, channel, action, values, argv=None, neutral=None):
        rows, which = CONN.run_connection(self.settings, "agent_reach", lambda: self.external.call_action(channel, action, values),
                                          lambda: self.builtin.call(argv if argv is not None else neutral), now=self.now_fn())
        self.last_route = which
        return rows
