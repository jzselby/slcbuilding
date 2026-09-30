import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from slc_permits.__main__ import main
from slc_permits.config import Config
from slc_permits.scraper import AccelaClient, ScrapeError, launch_browser
from slc_permits.store import PermitStore
from slc_permits.summarize import build_report

from .mock_aca import PASSWORD, USER, MockACA, matching


@pytest.fixture(scope="module")
def site():
    with MockACA() as mock:
        yield mock


@pytest.fixture
def cfg(site, tmp_path):
    return Config(base_url=site.url, username=USER, password=PASSWORD, timeout_ms=10_000,
                  chromium_executable=os.environ.get("CHROMIUM_EXECUTABLE"),
                  data_dir=tmp_path / "data", reports_dir=tmp_path / "reports", debug_dir=tmp_path / "debug")


@pytest.fixture
def client(cfg):
    with sync_playwright() as pw:
        browser = launch_browser(pw, cfg)
        yield AccelaClient(browser.new_page(), cfg)
        browser.close()


def test_login_and_paged_search(client):
    client.login()
    found = client.search(date(2026, 9, 1), date(2026, 9, 5))
    assert len(found) == 23  # three pages: 10 + 10 + 3
    first = next(r for r in found if r["record_number"] == "BLD2026-00001")
    assert first["record_type"] == "Commercial Alteration"
    assert first["date"] == "09/02/2026"
    assert first["address"].startswith("101 S 1 E")
    assert first["status"] == "In Review"
    assert first["detail_url"].endswith("capID1=BLD2026-00001")
    assert "action" not in first


def test_record_type_filter(client):
    found = client.search(date(2026, 9, 1), date(2026, 9, 5), record_type="demolition")
    expected = matching(date(2026, 9, 1), date(2026, 9, 5), "Demolition")
    assert {r["record_number"] for r in found} == {r["number"] for r in expected}


def test_unknown_record_type_lists_choices(client):
    with pytest.raises(ScrapeError, match="Commercial Alteration"):
        client.search(date(2026, 9, 1), date(2026, 9, 5), record_type="spaceport")


def test_no_results(client):
    assert client.search(date(2025, 1, 1), date(2025, 1, 2)) == []


def test_empty_search_after_empty_search(client):
    # The grid looks the same before and after, so only the postback flag says it's done.
    client.search(date(2025, 1, 1), date(2025, 1, 2))
    client._set_date("#ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate", "01/03/2025")
    before = client._grid_signature()
    client.page.locator("#ctl00_PlaceHolderMain_btnNewSearch").click()
    assert client._wait_for_results(before) == "grid"


def test_single_result_redirects_to_detail(client):
    found = client.search(date(2026, 8, 15), date(2026, 8, 15))
    assert [r["record_number"] for r in found] == ["BLD2026-09999"]
    assert "Lone record" in found[0]["detail_text"]


def test_detail_expands_hidden_sections(client):
    text = client.fetch_detail(f"{client.cfg.base_url}/Cap/CapDetail.aspx?Module=Building&capID1=BLD2026-00003")
    assert "Scope of work #3" in text
    assert "$100,000.00" in text  # inside the collapsed "More Details" block
    assert "should not appear" not in text


def test_bad_password_fails_with_snapshot(client, cfg):
    cfg.password = "wrong"
    with pytest.raises(ScrapeError, match="Login did not succeed"):
        client.login()
    assert list(Path(cfg.debug_dir).glob("*login-failed.png"))


@pytest.fixture
def run_env(site, tmp_path, monkeypatch):
    monkeypatch.setenv("ACCELA_BASE_URL", site.url)
    monkeypatch.setenv("ACCELA_USERNAME", USER)
    monkeypatch.setenv("ACCELA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PERMITS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PERMITS_REPORTS_DIR", str(tmp_path / "reports"))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GITHUB_STEP_SUMMARY", "GOOGLE_SHEET_ID"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_end_to_end_only_reports_new(run_env):
    tmp_path = run_env
    assert main(["--start", "2026-09-01", "--end", "2026-09-02", "--all-types", "--modules", "Building"]) == 0
    first_batch = {r["number"] for r in matching(date(2026, 9, 1), date(2026, 9, 2), "")}
    store = PermitStore(tmp_path / "data" / "permits.jsonl")
    assert set(store.records) == first_batch
    assert all("detail_text" not in r for r in store.records.values())
    assert store.records["BLD2026-00000"]["job_value"] == 25_000.0  # read off the detail page
    report = (tmp_path / "reports" / "latest.md").read_text()
    assert f"**{len(first_batch)} new record(s).**" in report

    # Overlapping window: only the records not seen before are new.
    assert main(["--start", "2026-09-01", "--end", "2026-09-05", "--no-details", "--all-types", "--modules", "Building"]) == 0
    report = (tmp_path / "reports" / "latest.md").read_text()
    assert f"**{23 - len(first_batch)} new record(s).**" in report
    assert "BLD2026-00000" not in report  # 09/01, reported in the first run
    assert len(PermitStore(tmp_path / "data" / "permits.jsonl")) == 23


def test_type_filters_per_module():
    from slc_permits.__main__ import parse_args
    assert parse_args([]).filters == {"Building": ["Commercial", "Commericial"]}
    args = parse_args(["--types", "Building=Commercial, Sign", "--types", "Planning=Site Plan"])
    assert args.filters == {"Building": ["Commercial", "Sign"], "Planning": ["Site Plan"]}
    assert parse_args(["--types", "Building="]).filters == {}  # blank keeps every Building type
    assert parse_args(["--types", "Building=x", "--all-types"]).filters == {}
    assert parse_args(["--modules", "Planning, Building"]).modules == ["Planning", "Building"]


def test_default_is_commercial_building_plus_all_planning(run_env):
    assert main(["--start", "2026-09-01", "--end", "2026-09-05", "--no-details"]) == 0
    store = PermitStore(run_env / "data" / "permits.jsonl")
    building = {r["number"] for r in matching(date(2026, 9, 1), date(2026, 9, 5), "Commercial Alteration")}
    planning = {r["number"] for r in matching(date(2026, 9, 1), date(2026, 9, 5), "", "Planning")}
    assert len(planning) == 4
    assert set(store.records) == building | planning
    assert {r["module"] for r in store.records.values() if r["record_number"] in planning} == {"Planning"}
    assert {r["record_type"] for r in store.records.values() if r["module"] == "Building"} == {"Commercial Alteration"}
    first_planning = store.records["PLNSUB2026-00000"]
    assert first_planning["record_type"] == "Site Plan Review"  # from the "Petition Type" column
    assert first_planning["address"].startswith("500 W North Temple")  # from the unlabeled column
    assert "detail_url" not in store.records["PLNSUB2026-00001"]  # listed without a link, still kept
    report = (run_env / "reports" / "latest.md").read_text()
    assert "### Planning: Site Plan Review" in report


def test_dry_run_records_nothing(run_env):
    assert main(["--start", "2026-09-01", "--end", "2026-09-05", "--no-details", "--dry-run"]) == 0
    assert len(PermitStore(run_env / "data" / "permits.jsonl")) == 0
    assert not (run_env / "reports").exists()  # a dry run must not overwrite the day's report


def test_report_without_records():
    assert "No new records" in build_report([], date(2026, 9, 1), date(2026, 9, 2))


def test_report_table_escapes_pipes():
    rec = {"record_number": "B-1", "record_type": "Demo", "description": "a | b", "detail_url": "http://x/1"}
    report = build_report([rec], date(2026, 9, 1), date(2026, 9, 2))
    assert "a \\| b" in report
    assert "[B-1](http://x/1)" in report
