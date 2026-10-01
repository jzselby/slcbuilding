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
                         job_value=2_500_000, applicant="Acme LLC", contractor=None, business="Acme",
                         importance="high", category="Major project",
                         why_it_matters="Acme is building a $2.5M, 3-story office at 1 Main St.")],
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


def test_long_detail_pages_are_trimmed_and_batched(monkeypatch):
    calls = []
    response = SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=Digest(
        summary_markdown="part", permits=[]))
    monkeypatch.setattr(summarize.anthropic, "Anthropic", fake_client(response, calls))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(summarize, "MAX_PAYLOAD_CHARS", 20_000)
    records = [{"record_number": f"R{i}", "detail_text": "x" * 50_000} for i in range(5)]

    digest = summarize.analyze(records, date(2026, 9, 1), date(2026, 9, 2), "m")

    assert len(calls) == 2  # ~6k chars per record after trimming: 3 fit in 20k, then 2
    assert all("x" * 6_001 not in c["messages"][0]["content"] for c in calls)
    assert digest.summary_markdown.startswith("### Part 1 of 2")
    assert len(records[0]["detail_text"]) == 50_000  # caller's records untouched


def test_apply_notes_sets_importance_and_rules():
    records = [dict(RECORDS[0])]
    summarize.apply_notes(records, DIGEST)
    rec = records[0]
    assert (rec["importance"], rec["category"], rec["business"], rec["notable"]) == (
        "high", "Major project", "Acme", True)
    assert rec["why_it_matters"].startswith("Acme is building")


def test_rules_raise_importance_without_claude():
    big = {"record_number": "A", "record_type": "Commercial Electrical", "job_value": 1_200_000.0}
    rezone = {"record_number": "B", "record_type": "Planning Commission - Zoning Amendment"}
    mid = {"record_number": "C", "record_type": "Commercial Building Permit", "job_value": 400_000.0,
           "importance": "low"}
    small = {"record_number": "D", "record_type": "Commercial Roofing", "job_value": 9_000.0}
    summarize.apply_notes([big, rezone, mid, small], None)
    assert [r.get("importance") for r in (big, rezone, mid, small)] == ["high", "high", "medium", None]
    assert big["category"] == "Major project" and rezone["category"] == "Land use / zoning"
    assert small["notable"] is False


def test_rescope_false_keeps_existing_scope():
    records = [dict(RECORDS[0], scope="Original scope")]
    summarize.apply_notes(records, DIGEST, rescope=False)
    assert records[0]["scope"] == "Original scope"
    assert records[0]["importance"] == "high"


def test_void_permits_are_low_even_if_large():
    rec = {"record_number": "A", "record_type": "Commercial Electrical", "job_value": 1_200_000.0,
           "status": "VOID", "importance": "high"}
    summarize.apply_notes([rec], None)
    assert rec["importance"] == "low" and rec["notable"] is False
