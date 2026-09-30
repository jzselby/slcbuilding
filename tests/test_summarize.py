from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from slc_permits import summarize

RECORDS = [{"record_number": "BLD2026-00001", "record_type": "Demolition", "address": "1 Main St"}]


def fake_client(message, calls):
    @contextmanager
    def stream(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(get_final_message=lambda: message)

    return lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def test_summary_is_inserted_above_table(monkeypatch):
    calls = []
    msg = SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text="## Highlights\n- A demolition at 1 Main St"),
    ])
    monkeypatch.setattr(summarize.anthropic, "Anthropic", fake_client(msg, calls))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    report = summarize.build_report(RECORDS, date(2026, 9, 1), date(2026, 9, 2), "claude-opus-5-5")

    assert report.index("## Highlights") < report.index("## All new records")
    (kwargs,) = calls
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["fallbacks"] == "default"
    assert "BLD2026-00001" in kwargs["messages"][0]["content"]


def test_refusal_falls_back_to_table_only(monkeypatch):
    msg = SimpleNamespace(stop_reason="refusal", content=[])
    monkeypatch.setattr(summarize.anthropic, "Anthropic", fake_client(msg, []))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    report = summarize.build_report(RECORDS, date(2026, 9, 1), date(2026, 9, 2), "claude-opus-5-5")

    assert "## Highlights" not in report
    assert "BLD2026-00001" in report
