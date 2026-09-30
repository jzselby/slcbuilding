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
DIGESTS_TAB = "Daily digests"
# Column order for a new tab; existing tabs keep whatever order they have.
PERMIT_HEADERS = [
    "First seen", "Record", "Date opened", "Record type", "Address", "Scope",
    "Job value", "Applicant", "Contractor", "Status", "Notable", "Portal description", "Module",
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
        "First seen": run_date.isoformat(),
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


def _headers(ws: gspread.Worksheet, wanted: list[str]) -> list[str]:
    """The tab's header row, adding any of our columns that are missing (at the end)."""
    current = ws.row_values(1)
    missing = [h for h in wanted if h not in current]
    if not missing:
        return current
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
    return headers


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

PERMIT_WIDTHS = {
    "First seen": 95, "Record": 125, "Date opened": 100, "Record type": 190, "Address": 250,
    "Scope": 380, "Job value": 105, "Applicant": 210, "Contractor": 150, "Status": 110,
    "Notable": 75, "Portal description": 320, "Module": 85,
}
DIGEST_WIDTHS = {"Run date": 95, "Window": 190, "New records": 95, "Summary": 900}


def _cells(sheet_id: int, col: int | None = None, header: bool = False) -> dict:
    rng: dict[str, Any] = {"sheetId": sheet_id}
    rng.update({"startRowIndex": 0, "endRowIndex": 1} if header else {"startRowIndex": 1})
    if col is not None:
        rng.update({"startColumnIndex": col, "endColumnIndex": col + 1})
    return rng


def _repeat(rng: dict, fmt: dict, fields: str) -> dict:
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _common_requests(sheet_id: int, headers: list[str], widths: dict[str, int], frozen_cols: int) -> list[dict]:
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
    for name, px in widths.items():
        if name in headers:
            i = _col(headers, name)
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    return reqs


def permit_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    """Sheets API requests that style the Permits tab for scanning."""
    has = set(headers).__contains__
    reqs = _common_requests(sheet_id, headers, PERMIT_WIDTHS,
                            frozen_cols=_col(headers, "Record") + 1 if has("Record") else 0)
    for name in ("First seen", "Date opened"):
        if has(name):
            reqs.append(_repeat(_cells(sheet_id, _col(headers, name)),
                                {"numberFormat": {"type": "DATE", "pattern": "mmm d, yyyy"}},
                                "userEnteredFormat.numberFormat"))
    if has("Job value"):
        reqs.append(_repeat(_cells(sheet_id, _col(headers, "Job value")),
                            {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}},
                            "userEnteredFormat.numberFormat"))
    if has("Scope"):
        reqs.append(_repeat(_cells(sheet_id, _col(headers, "Scope")), {"wrapStrategy": "WRAP"},
                            "userEnteredFormat.wrapStrategy"))
    if has("Portal description"):
        reqs.append(_repeat(_cells(sheet_id, _col(headers, "Portal description")), {"wrapStrategy": "CLIP"},
                            "userEnteredFormat.wrapStrategy"))
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
    if has("Module"):
        planning_cols = [_cells(sheet_id, _col(headers, n)) for n in ("Module", "Record type") if has(n)]
        rules.append((planning_cols, _formula(f'=${_letter(_col(headers, "Module"))}2="Planning"'),
                      {"backgroundColor": PLANNING_BG}))
    if has("Notable"):
        rules.append((whole_rows, _formula(f'=${_letter(_col(headers, "Notable"))}2="Yes"'),
                      {"backgroundColor": NOTABLE_BG}))
    for index, (ranges, condition, fmt) in enumerate(rules):
        reqs.append({"addConditionalFormatRule": {
            "rule": {"ranges": ranges, "booleanRule": {"condition": condition, "format": fmt}},
            "index": index}})

    reqs.append({"setBasicFilter": {"filter": {"range": {
        "sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(headers)}}}})
    return reqs


def _formula(expr: str) -> dict:
    return {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": expr}]}


def digest_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    reqs = _common_requests(sheet_id, headers, DIGEST_WIDTHS, frozen_cols=0)
    if "Run date" in headers:
        reqs.append(_repeat(_cells(sheet_id, _col(headers, "Run date")),
                            {"numberFormat": {"type": "DATE", "pattern": "mmm d, yyyy"}},
                            "userEnteredFormat.numberFormat"))
    if "Summary" in headers:
        reqs.append(_repeat(_cells(sheet_id, _col(headers, "Summary")), {"wrapStrategy": "WRAP"},
                            "userEnteredFormat.wrapStrategy"))
    return reqs


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


def _sort_newest_first(sh: gspread.Spreadsheet, ws: gspread.Worksheet, headers: list[str]) -> None:
    specs = [{"dimensionIndex": _col(headers, n), "sortOrder": "DESCENDING"}
             for n in ("First seen", "Date opened") if n in headers]
    if specs:
        sh.batch_update({"requests": [{"sortRange": {"range": {
            "sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(headers)},
            "sortSpecs": specs}}]})


# --- publishing --------------------------------------------------------------

def _write_permits(ws: gspread.Worksheet, headers: list[str], records: list[dict], run_date: date) -> None:
    # Records already in the sheet (e.g. a re-run to fill in Claude's notes) are
    # updated in place: only our columns, and never "First seen". The rest are appended.
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


def publish(
    sheet_id: str,
    service_account_json: str,
    records: list[dict],
    run_date: date,
    window: str,
    summary: str | None,
) -> None:
    gc = gspread.service_account_from_dict(json.loads(service_account_json))
    sh = gc.open_by_key(sheet_id)

    permits = _worksheet(sh, PERMITS_TAB, PERMIT_HEADERS)
    headers = _headers(permits, PERMIT_HEADERS)
    if records:
        _write_permits(permits, headers, records, run_date)

    digests = _worksheet(sh, DIGESTS_TAB, DIGEST_HEADERS)
    digest_headers = _headers(digests, DIGEST_HEADERS)
    digest_values = {"Run date": run_date.isoformat(), "Window": text(window), "New records": len(records),
                     "Summary": text(md_to_text(summary or "")[:MAX_CELL])}
    # Newest first: each run's digest goes directly under the header row.
    digests.insert_row([digest_values.get(h, "") for h in digest_headers], index=2,
                       value_input_option="USER_ENTERED")

    # Presentation only: a failure here must not lose the data written above.
    try:
        _format_once(sh, permits, permit_format_requests(permits.id, headers))
        if _format_once(sh, digests, digest_format_requests(digests.id, digest_headers)):
            _plain_text_summaries(digests, digest_headers)
        _sort_newest_first(sh, permits, headers)
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
