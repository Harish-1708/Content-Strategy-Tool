"""How a video moves, measured with ffmpeg: shot length, cuts in the first seconds, loudness and when the first sound starts.

A saved competitor video becomes numbers instead of adjectives: "average shot 1.4 s, 4 cuts in the first 3 seconds, sound from 0.1 s, loudness -14 LUFS". The numbers go into the Idea Bank (technique
and visual-hook), into scoring, and into briefs ("match the winners: a visual change every 1.5 s").

The idea is the one in ReelMimic's analysis scripts (https://github.com/edenfunf/reelmimic, MIT licence, copyright 2026 ReelMimic contributors: analyze.py and fingerprint.py take a reference
video apart into shots, pace and audio). This is a slim re-write that needs nothing but ffmpeg and ffprobe (the workflows install it: the runner does not have it by default), so it adds no Python packages. It reads a file, never a web address.
"""
import json
import re
import subprocess

SCENE = 0.30                       # ffmpeg's own scene-change score above which a frame counts as a cut
_PTS = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_LUFS = re.compile(r"\bI:\s+(-?[0-9.]+|-inf)\s+LUFS")
_LRA = re.compile(r"\bLRA:\s+(-?[0-9.]+)\s+LU")
_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")
_SIL_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")


class PaceError(Exception):
    pass


def _run(runner, argv, timeout):
    try:
        return runner(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PaceError("ffmpeg is not installed here") from None
    except subprocess.TimeoutExpired:
        raise PaceError(f"ffmpeg did not finish within {timeout} seconds") from None


def probe(path, runner=subprocess.run, timeout=60):
    done = _run(runner, ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate", "-of", "json", str(path)], timeout)
    if done.returncode != 0:
        raise PaceError("the file could not be read as a video")
    try:
        info = json.loads(done.stdout or "{}")
    except ValueError:
        raise PaceError("ffprobe gave no usable answer") from None
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise PaceError("the file has no video stream")
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    num, _, den = str(video.get("r_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {"duration": duration, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0), "fps": round(fps, 2),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def cuts(path, runner=subprocess.run, threshold=SCENE, timeout=240):
    """The times (seconds) at which the picture changes shot."""
    done = _run(runner, ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], timeout)
    out = sorted({round(float(t), 2) for line in (done.stderr or "").splitlines() if "showinfo" in line for t in _PTS.findall(line)})
    return [t for t in out if t > 0.05]


def sound(path, runner=subprocess.run, timeout=240):
    """(integrated loudness in LUFS or None, loudness range or None, seconds until the first sound or None)."""
    done = _run(runner, ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "silencedetect=noise=-35dB:d=0.25,ebur128=peak=true", "-f", "null", "-"], timeout)
    text = done.stderr or ""
    lufs = [float(x) for x in _LUFS.findall(text) if x != "-inf"]
    lra = [float(x) for x in _LRA.findall(text)]
    first = None
    starts, ends = _SIL_START.findall(text), _SIL_END.findall(text)
    if starts and float(starts[0]) <= 0.05 and ends:
        first = round(float(ends[0]), 2)                       # the video opens in silence: sound starts when the first silence ends
    elif not starts or float(starts[0]) > 0.05:
        first = 0.0                                  # sound from the very start
    return (lufs[-1] if lufs else None), (lra[-1] if lra else None), first


def analyse(path, runner=subprocess.run):
    """The pace profile of a video file: a dict of plain numbers, or PaceError."""
    info = probe(path, runner)
    times = cuts(path, runner)
    duration = info["duration"]
    edges = [0.0] + [t for t in times if duration <= 0 or t < duration] + ([duration] if duration > 0 else [])
    lengths = [round(b - a, 2) for a, b in zip(edges, edges[1:]) if b - a > 0.05]
    shots = max(1, len(lengths))
    avg = round(sum(lengths) / shots, 2) if lengths else round(duration, 2)
    ordered = sorted(lengths)
    median = ordered[len(ordered) // 2] if ordered else avg
    lufs = lra = first = None
    if info["has_audio"]:
        lufs, lra, first = sound(path, runner)
    first10 = [t for t in times if t <= 10]
    return {"duration_s": round(duration, 2), "width": info["width"], "height": info["height"], "fps": info["fps"], "vertical": info["height"] > info["width"] > 0,
            "shots": shots, "cuts": len(times), "avg_shot_s": avg, "median_shot_s": median, "longest_shot_s": max(lengths) if lengths else avg,
            "first_cut_s": times[0] if times else None, "cuts_first_3s": sum(1 for t in times if t <= 3), "cuts_first_10s": len(first10),
            "cuts_per_10s": round(len(times) * 10 / duration, 1) if duration > 0 else 0, "has_audio": info["has_audio"],
            "loudness_lufs": lufs, "loudness_range": lra, "first_sound_s": first, "cut_times": times[:80], "pace": label(avg)}


def label(avg_shot):
    return "very fast" if avg_shot <= 1.0 else "fast" if avg_shot <= 2.0 else "medium" if avg_shot <= 4.0 else "slow"


def describe(profile):
    """One short line for a sheet cell or a prompt."""
    if not profile:
        return ""
    bits = [f"{profile['pace']} pace: average shot {profile['avg_shot_s']:g} s over {profile['shots']} shot(s)"]
    if profile.get("cuts_first_3s"):
        bits.append(f"{profile['cuts_first_3s']} cut(s) in the first 3 s")
    elif profile.get("shots", 1) > 1:
        bits.append("no cut in the first 3 s")
    if profile.get("first_sound_s") is not None and profile.get("has_audio"):
        bits.append("sound from the first frame" if profile["first_sound_s"] <= 0.3 else f"first sound at {profile['first_sound_s']:g} s")
    elif not profile.get("has_audio"):
        bits.append("no audio track")
    if profile.get("loudness_lufs") is not None:
        bits.append(f"{profile['loudness_lufs']:g} LUFS")
    return "; ".join(bits)


def rule_from(profiles):
    """The common pace of a set of winning videos, as a sentence a brief can state. Empty when there are fewer than two profiles."""
    good = [p for p in profiles if p and p.get("avg_shot_s")]
    if len(good) < 2:
        return ""
    avgs = sorted(p["avg_shot_s"] for p in good)
    mid = avgs[len(avgs) // 2]
    firsts = [p["cuts_first_3s"] for p in good]
    return (f"Across {len(good)} measured winning videos the typical shot lasts {mid:g} s ({label(mid)} pace) and {sum(1 for f in firsts if f)} of them change shot within the first 3 seconds.")
