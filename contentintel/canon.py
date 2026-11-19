"""The two registries every downstream skill reads: the NARRATIVE CANON (one per brand) and the OFFER-CLAIMS LEDGER (one per brand).

Design (approved): Sheets tabs + the git log. This module is the part that is independent of Sheets: an APPEND-ONLY event log per brand
(data/<brand>/registry/events.ndjson, committed with the rest of data/, so git keeps the history) and the state replayed from it. The Canon and Claims tabs of a brand's sheet are a
projection of that state (`canon_rows`, `claim_rows`); the owner's Decision column in those tabs is read back (`owner_decisions`).

Two rules the code enforces, not just documents:
  * Nothing is canonical until the OWNER approves it. The tool can only PROPOSE canon (a skill's output); `decide_canon` and `set_claim_status(..., "verified")` raise for any other actor.
  * A claim the ledger blocks is never repeated: `gate` checks a piece of text against the ledger and says OK, NEEDS_REVIEW (a sensitive statement nothing verifies) or BLOCKED.
"""
import hashlib
import json
import re
from pathlib import Path

CANON_STATUSES = ("proposed", "approved", "rejected", "superseded")
CLAIM_STATUSES = ("verified", "unverified", "blocked", "conflicted")
CATEGORIES = ("award", "press", "ranking", "health", "figure", "customer", "origin", "other")
OWNER, TOOL = "owner", "tool"
MEDIA = ("page", "press", "social post", "interview", "podcast", "video", "newsletter", "community post", "other")      # what a source was published as: recorded for the owner, never a reason to count it or not

# What makes a sentence sensitive: it says something a reader would take as a fact about the brand that can be checked.
SENSITIVE = {
    "award": r"\baward|\bwinner\b|\bwinning\b|\bwon\b|\bfinalist|\bbest of\b|\bmost innovative",
    "press": r"\bas seen on\b|\bfeatured (in|on)\b|\bshark tank\b|\bgood morning america\b|\bgma\b|\bforbes\b|\bnewsweek\b|\bgq\b|\bcnbc\b|\bfox news\b",
    "ranking": r"#\s?1\b|\bnumber one\b|\btop[- ]rated\b|\bbest[- ]selling\b|\bbest[- ]seller\b|\brank(ed|ing)\b",
    "health": r"\bclinically\b|\bproven\b|\bcures?\b|\bheals?\b|\bimmun|\bweight loss\b|\bketo\b|\bsugar[- ]free\b|\bgluten[- ]free\b|\bhigh[- ]protein\b|\bgrams? of protein\b",
    "figure": r"\$\s?\d|\b\d[\d,.]*\s?(million|billion|k)\b|\brevenue\b|\bvaluation\b|\braised\b",
    "customer": r"\b\d[\d,]*\s+(five[- ]star|5[- ]star|reviews|customers)\b|\bloved by\b|\bthousands of\b",
}
_STOP = set("a an the of and or to in on for with is are was were be by it its this that at as from our we you your".split())


# A line containing one of these never enters a story-bank file: a customer's own medical condition (the plan said EXCLUDE it; the review stays on the brand's site, not in our material).
EXCLUDE_MARKERS = ("CRPS & nerve damage",)
# ... and any line where a customer discloses their OWN medical condition (first person + a condition), so a differently worded disclosure in a future refresh is kept out as well.
MEDICAL_DISCLOSURE = re.compile(r"\b(I|I've|my)\b[^.\n]{0,80}\b(CRPS|nerve damage|cancer|arthritis|diabet\w*|disabilit\w*|disabled|chronic (pain|illness|fatigue|condition)|fibromyalgia|multiple sclerosis|epilep\w*|"
                                r"depress\w*|anxiety disorder|PTSD|autis\w*|ADHD|diagnos\w*|chemo\w*|surgery|surgeries|amputat\w*|wheelchair|lupus|crohn\w*|migraine\w*)", re.I)


CONDITIONS = (r"CRPS|nerve damage|cancer|arthritis|diabet\w*|disabilit\w*|disabled|chronic (pain|illness|fatigue|condition)|fibromyalgia|multiple sclerosis|epilep\w*|depress\w*|anxiety disorder|PTSD|autis\w*|ADHD|"
              r"diagnos\w*|chemo\w*|surgery|surgeries|amputat\w*|wheelchair|lupus|crohn\w*|migraine\w*|cerebral palsy|down syndrome|dementia|alzheimer\w*|parkinson\w*|stroke|ostomy|colostomy|hernia|dyslexi\w*|"
              r"eating disorder|bipolar|schizophreni\w*|OCD|sensory (processing|issues)|incontinen\w*|mastectomy|dialysis|transplant|asthma\w*|COPD|scoliosis|psoriasis|osteoporosis|endometriosis|insomnia|chronic \w+|"
              r"panic attacks?|hearing loss|vision loss|blind(ness)?|deaf(ness)?|celiac|crohns|colitis|IBS|tinnitus|vertigo|sciatica|herniated|slipped disc|spina bifida|MS")
MEDICAL_THIRD_PERSON = re.compile(r"\b(my|his|her|their|our)\s+(son|daughter|child|kid|wife|husband|partner|mother|mom|father|dad|friend|brother|sister|grandmother|grandfather|patient|client)\b[^.\n]{0,80}\b(" + CONDITIONS + r")\b", re.I)
MEDICAL_NO_PRONOUN = re.compile(r"\b(living with|lives with|suffer(?:s|ed|ing)? from|diagnosed with|diagnosis of|struggl(?:e|es|ed|ing) with|battl(?:e|es|ed|ing) with|dealing with|recovering from|managing)\b[^.\n]{0,40}\b(" + CONDITIONS + r")\b", re.I)
MEDICAL_SELF = re.compile(r"\b(I|I've|I'm|my)\b[^.\n]{0,80}\b(" + CONDITIONS + r")\b", re.I)


def is_medical_disclosure(line):
    """A line where someone discloses a medical condition: their own (first person) or a family member's or friend's (third person), from a list of conditions, or the exact marker once removed by hand."""
    return any(marker in line for marker in EXCLUDE_MARKERS) or bool(MEDICAL_SELF.search(line)) or bool(MEDICAL_THIRD_PERSON.search(line)) or bool(MEDICAL_NO_PRONOUN.search(line))


class CanonError(Exception):
    pass


def _dir(root, brand):
    if not re.fullmatch(r"[a-z0-9_\-]+", brand or ""):
        raise CanonError(f"{brand!r} is not a brand folder name")
    return Path(root) / brand / "registry"


def _words(text):
    words = {w for w in re.findall(r"[a-z0-9$#%]+", (text or "").lower()) if w not in _STOP and len(w) > 1}
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words}          # awards = award: a plural does not hide a claim


def _overlap(a, b):
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


GATE_FILE = "claims_gate.yaml"
_DEFAULT_GATE = {"threshold": 0.6, "aliases": {"gma": "good morning america"}, "negations": ["no", "not", "never", "without", "zero", "none", "n't", "nor"], "generic_words": [], "extra_sensitive": {}}


def load_gate(path=GATE_FILE):
    """The gate's settings (claims_gate.yaml; the built-in defaults when the file is absent or unreadable). Validated: a bad value falls back to its default, never to a gate that lets everything through."""
    import yaml
    gate = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in _DEFAULT_GATE.items()}
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    data = data if isinstance(data, dict) else {}
    if isinstance(data.get("threshold"), (int, float)) and not isinstance(data["threshold"], bool) and 0.3 <= data["threshold"] <= 0.95:
        gate["threshold"] = float(data["threshold"])
    if isinstance(data.get("aliases"), dict):
        gate["aliases"] = {str(k).lower(): str(v).lower() for k, v in data["aliases"].items()}
    for key in ("negations", "generic_words"):
        if isinstance(data.get(key), list) and data[key] and all(isinstance(x, str) for x in data[key]):          # a word YAML read as true/false (an unquoted no) makes the whole list unusable: the default stays
            gate[key] = [x.lower() for x in data[key]]
    if isinstance(data.get("extra_sensitive"), dict):
        extra = {}
        for cat, pattern in data["extra_sensitive"].items():
            try:
                re.compile(str(pattern))
                extra[str(cat)] = str(pattern)
            except re.error:
                continue
        gate["extra_sensitive"] = extra
    return gate


def _norm_text(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$#% ]+", " ", (text or "").lower())).strip()


def _expand(sentence, gate):
    """The sentence with short forms written out (GMA -> good morning america)."""
    out = sentence
    for short, long in gate["aliases"].items():
        out = re.sub(rf"\b{re.escape(short)}\b", long, out, flags=re.I)
    return out


def claim_key(text):
    """A claim's NAME: what comes before the first colon or dash ("Sample price: Starter Story says ..." is named "Sample price"), at most 12 words. The long explanation after it is not what identifies the claim."""
    key = re.split(r"\s*[:—]\s+", (text or "").strip(), maxsplit=1)[0]
    return " ".join(key.split()[:12])


def _anchors(claim, gate):
    """The distinctive words of a claim's NAME: what makes a sentence about THIS claim, whatever its wording. Taken from the name only, never from the long explanation that follows it (a word like
    'quoted' deep in an explanation once blocked an unrelated sentence)."""
    skip = set(gate["generic_words"])
    name = claim_key(claim["text"])
    return {w for w in _words(name) if len(w) >= 4 and w not in skip and not w.isdigit()} | {w for w in _words(name) if re.search(r"\d", w) and len(w) >= 2}


def _share(sentence_words, claim):
    """How much of the sentence is made of the claim's own text and reason."""
    claim_words = _words(claim["text"] + " " + (claim.get("reason") or ""))
    return len(sentence_words & claim_words) / len(sentence_words) if sentence_words else 0.0


GENESIS = "0" * 16


def _line_hash(line):
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def _lines_digest(lines):
    return hashlib.sha256("\n".join(l.strip() for l in lines).encode("utf-8")).hexdigest()[:32]


def claim_id(brand, text):
    return "cl-" + hashlib.sha256(f"{brand}|{_norm_text(text)}".encode()).hexdigest()[:10]          # the wording, not just its word set: two different claims never collide


class Registry:
    """One brand's registries. `append` writes one JSON line and nothing ever rewrites or deletes a line."""

    def __init__(self, data_root, brand, gate=None):
        self.brand, self.path = brand, _dir(data_root, brand) / "events.ndjson"
        self.gate_settings = gate if gate is not None else load_gate()
        self.damaged = 0
        self.chain_problems = []                                                   # lines that do not follow the line before them (an edit, a deletion or a reordering of the log)
        self.rejected = []                                                         # events the replay ignored because they break the rules (a decision by a non-owner, a transition that is not allowed)
        self._events = self._read()

    def _read(self):
        events, self.damaged, self.chain_problems, before, seen = [], 0, [], None, []
        if self.path.is_file():
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or "kind" not in event:
                        raise ValueError
                    if "prev" in event:
                        expected = GENESIS if before is None else _line_hash(before)
                        if event["prev"] != expected:
                            self.chain_problems.append(f"line {number} does not follow the line before it: the log was edited, shortened or reordered" if before is not None
                                                       else f"line {number} is the first line but is not the start of the chain: the lines before it were deleted")
                    if event.get("kind") == "seal" and event.get("digest") != _lines_digest(seen[: int(event.get("upto") or 0)]):
                        self.chain_problems.append(f"line {number}: the lines before the seal were edited, shortened or reordered since it was written")
                    events.append(event)
                except ValueError:
                    self.damaged += 1                                            # a damaged line is counted and skipped, never a crash and never rewritten
                before = line
                seen.append(line)
        return events

    def append(self, kind, actor, at, **payload):
        if actor not in (OWNER, TOOL):
            raise CanonError(f"actor must be owner or tool, not {actor!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(at or "")):
            raise CanonError(f"{at!r} is not a UTC timestamp like 2026-11-17T08:00:00Z")
        event = {"n": len(self._events) + 1, "at": at, "kind": kind, "actor": actor, **payload}
        lines = [l for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()] if self.path.is_file() else []
        if lines and not any('"prev"' in l or '"kind": "seal"' in l for l in lines):          # a log that began before the chain existed: SEAL what is there first, so the old lines are covered too
            seal = {"n": len(self._events) + 1, "at": at, "kind": "seal", "actor": TOOL, "upto": len(lines), "digest": _lines_digest(lines), "prev": _line_hash(lines[-1])}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(seal, ensure_ascii=False, sort_keys=True) + "\n")
            self._events.append(seal)
            event["n"] = len(self._events) + 1
            lines.append(json.dumps(seal, ensure_ascii=False, sort_keys=True))
        event["prev"] = _line_hash(lines[-1]) if lines else GENESIS                  # each line carries the hash of the line before it (the first one, GENESIS): an edit, a deletion or a reordering shows
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._events.append(event)
        return event

    # ---- state, replayed from the events ------------------------------------------------------------------
    def state(self):
        canon, claims, foundation = {}, {}, {}
        self.rejected = []
        for e in self._events:
            kind = e["kind"]
            if kind == "canon_proposed":
                canon[e["id"]] = {"id": e["id"], "section": e["section"], "text": e["text"], "status": "proposed", "source": e.get("source", ""), "source_sha": e.get("source_sha", ""), "proposed_at": e["at"], "decided_at": "", "gate_note": e.get("note", ""), "needs": e.get("needs", []), "claims_used": e.get("claims_used", [])}
            elif kind == "canon_superseded" and e.get("id") in canon and e.get("actor") == TOOL and canon[e["id"]]["status"] == "proposed":
                canon[e["id"]]["status"] = "superseded"
            elif kind in ("foundation_needs_input", "foundation_rejected"):
                foundation[kind] = foundation.get(kind, 0) + 1
            elif kind == "canon_decided" and e.get("id") in canon:
                if e.get("actor") != OWNER or canon[e["id"]]["status"] != "proposed" or e.get("decision") not in ("approved", "rejected"):
                    self.rejected.append(f"event {e.get('n')}: a canon decision that is not the owner's, or not on a proposal, was ignored")
                    continue
                entry = canon[e["id"]]
                entry["status"], entry["decided_at"], entry["note"] = e["decision"], e["at"], e.get("note", "")
                if e["decision"] == "approved":
                    for other in canon.values():                                  # a newly approved text replaces the section's earlier approved one
                        if other is not entry and other["section"] == entry["section"] and other["status"] == "approved":
                            other["status"] = "superseded"
            elif kind == "claim_added":
                if e.get("status") == "verified" and e.get("actor") != OWNER:
                    self.rejected.append(f"event {e.get('n')}: a claim added as verified by someone other than the owner was ignored")
                    continue
                claims.setdefault(e["id"], {"id": e["id"], "text": e["text"], "category": e["category"], "status": e["status"], "source": e.get("source", ""), "reason": e.get("reason", ""), "added_at": e["at"], "decided_at": ""})
            elif kind == "claim_conflict" and e.get("id") in claims and e.get("with") in claims:
                claims[e["id"]].setdefault("conflicts_with", []).append(e["with"])
                claims[e["with"]].setdefault("conflicts_with", []).append(e["id"])                  # both sides know about it; neither changes status
            elif kind == "claim_status" and e.get("id") in claims:
                if e.get("status") == "verified" and e.get("actor") != OWNER:
                    self.rejected.append(f"event {e.get('n')}: a verification by someone other than the owner was ignored")
                    continue
                claims[e["id"]].update(status=e["status"], decided_at=e["at"], source=e.get("source") or claims[e["id"]]["source"], reason=e.get("reason") or claims[e["id"]]["reason"])
                if e.get("actor") == OWNER:
                    claims[e["id"]]["owner_decided"] = True                                   # the owner's word is final: evidence never overrides it
            elif kind == "claim_evidence" and e.get("id") in claims:
                claim = claims[e["id"]]
                supports = [s for s in (e.get("supports") or []) if isinstance(s, dict) and len(str(s.get("quote", ""))) >= 25 and s.get("domain")]
                contradicts = [c for c in (e.get("contradicts") or []) if isinstance(c, dict) and len(str(c.get("quote", ""))) >= 25 and c.get("domain")]
                outcome = e.get("outcome") or ("verified" if e.get("verified") else "")
                claim["evidence"] = {"supports": supports, "contradicts": contradicts, "checked_at": e["at"], "outcome": outcome}
                supports = [s for s in supports if s.get("close") is True]                         # a source counts when it is CLOSE to the fact (judged by the reviewer, whatever the medium)
                domains = {s["domain"] for s in supports}
                claim["evidence"]["supports"] = supports
                if claim.get("owner_decided"):
                    pass                                                                          # the owner's word is final: evidence is kept, the status is never moved
                elif outcome == "verified" and not contradicts and supports:
                    # defence in depth: the fold itself re-checks the rule (one source close to the fact, in ANY medium, with an exact quotation, and nothing contradicting), so an event written any other way cannot verify a claim
                    best = supports[0]
                    single = len(domains) == 1
                    risky = [s for s in supports if s.get("risk") == "high"]
                    claim.update(status="verified", verified_by="evidence", single_source=single, decided_at=e["at"], source=best.get("url", claim["source"]),
                                 reason=(f"VERIFIED FROM ONE SOURCE (spot-check it): {best.get('medium', 'other')} at {best['domain']}" + ("; the source may have a reason to exaggerate this claim" if risky else "")
                                         if single else f"verified from {len(domains)} independent sources by the research check: " + ", ".join(sorted(domains))[:200]
                                         + ("; one source may have a reason to exaggerate this claim" if risky else "")))
                elif outcome == "conflicted" and contradicts:
                    claim.update(status="conflicted", decided_at=e["at"], reason=f"sources disagree: {len(supports)} support it and {len(contradicts)} contradict it (" + ", ".join(sorted({c['domain'] for c in contradicts}))[:160] + "): the owner decides")
                elif outcome == "unverified" and claim["status"] == "blocked" and not contradicts:
                    claim.update(status="unverified", reason="checked from public sources: none that is reliable was found, nothing contradicts it: left unverified for the owner to decide")
        return {"canon": canon, "claims": claims, "foundation": foundation}

    # ---- the canon ------------------------------------------------------------------------------------------
    def propose_canon(self, section, text, source, source_sha, at, note="", needs=None, claims_used=None):
        """A skill's output, PROPOSED. It is not canonical until the owner approves it."""
        if not (section or "").strip() or not (text or "").strip():
            raise CanonError("a canon proposal needs a section and a text")
        raw = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        used = [int(n) for n in re.findall(r"canon-(\d+)", raw)]                         # every id ever written, even on a damaged line
        n = 1 + max(used or [0])                                                       # one more than the highest id ever used: a skipped damaged line cannot make an id repeat
        for entry in self.state()["canon"].values():                                      # a new proposal replaces the tool's own earlier UNAPPROVED one for the section (an approved text only changes by the owner's approval)
            if entry["section"] == section.strip() and entry["status"] == "proposed":
                self.append("canon_superseded", TOOL, at, id=entry["id"])
        return self.append("canon_proposed", TOOL, at, id=f"canon-{n:04d}", section=section.strip(), text=text.strip(), source=source, source_sha=source_sha, note=note,
                           needs=[str(x)[:300] for x in (needs or [])][:20], claims_used=[str(x)[:200] for x in (claims_used or [])][:20])["id"]

    def decide_canon(self, entry_id, decision, actor, at, note=""):
        if actor != OWNER:
            raise CanonError("only the owner approves or rejects canon: the tool can only propose it")
        if decision not in ("approved", "rejected"):
            raise CanonError("a decision is approved or rejected")
        if self.state()["canon"].get(entry_id, {}).get("status") != "proposed":
            raise CanonError(f"{entry_id} is not a proposal waiting for a decision")
        return self.append("canon_decided", OWNER, at, id=entry_id, decision=decision, note=note)

    def canonical(self):
        """{section: text} of what the owner has approved: the only thing a downstream skill may treat as canon."""
        return {e["section"]: e["text"] for e in self.state()["canon"].values() if e["status"] == "approved"}

    # ---- the claims ledger ----------------------------------------------------------------------------------
    def add_claim(self, text, category, status, source, reason, actor, at):
        if category not in CATEGORIES or status not in CLAIM_STATUSES:
            raise CanonError("unknown claim category or status")
        if status == "verified" and actor != OWNER:
            raise CanonError("only the owner marks a claim verified")
        cid = claim_id(self.brand, text)
        if cid in self.state()["claims"]:
            return cid                                                           # the same claim is never added twice (seeding can be run again)
        self.append("claim_added", actor, at, id=cid, text=text.strip(), category=category, status=status, source=source, reason=reason)
        return cid

    def set_claim_status(self, cid, status, actor, at, source="", reason=""):
        if status not in CLAIM_STATUSES:
            raise CanonError("unknown claim status")
        if cid not in self.state()["claims"]:
            raise CanonError(f"{cid} is not in the ledger")
        if status == "verified" and actor != OWNER:
            raise CanonError("only the owner marks a claim verified")
        return self.append("claim_status", actor, at, id=cid, status=status, source=source, reason=reason)

    def record_evidence(self, cid, supports, contradicts, outcome, at):
        """What a public-source check found for a claim: pages that support it (each with its exact quotation and the kind of source) and pages that contradict it. `outcome`:
        verified     ONE source close to the fact, in any medium (a page, a post, an interview, a podcast, a newsletter...), with an exact quotation and nothing contradicting it; with only one source the claim
                     is marked single-source so the owner can spot-check it;
        conflicted   sources contradict each other, or one directly contradicts the claim: it is blocked and the owner decides;
        unverified   no source close to the fact was found and nothing contradicts it: a claim that was blocked is left UNVERIFIED for the owner to decide.
        The ledger re-checks every rule when it reads the event, and the owner's own decision is never moved. True and False are accepted for verified and unverified."""
        if cid not in self.state()["claims"]:
            raise CanonError(f"{cid} is not in the ledger")
        outcome = {True: "verified", False: "unverified"}.get(outcome, outcome)
        if outcome not in ("verified", "conflicted", "unverified"):
            raise CanonError("an evidence outcome is verified, conflicted or unverified")
        return self.append("claim_evidence", TOOL, at, id=cid, supports=list(supports)[:6], contradicts=list(contradicts)[:6], outcome=outcome)

    def add_conflict(self, text, category, source, reason, against, at):
        """New research disagrees with one or more claims already in the ledger (`against`: an id or a list of ids): the NEW version is added as conflicted and linked to EACH of them. The old claims are never
        touched (a verified one stays verified); all stay for the owner to decide."""
        ids = [against] if isinstance(against, str) else list(against)
        for against_id in ids:
            if against_id not in self.state()["claims"]:
                raise CanonError(f"{against_id} is not in the ledger")
        cid = self.add_claim(text, category, "conflicted", source, reason, TOOL, at)
        for against_id in ids:
            if not any(e["kind"] == "claim_conflict" and e.get("id") == cid and e.get("with") == against_id for e in self._events):
                self.append("claim_conflict", TOOL, at, id=cid, **{"with": against_id})
        return cid

    def record_sources(self, urls, at):
        """The pages a refresh has already read (so the next refresh does not read them again)."""
        seen = self.refresh_seen()
        for url in urls:
            if url not in seen:
                self.append("refresh_source", TOOL, at, url=url)

    def refresh_seen(self):
        return {e["url"] for e in self._events if e["kind"] == "refresh_source" and e.get("url")}

    def reconsider_conflict(self, cid, at, reason):
        """The tool takes back a conflict it created itself (a refresh fact linked to an old claim) when a better check finds there was no real disagreement: the claim goes to UNVERIFIED with the reason
        written down. The link stays in the log; nothing is deleted; a claim the owner decided is never touched."""
        claim = self.state()["claims"].get(cid)
        if not claim or claim["status"] != "conflicted" or claim.get("decided_at"):
            raise CanonError(f"{cid} is not a conflict the tool may take back")
        return self.append("claim_status", TOOL, at, id=cid, status="unverified", reason=reason[:300])

    def gate(self, text):
        """{verdict, findings}. Each sentence that says something checkable (an award, press, a ranking, a health claim, a figure, a customer count) is read against the ledger:
        BLOCKED   it repeats a claim the ledger blocks or marks conflicted (by its distinctive words or by its text and reason, short forms written out: GMA = Good Morning America);
        NEEDS_REVIEW   it matches an unverified claim, denies something (a denial is not repeating it), or is checkable and nothing verified, nothing the owner approved as canon covers it;
        OK   nothing checkable, or the owner verified a claim it is about, or approved canon covers it.
        The wording is lexical (words, not meaning): a claim worded very differently can reach NEEDS_REVIEW instead of BLOCKED, and the ledger is the only authority on what is true."""
        gate = self.gate_settings
        state = self.state()
        claims, canon_texts = list(state["claims"].values()), [e["text"] for e in state["canon"].values() if e["status"] == "approved"]
        patterns = dict(SENSITIVE)
        for cat, extra in gate["extra_sensitive"].items():
            patterns[cat] = patterns.get(cat, "(?!)") + "|" + extra
        findings, verdict = [], "OK"
        for sentence in [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]:
            expanded = _expand(sentence, gate)
            hits = [cat for cat, pattern in patterns.items() if re.search(pattern, expanded, re.I)]
            if not hits:
                continue
            words = _words(expanded)
            negated = bool(re.search(r"\b(" + "|".join(re.escape(n) for n in gate["negations"] if n != "n't") + r")\b|n't\b", expanded, re.I))
            matched = [c for c in claims if c["status"] != "verified" and (words & _anchors(c, gate) or _share(words, c) >= gate["threshold"])]
            if matched:
                worst = next((c for c in matched if c["status"] in ("blocked", "conflicted")), matched[0])
                blocking = worst["status"] in ("blocked", "conflicted") and not negated
                why = f"the ledger says {worst['status']}: {worst['reason'] or worst['text'][:120]}" + (" (the sentence denies it, so it goes to review rather than being blocked)" if worst["status"] in ("blocked", "conflicted") and negated else "")
                findings.append({"sentence": sentence, "categories": hits, "result": "BLOCKED" if blocking else "NEEDS_REVIEW", "claim": worst["id"], "why": why})
                verdict = "BLOCKED" if blocking else (verdict if verdict == "BLOCKED" else "NEEDS_REVIEW")
                continue
            if any(c["status"] == "verified" and (words & _anchors(c, gate) or _share(words, c) >= gate["threshold"]) for c in claims):
                continue                                                          # the owner verified a claim this sentence is about
            if any(len(words & _words(ct)) / len(words) >= gate["threshold"] for ct in canon_texts if words):
                continue                                                          # approved canon says it
            findings.append({"sentence": sentence, "categories": hits, "result": "NEEDS_REVIEW", "claim": "", "why": "a statement that can be checked, and no verified claim or approved canon covers it"})
            verdict = verdict if verdict == "BLOCKED" else "NEEDS_REVIEW"
        return {"verdict": verdict, "findings": findings}

    # ---- the Sheets projection and the owner's decisions read back ------------------------------------------
    def canon_rows(self):
        head = ["ID", "Section", "Text", "Status", "Source", "Proposed", "Gate note", "Decision (approve / reject)"]
        return [head] + [[e["id"], e["section"], e["text"], e["status"], e["source"], e["proposed_at"], e.get("gate_note", ""), ""] for e in self.state()["canon"].values()]

    def claim_rows(self):
        head = ["ID", "Claim", "Category", "Status", "Source", "Reason", "Decision (verified / unverified / blocked / conflicted)"]
        return [head] + [[c["id"], c["text"], c["category"], c["status"], c["source"], c["reason"], ""] for c in self.state()["claims"].values()]

    def owner_decisions(self, canon_rows=None, claim_rows=None, at=""):
        """Apply the Decision column of the two tabs (rows as read back from the sheet). Returns {applied, ignored}: a decision that is blank, unknown or not allowed is ignored and named."""
        applied, ignored, settled = 0, [], {"canon": [], "claims": []}                   # settled: the rows whose Decision is now reflected in their status (the sheet clears those cells)
        for row in (canon_rows or [])[1:]:
            entry_id, decision = str(row[0]), str(row[-1]).strip().lower()
            if not decision:
                continue
            word = {"approve": "approved", "approved": "approved", "reject": "rejected", "rejected": "rejected"}.get(decision)
            try:
                if not word:
                    raise CanonError(f"{decision!r} is not approve or reject")
                if self.state()["canon"].get(entry_id, {}).get("status") == word:
                    settled["canon"].append(entry_id)
                    continue                                                        # the Decision cell still shows what was already applied: nothing to do
                self.decide_canon(entry_id, word, OWNER, at)
                applied += 1
                settled["canon"].append(entry_id)
            except CanonError as exc:
                ignored.append(f"{entry_id}: {exc}")
        for row in (claim_rows or [])[1:]:
            cid, decision = str(row[0]), str(row[-1]).strip().lower()
            if not decision:
                continue
            try:
                if self.state()["claims"].get(cid, {}).get("status") == decision:
                    settled["claims"].append(cid)
                    continue
                self.set_claim_status(cid, decision, OWNER, at)
                applied += 1
                settled["claims"].append(cid)
            except CanonError as exc:
                ignored.append(f"{cid}: {exc}")
        return {"applied": applied, "ignored": ignored, "settled": settled}


# ---- the story bank: split per brand and the Gaps sections turned into ledger seeds ----------------------------
def split_story_bank(md):
    """{brand_slug: text}: the three brands' parts of Story-Bank-Material.md, each with the shared header (it holds the two corrections). The brands are in the file's order."""
    lines = md.split("\n")
    gaps = [i for i, l in enumerate(lines) if re.match(r"^##\s+(\d+\.\s+)?Gaps / not found", l)]
    if len(gaps) != 3:
        raise CanonError(f"expected three 'Gaps / not found' sections in the story bank, found {len(gaps)}")
    first_rule = next(i for i, l in enumerate(lines) if l.strip() == "---")
    header = "\n".join(lines[:first_rule]).strip()
    cuts = []
    for g in gaps[:2]:
        cuts.append(next(i for i in range(g, len(lines)) if lines[i].strip() == "---"))
    parts = ["\n".join(lines[first_rule + 1:cuts[0]]), "\n".join(lines[cuts[0] + 1:cuts[1]]), "\n".join(lines[cuts[1] + 1:])]
    marks = (("swoveralls", "swoveralls"), ("duderobe", "duderobe"), ("defi_snacks", "defi"))
    out = {}
    titles = {"swoveralls": "Swoveralls", "duderobe": "DudeRobe", "defi_snacks": "DeFi Snacks"}
    for (slug, needle), part in zip(marks, parts):
        if needle not in part.lower():
            raise CanonError(f"the {slug} part of the story bank does not mention the brand: the file's order changed")
        own_header = re.sub(r"^#[^\n]*", f"# Story Bank Material — {titles[slug]}", header, count=1)           # each brand's file carries its own title, not the combined one
        kept = [l for l in part.split("\n") if not is_medical_disclosure(l)]
        out[slug] = own_header + "\n\n---\n\n" + "\n".join(kept).strip() + "\n"
    return out


def gap_items(brand_md):
    """The bullets or numbered points under the 'Gaps / not found' heading: [{'title', 'text'}]."""
    lines = brand_md.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^##\s+(\d+\.\s+)?Gaps / not found", l)), None)
    if start is None:
        return []
    items, current, parent_index, with_children = [], None, 0, set()
    for l in lines[start + 1:]:
        if l.startswith("## ") or l.strip() == "---":
            break
        match = re.match(r"^\s{0,2}(?:[-*]|\d+\.)\s+(.*)$", l)
        if match and not l.startswith("    "):
            current = [match.group(1)]
            if l.startswith("  ") and items:                                      # a nested point inherits what its parent says (for example "Conflicting figures")
                current.append("PARENT:" + items[parent_index][0])
                with_children.add(parent_index)
            else:
                parent_index = len(items)
            items.append(current)
        elif current is not None and l.strip():
            current.append(l.strip().lstrip("-* "))
    out = []
    for index, parts in enumerate(items):
        parent = next((p[len("PARENT:"):] for p in parts if p.startswith("PARENT:")), "")
        parts = [p for p in parts if not p.startswith("PARENT:")]
        text = " ".join(parts)
        bold = re.match(r"^\*\*(.+?)\*\*[:.]?\s*(.*)$", text)
        if bold:
            title = bold.group(1).strip(" .:")
        else:                                                                       # up to 100 characters, ending at a word, never in the middle of one
            title = text if len(text) <= 100 else text[:100].rsplit(" ", 1)[0] + " ..."
        out.append({"title": title, "text": re.sub(r"\*\*", "", text).strip(), "parent": re.sub(r"\*\*", "", parent).strip(), "is_parent": index in with_children})
    return out


def classify_gap(item):
    low = (item["title"] + " " + item["text"] + " " + item.get("parent", "")).lower()

    def category_of(s):
        return ("award" if re.search(SENSITIVE["award"], s) else "press" if re.search(r"article|press|media|gma|good morning|thrillist|interview|podcast|reddit|forum", s)
                else "figure" if re.search(SENSITIVE["figure"] + r"|funding|revenue|sales|price", s) else "customer" if re.search(r"customer|review|testimonial|trustpilot|bbb|sitejabber", s)
                else "origin" if re.search(r"founder|founding|name|date|team|hq|manufactur", s) else "other")
    absence = item["title"].lower().startswith("no ")                                       # "No public information found": an absence, said once, the same way for every brand
    category = category_of(item["title"].lower())                                        # what the title says wins; the longer text is only the fallback
    if category == "other" and not absence:
        category = category_of(low)
    status = "conflicted" if "conflict" in low or "discrepancy" in low else "unverified" if absence else "blocked" if re.search(r"\bdo not\b|\bnot verified\b|\bunverified\b|\bnot independently\b|\btreat as\b|\bnot corroborated\b|\bestimate", low) else "unverified"
    return category, status


def seed_from_story_bank(brand_slug, brand_md):
    """[(text, category, status, reason)] for the ledger, from the brand's own Gaps section. Nothing is invented: every seed is a gap the research itself recorded."""
    seeds = []
    for item in gap_items(brand_md):
        if item.get("is_parent"):                                                         # a heading over nested points ("Conflicting figures"): its points are the claims
            continue
        category, status = classify_gap(item)
        seeds.append((item["title"], category, status, item["text"]))
    return seeds


# ---- the Sheets tabs -------------------------------------------------------------------------------------------
CANON_TAB, CLAIMS_TAB = "Canon", "Claims"
CANON_COLUMNS = ["id", "section", "text", "status", "source", "proposed_at", "gate_note"]
CLAIM_COLUMNS = ["id", "claim", "category", "status", "source", "reason"]


def sheet_tabs(data_root, brand):
    """The Canon and Claims tab definitions for the sheet sync (the same shape as the other tabs): [] while the brand has no registry events. The Decision and Notes columns belong to the owner."""
    reg = Registry(data_root, brand)
    state = reg.state()
    if not state["canon"] and not state["claims"]:
        return []
    return [{"title": CANON_TAB, "columns": CANON_COLUMNS, "key_col": "id", "starter_manual": ["Decision", "Notes"],
             "rows_by_key": {e["id"]: {"id": e["id"], "section": e["section"], "text": e["text"], "status": e["status"], "source": e["source"], "proposed_at": e["proposed_at"], "gate_note": e.get("gate_note", "")} for e in state["canon"].values()}},
            {"title": CLAIMS_TAB, "columns": CLAIM_COLUMNS, "key_col": "id", "starter_manual": ["Decision", "Notes"],
             "rows_by_key": {c["id"]: {"id": c["id"], "claim": c["text"], "category": c["category"], "status": c["status"], "source": c["source"], "reason": c["reason"]} for c in state["claims"].values()}}]


def rows_from_sheet(values):
    """A tab as read from the sheet (header row first) -> [header, [id, ..., decision]] for `owner_decisions`, found by header NAME so a moved column still works. Empty when the tab lacks id or Decision."""
    if not values:
        return []
    header = [str(h).strip().lower() for h in values[0]]
    decision_at = next((k for k, h in enumerate(header) if h == "decision" or h.startswith("decision (")), None)          # "Decision" or "Decision (approve / reject)": a header pasted from an export still works
    if "id" not in header or decision_at is None:
        return []
    i, d = header.index("id"), decision_at
    return [["id", "decision"]] + [[str(r[i]) if i < len(r) else "", str(r[d]) if d < len(r) else ""] for r in values[1:]]


def load_seed(path):
    """The claims seed of a brand (brands/<slug>/claims_seed.yaml): [{text, category, status, reason, source}]. Empty when the file is absent or unreadable."""
    import yaml
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else []
    except (OSError, yaml.YAMLError):
        return []
    return [s for s in (data or []) if isinstance(s, dict) and s.get("text") and s.get("category") in CATEGORIES and s.get("status") in CLAIM_STATUSES and s.get("status") != "verified"]


def apply_seed(reg, seeds, at):
    """Add the seeds to the ledger as the TOOL (never as verified). Returns (added, already in the ledger, of those already there how many carry another status or category than the seed says: the ledger is
    append-only, so a seed never changes an existing claim)."""
    added, differs = 0, 0
    for s in seeds:
        before = reg.state()["claims"]
        cid = reg.add_claim(s["text"], s["category"], s["status"], s.get("source", ""), s.get("reason", ""), TOOL, at)
        if cid not in before:
            added += 1
        elif (before[cid]["category"], before[cid]["status"]) != (s["category"], s["status"]):
            differs += 1
    return added, len(seeds) - added, differs