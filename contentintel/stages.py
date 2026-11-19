"""Batch 2 stages 4, 6, 7, 8 and 9: one engine, five stages, each a chain of the connected marketing skills applied to ONE brand's own material, through the free pool.

Every stage has its OWN job lane (ai_lanes.yaml) and its OWN retest probe (ranking.py reads `probes()` here), so each is ranked from what works live for that exact job, Claude last. A stage:
  * reads only what the brand really has: its brand facts, its story bank, the claims ledger, what the collectors found, the audience voice. What is missing is listed as needs_input, never invented;
  * writes a section at a time; every section goes through the claims gate (BLOCKED -> the claims that block it are checked from public sources by the pool model, then one correction, then the section is
    REJECTED and recorded: the run goes on and does not fail for it; NEEDS_REVIEW is marked);
  * saves only PROPOSALS (data/<brand>/stage_<stage>.json and an export); nothing is posted, sent or published, and nothing is canon until the owner approves it;
  * the store-number skills (GA4, Search Console) stay NEEDS_INPUT: their skills are 'waiting' and are never read.
Brands are not hardcoded: a brand brings its own folder and data; a new brand needs no change here."""
import re

from . import foundation as FD
from . import skills as SK

MAX_TEXT = 8000
INPUT_CAP = 9000
SYSTEM = FD.SYSTEM

STAGES = {
    "competitor_ideas": {
        "title": "Stage 4: competitor-to-ideas", "job": "competitor_ideas", "lane": "competitor_ideas_json", "inputs": ("collected",),
        "sections": (("idea_candidates", "influencer/target/competitor-tracker",
                      "From what competitors are doing (their collected ads and posts below) write 5 to 8 ORIGINAL idea candidates for THIS brand. For each: a title, the angle, why it should work for this brand's audience, and which "
                      "competitor item it responds to (by its number). Never copy a competitor's wording, claim, offer or price."),),
    },
    "creator_chain": {
        "title": "Stage 6: creator chain", "job": "creator_chain", "lane": "creator_chain_json", "inputs": ("collected", "voice"),
        "sections": (("creator_shortlist", "protocol/creator-registry",
                      "From the creators and accounts that appear in the collected material and the audience voice, write a shortlist of creators worth approaching for THIS brand, with the reason for each taken from the material. "
                      "List only creators that appear in the material; never invent a name or a follower count."),
                     ("creator_brief", "influencer/target/brief-generator",
                      "Write one creator brief for this brand: what the creator should show and say, what they must not say (the DO NOT STATE list), the deliverables and how the result is judged. Use only what the material and the ledger allow.")),
    },
    "listen_learn": {
        "title": "Stage 7: listen, measure, learn", "job": "listen_learn", "lane": "listen_learn_json", "inputs": ("voice",),
        "needs_numbers": ("store numbers: GA4 and Search Console are not connected, so no performance figure is read or used (the skills that need them stay waiting)",),
        "sections": (("voice_dossier", "social/explore/voice-dossier-builder",
                      "From the audience voice below (what real people say), write the voice dossier for THIS brand: the words and phrases they use, what they want, what puts them off, with the comment each point rests on."),
                     ("resonance_read", "narrative/evaluate/narrative-resonance-monitor",
                      "Read how the brand's narrative (the canon so far) lines up with what the audience says. List where it matches, where it does not, and what to test next. Do not state any performance number: none is connected.")),
    },
    "voice_creative": {
        "title": "Stage 8: voice-to-creative", "job": "voice_creative", "lane": "voice_creative_json", "inputs": ("voice",),
        "sections": (("short_video_scripts", "social/craft/short-video-scripter",
                      "Write three short-video scripts for THIS brand in the audience's own words (taken from the voice below). Each: hook, beats, call to action. State only what the ledger allows."),
                     ("social_creative", "social/craft/social-creative-builder",
                      "Write five social post concepts for this brand grounded in the audience voice: the line, the visual idea and which voice item it comes from.")),
    },
    "seo_lens": {
        "title": "Stage 9: SEO lens", "job": "seo_lens", "lane": "seo_lens_json", "inputs": ("collected",),
        "needs_numbers": ("store numbers: Search Console and GA4 are not connected, so no ranking or traffic figure is used",),
        "sections": (("keyword_research", "seo-geo/survey/keyword-research",
                      "From the brand's own material and what the collectors found, propose keyword themes for THIS brand with the reason for each. Give no search-volume number: none is available."),
                     ("content_gaps", "seo-geo/survey/content-gap-analysis",
                      "List content the brand's audience asks for or competitors cover that the brand does not, each with the evidence from the material. No traffic claims.")),
    },
}
LANES = tuple(s["lane"] for s in STAGES.values())


def _items_text(rows, limit=INPUT_CAP):
    lines, size = [], 0
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        text = " ".join(str(r.get(k, "")) for k in ("text", "caption", "title", "description", "body", "comment", "message") if r.get(k)).strip()
        if not text:
            continue
        who = str(r.get("source") or r.get("platform") or r.get("handle") or r.get("author") or "")[:40]
        line = f"{i}. [{who}] {re.sub(chr(10) + '+', ' ', text)[:300]}"
        if size + len(line) > limit:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def gather(store, slug, kinds):
    """{kind: text} for the inputs a stage asks for; an empty text means the brand has none yet."""
    out = {}
    if "collected" in kinds:
        out["collected"] = _items_text(store.read_collected(slug)[-200:] if hasattr(store, "read_collected") else [])
    if "voice" in kinds:
        from . import voice as VOICE
        voice = store.read_extra(slug, "voice") if hasattr(store, "read_extra") else None
        out["voice"] = (VOICE.prompt_lines(voice, 40) if voice else "")[:INPUT_CAP]          # the real posts and comments the voice research kept, with their engagement
    return out


def _prompt(skill, task, brand_text, story_text, reg, inputs, feedback=""):
    material = "\n\n".join(f"=== {k.upper()} (numbered items) ===\n{v}" for k, v in inputs.items() if v)
    user = (f"{SK.as_data(skill)}\n\nTASK: {task}\n\n=== BRAND FACTS ===\n{brand_text[:FD.BRAND_LIMIT]}\n\n=== STORY BANK ===\n{story_text[:FD.STORY_LIMIT]}\n\n{material}\n\n"
            f"=== CLAIMS LEDGER ===\n{FD.ledger_summary(reg)}\n\n=== CANON SO FAR ===\n{FD.canon_context(reg)}")
    return user + (f"\n\nYOUR PREVIOUS ANSWER WAS REFUSED because it repeats claims the ledger does not allow:\n{feedback}\nRewrite it without them." if feedback else "")


def run_stage(key, reg, cfg, root, claude, *, slug, name, brand_text, story_text, store, now, max_calls=None, verifier=None, read=SK.read_skill, verify_connection=True, retry_wait=2.0):
    """Run one stage for one brand. Returns [{section, skill, result, detail}] with results proposed, rejected, failed, refused or needs_input, and saves what was proposed."""
    stage = STAGES.get(key)
    if stage is None:
        return [{"section": key, "skill": "", "result": "refused", "detail": f"'{key}' is not a stage; the stages are: {', '.join(STAGES)}"}]
    steps = stage["sections"]
    if claude is None:
        return [{"section": s, "skill": p, "result": "refused", "detail": "no AI client is available in this run"} for s, p, _t in steps]
    if verify_connection:
        trusted = SK.verify(cfg, root)
        if not trusted["ok"]:
            return [{"section": s, "skill": p, "result": "refused", "detail": "the skills connection cannot be trusted: " + "; ".join(trusted["problems"][:2])} for s, p, _t in steps]
    inputs = gather(store, slug, stage["inputs"])
    missing = [k for k in stage["inputs"] if not inputs.get(k)]
    if missing and not (brand_text or "").strip():
        return [{"section": s, "skill": p, "result": "needs_input", "detail": f"this brand has no {' and no '.join(missing)} yet and no brand facts: run the collectors or the voice research first"} for s, p, _t in steps]
    budget = [max_calls if max_calls else None]
    report, proposals = [], {}
    for section, path, task in steps:
        try:
            skill = read(cfg, root, path)
        except SK.SkillsError as exc:
            result = "needs_input" if "NEEDS_INPUT" in str(exc) or "waiting" in str(exc).lower() else "refused"
            report.append({"section": section, "skill": path, "result": result, "detail": str(exc)})
            continue
        if budget[0] is not None and budget[0] <= 0:
            report.append({"section": section, "skill": path, "result": "skipped", "detail": f"this run's limit of {max_calls} model call(s) was reached; the next run continues"})
            continue
        feedback, answer, done = "", None, False
        for attempt in (1, 2):
            answer, why = _ask(claude, stage["job"], _prompt(skill, task, brand_text, story_text, reg, inputs, feedback), budget, retry_wait)
            if answer is None:
                report.append({"section": section, "skill": path, "result": "failed", "detail": "the model could not answer: " + (why or "no answer")})
                done = True
                break
            text = str(answer.get("text") or "").strip()[:MAX_TEXT] if isinstance(answer, dict) else ""
            needs = [str(n) for n in answer.get("needs_input")] if isinstance(answer, dict) and isinstance(answer.get("needs_input"), list) else []
            if not text:
                report.append({"section": section, "skill": path, "result": "needs_input", "detail": "; ".join(needs[:5]) or "the model gave no text and said nothing about what it lacks"})
                done = True
                break
            gate = reg.gate(text)
            if gate["verdict"] == "BLOCKED" and verifier is not None:
                ids = sorted({f["claim"] for f in gate["findings"] if f["result"] == "BLOCKED" and f.get("claim")})
                if ids and verifier(ids):
                    gate = reg.gate(text)
            if gate["verdict"] == "BLOCKED" and attempt == 1:
                feedback = "\n".join(f"- \"{f['sentence'][:140]}\": {f['why']}" for f in gate["findings"] if f["result"] == "BLOCKED")
                continue
            if gate["verdict"] == "BLOCKED":
                report.append({"section": section, "skill": path, "result": "rejected", "detail": "it still repeats blocked claims after one correction: " + "; ".join(f["sentence"][:80] for f in gate["findings"] if f["result"] == "BLOCKED")[:300]})
                done = True
                break
            review = [f for f in gate["findings"] if f["result"] == "NEEDS_REVIEW"]
            note = "gate: OK" if not review else f"gate: NEEDS_REVIEW: {len(review)} checkable statement(s) nothing verifies"
            if needs:
                note += "; needs input: " + "; ".join(needs[:3])
            if missing:
                note += "; the brand has no " + " and no ".join(missing) + " yet"
            proposals[section] = {"text": text, "gate": gate["verdict"], "note": note, "skill": path, "at": now}
            report.append({"section": section, "skill": path, "result": "proposed", "detail": note})
            done = True
            break
        if not done:
            report.append({"section": section, "skill": path, "result": "failed", "detail": "no usable answer"})
    for note in stage.get("needs_numbers", ()):
        report.append({"section": "numbers", "skill": "", "result": "needs_input", "detail": note})
    if proposals:
        save(store, slug, key, stage, proposals, now)
    return report


def save(store, slug, key, stage, proposals, now):
    """Append this run's proposals to data/<brand>/stage_<stage>.json (newest first, nothing replaced) and write a readable export."""
    kept = store.read_extra(slug, f"stage_{key}") if hasattr(store, "read_extra") else None
    runs = (kept or {}).get("runs", []) if isinstance(kept, dict) else []
    runs.insert(0, {"at": now, "proposals": proposals})
    store.write_extra(slug, f"stage_{key}", {"stage": key, "title": stage["title"], "note": "PROPOSALS only: nothing here is canon, posted or sent", "runs": runs[:20]})
    md = [f"# {stage['title']} for {slug}: proposals of {now}", "", "Proposals only: nothing here is canon, posted or sent. Each section was read against the claims gate.", ""]
    for section, p in proposals.items():
        md += [f"## {section}", f"({p['note']})", "", p["text"], ""]
    store.write_export(slug, f"stage_{key}.md", "\n".join(md))


def _ask(claude, job, user, budget, retry_wait=0.0):
    import time
    last = ""
    for attempt in (1, 2):
        if budget[0] is not None and budget[0] <= 0:
            return None, "the call limit of this run was reached" if not last else last
        if budget[0] is not None:
            budget[0] -= 1
        try:
            return FD.for_job(claude, job).json_task(SYSTEM, user, 3500), None
        except Exception as exc:                                                           # noqa: BLE001
            route = FD._route(claude)
            last = f"{FD._why(exc)}" + (f"; route tried: {', '.join(route)}" if route else "") + f"; attempt {attempt} of 2"
            if attempt == 1 and retry_wait:
                time.sleep(retry_wait)
    return None, last


def report_lines(slug, key, report):
    return [f"stage: {slug}: {key} {r['section']}: {r['result']}: {r['detail']}" for r in report]


def probe_score(out):
    text = str(out.get("text") or "") if isinstance(out, dict) else ""
    needs = out.get("needs_input") if isinstance(out, dict) else None
    return round(0.25 * (len(text) >= 200) + 0.25 * isinstance(needs, list) + 0.25 * (bool(text.strip()) and "award" not in text.lower() and "press" not in text.lower()) + 0.25 * ("probe brand" in text.lower()), 3)


def probes():
    """{lane: [(system, user, score_fn)]}: one probe per stage lane, with the stage's own kind of task on a tiny made-up brand whose right answer is known (it must name the brand, stay inside the
    facts, keep the blocked claims out and list what it lacks)."""
    base = ("=== BRAND FACTS ===\nBrand name: Probe Brand. Soft overalls for adults, cut from organic cotton, sold online, founded in 2017 in Portland.\n\n=== AUDIENCE VOICE / COLLECTED ===\n"
            "1. [comments] 'The pockets are deep enough for my phone and keys, I love that.'\n2. [comments] 'Washed them ten times and the cotton still feels soft.'\n3. [competitor ad] 'Workwear that moves with you.'\n\n"
            "=== CLAIMS LEDGER ===\nDO NOT STATE (the ledger blocks or disputes these):\n- Won a national design award (blocked)\n- Featured in a national magazine (blocked)\n")
    return {s["lane"]: [(SYSTEM, f"TASK: {s['sections'][0][2]}\n\n{base}", probe_score)] for s in STAGES.values()}
