"""Connections: the platform uses other people's repositories as they are, and keeps its own versions as the backup.

A connection is a repository (a Python package with a command line, a library, a Node server, or a repository of data and templates). The owner controls each one in one place (Controls tab,
`observatory controls`, observatory.yaml):

  services.connections.channels.<id>          ON / OFF for the connection
  services.connection_routes.routes.<id>      external_first (the repo, then our own version), builtin_first, external_only, builtin_only
  services.connection_versions.routes.<id>    latest (always the newest) or pinned (the version recorded here)
  services.connection_secrets.channels.<id>   whether the secrets the repo declares it needs are handed to it (nothing else ever is)

Every connection has a health score (0 to 100) built from what really happened when it was used, kept in data/_shared/connections_health.json and shown in the Connections Health tab.
Nothing here decides for the owner: it records, scores, falls back when the repo fails, and says which route answered.

This module has no dependencies on the rest of the platform so that any repository can read it. `connections_extra.yaml` declares more connections without code (Stage B).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROUTES = ("external_first", "builtin_first", "external_only", "builtin_only")
VERSIONS = ("latest", "pinned")

# What a repository's own process always gets, whatever the owner's secrets are: enough to run, nothing that identifies the owner or opens an account.
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

CONNECTIONS = {
    "agent_reach": {
        "label": "Agent-Reach", "repo": "https://github.com/Panniantong/Agent-Reach", "kind": "pip_cli", "pip": "git+https://github.com/Panniantong/Agent-Reach", "binary": "agent-reach",
        "pinned": "", "probe": ["channels"], "secrets": ("GITHUB_TOKEN", "EXA_API_KEY", "JINA_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"),
        "fallback": "the platform's own OpenCLI, yt-dlp, GitHub and page-reader adapters (contentintel/agent_reach_catalog.py and friends)",
        "needs": "Python 3.10 or newer; Instagram, Reddit, X, Facebook and LinkedIn need a logged-in browser on a machine you control; transcription needs a Groq or OpenAI key",
    },
    "scrapling": {
        "label": "Scrapling", "repo": "https://github.com/D4Vinci/Scrapling", "kind": "pip_lib", "pip": "scrapling[fetchers]", "pip_extra": ("markdownify",), "pinned_requirements": "requirements-scrapling.txt",
        "pinned": "0.5", "probe": [], "secrets": (),                         # the worker is handed only the proxy and cookie secrets a job names, never anything else
        "fallback": "the built-in polite reader (Jina Reader, then the built-in fetcher)",
        "needs": "an isolated Python environment; the stealth browser needs a browser installed",
    },
    "freellmapi": {
        "label": "FreeLLMAPI", "repo": "https://github.com/tashfeenahmed/freellmapi", "kind": "node_server", "lock": "ai/gateway.lock.json", "binary": "node",
        "pinned": "", "probe": [], "secrets": ("FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD", "GROQ_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "ZHIPU_API_KEY", "OLLAMA_API_KEY", "COHERE_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
                                              "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "GITHUB_TOKEN"),
        "fallback": "Claude only (the platform's Claude client)",
        "needs": "Node 22 and a build in each job; every provider key you want it to use (its code gives no free access by itself)",
    },
    "instagram_skill": {
        "label": "Instagram Agent Skill", "repo": "https://github.com/Jakeschincariol/instagram-agent-skill", "kind": "git_data", "pinned": "", "probe": [], "secrets": (),
        "fallback": "the platform's own technique labels and scoring", "needs": "nothing (data and dependency-free scripts)",
    },
    "youtube_skill": {
        "label": "YouTube Agent Skill", "repo": "https://github.com/Jakeschincariol/youtube-agent-skill", "kind": "git_data", "pinned": "", "probe": [], "secrets": (),
        "fallback": "the platform's own outlier ranking and trend speed", "needs": "nothing (data and dependency-free scripts)",
    },
    "video_shotcraft": {
        "label": "Video Shotcraft", "repo": "https://github.com/Vincentwei1021/video-shotcraft", "kind": "git_data", "pinned": "", "probe": [], "secrets": (),
        "fallback": "none yet (Creative Studio is not built)", "needs": "Remotion (its own licence) and Node to render; nothing to read the cards and templates",
    },
}
IDS = tuple(CONNECTIONS)


def load_declared(path="connections_extra.yaml"):
    """Add connections declared in a file (no code), as services_extra.yaml does for services. Bad entries are skipped and reported. Returns the ids added and a list of problems."""
    added, problems = [], []
    p = Path(path)
    if not p.is_file():
        return added, problems
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:                                                    # noqa: BLE001
        return added, [f"{path}: unreadable ({exc.__class__.__name__})"]
    for item in data.get("connections") or []:
        try:
            cid = str(item["id"])
            if not cid.replace("_", "").isalnum() or not cid[0].isalpha() or cid != cid.lower() or cid in CONNECTIONS:
                raise ValueError(f"{cid!r} is not a usable new connection id")
            kind = item.get("kind", "git_data")
            if kind not in ("pip_cli", "pip_lib", "node_server", "git_data"):
                raise ValueError(f"{cid}: kind must be pip_cli, pip_lib, node_server or git_data")
            CONNECTIONS[cid] = {"label": item.get("label", cid), "repo": str(item["repo"]), "kind": kind, "pip": item.get("pip", f"git+{item['repo']}"), "binary": item.get("binary", ""),
                                "pinned": str(item.get("pinned", "")), "probe": list(item.get("probe") or []), "secrets": tuple(item.get("secrets") or ()),
                                "fallback": item.get("fallback", "none"), "needs": item.get("needs", "")}
            added.append(cid)
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"{path}: {exc}")
    return added, problems


# ---- the owner's controls ---------------------------------------------------------------------------------------------------------------------

def _sec(settings, service):
    return settings["services"].get(service) or {}


def decide(settings, conn_id):
    """The owner's choices for one connection: {on, route, version, secrets}. Anything not set takes the default (on, external_first, latest, secrets handed over)."""
    return {"on": bool(_sec(settings, "connections").get("channels", {}).get(conn_id, True)) and bool(_sec(settings, "connections").get("enabled", True)),
            "route": (_sec(settings, "connection_routes").get("routes") or {}).get(conn_id, "external_first"),
            "version": (_sec(settings, "connection_versions").get("routes") or {}).get(conn_id, "latest"),
            "secrets": bool((_sec(settings, "connection_secrets").get("channels") or {}).get(conn_id, True))}


def external_env(settings, conn_id, environ):
    """The environment for the repo's own process: the basics, plus ONLY the secrets this connection declares it needs and only if the owner lets it have them. Every other secret is withheld."""
    env = {k: environ[k] for k in BASE_ENV if k in environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if decide(settings, conn_id)["secrets"]:
        for name in CONNECTIONS[conn_id]["secrets"]:
            if environ.get(name):
                env[name] = environ[name]
    return env


# ---- health -----------------------------------------------------------------------------------------------------------------------------------

def data_dir():
    return os.environ.get("CONTENTINTEL_DATA_DIR", "data")


# A failure that is not the repository's fault never lowers its health: a missing environment or key, a login the site wants, a site's own refusal or limit, a page that is not there, a bad request.
NOT_THE_REPOSITORYS_FAULT = frozenset({"NEED_SETUP", "NEED_LOGIN", "PLATFORM_BLOCKED", "RATE_LIMITED", "NOT_FOUND", "BAD_INPUT"})
HISTORY_VERSION = 2


class ConnectionHealth:
    """What really happened when each connection was used. Kept between runs in data/_shared/connections_health.json."""

    KEEP = 40

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(data_dir()) / "_shared" / "connections_health.json"
        self.data = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}
        for entry in self.data.values():                                       # history from before version 2 counted setup failures and refusals as the repository failing: its failures are dropped once
            if isinstance(entry, dict) and entry.get("v", 1) < HISTORY_VERSION:
                entry["attempts"] = [a for a in entry.get("attempts") or [] if a.get("ok")]
                entry["last_error"], entry["v"] = None, HISTORY_VERSION

    def record(self, conn_id, ok, now, *, route="external", code="", message="", latency_ms=0):
        if not ok and code in NOT_THE_REPOSITORYS_FAULT:
            return
        entry = self.data.setdefault(conn_id, {"attempts": [], "last_success": "", "last_error": None, "builtin_used": 0, "v": HISTORY_VERSION})
        if route == "builtin":
            entry["builtin_used"] = entry.get("builtin_used", 0) + 1
            return
        entry["attempts"] = (entry["attempts"] + [{"at": now, "ok": bool(ok), "ms": int(latency_ms)}])[-self.KEEP:]
        if ok:
            entry["last_success"] = now
        else:
            entry["last_error"] = {"at": now, "code": code, "message": str(message)[:300]}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def summary(self, conn_id):
        """score (0 to 100, recent attempts count more; None before anything was tried), state (unknown / healthy / degraded / down), and the facts behind it."""
        entry = self.data.get(conn_id) or {}
        attempts = entry.get("attempts") or []
        score = None
        if attempts:
            window = attempts[-20:]
            weights = list(range(1, len(window) + 1))
            score = round(100 * sum(w for w, a in zip(weights, window) if a["ok"]) / sum(weights))
        state = "unknown" if score is None else "healthy" if score >= 80 else "degraded" if score >= 40 else "down"
        good = [a["ms"] for a in attempts if a["ok"] and a.get("ms")]
        return {"score": score, "state": state, "attempts": len(attempts), "last_success": entry.get("last_success", ""), "last_error": entry.get("last_error"),
                "avg_ms": round(sum(good) / len(good)) if good else None, "builtin_used": entry.get("builtin_used", 0)}


def note(settings, conn_id, ok, now, *, code="", message="", latency_ms=0, route="external", ledger=None):
    """Record one outcome (best effort: a health ledger that cannot be written never stops a run)."""
    try:
        led = ledger or ConnectionHealth()
        led.record(conn_id, ok, now, route=route, code=code, message=message, latency_ms=latency_ms)
        if ledger is None:
            led.save()
    except Exception:                                                           # noqa: BLE001
        pass


def rows(settings, ledger=None, now=""):
    """One row per connection for the Connections Health tab and the command: the owner's switches, the health score and what the fallback is."""
    led = ledger or ConnectionHealth()
    out = []
    for cid, spec in CONNECTIONS.items():
        d, h = decide(settings, cid), led.summary(cid)
        err = h["last_error"] or {}
        out.append({"connection": cid, "name": spec["label"], "repo": spec["repo"], "on": "on" if d["on"] else "off", "route": d["route"], "version": d["version"],
                    "secrets_handed_over": "yes" if d["secrets"] else "no", "health_score": "" if h["score"] is None else h["score"], "state": h["state"], "attempts": h["attempts"],
                    "last_success": h["last_success"], "last_error": (f"{err.get('code', '')}: {err.get('message', '')}" if err else ""), "avg_ms": "" if h["avg_ms"] is None else h["avg_ms"],
                    "backup_used": h["builtin_used"], "fallback": spec["fallback"], "needs": spec["needs"]})
    return out


# ---- running a connection, with the platform's own version as the backup ------------------------------------------------------------------------------

def run_connection(settings, conn_id, external, builtin, *, now, ledger=None):
    """(result, which) where which is 'external' or 'builtin'. `external` and `builtin` are zero-argument callables that return the result or raise. The owner's route decides the order; a failure
    of the first route is recorded and the second is tried; if both fail the last failure is raised. A connection switched OFF never calls the repo."""
    d = decide(settings, conn_id)
    order = {"external_first": ("external", "builtin"), "builtin_first": ("builtin", "external"), "external_only": ("external",), "builtin_only": ("builtin",)}[d["route"]]
    if not d["on"]:
        order = tuple(r for r in order if r == "builtin")
        if not order:
            from . import contract as K
            raise K.Failure(K.NEED_SETUP, f"the {conn_id} connection is switched off (services.connections.channels.{conn_id}) and its route is external_only")
    last = None
    for which in order:
        fn = external if which == "external" else builtin
        if fn is None:
            continue
        started = __import__("time").monotonic()
        try:
            result = fn()
        except Exception as exc:                                                # noqa: BLE001 - every failure is recorded and the next route is tried
            last = exc
            if which == "external":
                note(settings, conn_id, False, now, code=getattr(exc, "code", "UPSTREAM_BROKEN"), message=str(exc), ledger=ledger)
            continue
        if which == "external":
            note(settings, conn_id, True, now, latency_ms=int((__import__("time").monotonic() - started) * 1000), ledger=ledger)
        else:
            note(settings, conn_id, True, now, route="builtin", ledger=ledger)
        return result, which
    if last is not None:
        raise last
    from . import contract as K
    raise K.Failure(K.NEED_SETUP, f"the {conn_id} connection has no route that can run here")


# ---- installing and updating ---------------------------------------------------------------------------------------------------------------------

def external_dir():
    return Path(os.environ.get("CONTENTINTEL_EXTERNAL_DIR", "external"))


def binary_path(conn_id, root=None):
    spec = CONNECTIONS[conn_id]
    base = Path(root) if root else external_dir()
    candidate = base / f"{conn_id}-env" / "bin" / (spec.get("binary") or "python")
    return str(candidate) if candidate.exists() else (shutil.which(spec.get("binary") or "") or "")


def install(settings, conn_id, *, root=None, runner=subprocess.run, which=shutil.which, requirements_root="."):
    """Install or update one connection according to the owner's version mode: latest (the newest) or pinned. Returns (ok, message). Nothing is run unless the connection is ON."""
    spec, d = CONNECTIONS[conn_id], decide(settings, conn_id)
    if not d["on"]:
        return False, f"{conn_id}: switched off, nothing installed"
    base = Path(root) if root else external_dir()
    base.mkdir(parents=True, exist_ok=True)

    def sh(argv, cwd=None):
        done = runner(argv, cwd=cwd, capture_output=True, text=True, timeout=900)
        if done.returncode != 0:
            raise RuntimeError(" ".join((done.stderr or done.stdout or "").split())[-240:] or f"{argv[0]} failed")
        return done.stdout
    try:
        if spec["kind"] in ("pip_cli", "pip_lib"):
            env_dir = base / f"{conn_id}-env"
            if not (env_dir / "bin" / "python").exists():
                sh(["python3", "-m", "venv", str(env_dir)])
            pip = str(env_dir / "bin" / "pip")
            if d["version"] == "latest":
                sh([pip, "install", "-q", "--upgrade", spec["pip"], *spec.get("pip_extra", ())])
            elif spec.get("pinned_requirements") and (Path(requirements_root) / spec["pinned_requirements"]).is_file():
                sh([pip, "install", "-q", "-r", str(Path(requirements_root) / spec["pinned_requirements"])])
            else:
                sh([pip, "install", "-q", spec["pip"] + (f"@{spec['pinned']}" if spec.get("pinned") and spec["pip"].startswith("git+") else (f"=={spec['pinned']}" if spec.get("pinned") else ""))])
            return True, f"{conn_id}: installed ({d['version']}) in {env_dir}"
        dest = base / conn_id if spec["kind"] == "git_data" else Path(os.environ.get("CONTENTINTEL_GATEWAY_DIR", "gateway-src"))
        if (dest / ".git").exists():
            sh(["git", "-C", str(dest), "fetch", "--quiet", "--depth", "1", "origin"] + (["HEAD"] if d["version"] == "latest" else []))
            if d["version"] == "latest":
                sh(["git", "-C", str(dest), "checkout", "--quiet", "FETCH_HEAD"])
        else:
            dest.mkdir(parents=True, exist_ok=True)
            sh(["git", "init", "--quiet"], cwd=str(dest))
            sh(["git", "remote", "add", "origin", spec["repo"]], cwd=str(dest))
            ref = "HEAD"
            if spec["kind"] == "node_server" and d["version"] == "pinned" and Path(spec.get("lock", "")).is_file():
                ref = json.loads(Path(spec["lock"]).read_text(encoding="utf-8"))["gateway"]["commit"]
            elif spec["kind"] == "node_server":
                tags = [line.split("refs/tags/")[-1] for line in sh(["git", "ls-remote", "--tags", "--sort=-v:refname", "origin", "v*"], cwd=str(dest)).splitlines() if "^{}" not in line]
                ref = tags[0] if tags else "HEAD"
            sh(["git", "fetch", "--quiet", "--depth", "1", "origin", ref], cwd=str(dest))
            sh(["git", "checkout", "--quiet", "FETCH_HEAD"], cwd=str(dest))
        if spec["kind"] == "node_server":
            sh(["npm", "ci", "--workspace=shared", "--workspace=server", "--include-workspace-root", "--no-audit", "--no-fund"], cwd=str(dest))
            sh(["npm", "run", "build", "-w", "server"], cwd=str(dest))
        return True, f"{conn_id}: {'updated' if (dest / '.git').exists() else 'cloned'} ({d['version']}) in {dest}"
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{conn_id}: could not be installed or updated ({exc.__class__.__name__}: {str(exc)[:200]}); the platform's own version is used instead"


def probe(settings, conn_id, *, now, runner=subprocess.run, environ=None, root=None, ledger=None):
    """A cheap real check that the connection works here, recorded in its health. Returns (ok, message)."""
    spec = CONNECTIONS[conn_id]
    environ = os.environ if environ is None else environ
    try:
        if spec["kind"] == "pip_cli":
            exe = binary_path(conn_id, root)
            if not exe:
                raise RuntimeError("it is not installed here (run the install action)")
            done = runner([exe, *spec["probe"]], env=external_env(settings, conn_id, environ), capture_output=True, text=True, timeout=120)
            if done.returncode != 0:
                raise RuntimeError(" ".join((done.stderr or done.stdout or "").split())[-200:] or "the command failed")
            return _ok(settings, conn_id, now, ledger, "answers its command line")
        if spec["kind"] == "pip_lib":
            exe = binary_path(conn_id, root)
            if not exe:
                raise RuntimeError("its environment is not installed here")
            done = runner([exe, "-c", "import importlib.metadata as m; print(m.version('scrapling'))"], capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                raise RuntimeError("the library cannot be imported")
            return _ok(settings, conn_id, now, ledger, f"version {done.stdout.strip()}")
        done = runner(["git", "ls-remote", spec["repo"], "HEAD"], capture_output=True, text=True, timeout=60)
        if done.returncode != 0 or not done.stdout.strip():
            raise RuntimeError("the repository could not be reached")
        return _ok(settings, conn_id, now, ledger, "reachable, latest commit " + done.stdout.split()[0][:10])
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        note(settings, conn_id, False, now, code="NEED_SETUP", message=str(exc), ledger=ledger)
        return False, f"{conn_id}: {exc}"


CONNECTIONS_TAB = "Connections Health"
TAB_COLUMNS = ["connection", "name", "on", "health_score", "state", "route", "version", "secrets_handed_over", "attempts", "last_success", "last_error", "avg_ms", "backup_used", "fallback", "needs", "repo"]


def sheet_tab(settings, now=""):
    """The Connections Health tab: each connection, whether it is on, its health score and state, the route and version the owner chose, and what the backup is."""
    data = rows(settings)
    return {"title": CONNECTIONS_TAB, "columns": TAB_COLUMNS, "rows_by_key": {r["connection"]: {k: r[k] for k in TAB_COLUMNS} for r in data}, "key_col": "connection", "starter_manual": []}


def _ok(settings, conn_id, now, ledger, message):
    note(settings, conn_id, True, now, ledger=ledger)
    return True, f"{conn_id}: {message}"
