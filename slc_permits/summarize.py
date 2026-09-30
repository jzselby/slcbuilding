"""Turn a batch of new permits into a Markdown digest and per-permit notes."""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You review new records filed with Salt Lake City, Utah, for a reader who \
tracks local commercial construction and development.

You will receive the new records as JSON from the city's Accela Citizen \
Access portal. `module` says which part of the portal each came from: \
"Building" records are commercial building permits; "Planning" records are \
land-use applications (site plans, zoning, subdivisions, design review and \
the like), which often signal projects before building permits are filed. \
`detail_text` (when present) is the raw text of the record's detail page and \
may include job value, applicant, contractor, parcel and scope information.

Return two things:

`permits` - one entry per input record, same record_number, with:
- scope: one plain-English sentence on what the work or proposal is (e.g. \
  "Tenant improvement for a 4,000 sq ft restaurant on the ground floor").
- job_value: the declared job/project valuation in dollars, if stated.
- applicant, contractor: names as written, if stated.
- notable: true for new buildings, additions, large job values, \
  multi-family or mixed-use, demolitions, rezonings or large site plans, or \
  otherwise unusual scopes.

`summary_markdown` - a short GitHub-flavored Markdown digest with sections \
**Highlights** (3-7 bullets on the most notable records across both \
modules, each citing record number and address), **Planning pipeline** \
(one or two sentences on the Planning applications; omit if there are \
none), **By the numbers** (counts by module and record type), and \
**Themes** (patterns such as geographic clusters, repeat applicants or \
contractors, or planning applications that match building permits). Do not \
list every record; the caller shows a full table.

Only state facts present in the data. Use null for anything not stated \
rather than guessing.\
"""


class PermitNotes(BaseModel):
    record_number: str
    scope: str = Field(description="One plain-English sentence describing the work")
    job_value: float | None = Field(description="Declared job value in US dollars, or null")
    applicant: str | None
    contractor: str | None
    notable: bool


class Digest(BaseModel):
    summary_markdown: str
    permits: list[PermitNotes]

    def notes_by_record(self) -> dict[str, PermitNotes]:
        return {p.record_number: p for p in self.permits}


# Detail pages run ~30k characters, mostly portal boilerplate below the record's
# key facts; Claude gets the start of each. Job value is parsed from the full page.
DETAIL_CHARS = 6_000
# Records per Claude request are capped by payload size (~4 chars/token) so a
# large backfill is split well under the 1M-token context window.
MAX_PAYLOAD_CHARS = 1_200_000


def _for_prompt(rec: dict) -> dict:
    rec = dict(rec)
    if len(rec.get("detail_text") or "") > DETAIL_CHARS:
        rec["detail_text"] = rec["detail_text"][:DETAIL_CHARS] + " [...]"
    return rec


def _batches(records: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = [[]]
    size = 0
    for rec in records:
        n = len(json.dumps(rec))
        if batches[-1] and size + n > MAX_PAYLOAD_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(rec)
        size += n
    return batches


def analyze(records: list[dict], start: date, end: date, model: str) -> Digest | None:
    """Ask Claude for a digest plus structured per-permit notes; None if unavailable."""
    if not records:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.info("No ANTHROPIC_API_KEY set; skipping the Claude summary")
        return None

    client = anthropic.Anthropic()
    batches = _batches([_for_prompt(r) for r in records])
    digests = []
    for i, batch in enumerate(batches, 1):
        if len(batches) > 1:
            log.info("Claude request %d of %d (%d records)", i, len(batches), len(batch))
        digest = _analyze_batch(client, batch, start, end, model)
        if digest is not None:
            digests.append(digest)
    if not digests:
        return None
    if len(digests) == 1:
        return digests[0]
    summary = "\n\n".join(f"### Part {i} of {len(digests)}\n\n{d.summary_markdown.strip()}"
                           for i, d in enumerate(digests, 1))
    return Digest(summary_markdown=summary, permits=[p for d in digests for p in d.permits])


def _analyze_batch(client: anthropic.Anthropic, records: list[dict], start: date, end: date,
                   model: str) -> Digest | None:
    payload = json.dumps(records, indent=1, sort_keys=True)
    prompt = (
        f"New Salt Lake City records opened {start:%B %d, %Y} "
        f"through {end:%B %d, %Y} ({len(records)} records):\n\n{payload}"
    )
    try:
        # Streamed so a large first backfill can use a big output budget without an HTTP timeout.
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=Digest,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            # On a safety-classifier decline, retry server-side on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        log.error("Anthropic API key rejected; continuing without a summary")
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Could not reach the Anthropic API (%s); continuing without a summary", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.error("Anthropic API error %s: %s; continuing without a summary", exc.status_code, exc.message)
        return None

    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info("Claude usage: %s input tokens, %s output tokens", usage.input_tokens, usage.output_tokens)
    if response.stop_reason == "refusal":
        log.warning("Summary request was declined; continuing without a summary")
        return None
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        log.warning("Summary was cut off or unparseable (stop_reason=%s); continuing without it", response.stop_reason)
        return None
    return response.parsed_output


def apply_notes(records: list[dict], digest: Digest | None) -> None:
    """Merge Claude's per-permit notes into the records. Values read off the page win."""
    if digest is None:
        return
    notes = digest.notes_by_record()
    for rec in records:
        note = notes.get(rec["record_number"])
        if note is None:
            continue
        rec["scope"] = note.scope
        rec["notable"] = note.notable
        for field in ("job_value", "applicant", "contractor"):
            if rec.get(field) is None and getattr(note, field) is not None:
                rec[field] = getattr(note, field)


def records_table(records: list[dict]) -> str:
    """Deterministic Markdown table of every record, grouped by record type."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_type[f"{r.get('module', 'Building')}: {r.get('record_type') or 'Unspecified type'}"].append(r)

    def cell(value) -> str:
        return ("" if value is None else str(value)).replace("|", "\\|")

    parts = []
    for rtype in sorted(by_type):
        parts.append(f"### {rtype} ({len(by_type[rtype])})\n")
        parts.append("| Record | Date | Address | Scope | Job value | Status |")
        parts.append("|---|---|---|---|---|---|")
        for r in sorted(by_type[rtype], key=lambda r: r["record_number"]):
            number = cell(r["record_number"])
            if r.get("detail_url"):
                number = f"[{number}]({r['detail_url']})"
            value = f"${r['job_value']:,.0f}" if isinstance(r.get("job_value"), (int, float)) else ""
            scope = r.get("scope") or r.get("description") or r.get("project_name")
            parts.append(
                f"| {number} | {cell(r.get('date'))} | {cell(r.get('address'))} "
                f"| {cell(scope)} | {value} | {cell(r.get('status'))} |"
            )
        parts.append("")
    return "\n".join(parts)


def build_report(records: list[dict], start: date, end: date, digest: Digest | None = None) -> str:
    header = f"# New SLC commercial permits and planning applications: {start:%b %d} – {end:%b %d, %Y}\n"
    if not records:
        return header + "\nNo new records in this window.\n"
    parts = [header, f"**{len(records)} new record(s).**\n"]
    if digest:
        parts += [digest.summary_markdown.strip(), ""]
    parts += ["## All new records\n", records_table(records)]
    return "\n".join(parts)
