"""Turn a batch of new permits into a Markdown digest and per-permit notes."""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a city-desk editor screening new records filed with Salt Lake City, \
Utah, for reporters. They want what the public would care about: big \
projects, new businesses, housing, public projects and changes to the city. \
Routine day-to-day permits should stay out of their way.

You will receive the records as JSON from the city's Accela Citizen Access \
portal. `module` says which part of the portal each came from: "Building" \
records are commercial building permits; "Planning" records are land-use \
applications (rezonings, subdivisions, design review and the like), which \
often signal projects before building permits are filed. `detail_text` (when \
present) is the start of the record's detail page and may include job value, \
applicant, contractor, parcel and scope information. `scope` (when present) \
is an earlier one-line summary of the work.

Return two things:

`permits` - one entry per input record, same record_number, with:
- scope: one plain-English sentence on what the work or proposal is (e.g. \
  "Tenant improvement for a 4,000 sq ft restaurant on the ground floor").
- job_value: the declared job/project valuation in dollars, if stated.
- applicant, contractor: names as written, if stated.
- business: the business, tenant, institution or development that the work \
  is for (e.g. "Postino", "University of Utah", "The Hive on 11th"), if the \
  record names one; not the contractor or permit filer.
- importance:
  - "high": a reporter would want to know. New buildings or additions; \
    projects of $1M or more; housing of 10+ units; a new or relocating \
    business the public would recognize or notice (a named restaurant, store, \
    employer, venue or school); city, county, state, school-district, \
    transit or utility projects; demolition of a building; rezonings, \
    planned developments, conditional uses, design review and other Planning \
    Commission items; major subdivisions or condo conversions; new \
    construction in a historic district; anything likely to draw public \
    interest or controversy (shelters, data centers, cannabis, liquor, jails, \
    large parking lots, billboards).
  - "medium": possibly worth a look: mid-size tenant improvements ($250k \
    to $1M), smaller subdivisions or lot changes, notable equipment such as \
    large solar or battery installations, or a change of use.
  - "low": routine trade or maintenance work: re-roofs, electrical panels, \
    HVAC swaps, fixture replacements, sprinkler-head relocations, fire-alarm \
    upgrades, small remodels, test or placeholder records.
- category: the single best fit.
- why_it_matters: for high or medium, one sentence giving the news angle \
  (who, what, how big, where), e.g. "Postino is opening a 5,656 sq ft \
  restaurant in the Granary District, a $1.1M build-out." For low, a few \
  words such as "Routine electrical work."

`summary_markdown` - a short GitHub-flavored Markdown digest with sections \
**Highlights** (the high-importance records, most newsworthy first, each \
citing record number and address; at most 8), **Planning pipeline** (one or \
two sentences on the Planning applications; omit if there are none), and \
**Themes** (patterns such as geographic clusters, repeat developers, or \
planning applications that match building permits). Do not list every \
record; the caller shows a full table.

Only state facts present in the data. Use null for anything not stated \
rather than guessing.\
"""

Importance = Literal["high", "medium", "low"]
Category = Literal[
    "Major project", "New business", "Housing", "Public / government", "Demolition",
    "Land use / zoning", "Historic", "Energy / infrastructure", "Routine",
]


class PermitNotes(BaseModel):
    record_number: str
    scope: str = Field(description="One plain-English sentence describing the work")
    job_value: float | None = Field(description="Declared job value in US dollars, or null")
    applicant: str | None
    contractor: str | None
    business: str | None = Field(description="Business, tenant or development the work is for, or null")
    importance: Importance
    category: Category
    why_it_matters: str


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


def apply_notes(records: list[dict], digest: Digest | None, rescope: bool = True) -> None:
    """Merge Claude's per-permit notes into the records. Values read off the page win.

    With rescope=False (re-rating stored records) an existing scope is kept.
    """
    if digest is not None:
        notes = digest.notes_by_record()
        for rec in records:
            note = notes.get(rec["record_number"])
            if note is None:
                continue
            if rescope or not rec.get("scope"):
                rec["scope"] = note.scope
            rec["importance"] = note.importance
            rec["category"] = note.category
            rec["why_it_matters"] = note.why_it_matters
            for field in ("job_value", "applicant", "contractor", "business"):
                if rec.get(field) is None and getattr(note, field) is not None:
                    rec[field] = getattr(note, field)
    for rec in records:
        apply_rules(rec)


# Record types that are always high importance, whatever Claude says.
ALWAYS_HIGH_TYPES = ("Planning Commission",)


def apply_rules(rec: dict) -> None:
    """Deterministic importance floors, so big items surface even without Claude."""
    value = rec.get("job_value") if isinstance(rec.get("job_value"), (int, float)) else 0
    rtype = (rec.get("record_type") or "").lower()
    if value >= 1_000_000:
        rec["importance"] = "high"
        rec.setdefault("category", "Major project")
    elif any(t.lower() in rtype for t in ALWAYS_HIGH_TYPES):
        rec["importance"] = "high"
        rec.setdefault("category", "Land use / zoning")
    elif value >= 250_000 and rec.get("importance") in (None, "low"):
        rec["importance"] = "medium"
    rec["notable"] = rec.get("importance") == "high"


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
