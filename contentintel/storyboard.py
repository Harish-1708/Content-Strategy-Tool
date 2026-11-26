"""Stage 7 layer 1: the storyboard and the styleframes, built by CODE from a brief that already passed the rules, the claims gate, the validator bank and the reviewer. No model is called and no video is rendered (layers 2 and 3
wait for the Remotion licence answer and a timed test render).

For one concept of a brief: the hook becomes the first shot (three seconds at most), each sentence of the script a shot, the call to action the last; every shot has its seconds (words at 2.5 a second, then fitted to the length the format
names, 30 seconds when it names none), its spoken line, a short on-screen line (six words at most), its visual, the product and a picture query. Every text is checked again (slop, rules, the claims gate) because a storyboard is
a copy of the brief that a person may have edited. Each shot then gets a STYLEFRAME: a still the brand may use (its own saved pictures, its own site, Openverse, Wikimedia Commons, Pixabay), with its licence and credit, or an honest 'none found'.
"""
import html
import re

from . import validators as V

WORDS_PER_SECOND = 2.5
HOOK_MAX_SECONDS = 3.0
SHOT_MIN, SHOT_MAX = 1.5, 8.0
ON_SCREEN_WORDS = 6
DEFAULT_SECONDS = 30
FIT_TOLERANCE = 0.2          # a storyboard further than this share from the length the format names says so


def parse_seconds(fmt, default=DEFAULT_SECONDS):
    m = re.search(r"(\d{1,3})\s*[- ]?\s*(?:second|sec)\b", str(fmt or ""), re.I)
    return int(m.group(1)) if m else default


def sentences(text):
    """The sentences (or numbered / bulleted lines) of a text, in order."""
    out = []
    for line in str(text or "").replace("\r", "").split("\n"):
        line = re.sub(r"^\s*(?:\d+[.)]|[-*\u2022])\s*", "", line).strip()
        out += [p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]
    return out


def short(text, n=ON_SCREEN_WORDS):
    words = V.words_of(text)
    return " ".join(words[:n]).strip(" ,.;:-")


def _query(visual, spoken, product):
    stop = {"with", "that", "this", "then", "from", "into", "over", "show", "shot", "while", "their", "there", "about", "close", "wide"}
    words = [w.lower() for w in V.words_of(f"{product} {visual or spoken}") if len(w) >= 4 and w.lower() not in stop]
    return " ".join(dict.fromkeys(words))[:80]


def build(concept, *, brief_id="", concept_no=1):
    """The storyboard of one concept ({name, format, hook, script, visual_direction, cta, product}) as {'concept', 'target_seconds', 'shots': [...], 'total_seconds', 'notes': [...]}."""
    target = parse_seconds(concept.get("format"))
    visuals = sentences(concept.get("visual_direction")) or [""]
    product = str(concept.get("product") or "")
    beats = sentences(concept.get("script"))
    plan = [("hook", str(concept.get("hook") or "").strip())] + [("beat", b) for b in beats] + ([("cta", str(concept.get("cta") or "").strip())] if str(concept.get("cta") or "").strip() else [])
    raw = []
    for role, spoken in plan:
        secs = max(SHOT_MIN, min(SHOT_MAX, len(V.words_of(spoken)) / WORDS_PER_SECOND))
        raw.append([role, spoken, secs])
    if raw and raw[0][0] == "hook":
        raw[0][2] = min(raw[0][2], HOOK_MAX_SECONDS)
    fixed = raw[0][2] if raw and raw[0][0] == "hook" else 0.0
    rest = sum(r[2] for r in raw[1:])
    notes = []
    if rest > 0 and target > fixed:
        factor = (target - fixed) / rest
        for r in raw[1:]:
            r[2] = max(SHOT_MIN, min(SHOT_MAX, r[2] * factor))                                  # fitted to the length the format names, never below or above what a shot can hold
    shots = []
    for n, (role, spoken, secs) in enumerate(raw, start=1):
        visual = visuals[min(n - 1, len(visuals) - 1)]
        shots.append({"n": n, "role": role, "seconds": round(secs, 1), "spoken": spoken, "on_screen": short(spoken), "visual": visual, "product": product, "still_query": _query(visual, spoken, product), "still": None})
    total = round(sum(s["seconds"] for s in shots), 1)
    if abs(total - target) > target * FIT_TOLERANCE:
        notes.append(f"the shots add up to {total} s but the format names {target} s: the script is too {'short' if total < target else 'long'} for it ("
                     + (f"add beats to the script, or name a shorter format such as {int(round(total / 5.0) * 5) or 5} second" if total < target else "cut beats from the script, or name a longer format") + ")")
    if not beats:
        notes.append("the script has no sentences: the storyboard holds only the hook and the call to action")
    return {"brief": brief_id, "concept_no": concept_no, "concept": str(concept.get("name") or ""), "format": str(concept.get("format") or ""), "target_seconds": target, "aspect": "9:16",
            "shots": shots, "total_seconds": total, "notes": notes}


def check(board, lib, *, rules=None, claim_gate=None):
    """Findings for a storyboard, as {where, rule, severity, message}: the hook shot's length, the on-screen length, slop in every text, the brand's text rules and the claims gate over the whole spoken text."""
    out = []
    for s in board["shots"]:
        where = f"shot {s['n']} ({s['role']})"
        if s["role"] == "hook" and s["seconds"] > HOOK_MAX_SECONDS:
            out.append({"where": where, "rule": "hook_shot_long", "severity": "warning", "message": f"the hook shot lasts {s['seconds']} s; a hook has {HOOK_MAX_SECONDS:g} seconds at most"})
        if len(V.words_of(s["on_screen"])) > ON_SCREEN_WORDS:
            out.append({"where": where, "rule": "on_screen_long", "severity": "warning", "message": f"the on-screen line is longer than {ON_SCREEN_WORDS} words"})
        for field in ("spoken", "on_screen", "visual"):
            out += V.slop_check(s[field], lib, f"{where} {field.replace('_', ' ')}")
            if rules is not None and s[field]:
                from . import rules as rules_mod
                for v in rules_mod.check_text(rules, s[field], scope="hook" if s["role"] == "hook" and field == "spoken" else "copy"):
                    out.append({"where": f"{where} {field.replace('_', ' ')}", "rule": v.rule, "severity": v.severity, "message": v.message})
    if claim_gate is not None:
        concept = {"name": board["concept"], "hook": next((s["spoken"] for s in board["shots"] if s["role"] == "hook"), ""), "script": " ".join(s["spoken"] for s in board["shots"] if s["role"] == "beat"),
                   "visual_direction": " ".join(s["visual"] for s in board["shots"]), "cta": next((s["spoken"] for s in board["shots"] if s["role"] == "cta"), "")}
        out += claim_gate({"concepts": [concept]})
    for n in board["notes"]:
        out.append({"where": "storyboard", "rule": "length", "severity": "warning", "message": n})
    return out


def add_styleframes(board, finder):
    """A still for every shot. finder(query, kind) -> [{url, title, license, attribution, source, ...}] (the brand's saved pictures first, then the usable-picture search); the first one with a licence is used; none found is said plainly. Returns how many shots got one."""
    got = 0
    for s in board["shots"]:
        try:
            found = [a for a in (finder(s["still_query"], "image") or []) if a.get("url")] if s["still_query"] else []
        except Exception as exc:                                                            # noqa: BLE001
            s["still"] = {"missing": f"the picture search failed ({type(exc).__name__})"}
            continue
        if not found:
            s["still"] = {"missing": "no usable picture was found for: " + (s["still_query"] or "(no query)")}
            continue
        pick = next((a for a in found if a.get("license")), found[0])
        s["still"] = {"url": pick["url"], "title": str(pick.get("title", ""))[:120], "license": str(pick.get("license", "")), "credit": str(pick.get("attribution") or pick.get("credit") or ""), "source": str(pick.get("source", ""))}
        got += 1
    return got


def render_html(board, findings=()):
    """A contact sheet a person can open: one card per shot with its still (when there is one), seconds, words, visual and the findings."""
    esc = html.escape
    cards = []
    for s in board["shots"]:
        st = s.get("still") or {}
        pic = (f'<img src="{esc(st["url"])}" alt="{esc(s["visual"][:80])}" style="max-width:100%;max-height:240px"><div class="credit">{esc(st.get("license", ""))} {esc(st.get("credit", ""))}</div>' if st.get("url")
               else f'<div class="missing">{esc(st.get("missing", "no still yet"))}</div>')
        cards.append(f'<div class="shot"><h3>Shot {s["n"]} <small>{esc(s["role"])} &middot; {s["seconds"]} s</small></h3>{pic}<p><b>Says:</b> {esc(s["spoken"])}</p><p><b>On screen:</b> {esc(s["on_screen"])}</p><p><b>Visual:</b> {esc(s["visual"])}</p></div>')
    notes = "".join(f"<li>{esc(f['where'])}: {esc(f['message'])}</li>" for f in findings)
    return ('<!doctype html><meta charset="utf-8"><title>Storyboard</title><style>body{font-family:sans-serif;max-width:1000px;margin:1em auto}.shot{border:1px solid #ccc;border-radius:6px;padding:8px 12px;margin:10px 0}'
            '.credit,.missing{font-size:12px;color:#666}.missing{color:#a00}</style>'
            + ('<p style="background:#fde8e8;border:1px solid #c33;padding:6px 10px"><b>BLOCKED</b>: this concept broke a rule or the claims gate and is not ready; see the findings below.</p>' if board.get("status") == "blocked" else "")
            + f'<h1>{esc(board["concept"])}</h1><p>{esc(board["format"])} &middot; {board["total_seconds"]} s of {board["target_seconds"]} s &middot; {esc(board["aspect"])} &middot; brief {esc(board["brief"])}, concept {board["concept_no"]}</p>'
            + "".join(cards) + (f"<h2>Findings</h2><ul>{notes}</ul>" if notes else ""))
