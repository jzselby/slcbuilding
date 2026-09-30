import json
from datetime import date

import gspread
import pytest

from slc_permits import sheets


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.values = []
        self.frozen = 0

    def get_all_values(self):
        return self.values

    def update_title(self, title):
        self.title = title

    def update(self, values, cell):
        assert cell == "A1"
        self.values[:len(values)] = values

    def format(self, rng, fmt):
        pass

    def freeze(self, rows):
        self.frozen = rows

    def append_rows(self, rows, value_input_option, table_range):
        assert value_input_option == "USER_ENTERED"
        self.values.extend(rows)


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = [FakeWorksheet("Sheet1")]

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
    "description": "-demo interior walls",
}


def test_publish_sets_up_tabs_and_appends(spreadsheet):
    sheets.publish("sheet123", json.dumps({"type": "service_account"}), [RECORD], date(2026, 9, 30),
                   "09/27/2026 – 09/30/2026", "summary text")
    assert spreadsheet.opened == {"creds": {"type": "service_account"}, "key": "sheet123"}

    permits = spreadsheet.worksheet("Permits")
    assert permits is spreadsheet.sheets[0]  # took over the empty Sheet1
    assert permits.values[0] == sheets.PERMIT_HEADERS
    assert permits.frozen == 1
    row = permits.values[1]
    assert row[0] == "2026-09-30"
    assert row[1] == '=HYPERLINK("http://x/CapDetail.aspx?id=""1""", "BLD2026-00001")'
    assert row[6] == 125000.0
    assert row[10] == "Yes"
    assert row[11] == "'-demo interior walls"  # kept as text, not parsed as a formula

    digests = spreadsheet.worksheet("Daily digests")
    assert digests.values == [sheets.DIGEST_HEADERS, ["2026-09-30", "09/27/2026 – 09/30/2026", 1, "summary text"]]

    # A second run reuses the tabs and just appends.
    sheets.publish("sheet123", "{}", [], date(2026, 10, 1), "w", None)
    assert len(permits.values) == 2
    assert digests.values[-1] == ["2026-10-01", "w", 0, ""]
    assert len(spreadsheet.sheets) == 2


def test_text_escapes_formula_prefixes():
    assert sheets.text("=IMPORTXML(1)") == "'=IMPORTXML(1)"
    assert sheets.text("+1") == "'+1"
    assert sheets.text("plain") == "plain"
    assert sheets.text(None) == ""
