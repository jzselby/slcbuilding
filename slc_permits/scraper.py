"""Drive the Accela Citizen Access site with Playwright.

ACA is an ASP.NET WebForms app: the search and paging buttons fire AJAX
postbacks that swap the results grid in place, so after each click we wait
for the grid's content to change rather than for a navigation.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import Browser, Frame, Page, Playwright, TimeoutError as PlaywrightTimeout

from .config import Config
from .parse import (
    RESULTS_TABLE_SELECTOR,
    describe_grid,
    parse_detail_record_number,
    parse_detail_text,
    parse_results,
)

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

SEARCH_FORM = "ctl00_PlaceHolderMain_generalSearchForm"
START_DATE = f"#{SEARCH_FORM}_txtGSStartDate"
END_DATE = f"#{SEARCH_FORM}_txtGSEndDate"
RECORD_TYPE = f"#{SEARCH_FORM}_ddlGSPermitType"
SEARCH_BUTTON = "#ctl00_PlaceHolderMain_btnNewSearch"
MY_RECORDS_ONLY = "#ctl00_PlaceHolderMain_chkSearch"
# The portal occasionally ignores a click on Search or Next; try this many times.
CLICK_ATTEMPTS = 3
# Seconds to wait for a late results grid before accepting "no results".
EMPTY_GRACE_S = 3
# True while an ASP.NET UpdatePanel postback is in flight; null on pages without one.
IN_POSTBACK_JS = """() => { try { return Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack(); }
                          catch (e) { return null; } }"""

USERNAME_INPUTS = (
    "input[id$='txtUserId']",
    "input[id*='username' i]",
    "input[name*='username' i]",
    "input[type='email']",
)
LOGIN_BUTTONS = (
    "[id$='btnLogin']",
    "button:has-text('Sign In')",
    "a:has-text('Sign In')",
    "input[type='submit'][value*='Sign In' i]",
    "button:has-text('Log In')",
    "button:has-text('Login')",
)
# No word boundaries: SLC's header renders an icon ligature straight into the
# link text ("lock_openLogout").
LOGGED_IN_TEXT = re.compile(r"Logout|Log Out|Sign Out|Logged in as", re.I)
LOGOUT_LINK = "[id$='btnLogout']"
DETAIL_EXPANDERS = re.compile(r"^\s*(More Details|Additional Information|Application Information)\s*$", re.I)


class ScrapeError(RuntimeError):
    pass


def launch_browser(pw: Playwright, cfg: Config) -> Browser:
    return pw.chromium.launch(headless=cfg.headless, executable_path=cfg.chromium_executable)


def aca_date(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class AccelaClient:
    def __init__(self, page: Page, cfg: Config):
        self.page = page
        self.cfg = cfg
        page.set_default_timeout(cfg.timeout_ms)

    # --- diagnostics -------------------------------------------------------

    def save_debug(self, name: str) -> None:
        """Screenshot + HTML of the current page, for troubleshooting selectors."""
        try:
            self.cfg.debug_dir.mkdir(parents=True, exist_ok=True)
            stem = self.cfg.debug_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
            self.page.screenshot(path=f"{stem}.png", full_page=True)
            Path(f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            log.info("Saved debug snapshot %s.{png,html}", stem)
        except Exception as exc:  # diagnostics must never mask the real error
            log.warning("Could not save debug snapshot: %s", exc)
        log.info("Page at failure:\n%s", self.describe_page())

    def describe_page(self, max_text: int = 2500) -> str:
        """Text outline of the page (frames, form controls, visible text) for the run log.

        Snapshots can be hard to get at, but the log is always there. Field
        values are never included, so credentials can't leak into it.
        """
        lines = [f"URL: {self.page.url}"]
        try:
            lines.append(f"Title: {self.page.title()}")
        except Exception:
            pass
        for frame in self.page.frames:
            try:
                controls = frame.locator("input:not([type=hidden]), button, select, a[id*='btn' i], a[id*='login' i]").evaluate_all(
                    """els => els.filter(e => e.offsetParent !== null).slice(0, 40).map(e => [
                        e.tagName.toLowerCase(), e.type || '', e.id || '', e.name || '',
                        (e.tagName === 'INPUT' && !['submit', 'button'].includes(e.type)) ? '' : (e.innerText || e.value || '').trim().slice(0, 40)
                    ].join(' | '))"""
                )
                text = re.sub(r"\n\s*\n+", "\n", frame.locator("body").inner_text(timeout=3000)).strip()
            except Exception as exc:
                lines.append(f"-- frame {frame.url}: unreadable ({exc.__class__.__name__})")
                continue
            lines.append(f"-- frame {frame.url}")
            lines += [f"   control: {c}" for c in controls]
            lines.append("   text: " + text[:max_text].replace("\n", "\n         "))
        return "\n".join(lines)

    # --- login -------------------------------------------------------------

    def _find_in_frames(self, selectors: tuple[str, ...]) -> tuple[Frame, str] | None:
        for frame in self.page.frames:
            for sel in selectors:
                try:
                    if frame.locator(sel).first.is_visible():
                        return frame, sel
                except Exception:
                    continue
        return None

    def login(self) -> None:
        if not (self.cfg.username and self.cfg.password):
            log.info("No ACCELA_USERNAME/ACCELA_PASSWORD set; searching anonymously")
            return
        log.info("Logging in as %s", self.cfg.username)
        self.page.goto(self.cfg.login_url, wait_until="load")

        # SLC renders the login box inside an iframe (an Angular panel) a moment
        # after the page loads, so give it time to appear.
        deadline = time.monotonic() + 20
        found = self._find_in_frames(("input[type='password']",))
        while not found and time.monotonic() < deadline:
            time.sleep(0.5)
            found = self._find_in_frames(("input[type='password']",))
        if not found:
            self.save_debug("login-form-missing")
            raise ScrapeError("Could not find a password field on the login page")
        frame, _ = found
        user_sel = next(
            (s for s in USERNAME_INPUTS if frame.locator(s).count() and frame.locator(s).first.is_visible()),
            None,
        )
        if user_sel is None:
            self.save_debug("login-username-missing")
            raise ScrapeError("Could not find the username field on the login page")

        frame.locator(user_sel).first.fill(self.cfg.username)
        frame.locator("input[type='password']").first.fill(self.cfg.password)
        button = next(
            (s for s in LOGIN_BUTTONS if frame.locator(s).count() and frame.locator(s).first.is_visible()),
            None,
        )
        log.info("Login form in frame %s; username field %r; submit via %r", frame.url, user_sel, button or "Enter key")
        if button:
            frame.locator(button).first.click()
        else:
            frame.locator("input[type='password']").first.press("Enter")

        try:
            self.page.wait_for_load_state("networkidle")
        except PlaywrightTimeout:
            pass
        if not self._logged_in():
            self.save_debug("login-failed")
            raise ScrapeError("Login did not succeed; check ACCELA_USERNAME / ACCELA_PASSWORD")
        log.info("Logged in")

    def _logged_in(self, wait_s: float = 15) -> bool:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for frame in self.page.frames:
                try:
                    if frame.locator(LOGOUT_LINK).count() or LOGGED_IN_TEXT.search(
                        frame.locator("body").inner_text(timeout=2000)
                    ):
                        return True
                except Exception:
                    continue
            time.sleep(0.5)
        return False

    # --- search ------------------------------------------------------------

    def _set_date(self, selector: str, value: str) -> None:
        # The date boxes carry an input mask and a watermark; set the value
        # directly and fire the events ACA's validators listen for.
        field = self.page.locator(selector)
        field.click()
        field.fill(value)
        field.evaluate(
            """(el, v) => { el.value = v;
                 el.dispatchEvent(new Event('change', {bubbles: true}));
                 el.dispatchEvent(new Event('blur', {bubbles: true})); }""",
            value,
        )

    def _select_record_type(self, wanted: str) -> None:
        options = self.page.locator(f"{RECORD_TYPE} option").evaluate_all(
            "opts => opts.map(o => ({value: o.value, text: o.textContent.trim()}))"
        )
        match = next((o for o in options if wanted.lower() in o["text"].lower()), None)
        if match is None:
            choices = ", ".join(o["text"] for o in options if o["value"])
            raise ScrapeError(f"No record type matching {wanted!r}. Options: {choices}")
        log.info("Record type filter: %s", match["text"])
        self.page.select_option(RECORD_TYPE, value=match["value"])

    def _grid_signature(self) -> str | None:
        """Fingerprint of the results grid, used to detect when a postback lands."""
        table = self.page.locator(RESULTS_TABLE_SELECTOR)
        if not table.count():
            return None
        return table.first.inner_text()

    def _wait_idle(self, timeout_s: float = 20) -> None:
        """Let any postback the page started on its own finish, so it isn't mistaken for ours."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if not self.page.evaluate(IN_POSTBACK_JS):
                    return
            except Exception:
                pass  # mid-navigation
            time.sleep(0.25)
        log.warning("Page still busy after %ss; continuing", timeout_s)

    def _wait_for_results(self, previous: str | None) -> str:
        """Wait for a search or paging postback to land.

        Returns "grid" (results grid present, possibly empty), "empty" (no grid
        at all), or "detail" (ACA jumped straight to the only matching record).
        """
        deadline = time.monotonic() + self.cfg.timeout_ms / 1000
        saw_postback = False
        while time.monotonic() < deadline:
            if "CapDetail.aspx" in self.page.url:
                return "detail"
            sig = self._grid_signature()
            if sig is not None and sig != previous:
                return "grid"
            try:
                busy = self.page.evaluate(IN_POSTBACK_JS)
            except Exception:
                busy = None  # mid-navigation
            if busy:
                saw_postback = True
            elif saw_postback and busy is False:
                # The postback finished without changing the grid (e.g. an empty
                # result replaced an empty result).
                if sig is None:
                    # Give a late grid a moment before concluding there are no results.
                    time.sleep(EMPTY_GRACE_S)
                    if "CapDetail.aspx" in self.page.url:
                        return "detail"
                    if self._grid_signature() is not None:
                        return "grid"
                    log.warning("Search finished without a results grid; treating as no results")
                    log.info("Page:\n%s", self.describe_page(max_text=800))
                    return "empty"
                return "grid"
            time.sleep(0.25)
        return "timeout"

    def _click_for_results(self, target, what: str) -> str:
        """Click Search or Next and wait for the grid to update, retrying a click the portal ignores.

        `target` returns the element to click (looked up afresh on each attempt).
        """
        for attempt in range(1, CLICK_ATTEMPTS + 1):
            self._wait_idle()
            before = self._grid_signature()
            target().click()
            outcome = self._wait_for_results(before)
            if outcome != "timeout":
                return outcome
            log.warning("%s got no response (attempt %d of %d)", what, attempt, CLICK_ATTEMPTS)
        self.save_debug("results-timeout")
        raise ScrapeError(f"Timed out waiting for search results after {what}")

    def _next_page_link(self):
        link = self.page.locator(
            f"{RESULTS_TABLE_SELECTOR} tr.ACA_Table_Pages a, "
            f"{RESULTS_TABLE_SELECTOR} tr[class*='Pages'] a"
        ).filter(has_text=re.compile(r"^\s*Next\b", re.I))
        return link.first if link.count() else None

    def search(self, start: date, end: date, record_type: str | None = None, module: str = "Building") -> list[dict]:
        """Records opened between start and end in one portal tab, each tagged with its module."""
        records = self._search(start, end, record_type, module)
        for rec in records:
            rec["module"] = module
        return records

    def _search(self, start: date, end: date, record_type: str | None, module: str) -> list[dict]:
        log.info("Searching %s records opened %s to %s", module, aca_date(start), aca_date(end))
        url = self.cfg.search_url(module)
        self.page.goto(url, wait_until="load")
        try:
            self.page.wait_for_selector(START_DATE, state="visible")
        except PlaywrightTimeout:
            self.save_debug("search-form-missing")
            raise ScrapeError(f"Search form not found at {url}")

        # Logged-in users get a "Search my records only" box; we want everyone's records.
        my_only = self.page.locator(MY_RECORDS_ONLY)
        if my_only.count() and my_only.is_checked():
            log.info("Unchecking 'Search my records only'")
            my_only.uncheck()
            try:
                self.page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass

        self._set_date(START_DATE, aca_date(start))
        self._set_date(END_DATE, aca_date(end))
        if record_type:
            self._select_record_type(record_type)

        outcome = self._click_for_results(lambda: self.page.locator(SEARCH_BUTTON), "Search")
        if outcome == "empty":
            log.info("No records found")
            return []
        if outcome == "detail":
            html = self.page.content()
            number = parse_detail_record_number(html)
            if not number:
                self.save_debug("single-result-unparsed")
                raise ScrapeError("Search jumped to a detail page but its record number was not found")
            return [{"record_number": number, "detail_url": self.page.url, "detail_text": parse_detail_text(html)}]

        records: dict[str, dict] = {}
        for page_no in range(1, self.cfg.max_pages + 1):
            html = self.page.content()
            rows = parse_results(html, self.page.url)
            if page_no == 1 and rows and not any(r.get("record_type") or r.get("address") for r in rows):
                log.warning("Could not read the %s grid's columns; first rows:\n%s", module, describe_grid(html))
            for row in rows:
                records.setdefault(row["record_number"], row)
            log.info("Page %d: %d rows (%d unique so far)", page_no, len(rows), len(records))
            if self._next_page_link() is None:
                break
            if self._click_for_results(self._next_page_link, f"Next (from page {page_no})") != "grid":
                break
        else:
            log.warning("Stopped after max_pages=%d; results may be incomplete", self.cfg.max_pages)
        return list(records.values())

    # --- record detail -----------------------------------------------------

    def fetch_detail(self, url: str) -> str:
        self.page.goto(url, wait_until="load")
        # Expand the collapsed sections so their text is included.
        expanders = self.page.locator("a").filter(has_text=DETAIL_EXPANDERS)
        for i in range(expanders.count()):
            try:
                expanders.nth(i).click(timeout=3000)
            except Exception:
                pass
        return parse_detail_text(self.page.content())
