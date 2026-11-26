"""Briefs: a creative brief built only from the Idea Bank rows a person marked Use.

    * The catalogue gate must pass (the same check that guards everything else): no brief on a catalogue nobody has confirmed.
    * Only rows whose Decision is Use are used (Maybe too, if asked). The brief reads the pictures saved in Drive; nothing is searched again.
    * Claude writes the brief as structured JSON, using only the approved items, the brand's real products and the brief's own rules.
    * The exact rules in briefs/<name>/rules.yaml (banned characters and words, word order, product eligibility) are checked by code on
      every hook and line of copy and on every product. Violations are sent back to Claude for ONE repair pass; whatever is still
      wrong is listed in the brief under "Needs review", never hidden. Judgement rules (tone, visuals) are handed to Claude as guidance
      and listed for a person, because code cannot enforce them.
    * With more than one model in brief.models the brief is written by each, judged, and the best one kept (the others are noted).
    * The result is a Word file (with the saved pictures and links) kept in the brand's Briefs folder in Drive, and a row in the Briefs tab.
"""
import io
import json

from . import claimcheck
from . import connections as CONN
from . import motion_prompts as MOTION
from . import openverse as OPENVERSE
from . import pace as PACE
from . import videometa as VMETA
from . import voice as VOICE
from . import formulas as FORMULAS
from . import story_refresh as SR
from . import validators as VALIDATORS
from . import rules as rules_mod
from .drive import DOCX, DriveError, file_url, safe_name
from .export import price_range
from .ideabank import normalise_decision, state_of
from .providers import ProviderError
from .sync import catalogue_state


class BriefError(Exception):
    pass


BRIEF_SYSTEM = (
    "You write creative briefs for a marketing agency, for creators and editors who will make the content. Write one brief for the "
    "request below, using ONLY the approved reference ideas given, the brand's real products given, and the rules given. Reply with "
    "JSON only: {\"title\": \"...\", \"objective\": \"...\", \"audience\": \"...\", \"key_message\": \"...\", \"tone\": \"...\", "
    "\"concepts\": [{\"name\": \"...\", \"format\": \"for example 30 second vertical video\", \"hook\": \"the first line or the first "
    "2 seconds\", \"script\": \"what is said or shown, step by step\", \"visual_direction\": \"...\", \"cta\": \"...\", \"product\": "
    "\"the product handle from the list, or empty\", \"references\": [\"IB-0001\"]}], \"dos\": [\"...\"], \"donts\": [\"...\"], "
    "\"deliverables\": [\"...\"], \"notes_for_creators\": \"...\"}. Each concept must be inspired by at least one reference idea and "
    "list it in references; write the brand's own version, never a copy of someone else's content. Obey every rule exactly, including "
    "the banned characters and words, in every hook, script, visual direction and cta. Use only product handles from the list. Do not "
    "invent prices, offers, claims, results or quotes. Write the script as plain text with one spoken or shown line per line (never a JSON list), "
    "and write natural speech with normal apostrophes in contractions (can't, doesn't, dad's).")

REVISE_ADDENDUM = (" You are REVISING a brief that was checked and sent back. Fix every listed problem in the concepts it names, keep what was not criticised word for word, "
                   "never restate a blocked claim (replace it with something approved), and return the full revised brief in the same JSON shape.")

REPAIR_SYSTEM = (
    "You fix a creative brief that broke some of its rules. Return the full corrected brief as JSON in exactly the same shape, changing "
    "only what is needed to fix each listed problem, and keeping everything else word for word. Obey every rule exactly.")

JUDGE_SYSTEM = (
    "You judge several versions of the same creative brief. Score each from 1 to 10 for how usable it is for the creators (clear hooks "
    "and scripts), how well it uses the approved reference ideas, how well it fits the brand and the request, and how original it is. "
    "Reply with JSON only: {\"scores\": {\"<model name>\": number}, \"reason\": \"one sentence\"}.")

_CONCEPT_TEXT = ("hook", "script", "visual_direction", "cta")


def approved_items(bank, decisions, include_maybe=False, request_id=""):
    """The Idea Bank items a person approved, oldest first. Items with no decision are never used."""
    wanted = {"use", "maybe"} if include_maybe else {"use"}
    out = []
    for idea_id, item in sorted(bank["items"].items()):
        decision = normalise_decision((decisions.get(idea_id) or {}).get("decision"))
        if decision in wanted and state_of(item) != "removed" and (not request_id or item.get("request_id") == request_id):
            out.append(item)
    return out


def product_table(store, slug, ruleset):
    """{handle: record} for the products a brief may use: in the catalogue, and (if the rules name an approved list) on that list."""
    latest = store.load_latest(slug) or {}
    records = {p["handle"]: p for p in (latest.get("products") or {}).values() if isinstance(p, dict) and p.get("handle")}
    approved = set((ruleset.rules["product_rules"] or {}).get("approved_products") or [])
    return {h: r for h, r in records.items() if not approved or h in approved}


def _rules_text(ruleset):
    text = ruleset.rules["text_rules"] or {}
    lines = []
    for item in text.get("banned_chars") or []:
        lines.append(f"Never use {item.get('name', item['char'])} ({item['char']!r}).")
    for item in text.get("banned_phrases") or []:
        lines.append(f"Never use the word '{item['phrase']}'.")
    for item in text.get("ordered_terms") or []:
        where = f" in {', '.join(item['scopes'])}" if item.get("scopes") else ""
        lines.append(f"'{item['first']}' must come before '{item['then']}'{where}, whenever both appear.")
    lines += [f"Guidance: {r}" for r in ruleset.judgement_rules]
    return "\n".join(f"- {line}" for line in lines) or "- (no extra rules)"


def _item_text(item):
    measured = "; ".join(x for x in (VMETA.line(item.get("video_meta")), PACE.describe(item.get("pace"))) if x)
    return (f'{item["id"]} ({item["kind"]}, {item["platform"]}) "{item["title"]}": {item["summary"] or item["text"][:200]} '
            f'How we could use it: {item["how_to_use"] or "not scored"}. Risk: {item["risk"] or "none noted"}.' + (f" Measured: {measured}." if measured else ""))


def extras_text(store, brand, items, *, motion=None, assets=None):
    """Real evidence for the concepts, kept apart from the facts the brief may state: what audiences say (with engagement), how the winning videos move (measured), motion formats other creators used (with their
    authors) and licence-safe pictures to point the creative team to. '' when there is none of it, so a brief for a brand without any of it is written exactly as before."""
    parts = []
    try:
        voice = store.read_extra(brand.slug, "voice")
    except Exception:                                                          # noqa: BLE001 - extra evidence never stops a brief
        voice = None
    lines = VOICE.prompt_lines(voice, 8) if voice else ""
    if lines:
        parts.append("What audiences are saying right now (real posts and their engagement; use the language and the worries, quote nobody by name, state no number from them):\n" + lines)
    rule = PACE.rule_from([i.get("pace") for i in items])
    if rule:
        parts.append("How the reference videos move (measured with ffmpeg): " + rule)
    shown = []
    for entry in (motion or [])[:5]:
        shown.append("- " + MOTION.line(entry))
    if shown:
        parts.append("Motion formats other creators have used (for the look and movement only; never copy; credit if used):\n" + "\n".join(shown))
    got = [a for a in (assets or [])[:6] if a.get("url")]
    if got:
        parts.append("Licence-safe references the team may use (credit line shown):\n" + "\n".join(f"- {a['title']} [{a['license'].upper()}] {a['page'] or a['url']} | credit: {OPENVERSE.credit_line(a)}"[:260] for a in got))
    return "\n\n".join(parts)


def facts_line(handle, record):
    """What the catalogue states about one product, in a line a model can stay within."""
    ev = claimcheck.Evidence().absorb(record)
    bits = []
    if ev.materials:
        bits.append("materials " + ", ".join(f"{p}% {f}" for f, p in sorted(ev.materials, key=lambda x: -x[1])))
    if ev.pockets:
        bits.append("pockets " + ", ".join(str(c) for c in sorted(ev.pockets)))
    facts = record.get("facts") or {}
    stated = [name for name in ("ingredients", "allergens") if name in facts]
    if stated:
        bits.append("the description states its " + " and ".join(stated))
    return f"- {handle}: " + ("; ".join(bits) if bits else "no materials, counts or nutrition are stated")


def claim_lines(brand, ruleset, products):
    """The 'what you may state' block of the prompt: product facts, allowed offers and approved claims."""
    rules = ruleset.rules.get("claim_rules") or {}
    offers = rules.get("allowed_offers") or []
    approved = [c["text"] for c in brand.claims if isinstance(c, dict) and c.get("text") and str(c.get("status", "")).lower() in ("licensed", "approved", "active")]
    lines = ["Facts you may state (state NO other fact, price, number, offer, ingredient, dietary or health claim, endorsement or result):"]
    lines += [facts_line(h, r) for h, r in sorted(products.items())] or ["- (none)"]
    lines.append("Allowed offers: " + ("; ".join(offers) if offers else "none: state no offer, discount, code, free shipping or guarantee"))
    lines.append("Approved claims you may use word for word: " + ("; ".join(approved) if approved else "none"))
    if rules.get("allowed_claims"):
        lines.append("Other allowed wording: " + "; ".join(rules["allowed_claims"]))
    return "\n".join(lines)


def build_prompt(brand, ask, items, products, ruleset, max_concepts, extras=""):
    def price(record):
        low, high = price_range(record)
        return "" if low is None else f" (${low:g})" if low == high else f" (${low:g}-${high:g})"
    product_lines = "\n".join(f'- {h}: {r.get("title", h)}{price(r)}' for h, r in sorted(products.items())) or "- (none)"
    return (f"Brand: {brand.name} ({brand.domain}).\nRequest: {ask}\nWrite between 3 and {max_concepts} concepts, about one for each reference idea.\n\n"
            f"Approved reference ideas:\n" + "\n".join(_item_text(i) for i in items) + f"\n\nProducts you may use:\n{product_lines}\n\n"
            f"{claim_lines(brand, ruleset, products)}\n\nRules for this brief:\n{_rules_text(ruleset)}" + (f"\n\nEvidence for the concepts (not facts about our products):\n{extras}" if extras else ""))


def clean_brief(raw, item_ids, handles, max_concepts):
    """A brief the rest of the code can rely on, and notes on what was fixed or dropped. Raises BriefError if it has no concepts."""
    if not isinstance(raw, dict):
        raise BriefError("the brief was not in the expected shape")
    notes, concepts = [], []

    def text(value, limit=1200, lines=False):
        if isinstance(value, (list, tuple)):                                   # a model may give the script as a list of lines: it stays lines (BR-0001 showed "['a', 'b']")
            parts = [" ".join(str(v).split()) for v in value if str(v or "").strip()]
            return ("\n".join(parts) if lines else " ".join(parts))[:limit]
        if lines:
            return "\n".join(" ".join(row.split()) for row in str(value or "").splitlines() if row.strip())[:limit]
        return " ".join(str(value or "").split())[:limit]

    for entry in raw.get("concepts") if isinstance(raw.get("concepts"), list) else []:
        if not isinstance(entry, dict) or not text(entry.get("hook")):
            continue
        refs = [r for r in (entry.get("references") or []) if isinstance(r, str)]
        unknown = [r for r in refs if r not in item_ids]
        refs = [r for r in dict.fromkeys(refs) if r in item_ids]
        if unknown:
            notes.append(f"concept '{text(entry.get('name'), 60)}': dropped reference(s) to unapproved or unknown idea(s) {', '.join(unknown)}")
        product = text(entry.get("product"), 120)
        if product and product not in handles:
            notes.append(f"concept '{text(entry.get('name'), 60)}': product '{product}' is not an allowed product and was removed")
            product = ""
        concepts.append({"name": text(entry.get("name"), 80) or f"Concept {len(concepts) + 1}", "format": text(entry.get("format"), 120),
                         "hook": text(entry.get("hook"), 400), "script": text(entry.get("script"), 1500, lines=True),
                         "visual_direction": text(entry.get("visual_direction"), 800), "cta": text(entry.get("cta"), 200),
                         "product": product, "references": refs})
    if not concepts:
        raise BriefError("the brief has no usable concepts")
    if len(concepts) > max_concepts:
        notes.append(f"kept the first {max_concepts} of {len(concepts)} concepts")
        concepts = concepts[:max_concepts]
    brief = {"title": text(raw.get("title"), 160) or "Creative brief", "objective": text(raw.get("objective")),
             "audience": text(raw.get("audience")), "key_message": text(raw.get("key_message")), "tone": text(raw.get("tone")),
             "concepts": concepts, "dos": [text(d, 240) for d in raw.get("dos") or [] if text(d)][:10],
             "donts": [text(d, 240) for d in raw.get("donts") or [] if text(d)][:10],
             "deliverables": [text(d, 240) for d in raw.get("deliverables") or [] if text(d)][:10],
             "notes_for_creators": text(raw.get("notes_for_creators"), 1500)}
    return brief, notes


def check_brief(brief, ruleset, products, claims=()):
    """Every rule broken by the brief, as [{where, rule, severity, message}]. Exact rules only: this is code, not opinion."""
    found = []

    def add(where, violations):
        for v in violations:
            found.append({"where": where, "rule": v.rule, "severity": v.severity, "message": v.message})

    for field in ("title", "objective", "audience", "key_message", "tone", "notes_for_creators"):
        add(field, rules_mod.check_text(ruleset, brief[field], scope="copy"))
    for n, concept in enumerate(brief["concepts"], start=1):
        label = f"concept {n} ({concept['name']})"
        add(f"{label} name", rules_mod.check_text(ruleset, concept["name"], scope="copy"))
        add(f"{label} hook", rules_mod.check_text(ruleset, concept["hook"], scope="hook"))
        for field in ("script", "visual_direction", "cta"):
            add(f"{label} {field.replace('_', ' ')}", rules_mod.check_text(ruleset, concept[field], scope="copy"))
        if concept["product"]:
            record = products.get(concept["product"])
            if record is None:
                found.append({"where": label, "rule": "unknown_product", "severity": "error",
                              "message": f"product '{concept['product']}' is not in the catalogue"})
            else:
                add(f"{label} product", rules_mod.check_product(ruleset, record))
    for field in ("dos", "donts", "deliverables"):
        for line in brief[field]:
            add(field, rules_mod.check_text(ruleset, line, scope="copy"))
    # what a creator will say or show must be true: every claim backed by the catalogue, the register or this brief's allowed list
    claim_rules = ruleset.rules.get("claim_rules") or {}
    everything = claimcheck.evidence_for(products.values())
    for field in ("title", "key_message"):
        add(field, claimcheck.check_text(brief[field], everything, claims, claim_rules))
    for n, concept in enumerate(brief["concepts"], start=1):
        label = f"concept {n} ({concept['name']})"
        record = products.get(concept["product"]) if concept["product"] else None
        evidence = claimcheck.evidence_for([record]) if record else everything
        for field in ("name", "hook", "script", "visual_direction", "cta"):
            add(f"{label} {field.replace('_', ' ')}", claimcheck.check_text(concept[field], evidence, claims, claim_rules))
    return found


def errors_of(violations):
    return [v for v in violations if v["severity"] == "error"]


def _violation_lines(violations):
    return "\n".join(f"- {v['where']}: {v['message']}" for v in violations)


def write_brief(claude, brand, ask, items, products, ruleset, settings, budget, extras="", revise=None):
    """One model's brief: write, check against the rules, repair once. Returns (brief, violations, notes, calls used).
    The writer is the ranked list for this job (lane brief_json: best measured first, backups behind it, Claude last); `revise` is {'brief': the previous brief, 'problems': [...]}: the brief is then revised, not written afresh."""
    claude = SR.for_job(claude, "brief")
    ids, handles = {i["id"] for i in items}, set(products)
    calls, notes = 0, []
    if not budget.take():
        raise BriefError(f"the monthly Claude limit ({budget.cap} calls) is reached")
    calls += 1
    try:
        prompt = build_prompt(brand, ask, items, products, ruleset, settings.brief_max_concepts, extras)
        if revise:
            prompt += ("\n\nThe brief to revise:\n" + json.dumps(revise["brief"], ensure_ascii=False)[:9000] + "\n\nProblems found by the checks and the independent reviewer (fix each):\n"
                       + "\n".join(f"- {p}" for p in revise["problems"][:40]))
        raw = claude.json_task(BRIEF_SYSTEM + (REVISE_ADDENDUM if revise else ""), prompt, max_tokens=6000)
    except ProviderError as exc:
        raise BriefError(f"Claude could not write the brief: {exc}") from exc
    brief, fixes = clean_brief(raw, ids, handles, settings.brief_max_concepts)
    notes += fixes
    violations = check_brief(brief, ruleset, products, brand.claims)
    if errors_of(violations):
        if calls < settings.brief_max_calls and budget.take():
            calls += 1
            try:
                raw = claude.json_task(REPAIR_SYSTEM, f"{claim_lines(brand, ruleset, products)}\n\nRules:\n{_rules_text(ruleset)}\n\nProblems to fix:\n{_violation_lines(errors_of(violations))}"
                                                      f"\n\nBrief:\n{json.dumps(brief, ensure_ascii=False)}", max_tokens=6000)
                fixed, more = clean_brief(raw, ids, handles, settings.brief_max_concepts)
                again = check_brief(fixed, ruleset, products, brand.claims)
                if len(errors_of(again)) <= len(errors_of(violations)):
                    brief, violations = fixed, again
                    notes += more
                    notes.append(f"one repair pass: {len(errors_of(violations))} rule problem(s) left")
                else:
                    notes.append("the repair made the brief worse and was not used")
            except (ProviderError, BriefError) as exc:
                notes.append(f"the repair pass failed ({exc}); the first version is kept")
        else:
            notes.append("no Claude call was left for a repair pass")
    return brief, violations, notes, calls


def pick_best(candidates, judge, budget, models):
    """The best of several briefs. candidates: [{model, brief, violations, ...}]. Returns (index of winner, notes)."""
    if len(candidates) == 1:
        return 0, []
    notes = []
    scores = {}
    if judge is not None and budget.take():
        body = "\n\n".join(f"Model {c['model']}:\n{json.dumps(c['brief'], ensure_ascii=False)[:6000]}" for c in candidates)
        try:
            raw = judge.json_task(JUDGE_SYSTEM, body, max_tokens=500)
            for model, score in (raw.get("scores") or {}).items() if isinstance(raw, dict) else []:
                if model in models and isinstance(score, (int, float)) and not isinstance(score, bool) and 1 <= score <= 10:
                    scores[model] = float(score)
            if raw.get("reason") if isinstance(raw, dict) else None:
                notes.append(f"judge: {' '.join(str(raw['reason']).split())[:200]}")
        except ProviderError as exc:
            notes.append(f"the judge failed ({exc}); versions were ranked by rule problems alone")
    else:
        notes.append("no judge call was possible; versions were ranked by rule problems alone")
    def total(c):
        return scores.get(c["model"], 5.0) - 1.5 * len(errors_of(c["violations"]))
    best = max(range(len(candidates)), key=lambda i: (total(candidates[i]), -i))
    notes.append("bake-off: " + ", ".join(f"{c['model']} {total(c):.1f}" for c in candidates) + f"; kept {candidates[best]['model']}")
    return best, notes


# ---------------------------------------------------------------------------------------------------------------------
# the Word file
# ---------------------------------------------------------------------------------------------------------------------

def build_docx(brief, brand, meta, items, images, violations, ruleset, notes):
    """The brief as a Word file (bytes). `images` maps an idea id to the picture bytes saved in Drive, if any."""
    from docx import Document
    from docx.shared import Inches
    doc = Document()
    doc.add_heading(brief["title"], 0)
    doc.add_paragraph(f"{brand.name}  |  {meta['brief']}  |  {meta['id']}  |  {meta['created'][:10]}")
    doc.add_paragraph(f"Request: {meta['ask']}")
    for label, key in (("Objective", "objective"), ("Audience", "audience"), ("Key message", "key_message"), ("Tone", "tone")):
        if brief[key]:
            doc.add_heading(label, 1)
            doc.add_paragraph(brief[key])
    by_id = {i["id"]: i for i in items}
    doc.add_heading("Concepts", 1)
    for n, c in enumerate(brief["concepts"], start=1):
        doc.add_heading(f"{n}. {c['name']}", 2)
        table = doc.add_table(rows=0, cols=2)
        table.style = "Light Grid Accent 1"
        for label, value in (("Format", c["format"]), ("Hook", c["hook"]), ("Script", c["script"]), ("Visual direction", c["visual_direction"]),
                             ("Call to action", c["cta"]), ("Product", c["product"]), ("Inspired by", ", ".join(c["references"]))):
            if value:
                row = table.add_row().cells
                row[0].text, row[1].text = label, value
    if brief["dos"] or brief["donts"]:
        doc.add_heading("Do and don't", 1)
        for label, key in (("Do", "dos"), ("Don't", "donts")):
            for line in brief[key]:
                doc.add_paragraph(f"{label}: {line}", style="List Bullet")
    if brief["deliverables"]:
        doc.add_heading("Deliverables", 1)
        for line in brief["deliverables"]:
            doc.add_paragraph(line, style="List Bullet")
    if brief["notes_for_creators"]:
        doc.add_heading("Notes for creators", 1)
        doc.add_paragraph(brief["notes_for_creators"])
    used = [r for c in brief["concepts"] for r in c["references"]]
    shown = [by_id[i] for i in dict.fromkeys(used) if i in by_id]
    if shown:
        doc.add_heading("Reference gallery", 1)
        doc.add_paragraph("Examples to look at. The link opens the original post; the folder holds the saved copy.")
        for item in shown:
            doc.add_heading(f"{item['id']}: {item['topic'] or item['title'][:60]}", 2)
            if images.get(item["id"]):
                try:
                    doc.add_picture(io.BytesIO(images[item["id"]]), width=Inches(2.4))
                except Exception:                       # an image Word cannot read must not lose the brief
                    doc.add_paragraph("(the saved picture could not be placed in this file; open the folder link)")
            doc.add_paragraph(f"{item['kind'].capitalize()} on {item['platform']} by {item['handle'] or 'unknown'}")
            if item["summary"]:
                doc.add_paragraph(f"What it is: {item['summary']}")
            if item["how_to_use"]:
                doc.add_paragraph(f"How to use it: {item['how_to_use']}")
            doc.add_paragraph(f"Original: {item['url'] or 'no link'}")
            if item["drive_folder_url"]:
                doc.add_paragraph(f"Saved files: {item['drive_folder_url']}")
    review = [f"[{v['severity']}] {v['where']}: {v['message']}" for v in violations] + [f"Open question: {q}" for q in ruleset.open_questions]
    review += [f"For a person to check: {r}" for r in ruleset.judgement_rules] + [f"Note: {n}" for n in notes]
    if review:
        doc.add_heading("Needs review", 1)
        for line in review:
            doc.add_paragraph(line, style="List Bullet")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------------------------------
# one brief, start to finish
# ---------------------------------------------------------------------------------------------------------------------

def saved_pictures(items, drive, per_item):
    """{idea id: picture bytes} downloaded from the item's Drive folder, and notes on any that could not be."""
    images, notes = {}, []
    if drive is None or per_item < 1:
        return images, notes
    for item in items:
        pictures = [a for a in item["assets"] if a["kind"] == "image"][:per_item]
        for asset in pictures:
            try:
                images[item["id"]] = drive.download(asset["drive_id"])
                break
            except DriveError as exc:
                notes.append(f"{item['id']}: the saved picture could not be read from Drive ({exc})")
    return images, notes


def _motion_for(ask, items):
    """Motion formats close to the ask and the approved ideas (from the prompt library: the checkout, else its snapshot). [] when none is close or the library cannot be read."""
    try:
        entries, _ = MOTION.read(CONN.external_dir() / "opus_video_prompts")
        return MOTION.search(entries, " ".join([str(ask)] + [f"{i.get('mechanism', '')} {i.get('concept', '')} {i.get('visual_hook', '')}" for i in items]), 5)
    except Exception:                                                          # noqa: BLE001
        return []


def _assets_of(store, brand):
    try:
        data = store.read_extra(brand.slug, "assets")
    except Exception:                                                          # noqa: BLE001
        return []
    return list((data or {}).get("assets") or [])


def canon_layer(store, slug):
    """Stage 3: a brief is written ONLY from approved canon and read against the claims ledger. Returns (registry or None, prompt block, notes). A brand with no canon and no claims yet has nothing to read
    against: the brief goes ahead with a note. A brand whose canon has proposals but none approved is refused with the fix: the owner approves the foundation first."""
    from . import canon as canon_mod
    reg = canon_mod.Registry(store.data_dir, slug)
    state = reg.state()
    if not state["canon"] and not state["claims"]:
        return None, "", ["no narrative canon and no claims ledger exist for this brand yet: the brief was NOT read against approved canon"]
    approved = [e for e in state["canon"].values() if e["status"] == "approved"]
    if state["canon"] and not approved:
        raise BriefError("this brand's narrative canon has proposals but none is approved: approve the foundation sections in the Canon tab (Decision = approve), run the sheet update, then this again")
    claims = list(state["claims"].values())
    stop = [f"- {c['text'][:160]} ({c['status']})" for c in claims if c["status"] in ("blocked", "conflicted")]
    ok = [f"- {c['text'][:160]}" for c in claims if c["status"] == "verified"]
    block = ("APPROVED CANON (the brand's narrative, approved by the owner; stay inside it):\n" + "\n\n".join(f"[{e['section']}] {e['text'][:1500]}" for e in approved)[:6000]
             + "\n\nDO NOT STATE (the claims ledger blocks or disputes these):\n" + ("\n".join(stop) or "- (none)") + "\n\nMAY STATE AS FACT (verified):\n" + ("\n".join(ok) or "- (none yet)")) if approved or claims else ""
    return reg, block, []


def gate_violations(brief, reg):
    """The claims gate over every text of the brief: a BLOCKED sentence is an error (the brief needs review), a checkable statement nothing verifies is a warning. Returns [{where, rule, severity, message}]."""
    fields = [(f, brief.get(f, "")) for f in ("title", "objective", "audience", "key_message", "tone", "notes_for_creators")]
    for n, concept in enumerate(brief.get("concepts", []), start=1):
        fields += [(f"concept {n} ({concept.get('name', '')}) {f}", concept.get(f, "")) for f in ("name", "hook", "script", "visual_direction", "cta")]
    found = []
    for where, text in fields:
        if not str(text or "").strip():
            continue
        for f in reg.gate(str(text))["findings"]:
            found.append({"where": where, "rule": "claims-gate", "severity": "error" if f["result"] == "BLOCKED" else "warning", "message": f"{f['result']}: \"{f['sentence'][:120]}\": {f['why']}"})
    return found


def revision_problems(record):
    """The problems a revision must fix, as plain lines: the rule and claim violations, the validator bank's findings and what the independent reviewer said."""
    out = [f"{v['where']}: {v['message']}" + (" (BLOCKED claim: remove it, never restate it)" if v.get("rule") == "claims-gate" or "BLOCKED" in v["message"] else "") for v in record.get("violations", [])]
    out += [f"{f['where']}: {f['message']}" for f in record.get("validator_findings", []) if f["severity"] == "error"]
    out += [f"{p['where']}: {p['issue']} (independent reviewer)" for p in (record.get("review") or {}).get("problems", [])]
    out += [f"{f['where']}: {f['message']} (warning)" for f in record.get("validator_findings", []) if f["severity"] != "error"][:12]
    return out


def make_brief(brand, store, *, brief_name, ask, ruleset, decisions, claude_for, settings, budget, now, drive=None,
               include_maybe=False, request_id="", revise_of=None):
    """Write, check and save one brief from the approved items. Returns the saved record. Raises BriefError with the fix if it cannot."""
    state = catalogue_state(brand.slug, store, now)
    if not state["usable"]:
        raise BriefError("the catalogue cannot be used right now: " + " ".join(state["reasons"]))
    if not " ".join(str(ask or "").split()):
        raise BriefError("say what the brief is for (the ask)")
    bank = store.read_ideabank(brand.slug)
    items = approved_items(bank, decisions, include_maybe, request_id)
    if not items:
        raise BriefError("no Idea Bank item is approved: mark rows Use in the Decision column of the Idea Bank tab"
                         + (f" (for request {request_id})" if request_id else "") + ", then run the sheet update, then this again")
    products = product_table(store, brand.slug, ruleset)
    if not products:
        raise BriefError("no product in the catalogue is allowed by this brief's rules")
    canon_reg, canon_block, canon_notes = canon_layer(store, brand.slug)
    candidates, notes, calls = [], [], 0
    extras = extras_text(store, brand, items, motion=_motion_for(ask, items), assets=_assets_of(store, brand)) + (("\n\n" + canon_block) if canon_block else "")
    revise = {"brief": revise_of["content"], "problems": revision_problems(revise_of)} if revise_of else None
    for model in settings.brief_models:
        client = claude_for(model)
        seen_calls, seen_notes = len(getattr(client, "calls", [])), len(getattr(client, "notes", []))
        try:
            brief, violations, model_notes, used = write_brief(client, brand, ask, items, products, ruleset, settings, budget, extras, revise=revise)
        except BriefError as exc:
            notes.append(f"{model}: {exc}")
            continue
        route = list(getattr(client, "calls", []))[seen_calls:]
        built_by = ("the pool (lane brief_json)" if route and set(route) == {"gateway"} else "Claude (the pool had no usable answer)" if route and "gateway" not in route else "the pool, then Claude" if route else model)
        model_notes = [f"writer: {built_by}"] + [n for n in list(getattr(client, "notes", []))[seen_notes:]] + model_notes          # why the pool did not write it is in the brief's notes, not only in the log
        calls += used
        candidates.append({"model": model, "brief": brief, "violations": violations, "notes": model_notes, "built_by": built_by})
    if not candidates:
        raise BriefError("no model produced a usable brief: " + "; ".join(notes))
    best, judged = pick_best(candidates, claude_for(settings.brief_models[0]) if len(candidates) > 1 else None, budget,
                             set(settings.brief_models))
    won = candidates[best]
    if canon_reg is not None:
        won["violations"] = list(won["violations"]) + gate_violations(won["brief"], canon_reg)                 # every claim in the brief goes through the gate
    notes = list(canon_notes) + notes + won["notes"] + judged                                           # the canon note is part of a brief that was written, never of the reason none was
    library = FORMULAS.load_library(CONN.external_dir())                                                    # Stage 5: the validator bank (exact checks) and the independent reviewer, on the brief that won
    bank_findings = VALIDATORS.brief_bank(won["brief"], library)
    if bank_findings:
        stats = VALIDATORS.summarise(bank_findings)
        notes.append(f"validator bank: {stats['errors']} error(s) and {stats['warnings']} warning(s) ({', '.join(stats['rules'])})")
    review = VALIDATORS.review(won["brief"], bank_findings, SR.for_job(claude_for(settings.brief_models[0]), "brief_review"), rules_note=_rules_text(ruleset))
    notes.append(f"independent review: {review['verdict']}" + ("".join(f"; {p['where']}: {p['issue']}" for p in review["problems"][:4]) if review["problems"] else "") + (f" ({review['why']})" if review.get("why") else ""))
    rules_version, rules_before = VALIDATORS.note_rules_version(store, brand.slug, ruleset.digest, now)
    if rules_before:
        notes.append(f"the brief rules changed since the last brief: version {rules_before} -> {rules_version}")
    shown = [i for i in items if i["id"] in {r for c in won["brief"]["concepts"] for r in c["references"]}]
    images, picture_notes = saved_pictures(shown, drive, settings.brief_images_per_item)
    notes += picture_notes
    record = {"id": store.next_brief_id(brand.slug), "brief": brief_name, "ask": " ".join(ask.split()), "created": now,
              "status": "needs review" if (errors_of(won["violations"]) or errors_of(bank_findings) or review["verdict"] == "revise") else "ready", "items": [i["id"] for i in items],
              "used_items": [i["id"] for i in shown], "violations": won["violations"], "models": [won["model"]]
              + [c["model"] for c in candidates if c is not won], "notes": notes, "drive_link": "", "title": won["brief"]["title"],
              "request_id": request_id, "rules_digest": ruleset.digest, "rules_version": rules_version, "review": review, "validator_findings": bank_findings, "content": won["brief"],
              "built_by": won.get("built_by", won["model"]), "revision": ((revise_of or {}).get("revision") or 0) + 1 if revise_of else 0, "revision_of": (revise_of or {}).get("id", "")}
    try:
        store.write_extra(brand.slug, "last_brief", {"id": record["id"], "title": record["title"], "status": record["status"], "created": now, "violations": record["violations"], "brief": won["brief"]})     # the storyboard stage and the draft-video step read the latest brief from here
    except OSError:
        pass
    meta = {"id": record["id"], "brief": brief_name, "ask": record["ask"], "created": now}
    docx = build_docx(won["brief"], brand, meta, items, images, list(won["violations"]) + bank_findings, ruleset, notes)
    if drive is not None:
        try:
            folder = drive.briefs_folder(drive.brand_folder(brand.name))
            file_id = drive.upload(folder, f"{record['id']} - {safe_name(record['title'], 60)}.docx", docx, DOCX)
            record["drive_link"] = file_url(file_id)
        except DriveError as exc:
            record["notes"].append(f"the file was not saved in Drive ({exc}); the copy in the repository is the only one")
    store.write_brief(brand.slug, record, docx)
    return record
