"""A strict in-memory stand-in for gspread, so the sheet sync can be tested without Google.

It is deliberately unforgiving, to catch the mistakes that only show up against the real API:
  * writing outside the grid raises, like Google's "Range exceeds grid limits";
  * reading without the unformatted option returns text ("99", "TRUE"), like the real API;
  * every method accepts only parameter names the real gspread method has (a test checks this).
"""
import re

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption


class FakeResponse:
    def __init__(self, code, message="error", status="ERROR"):
        self.status_code = code
        self.text = message
        self._body = {"error": {"code": code, "message": message, "status": status}}

    def json(self):
        return self._body


def api_error(code, message="boom"):
    return gspread.exceptions.APIError(FakeResponse(code, message))


def split_a1(ref):
    letters, row = re.fullmatch(r"([A-Z]+)(\d+)", ref).groups()
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch) - 64
    return int(row), col


def _formatted(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


class FakeWorksheet:
    def __init__(self, spreadsheet, title, rows=1000, cols=26):
        self.spreadsheet = spreadsheet
        self.title = title
        self.row_count = rows
        self.col_count = cols
        self.cells = {}                 # (row, col) -> value, 1-based
        self.frozen_rows = 0
        self.formats = []
        self.write_calls = []           # names of mutating calls, in order

    # -- helpers ---------------------------------------------------------------------
    def _put(self, row, col, value):
        if row > self.row_count or col > self.col_count:
            raise api_error(400, "Range exceeds grid limits")
        self.cells[(row, col)] = value

    def _live(self):
        return {k: v for k, v in self.cells.items() if v not in ("", None)}

    def _write_block(self, top, left, values):
        for r, row in enumerate(values):
            for c, value in enumerate(row):
                self._put(top + r, left + c, value)

    # -- the gspread surface the tool uses -------------------------------------------
    def get_all_values(self, range_name=None, value_render_option=None):
        live = self._live()
        if not live:
            return []
        max_r = max(r for r, _ in live)
        max_c = max(c for _, c in live)
        unformatted = value_render_option == ValueRenderOption.unformatted
        rows = []
        for r in range(1, max_r + 1):
            row = []
            for c in range(1, max_c + 1):
                value = self.cells.get((r, c), "")
                row.append("" if value is None else value if unformatted else _formatted(value))
            rows.append(row)
        return rows

    def col_values(self, col, value_render_option=ValueRenderOption.formatted):
        live = [r for (r, c), v in self._live().items() if c == col]
        return [_formatted(self.cells.get((r, col), "")) for r in range(1, max(live, default=0) + 1)]

    def update(self, values, range_name=None, raw=True):
        top, left = split_a1(range_name.split(":")[0]) if range_name else (1, 1)
        self._write_block(top, left, values)
        self.write_calls.append("update")

    def batch_update(self, data, raw=True):
        for item in data:
            top, left = split_a1(item["range"].split(":")[0])
            self._write_block(top, left, item["values"])
        self.write_calls.append("batch_update")

    def append_rows(self, values, value_input_option=ValueInputOption.raw, table_range=None):
        live = self._live()
        start = max((r for r, _ in live), default=0) + 1
        if start + len(values) - 1 > self.row_count:
            raise api_error(400, "Range exceeds grid limits")
        self._write_block(start, 1, values)
        self.write_calls.append("append_rows")

    def resize(self, rows=None, cols=None):
        if rows is not None:
            self.row_count = rows
        if cols is not None:
            self.col_count = cols
        self.write_calls.append("resize")

    def freeze(self, rows=None, cols=None):
        self.frozen_rows = rows or 0
        self.write_calls.append("freeze")

    def format(self, ranges, format):
        self.formats.append((ranges, format))
        self.write_calls.append("format")

    def clear(self):
        self.cells = {}
        self.write_calls.append("clear")

    def update_title(self, title):
        self.title = title
        self.write_calls.append("update_title")


class FakeSpreadsheet:
    def __init__(self, sheet_id, title="Untitled", blank_tab=True):
        self.id = sheet_id
        self.title = title
        self.url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        self._sheets = [FakeWorksheet(self, "Sheet1")] if blank_tab else []
        self.shares = []

    def worksheets(self, exclude_hidden=False):
        return list(self._sheets)

    def worksheet(self, title):
        for ws in self._sheets:
            if ws.title == title:
                return ws
        raise gspread.exceptions.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols, index=None):
        if any(ws.title == title for ws in self._sheets):
            raise api_error(400, f"A sheet with the name {title} already exists")
        ws = FakeWorksheet(self, title, rows, cols)
        self._sheets.append(ws)
        return ws

    def share(self, email_address, perm_type, role, notify=True, email_message=None, with_link=False):
        self.shares.append((email_address, perm_type, role, notify))


class FakeClient:
    def __init__(self, spreadsheets=()):
        self.spreadsheets = {s.id: s for s in spreadsheets}
        self.created = []
        self.create_folders = []

    def open_by_key(self, key):
        if key not in self.spreadsheets:
            raise gspread.exceptions.SpreadsheetNotFound("not found or not shared")
        return self.spreadsheets[key]

    def create(self, title, folder_id=None):
        self.create_folders.append(folder_id)
        sheet = FakeSpreadsheet(f"fake-{len(self.created) + 1}", title)
        self.spreadsheets[sheet.id] = sheet
        self.created.append(sheet)
        return sheet


# -- helpers that act like a person editing the sheet ------------------------------------

def read(ws):
    """(header, [row dicts]) using raw values."""
    values = ws.get_all_values(value_render_option=ValueRenderOption.unformatted)
    if not values:
        return [], []
    header = values[0]
    return header, [dict(zip(header, row)) for row in values[1:]]


def col_of(ws, name):
    header, _ = read(ws)
    return header.index(name) + 1


def last_row(ws):
    return max((r for r, _ in ws._live()), default=0)


def insert_column(ws, at, name, values=None):
    """Insert a new column at 1-based position `at`, like Insert column left in Sheets."""
    shifted = {}
    for (r, c), v in ws.cells.items():
        shifted[(r, c + 1 if c >= at else c)] = v
    ws.cells = shifted
    ws.col_count += 1
    ws.cells[(1, at)] = name
    for row_no, value in (values or {}).items():
        ws.cells[(row_no, at)] = value


def delete_column(ws, name):
    at = col_of(ws, name)
    ws.cells = {(r, c - 1 if c > at else c): v for (r, c), v in ws.cells.items() if c != at}


def reverse_data_rows(ws):
    """Sort the data rows into reverse order, like sorting a sheet."""
    last = last_row(ws)
    width = max((c for _, c in ws._live()), default=0)
    rows = [[ws.cells.get((r, c), "") for c in range(1, width + 1)] for r in range(2, last + 1)]
    for offset, row in enumerate(reversed(rows)):
        for c, value in enumerate(row, start=1):
            ws.cells[(2 + offset, c)] = value


def row_of(ws, key_col, key):
    c = col_of(ws, key_col)
    for (r, cc), v in ws.cells.items():
        if cc == c and r > 1 and str(v) == str(key):
            return r
    raise KeyError(key)
