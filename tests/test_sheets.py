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

    def col_values(self, n, value_render_option=None):
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

    def delete_rows(self, index):
        del self.values[index - 1]

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
        by_id = {ws.id: ws for ws in self.sheets}
        for r in body["requests"]:
            if "deleteDimension" in r and r["deleteDimension"]["range"]["dimension"] == "ROWS":
                rng = r["deleteDimension"]["range"]
                del by_id[rng["sheetId"]].values[rng["startIndex"]:rng["endIndex"]]
            if "moveDimension" in r:
                move = r["moveDimension"]
                i, dest = move["source"]["startIndex"], move["destinationIndex"]
                for row in by_id[move["source"]["sheetId"]].values:
                    row.extend([""] * (i + 1 - len(row)))
                    row.insert(dest, row.pop(i))

    def fetch_sheet_metadata(self, params):
        markers, rules = {}, {}
        for r in self.requests:
            if "createDeveloperMetadata" in r:
                dm = r["createDeveloperMetadata"]["developerMetadata"]
                markers[dm["location"]["sheetId"]] = dm
            if "addConditionalFormatRule" in r:
                sheet = r["addConditionalFormatRule"]["rule"]["ranges"][0]["sheetId"]
                rules[sheet] = rules.get(sheet, 0) + 1
            if "deleteConditionalFormatRule" in r:
                rules[r["deleteConditionalFormatRule"]["sheetId"]] -= 1
        return {"sheets": [{"properties": {"sheetId": ws.id},
                            "developerMetadata": [markers[ws.id]] if ws.id in markers else [],
                            "conditionalFormats": [{}] * rules.get(ws.id, 0)}
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
    assert len(spreadsheet.kinds("createDeveloperMetadata")) == 3
    assert len(spreadsheet.kinds("sortRange")) == 2

    # A second run reuses the tabs, doesn't re-format, and puts its digest on top.
    sheets.publish("sheet123", "{}", [], date(2026, 10, 1), "w", None)
    assert len(permits.values) == 2
    assert digests.values[1] == ["2026-10-01", "w", 0, ""]
    assert len(spreadsheet.sheets) == 3
    assert len(spreadsheet.kinds("createDeveloperMetadata")) == 3


def test_text_escapes_formula_prefixes():
    assert sheets.text("=IMPORTXML(1)") == "'=IMPORTXML(1)"
    assert sheets.text("+1") == "'+1"
    assert sheets.text("plain") == "plain"
    assert sheets.text(None) == ""


def test_existing_tab_gains_module_column(spreadsheet):
    old_headers = sheets.PERMIT_HEADERS[:12]  # the layout before the Module column
    ws = FakeWorksheet("Permits", cols=len(old_headers))
    ws.values = [list(old_headers), ["2026-09-30", "BLD-1"] + [""] * 10, ["2026-09-30", "BLD-2"] + [""] * 10]
    spreadsheet.sheets = [ws]
    spreadsheet.requests.append({"createDeveloperMetadata": {"developerMetadata": {  # already formatted
        "metadataKey": sheets.FORMAT_KEY, "metadataValue": sheets.FORMAT_VERSION, "location": {"sheetId": ws.id}}}})

    sheets.publish("id", "{}", [RECORD], date(2026, 10, 1), "w", None)

    assert ws.values[0] == sheets.PERMIT_HEADERS
    assert [row[12] for row in ws.values[1:]] == ["Building", "Building", "Planning"]
    # The tab was already formatted, so only the new columns get styled.
    widths = [r for r in spreadsheet.kinds("updateDimensionProperties")
              if r["updateDimensionProperties"]["range"]["sheetId"] == ws.id
              and "pixelSize" in r["updateDimensionProperties"]["properties"]]
    assert {w["updateDimensionProperties"]["range"]["startIndex"] for w in widths} == {12, 13, 14, 15, 16}
    # The highlight rules are rebuilt to use the new Importance column.
    formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
                for r in spreadsheet.kinds("addConditionalFormatRule")
                if r["addConditionalFormatRule"]["rule"]["ranges"][0]["sheetId"] == ws.id]
    assert '=$N2="High"' in formulas and '=OR($N2="Low",MID($B2,3,3)="TMP")' in formulas


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
    new = 'AND($A2<>"",$A2=MAX($A$2:$A))'
    muted = 'OR($N2="Low",MID($B2,3,3)="TMP")'
    # Combinations come before the single conditions, since only the first matching rule applies.
    assert formulas == ['=$N2="High"',
                        f'=AND(ISNUMBER($G2),$G2>=1000000,NOT({muted}),{new})',
                        f'=AND(ISNUMBER($G2),$G2>=1000000,NOT({muted}))',
                        f'=AND({new},{muted})', f'={new}', f'={muted}']
    hidden = [r["updateDimensionProperties"]["range"]["startIndex"] for r in reqs
              if r.get("updateDimensionProperties", {}).get("properties", {}).get("hiddenByUser")]
    assert hidden == [sheets.PERMIT_HEADERS.index("Contractor"), sheets.PERMIT_HEADERS.index("Notable")]
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


def test_high_importance_tab(spreadsheet):
    big = dict(RECORD, record_number="BLD2026-00009", importance="high", category="Major project",
               why_it_matters="A $3M lab build-out.", business="Nuton", job_value=3_000_000.0)
    routine = dict(RECORD, record_number="BLD2026-00010", importance="low")
    sheets.publish("id", "{}", [big, routine], date(2026, 10, 1), "w", None)

    high = spreadsheet.worksheet(sheets.HIGH_TAB)
    assert high.values[0] == sheets.HIGH_HEADERS
    assert len(high.values) == 2  # only the high-importance record
    row = dict(zip(sheets.HIGH_HEADERS, high.values[1]))
    assert (row["Category"], row["Why it matters"], row["Business"], row["Job value"]) == (
        "Major project", "A $3M lab build-out.", "Nuton", 3_000_000.0)
    permits = spreadsheet.worksheet("Permits")
    assert len(permits.values) == 3  # everything stays on Permits
    importance = sheets.PERMIT_HEADERS.index("Importance")
    assert sorted(r[importance] for r in permits.values[1:]) == ["High", "Low"]

    # Re-rated as medium later: it drops off the High tab but stays on Permits.
    high.values[1][sheets.HIGH_HEADERS.index("Record")] = big["record_number"]  # Sheets shows the link label
    sheets.publish("id", "{}", [dict(big, importance="medium")], date(2026, 10, 2), "w", None, digest_row=False)
    assert len(high.values) == 1
    assert len(spreadsheet.worksheet(sheets.DIGESTS_TAB).values) == 2  # no digest row for the re-rate


def test_high_tab_drops_permits_after_60_days(spreadsheet):
    high = FakeWorksheet(sheets.HIGH_TAB)
    rows = {"BLD-OLD": "2026-07-01", "BLD-EDGE": "2026-08-03", "BLD-RECENT": "2026-09-20"}
    high.values = [list(sheets.HIGH_HEADERS)] + [
        [seen if h == "First seen" else number if h == "Record" else "" for h in sheets.HIGH_HEADERS]
        for number, seen in rows.items()]
    high.values[2][0] = 46237  # Sheets returns dates unformatted as serial numbers: 2026-08-03
    spreadsheet.sheets = [FakeWorksheet("Permits"), high]

    stale = dict(RECORD, record_number="BLD-STALE", importance="high", first_seen="2026-07-15")
    fresh = dict(RECORD, record_number="BLD-NEW", importance="high")
    sheets.publish("id", "{}", [stale, fresh], date(2026, 10, 2), "w", None)

    record = sheets.HIGH_HEADERS.index("Record")
    assert sorted(r[record].split('"')[-2] if r[record].startswith("=") else r[record]
                  for r in high.values[1:]) == ["BLD-EDGE", "BLD-NEW", "BLD-RECENT"]
    permits = spreadsheet.worksheet("Permits")
    assert len(permits.values) == 3  # Permits keeps everything


def test_format_upgrade_replaces_old_rules_and_moves_first_seen(spreadsheet):
    old_high = ["Date opened", "Category", "Why it matters", "Address", "Job value", "Business",
                "Record", "Record type", "Status", "Scope", "Module", "First seen"]
    high = FakeWorksheet(sheets.HIGH_TAB)
    high.values = [list(old_high), ["09/30/2026", "Housing", "", "", "", "", "BLD-1", "", "", "", "", "2026-10-01"]]
    spreadsheet.sheets = [FakeWorksheet("Permits"), high]
    for _ in range(2):  # an old-version tab with two rules
        spreadsheet.requests.append({"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": high.id}]}}})
    spreadsheet.requests.append({"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": sheets.FORMAT_KEY, "metadataValue": "1", "location": {"sheetId": high.id}}}})

    sheets.publish("id", "{}", [], date(2026, 10, 2), "w", None)

    assert high.values[0] == ["First seen"] + old_high[:-1]
    assert high.values[1][0] == "2026-10-01" and high.values[1][7] == "BLD-1"
    deletes = [r for r in spreadsheet.kinds("deleteConditionalFormatRule")
               if r["deleteConditionalFormatRule"]["sheetId"] == high.id]
    assert len(deletes) == 2
    formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
                for r in spreadsheet.kinds("addConditionalFormatRule")
                if r["addConditionalFormatRule"]["rule"]["ranges"][0]["sheetId"] == high.id
                and "booleanRule" in r["addConditionalFormatRule"]["rule"]]
    assert '=AND($A2<>"",$A2=MAX($A$2:$A))' in formulas  # new rows, keyed on First seen in column A
    sort = spreadsheet.kinds("sortRange")[-1]["sortRange"]
    assert [s["dimensionIndex"] for s in sort["sortSpecs"]] == [0, 1]  # First seen, then Date opened
