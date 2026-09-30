"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://aca-prod.accela.com/SLCREF"


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    module: str = "Building"
    username: str | None = None
    password: str | None = None
    headless: bool = True
    # Use an already-installed Chromium instead of Playwright's download.
    chromium_executable: str | None = None
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    debug_dir: Path = Path("debug")
    summary_model: str = "claude-opus-5-5"
    # Safety cap on result pages walked per search (ACA shows 10 rows per page).
    max_pages: int = 100
    # Milliseconds to wait for ACA's AJAX postbacks to finish.
    timeout_ms: int = 60_000

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/Login.aspx"

    @property
    def search_url(self) -> str:
        return f"{self.base_url}/Cap/CapHome.aspx?module={self.module}&TabName={self.module}"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            base_url=env.get("ACCELA_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            module=env.get("ACCELA_MODULE", "Building"),
            username=env.get("ACCELA_USERNAME") or None,
            password=env.get("ACCELA_PASSWORD") or None,
            headless=env.get("HEADLESS", "1") != "0",
            chromium_executable=env.get("CHROMIUM_EXECUTABLE") or None,
            data_dir=Path(env.get("PERMITS_DATA_DIR", "data")),
            reports_dir=Path(env.get("PERMITS_REPORTS_DIR", "reports")),
            debug_dir=Path(env.get("PERMITS_DEBUG_DIR", "debug")),
            summary_model=env.get("SUMMARY_MODEL", "claude-opus-5-5"),
            max_pages=int(env.get("ACCELA_MAX_PAGES", "100")),
            timeout_ms=int(env.get("ACCELA_TIMEOUT_MS", "60000")),
        )
