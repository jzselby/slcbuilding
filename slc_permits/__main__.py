"""Search SLC's Accela portal for new building permits and write a digest.

    python -m slc_permits                    # last 3 days, all Building records
    python -m slc_permits --days-back 7 --record-type "Residential"
    python -m slc_permits --start 2026-09-01 --end 2026-09-15 --no-details
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from .config import Config
from .scraper import USER_AGENT, AccelaClient, ScrapeError, launch_browser
from .store import PermitStore
from .summarize import build_report

log = logging.getLogger("slc_permits")
SLC_TZ = ZoneInfo("America/Denver")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="slc_permits", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days-back", type=int, default=3,
                   help="search records opened in the last N days (default 3; overlap is fine, repeats are skipped)")
    p.add_argument("--start", type=date.fromisoformat, help="start date YYYY-MM-DD (overrides --days-back)")
    p.add_argument("--end", type=date.fromisoformat, help="end date YYYY-MM-DD (default today)")
    p.add_argument("--record-type", help="only records whose type contains this text, e.g. 'Commercial'")
    p.add_argument("--no-details", action="store_true", help="skip opening each new record's detail page")
    p.add_argument("--no-summary", action="store_true", help="skip the Claude summary; write the table only")
    p.add_argument("--dry-run", action="store_true", help="don't record permits as seen")
    p.add_argument("--headful", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if args.headful:
        cfg.headless = False

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
            new = store.new_only(found)
            log.info("%d records in window, %d new", len(found), len(new))
            if not args.no_details:
                for i, rec in enumerate(new, 1):
                    if rec.get("detail_url") and not rec.get("detail_text"):
                        log.info("Detail %d/%d: %s", i, len(new), rec["record_number"])
                        try:
                            rec["detail_text"] = client.fetch_detail(rec["detail_url"])
                        except Exception as exc:
                            log.warning("Could not load detail for %s: %s", rec["record_number"], exc)
        except ScrapeError as exc:
            log.error("%s (debug snapshots in %s/)", exc, cfg.debug_dir)
            return 1
        except Exception:
            client.save_debug("unexpected-error")
            raise
        finally:
            browser.close()

    report = build_report(new, start, end, cfg.summary_model, use_llm=not args.no_summary)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = cfg.reports_dir / f"{today.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    (cfg.reports_dir / "latest.md").write_text(report, encoding="utf-8")
    log.info("Wrote %s", report_path)

    # Show the digest on the GitHub Actions run page.
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report)

    if not args.dry_run:
        store.add(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
