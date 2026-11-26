"""Stage 4, Creative Intelligence: the formula library, the winners, the opportunity score and the cross-brand view, all computed from what the Idea Bank already holds.

No model call is made here: everything is plain code over the saved items, so every number can be re-checked by hand. Every report shows its SAMPLE SIZE and a CONFIDENCE label (the Stage 4 gate), and a figure that
cannot be computed from the sample is reported as 'not enough data', never guessed.

* The library: 26 Reel hook formulas and 21 YouTube hook formulas (two MIT-licensed repositories; snapshots live in contentintel/data/formulas, a newer checkout in the external folder is used when present).
* Winners: an item is a winner when its signal is at least 3 times the median of its own platform in the same brand (the outlier multiple), with at least 4 items to compare against.
* Opportunity score: Fit 30%, momentum 20%, originality 15%, feasibility 15%, strategic value 10%, evidence 10%. An input that cannot be known is neutral AND listed, and lowers the confidence.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from statistics import median

DATA = Path(__file__).parent / "data" / "formulas"
WINNER_MULTIPLE = 3.0
MIN_GROUP = 4                       # the least items a platform needs in a brand before an outlier multiple means anything
MIN_TAGGED_FOR_LIFT = 8
WEIGHTS = {"fit": 0.30, "momentum": 0.20, "originality": 0.15, "feasibility": 0.15, "strategic": 0.10, "evidence": 0.10}
SHORT_VIDEO = ("instagram", "tiktok", "meta_ads")


class Library:
    def __init__(self, ig, yt, slop, source, overrides=None):
        self.ig, self.yt, self.slop, self.source = ig, yt, slop, source
        self.overrides = overrides or {}
        self._ig_by_id = {h["id"]: h for h in ig["hooks"]}
        self._ig_order = [self._ig_by_id[i] for i in ig.get("classify_order", []) if i in self._ig_by_id] or list(ig["hooks"])
        ig_over, yt_over = self.overrides.get("instagram", {}), self.overrides.get("youtube", {})
        self._ig_re, self._yt_re = {}, {}
        for h in ig["hooks"]:
            key = str(h["id"])
            base = (ig_over.get(key, {}).get("replace") or h.get("match") or "")
            parts = [f"(?:{base})"] if base else []
            parts += [f"(?:{x})" for x in ig_over.get(key, {}).get("add", [])]
            if parts:
                self._ig_re[h["id"]] = re.compile("|".join(parts), re.I)
        for h in yt["hooks"]:
            pats = list(h.get("match", [])) + list(yt_over.get(h["id"], {}).get("add", []))
            self._yt_re[h["id"]] = [re.compile(x, re.I) for x in pats]

    @property
    def counts(self):
        return {"instagram": len(self.ig["hooks"]), "youtube": len(self.yt["hooks"])}

    def classify(self, text, platform="instagram"):
        """{'id', 'name', 'platform'} of the first formula (most specific first) the hook line matches, or None."""
        line = " ".join(str(text or "").split())[:400]                           # the whole hook, as the library's own patterns were written for it (not only its first sentence)
        if not line:
            return None
        if platform == "youtube":
            best, best_len = None, 0
            for h in self.yt["hooks"]:                                          # the formula whose patterns cover the most of the hook wins (file order breaks a tie): 'any number and a percent' no longer beats a pattern written for the shape itself
                covered = 0
                for rx in self._yt_re[h["id"]]:
                    m = rx.search(line)
                    covered += (len(m.group(0)) * (2 if rx.pattern.startswith("^") else 1)) if m else 0          # a pattern anchored to the start of the hook says more about its shape than one found anywhere
                if covered > best_len:
                    best, best_len = h, covered
            return {"id": f"yt:{best['id']}", "name": best["name"], "platform": "youtube"} if best else None
        for h in self._ig_order:
            rx = self._ig_re.get(h["id"])
            if rx and rx.search(line):
                return {"id": f"ig:{h['id']}", "name": h["name"], "platform": "instagram"}
        return None

    def examples(self):
        """[(platform, formula id, name, example text)] - the worked example each formula ships with."""
        out = [("instagram", f"ig:{h['id']}", h["name"], h.get("example", "")) for h in self.ig["hooks"]]
        out += [("youtube", f"yt:{h['id']}", h["name"], h.get("example", "")) for h in self.yt["hooks"]]
        return [e for e in out if e[3]]


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_library(external_root=None, data_root=None):
    """The formula library: a newer checkout of the two skill repositories when one is present in the external folder, else the snapshots in contentintel/data/formulas. Says which."""
    found = {}
    if external_root:
        root = Path(external_root)
        for key, rel in (("ig", "instagram_skill/skills/ig-reel/hooks.json"), ("yt", "youtube_skill/skills/yt-script/hooks.json"), ("slop", "instagram_skill/skills/ig-human/slop.json")):
            if (root / rel).is_file():
                try:
                    found[key] = _read(root / rel)
                except (OSError, ValueError):
                    pass
    base = Path(data_root) if data_root else DATA
    overrides = _read(base / "overrides.json") if (base / "overrides.json").is_file() else {}
    data = {"ig": found.get("ig") or _read(base / "ig_hooks.json"), "yt": found.get("yt") or _read(base / "yt_hooks.json"), "slop": found.get("slop") or _read(base / "slop.json")}
    source = "external checkout: " + ", ".join(sorted(found)) if found else "snapshot in contentintel/data/formulas"
    if found and len(found) < 3:
        source += " (the rest from the snapshot)"
    return Library(data["ig"], data["yt"], data["slop"], source, overrides)


def hook_line(text):
    """The first sentence or line of a text: the part a viewer takes in first."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    m = re.search(r"[.!?](?:\s|$)", text)
    return (text[:m.end()] if m else text)[:300].strip()


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def signal_of(item):
    """(kind, value) of the item's performance signal, or None: interactions for a social post, days running for an ad (an ad someone kept paying for is better evidence than any score)."""
    m = item.get("metrics") or {}
    if item.get("platform") == "meta_ads":
        d = _num(m.get("days_running"))
        return ("days_running", d) if d is not None else None
    parts = [_num(m.get(k)) for k in ("likes", "comments", "shares", "saves")]
    parts = [p for p in parts if p is not None]
    return ("interactions", sum(parts)) if parts else None


def confidence_of(n):
    return "high" if n >= 25 else "medium" if n >= 8 else "low"


def _live(bank):
    return [it for it in (bank or {}).get("items", {}).values() if it.get("state") != "removed"]


def outlier_multiples(items):
    """{item id: {'multiple', 'group_n', 'confidence'}}: the signal over the median signal of the same platform in the same brand. A group under MIN_GROUP gives multiple None (not enough to compare against)."""
    groups = {}
    for it in items:
        s = signal_of(it)
        if s is not None:
            groups.setdefault((it.get("platform"), s[0]), []).append((it["id"], s[1]))
    out = {}
    for (_platform, _kind), rows in groups.items():
        base = median(v for _i, v in rows)
        for iid, value in rows:
            multiple = round(value / base, 2) if len(rows) >= MIN_GROUP and base > 0 else None
            out[iid] = {"multiple": multiple, "group_n": len(rows), "confidence": confidence_of(len(rows))}
    return out


def platform_of_item(item):
    return "youtube" if item.get("platform") == "youtube" else "instagram"


def tag_with_field(item, lib):
    """(formula or None, the field that matched): every text the item has is tried, not only the first one that is not empty (a hook line that matches nothing used to hide a title or a caption that does): the hook line, the title, the first
    sentence of the text and then the text itself. The library's patterns describe SPOKEN hooks, so a caption or an ad's body matches less often than a hook line."""
    platform = platform_of_item(item)
    for field, value in (("hook_line", item.get("hook_line")), ("title", item.get("title")), ("first sentence of the text", hook_line(item.get("text"))), ("text", item.get("text"))):
        if value:
            found = lib.classify(value, platform)
            if found:
                return found, field
    return None, ""


def tag(item, lib):
    return tag_with_field(item, lib)[0]


def coverage(items, lib, sample=8):
    """How much of the bank the library names, and what it does not: {'kept', 'tagged', 'rate', 'by_platform': {platform: [tagged, total]}, 'by_field': {...}, 'untagged_sample': [{id, platform, text}]}.
    The sample is what a person reads to judge whether the library is missing shapes it should know or the items simply are not hooks."""
    by_platform, by_field, untagged = {}, {}, []
    tagged = 0
    for it in items:
        found, field = tag_with_field(it, lib)
        row = by_platform.setdefault(it.get("platform") or "?", [0, 0])
        row[1] += 1
        if found:
            row[0] += 1
            tagged += 1
            by_field[field] = by_field.get(field, 0) + 1
        elif len(untagged) < sample:
            text = it.get("hook_line") or it.get("title") or it.get("text") or ""
            untagged.append({"id": it["id"], "platform": it.get("platform", ""), "text": " ".join(str(text).split())[:160]})
    return {"kept": len(items), "tagged": tagged, "rate": round(tagged / len(items), 3) if items else 0.0, "by_platform": by_platform, "by_field": by_field, "untagged_sample": untagged}


def winners(items, lib):
    """Items whose signal is at least WINNER_MULTIPLE times their platform's median, best first, each with its formula (or None), the group it was compared with and the confidence."""
    multiples = outlier_multiples(items)
    out = []
    for it in items:
        m = multiples.get(it["id"])
        if m and m["multiple"] is not None and m["multiple"] >= WINNER_MULTIPLE:
            f = tag(it, lib)
            out.append({"id": it["id"], "platform": it.get("platform"), "handle": it.get("handle", ""), "url": it.get("url", ""), "multiple": m["multiple"], "group_n": m["group_n"], "confidence": m["confidence"],
                        "formula": f["id"] if f else "", "formula_name": f["name"] if f else ""})
    return sorted(out, key=lambda w: -w["multiple"])


def formula_report(items, lib, winner_ids):
    """Per formula: how many items use it, how many winners, and the lift (the share of winners that use it over the share of all tagged items that use it). Lift is only given with at least MIN_TAGGED_FOR_LIFT tagged items."""
    tagged = [(it, tag(it, lib)) for it in items]
    tagged = [(it, f) for it, f in tagged if f]
    n_all, n_win = len(tagged), sum(1 for it, _f in tagged if it["id"] in winner_ids)
    rows = {}
    for it, f in tagged:
        r = rows.setdefault(f["id"], {"formula": f["id"], "name": f["name"], "items": 0, "winners": 0})
        r["items"] += 1
        r["winners"] += 1 if it["id"] in winner_ids else 0
    out = []
    for r in rows.values():
        lift = round((r["winners"] / n_win) / (r["items"] / n_all), 2) if n_all >= MIN_TAGGED_FOR_LIFT and n_win and r["items"] else None
        out.append({**r, "lift": lift, "confidence": confidence_of(r["items"]), "note": "" if lift is not None else f"lift needs at least {MIN_TAGGED_FOR_LIFT} tagged items and one winner (this brand has {n_all} tagged, {n_win} winner(s))"})
    return sorted(out, key=lambda r: (-(r["lift"] or 0), -r["items"])), n_all


def _recency(item, now):
    from datetime import datetime
    raw = str(item.get("posted_at") or "")[:10]
    try:
        days = (datetime.strptime(str(now)[:10], "%Y-%m-%d") - datetime.strptime(raw, "%Y-%m-%d")).days
    except ValueError:
        return None
    return max(0.0, min(1.0, 1.0 - days / 90.0))


def opportunity_score(item, ctx):
    """{'score' 0-100, 'parts', 'unknown', 'confidence'}. ctx: {'now', 'formula_share': {formula id: share of items}, 'multiples': outlier_multiples(...)}. Every input is 0-1; an input that cannot be known is 0.5 and named in 'unknown'."""
    unknown, parts = [], {}
    score = item.get("score")
    parts["fit"] = round(float(score) / 100.0, 3) if isinstance(score, (int, float)) else (unknown.append("fit") or 0.5)
    m = item.get("metrics") or {}
    days = _num(m.get("days_running"))
    rec = _recency(item, ctx.get("now", ""))
    if item.get("platform") == "meta_ads" and days is not None:
        parts["momentum"] = round(min(days / 60.0, 1.0), 3)
    elif rec is not None:
        parts["momentum"] = round(rec, 3)
    else:
        parts["momentum"] = unknown.append("momentum") or 0.5
    f = ctx.get("formula")
    share = (ctx.get("formula_share") or {}).get(f["id"]) if f else None
    parts["originality"] = round(1.0 - share, 3) if share is not None else (unknown.append("originality") or 0.5)
    fmt = f"{item.get('media_format', '')} {item.get('format', '')}".upper()
    parts["feasibility"] = 0.9 if any(k in fmt for k in ("IMAGE", "STATIC", "CAROUSEL")) else 0.6 if any(k in fmt for k in ("VIDEO", "REEL")) else (unknown.append("feasibility") or 0.5)
    parts["strategic"] = 1.0 if (item.get("approved") or item.get("rescued")) else 0.4
    has = sum(bool(x) for x in (item.get("url"), item.get("posted_at"), item.get("metrics")))
    parts["evidence"] = round(has / 3.0, 3)
    total = round(100 * sum(WEIGHTS[k] * parts[k] for k in WEIGHTS), 1)
    return {"score": total, "parts": parts, "unknown": unknown, "confidence": "high" if not unknown else "medium" if len(unknown) <= 2 else "low"}


def build_report(slug, bank, lib, now, top=20):
    """The whole Stage 4 report for one brand."""
    items = _live(bank)
    kept = [it for it in items if it.get("state") == "kept"]
    multiples = outlier_multiples(kept)
    win = winners(kept, lib)
    win_ids = {w["id"] for w in win}
    formulas, n_tagged = formula_report(kept, lib, win_ids)
    share = {r["formula"]: r["items"] / n_tagged for r in formulas} if n_tagged else {}
    opps = []
    for it in kept:
        f = tag(it, lib)
        o = opportunity_score(it, {"now": now, "formula_share": share, "formula": f, "multiples": multiples})
        opps.append({"id": it["id"], "platform": it.get("platform"), "handle": it.get("handle", ""), "title": (it.get("title") or it.get("hook_line") or it.get("text") or "")[:100],
                     "formula": f["id"] if f else "", **o})
    opps.sort(key=lambda o: -o["score"])
    with_signal = sum(1 for it in kept if signal_of(it) is not None)
    cov = coverage(kept, lib)
    notes = []
    if kept and cov["rate"] < 0.5:
        notes.append(f"the library names a formula for only {cov['tagged']} of {len(kept)} kept item(s) ({int(cov['rate'] * 100)}%): its patterns describe spoken hooks, so captions and ad copy match less; read the untagged sample to judge whether it is missing shapes or the items are not hooks")
    if len(kept) < MIN_GROUP:
        notes.append(f"only {len(kept)} kept item(s): too few to compare anything; the report shows what it can and marks the rest as not enough data")
    if with_signal < len(kept):
        notes.append(f"{len(kept) - with_signal} kept item(s) carry no performance signal (no likes, comments, shares or days running), so they cannot be winners")
    age = library_age_days(now)
    if age is None or age > STALE_DAYS:
        notes.append(f"the formula library copy was last checked {'never' if age is None else f'{age} days ago'}: run Creative intelligence > refresh-library")
    return {"brand": slug, "generated_at": now, "library": {"source": lib.source, **lib.counts, "version": {k: v["commit"][:10] for k, v in read_version().get("files", {}).items()}, "checked_at": read_version().get("checked_at", ""), "age_days": age},
            "sample": {"kept_items": len(kept), "with_signal": with_signal, "tagged_with_a_formula": n_tagged, "winners": len(win), "confidence": confidence_of(len(kept))},
            "coverage": cov, "winners": win[:top], "formulas": formulas[:top], "opportunities": opps[:top], "weights": WEIGHTS, "notes": notes}


def cross_brand(reports):
    """Which formulas win in more than one brand, and which win in one brand while another brand's kept items never use them (a transferable idea). Needs at least two reports."""
    win = {}
    used = {}
    for rep in reports:
        for f in rep["formulas"]:
            used.setdefault(f["formula"], set()).add(rep["brand"])
            if f["winners"]:
                win.setdefault(f["formula"], set()).add(rep["brand"])
    brands = {r["brand"] for r in reports}
    shared = sorted(f for f, b in win.items() if len(b) >= 2)
    transfer = [{"formula": f, "wins_in": sorted(b), "unused_in": sorted(brands - used.get(f, set()))} for f, b in sorted(win.items()) if len(b) == 1 and brands - used.get(f, set())]
    return {"brands": sorted(brands), "winning_in_several_brands": shared, "could_transfer": transfer,
            "note": "" if len(reports) >= 2 else "cross-brand needs at least two brands' reports"}


def render(report):
    """Plain lines for the run page."""
    s = report["sample"]
    lines = [f"{report['brand']}: {s['kept_items']} kept item(s) (confidence {s['confidence']}), {s['with_signal']} with a performance signal, {s['tagged_with_a_formula']} tagged with a formula, {s['winners']} winner(s); library: {report['library']['source']}"]
    cov = report.get("coverage")
    if cov:
        lines.append(f"  coverage: {cov['tagged']} of {cov['kept']} kept item(s) name a formula ({int(cov['rate'] * 100)}%); by platform " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(cov["by_platform"].items())) + (f"; matched through {', '.join(f'{k} {v}' for k, v in sorted(cov['by_field'].items()))}" if cov["by_field"] else ""))
        for u in cov["untagged_sample"][:4]:
            lines.append(f"    untagged {u['id']} ({u['platform']}): {u['text'][:90]}")
    for w in report["winners"][:5]:
        lines.append(f"  winner {w['id']} ({w['platform']}, {w['handle'] or 'unknown'}): {w['multiple']}x the median of {w['group_n']} ({w['confidence']} confidence)" + (f", formula {w['formula_name']}" if w["formula"] else ", no known formula"))
    for f in report["formulas"][:5]:
        lines.append(f"  formula {f['name']}: {f['items']} item(s), {f['winners']} winner(s), lift {f['lift'] if f['lift'] is not None else 'n/a'} ({f['confidence']} confidence)")
    for o in report["opportunities"][:5]:
        lines.append(f"  opportunity {o['id']}: {o['score']} ({o['confidence']} confidence" + (f"; not known: {', '.join(o['unknown'])}" if o["unknown"] else "") + f") {o['title'][:60]}")
    lines += [f"  note: {n}" for n in report["notes"]]
    return lines

# ---- keeping the library current -----------------------------------------------------------------------------------------------------------------------------------------------------------

SOURCES = {"ig_hooks": ("Jakeschincariol/instagram-agent-skill", "skills/ig-reel/hooks.json", "ig_hooks.json"),
           "yt_hooks": ("Jakeschincariol/youtube-agent-skill", "skills/yt-script/hooks.json", "yt_hooks.json"),
           "slop": ("Jakeschincariol/instagram-agent-skill", "skills/ig-human/slop.json", "slop.json")}
STALE_DAYS = 14                      # a copy last checked longer ago than this is called stale in every report


def check_data(key, data):
    """'' when a fetched file is usable, else why not (nothing that fails this ever replaces the copy in use)."""
    try:
        if key == "ig_hooks":
            hooks = data["hooks"]
            if len(hooks) < 20 or not all(h.get("id") is not None and h.get("name") and isinstance(h.get("match"), str) for h in hooks):
                return "fewer than 20 Reel formulas, or a formula without an id, name or pattern"
            for h in hooks:
                re.compile(h["match"])
            if not set(data.get("classify_order", [])) <= {h["id"] for h in hooks}:
                return "classify_order names a formula that does not exist"
        elif key == "yt_hooks":
            hooks = data["hooks"]
            if len(hooks) < 15 or not all(h.get("id") and h.get("name") and isinstance(h.get("match"), list) and h["match"] for h in hooks):
                return "fewer than 15 YouTube formulas, or a formula without an id, name or patterns"
            for h in hooks:
                for p in h["match"]:
                    re.compile(p)
        else:
            if not all(isinstance(data.get(k), list) and data[k] for k in ("invisible", "words", "phrases", "structures")):
                return "the lexicon lacks invisible characters, words, phrases or structures"
            for e in data["structures"]:
                re.compile(e["regex"])
    except (KeyError, TypeError, re.error, AttributeError) as exc:
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    return ""


def read_version(root=DATA):
    path = Path(root) / "VERSION.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def library_age_days(now, root=DATA):
    checked = str(read_version(root).get("checked_at") or "")[:10]
    try:
        return (datetime.strptime(str(now)[:10], "%Y-%m-%d") - datetime.strptime(checked, "%Y-%m-%d")).days
    except ValueError:
        return None


def refresh(fetch, now, root=DATA, golden=None, head=None):
    """Pull the newest version of the three data files from their repositories and keep the copy in `root` current, recording which commit each came from (VERSION.json).
    fetch(url) -> text (raises on failure); head(repo) -> the newest commit id (default: GitHub's API through fetch). Nothing replaces the copy in use unless it parses, has the expected shape and (when `golden` is given) the validator bank's golden set still passes with it; otherwise the copy stays and the report says why.
    Returns {'status': 'up to date'|'updated'|'refused'|'failed', 'lines': [...], 'changed': [...]}."""
    root = Path(root)
    lines, fresh, commits = [], {}, {}
    for key, (repo, path, fname) in SOURCES.items():
        try:
            if repo not in commits:
                commits[repo] = (head or (lambda r: json.loads(fetch(f"https://api.github.com/repos/{r}/commits/HEAD"))["sha"]))(repo)
            sha = commits[repo]
            text = fetch(f"https://raw.githubusercontent.com/{repo}/{sha}/{path}")
            data = json.loads(text)
        except Exception as exc:                                                            # noqa: BLE001
            return {"status": "failed", "lines": [f"library refresh: could not read {repo}/{path}: {type(exc).__name__}: {str(exc)[:140]}; the copy in use is kept"], "changed": []}
        why = check_data(key, data)
        if why:
            return {"status": "refused", "lines": [f"library refresh: {repo}/{path} at {sha[:10]} was REFUSED ({why}); the copy in use is kept"], "changed": []}
        fresh[key] = (text, data, repo, path, sha, fname)
    changed = [k for k, (text, *_r) in fresh.items() if not (root / fresh[k][5]).is_file() or hashlib.sha256((root / fresh[k][5]).read_bytes()).hexdigest() != hashlib.sha256(text.encode("utf-8")).hexdigest()]
    old_files = {k: (root / fresh[k][5]).read_bytes() for k in changed if (root / fresh[k][5]).is_file()}
    for k in changed:
        (root / fresh[k][5]).write_text(fresh[k][0], encoding="utf-8")
    if changed and golden is not None:
        res = golden(load_library(None, root))
        if res["failures"]:
            for k in changed:                                                                  # the new data made the validator bank's own check fail: put the old copy back
                if k in old_files:
                    (root / fresh[k][5]).write_bytes(old_files[k])
            return {"status": "refused", "lines": ["library refresh: the newest data was REFUSED because the golden set fails with it (" + res["failures"][0][:140] + "); the copy in use is kept"], "changed": []}
    version = {"checked_at": now, "note": "which commit of each repository the copy in this folder came from; written by `intel --action refresh-library`",
               "files": {k: {"repo": r, "path": p, "commit": sha, "sha256": hashlib.sha256((root / f).read_bytes()).hexdigest(),
                             "entries": len(d.get("hooks", [])) or len(d.get("words", [])), "changed_on": now if k in changed else (read_version(root).get("files", {}).get(k, {}).get("changed_on") or now)}
                         for k, (_t, d, r, p, sha, f) in fresh.items()}}
    (root / "VERSION.json").write_text(json.dumps(version, indent=2) + "\n", encoding="utf-8")
    if changed:
        lines.append("library refresh: UPDATED " + ", ".join(f"{k} (now {fresh[k][4][:10]})" for k in changed))
    else:
        lines.append("library refresh: up to date (" + ", ".join(f"{k} at {fresh[k][4][:10]}" for k in fresh) + ")")
    return {"status": "updated" if changed else "up to date", "lines": lines, "changed": changed}
