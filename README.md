# SLC building permit digest

Searches Salt Lake City's Accela Citizen Access portal
(<https://aca-prod.accela.com/SLCREF>) for newly opened building records, skips
ones it has already reported, opens each new record's detail page, and writes
a Markdown digest. Claude writes the summary at the top. A full table of the
new records goes below it.

```
slc_permits/
  scraper.py    Playwright: login, search by date range, walk result pages, open detail pages
  parse.py      ACA HTML -> records (grid rows, detail-page text)
  store.py      data/permits.jsonl: every record already reported
  summarize.py  Claude summary + deterministic records table
  __main__.py   CLI
.github/workflows/permits.yml   daily scheduled run
```

## How a run works

1. Log in with `ACCELA_USERNAME` / `ACCELA_PASSWORD`. If they aren't set, it searches anonymously.
2. Open `Cap/CapHome.aspx?module=Building` and search records opened in the last
   `--days-back` days (default 3). The windows overlap on purpose, so records the
   city back-dates are still caught. Repeats are filtered against `data/permits.jsonl`.
3. Click through every results page. Open each new record's detail page and expand
   "More Details" to capture job value, contractor, and so on.
4. Write `reports/YYYY-MM-DD.md` and `reports/latest.md`, then append the new records
   to `data/permits.jsonl`.

## Run it locally

```bash
pip install -r requirements.txt
python -m playwright install chromium

export ACCELA_USERNAME='you@example.com'
export ACCELA_PASSWORD='...'
export ANTHROPIC_API_KEY='sk-ant-...'   # optional; without it you get the table only

python -m slc_permits                          # last 3 days, all Building records
python -m slc_permits --days-back 14 --dry-run # look back further without marking anything seen
python -m slc_permits --record-type Commercial # only record types containing "Commercial"
python -m slc_permits --headful -v             # watch the browser while it runs
```

The first run reports everything in the window as new. Use `--dry-run` to try
it without writing to `data/permits.jsonl`.

Other settings (environment variables): `ACCELA_MODULE` (default `Building`),
`SUMMARY_MODEL` (default `claude-opus-5-5`), `ACCELA_TIMEOUT_MS`,
`ACCELA_MAX_PAGES`, `CHROMIUM_EXECUTABLE`, and `HEADLESS=0`.

## Run it daily on GitHub Actions

1. Merge this into the default branch. Scheduled workflows only run from there.
2. Under **Settings → Secrets and variables → Actions**, add `ACCELA_USERNAME`,
   `ACCELA_PASSWORD`, and `ANTHROPIC_API_KEY`.
3. Under **Actions → SLC permit digest**, click **Run workflow** to test it.
   After that it runs daily at 7:48am Mountain.

Each run commits the new report and the updated `data/permits.jsonl`. It also
shows the digest on the run's summary page. If a run fails, download the
`debug-snapshots` artifact, which has a screenshot and the HTML of the page
where it stopped.

## If the portal changes

The selectors are based on standard Accela Citizen Access element ids, like
`ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate` and the
`…gdvPermitList` results grid. If a run fails with "Search form not found" or
"Timed out waiting for search results", compare the debug snapshot against
the constants at the top of `slc_permits/scraper.py`.

## Tests

`tests/mock_aca.py` is a small local imitation of the portal: login, AJAX
search, a paged grid, the jump straight to the record when there's only one
result, and collapsed detail sections. The scraper runs against it end to end:

```bash
pip install pytest
python -m pytest            # add CHROMIUM_EXECUTABLE=/path/to/chrome if you use a system Chromium
```
