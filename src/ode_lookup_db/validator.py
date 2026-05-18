"""All failsafes for the database pipeline.

Hard failures abort the commit. Soft warnings are returned so the workflow
can file an issue without blocking the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from . import ALLOWED_SYSTEMS

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "disc.schema.json"

# Configurable failsafe thresholds.
MAX_NEW_ROWS_PER_RUN = 500


class HardFailure(Exception):
    """A failsafe that aborts the commit."""


@dataclass
class ValidationReport:
    hard_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard_errors


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("rb") as f:
        return json.load(f)


def validate_rows(
    rows: Iterable[dict[str, Any]],
    *,
    previous_row_count: int | None = None,
    previous_ids: set[int] | None = None,
    max_new_rows: int = MAX_NEW_ROWS_PER_RUN,
) -> ValidationReport:
    """Run every failsafe over a full row set.

    Hard failures (commit-aborting):
      - schema validation (per row)
      - required-field check
      - hash format (covered by schema regex)
      - duplicate redump_id
      - system not in allowlist
      - growth cap exceeded

    Soft warnings (issue-filed but commit proceeds):
      - row count shrank vs. previous (possible legitimate redump deletions)
    """
    report = ValidationReport()
    schema = load_schema()
    validator = Draft202012Validator(schema)

    seen_ids: set[int] = set()
    rows_list = list(rows)

    for i, row in enumerate(rows_list):
        for err in validator.iter_errors(row):
            report.hard_errors.append(
                f"row {i} (redump_id={row.get('redump_id')!r}): schema: {err.message}"
            )

        rid = row.get("redump_id")
        if rid in seen_ids:
            report.hard_errors.append(f"duplicate redump_id {rid}")
        elif isinstance(rid, int):
            seen_ids.add(rid)

        system = row.get("system")
        if system not in ALLOWED_SYSTEMS:
            report.hard_errors.append(
                f"row {i} (redump_id={rid!r}): system {system!r} not in allowlist"
            )

    # Growth cap and shrink-warn use the previous snapshot.
    if previous_ids is not None:
        new_ids = seen_ids - previous_ids
        removed_ids = previous_ids - seen_ids
        if len(new_ids) > max_new_rows:
            report.hard_errors.append(
                f"growth cap exceeded: {len(new_ids)} new rows > {max_new_rows} "
                "(possible scraper bug or upstream structural change)"
            )
        if removed_ids:
            sample = sorted(removed_ids)[:20]
            report.warnings.append(
                f"row count shrank: {len(removed_ids)} IDs removed "
                f"(sample: {sample}). Verify these were legitimate upstream removals."
            )
    elif previous_row_count is not None and len(rows_list) < previous_row_count:
        report.warnings.append(
            f"row count shrank from {previous_row_count} to {len(rows_list)}"
        )

    return report
