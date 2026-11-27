"""Stage 5, Brief Generation v2: the validator bank (exact checks, no model), the golden set, the rule versions and the independent reviewer.

The bank looks at every concept of a brief: the hook (short enough to be heard in about two seconds, and which of the 47 known formulas it uses, if any), AI-writing slop (invisible characters, stock words, stock
phrases and stock sentence shapes from the lexicon), and the structure each format needs. It works the same for every brand: nothing in this file names a brand, so a new brand passes with no code change (the Defi gate).
Findings use the shape the brief already uses ({where, rule, severity, message}); only an invisible character or a missing part is an ERROR, the rest are WARNINGS the owner sees.

The golden set is the proof that the bank itself still works: every worked example of the 47 formulas, and a list of texts that MUST be flagged, are run through it, so a change to a validator that stops it seeing what it saw is
caught. The reviewer is an independent pass by a model that did not write the brief: it gets the brief, the rules and the validator findings, and answers pass or revise.
"""
import re

from . import formulas as F

HOOK_WORDS_WARN = 16            # a spoken hook of about two seconds is some twelve words; sixteen is the point to warn
HOOK_WORDS_ERROR = 45           # beyond this it is a paragraph, not a hook
SCRIPT_WORDS_MIN = 8
STATIC_WORDS_MAX = 90


def _finding(where, rule, severity, message):
    return {"where": where, "rule": rule, "severity": severity, "message": message}


def words_of(text):
    return re.findall(r"[A-Za-z0-9$%'’-]+(?:[,.]\d+)*", str(text or ""))             # "$18,000" and "2.5" are one word each


def slop_check(text, lib, where="text"):
    """Findings for AI-writing slop in a text: invisible characters (error), stock words, stock phrases and stock sentence shapes (warnings), and one warning for typographic marks."""
    text = str(text or "")
    out = []
    slop = lib.slop
    for ch in slop.get("invisible", []):
        cp = str(ch.get("cp", ""))
        try:
            char = chr(int(cp.replace("U+", ""), 16))
        except ValueError:
            continue
        if char in text:
            out.append(_finding(where, "slop_invisible", "error", f"contains an invisible character ({ch.get('name', cp)}): delete it"))
    for kind, rule in (("phrases", "slop_phrase"), ("words", "slop_word")):
        for entry in slop.get(kind, []):
            find = str(entry.get("find", ""))
            if find and re.search(r"(?<![A-Za-z])" + re.escape(find) + r"(?![A-Za-z])", text, re.I):
                out.append(_finding(where, rule, "warning", f"'{find}' reads as AI-written ({entry.get('family', kind)}); say '{entry.get('replace', '')}'" if entry.get("replace") else f"'{find}' reads as AI-written"))
    for entry in slop.get("structures", []):
        try:
            if re.search(entry["regex"], text):
                out.append(_finding(where, "slop_structure", "warning", f"{entry.get('name', entry['id'])}: {entry.get('fix', '')}".strip()))
        except (re.error, KeyError):
            continue
    marks = sorted({t["name"] for t in slop.get("typographic", []) if t.get("from") and t["from"] in text})
    if marks:
        out.append(_finding(where, "slop_typographic", "warning", "typographic marks that read as AI-written: " + ", ".join(marks)))
    return out


def hook_check(hook, lib, where="hook", platform="instagram"):
    """Findings for one hook: empty or far too long (errors), longer than a spoken hook (warning) and not any known formula (warning)."""
    out = []
    n = len(words_of(F.hook_line(hook)))
    if not str(hook or "").strip():
        return [_finding(where, "hook_empty", "error", "the hook is empty")]
    if n > HOOK_WORDS_ERROR:
        out.append(_finding(where, "hook_too_long", "error", f"the first sentence of the hook is {n} words: that is a paragraph, a spoken hook has about two seconds"))
    elif n > HOOK_WORDS_WARN:
        out.append(_finding(where, "hook_long", "warning", f"the first sentence of the hook is {n} words; about {HOOK_WORDS_WARN - 4} fit in the two seconds a spoken hook has"))
    if lib.classify(hook, platform) is None:
        out.append(_finding(where, "hook_no_formula", "warning", "the hook matches none of the 47 known hook formulas, so there is no evidence about how this shape performs"))
    return out


def structure_check(concept, where):
    """Findings for the parts a concept needs, by format: all need a name, format, hook, script and call to action; a carousel needs its slides, a static image a short script, a motion piece a visual direction."""
    out = []
    for field in ("name", "format", "hook", "script", "cta"):
        if not str(concept.get(field) or "").strip():
            out.append(_finding(where, f"missing_{field}", "error", f"the concept has no {field.replace('cta', 'call to action')}"))
    fmt = str(concept.get("format") or "").lower()
    script = str(concept.get("script") or "")
    n = len(words_of(script))
    if script.strip() and n < SCRIPT_WORDS_MIN:
        out.append(_finding(where, "script_thin", "warning", f"the script is {n} words: too thin for a creator to shoot"))
    if "carousel" in fmt and len(re.findall(r"(?im)^\s*(?:slide\s*\d+|\d+[.)])", script)) < 3:
        out.append(_finding(where, "carousel_slides", "warning", "a carousel needs at least three numbered slides in the script"))
    if any(k in fmt for k in ("static", "image", "photo")) and n > STATIC_WORDS_MAX:
        out.append(_finding(where, "static_long", "warning", f"a static image carries {n} words; keep it under {STATIC_WORDS_MAX}"))
    if any(k in fmt for k in ("motion", "animation", "animated")) and len(words_of(concept.get("visual_direction"))) < 10:
        out.append(_finding(where, "motion_direction", "warning", "a motion piece needs a visual direction of at least ten words"))
    return out


def f_hook_shape_repeat(brief, concept, lib):
    """True when this concept's hook names the same known formula as every other concept's (and there are at least three concepts): seven hooks of one shape is what the independent reviewer faulted in BR-0003."""
    concepts = brief.get("concepts", [])
    if len(concepts) < 3:
        return False
    shapes = [lib.classify(c.get("hook"), "instagram") for c in concepts]
    mine = lib.classify(concept.get("hook"), "instagram")
    return bool(mine) and all(s and s["id"] == mine["id"] for s in shapes)


def brief_bank(brief, lib):
    """The whole bank over a brief ({'concepts': [...]}) -> list of findings."""
    out = []
    for n, c in enumerate(brief.get("concepts", []), start=1):
        label = f"concept {n} ({c.get('name', '')})"
        out += structure_check(c, label)
        out += hook_check(c.get("hook"), lib, f"{label} hook")
        if brief.get("kind"):                                                          # a brief written with the production fields (alt hook, angle, awareness, what it tests, size)
            if c.get("alt_hook"):
                out += hook_check(c["alt_hook"], lib, f"{label} alt hook")
                if str(c["alt_hook"]).strip().lower() == str(c.get("hook", "")).strip().lower():
                    out.append(_finding(f"{label} alt hook", "alt_hook_same", "warning", "the alt hook is the same as the hook: there is nothing to test against"))
            for field, rule, what in (("alt_hook", "missing_alt_hook", "an alt hook to test against the hook"), ("angle", "missing_angle", "an angle"), ("awareness", "missing_awareness", "an awareness level (Unaware, Cold or Warm)"), ("tests", "missing_tests", "a line on what it tests")):
                if not str(c.get(field) or "").strip():
                    out.append(_finding(label, rule, "warning", f"the concept has no {what}"))
            if f_hook_shape_repeat(brief, c, lib):
                out.append(_finding(label, "same_hook_shape", "warning", "this concept's hook uses the same formula as every other concept's: the reviewer faults a brief whose hooks all share one shape"))
        for field in ("name", "hook", "alt_hook", "script", "visual_direction", "cta", "angle", "tests"):
            out += slop_check(c.get(field), lib, f"{label} {field.replace('_', ' ')}")
    for field in ("title", "key_message"):
        out += slop_check(brief.get(field), lib, field)
    return out


def summarise(findings):
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {"errors": errors, "warnings": len(findings) - errors, "rules": sorted({f["rule"] for f in findings})}


# ---- the golden set -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

MUST_FLAG = (
    ("slop_word", "Let's delve into why this overall is different."),
    ("slop_phrase", "In today's fast-paced world, comfort matters."),
    ("slop_structure", "It's not just an overall, it's a lifestyle."),
    ("slop_invisible", "Soft cotton\u200b overalls for every day."),
)


def run_golden(lib, extra=()):
    """The golden set against the live bank. Returns {'checked', 'failures': [...], 'library_mismatches': [...]}.
    * every MUST_FLAG text must produce its finding (so a validator that goes blind is caught);
    * every worked example of the 47 formulas must produce no ERROR from the bank and no invisible character;
    * a formula example that does not classify as its own formula is a LIBRARY MISMATCH (reported, not counted against the bank: the example and the pattern come from the library);
    * extra: the owner's own cases from a brand's golden.yaml as [{'text', 'platform', 'expect': 'pass'|'flag', 'rule'}]."""
    failures, mismatches, checked = [], [], 0
    for rule, text in MUST_FLAG:
        checked += 1
        if rule not in {f["rule"] for f in slop_check(text, lib)}:
            failures.append(f"a text that must be flagged as {rule} was not: {text[:60]!r}")
    for platform, fid, name, example in lib.examples():
        checked += 1
        errors = [f for f in hook_check(example, lib, platform=platform) + slop_check(example, lib) if f["severity"] == "error"]
        if errors:
            failures.append(f"the worked example of '{name}' ({fid}) is refused by the bank: {errors[0]['message']}")
        got = lib.classify(example, platform)
        if not got or got["id"] != fid:
            mismatches.append(f"{fid}: its own example classifies as {got['id'] if got else 'nothing'}")
    for case in extra:
        checked += 1
        text, platform = str(case.get("text", "")), case.get("platform", "instagram")
        found = hook_check(text, lib, platform=platform) + slop_check(text, lib)
        errors = [f for f in found if f["severity"] == "error"]
        if case.get("expect") == "pass" and errors:
            failures.append(f"an approved text is refused: {text[:60]!r}: {errors[0]['message']}")
        if case.get("expect") == "flag" and not [f for f in found if not case.get("rule") or f["rule"] == case["rule"]]:
            failures.append(f"a text that must be flagged was not: {text[:60]!r}")
    return {"checked": checked, "failures": failures, "library_mismatches": mismatches}


# ---- rule versions ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def note_rules_version(store, slug, digest, now):
    """(version, changed_from): the version number of a brand's brief rules (a new number each time their digest changes, kept in data/<brand>/extras/rules_versions.json) and the earlier version when this digest is new."""
    data = store.read_extra(slug, "rules_versions") or {"versions": []}
    versions = data.get("versions", [])
    for v in versions:
        if v["digest"] == digest:
            return v["version"], None
    previous = versions[-1]["version"] if versions else None
    version = (previous or 0) + 1
    versions.append({"version": version, "digest": digest, "first_seen": now})
    store.write_extra(slug, "rules_versions", {"note": "each time a brand's brief rules change, they get the next version number; every brief records the version it was written under", "versions": versions})
    return version, previous


# ---- the independent reviewer -----------------------------------------------------------------------------------------------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "You are an independent reviewer of a creative brief. You did not write it and you have no stake in it. Check it against this checklist and answer ONLY with JSON: "
    "{\"verdict\": \"pass\" or \"revise\", \"problems\": [{\"where\": \"which concept and field\", \"issue\": \"what is wrong, in one sentence\"}], "
    "\"checklist\": {\"hook_confirms_the_ask\": true or false, \"one_idea_per_concept\": true or false, \"no_unverifiable_claim\": true or false, \"specific_not_vague\": true or false, \"creator_can_shoot_it\": true or false}}. "
    "Say 'revise' when any checklist item is false or any problem is real. Do not praise. Do not rewrite the brief. Use only what the brief, the rules and the validator findings say.")


def review_prompt(brief, findings, rules_note=""):
    lines = [f"Title: {brief.get('title', '')}", f"Key message: {brief.get('key_message', '')}"]
    for n, c in enumerate(brief.get("concepts", []), start=1):
        lines.append(f"Concept {n} ({c.get('format', '')}) {c.get('name', '')}\n  Hook: {c.get('hook', '')}\n  Script: {c.get('script', '')}\n  Visual direction: {c.get('visual_direction', '')}\n  Call to action: {c.get('cta', '')}")
    if rules_note:
        lines.append("Rules in force: " + rules_note[:1200])
    lines.append("Validator findings (exact checks already run): " + ("; ".join(f"{f['where']}: {f['message']}" for f in findings[:30]) if findings else "none"))
    return "\n".join(lines)


def parse_review(out):
    """{'verdict': 'pass'|'revise'|'unclear', 'problems': [...], 'checklist': {...}} from the reviewer's JSON; anything else is 'unclear' (never a pass)."""
    if not isinstance(out, dict) or out.get("verdict") not in ("pass", "revise"):
        return {"verdict": "unclear", "problems": [], "checklist": {}}
    checklist = {k: bool(v) for k, v in (out.get("checklist") or {}).items() if isinstance(v, bool)}
    problems = [{"where": str(p.get("where", ""))[:120], "issue": str(p.get("issue", ""))[:300]} for p in (out.get("problems") or []) if isinstance(p, dict) and p.get("issue")][:12]
    verdict = out["verdict"]
    if verdict == "pass" and (problems or (checklist and not all(checklist.values()))):
        verdict = "revise"                                   # a pass that lists a problem or a false checklist item is not a pass
    return {"verdict": verdict, "problems": problems, "checklist": checklist}


def review(brief, findings, judge, rules_note=""):
    """Ask the independent reviewer (a pool model through its own lane). Returns parse_review's dict, or {'verdict': 'unavailable', 'why': ...} when the model could not answer: a brief is never blocked by a reviewer that was down."""
    try:
        out = judge.json_task(REVIEW_SYSTEM, review_prompt(brief, findings, rules_note), 700)
    except Exception as exc:                                                         # noqa: BLE001
        return {"verdict": "unavailable", "problems": [], "checklist": {}, "why": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return parse_review(out)
