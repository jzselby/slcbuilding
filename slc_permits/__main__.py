"""Search SLC's Accela portal for new commercial permits and planning applications,
summarize them, and log them to a Google Sheet.

    python -m slc_permits                                  # last 14 days: commercial Building + all Planning
    python -m slc_permits --modules Planning --dry-run --no-details --no-summary
    python -m slc_permits --types "Planning=Site Plan,Design Review"
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
# Record-type filters per module; a module without one keeps every type.
# SLC's portal spells one Building type "Commericial Demolition".
DEFAULT_TYPES = {"Building": ["Commercial", "Commericial"]}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="slc_permits", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days-back", type=int, default=14,
                   help="search records opened in the last N days (default 14; repeats are skipped)")
    p.add_argument("--start", type=date.fromisoformat, help="start date YYYY-MM-DD (overrides --days-back)")
    p.add_argument("--end", type=date.fromisoformat, help="end date YYYY-MM-DD (default today)")
    p.add_argument("--modules", metavar="A,B",
                   help="portal tabs to search, comma-separated (default: ACCELA_MODULES or Building,Planning)")
    p.add_argument("--types", action="append", metavar="MODULE=TEXT[,TEXT]",
                   help="keep that module's records whose Record Type contains any TEXT; 'MODULE=' keeps all. "
                        "Repeatable. Default: " + "; ".join(f"{m}={','.join(t)}" for m, t in DEFAULT_TYPES.items()))
    p.add_argument("--all-types", action="store_true", help="keep every record type in every module")
    p.add_argument("--no-details", action="store_true", help="skip opening each new record's detail page")
    p.add_argument("--no-summary", action="store_true", help="skip the Claude summary")
    p.add_argument("--no-sheet", action="store_true", help="don't write to the Google Sheet")
    p.add_argument("--dry-run", action="store_true", help="don't write to the sheet or record permits as seen")
    p.add_argument("--headful", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    filters = {m: list(t) for m, t in DEFAULT_TYPES.items()}
    for spec in args.types or []:
        module, sep, texts = spec.partition("=")
        if not sep or not module.strip():
            p.error(f"--types expects MODULE=TEXT[,TEXT], got {spec!r}")
        filters[module.strip()] = [t.strip() for t in texts.split(",") if t.strip()]
    args.filters = {} if args.all_types else {m: t for m, t in filters.items() if t}
    if args.modules is not None:
        args.modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    return args


def keep_types(module: str, records: list[dict], needles: list[str] | None) -> list[dict]:
    types = Counter(r.get("record_type") or "(none)" for r in records)
    log.info("%s record types in window: %s", module,
             ", ".join(f"{t} ({n})" for t, n in types.most_common()) or "none")
    if not needles:
        return records
    lowered = [n.lower() for n in needles]
    kept = [r for r in records if any(n in (r.get("record_type") or "").lower() for n in lowered)]
    log.info("%s: %d of %d records match %s", module, len(kept), len(records), " or ".join(repr(n) for n in needles))
    return kept


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if args.modules:
        cfg.modules = tuple(args.modules)
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
            wanted = []
            for module in cfg.modules:
                found = client.search(start, end, module=module)
                wanted += keep_types(module, found, args.filters.get(module))
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
    if args.dry_run:
        log.info("Dry run: %d record(s) would be added:\n%s", len(new),
                 "\n".join(f"  {r['module']}: {r['record_number']} {r.get('record_type', '')} | {r.get('address', '')}"
                           for r in new))
    else:
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
