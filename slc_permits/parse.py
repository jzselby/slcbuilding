"""Parse Accela Citizen Access (ACA) HTML into plain records.

Kept free of Playwright so it can be tested against saved HTML.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

# ACA column headers (they vary slightly by agency config) -> our field names.
HEADER_ALIASES = {
    "date": "date",
    "record number": "record_number",
    "permit number": "record_number",
    "record type": "record_type",
    "permit type": "record_type",
    "address": "address",
    "description": "description",
    "project name": "project_name",
    "status": "status",
    "expiration date": "expiration_date",
    "short notes": "short_notes",
}
IGNORED_HEADERS = {"", "action", "related records"}

RESULTS_TABLE_SELECTOR = "table[id$='gdvPermitList']"
NO_RESULTS_PATTERNS = ("no records found", "your search returned no results")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _field_name(header: str) -> str | None:
    key = _clean(header).lower()
    if key in IGNORED_HEADERS:
        return None
    return HEADER_ALIASES.get(key) or re.sub(r"[^a-z0-9]+", "_", key).strip("_")


def _is_data_row(row: Tag) -> bool:
    classes = " ".join(row.get("class", []))
    return "ACA_TabRow_Odd" in classes or "ACA_TabRow_Even" in classes


def _header_cells(table: Tag) -> list[str]:
    for row in table.find_all("tr"):
        classes = " ".join(row.get("class", []))
        if "ACA_TabRow_Header" in classes or row.find("th"):
            return [_clean(c.get_text(" ")) for c in row.find_all(["th", "td"])]
    return []


def parse_results(html: str, page_url: str) -> list[dict]:
    """Return one dict per record in the ACA search-results grid."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(RESULTS_TABLE_SELECTOR)
    if table is None:
        return []
    headers = [_field_name(h) for h in _header_cells(table)]
    records = []
    for row in table.find_all("tr"):
        if not _is_data_row(row):
            continue
        cells = row.find_all("td", recursive=False)
        record: dict = {}
        for name, cell in zip(headers, cells):
            if name:
                record[name] = _clean(cell.get_text(" "))
        link = row.find("a", href=re.compile(r"CapDetail\.aspx", re.I))
        if link:
            record["detail_url"] = urljoin(page_url, link["href"])
            record.setdefault("record_number", _clean(link.get_text(" ")))
        if record.get("record_number"):
            records.append(record)
    return records


def has_no_results_message(html: str) -> bool:
    text = BeautifulSoup(html, "lxml").get_text(" ").lower()
    return any(p in text for p in NO_RESULTS_PATTERNS)


def parse_detail_text(html: str) -> str:
    """Readable text of a CapDetail page's main content, without scripts/nav."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = (
        soup.select_one("#ctl00_PlaceHolderMain_PermitDetailList1")
        or soup.select_one("[id$='PlaceHolderMain']")
        or soup.select_one("#divMainContent")
        or soup.body
        or soup
    )
    lines = [_clean(line) for line in main.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def parse_detail_record_number(html: str) -> str | None:
    """Record number shown in a CapDetail page header ("Record BLD2026-01234: ...")."""
    soup = BeautifulSoup(html, "lxml")
    label = soup.select_one("[id$='lblPermitNumber']")
    if label:
        return _clean(label.get_text(" ")) or None
    match = re.search(r"Record\s+([A-Z]{2,}[\w-]*\d)\s*:", soup.get_text(" "))
    return match.group(1) if match else None


def parse_job_value(detail_text: str) -> float | None:
    """Declared job value from detail-page text ("Job Value: $1,250,000.00")."""
    match = re.search(r"Job Value\s*:?\s*\$?\s*([\d,]+(?:\.\d+)?)", detail_text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None
