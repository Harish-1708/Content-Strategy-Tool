"""Keeps one Google spreadsheet per brand in step with the catalogue data.

One service account serves every brand. For each brand the tool makes sure these tabs
exist, creating any that are missing:
  Products   one row per product (matched by product_id)
  Variants   one row per size/colour (matched by variant_id)
  Changes    every detected change, appended once
  Status     whether the catalogue can be trusted right now, and when it last synced

Rules that protect people's work
  * The tool owns only the columns it knows by name. Any column you add (Notes, Owner,
    anything) is never read, overwritten or reordered. Columns may sit anywhere.
  * Rows are matched by id, never by position, so sorting or filtering is safe.
  * Existing rows are updated in place; new products go to the bottom.
  * A product that leaves the catalogue keeps its row and is marked, never deleted.
  * Only cells whose value actually changed are written, so a quiet day writes almost
    nothing (just the Status tab).
Missing auto columns are added to the right of the existing ones.
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import gspread
import requests
import yaml
from gspread.utils import ValueInputOption, ValueRenderOption

from .export import PRODUCT_COLUMNS, VARIANT_COLUMNS, product_rows, variant_rows
from . import benchmarks as bench
from . import health as health_mod
from . import ideabank as ib
from .collectors import collected_rows
from .research import research_sources
from .sync import catalogue_state, describe_event

PRODUCTS, VARIANTS, CHANGES, STATUS = "Products", "Variants", "Changes", "Status"
RESEARCH = "Research"
COLLECTED = "Collected"
BENCHMARKS = "Benchmarks"
COLLECTOR_HEALTH = "Collector Health"
IDEABANK, REQUESTS, BRIEFS, REJECTED = "Idea Bank", "Requests", "Briefs", "Rejected"
REQUEST_COLUMNS = ["request_id", "created", "status", "text", "region", "formats", "tools", "plan", "items_added", "run_notes"]
BRIEF_COLUMNS = ["brief_id", "created", "brief", "ask", "status", "items", "violations", "models", "drive_link"]
DECISIONS = ("Use", "Maybe", "Skip")
BENCHMARK_COLUMNS = ["name", "kind", "status", "page_id", "page_name", "page_followers", "reason", "candidates",
                     "checked_at", "source"]
CHANGE_COLUMNS = ["event_id", "at", "type", "product_id", "title", "handle", "summary"]
RESEARCH_COLUMNS = ["source_id", "title", "url", "domain", "published", "first_seen", "last_seen",
                    "times_seen", "providers", "purposes", "queries", "best_rank", "snippet"]
COLLECTED_COLUMNS = ["item_id", "source", "handle", "title", "text", "url", "posted_at", "likes",
                     "comments", "shares", "views", "saves", "reach", "days_running", "variants",
                     "engagement_pct", "signal", "media_url", "details", "first_seen", "last_seen", "times_seen", "queries"]
STARTER_MANUAL = {PRODUCTS: ["Notes"], RESEARCH: ["Review"], COLLECTED: ["Review"], BENCHMARKS: ["Review"],
                  IDEABANK: ["Decision", "Notes"], REQUESTS: ["Changes"], BRIEFS: ["Feedback"], REJECTED: ["Rescue"]}   # added only when a tab is brand new
STAMP = "last_updated"                          # when the tool last changed that row

NUMERIC = {"price_min", "price_max", "price", "compare_at_price", "engagement_pct", "success_rate_7d", "avg_items", "multiple", "trend_multiple", "views_per_day", "spend_this_month_usd"}
INTEGER = {"variant_count", "image_count", "times_seen", "best_rank", "likes", "comments", "shares", "attempts_7d", "acceptance_items",
           "views", "saves", "reach", "days_running", "variants", "page_followers", "score", "fit", "idea", "assets", "violations", "items_added",
           "products", "collections", "on_sale_products", "events_7d", "median", "approved_models", "requests_today", "daily_limit", "left_today", "requests_this_month",
           "failures_this_month", "tokens_today"}
BOOLEAN = {"excluded", "in_stock", "on_sale", "available"}

_BLANK_TABS = ("Sheet1", "Sheet 1")
_RETRY_CODES = {429, 500, 502, 503}
_CHUNK = 400
_SLEEP = time.sleep


class SheetsError(Exception):
    """A problem a person can fix (sharing, a missing key, a renamed id column)."""


class DriveQuotaError(SheetsError):
    """Google refused because the service account has no Drive storage of its own."""


# ---------------------------------------------------------------------------------------
# configuration and connection

def _extract_id(value):
    value = (value or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def load_sheet_config(path, known_slugs):
    """sheets.yaml maps a brand to an existing spreadsheet id (or its full URL). Optional."""
    path = Path(path)
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    extra = set(raw) - {"sheets"}
    if extra:
        raise SheetsError(f"{path}: unknown keys {sorted(extra)}")
    sheets = raw.get("sheets") or {}
    if not isinstance(sheets, dict):
        raise SheetsError(f"{path}: 'sheets' must be a list of brand: id pairs")
    out = {}
    for slug, value in sheets.items():
        if slug not in known_slugs:
            raise SheetsError(f"{path}: unknown brand {slug!r}. Known: {sorted(known_slugs)}")
        if value is not None and not isinstance(value, str):
            raise SheetsError(f"{path}: the id for {slug!r} must be text")
        out[slug] = _extract_id(value) or None
    return out


def parse_folder_id(text):
    """A Drive folder id, or the folder's full URL. Blank means none."""
    text = (text or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", text)
    return (match.group(1) if match else text) or None


def parse_emails(text):
    return [e.strip() for e in (text or "").replace(";", ",").split(",") if e.strip()]


def connect(key_json):
    """Return (client, service_account_email). Never echoes the key."""
    try:
        info = json.loads(key_json)
    except (TypeError, ValueError) as exc:
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. "
                          "Paste the whole key file into the secret.") from exc
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key "
                          "(it needs client_email and private_key).")
    return gspread.service_account_from_dict(info), info["client_email"]


def _is_storage_quota(exc):
    text = (str(exc) + " " + str(getattr(exc, "error", ""))).lower()
    return getattr(exc, "code", None) == 403 and ("storage quota" in text or "storagequotaexceeded" in text)


def _api_message(exc):
    code = getattr(exc, "code", None)
    text = str(exc)
    hint = ""
    if code in (403, 404):
        hint = (" Check the spreadsheet is shared with the service account as Editor, and that "
                "the Google Sheets API (and the Google Drive API, for auto-created sheets) is "
                "enabled for its Google Cloud project.")
    return f"Google said: {text}.{hint}"


# Google allows 60 write requests a minute per user. Waiting a few seconds after a 429 only fails again: the allowance comes back with the
# minute. So writes are paced to stay under it, and a 429 that happens anyway is waited out for a whole quota window.
WRITES_PER_MINUTE = 45
_WRITES = {"update", "batch_update", "append_rows", "delete_rows", "insert_rows", "resize", "freeze", "format", "clear", "batch_clear",
           "update_title", "add_worksheet", "share", "create"}
_RATE_WAITS = (20, 65, 65, 65, 65)            # seconds before each retry of a refused READ (a refused write waits out a full minute instead)
_TEMP_WAITS = (2, 4, 8, 16, 32)               # and of a temporary error (500, 502, 503)


class WritePacer:
    """Waits before the write that would be one too many in a minute. After a pause it counts the pause as time passed, so a no-op sleep in
    a test still moves on instead of waiting for ever."""

    def __init__(self, limit=WRITES_PER_MINUTE, window=60.0, clock=time.monotonic):
        self.limit, self.window, self.clock, self.skew, self.stamps = limit, window, clock, 0.0, []

    def _now(self):
        return self.clock() + self.skew

    def wait(self):
        now = self._now()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) >= self.limit:
            pause = self.window - (now - self.stamps[0]) + 1
            _SLEEP(pause)
            self.skew += pause
            now = self._now()
            self.stamps = [t for t in self.stamps if now - t < self.window]
        self.stamps.append(now)

    def spent(self):
        """Google refused a write for quota: this minute's allowance is gone, whatever we counted."""
        self.stamps = [self._now()] * self.limit


_PACER = WritePacer()


def _call(fn, *args, **kwargs):
    """Call the Google API, pacing writes under the quota and retrying rate limits and temporary errors."""
    write = getattr(fn, "__name__", "") in _WRITES
    attempt = 0
    while True:
        if write:
            _PACER.wait()
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            if _is_storage_quota(exc):
                raise DriveQuotaError(
                    "Google says the service account has no Drive storage left, so it cannot "
                    "create files. Use spreadsheets created in your own Drive instead.") from exc
            code = getattr(exc, "code", None)
            waits = _RATE_WAITS if code == 429 else _TEMP_WAITS
            if code not in _RETRY_CODES or attempt >= len(waits):
                raise SheetsError(_api_message(exc)) from exc
            if code == 429 and write:
                _PACER.spent()                    # the pacer's next wait is a whole minute, which is what a refused write needs
            else:
                _SLEEP(waits[attempt])            # a refused read, or a temporary error
            attempt += 1


# ---------------------------------------------------------------------------------------
# values

def _plain(value):
    """A value from the observatory as the text the typing step expects: true/false in lower case, nothing as empty."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value


def _observatory_tabs(brand, store, now, report):
    """The competitor tabs, or none. A problem here is written in the run's notes and never stops the rest of the sheet."""
    try:
        from .observatory import report as obs_report
        from .observatory.snapshots import SnapshotStore
        from . import services
        return obs_report.sheet_tabs(brand, store, SnapshotStore(store.data_dir), now, services.load_settings(None, store.data_dir))
    except ImportError:
        return []
    except Exception as exc:
        report.notes.append(f"Competitor tabs skipped: {exc}")
        return []


def _read_competitor_decisions(spreadsheet, brand, store, now, report, dry_run):
    """Before the tabs are rewritten, read what a person typed in the Competitors tab's Decision column (monitor, stop, reset) and record it. Never stops the sheet update."""
    try:
        from .observatory import report as obs_report
    except ImportError:
        return
    try:
        if "Competitors" not in [w.title for w in _call(spreadsheet.worksheets)]:         # through _call: a refused read (429) is waited out and retried, never given up on
            return
        values = _call(_call(spreadsheet.worksheet, "Competitors").get_all_values)
        n = obs_report.apply_sheet_decisions(values, store, brand.slug, now, save=not dry_run)
        report.notes.append(f"Competitor decisions read from the sheet: {n} (Competitors tab: {max(0, len(values) - 1)} row(s) read)")
    except Exception as exc:
        report.notes.append(f"Competitor decisions not read: {exc}")


def _read_controls(spreadsheet, brand, store, now, report, dry_run):
    """Read the values a person changed in the Controls tab (on/off, numbers, text) and record them, before the tab is rewritten. Never stops the sheet update."""
    if spreadsheet is None:                                      # a dry run for a sheet that does not exist yet has nothing to read
        return
    try:
        from . import services
        if services.CONTROLS_TAB not in [w.title for w in _call(spreadsheet.worksheets)]:
            return
        settings = services.load_settings(None, store.data_dir)
        values = _call(_call(spreadsheet.worksheet, services.CONTROLS_TAB).get_all_values)
        changes, problems = services.apply_sheet_controls(values, settings, store.data_dir, now, save=not dry_run, sheet_id=brand.slug)
        report.notes.append(f"Controls read from the sheet: {len(changes)} change(s) ({max(0, len(values) - 1)} row(s) read)" + (": " + "; ".join(changes) if changes else ""))
        for p in problems:
            report.notes.append(f"Controls: {p}; the value was left as it was")
    except Exception as exc:
        report.notes.append(f"Controls not read: {exc}")


def _controls_tabs(brand, store, now, report):
    """The Controls tab: the platform's shared-service settings, editable in the sheet. Never stops the rest of the sheet."""
    try:
        from . import services
        return [services.sheet_tab(services.load_settings(None, store.data_dir), store.data_dir, now, sheet_id=brand.slug)]
    except Exception as exc:
        report.notes.append(f"Controls tab skipped: {exc}")
        return []


def _connection_tabs(store, now, report):
    """The Connections Health tab. Never stops the rest of the sheet."""
    try:
        from . import connections, services
        import os as _os
        _os.environ.setdefault("CONTENTINTEL_DATA_DIR", str(store.data_dir))
        return [connections.sheet_tab(services.load_settings(None, store.data_dir), now)]
    except Exception as exc:
        report.notes.append(f"Connections Health tab skipped: {exc}")
        return []


def _ai_tabs(store, now, report):
    """The AI Gateway Health tab, or none (no AI layer files, or nothing switched on or used). Never stops the rest of the sheet."""
    try:
        from .ai import registry as ai_registry
        from .ai import sheetrows as ai_rows
        from .ai import usage as ai_usage
        from pathlib import Path as _Path
        if not _Path("ai_providers.yaml").is_file():
            return []
        spec = ai_rows.tab(ai_registry.load_registry("ai_providers.yaml"), ai_usage.UsageLedger(_Path(store.data_dir) / "_shared" / "ai_usage.json"), store.read_health(), now)
        return [spec] if spec else []
    except ImportError:
        return []
    except Exception as exc:
        report.notes.append(f"AI Gateway Health tab skipped: {exc}")
        return []


def _typed(column, text):
    """The export rows are text. Sheets gets numbers and true/false where they belong."""
    if text is None or text == "":
        return ""
    text = str(text)
    if column in NUMERIC:
        try:
            return float(text)
        except ValueError:
            return text
    if column in INTEGER:
        try:
            return int(text)
        except ValueError:
            return text
    if column in BOOLEAN:
        return text == "true"
    return text


def _canon(value):
    if value is None or value == "":
        return ("s", "")
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float)):
        return ("n", float(value))
    return ("s", str(value))


def _same(a, b):
    return _canon(a) == _canon(b)


def _col_letter(n):
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _a1(row, col):
    return f"{_col_letter(col)}{row}"


def event_id(event):
    blob = json.dumps({k: event.get(k) for k in ("at", "type", "product_id", "details")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------
# report

@dataclass
class SheetReport:
    brand: str
    url: str = ""
    created_spreadsheet: bool = False
    tabs_created: list = field(default_factory=list)
    columns_added: dict = field(default_factory=dict)
    rows_updated: int = 0
    cells_updated: int = 0
    rows_added: int = 0
    rows_by_tab: dict = field(default_factory=dict)
    rows_marked_removed: int = 0
    events_added: int = 0
    notes: list = field(default_factory=list)
    dry_run: bool = False

    def _where(self):
        """' (Rejected 427)': which tabs the added rows went to, so a big number is never a mystery."""
        return " (" + ", ".join(f"{tab} {n}" for tab, n in self.rows_by_tab.items()) + ")" if self.rows_by_tab else ""

    def lines(self):
        out = [f"{self.brand}: {self.url or '(no spreadsheet yet)'}"]
        if self.dry_run:
            if self.created_spreadsheet:
                out.append("  would create a new spreadsheet")
            if self.tabs_created:
                out.append(f"  would create tabs: {', '.join(self.tabs_created)}")
            for tab, cols in self.columns_added.items():
                out.append(f"  would add columns to {tab}: {', '.join(cols)}")
            out.append(f"  would update {self.rows_updated} rows ({self.cells_updated} cells), "
                       f"add {self.rows_added} rows{self._where()}, mark {self.rows_marked_removed} as removed, "
                       f"add {self.events_added} change entries")
        else:
            if self.created_spreadsheet:
                out.append("  created a new spreadsheet")
            if self.tabs_created:
                out.append(f"  created tabs: {', '.join(self.tabs_created)}")
            for tab, cols in self.columns_added.items():
                out.append(f"  added columns to {tab}: {', '.join(cols)}")
            out.append(f"  updated {self.rows_updated} rows ({self.cells_updated} cells), "
                       f"added {self.rows_added} rows{self._where()}, marked {self.rows_marked_removed} as removed, "
                       f"new change entries: {self.events_added}")
        out.extend(f"  note: {n}" for n in self.notes)
        return out


# ---------------------------------------------------------------------------------------
# spreadsheet and tabs

def _ensure_shared(sp, store, slug, emails, info):
    """Spreadsheets the tool created are shared with the configured people; retried if needed."""
    done = set(info.get("shared_with", []))
    for email in emails:
        if email not in done:
            _call(sp.share, email, perm_type="user", role="writer", notify=False)
            done.add(email)
            info = {**info, "shared_with": sorted(done)}
            store.save_sheet_info(slug, info)
    return info


def resolve_spreadsheet(client, brand, store, config, emails, report, dry_run, bot_email,
                        folder_id=None):
    saved = store.load_sheet_info(brand.slug) or {}
    configured = config.get(brand.slug)
    sheet_id = configured or saved.get("spreadsheet_id")
    if sheet_id:
        try:
            sp = _call(client.open_by_key, sheet_id)
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetsError(
                f"Spreadsheet {sheet_id} was not found. Share it with {bot_email} as Editor "
                f"(or remove the id from sheets.yaml to have a new one created).") from exc
        info = dict(saved)
        if info.get("spreadsheet_id") != sheet_id:
            info = {"spreadsheet_id": sheet_id, "url": sp.url, "created_by_tool": False}
            if not dry_run:
                store.save_sheet_info(brand.slug, info)
        report.url = sp.url
        if info.get("created_by_tool") and not dry_run:
            _ensure_shared(sp, store, brand.slug, emails, info)
        return sp
    if not emails:
        raise SheetsError(
            f"No spreadsheet is set up for {brand.slug}. Either paste its id into sheets.yaml "
            f"(and share it with {bot_email}), or add the SHEET_SHARE_EMAIL secret so a new "
            f"one can be created and shared with you.")
    report.created_spreadsheet = True
    if dry_run:
        return None
    try:
        sp = _call(client.create, f"{brand.name} catalogue (auto-updated)", folder_id=folder_id)
    except DriveQuotaError as exc:
        raise SheetsError(
            f"Google will not let the service account create spreadsheets (it has no Drive "
            f"storage of its own). Create a blank spreadsheet for {brand.name} in your own "
            f"Google Drive, share it with {bot_email} as Editor, and paste its link next to "
            f"'{brand.slug}' in sheets.yaml. Alternatively set the SHEET_FOLDER_ID secret to a "
            f"Shared Drive folder the service account can add files to.") from exc
    info = {"spreadsheet_id": sp.id, "url": sp.url, "created_by_tool": True, "shared_with": []}
    store.save_sheet_info(brand.slug, info)
    report.url = sp.url
    _ensure_shared(sp, store, brand.slug, emails, info)
    return sp


def _is_blank(ws):
    values = _call(ws.get_all_values)
    return not any(str(c).strip() for row in values for c in row)


def _ensure_tab(sp, title, report, dry_run, created):
    """Return the worksheet called `title`, creating it if it is missing."""
    if sp is None:                                    # dry run, spreadsheet does not exist yet
        report.tabs_created.append(title)
        return None
    sheets = _call(sp.worksheets)
    for ws in sheets:
        if ws.title == title:
            return ws
    report.tabs_created.append(title)
    if (title == PRODUCTS and len(sheets) == 1 and sheets[0].title in _BLANK_TABS
            and _is_blank(sheets[0])):                # reuse the empty starter tab
        if dry_run:
            return None
        _call(sheets[0].update_title, title)
        created[title] = sheets[0]
        return sheets[0]
    if dry_run:
        return None
    ws = _call(sp.add_worksheet, title=title, rows=1000, cols=30)
    created[title] = ws
    return ws


def _prune_rows(spreadsheet, ws, key_col, remove_keys, protect=("Decision", "Notes")):
    """Delete the rows of removed items. A row where a person wrote a Decision or Notes is never deleted. All the deletions go to Google in
    ONE request (neighbouring rows as one range, last range first so the row numbers stay right): a request per row is what hit
    Google's write quota when a tidy removed 137 rows. Returns (rows deleted, rows kept because a person had written on them)."""
    values = _call(ws.get_all_values)
    header = [str(h) for h in (values[0] if values else [])]
    if key_col not in header:
        return 0, 0
    key_at = header.index(key_col)
    written = [header.index(c) for c in protect if c in header]
    doomed, kept = [], 0
    for row_no, row in enumerate(values[1:], start=2):
        key = str(row[key_at]).strip() if key_at < len(row) else ""
        if key in remove_keys:
            if any(i < len(row) and str(row[i]).strip() for i in written):
                kept += 1
            else:
                doomed.append(row_no)
    ranges = []                                              # [first, last] of neighbouring rows, 1-based
    for row_no in doomed:
        if ranges and ranges[-1][1] == row_no - 1:
            ranges[-1][1] = row_no
        else:
            ranges.append([row_no, row_no])
    requests = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": first - 1, "endIndex": last}}}
                for first, last in reversed(ranges)]
    for start in range(0, len(requests), 100):
        _call(spreadsheet.batch_update, {"requests": requests[start:start + 100]})
    return len(doomed), kept


def _decision_dropdown(sp, ws):
    """A Use / Maybe / Skip list on the Decision column, so a decision is picked, not typed. Only when the tab is new."""
    header = _call(ws.get_all_values)[0]
    if "Decision" not in header:
        return
    col = header.index("Decision")
    rule = {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": d} for d in DECISIONS]}, "showCustomUi": True, "strict": False}
    _call(sp.batch_update, {"requests": [{"setDataValidation": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 5000, "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": rule}}]})


def request_sheet_rows(requests):
    """The Requests tab: one row per request, with a one-line account of its plan."""
    from .planner import plan_summary
    rows = {}
    for r in requests:
        run = r.get("run") or {}
        rows[r["id"]] = {"request_id": r["id"], "created": r.get("created", ""), "status": r.get("status", ""), "text": r.get("text", ""),
                         "region": r.get("region", ""), "formats": ", ".join(r.get("formats") or []),
                         "tools": ", ".join(((r.get("plan") or {}).get("tools")) or r.get("allowed") or []), "plan": plan_summary(r.get("plan") or {}),
                         "items_added": len(run.get("items_added") or []), "run_notes": " | ".join((run.get("notes") or [])[:4])}
    return rows


def brief_sheet_rows(briefs):
    rows = {}
    for b in briefs:
        rows[b["id"]] = {"brief_id": b["id"], "created": b.get("created", ""), "brief": b.get("brief", ""), "ask": b.get("ask", ""),
                         "status": b.get("status", ""), "items": ", ".join(b.get("items") or []), "violations": len(b.get("violations") or []),
                         "models": ", ".join(b.get("models") or []), "drive_link": b.get("drive_link", "")}
    return rows


def _style_header(ws):
    _call(ws.freeze, rows=1)
    _call(ws.format, "1:1", {"textFormat": {"bold": True}})


def _sync_table(ws, *, tab, columns, rows_by_key, key_col, report, dry_run,
                starter_manual=(), update_existing=True, removed=None, stamp=None):
    """Bring one tab in line with `rows_by_key` without touching columns the tool doesn't own."""
    values = _call(ws.get_all_values, value_render_option=ValueRenderOption.unformatted) \
        if ws is not None else []
    header = [str(h) if h is not None else "" for h in (values[0] if values else [])]
    while header and header[-1] == "":
        header.pop()
    has_data = any(str(c).strip() for row in values for c in row)
    if has_data and key_col not in header:
        raise SheetsError(f"The {tab} tab has content but no '{key_col}' column, so rows cannot "
                          f"be matched. Nothing was written to it. Restore the header or "
                          f"delete the tab and run again.")
    new_header = list(header)
    added = []
    for col in columns:
        if col not in new_header:
            new_header.append(col)
            added.append(col)
    stamp_name, stamp_value = stamp if stamp else (None, None)
    if stamp_name and stamp_name not in new_header:
        new_header.append(stamp_name)
        added.append(stamp_name)
    if not has_data:
        for col in starter_manual:
            if col not in new_header:
                new_header.append(col)
                added.append(col)
    if added and header:
        report.columns_added[tab] = added
    index = {}
    for i, name in enumerate(new_header):
        index.setdefault(name, i)
    key_idx = index[key_col]

    existing = {}
    for row_no, row in enumerate(values[1:], start=2):
        if key_idx < len(row):
            key = "" if row[key_idx] is None else str(row[key_idx])
            if key and key not in existing:
                existing[key] = (row_no, row)

    cell_updates = []
    touched_rows = set()
    to_append = []
    for key, desired in rows_by_key.items():
        if key in existing:
            if not update_existing:
                continue
            row_no, row = existing[key]
            for col in columns:
                ci = index[col]
                old = row[ci] if ci < len(row) else ""
                if not _same(old, desired[col]):
                    cell_updates.append((row_no, ci + 1, desired[col]))
                    touched_rows.add(row_no)
        else:
            to_append.append([desired[n] if n in columns
                              else (stamp_value if n == stamp_name else "")
                              for n in new_header])

    marked = 0
    marked_rows = set()
    if removed and update_existing:
        col_name, label_for = removed
        ci = index[col_name]
        for key, (row_no, row) in existing.items():
            if key not in rows_by_key:
                label = label_for(key)
                old = row[ci] if ci < len(row) else ""
                if not _same(old, label):
                    cell_updates.append((row_no, ci + 1, label))
                    marked_rows.add(row_no)
                    marked += 1

    # Stamp every row the tool changed, and any row whose stamp is blank (older rows get the
    # time of the first run after the column appeared).
    stamp_cells = []
    if stamp_name and update_existing:
        si = index[stamp_name]
        for key, (row_no, row) in existing.items():
            old = row[si] if si < len(row) else ""
            if row_no in touched_rows or row_no in marked_rows or str(old).strip() == "":
                stamp_cells.append((row_no, si + 1, stamp_value))

    report.rows_updated += len(touched_rows)
    report.cells_updated += len(cell_updates) - marked
    report.rows_added += len(to_append)
    if to_append:
        report.rows_by_tab[tab] = report.rows_by_tab.get(tab, 0) + len(to_append)
    report.rows_marked_removed += marked
    if dry_run or ws is None:
        return

    need_rows = len(values) + len(to_append) + 1
    need_cols = len(new_header)
    if ws.row_count < need_rows or ws.col_count < need_cols:
        _call(ws.resize, rows=max(ws.row_count, need_rows + 200),
              cols=max(ws.col_count, need_cols + 2))
    if new_header != header:
        _call(ws.update, values=[new_header], range_name=f"A1:{_a1(1, len(new_header))}")
    writes = cell_updates + stamp_cells
    for i in range(0, len(writes), _CHUNK):
        chunk = [{"range": _a1(r, c), "values": [[v]]} for r, c, v in writes[i:i + _CHUNK]]
        _call(ws.batch_update, chunk)
    for i in range(0, len(to_append), _CHUNK):
        _call(ws.append_rows, to_append[i:i + _CHUNK],
              value_input_option=ValueInputOption.raw, table_range="A1")


def _status_rows(brand, store, latest, registry, now):
    state = catalogue_state(brand.slug, store, now)
    records = (latest or {}).get("products", {})
    needs = sorted(r["title"] for r in records.values() if r["readiness"]["state"] == "needs_facts")
    gone = sorted(e["title"] for e in registry.values() if e["presence"] == "suspected_removed")
    notes = "; ".join(state["reasons"]) or "None"
    return [
        ["Brand", brand.name],
        ["Catalogue usable for generation", "Yes" if state["usable"] else "NO"],
        ["Notes", notes],
        ["Last good sync (UTC)", state.get("last_good_run_at") or "never"],
        ["Products in catalogue", len(records) if latest else ""],
        ["Products needing facts", f"{len(needs)}: {', '.join(needs)}" if needs else 0],
        ["Suspected removals", f"{len(gone)}: {', '.join(gone)}" if gone else 0],
        ["This sheet updated (UTC)", now],
        ["Source of truth", "The data folder in the GitHub repository. This sheet is a copy: "
                            "use only the extra columns you added."],
    ]


def update_brand_sheet(brand, store, spreadsheet, now, dry_run=False, report=None, health_rows=None):
    """Sync one brand's spreadsheet. Returns a SheetReport."""
    report = report or SheetReport(brand=brand.slug, dry_run=dry_run)
    report.dry_run = dry_run
    if spreadsheet is not None and not report.url:
        report.url = spreadsheet.url
    latest = store.load_latest(brand.slug)
    registry = store.load_registry(brand.slug)
    records = (latest or {}).get("products", {})
    created = {}

    ws = _ensure_tab(spreadsheet, PRODUCTS, report, dry_run, created)
    rows = {r["product_id"]: {c: _typed(c, r[c]) for c in PRODUCT_COLUMNS}
            for r in product_rows(records)}

    def product_label(key):
        presence = (registry.get(key) or {}).get("presence")
        return presence if presence in ("suspected_removed", "confirmed_removed") else "not_in_feed"
    _sync_table(ws, tab=PRODUCTS, columns=PRODUCT_COLUMNS, rows_by_key=rows, key_col="product_id",
                report=report, dry_run=dry_run, starter_manual=STARTER_MANUAL[PRODUCTS],
                removed=("status", product_label), stamp=(STAMP, now))

    ws = _ensure_tab(spreadsheet, VARIANTS, report, dry_run, created)
    rows = {str(r["variant_id"]): {c: _typed(c, r[c]) for c in VARIANT_COLUMNS}
            for r in variant_rows(records)}
    _sync_table(ws, tab=VARIANTS, columns=VARIANT_COLUMNS, rows_by_key=rows, key_col="variant_id",
                report=report, dry_run=dry_run, removed=("available", lambda key: "removed"),
                stamp=(STAMP, now))

    ws = _ensure_tab(spreadsheet, CHANGES, report, dry_run, created)
    events = {}
    for ev in store.read_changes(brand.slug):
        row = {"event_id": event_id(ev), "at": ev["at"], "type": ev["type"],
               "product_id": ev["product_id"], "title": ev["title"], "handle": ev["handle"],
               "summary": describe_event(ev)}
        events[row["event_id"]] = row
    before = report.rows_added
    _sync_table(ws, tab=CHANGES, columns=CHANGE_COLUMNS, rows_by_key=events, key_col="event_id",
                report=report, dry_run=dry_run)
    report.events_added = report.rows_added - before
    report.rows_added = before
    report.rows_by_tab.pop(CHANGES, None)                  # change entries are counted on their own

    sources = research_sources(store.read_searches(brand.slug))
    if sources:                                  # the tab appears once there is something to show
        ws = _ensure_tab(spreadsheet, RESEARCH, report, dry_run, created)
        rows = {sid: {c: _typed(c, row[c]) for c in RESEARCH_COLUMNS}
                for sid, row in sources.items()}
        _sync_table(ws, tab=RESEARCH, columns=RESEARCH_COLUMNS, rows_by_key=rows,
                    key_col="source_id", report=report, dry_run=dry_run,
                    starter_manual=STARTER_MANUAL[RESEARCH])

    items = collected_rows(store.read_collected(brand.slug))
    if items:                                    # ads, social posts and deep research reports
        ws = _ensure_tab(spreadsheet, COLLECTED, report, dry_run, created)
        rows = {iid: {c: _typed(c, "" if row[c] is None else row[c]) for c in COLLECTED_COLUMNS}
                for iid, row in items.items()}
        _sync_table(ws, tab=COLLECTED, columns=COLLECTED_COLUMNS, rows_by_key=rows,
                    key_col="item_id", report=report, dry_run=dry_run,
                    starter_manual=STARTER_MANUAL[COLLECTED])

    benchmark_state = bench.clean_state(store.read_benchmarks(brand.slug))
    if benchmark_state["items"]:                 # which advertiser page each benchmark brand means, and what needs a decision
        ws = _ensure_tab(spreadsheet, BENCHMARKS, report, dry_run, created)
        rows = {row["name"]: {c: _typed(c, row[c]) for c in BENCHMARK_COLUMNS}
                for row in bench.sheet_rows(benchmark_state).values()}
        _sync_table(ws, tab=BENCHMARKS, columns=BENCHMARK_COLUMNS, rows_by_key=rows, key_col="name",
                    report=report, dry_run=dry_run, starter_manual=STARTER_MANUAL[BENCHMARKS])

    if health_rows:                              # how each source and route is doing, and which have earned trust (the same on every brand's sheet)
        ws = _ensure_tab(spreadsheet, COLLECTOR_HEALTH, report, dry_run, created)
        rows = {r["route_key"]: {c: _typed(c, r[c]) for c in health_mod.SHEET_COLUMNS} for r in health_rows}
        _sync_table(ws, tab=COLLECTOR_HEALTH, columns=health_mod.SHEET_COLUMNS, rows_by_key=rows, key_col="route_key", report=report, dry_run=dry_run)

    _read_competitor_decisions(spreadsheet, brand, store, now, report, dry_run)
    _read_controls(spreadsheet, brand, store, now, report, dry_run)
    for tab in _observatory_tabs(brand, store, now, report) + _controls_tabs(brand, store, now, report) + _connection_tabs(store, now, report) + _ai_tabs(store, now, report):     # what the competitor observatory has found, and how the AI providers are doing
        ws = _ensure_tab(spreadsheet, tab["title"], report, dry_run, created)
        rows = {key: {c: _typed(c, _plain(row.get(c))) for c in tab["columns"]} for key, row in tab["rows_by_key"].items()}
        _sync_table(ws, tab=tab["title"], columns=tab["columns"], rows_by_key=rows, key_col=tab["key_col"], report=report, dry_run=dry_run,
                    starter_manual=tab["starter_manual"], stamp=(STAMP, now))

    bank = ib.normalise_bank(store.read_ideabank(brand.slug))
    shown = ib.sheet_rows(bank)
    gone = {i for i, it in bank["items"].items() if ib.state_of(it) != "kept"}      # copies, the brand's own, not relevant, or not scored yet
    ws = None
    if shown:                                    # items judged useful, waiting for a human decision
        ws = _ensure_tab(spreadsheet, IDEABANK, report, dry_run, created)
        rows = {iid: {c: _typed(c, row[c]) for c in ib.IDEABANK_COLUMNS} for iid, row in shown.items()}
        _sync_table(ws, tab=IDEABANK, columns=ib.IDEABANK_COLUMNS, rows_by_key=rows, key_col="idea_id", report=report,
                    dry_run=dry_run, starter_manual=STARTER_MANUAL[IDEABANK])
        if ws is not None and not dry_run and IDEABANK in created:
            _decision_dropdown(spreadsheet, ws)
    elif gone:
        try:
            ws = _call(spreadsheet.worksheet, IDEABANK)
        except gspread.exceptions.WorksheetNotFound:
            ws = None
    if ws is not None and gone and not dry_run:
        deleted, kept_rows = _prune_rows(spreadsheet, ws, "idea_id", gone)
        if deleted or kept_rows:
            report.notes.append(f"Idea Bank: removed {deleted} row(s) of items that were copies, the brand's own content or not relevant enough"
                                + (f"; kept {kept_rows} row(s) you had written a Decision or Notes on" if kept_rows else ""))

    rejected = ib.rejected_rows(bank)
    ws = None
    if rejected:                                 # what the tool removed and why, so its judgement can be checked and anything rescued
        ws = _ensure_tab(spreadsheet, REJECTED, report, dry_run, created)
        rows = {iid: {c: _typed(c, row[c]) for c in ib.REJECTED_COLUMNS} for iid, row in rejected.items()}
        _sync_table(ws, tab=REJECTED, columns=ib.REJECTED_COLUMNS, rows_by_key=rows, key_col="idea_id", report=report,
                    dry_run=dry_run, starter_manual=STARTER_MANUAL[REJECTED])
    elif spreadsheet is not None:
        try:
            ws = _call(spreadsheet.worksheet, REJECTED)
        except gspread.exceptions.WorksheetNotFound:
            ws = None
    back = {i for i, it in bank["items"].items() if it["state"] != "removed"}                   # rescued, or never removed: off this tab
    if ws is not None and back and not dry_run:
        _prune_rows(spreadsheet, ws, "idea_id", back, protect=("Rescue",))                      # a row with a Rescue mark written on it stays

    requests_made = store.list_requests(brand.slug)
    if requests_made:
        ws = _ensure_tab(spreadsheet, REQUESTS, report, dry_run, created)
        rows = {rid: {c: _typed(c, row[c]) for c in REQUEST_COLUMNS} for rid, row in request_sheet_rows(requests_made).items()}
        _sync_table(ws, tab=REQUESTS, columns=REQUEST_COLUMNS, rows_by_key=rows, key_col="request_id", report=report,
                    dry_run=dry_run, starter_manual=STARTER_MANUAL[REQUESTS])

    briefs_made = store.list_briefs(brand.slug)
    if briefs_made:
        ws = _ensure_tab(spreadsheet, BRIEFS, report, dry_run, created)
        rows = {bid: {c: _typed(c, row[c]) for c in BRIEF_COLUMNS} for bid, row in brief_sheet_rows(briefs_made).items()}
        _sync_table(ws, tab=BRIEFS, columns=BRIEF_COLUMNS, rows_by_key=rows, key_col="brief_id", report=report,
                    dry_run=dry_run, starter_manual=STARTER_MANUAL[BRIEFS])

    ws = _ensure_tab(spreadsheet, STATUS, report, dry_run, created)
    if ws is not None and not dry_run:
        _call(ws.clear)
        _call(ws.update, values=_status_rows(brand, store, latest, registry, now), range_name="A1")
    if not dry_run:
        for title, new_ws in created.items():
            if title != STATUS:
                _style_header(new_ws)
    return report


def has_spreadsheet(brand, store, config):
    """Is there already a spreadsheet for this brand (named in sheets.yaml, or recorded when the tool made one)? Reading must never make one."""
    return bool(config.get(brand.slug) or (store.load_sheet_info(brand.slug) or {}).get("spreadsheet_id"))


def fetch_decisions(brand, store, *, key_json, share_emails, config, folder_id=None, connect_fn=connect):
    """{idea id: {decision, notes}} as a person filled them in on the brand's Idea Bank tab."""
    client, bot_email = connect_fn(key_json)
    report = SheetReport(brand=brand.slug, dry_run=False)
    sp = resolve_spreadsheet(client, brand, store, config, share_emails, report, False, bot_email, folder_id=folder_id)
    try:
        ws = _call(sp.worksheet, IDEABANK)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise SheetsError("the brand's sheet has no Idea Bank tab yet: run the Sheets update first") from exc
    try:
        return ib.read_decisions(ws)
    except ib.IdeaBankError as exc:
        raise SheetsError(str(exc)) from exc


def fetch_rescues(brand, store, *, key_json, share_emails, config, folder_id=None, connect_fn=connect):
    """[idea id] a person marked Use or Maybe in the Rescue column of the brand's Rejected tab (none if there is no such tab yet)."""
    client, bot_email = connect_fn(key_json)
    report = SheetReport(brand=brand.slug, dry_run=False)
    sp = resolve_spreadsheet(client, brand, store, config, share_emails, report, False, bot_email, folder_id=folder_id)
    try:
        ws = _call(sp.worksheet, REJECTED)
    except gspread.exceptions.WorksheetNotFound:
        return []
    return ib.read_rescues(ws)


def run_sheets(brands, store, *, key_json, share_emails, config, now, dry_run=False,
               connect_fn=connect, folder_id=None, health_rows=None):
    """Update every given brand's sheet. Returns [(slug, SheetReport | None, error text | None)].

    One brand failing never stops the others. Returns None when no key is configured."""
    if not key_json:
        return None
    client, bot_email = connect_fn(key_json)
    results = []
    for brand in brands:
        report = SheetReport(brand=brand.slug, dry_run=dry_run)
        try:
            sp = resolve_spreadsheet(client, brand, store, config, share_emails, report,
                                     dry_run, bot_email, folder_id=folder_id)
            update_brand_sheet(brand, store, sp, now, dry_run=dry_run, report=report, health_rows=health_rows)
            results.append((brand.slug, report, None))
        except (SheetsError, gspread.exceptions.GSpreadException,
                requests.exceptions.RequestException) as exc:
            results.append((brand.slug, None, str(exc) or exc.__class__.__name__))
    return results
