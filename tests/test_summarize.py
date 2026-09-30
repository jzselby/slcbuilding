from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from slc_permits import summarize
from slc_permits.parse import parse_job_value
from slc_permits.summarize import Digest, PermitNotes

RECORDS = [{"record_number": "BLD2026-00001", "record_type": "Commercial New", "address": "1 Main St"}]
DIGEST = Digest(
    summary_markdown="**Highlights**\n- New building at 1 Main St",
    permits=[PermitNotes(record_number="BLD2026-00001", scope="New 3-story office building",
                         job_value=2_500_000, applicant="Acme LLC", contractor=None, notable=True)],
)


def fake_client(response, calls):
    @contextmanager
    def stream(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(get_final_message=lambda: response)

    return lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def test_analyze_request_and_notes(monkeypatch):
    calls = []
    response = SimpleNamespace(stop_reason="end_turn", parsed_output=DIGEST)
    monkeypatch.setattr(summarize.anthropic, "Anthropic", fake_client(response, calls))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    records = [dict(r) for r in RECORDS]
    digest = summarize.analyze(records, date(2026, 9, 1), date(2026, 9, 2), "claude-opus-5-5")
    summarize.apply_notes(records, digest)
    report = summarize.build_report(records, date(2026, 9, 1), date(2026, 9, 2), digest)

    (kwargs,) = calls
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_format"] is Digest
    assert kwargs["fallbacks"] == "default"
    assert "BLD2026-00001" in kwargs["messages"][0]["content"]
    assert records[0]["scope"] == "New 3-story office building"
    assert records[0]["job_value"] == 2_500_000
    assert "contractor" not in records[0]
    assert report.index("**Highlights**") < report.index("## All new records")
    assert "$2,500,000" in report


def test_page_job_value_beats_claude():
    records = [dict(RECORDS[0], job_value=1_000.0)]
    summarize.apply_notes(records, DIGEST)
    assert records[0]["job_value"] == 1_000.0


def test_refusal_gives_no_digest(monkeypatch):
    response = SimpleNamespace(stop_reason="refusal", parsed_output=None)
    monkeypatch.setattr(summarize.anthropic, "Anthropic", fake_client(response, []))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert summarize.analyze(RECORDS, date(2026, 9, 1), date(2026, 9, 2), "m") is None


def test_no_key_skips_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(summarize.anthropic, "Anthropic", lambda: 1 / 0)
    assert summarize.analyze(RECORDS, date(2026, 9, 1), date(2026, 9, 2), "m") is None


def test_parse_job_value():
    assert parse_job_value("Job Value:\n$1,250,000.00\nApplicant") == 1_250_000.0
    assert parse_job_value("Job Value\n$0.00") == 0.0
    assert parse_job_value("No valuation listed") is None
