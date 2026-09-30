"""Append new permits and each run's digest to a Google Sheet.

Authenticates as a Google Cloud service account; share the sheet with the
service account's email (Editor) so it can write.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import gspread

log = logging.getLogger(__name__)

PERMITS_TAB = "Permits"
DIGESTS_TAB = "Daily digests"
PERMIT_HEADERS = [
    "First seen", "Record", "Date opened", "Record type", "Address", "Scope",
    "Job value", "Applicant", "Contractor", "Status", "Notable", "Portal description",
]
DIGEST_HEADERS = ["Run date", "Window", "New records", "Summary"]
# Google Sheets' per-cell character limit.
MAX_CELL = 50_000


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


def permit_row(rec: dict, run_date: date) -> list:
    job_value = rec.get("job_value")
    return [
        run_date.isoformat(),
        hyperlink(rec.get("detail_url"), rec["record_number"]),
        text(rec.get("date")),
        text(rec.get("record_type")),
        text(rec.get("address")),
        text(rec.get("scope")),
        job_value if isinstance(job_value, (int, float)) else "",
        text(rec.get("applicant")),
        text(rec.get("contractor")),
        text(rec.get("status")),
        {True: "Yes", False: ""}.get(rec.get("notable"), ""),
        text(rec.get("description") or rec.get("project_name")),
    ]


def _worksheet(sh: gspread.Spreadsheet, title: str, headers: list[str]) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        pass
    # A brand-new spreadsheet has one empty "Sheet1"; take it over instead of leaving it blank.
    sheets = sh.worksheets()
    if len(sheets) == 1 and not sheets[0].get_all_values():
        ws = sheets[0]
        ws.update_title(title)
    else:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
    ws.update([headers], "A1")
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.freeze(rows=1)
    log.info("Created %r tab", title)
    return ws


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
    if records:
        rows = [permit_row(r, run_date) for r in sorted(records, key=lambda r: r["record_number"])]
        permits.append_rows(rows, value_input_option="USER_ENTERED", table_range="A1")
        log.info("Appended %d rows to %r", len(rows), PERMITS_TAB)

    digests = _worksheet(sh, DIGESTS_TAB, DIGEST_HEADERS)
    digests.append_rows(
        [[run_date.isoformat(), text(window), len(records), text((summary or "")[:MAX_CELL])]],
        value_input_option="USER_ENTERED",
        table_range="A1",
    )
