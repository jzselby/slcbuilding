"""Search SLC's Accela portal for new commercial permits, summarize them, and log them to a Google Sheet.

    python -m slc_permits                         # last 3 days, record types containing "Commercial"
    python -m slc_permits --days-back 14 --dry-run
    python -m slc_permits --all-types --start 2026-09-01 --end 2026-09-15
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from .config import Config
from .parse import parse_job_value
from .scraper import USER_AGENT, AccelaClient, ScrapeError, launch_browser
from .store import PermitStore
from .summarize import analyze, apply_notes, build_report

log = logging.getLogger("slc_permits")
SLC_TZ = ZoneInfo("America/Denver")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="slc_permits", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days-back", type=int, default=3,
                   help="search records opened in the last N days (default 3; overlap is fine, repeats are skipped)")
    p.add_argument("--start", type=date.fromisoformat, help="start date YYYY-MM-DD (overrides --days-back)")
    p.add_argument("--end", type=date.fromisoformat, help="end date YYYY-MM-DD (default today)")
    p.add_argument("--type-contains", action="append", metavar="TEXT",
                   help="keep records whose Record Type contains TEXT (repeatable; default: Commercial)")
    p.add_argument("--all-types", action="store_true", help="keep every record type")
    p.add_argument("--record-type", metavar="TEXT",
                   help="also narrow the portal search itself to the first Record Type option containing TEXT")
    p.add_argument("--no-details", action="store_true", help="skip opening each new record's detail page")
    p.add_argument("--no-summary", action="store_true", help="skip the Claude summary")
    p.add_argument("--no-sheet", action="store_true", help="don't write to the Google Sheet")
    p.add_argument("--dry-run", action="store_true", help="don't write to the sheet or record permits as seen")
    p.add_argument("--headful", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.type_contains is None:
        args.type_contains = ["Commercial"]
    return args


def keep_types(records: list[dict], needles: list[str] | None) -> list[dict]:
    types = Counter(r.get("record_type") or "(none)" for r in records)
    log.info("Record types in window: %s", ", ".join(f"{t} ({n})" for t, n in types.most_common()) or "none")
    if not needles:
        return records
    lowered = [n.lower() for n in needles]
    kept = [r for r in records if any(n in (r.get("record_type") or "").lower() for n in lowered)]
    log.info("%d of %d records match %s", len(kept), len(records), " or ".join(repr(n) for n in needles))
    return kept


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if args.headful:
        cfg.headless = False

    use_sheet = not (args.no_sheet or args.dry_run)
    if use_sheet and cfg.google_sheet_id and not cfg.google_service_account_json:
        log.error("GOOGLE_SHEET_ID is set but GOOGLE_SERVICE_ACCOUNT_JSON is not")
        return 2
    use_sheet = use_sheet and bool(cfg.google_sheet_id)

    today = datetime.now(SLC_TZ).date()
    end = args.end or today
    start = args.start or end - timedelta(days=args.days_back)
    if start > end:
        log.error("--start is after --end")
        return 2

    store = PermitStore(cfg.data_dir / "permits.jsonl")
    log.info("%d permits already on record", len(store))

    with sync_playwright() as pw:
        browser = launch_browser(pw, cfg)
        page = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1000}).new_page()
        client = AccelaClient(page, cfg)
        try:
            client.login()
            found = client.search(start, end, args.record_type)
            wanted = keep_types(found, None if args.all_types else args.type_contains)
            new = store.new_only(wanted)
            log.info("%d new", len(new))
            if not args.no_details:
                for i, rec in enumerate(new, 1):
                    if rec.get("detail_url") and not rec.get("detail_text"):
                        log.info("Detail %d/%d: %s", i, len(new), rec["record_number"])
                        try:
                            rec["detail_text"] = client.fetch_detail(rec["detail_url"])
                        except Exception as exc:
                            log.warning("Could not load detail for %s: %s", rec["record_number"], exc)
                    if rec.get("detail_text"):
                        rec["job_value"] = parse_job_value(rec["detail_text"])
        except ScrapeError as exc:
            log.error("%s (debug snapshots in %s/)", exc, cfg.debug_dir)
            return 1
        except Exception:
            client.save_debug("unexpected-error")
            raise
        finally:
            browser.close()

    digest = None if args.no_summary else analyze(new, start, end, cfg.summary_model)
    apply_notes(new, digest)

    report = build_report(new, start, end, digest)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = cfg.reports_dir / f"{today.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    (cfg.reports_dir / "latest.md").write_text(report, encoding="utf-8")
    log.info("Wrote %s", report_path)

    # Show the digest on the GitHub Actions run page.
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report)

    if use_sheet:
        from .sheets import publish

        try:
            publish(cfg.google_sheet_id, cfg.google_service_account_json, new, today,
                    f"{start:%m/%d/%Y} – {end:%m/%d/%Y}", digest.summary_markdown if digest else None)
        except Exception:
            # Leave the permits unrecorded so the next run retries them.
            log.exception("Writing to the Google Sheet failed")
            return 1

    if not args.dry_run:
        store.add(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
