"""Build data/redump.sqlite from data/redump.jsonl."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import SQLITE_PATH, build_sqlite, read_jsonl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=SQLITE_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    commit = os.environ.get("GITHUB_SHA", "")
    count = build_sqlite(read_jsonl(), path=args.out, source_commit=commit)
    logging.info("built %s with %d rows", args.out, count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
