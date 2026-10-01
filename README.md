# SLC commercial permit digest

Searches Salt Lake City's Accela Citizen Access portal
(<https://aca-prod.accela.com/SLCREF>) for newly opened **commercial** building
records and **Planning** applications, and skips ones it has already reported. It opens each new record's
detail page, then logs each permit as a row in a Google Sheet. Claude adds a
plain-English scope, job value, applicant, contractor, and a "notable" flag.
Claude also writes a short digest of each run to a second tab.

```
slc_permits/
  scraper.py    Playwright: login, search by date range, walk result pages, open detail pages
  parse.py      ACA HTML -> records (grid rows, detail-page text, job value)
  summarize.py  Claude: digest + structured per-permit notes; Markdown report
  sheets.py     append rows to the Google Sheet
  store.py      data/permits.jsonl: every record already reported
  __main__.py   CLI
.github/workflows/permits.yml   daily scheduled run
```

## How a run works

1. Log in with `ACCELA_USERNAME` / `ACCELA_PASSWORD`. If they aren't set, it searches anonymously.
2. Search the **Building** and **Planning** tabs for records opened in the last `--days-back`
   days (default 14). The windows overlap on purpose, so records the city back-dates are still
   caught.
3. Filter each tab by **Record Type**. Building keeps types containing "Commercial" (or SLC's
   misspelling "Commericial"). This catches every commercial subtype, where the portal's type
   dropdown only allows one. Planning keeps every type except routine "Minor Alteration"
   (historic districts) and "Zoning Verification Letter" requests. That keeps rezonings, design
   review, subdivisions and condos, and historic new construction, plus any new types the city
   adds. The log lists every record type seen in each tab, so you can tune the filters with
   `--types` and `--skip-types`.
4. Drop records already in `data/permits.jsonl`. Open each new record's detail page and expand
   "More Details" to get the job value, contractor, and so on.
5. Claude returns a digest plus structured notes for each permit. A job value read directly off
   the page takes precedence over Claude's.
6. Append rows to the sheet, write `reports/YYYY-MM-DD.md`, and record the permits as seen.
   If the sheet write fails, nothing is recorded, so the next run retries those permits.

### The sheet

**High importance** tab: what a reporter scans first. Claude rates every permit's importance
(High / Medium / Low) against an editor's checklist and picks a category: Major project, New
business, Housing, Public / government, Demolition, Land use / zoning, Historic, Energy /
infrastructure, or Routine. High covers:

- new buildings and additions, and projects of $1M or more
- housing of 10+ units
- new or relocating businesses the public would recognize
- city, school and other public projects
- demolitions of whole buildings
- rezonings and other Planning Commission items
- anything likely to draw public interest

Projects of $1M+ and Planning Commission items are always High, even without Claude. This tab
holds only the High items, newest first, with a one-line "Why it matters" and the business
name. Items re-rated below High are removed from it.

**Permits** tab: every permit, one row each.

| First seen | Record | Date opened | Record type | Address | Scope | Job value | Applicant | Contractor | Status | Notable | Portal description | Module | Importance | Category | Why it matters | Business |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

"Record" links to the permit's page on the portal. The run formats the tabs once and then sorts
them newest first after every run:

- **High-importance** rows ("Notable") are highlighted yellow, and **Low** (routine) rows are gray.
- Job values of **$1M+** are shown in bold orange, and **$250k+** in light orange.
- Planning applications are tinted blue, and unsubmitted drafts ("26TMP-…") are gray.
- Every column has a filter. The header row and the Record column stay in view while scrolling.
- The Contractor column is hidden, because SLC lists the contractor under Applicant.

Values are written under their column headings, so you can reorder columns or add your own,
such as a "Notes" column; runs leave those alone. Don't rename the headings: the run would add
a new column with the original name. For personal sorting and filtering, use
**Data → Filter views**, which doesn't change the view for anyone else.

**Daily digests** tab: one row per run, newest first, with the date window, the count of new
permits, and Claude's summary of the highlights.

## Set up the Google Sheet

The daily job writes to the sheet as a Google Cloud **service account**, a robot Google account:

1. In the [Google Cloud console](https://console.cloud.google.com/), create or pick a project,
   then enable the **Google Sheets API** (APIs & Services → Library).
2. Under APIs & Services → Credentials, choose **Create credentials → Service account**. Open
   it, then under **Keys → Add key → JSON**, download the key file.
3. Open the sheet, click **Share**, and add the service account's email
   (`…@….iam.gserviceaccount.com`, from the key file) as an **Editor**.
4. The sheet ID is the long string in its URL: `docs.google.com/spreadsheets/d/<ID>/edit`.

## Run it daily on GitHub Actions

1. Merge this into the default branch. Scheduled workflows only run from there.
2. Under **Settings → Secrets and variables → Actions**, add these secrets:

   | Secret | Value |
   |---|---|
   | `ACCELA_USERNAME`, `ACCELA_PASSWORD` | your Accela login |
   | `ANTHROPIC_API_KEY` | for the Claude notes and digest (without it, rows still get written, minus those columns) |
   | `GOOGLE_SHEET_ID` | the sheet ID from above |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the entire contents of the downloaded key file |

3. Under **Actions → SLC commercial permit digest**, click **Run workflow** to test it.
   After that it should run every morning before 8am Mountain. The workflow has GitHub schedules
   at 5:47, 6:43 and 7:37am in summer (an hour earlier in winter), but GitHub's scheduler hasn't
   fired for this repo so far. So a scheduled Claude task, "SLC permit digest daily update",
   checks at 7:41am Mountain and starts the run if today's report doesn't exist yet. It sends a
   push notification only if the update fails. Any extra scheduled attempts skip themselves
   once the day's report exists. A manual run lets you change the look-back
   window, either tab's type filter (blank keeps all types), or Planning's skip list. Tick **dry run** to see in the
   log what would be added, without writing the sheet or marking anything as seen.
   Tick **reclassify** to re-rate every permit already in the sheet with the current checklist,
   for example after changing it in `slc_permits/summarize.py`. It doesn't search the portal and
   costs a few cents.

Each run also commits `reports/` and `data/permits.jsonl` and shows the digest on the run's
summary page. If a run fails, download the `debug-snapshots` artifact, which has a screenshot
and the HTML of the page where it stopped.

## Run it locally

```bash
pip install -r requirements.txt
python -m playwright install chromium

export ACCELA_USERNAME='you@example.com' ACCELA_PASSWORD='...'
export ANTHROPIC_API_KEY='sk-ant-...'
export GOOGLE_SHEET_ID='...' GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)"

python -m slc_permits                              # last 14 days: commercial Building + all Planning
python -m slc_permits --dry-run --no-details       # try it: no sheet writes, nothing marked seen
python -m slc_permits --types "Planning=Site Plan,Zoning"   # narrow Planning to some types
python -m slc_permits --modules Building --all-types --no-sheet   # one tab, every type, report only
python -m slc_permits --headful -v                 # watch the browser while it runs
```

The first real run treats everything in the window as new. To backfill, do one run with
a larger `--days-back`.

Other settings (environment variables): `ACCELA_MODULES` (default `Building,Planning`),
`SUMMARY_MODEL` (default `claude-opus-5-5`), `ACCELA_TIMEOUT_MS`,
`ACCELA_MAX_PAGES`, `CHROMIUM_EXECUTABLE`, and `HEADLESS=0`.

## If the portal changes

The selectors are based on standard Accela Citizen Access element ids, like
`ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate` and the
`…gdvPermitList` results grid. If a run fails with "Search form not found" or
"Timed out waiting for search results", compare the debug snapshot against
the constants at the top of `slc_permits/scraper.py`.

## Tests

`tests/mock_aca.py` is a small local imitation of the portal: login, AJAX
search, a paged grid, the jump straight to the record when there's only one
result, and collapsed detail sections. The scraper runs against it end to end.
Claude and Google Sheets are faked.

```bash
pip install pytest
python -m pytest            # add CHROMIUM_EXECUTABLE=/path/to/chrome if you use a system Chromium
```
