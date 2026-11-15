"""A library of 700+ prompts people used to have an AI model write animated videos as code (motion graphics, explainers, 3D scenes, interactive pieces), as inspiration for the visual side of a brief.

Source: awesome-opus5-5-videos (https://github.com/yihui-dev/awesome-opus5-5-videos, MIT licence, copyright 2026 yihui-dev), data/videos.json; each entry keeps its author and the address of the original
post, and the prompts remain the creators'. Read from the connection `opus_video_prompts` (its checkout) or from the snapshot in reference/motion-prompts.json. Used to suggest motion formats for a concept
(Briefs' visual direction, Idea Bank hooks), never to copy a creator's work: an entry is shown with its author and link.
"""
import json
import re
from pathlib import Path

DATA_FILE = Path("data") / "videos.json"
SNAPSHOT = Path("reference") / "motion-prompts.json"
PROMPT_KEPT = 500


def parse(text):
    data = json.loads(text)
    out = []
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and r.get("prompt"):
            out.append({"slug": str(r.get("slug") or ""), "author": str(r.get("author") or ""), "post_url": str(r.get("post_url") or ""), "category": str(r.get("category") or ""),
                        "tags": [str(t) for t in (r.get("tech_tags") or [])][:6], "prompt": " ".join(str(r["prompt"]).split())[:PROMPT_KEPT]})
    return out


def read(checkout=None, snapshot=None):
    if checkout and (Path(checkout) / DATA_FILE).is_file():
        try:
            entries = parse((Path(checkout) / DATA_FILE).read_text(encoding="utf-8"))
        except ValueError:
            entries = []
        if entries:
            return entries, "the prompt library checkout"
    snap = Path(snapshot) if snapshot else SNAPSHOT
    if snap.is_file():
        data = json.loads(snap.read_text(encoding="utf-8"))
        return data.get("entries", []), f"the snapshot taken {data.get('taken', '?')}"
    return [], "nothing (no checkout and no snapshot)"


def make_snapshot(entries, taken, commit=""):
    return {"source": "https://github.com/yihui-dev/awesome-opus5-5-videos (data/videos.json, MIT; prompts belong to their creators)", "commit": commit, "taken": taken, "entries": entries}


def _words(text):
    return set(re.findall(r"[a-z0-9]{4,}", str(text).lower()))


def categories(entries):
    out = {}
    for e in entries:
        out[e["category"]] = out.get(e["category"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def search(entries, text, limit=3, category=""):
    """The prompts closest to `text` (an idea, a hook, a product): by shared words and, when given, a category. [] when nothing shares two words (nothing is forced)."""
    want = _words(text)
    scored = []
    for e in entries:
        if category and e["category"] != category:
            continue
        common = want & _words(f"{e['prompt']} {' '.join(e['tags'])} {e['category']}")
        if len(common) >= 2:
            scored.append((len(common), e))
    return [e for _, e in sorted(scored, key=lambda t: (-t[0], t[1]["slug"]))[:limit]]


def line(entry):
    return f"{entry['category'] or 'motion'} ({', '.join(entry['tags']) or 'code'}) by {entry['author']}: {entry['prompt'][:200]} [{entry['post_url']}]"
