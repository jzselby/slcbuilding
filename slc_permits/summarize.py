"""Turn a batch of new permits into a Markdown digest."""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date

import anthropic

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You write a concise daily digest of new building permit records filed with \
Salt Lake City, Utah, for a reader who tracks local construction and development.

You will receive the new records as JSON. Fields come from the city's Accela \
Citizen Access portal; `detail_text` (when present) is the raw text of the \
record's detail page and may include job value, applicant, contractor, \
parcel and scope information.

Write GitHub-flavored Markdown with these sections:
1. **Highlights** - 3-7 bullets on the most notable records: new construction, \
large job values, multi-family or commercial projects, demolitions, \
unusual scopes. Cite each by record number and address.
2. **By the numbers** - counts by record type and by status.
3. **Themes** - short notes on patterns (geographic clusters, repeat \
applicants or contractors, many similar small jobs).

Rules: only state facts present in the data; do not invent job values, \
neighborhoods or contractors. If a field is missing, leave it out rather \
than guessing. Do not include a full list of records - the caller appends one.\
"""


def records_table(records: list[dict]) -> str:
    """Deterministic Markdown table of every record, grouped by record type."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_type[r.get("record_type") or "Unspecified type"].append(r)

    def cell(value: str | None) -> str:
        return (value or "").replace("|", "\\|")

    parts = []
    for rtype in sorted(by_type):
        parts.append(f"### {rtype} ({len(by_type[rtype])})\n")
        parts.append("| Record | Date | Address | Description | Status |")
        parts.append("|---|---|---|---|---|")
        for r in sorted(by_type[rtype], key=lambda r: r["record_number"]):
            number = cell(r["record_number"])
            if r.get("detail_url"):
                number = f"[{number}]({r['detail_url']})"
            parts.append(
                f"| {number} | {cell(r.get('date'))} | {cell(r.get('address'))} "
                f"| {cell(r.get('description') or r.get('project_name'))} | {cell(r.get('status'))} |"
            )
        parts.append("")
    return "\n".join(parts)


def claude_summary(records: list[dict], start: date, end: date, model: str) -> str | None:
    """Narrative summary from Claude, or None if unavailable or declined."""
    client = anthropic.Anthropic()
    payload = json.dumps(records, indent=1, sort_keys=True)
    prompt = (
        f"New Salt Lake City building permit records opened {start:%B %d, %Y} "
        f"through {end:%B %d, %Y} ({len(records)} records):\n\n{payload}"
    )
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            # On a safety-classifier decline, retry server-side on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        log.error("Anthropic API key rejected; writing report without a summary")
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Could not reach the Anthropic API (%s); writing report without a summary", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.error("Anthropic API error %s: %s; writing report without a summary", exc.status_code, exc.message)
        return None

    if message.stop_reason == "refusal":
        log.warning("Summary request was declined; writing report without a summary")
        return None
    if message.stop_reason == "max_tokens":
        log.warning("Summary hit max_tokens and may be cut off")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text or None


def build_report(records: list[dict], start: date, end: date, model: str, use_llm: bool = True) -> str:
    header = f"# New SLC building permits: {start:%b %d} – {end:%b %d, %Y}\n"
    if not records:
        return header + "\nNo new records in this window.\n"

    summary = None
    if use_llm and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        summary = claude_summary(records, start, end, model)
    elif use_llm:
        log.info("No ANTHROPIC_API_KEY set; skipping the Claude summary")

    parts = [header, f"**{len(records)} new record(s).**\n"]
    if summary:
        parts += [summary, ""]
    parts += ["## All new records\n", records_table(records)]
    return "\n".join(parts)
