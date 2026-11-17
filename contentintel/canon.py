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


def _anchors(claim, gate):
    """The distinctive words of a claim's own title (its name): what makes a sentence about THIS claim, whatever its wording."""
    skip = set(gate["generic_words"])
    return {w for w in _words(claim["text"]) if len(w) >= 4 and w not in skip and not w.isdigit()} | {w for w in _words(claim["text"]) if re.search(r"\d", w) and len(w) >= 2}


def _share(sentence_words, claim):
    """How much of the sentence is made of the claim's own text and reason."""
    claim_words = _words(claim["text"] + " " + (claim.get("reason") or ""))
    return len(sentence_words & claim_words) / len(sentence_words) if sentence_words else 0.0


def claim_id(brand, text):
    return "cl-" + hashlib.sha256(f"{brand}|{_norm_text(text)}".encode()).hexdigest()[:10]          # the wording, not just its word set: two different claims never collide


class Registry:
    """One brand's registries. `append` writes one JSON line and nothing ever rewrites or deletes a line."""

    def __init__(self, data_root, brand, gate=None):
        self.brand, self.path = brand, _dir(data_root, brand) / "events.ndjson"
        self.gate_settings = gate if gate is not None else load_gate()
        self.damaged = 0
        self.rejected = []                                                         # events the replay ignored because they break the rules (a decision by a non-owner, a transition that is not allowed)
        self._events = self._read()

    def _read(self):
        events, self.damaged = [], 0
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or "kind" not in event:
                        raise ValueError
                    events.append(event)
                except ValueError:
                    self.damaged += 1                                            # a damaged line is counted and skipped, never a crash and never rewritten
        return events

    def append(self, kind, actor, at, **payload):
        if actor not in (OWNER, TOOL):
            raise CanonError(f"actor must be owner or tool, not {actor!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(at or "")):
            raise CanonError(f"{at!r} is not a UTC timestamp like 2026-11-17T08:00:00Z")
        event = {"n": len(self._events) + 1, "at": at, "kind": kind, "actor": actor, **payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._events.append(event)
        return event

    # ---- state, replayed from the events ------------------------------------------------------------------
    def state(self):
        canon, claims = {}, {}
        self.rejected = []
        for e in self._events:
            kind = e["kind"]
            if kind == "canon_proposed":
                canon[e["id"]] = {"id": e["id"], "section": e["section"], "text": e["text"], "status": "proposed", "source": e.get("source", ""), "source_sha": e.get("source_sha", ""), "proposed_at": e["at"], "decided_at": ""}
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
            elif kind == "claim_status" and e.get("id") in claims:
                if e.get("status") == "verified" and e.get("actor") != OWNER:
                    self.rejected.append(f"event {e.get('n')}: a verification by someone other than the owner was ignored")
                    continue
                claims[e["id"]].update(status=e["status"], decided_at=e["at"], source=e.get("source") or claims[e["id"]]["source"], reason=e.get("reason") or claims[e["id"]]["reason"])
        return {"canon": canon, "claims": claims}

    # ---- the canon ------------------------------------------------------------------------------------------
    def propose_canon(self, section, text, source, source_sha, at):
        """A skill's output, PROPOSED. It is not canonical until the owner approves it."""
        if not (section or "").strip() or not (text or "").strip():
            raise CanonError("a canon proposal needs a section and a text")
        used = [int(m.group(1)) for e in self._events if e["kind"] == "canon_proposed" for m in [re.fullmatch(r"canon-(\d+)", str(e.get("id")))] if m]
        n = 1 + max(used or [0])                                                       # one more than the highest id ever used: a skipped damaged line cannot make an id repeat
        return self.append("canon_proposed", TOOL, at, id=f"canon-{n:04d}", section=section.strip(), text=text.strip(), source=source, source_sha=source_sha)["id"]

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
        head = ["ID", "Section", "Text", "Status", "Source", "Proposed", "Decision (approve / reject)"]
        return [head] + [[e["id"], e["section"], e["text"], e["status"], e["source"], e["proposed_at"], ""] for e in self.state()["canon"].values()]

    def claim_rows(self):
        head = ["ID", "Claim", "Category", "Status", "Source", "Reason", "Decision (verified / unverified / blocked / conflicted)"]
        return [head] + [[c["id"], c["text"], c["category"], c["status"], c["source"], c["reason"], ""] for c in self.state()["claims"].values()]

    def owner_decisions(self, canon_rows=None, claim_rows=None, at=""):
        """Apply the Decision column of the two tabs (rows as read back from the sheet). Returns {applied, ignored}: a decision that is blank, unknown or not allowed is ignored and named."""
        applied, ignored = 0, []
        for row in (canon_rows or [])[1:]:
            entry_id, decision = str(row[0]), str(row[-1]).strip().lower()
            if not decision:
                continue
            word = {"approve": "approved", "approved": "approved", "reject": "rejected", "rejected": "rejected"}.get(decision)
            try:
                if not word:
                    raise CanonError(f"{decision!r} is not approve or reject")
                if self.state()["canon"].get(entry_id, {}).get("status") == word:
                    continue                                                        # the Decision cell still shows what was already applied: nothing to do
                self.decide_canon(entry_id, word, OWNER, at)
                applied += 1
            except CanonError as exc:
                ignored.append(f"{entry_id}: {exc}")
        for row in (claim_rows or [])[1:]:
            cid, decision = str(row[0]), str(row[-1]).strip().lower()
            if not decision:
                continue
            try:
                if self.state()["claims"].get(cid, {}).get("status") == decision:
                    continue
                self.set_claim_status(cid, decision, OWNER, at)
                applied += 1
            except CanonError as exc:
                ignored.append(f"{cid}: {exc}")
        return {"applied": applied, "ignored": ignored}


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
        kept = [l for l in part.split("\n") if not any(marker in l for marker in EXCLUDE_MARKERS)]
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
CANON_COLUMNS = ["id", "section", "text", "status", "source", "proposed_at"]
CLAIM_COLUMNS = ["id", "claim", "category", "status", "source", "reason"]


def sheet_tabs(data_root, brand):
    """The Canon and Claims tab definitions for the sheet sync (the same shape as the other tabs): [] while the brand has no registry events. The Decision and Notes columns belong to the owner."""
    reg = Registry(data_root, brand)
    state = reg.state()
    if not state["canon"] and not state["claims"]:
        return []
    return [{"title": CANON_TAB, "columns": CANON_COLUMNS, "key_col": "id", "starter_manual": ["Decision", "Notes"],
             "rows_by_key": {e["id"]: {"id": e["id"], "section": e["section"], "text": e["text"], "status": e["status"], "source": e["source"], "proposed_at": e["proposed_at"]} for e in state["canon"].values()}},
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
    """Add the seeds to the ledger as the TOOL (never as verified). Returns (added, already_there)."""
    added = 0
    for s in seeds:
        before = len(reg.state()["claims"])
        reg.add_claim(s["text"], s["category"], s["status"], s.get("source", ""), s.get("reason", ""), TOOL, at)
        added += len(reg.state()["claims"]) - before
    return added, len(seeds) - added
