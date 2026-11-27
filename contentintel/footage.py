"""The footage inventory, built by the tool from the brand's footage folder: nothing is listed by hand.

WHERE THE FOOTAGE GOES: the Google Drive folder the Idea Bank already uses (the one named in the IDEA_BANK_FOLDER_ID secret, shared with the tool's service account) holds one folder per brand. Put the brand's clips in
    <Idea Bank folder> / <Brand name> / Footage /        (subfolders are fine and become part of each clip's tags: Footage/unboxing/..., Footage/creators/Sarah/...)
The tool makes the Footage folder itself the first time it looks. Footage > scan (every Monday, and on demand) lists everything under it with Drive's own video information (length, size shape): no clip is downloaded, so the folder can
be as large as Drive allows. A clip whose length Drive has not read yet (a fresh upload) is listed without one and filled in by the next scan.
The inventory is saved in data/<brand>/extras/footage.json and is what a cuts brief may use: each clip has a stable number (FT-xxxxxx), its path and name, its length, its shape (vertical, square or horizontal) and tags taken from its
folder names and file name."""
import hashlib
import re

VIDEO = ("video/",)
IMAGE = ("image/",)
STOP = {"the", "and", "for", "with", "mov", "mp4", "img", "dsc", "final", "copy", "new", "clip", "video", "footage", "raw", "edit", "export"}


def clip_id(file_id):
    return "FT-" + hashlib.sha1(str(file_id).encode("utf-8")).hexdigest()[:6]


def shape(width, height):
    if not width or not height:
        return ""
    ratio = width / height
    return "vertical" if ratio < 0.8 else "square" if ratio <= 1.25 else "horizontal"


def tags_of(path, name):
    words = re.findall(r"[a-z0-9]+", f"{path} {name.rsplit('.', 1)[0]}".lower().replace("_", " ").replace("-", " "))
    return list(dict.fromkeys(w for w in words if len(w) >= 3 and w not in STOP and not w.isdigit()))[:12]


def build_inventory(files):
    """The clips among the files (videos and pictures), as stable, sorted records. Anything else in the folder is counted and ignored."""
    clips, ignored = [], 0
    for f in files:
        mime = f.get("mime", "")
        kind = "video" if mime.startswith(VIDEO) else "image" if mime.startswith(IMAGE) else ""
        if not kind:
            ignored += 1
            continue
        clips.append({"id": clip_id(f["id"]), "file_id": f["id"], "name": f["name"], "path": f.get("path", ""), "kind": kind, "seconds": f.get("seconds"), "shape": shape(f.get("width"), f.get("height")),
                      "width": f.get("width"), "height": f.get("height"), "size_mb": round(int(f.get("size") or 0) / 1_000_000, 1), "modified": str(f.get("modified", ""))[:10], "tags": tags_of(f.get("path", ""), f["name"]),
                      "url": f"https://drive.google.com/file/d/{f['id']}/view"})
    clips.sort(key=lambda c: (c["path"], c["name"]))
    return clips, ignored


def load(store, slug):
    return store.read_extra(slug, "footage") or {"clips": []}


def save(store, slug, clips, ignored, folder_id, now):
    videos = [c for c in clips if c["kind"] == "video"]
    store.write_extra(slug, "footage", {"note": "built by the tool from the brand's Footage folder in Google Drive (nothing is listed by hand)", "updated": now, "folder_id": folder_id, "folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
                                        "count": len(clips), "videos": len(videos), "total_seconds": round(sum(c["seconds"] or 0 for c in videos), 1), "without_length": sum(1 for c in videos if not c["seconds"]), "ignored_files": ignored, "clips": clips})


def prompt_block(clips, limit=80):
    """The inventory as the writer sees it: video clips only, newest first, one line each."""
    videos = [c for c in clips if c["kind"] == "video" and c.get("seconds")]
    videos.sort(key=lambda c: c["modified"], reverse=True)
    lines = [f"- {c['id']}: {c['path'] + '/' if c['path'] else ''}{c['name']} ({c['seconds']:g} s, {c['shape'] or 'shape unknown'}; {', '.join(c['tags'][:6])})" for c in videos[:limit]]
    return ("Footage you may cut from. Use ONLY these clips, by their number; for each clip give the part you use as start and end in seconds, inside the clip's own length. Never invent a clip, and never use a part longer than the clip:\n" + "\n".join(lines)) if lines else ""


def check_clips(brief, clips, target_seconds_of):
    """Findings for a cuts brief: every concept must cut from at least one real clip, each part inside its clip, and the parts together close to the length its format names."""
    by_id = {c["id"]: c for c in clips}
    findings = []
    for n, c in enumerate(brief.get("concepts", []), start=1):
        label = f"concept {n} ({c.get('name', '')})"
        parts = c.get("clips") or []
        if not parts:
            findings.append({"where": f"{label} clips", "rule": "cuts_no_clips", "severity": "error", "message": "a cuts concept must say which clips it is cut from (their FT- numbers from the footage inventory, with start and end seconds)"})
            continue
        total = 0.0
        for part in parts:
            clip = by_id.get(part.get("clip"))
            if clip is None:
                findings.append({"where": f"{label} clips", "rule": "cuts_unknown_clip", "severity": "error", "message": f"{part.get('clip') or 'a clip with no number'} is not in the footage inventory: cut only from clips that exist"})
                continue
            start, end = float(part.get("start") or 0), float(part.get("end") or 0)
            if start < 0 or end <= start:
                findings.append({"where": f"{label} clips", "rule": "cuts_bad_times", "severity": "error", "message": f"{clip['id']}: the part {start:g}-{end:g} s is not a start before an end"})
            elif clip.get("seconds") and end > clip["seconds"] + 0.05:
                findings.append({"where": f"{label} clips", "rule": "cuts_past_the_end", "severity": "error", "message": f"{clip['id']} is {clip['seconds']:g} s long, but the part runs to {end:g} s"})
            else:
                total += end - start
        target = target_seconds_of(c.get("format"))
        if total and target and abs(total - target) > max(3.0, target * 0.25):
            findings.append({"where": f"{label} clips", "rule": "cuts_length", "severity": "warning", "message": f"the parts add up to {total:g} s but the format names {target} s"})
    return findings
