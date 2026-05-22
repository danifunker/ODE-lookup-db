"""Build data/ode-lookup.sqlite from per-source JSONLs.

Sources ingested (each is optional — if its JSONL is missing it's skipped):
  data/redump.jsonl             -> redump_*       tables
  winworld/data/winworld.jsonl  -> winworld_*     tables
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import (                                          # noqa: E402
    JSONL_PATH,
    SQLITE_PATH,
    WINWORLD_JSONL_PATH,
    build_sqlite,
    read_jsonl,
    read_winworld_jsonl,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=SQLITE_PATH)
    ap.add_argument("--no-redump", action="store_true", help="Skip the redump source")
    ap.add_argument("--no-winworld", action="store_true", help="Skip the winworld source")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    commit = os.environ.get("GITHUB_SHA", "")

    rows = None if args.no_redump else (read_jsonl() if JSONL_PATH.exists() else None)
    if rows is None and not args.no_redump:
        logging.info("skipping redump (no jsonl at %s)", JSONL_PATH)

    ww = None if args.no_winworld else (
        read_winworld_jsonl() if WINWORLD_JSONL_PATH.exists() else None
    )
    if ww is None and not args.no_winworld:
        logging.info("skipping winworld (no jsonl at %s)", WINWORLD_JSONL_PATH)

    total = build_sqlite(rows, winworld_records=ww, path=args.out, source_commit=commit)
    logging.info("built %s with %d total rows", args.out, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
