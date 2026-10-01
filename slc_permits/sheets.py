"""Append new permits and each run's digest to a Google Sheet.

Authenticates as a Google Cloud service account; share the sheet with the
service account's email (Editor) so it can write.

Values are written by column heading, not position, so people can reorder
columns or add their own. Formatting is applied once per FORMAT_VERSION
(tracked in the sheet's developer metadata), so later manual formatting
isn't overwritten.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import gspread
from gspread.utils import rowcol_to_a1

log = logging.getLogger(__name__)

PERMITS_TAB = "Permits"
HIGH_TAB = "High importance"
DIGESTS_TAB = "Daily digests"
# Column order for a new tab; existing tabs keep whatever order they have.
PERMIT_HEADERS = [
    "First seen", "Record", "Date opened", "Record type", "Address", "Scope",
    "Job value", "Applicant", "Contractor", "Status", "Notable", "Portal description", "Module",
    "Importance", "Category", "Why it matters", "Business",
]
# The High importance tab: what a reporter scans first.
HIGH_HEADERS = [
    "Date opened", "Category", "Why it matters", "Address", "Job value", "Business",
    "Record", "Record type", "Status", "Scope", "Module", "First seen",
]
DIGEST_HEADERS = ["Run date", "Window", "New records", "Summary"]
# Google Sheets' per-cell character limit.
MAX_CELL = 50_000

FORMAT_KEY = "slc_permits_format"
# Bumping this re-applies the formatting on the next run. Conditional-format rules
# are added again rather than replaced, so delete the old ones in the sheet first.
FORMAT_VERSION = "1"


def text(value) -> str:
    """A cell value that Sheets will keep as literal text.

    Rows are written with USER_ENTERED so dates and numbers parse, which means
    scraped text starting with = + - @ would otherwise be run as a formula.
    """
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def hyperlink(url: str | None, label: str) -> str:
    if not url:
        return text(label)
    return '=HYPERLINK("{}", "{}")'.format(url.replace('"', '""'), label.replace('"', '""'))


def permit_values(rec: dict, run_date: date) -> dict[str, Any]:
    job_value = rec.get("job_value")
    return {
        "First seen": (rec.get("first_seen") or run_date.isoformat())[:10],
        "Record": hyperlink(rec.get("detail_url"), rec["record_number"]),
        "Date opened": text(rec.get("date")),
        "Record type": text(rec.get("record_type")),
        "Address": text(rec.get("address")),
        "Scope": text(rec.get("scope")),
        "Job value": job_value if isinstance(job_value, (int, float)) else "",
        "Applicant": text(rec.get("applicant")),
        "Contractor": text(rec.get("contractor")),
        "Status": text(rec.get("status")),
        "Notable": {True: "Yes", False: ""}.get(rec.get("notable"), ""),
        "Portal description": text(rec.get("description") or rec.get("project_name")),
        "Module": text(rec.get("module")),
        "Importance": (rec.get("importance") or "").capitalize(),
        "Category": text(rec.get("category")),
        "Why it matters": text(rec.get("why_it_matters")),
        "Business": text(rec.get("business")),
    }


def md_to_text(markdown: str) -> str:
    """Claude's Markdown digest as plain text that reads well in a cell."""
    lines = []
    for line in markdown.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line) or re.fullmatch(r"\s*\*\*([^*]+)\*\*\s*", line)
        if heading:  # "## Highlights" or a bold-only line like "**Highlights**"
            line = heading.group(1).strip().upper()
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        lines.append(line)
    return "\n".join(lines).strip()


def _col(headers: list[str], name: str) -> int:
    return headers.index(name)


def _letter(index: int) -> str:
    return rowcol_to_a1(1, index + 1).rstrip("1")


def _headers(ws: gspread.Worksheet, wanted: list[str]) -> tuple[list[str], list[str]]:
    """The tab's header row, adding any of our columns that are missing (at the end).

    Returns (headers, the columns just added).
    """
    current = ws.row_values(1)
    missing = [h for h in wanted if h not in current]
    if not missing:
        return current, []
    headers = current + missing
    if ws.col_count < len(headers):
        ws.add_cols(len(headers) - ws.col_count)
    ws.update([headers], "A1")
    log.info("Added %s column(s) to %r", ", ".join(missing), ws.title)
    if current and "Module" in missing:
        # Before the Module column existed, only the Building tab was searched.
        rows = len(ws.col_values(1))
        if rows > 1:
            ws.update([["Building"]] * (rows - 1), f"{_letter(_col(headers, 'Module'))}2")
    return headers, missing


def _worksheet(sh: gspread.Spreadsheet, title: str, headers: list[str]) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        pass
    # A brand-new spreadsheet has one empty "Sheet1"; take it over instead of leaving it blank.
    sheets = sh.worksheets()
    if len(sheets) == 1 and not any(any(row) for row in sheets[0].get_all_values()):
        ws = sheets[0]
        ws.update_title(title)
    else:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
    log.info("Created %r tab", title)
    return ws


# --- formatting --------------------------------------------------------------

def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


HEADER_BG, HEADER_FG = _rgb("#1F3864"), _rgb("#FFFFFF")
NOTABLE_BG = _rgb("#FFF2CC")
MILLION_BG, QUARTER_MILLION_BG = _rgb("#F9CB9C"), _rgb("#FCE5CD")
PLANNING_BG = _rgb("#DEEAF6")
DRAFT_FG = _rgb("#999999")

LOW_FG = _rgb("#999999")

DATE_FORMAT = {"type": "DATE", "pattern": "mmm d, yyyy"}
MONEY_FORMAT = {"type": "CURRENCY", "pattern": "$#,##0"}
# Per-column (width in pixels, wrap strategy, number format), shared by all tabs.
COLUMN_STYLE: dict[str, tuple[int, str | None, dict | None]] = {
    "First seen": (95, None, DATE_FORMAT),
    "Record": (125, None, None),
    "Date opened": (100, None, DATE_FORMAT),
    "Record type": (190, None, None),
    "Address": (250, None, None),
    "Scope": (380, "WRAP", None),
    "Job value": (105, None, MONEY_FORMAT),
    "Applicant": (210, None, None),
    "Contractor": (150, None, None),
    "Status": (110, None, None),
    "Notable": (75, None, None),
    "Portal description": (320, "CLIP", None),
    "Module": (85, None, None),
    "Importance": (95, None, None),
    "Category": (150, None, None),
    "Why it matters": (420, "WRAP", None),
    "Business": (180, "WRAP", None),
    "Run date": (95, None, DATE_FORMAT),
    "Window": (190, None, None),
    "New records": (95, None, None),
    "Summary": (900, "WRAP", None),
}


def _cells(sheet_id: int, col: int | None = None, header: bool = False) -> dict:
    rng: dict[str, Any] = {"sheetId": sheet_id}
    rng.update({"startRowIndex": 0, "endRowIndex": 1} if header else {"startRowIndex": 1})
    if col is not None:
        rng.update({"startColumnIndex": col, "endColumnIndex": col + 1})
    return rng


def _repeat(rng: dict, fmt: dict, fields: str) -> dict:
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _column_requests(sheet_id: int, headers: list[str], names: list[str]) -> list[dict]:
    """Width, wrapping and number format for the named columns."""
    reqs = []
    for name in names:
        if name not in headers or name not in COLUMN_STYLE:
            continue
        i = _col(headers, name)
        width, wrap, number = COLUMN_STYLE[name]
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})
        if wrap:
            reqs.append(_repeat(_cells(sheet_id, i), {"wrapStrategy": wrap}, "userEnteredFormat.wrapStrategy"))
        if number:
            reqs.append(_repeat(_cells(sheet_id, i), {"numberFormat": number}, "userEnteredFormat.numberFormat"))
    return reqs


def _common_requests(sheet_id: int, headers: list[str], frozen_cols: int) -> list[dict]:
    reqs = [
        _repeat(_cells(sheet_id, header=True),
                {"backgroundColor": HEADER_BG, "textFormat": {"bold": True, "foregroundColor": HEADER_FG},
                 "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"},
                "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"),
        _repeat(_cells(sheet_id), {"verticalAlignment": "TOP"}, "userEnteredFormat.verticalAlignment"),
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": frozen_cols}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
    ]
    return reqs + _column_requests(sheet_id, headers, headers)


def permit_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    """Sheets API requests that style the Permits tab for scanning."""
    has = set(headers).__contains__
    reqs = _common_requests(sheet_id, headers, frozen_cols=_col(headers, "Record") + 1 if has("Record") else 0)
    if has("Contractor"):  # SLC lists the contractor's company under Applicant; this is almost always empty
        i = _col(headers, "Contractor")
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})

    # Conditional formats. For each cell only the first matching rule applies, so order matters.
    whole_rows = [_cells(sheet_id)]
    rules = []
    if has("Job value"):
        value_col = [_cells(sheet_id, _col(headers, "Job value"))]
        rules += [
            (value_col, {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "1000000"}]},
             {"backgroundColor": MILLION_BG, "textFormat": {"bold": True}}),
            (value_col, {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "250000"}]},
             {"backgroundColor": QUARTER_MILLION_BG}),
        ]
    if has("Record"):  # unsubmitted drafts ("26TMP-...") are de-emphasized
        rules.append((whole_rows, _formula(f'=LEFT(${_letter(_col(headers, "Record"))}2,5)="26TMP"'),
                      {"textFormat": {"foregroundColor": DRAFT_FG}}))
    if has("Importance"):  # so are routine permits
        rules.append(_low_rule(sheet_id, headers))
    if has("Module"):
        planning_cols = [_cells(sheet_id, _col(headers, n)) for n in ("Module", "Record type") if has(n)]
        rules.append((planning_cols, _formula(f'=${_letter(_col(headers, "Module"))}2="Planning"'),
                      {"backgroundColor": PLANNING_BG}))
    if has("Notable"):
        rules.append((whole_rows, _formula(f'=${_letter(_col(headers, "Notable"))}2="Yes"'),
                      {"backgroundColor": NOTABLE_BG}))
    reqs += [_rule_request(rule, index) for index, rule in enumerate(rules)]

    reqs.append({"setBasicFilter": {"filter": {"range": {
        "sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(headers)}}}})
    return reqs


def _formula(expr: str) -> dict:
    return {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": expr}]}


def _low_rule(sheet_id: int, headers: list[str]) -> tuple:
    return ([_cells(sheet_id)], _formula(f'=${_letter(_col(headers, "Importance"))}2="Low"'),
            {"textFormat": {"foregroundColor": LOW_FG}})


def _rule_request(rule: tuple, index: int) -> dict:
    ranges, condition, fmt = rule
    return {"addConditionalFormatRule": {
        "rule": {"ranges": ranges, "booleanRule": {"condition": condition, "format": fmt}}, "index": index}}


def added_column_requests(sheet_id: int, headers: list[str], added: list[str]) -> list[dict]:
    """Styling for columns added to an already-formatted Permits tab."""
    reqs = _column_requests(sheet_id, headers, added)
    if "Importance" in added:
        reqs.append(_rule_request(_low_rule(sheet_id, headers), 0))
    return reqs


def high_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    reqs = _common_requests(sheet_id, headers, frozen_cols=0)
    if "Job value" in headers:
        value_col = [_cells(sheet_id, _col(headers, "Job value"))]
        reqs.append(_rule_request((value_col, {"type": "NUMBER_GREATER_THAN_EQ",
                                               "values": [{"userEnteredValue": "1000000"}]},
                                   {"backgroundColor": MILLION_BG, "textFormat": {"bold": True}}), 0))
    reqs.append({"setBasicFilter": {"filter": {"range": {
        "sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(headers)}}}})
    return reqs


def digest_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    return _common_requests(sheet_id, headers, frozen_cols=0)


def _format_version(sh: gspread.Spreadsheet, sheet_id: int) -> str | None:
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),developerMetadata)"})
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            for dm in sheet.get("developerMetadata", []):
                if dm.get("metadataKey") == FORMAT_KEY:
                    return dm.get("metadataValue")
    return None


def _format_once(sh: gspread.Spreadsheet, ws: gspread.Worksheet, requests: list[dict]) -> bool:
    """Apply formatting unless this tab already has the current FORMAT_VERSION."""
    version = _format_version(sh, ws.id)
    if version == FORMAT_VERSION:
        return False
    marker = {"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": FORMAT_KEY, "metadataValue": FORMAT_VERSION,
        "location": {"sheetId": ws.id}, "visibility": "DOCUMENT"}}}
    if version is not None:  # replace the old marker when upgrading
        requests = [{"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {
            "metadataKey": FORMAT_KEY, "metadataLocation": {"sheetId": ws.id}}}}}] + requests
    sh.batch_update({"requests": requests + [marker]})
    log.info("Formatted %r (format version %s)", ws.title, FORMAT_VERSION)
    return True


def _sort_newest_first(sh: gspread.Spreadsheet, ws: gspread.Worksheet, headers: list[str],
                       keys: tuple[str, ...] = ("First seen", "Date opened")) -> None:
    specs = [{"dimensionIndex": _col(headers, n), "sortOrder": "DESCENDING"} for n in keys if n in headers]
    if specs:
        sh.batch_update({"requests": [{"sortRange": {"range": {
            "sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(headers)},
            "sortSpecs": specs}}]})


# --- publishing --------------------------------------------------------------

def _upsert(ws: gspread.Worksheet, headers: list[str], records: list[dict], run_date: date) -> None:
    """Update rows already in the tab (only our columns, never "First seen"); append the rest."""
    rec_col = _col(headers, "Record")
    existing = {number: i + 1 for i, number in enumerate(ws.col_values(rec_col + 1)) if i > 0 and number}
    updates, appends = [], []
    for rec in sorted(records, key=lambda r: r["record_number"]):
        values = permit_values(rec, run_date)
        line = existing.get(rec["record_number"])
        if line is None:
            appends.append([values.get(h, "") for h in headers])
            continue
        for name, value in values.items():
            if name != "First seen" and name in headers:
                updates.append({"range": rowcol_to_a1(line, _col(headers, name) + 1), "values": [[value]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        rows = {re.sub(r"[A-Z]+", "", u["range"]) for u in updates}
        log.info("Updated %d existing rows in %r", len(rows), ws.title)
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED", table_range="A1")
        log.info("Appended %d rows to %r", len(appends), ws.title)


def _remove(ws: gspread.Worksheet, headers: list[str], record_numbers: set[str]) -> None:
    """Delete the rows for these records (e.g. no longer high importance after re-rating)."""
    numbers = ws.col_values(_col(headers, "Record") + 1)
    lines = [i + 1 for i, n in enumerate(numbers) if i > 0 and n in record_numbers]
    for line in sorted(lines, reverse=True):
        ws.delete_rows(line)
    if lines:
        log.info("Removed %d rows from %r", len(lines), ws.title)


def publish(
    sheet_id: str,
    service_account_json: str,
    records: list[dict],
    run_date: date,
    window: str,
    summary: str | None,
    digest_row: bool = True,
) -> None:
    gc = gspread.service_account_from_dict(json.loads(service_account_json))
    sh = gc.open_by_key(sheet_id)

    permits = _worksheet(sh, PERMITS_TAB, PERMIT_HEADERS)
    headers, added = _headers(permits, PERMIT_HEADERS)
    if records:
        _upsert(permits, headers, records, run_date)

    high = _worksheet(sh, HIGH_TAB, HIGH_HEADERS)
    high_headers, _ = _headers(high, HIGH_HEADERS)
    important = [r for r in records if r.get("importance") == "high"]
    if important:
        _upsert(high, high_headers, important, run_date)
    _remove(high, high_headers, {r["record_number"] for r in records if r.get("importance") != "high"})

    digests = _worksheet(sh, DIGESTS_TAB, DIGEST_HEADERS)
    digest_headers, _ = _headers(digests, DIGEST_HEADERS)
    if digest_row:
        digest_values = {"Run date": run_date.isoformat(), "Window": text(window), "New records": len(records),
                         "Summary": text(md_to_text(summary or "")[:MAX_CELL])}
        # Newest first: each run's digest goes directly under the header row.
        digests.insert_row([digest_values.get(h, "") for h in digest_headers], index=2,
                           value_input_option="USER_ENTERED")

    # Presentation only: a failure here must not lose the data written above.
    try:
        if not _format_once(sh, permits, permit_format_requests(permits.id, headers)) and added:
            sh.batch_update({"requests": added_column_requests(permits.id, headers, added)})
        _format_once(sh, high, high_format_requests(high.id, high_headers))
        if _format_once(sh, digests, digest_format_requests(digests.id, digest_headers)):
            _plain_text_summaries(digests, digest_headers)
        _sort_newest_first(sh, permits, headers)
        _sort_newest_first(sh, high, high_headers, ("Date opened", "First seen"))
    except Exception:
        log.exception("Formatting the sheet failed; the data was written")


def _plain_text_summaries(ws: gspread.Worksheet, headers: list[str]) -> None:
    """Convert digests written before summaries were stored as plain text."""
    if "Summary" not in headers:
        return
    col = _col(headers, "Summary") + 1
    cells = ws.col_values(col)[1:]
    converted = [md_to_text(c) for c in cells]
    if converted != cells:
        ws.update([[text(c)] for c in converted], f"{_letter(col - 1)}2", value_input_option="USER_ENTERED")
