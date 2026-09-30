"""A tiny stand-in for Accela Citizen Access, for end-to-end scraper tests.

It mirrors the parts of ACA the scraper depends on: the classic login box,
the general search form with its ctl00_* ids, results swapped in by AJAX
(like ASP.NET UpdatePanel postbacks), the grid's CSS classes and pager row,
the jump to CapDetail.aspx when there is exactly one hit, and collapsed
"More Details" sections on the detail page.
"""

from __future__ import annotations

import html
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USER, PASSWORD = "tester", "s3cret"
PAGE_SIZE = 10
BASE_DAY = date(2026, 9, 1)

TYPES = ["Residential New Construction", "Commercial Alteration", "Demolition"]
PLANNING_TYPES = ["Site Plan Review", "Zoning Map Amendment"]
MODULE_TYPES = {"Building": TYPES, "Planning": PLANNING_TYPES}
RECORDS = [
    {
        "module": "Building",
        "number": f"BLD2026-{i:05d}",
        "date": BASE_DAY + timedelta(days=i % 5),
        "type": TYPES[i % 3],
        "address": f"{100 + i} S {i} E, Salt Lake City UT 84102",
        "description": f"Scope of work #{i}",
        "status": "In Review" if i % 2 else "Issued",
        "value": 25_000 * (i + 1),
    }
    for i in range(23)
]
# One record alone on 2026-08-15 exercises the single-hit redirect.
RECORDS.append({"module": "Building", "number": "BLD2026-09999", "date": date(2026, 8, 15), "type": TYPES[0],
                "address": "1 Solo St", "description": "Lone record", "status": "Issued", "value": 1})
RECORDS += [
    {
        "module": "Planning",
        "number": f"PLNSUB2026-{i:05d}",
        "date": BASE_DAY + timedelta(days=i),
        "type": PLANNING_TYPES[i % 2],
        "address": f"{500 + i} W North Temple, Salt Lake City UT 84116",
        "description": f"Planning proposal #{i}",
        "status": "Under Review",
        "value": 0,
    }
    for i in range(4)
]


def _page(title: str, body: str, logged_in: bool) -> bytes:
    # Mirrors SLC's header, where an icon ligature runs into the link text ("lock_openLogout").
    account = ('<a id="ctl00_HeaderNavigation_btnLogout" href="/Logout.aspx"><i>lock_open</i>Logout</a>'
               if logged_in else '<a href="/Login.aspx">Login</a>')
    return f"""<!doctype html><html><head><title>{title}</title></head>
<body><div id="header">{account}</div><div id="ctl00_PlaceHolderMain">{body}</div></body></html>""".encode()


def _parse_mdY(value: str) -> date:
    m, d, y = (int(x) for x in value.split("/"))
    return date(y, m, d)


def matching(start: date, end: date, rtype: str, module: str = "Building") -> list[dict]:
    return [r for r in RECORDS if r["module"] == module and start <= r["date"] <= end
            and (not rtype or r["type"] == rtype)]


def grid_html(rows: list[dict], page: int) -> str:
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    chunk = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    trs = []
    for n, r in enumerate(chunk):
        cls = "ACA_TabRow_Odd" if n % 2 == 0 else "ACA_TabRow_Even"
        trs.append(
            f'<tr class="{cls}"><td><input type="checkbox"></td><td><span>{r["date"]:%m/%d/%Y}</span></td>'
            f'<td><a href="/Cap/CapDetail.aspx?Module=Building&amp;capID1={r["number"]}">'
            f'<strong><span>{r["number"]}</span></strong></a></td>'
            f'<td>{html.escape(r["type"])}</td><td>{html.escape(r["address"])}</td>'
            f'<td>{html.escape(r["description"])}</td><td></td><td>{r["status"]}</td>'
            f'<td><a href="#">Pay Fees</a></td></tr>'
        )
    pager = []
    if page > 1:
        pager.append(f'<a href="javascript:void(0)" onclick="loadPage({page - 1})">&lt; Prev</a>')
    pager.append(f"<span>{page}</span>")
    if page < pages:
        pager.append(f'<a href="javascript:void(0)" onclick="loadPage({page + 1})">Next &gt;</a>')
    return f"""<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
{GRID_HEADER}
{''.join(trs)}
<tr class="ACA_Table_Pages"><td colspan="9">{' '.join(pager)}</td></tr></table>"""


GRID_HEADER = """<tr class="ACA_TabRow_Header"><th></th><th>Date</th><th>Record Number</th><th>Record Type</th>
<th>Address</th><th>Description</th><th>Project Name</th><th>Status</th><th>Action</th></tr>"""


def empty_grid(table_id: str) -> str:
    return f"""<table id="{table_id}">{GRID_HEADER}
<tr><td colspan="9">No records found.</td></tr></table>"""


# Like SLC's page: a logged-in user's (empty) "my records" grid sits above the
# search form, and its id also ends in gdvPermitList. ASP.NET's
# Sys.WebForms.PageRequestManager reports when a postback is in flight.
SEARCH_PAGE = """
<h2>Records</h2>{my_records}
<input type="hidden" id="module" value="{module}">
<label><input type="checkbox" id="ctl00_PlaceHolderMain_chkSearch" checked> Search my records only</label>
<table><tr><td>Start Date <input id="ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate" class="watermark"></td>
<td>End Date <input id="ctl00_PlaceHolderMain_generalSearchForm_txtGSEndDate"></td>
<td>Record Type <select id="ctl00_PlaceHolderMain_generalSearchForm_ddlGSPermitType">
<option value="">--Select--</option>{options}</select></td></tr></table>
<a id="ctl00_PlaceHolderMain_btnNewSearch" href="javascript:void(0)" onclick="loadPage(1)">Search</a>
<div id="results"></div>
<script>
window.__busy = false;
window.Sys = {{WebForms: {{PageRequestManager: {{getInstance: () => ({{get_isInAsyncPostBack: () => window.__busy}})}}}}}};
async function loadPage(p) {{
  window.__busy = true;
  const q = new URLSearchParams({{
    mine: document.getElementById('ctl00_PlaceHolderMain_chkSearch').checked ? '1' : '',
    module: document.getElementById('module').value,
    start: document.getElementById('ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate').value,
    end: document.getElementById('ctl00_PlaceHolderMain_generalSearchForm_txtGSEndDate').value,
    type: document.getElementById('ctl00_PlaceHolderMain_generalSearchForm_ddlGSPermitType').value,
    page: p}});
  await new Promise(r => setTimeout(r, 600));  // postback latency
  const res = await fetch('/Cap/Results?' + q);
  const redirect = res.headers.get('X-Redirect');
  if (redirect) {{ location.href = redirect; return; }}
  document.getElementById('results').innerHTML = await res.text();
  window.__busy = false;
}}
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _logged_in(self) -> bool:
        return "session=ok" in (self.headers.get("Cookie") or "")

    def _send(self, body: bytes, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        logged_in = self._logged_in()
        if url.path == "/Login.aspx":
            body = """<form method="post" action="/Login.aspx">
<input id="ctl00_PlaceHolderMain_LoginBox_txtUserId" name="user">
<input id="ctl00_PlaceHolderMain_LoginBox_txtPassword" name="pw" type="password">
<input id="ctl00_PlaceHolderMain_LoginBox_btnLogin" type="submit" value="Sign In"></form>"""
            self._send(_page("Login", body, logged_in))
        elif url.path == "/Default.aspx":
            self._send(_page("Home", "<h1>Welcome</h1>", logged_in))
        elif url.path == "/Cap/CapHome.aspx":
            module = qs.get("module", "Building")
            options = "".join(f'<option value="{html.escape(t)}">{html.escape(t)}</option>'
                              for t in MODULE_TYPES.get(module, []))
            my_records = empty_grid("ctl00_PlaceHolderMain_dgvMyPermitList_gdvPermitList") if logged_in else ""
            self._send(_page("Search", SEARCH_PAGE.format(options=options, my_records=my_records, module=html.escape(module)), logged_in))
        elif url.path == "/Cap/Results":
            try:
                rows = matching(_parse_mdY(qs["start"]), _parse_mdY(qs["end"]), qs.get("type", ""),
                                qs.get("module", "Building"))
            except (KeyError, ValueError):
                self._send(b'<span class="ACA_Error">Invalid date</span>')
                return
            if qs.get("mine"):
                rows = []  # the test user has no records of their own
            if not rows:
                self._send(empty_grid("ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList").encode())
            elif len(rows) == 1:
                self._send(b"", headers={"X-Redirect": f"/Cap/CapDetail.aspx?Module=Building&capID1={rows[0]['number']}"})
            else:
                self._send(grid_html(rows, int(qs.get("page", 1))).encode())
        elif url.path == "/Cap/CapDetail.aspx":
            rec = next((r for r in RECORDS if r["number"] == qs.get("capID1")), None)
            if rec is None:
                self._send(b"not found", 404)
                return
            body = f"""<div id="ctl00_PlaceHolderMain_PermitDetailList1">
<h1>Record <span id="ctl00_PlaceHolderMain_lblPermitNumber">{rec['number']}</span>:<br>{html.escape(rec['type'])}</h1>
<h2>Work Location</h2><p>{html.escape(rec['address'])}</p>
<h2>Project Description</h2><p>{html.escape(rec['description'])}</p>
<a href="javascript:void(0)" onclick="document.getElementById('more').style.display='block'">More Details</a>
<div id="more" style="display:none"><h3>Job Value</h3><p>${rec['value']:,}.00</p></div>
</div><script>var noise = "should not appear";</script>"""
            self._send(_page("Record", body, logged_in))
        else:
            self._send(b"not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if urlparse(self.path).path == "/Login.aspx" and form.get("user") == USER and form.get("pw") == PASSWORD:
            self._send(b"", 302, {"Location": "/Default.aspx", "Set-Cookie": "session=ok; Path=/"})
        else:
            self._send(_page("Login", "<p class='ACA_Error'>Invalid credentials</p>", False))


class MockACA:
    def __enter__(self) -> "MockACA":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
