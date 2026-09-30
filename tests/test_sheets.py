import json
from datetime import date

import gspread
import pytest

from slc_permits import sheets


class FakeWorksheet:
    _ids = iter(range(100, 1000))

    def __init__(self, title, cols=26):
        self.id = next(self._ids)
        self.title = title
        self.values = []
        self.col_count = cols

    def row_values(self, n):
        return self.values[n - 1] if len(self.values) >= n else []

    def col_values(self, n):
        return [row[n - 1] if len(row) >= n else "" for row in self.values]

    def add_cols(self, n):
        self.col_count += n

    def get_all_values(self):
        return self.values

    def update_title(self, title):
        self.title = title

    def update(self, values, cell, value_input_option=None):
        col, row = ord(cell[0]) - ord("A"), int(cell[1:]) - 1  # single-letter columns suffice here
        assert col + max(len(v) for v in values) <= self.col_count, "exceeds grid limits"
        for i, v in enumerate(values):
            while len(self.values) <= row + i:
                self.values.append([])
            target = self.values[row + i]
            target.extend([""] * (col + len(v) - len(target)))
            target[col:col + len(v)] = v

    def insert_row(self, values, index, value_input_option):
        assert value_input_option == "USER_ENTERED"
        while len(self.values) < index - 1:
            self.values.append([])
        self.values.insert(index - 1, list(values))

    def batch_update(self, data, value_input_option):
        assert value_input_option == "USER_ENTERED"
        for item in data:
            self.update(item["values"], item["range"].split(":")[0])

    def append_rows(self, rows, value_input_option, table_range):
        assert value_input_option == "USER_ENTERED"
        self.values.extend(rows)


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = [FakeWorksheet("Sheet1")]
        self.requests = []  # Sheets API requests sent via batch_update

    def batch_update(self, body):
        self.requests += body["requests"]

    def fetch_sheet_metadata(self, params):
        markers = {}
        for r in self.requests:
            if "createDeveloperMetadata" in r:
                dm = r["createDeveloperMetadata"]["developerMetadata"]
                markers[dm["location"]["sheetId"]] = dm
        return {"sheets": [{"properties": {"sheetId": ws.id},
                            "developerMetadata": [markers[ws.id]] if ws.id in markers else []}
                           for ws in self.sheets]}

    def kinds(self, key):
        return [r for r in self.requests if key in r]

    def worksheets(self):
        return self.sheets

    def worksheet(self, title):
        for ws in self.sheets:
            if ws.title == title:
                return ws
        raise gspread.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title)
        self.sheets.append(ws)
        return ws


@pytest.fixture
def spreadsheet(monkeypatch):
    sh = FakeSpreadsheet()
    opened = {}

    class FakeClient:
        def open_by_key(self, key):
            opened["key"] = key
            return sh

    def from_dict(info):
        opened["creds"] = info
        return FakeClient()

    monkeypatch.setattr(sheets.gspread, "service_account_from_dict", from_dict)
    sh.opened = opened
    return sh


RECORD = {
    "record_number": "BLD2026-00001", "detail_url": 'http://x/CapDetail.aspx?id="1"', "date": "09/02/2026",
    "record_type": "Commercial Alteration", "address": "1 Main St", "scope": "Office remodel",
    "job_value": 125000.0, "contractor": "Acme", "status": "In Review", "notable": True,
    "description": "-demo interior walls", "module": "Planning",
}


def test_publish_sets_up_tabs_and_appends(spreadsheet):
    sheets.publish("sheet123", json.dumps({"type": "service_account"}), [RECORD], date(2026, 9, 30),
                   "09/27/2026 – 09/30/2026", "summary text")
    assert spreadsheet.opened == {"creds": {"type": "service_account"}, "key": "sheet123"}

    permits = spreadsheet.worksheet("Permits")
    assert permits is spreadsheet.sheets[0]  # took over the empty Sheet1
    assert permits.values[0] == sheets.PERMIT_HEADERS
    row = permits.values[1]
    assert row[0] == "2026-09-30"
    assert row[1] == '=HYPERLINK("http://x/CapDetail.aspx?id=""1""", "BLD2026-00001")'
    assert row[6] == 125000.0
    assert row[10] == "Yes"
    assert row[11] == "'-demo interior walls"  # kept as text, not parsed as a formula
    assert row[12] == "Planning"

    digests = spreadsheet.worksheet("Daily digests")
    assert digests.values == [sheets.DIGEST_HEADERS, ["2026-09-30", "09/27/2026 – 09/30/2026", 1, "summary text"]]

    # Formatting went on once per tab, then each run sorts newest first.
    assert len(spreadsheet.kinds("createDeveloperMetadata")) == 2
    assert len(spreadsheet.kinds("sortRange")) == 1

    # A second run reuses the tabs, doesn't re-format, and puts its digest on top.
    sheets.publish("sheet123", "{}", [], date(2026, 10, 1), "w", None)
    assert len(permits.values) == 2
    assert digests.values[1] == ["2026-10-01", "w", 0, ""]
    assert len(spreadsheet.sheets) == 2
    assert len(spreadsheet.kinds("createDeveloperMetadata")) == 2


def test_text_escapes_formula_prefixes():
    assert sheets.text("=IMPORTXML(1)") == "'=IMPORTXML(1)"
    assert sheets.text("+1") == "'+1"
    assert sheets.text("plain") == "plain"
    assert sheets.text(None) == ""


def test_existing_tab_gains_module_column(spreadsheet):
    old_headers = sheets.PERMIT_HEADERS[:-1]
    ws = FakeWorksheet("Permits", cols=len(old_headers))
    ws.values = [list(old_headers), ["2026-09-30", "BLD-1"] + [""] * 10, ["2026-09-30", "BLD-2"] + [""] * 10]
    spreadsheet.sheets = [ws]

    sheets.publish("id", "{}", [RECORD], date(2026, 10, 1), "w", None)

    assert ws.values[0] == sheets.PERMIT_HEADERS
    assert [row[12] for row in ws.values[1:]] == ["Building", "Building", "Planning"]


def test_rerun_updates_rows_in_place(spreadsheet):
    sheets.publish("id", "{}", [dict(RECORD, scope="")], date(2026, 9, 30), "w", None)
    permits = spreadsheet.worksheet("Permits")
    # The sheet shows the HYPERLINK's label, so col B reads back as the record number.
    permits.values[1][1] = RECORD["record_number"]

    other = dict(RECORD, record_number="BLD2026-00002")
    sheets.publish("id", "{}", [dict(RECORD, scope="Office remodel, now with notes"), other],
                   date(2026, 10, 1), "w", None)

    assert len(permits.values) == 3  # header, updated row, one new row
    assert permits.values[1][0] == "2026-09-30"  # first-seen date kept
    assert permits.values[1][5] == "Office remodel, now with notes"
    assert permits.values[2][1].endswith('"BLD2026-00002")')


def test_columns_are_matched_by_heading(spreadsheet):
    # A journalist reordered columns and added a Notes column of their own.
    headers = ["Notes", "Record", "Scope", "First seen"] + [
        h for h in sheets.PERMIT_HEADERS if h not in ("Record", "Scope", "First seen")]
    ws = FakeWorksheet("Permits")
    ws.values = [headers, ["call the owner", RECORD["record_number"], "old scope", "2026-09-30"]]
    spreadsheet.sheets = [ws]

    other = dict(RECORD, record_number="BLD2026-00002", scope="New scope")
    sheets.publish("id", "{}", [dict(RECORD, scope="Updated scope"), other], date(2026, 10, 1), "w", None)

    notes, record, scope, first_seen = ws.values[1][:4]
    assert (notes, scope, first_seen) == ("call the owner", "Updated scope", "2026-09-30")
    assert record.endswith(f'"{RECORD["record_number"]}")')  # rewritten as a link
    assert ws.values[2][0] == "" and ws.values[2][2] == "New scope" and ws.values[2][3] == "2026-10-01"
    assert ws.values[0] == headers  # nothing added or moved


def test_format_requests_follow_the_headers():
    reqs = sheets.permit_format_requests(7, sheets.PERMIT_HEADERS)
    money = [r["repeatCell"] for r in reqs if "repeatCell" in r
             and r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat", {}).get("type") == "CURRENCY"]
    assert money[0]["range"]["startColumnIndex"] == sheets.PERMIT_HEADERS.index("Job value")
    rules = [r["addConditionalFormatRule"] for r in reqs if "addConditionalFormatRule" in r]
    assert [r["index"] for r in rules] == list(range(len(rules)))
    formulas = [r["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for r in rules]
    assert '=$K2="Yes"' in formulas and '=$M2="Planning"' in formulas and '=LEFT($B2,5)="26TMP"' in formulas
    frozen = next(r["updateSheetProperties"] for r in reqs if "updateSheetProperties" in r)
    assert frozen["properties"]["gridProperties"] == {"frozenRowCount": 1, "frozenColumnCount": 2}
    assert any("setBasicFilter" in r for r in reqs)


def test_md_to_text():
    md = "## Highlights\n- **BLD1**, 1 Main St: $3M\n**By the numbers**\n- 5 records\n#10 wire"
    assert sheets.md_to_text(md) == "HIGHLIGHTS\n• BLD1, 1 Main St: $3M\nBY THE NUMBERS\n• 5 records\n#10 wire"


def test_formatting_failure_keeps_data(spreadsheet, monkeypatch):
    monkeypatch.setattr(spreadsheet, "fetch_sheet_metadata", lambda params: 1 / 0)
    sheets.publish("id", "{}", [RECORD], date(2026, 9, 30), "w", "s")
    assert spreadsheet.worksheet("Permits").values[1][6] == 125000.0
