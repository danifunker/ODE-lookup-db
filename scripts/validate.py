"""Run all failsafes over the JSONL file. Exit code 0 = ok, 1 = hard failure.

Soft warnings are printed and written to `data/warnings.json` for the workflow
to file as an issue.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import read_jsonl  # noqa: E402
from ode_lookup_db.validator import validate_rows  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PREV_STATS = REPO_ROOT / "data" / "stats.json"
WARNINGS_OUT = REPO_ROOT / "data" / "warnings.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    previous_ids: set[int] | None = None
    if PREV_STATS.exists():
        prev = json.loads(PREV_STATS.read_text())
        prev_ids_list = prev.get("ids")
        if prev_ids_list is not None:
            previous_ids = set(prev_ids_list)

    rows = list(read_jsonl())
    report = validate_rows(rows, previous_ids=previous_ids)

    for w in report.warnings:
        logging.warning(w)
    for e in report.hard_errors:
        logging.error(e)

    WARNINGS_OUT.write_text(json.dumps({"warnings": report.warnings}, indent=2))

    if not report.ok:
        logging.error("FAILED: %d hard errors", len(report.hard_errors))
        return 1
    logging.info("OK: %d rows, %d warnings", len(rows), len(report.warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
