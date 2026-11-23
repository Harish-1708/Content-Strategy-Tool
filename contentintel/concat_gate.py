"""Batch 3: the Concat go/no-go gate, run on GitHub Actions (no host, no GUI, no GPU). It makes a small test project with ffmpeg, renders it with Concat's own command three times and applies the owner's gate to each result:
1080x1920, under 300 seconds, with audio, in at least 2 of the 3 runs. `discover` prints what the installed Concat says about its own command line, so the render command can be set from facts, not guessed."""
import shlex
import subprocess
import time
from pathlib import Path

import yaml

from . import openmontage as OM


def load_config(path="concat.yaml"):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    gate = cfg.get("gate") or {}
    for key in ("width", "height", "max_seconds", "runs", "passes_needed"):
        if not isinstance(gate.get(key), int) or gate[key] < 1:
            raise ValueError(f"{path}: gate.{key} must be a whole number of at least 1")
    if gate["passes_needed"] > gate["runs"]:
        raise ValueError(f"{path}: gate.passes_needed cannot be more than gate.runs")
    return cfg


def discover(cfg, runner=subprocess.run):
    """[(command, exit code, first 60 lines of what it printed)] for each discover command: the installed Concat's own account of its command line."""
    out = []
    for cmd in cfg.get("discover_commands") or []:
        try:
            done = runner(cmd.split(), capture_output=True, text=True, timeout=60)
            text = ((getattr(done, "stdout", "") or "") + (getattr(done, "stderr", "") or "")).strip().splitlines()[:60]
            out.append((cmd, getattr(done, "returncode", 1), text))
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.append((cmd, -1, [f"{type(exc).__name__}: {str(exc)[:100]}"]))
    return out


def make_test_clips(workdir, runner=subprocess.run):
    """Three short clips with sound (colour bars and a tone), made with ffmpeg, to cut together. Returns their paths; raises RuntimeError when ffmpeg cannot."""
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, colour in enumerate(("red", "green", "blue"), 1):
        path = work / f"clip{i}.mp4"
        done = runner(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=1080x1920:d=2:r=30", "-f", "lavfi", "-i", f"sine=frequency={300 + 150 * i}:duration=2",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], capture_output=True, text=True, timeout=120)
        if getattr(done, "returncode", 1) != 0 or not path.is_file():
            raise RuntimeError("ffmpeg could not make the test clips: " + (getattr(done, "stderr", "") or "")[:120])
        paths.append(path)
    return paths


def run_gate(cfg, workdir, *, runner=subprocess.run, probe=OM.probe_video, clock=time.monotonic):
    """{verdict, runs: [...], reason}. verdict: 'NOT RUN' (no render command yet), 'GO' (passes_needed of runs passed) or 'NO-GO'."""
    command = (cfg.get("render_command") or "").strip()
    if not command:
        return {"verdict": "NOT RUN", "runs": [], "reason": "render_command is empty in concat.yaml: set it from the discover step's output of the first run"}
    gate = cfg["gate"]
    try:
        clips = make_test_clips(workdir, runner)
    except RuntimeError as exc:
        return {"verdict": "NOT RUN", "runs": [], "reason": str(exc)}
    checks = {"width": gate["width"], "height": gate["height"], "has_audio": gate.get("has_audio", True), "min_duration_s": 1}
    runs = []
    for n in range(1, gate["runs"] + 1):
        out = Path(workdir) / f"render{n}.mp4"
        out.unlink(missing_ok=True)
        argv = []
        for part in shlex.split(command):
            if part == "{inputs}":
                argv += [str(c) for c in clips]
            else:
                argv.append(part.replace("{output}", str(out)))
        t0 = clock()
        try:
            done = runner(argv, capture_output=True, text=True, timeout=gate["max_seconds"] + 60)
            seconds = round(clock() - t0, 1)
            if getattr(done, "returncode", 1) != 0 or not out.is_file():
                runs.append({"run": n, "passed": False, "seconds": seconds, "failures": [f"the render exited {getattr(done, 'returncode', '?')}: " + ((getattr(done, 'stderr', '') or getattr(done, 'stdout', '') or '')[:120])]})
                continue
            measured = probe(out)
            ok, failures = OM.quality_gate(measured, checks)
            if seconds > gate["max_seconds"]:
                ok, failures = False, failures + [f"it took {seconds} s, more than {gate['max_seconds']} s"]
            runs.append({"run": n, "passed": ok, "seconds": seconds, "measured": measured, "failures": failures})
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            runs.append({"run": n, "passed": False, "seconds": round(clock() - t0, 1), "failures": [f"{type(exc).__name__}: {str(exc)[:100]}"]})
    wins = sum(1 for r in runs if r["passed"])
    return {"verdict": "GO" if wins >= gate["passes_needed"] else "NO-GO", "runs": runs, "reason": f"{wins} of {len(runs)} run(s) passed (needs {gate['passes_needed']})"}


def format_report(result, discovered=None):
    lines = []
    for cmd, code, text in discovered or []:
        lines.append(f"concat: $ {cmd}  (exit {code})")
        lines += [f"concat:   {t}" for t in text[:25]]
    lines.append(f"concat: gate {result['verdict']}: {result['reason']}")
    for r in result["runs"]:
        lines.append(f"concat:   run {r['run']}: {'PASS' if r['passed'] else 'FAIL'} in {r['seconds']} s" + (f" {r['measured']}" if r.get("measured") else "") + ("; " + "; ".join(r["failures"]) if r["failures"] else ""))
    return lines


def _subcommands(lines):
    """The words that look like subcommands in a help text: the first word of an indented line, or of a line after a 'Commands:' heading."""
    found, in_block = [], False
    for ln in lines:
        text = ln.rstrip()
        if text.strip().lower().rstrip(":") in ("commands", "subcommands", "available commands"):
            in_block = True
            continue
        word = text.strip().split(" ")[0] if text.strip() else ""
        if (in_block or text.startswith("  ")) and word.isalpha() and word.islower() and len(word) > 2:
            found.append(word)
        if in_block and not text.strip():
            in_block = False
    return found


def candidates(discovered, limit=8):
    """Render commands inferred from Concat's own help (the discover step's output): for each subcommand that looks like an export or render (render, export, encode, build, run, project), the output flag its own help names
    (--output, -o, --out) and the input style (the clips as plain arguments). Returns command templates with {inputs} and {output}, most likely first. A guess is only ever TRIED: the gate decides."""
    by_cmd = {cmd: text for cmd, _code, text in discovered}
    root = " ".join(by_cmd.get("concat --help", []))
    subs = [s for s in _subcommands(by_cmd.get("concat --help", [])) if s in ("render", "export", "encode", "build", "run", "project", "cut", "edit", "compose")]
    if not subs and "render" in root.lower():
        subs = ["render"]
    out = []
    for sub in subs or ["render", "export"]:
        sub_help = " ".join(by_cmd.get(f"concat {sub} --help", []))
        flags = [f for f in ("--output", "--out", "-o", "--output-file", "--dest") if f in sub_help.split() or f in sub_help] or ["--output", "-o", "--out"]
        for flag in flags:
            out.append(f"concat {sub} {{inputs}} {flag} {{output}}")
    return list(dict.fromkeys(out))[:limit]


def run_auto(cfg, workdir, *, runner=subprocess.run, probe=OM.probe_video, clock=time.monotonic):
    """The whole gate in ONE run, with no one filling in a command: discover, infer candidate render commands from Concat's own help, try each once on the test clips, and run the full gate with the first one that
    produces a video. If the config already names a render_command it is used as it is (the manual fill-in stays the fallback). Returns (result, discovered, tried)."""
    discovered = discover(cfg, runner)
    if (cfg.get("render_command") or "").strip():
        return run_gate(cfg, workdir, runner=runner, probe=probe, clock=clock), discovered, [cfg["render_command"]]
    tried = []
    try:
        clips = make_test_clips(workdir, runner)
    except RuntimeError as exc:
        return {"verdict": "NOT RUN", "runs": [], "reason": str(exc)}, discovered, tried
    for cmd in candidates(discovered):
        tried.append(cmd)
        out = Path(workdir) / "trial.mp4"
        out.unlink(missing_ok=True)
        argv = []
        for part in shlex.split(cmd):
            if part == "{inputs}":
                argv += [str(c) for c in clips]
            else:
                argv.append(part.replace("{output}", str(out)))
        try:
            done = runner(argv, capture_output=True, text=True, timeout=cfg["gate"]["max_seconds"] + 60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if getattr(done, "returncode", 1) == 0 and out.is_file():
            result = run_gate({**cfg, "render_command": cmd}, workdir, runner=runner, probe=probe, clock=clock)
            result["reason"] += f"; the render command was inferred from Concat's help: {cmd}"
            return result, discovered, tried
    return {"verdict": "NO-GO", "runs": [], "reason": f"none of the {len(tried)} command(s) inferred from Concat's help produced a video (see the discover output above for its real command line); put the right one in concat.yaml's render_command"}, discovered, tried
