"""Deep Research reports in one Google Doc: a new tab for every report, newest first.

Collection saves each report in the repository and in the Collected tab as it always did. This is a separate step
that runs afterwards (like the Google Sheets step), so a Google problem can never fail or lose a research run: what
could not be archived is simply tried again next time. `data/<brand>/research/deep_archive.json` remembers which
reports already have a tab, so nothing is ever added twice.

Setup (once): create a Google Doc, press Share, add the service account's address (the client_email inside the
GOOGLE_SERVICE_ACCOUNT_JSON secret) as an Editor, switch the Google Docs API on for the same Google Cloud project,
and put the Doc's address (or just the id between /d/ and /edit) in the DEEP_RESEARCH_DOC_ID secret.
"""
import json
import re
import time

SCOPE = "https://www.googleapis.com/auth/documents"
API = "https://docs.googleapis.com/v1/documents"
TITLE_LIMIT = 60                 # characters of a tab's name
_RETRY = (429, 500, 502, 503, 504)
_SLEEP = time.sleep


class DocsError(Exception):
    """A problem a person can fix (sharing, a missing switch, a wrong id), worded as the fix."""


# ---------------------------------------------------------------------------------------
# the address of the Doc, and the connection
# ---------------------------------------------------------------------------------------

def parse_doc_id(text):
    """The Doc's id from its address or from the bare id; None when blank. Raises DocsError for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    found = re.search(r"/document/d/([A-Za-z0-9_-]{20,})", text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    raise DocsError("DEEP_RESEARCH_DOC_ID is not a Google Doc address or id. Paste the whole address of the Doc "
                    "(https://docs.google.com/document/d/.../edit) or just the long id between /d/ and /edit.")


def connect(key_json):
    """Return (session, service_account_email). Never echoes the key."""
    try:
        info = json.loads(key_json)
    except (TypeError, ValueError) as exc:
        raise DocsError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the whole key file into the secret.") from exc
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise DocsError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key (it needs client_email and private_key).")
    from google.auth.transport.requests import AuthorizedSession          # local import: only this step needs it
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    return AuthorizedSession(creds), info["client_email"]


# ---------------------------------------------------------------------------------------
# turning a report into text and formatting
# ---------------------------------------------------------------------------------------

def utf16_len(text):
    """Google Docs counts positions in UTF-16 code units, so an emoji is 2, not 1."""
    return len(text.encode("utf-16-le")) // 2


def tab_title(date, brand, question):
    """A short readable tab name: the date, the brand and the start of the question."""
    q = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]", " ", question or "")).strip()
    q = re.sub(r"\*+|`+", "", q).lstrip("# ").strip()                       # markdown bold and code marks
    q = re.sub(r"^(?:task|question)\s*:\s*", "", q, flags=re.I)
    head = f"{date} {brand} - "
    room = max(10, TITLE_LIMIT - len(head))
    return head + (q if len(q) <= room else q[:room - 1].rstrip() + "…")


_HEADING_END = re.compile(r"[.!?;,:]$")


def _merge_table(lines):
    """Gemini's page text splits each table row over several lines: the first cells on one line (separated by tabs),
    then each remaining cell on a line of its own. With the header's column count the row can be put back together.
    Anything that does not fit that pattern is left exactly as it was."""
    out, i, columns = [], 0, 0
    while i < len(lines):
        raw = lines[i]
        cells = [c.strip() for c in raw.split("\t") if c.strip()]
        if not columns and "\t" in raw and len(cells) >= 4:                    # the header row
            columns = len(cells)
            out.append(" | ".join(cells))
            i += 1
            continue
        if columns and "\t" in raw and 2 <= len(cells) < columns:               # the start of a row
            j = i + 1
            while len(cells) < columns and j < len(lines):
                if lines[j].strip():
                    cells.append(lines[j].strip())
                j += 1
            if len(cells) == columns:
                out.append(" | ".join(cells))
                i = j
                continue
        out.append(raw)
        i += 1
    return out


def report_blocks(text):
    """The report as (style, text) paragraphs. Gemini's page text has no markup, so headings are recognised by
    their shape: a short line without end punctuation, followed by a long paragraph or a table. Markdown "#" marks,
    when present, are honoured. Table rows are put back together and shown as " | " separated lines, and a table
    line is never taken for a heading."""
    lines = [re.sub(r"[ \xa0]+$", "", ln.replace("\xa0", " ")) for ln in (text or "").replace("\r", "").split("\n")]
    lines = _merge_table(lines)
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    blocks = []
    for pos, i in enumerate(nonempty):
        line = lines[i].strip()
        nxt = lines[nonempty[pos + 1]].strip() if pos + 1 < len(nonempty) else ""
        mark = re.match(r"(#{1,3})\s+(\S.*)", line)
        if mark:
            blocks.append(("heading1" if len(mark.group(1)) == 1 else "heading2", mark.group(2).strip()))
        elif ("\t" in line):
            blocks.append(("normal", " | ".join(c.strip() for c in line.split("\t") if c.strip())))
        elif (len(line) <= 100 and len(line.split()) <= 14 and not _HEADING_END.search(line)
              and (len(nxt) >= 80 or "\t" in nxt or " | " in nxt) and "\t" not in line and " | " not in line):
            blocks.append(("heading2", line))
        else:
            blocks.append(("normal", line))
    return blocks


def build_blocks(entry):
    """Every paragraph of one report's tab as (style, text, extras). `entry` carries: brand, at, run, question, prompt,
    exchanges, notes, diagnostics, claude_calls, text, sources."""
    blocks = [("title", entry["question"] or "Deep Research report", {})]
    blocks.append(("heading2", "Details", {}))
    facts = [("Brand", entry["brand"]), ("Collected", entry["at"]), ("Run", entry["run"] or "(unknown)"),
             ("Source", "Gemini Deep Research (web app)")]
    d = entry.get("diagnostics") or {}
    if d:
        facts.append(("Deep Research confirmed on", "yes" if d.get("mode_confirmed") else "NOT CONFIRMED"))
        facts.append(("Start research pressed", "yes" if d.get("start_clicked") else "no"))
        facts.append(("Waited", f"{(d.get('waited_seconds') or 0) // 60} min {(d.get('waited_seconds') or 0) % 60} s"))
    facts.append(("Claude calls", str(entry.get("claude_calls", 0))))
    for label, value in facts:
        blocks.append(("fact", f"{label}: {value}", {"bold_to": len(label) + 1}))
    for note in entry.get("notes") or []:
        blocks.append(("fact", f"Note: {note}", {"bold_to": 5}))
    blocks.append(("heading2", "Prompt sent to Gemini", {}))
    blocks += [("normal", ln, {}) for ln in (entry.get("prompt") or "").split("\n") if ln.strip()]
    if entry.get("exchanges"):
        blocks.append(("heading2", "Gemini's questions and Claude's answers", {}))
        for ex in entry["exchanges"]:
            blocks.append(("fact", f"Gemini: {ex['gemini']}", {"bold_to": 7}))
            blocks.append(("fact", f"Claude: {ex['reply']}", {"bold_to": 7}))
    blocks.append(("heading2", "Report", {}))
    blocks += [(style, text, {}) for style, text in report_blocks(entry["text"])]
    blocks.append(("heading2", "Sources", {}))
    for url in entry.get("sources") or []:
        blocks.append(("link", url, {"url": url}))
    if not entry.get("sources"):
        blocks.append(("normal", "(no source links were found in the report)", {}))
    return blocks


_NAMED = {"title": "TITLE", "heading1": "HEADING_1", "heading2": "HEADING_2"}


def build_requests(tab_id, blocks):
    """The batchUpdate requests that fill a new tab: one insert of all the text, then the styles by position."""
    text, spans = "", []
    for style, body, extra in blocks:
        start = utf16_len(text) + 1                       # a tab's body starts at position 1
        text += body + "\n"
        spans.append((style, start, start + utf16_len(body) + 1, body, extra))
    requests = [{"insertText": {"location": {"tabId": tab_id, "index": 1}, "text": text}}]
    for style, start, end, body, extra in spans:
        rng = {"tabId": tab_id, "startIndex": start, "endIndex": end}
        if style in _NAMED:
            requests.append({"updateParagraphStyle": {"range": rng, "paragraphStyle": {"namedStyleType": _NAMED[style]},
                                                      "fields": "namedStyleType"}})
        if extra.get("bold_to"):
            requests.append({"updateTextStyle": {"range": {**rng, "endIndex": start + utf16_len(body[:extra["bold_to"]])},
                                                 "textStyle": {"bold": True}, "fields": "bold"}})
        if style == "link":
            requests.append({"updateTextStyle": {"range": {**rng, "endIndex": end - 1}, "fields": "link",
                                                 "textStyle": {"link": {"url": extra["url"]}}}})
    return requests


# ---------------------------------------------------------------------------------------
# talking to Google
# ---------------------------------------------------------------------------------------

def _explain(code, payload, email):
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or "").strip()
    details = json.dumps(error.get("details") or [])
    if code == 403 and ("SERVICE_DISABLED" in details or "has not been used" in message or "is disabled" in message):
        found = re.search(r"https://console\.developers\.google\.com/apis/api/docs\.googleapis\.com[^\s\"'\\]*", message + " " + details)
        link = found.group(0) if found else "https://console.cloud.google.com/apis/library/docs.googleapis.com"
        return f"The Google Docs API is not switched on for the Google Cloud project of your service account. Open {link} and press Enable, then run this again."
    if code == 403:
        return (f"The Google Doc is not shared with {email}. Open the Doc, press Share, add that address as an Editor, "
                "and run this again.")
    if code == 404:
        return ("No Google Doc was found for DEEP_RESEARCH_DOC_ID. Check that it is the id of a Google Doc (not a Sheet) "
                "and that it has not been deleted.")
    if code == 401:
        return "Google did not accept the service account key. Create a new key and paste it into GOOGLE_SERVICE_ACCOUNT_JSON."
    return f"Google answered {code}" + (f": {message}" if message else "") + "."


class DocClient:
    def __init__(self, session, doc_id, email, sleep=None):
        self.session, self.doc_id, self.email = session, doc_id, email
        self.sleep = sleep or _SLEEP

    def _call(self, method, suffix="", **kw):
        url = f"{API}/{self.doc_id}{suffix}"
        for attempt in range(3):
            resp = self.session.request(method, url, timeout=60, **kw)
            code = resp.status_code
            if code in _RETRY and attempt < 2:
                self.sleep(2 * (attempt + 1))
                continue
            if code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                raise DocsError(_explain(code, payload, self.email))
            return resp.json()
        raise DocsError("Google kept answering with errors; try again later.")      # pragma: no cover

    def info(self):
        """(document title, titles of its tabs)."""
        body = self._call("GET", params={"includeTabsContent": "true", "fields": "title,tabs(tabProperties)"})
        tabs = [(t.get("tabProperties") or {}).get("title", "") for t in body.get("tabs") or []]
        return body.get("title", ""), tabs

    def add_tab(self, title):
        body = self._call("POST", ":batchUpdate",
                          json={"requests": [{"addDocumentTab": {"tabProperties": {"title": title, "index": 0}}}]})
        try:
            return body["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DocsError("Google did not say which tab it created; nothing was written.") from exc

    def delete_tab(self, tab_id):
        self._call("POST", ":batchUpdate", json={"requests": [{"deleteTab": {"tabId": tab_id}}]})

    def add_report(self, title, blocks):
        """A new first tab holding the blocks. Returns (tab id, address of the tab). A tab that could not be filled
        is removed again, so there is never an empty tab left behind."""
        tab_id = self.add_tab(title)
        try:
            self._call("POST", ":batchUpdate", json={"requests": build_requests(tab_id, blocks)})
        except DocsError:
            try:
                self.delete_tab(tab_id)
            except DocsError:
                pass
            raise
        return tab_id, f"https://docs.google.com/document/d/{self.doc_id}/edit?tab={tab_id}"

    def check(self):
        """Prove the whole path without leaving anything behind: read the Doc, add a tab, delete it."""
        title, tabs = self.info()
        tab_id = self.add_tab("connection test (safe to delete)")
        try:
            self.delete_tab(tab_id)
        except DocsError as exc:
            raise DocsError(f"A test tab could be added but not removed ({exc}); delete the tab called "
                            "'connection test' by hand.") from exc
        return {"title": title, "tabs": len(tabs)}


# ---------------------------------------------------------------------------------------
# which reports still need a tab
# ---------------------------------------------------------------------------------------

def report_key(item):
    """The saved report's file name: what identifies a report across runs."""
    url = str((item or {}).get("url") or "")
    return url.rsplit("/", 1)[-1] if url.endswith(".md") else ""


def entries_to_archive(slug, records, state):
    """Reports in the collected data that have no tab yet, oldest first, as entries for build_blocks."""
    done = (state or {}).get("items", {})
    out = []
    for rec in records:
        if rec.get("source") != "deep_research":
            continue
        for item in rec.get("items") or []:
            key = report_key(item)
            if not key or key in done or not (item.get("text") or "").strip():
                continue
            deep = rec.get("deep") or {}
            out.append({"key": key, "brand": slug, "at": rec.get("at", ""), "run": rec.get("run", ""),
                        "question": item.get("title", ""), "prompt": deep.get("prompt", ""),
                        "exchanges": deep.get("exchanges") or [], "notes": deep.get("notes") or [],
                        "diagnostics": deep.get("diagnostics") or {}, "claude_calls": deep.get("claude_calls", 0),
                        "text": item["text"], "sources": item.get("extra_sources") or []})
    return out


def archive_reports(store, slugs, client, now, dry_run=False):
    """Add a tab for every report that has none. Returns (lines for the log, number added, number failed).
    One failing report does not stop the others, and is retried next time."""
    lines, added, failed = [], 0, 0
    for slug in slugs:
        state = store.read_deep_archive(slug) or {"items": {}}
        todo = entries_to_archive(slug, store.read_collected(slug), state)
        if not todo:
            lines.append(f"{slug}: no new Deep Research reports to add")
            continue
        for entry in todo:
            title = tab_title(entry["at"][:10], slug, entry["question"])
            if dry_run:
                lines.append(f"{slug}: would add a tab '{title}'")
                continue
            try:
                tab_id, url = client.add_report(title, build_blocks(entry))
            except DocsError as exc:
                failed += 1
                lines.append(f"{slug}: could not add '{title}': {exc}")
                continue
            state.setdefault("items", {})[entry["key"]] = {"tab_id": tab_id, "url": url, "title": title, "archived_at": now}
            store.write_deep_archive(slug, state)                     # saved after each tab: a crash never repeats one
            added += 1
            lines.append(f"{slug}: added the tab '{title}' ({url})")
    return lines, added, failed
