"""Remember which permits have already been reported.

Stored as append-only JSON Lines so the daily commit is a small, readable diff.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class PermitStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict] = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rec = json.loads(line)
                        self.records[rec["record_number"]] = rec

    def __contains__(self, record_number: str) -> bool:
        return record_number in self.records

    def __len__(self) -> int:
        return len(self.records)

    def new_only(self, records: list[dict]) -> list[dict]:
        return [r for r in records if r["record_number"] not in self]

    def add(self, records: list[dict]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as f:
            for rec in records:
                # Detail-page text is only needed for the summary; keep the store small.
                rec = {k: v for k, v in rec.items() if k != "detail_text"}
                rec["first_seen"] = seen_at
                self.records[rec["record_number"]] = rec
                f.write(json.dumps(rec, sort_keys=True) + "\n")

    def all(self) -> list[dict]:
        return [dict(r) for r in self.records.values()]

    def update(self, records: list[dict]) -> None:
        """Merge new fields into stored records and rewrite the file (e.g. after re-rating)."""
        for rec in records:
            stored = self.records.get(rec["record_number"])
            if stored is not None:
                stored.update({k: v for k, v in rec.items() if k not in ("detail_text", "first_seen")})
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in self.records.values():
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        tmp.replace(self.path)
