"""Stage 10: the browser bake-off. Public pages only: the engines read the SAME list of public addresses and are compared on three things that matter to this tool: did the page come back, how fast, and how much of
what the current engine saw did the candidate also see. Nothing here logs in, solves a puzzle or hides what the browser is (no fingerprint spoofing, no stealth, no captcha solving: a page that shows a check is
reported as a check). The logged-in trial of agent-browser is a separate, later step with the cookie-session mechanism and its caps (cap, gap, rest, read log); this file never touches a session.

Engines are configured in bakeoff.yaml: each is a command template that prints the page's HTML to stdout (`{url}` is replaced). An engine with no command, or whose program is not installed on the machine, is reported
as NOT RUN with the reason, never skipped silently and never faked. The go/no-go gate (set by the owner): a candidate is GO only if, on the same pages, it answers at least as often as the current engine,
sees at least 90 percent of the words the current engine saw, and its median time is not more than twice the current engine's."""
import re
import shutil
import statistics
import subprocess
import time

import yaml

CONFIG_FILE = "bakeoff.yaml"
PARITY_AT, SPEED_FACTOR = 0.9, 2.0
CHECK_WORDS = ("verify you are human", "captcha", "are you a robot", "access denied", "unusual traffic", "just a moment")


def load_config(path=CONFIG_FILE):
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    engines = data.get("engines") or {}
    if not isinstance(engines, dict) or not engines or "current" not in engines:
        raise ValueError(f"{path}: needs an engines map with the current engine named 'current'")
    for name, e in engines.items():
        if not isinstance(e, dict) or set(e) - {"label", "command", "timeout", "note"}:
            raise ValueError(f"{path}: engine {name!r} may only have label, command, timeout and note")
    return {"engines": engines, "urls": [str(u) for u in (data.get("urls") or [])], "runs": int(data.get("runs", 3))}


def words(html):
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def run_engine(engine, url, *, runner=subprocess.run, clock=time.monotonic):
    """(html or None, seconds, why): one read of one public address by one engine's command."""
    cmd = str(engine.get("command") or "").strip()
    if not cmd:
        return None, 0.0, "no command is configured for this engine"
    argv = [part.replace("{url}", url) for part in cmd.split()]
    if not shutil.which(argv[0]) and runner is subprocess.run:
        return None, 0.0, f"{argv[0]} is not installed on this machine"
    t0 = clock()
    try:
        done = runner(argv, capture_output=True, text=True, timeout=int(engine.get("timeout", 60)))
    except Exception as exc:                                                                # noqa: BLE001
        return None, round(clock() - t0, 2), f"{type(exc).__name__}: {str(exc)[:120]}"
    out = getattr(done, "stdout", "") or ""
    if getattr(done, "returncode", 1) != 0 or len(out) < 200:
        return None, round(clock() - t0, 2), f"exit {getattr(done, 'returncode', '?')}, {len(out)} characters: " + (getattr(done, "stderr", "") or "")[:100]
    return out, round(clock() - t0, 2), ""


def run_bakeoff(config, *, runner=subprocess.run, clock=time.monotonic, urls=None):
    """{engine: {ran, why, answered, tried, median_seconds, parity, checks, verdict}} plus the gate verdict per candidate against 'current'."""
    urls = urls or config["urls"]
    reads = {}
    report = {}
    for name, engine in config["engines"].items():
        rows = {}
        why_not = ""
        for url in urls:
            for _ in range(max(1, config["runs"])):
                html, seconds, why = run_engine(engine, url, runner=runner, clock=clock)
                if html is None and ("no command" in why or "not installed" in why):
                    why_not = why
                    break
                rows.setdefault(url, []).append((html, seconds, why))
            if why_not:
                break
        reads[name] = rows
        if why_not:
            report[name] = {"ran": False, "why": why_not, "verdict": "NOT RUN"}
            continue
        tried = sum(len(v) for v in rows.values())
        ok = [(u, h, s) for u, v in rows.items() for h, s, _w in v if h is not None]
        report[name] = {"ran": True, "tried": tried, "answered": len(ok), "median_seconds": round(statistics.median([s for _u, _h, s in ok]), 2) if ok else 0.0,
                        "checks": sum(1 for _u, h, _s in ok if any(w in (h or "").lower() for w in CHECK_WORDS)), "why": next((w for v in rows.values() for _h, _s, w in v if w), ""), "verdict": ""}
    base = report.get("current", {})
    base_reads = {u: next((h for h, _s, _w in v if h), "") for u, v in reads.get("current", {}).items()}
    for name, r in report.items():
        if not r["ran"] or name == "current":
            if name == "current":
                r["verdict"] = "BASELINE" if r["ran"] else "NOT RUN"
            continue
        parities = []
        for u, v in reads[name].items():
            theirs = next((h for h, _s, _w in v if h), "")
            ref = words(base_reads.get(u, ""))
            if ref:
                parities.append(len(ref & words(theirs)) / len(ref))
        r["parity"] = round(statistics.mean(parities), 3) if parities else 0.0
        answers_ok = (r["answered"] / r["tried"]) >= (base.get("answered", 0) / base["tried"] if base.get("tried") else 1)
        fast_ok = not base.get("median_seconds") or r["median_seconds"] <= SPEED_FACTOR * base["median_seconds"]
        r["verdict"] = "GO" if (answers_ok and r["parity"] >= PARITY_AT and fast_ok and r["checks"] <= base.get("checks", 0)) else "NO-GO"
    return report


def format_report(report, urls):
    lines = [f"Browser bake-off on {len(urls)} public address(es); no login, no stealth, no puzzle solving. Gate: answers at least as often as the current engine, sees at least {int(PARITY_AT * 100)}% of its words, median time within {SPEED_FACTOR:g}x."]
    for name, r in report.items():
        if not r["ran"]:
            lines.append(f"  {name}: NOT RUN: {r['why']}")
        else:
            lines.append(f"  {name}: {r['verdict']}: answered {r['answered']} of {r['tried']}, median {r['median_seconds']} s, security checks {r['checks']}" + (f", sees {int(r.get('parity', 0) * 100)}% of the current engine's words" if "parity" in r else "") + (f"; last problem: {r['why']}" if r["why"] else ""))
    return "\n".join(lines)
