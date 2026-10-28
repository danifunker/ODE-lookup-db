"""Recompute data/stats.json from the current JSONL."""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import read_jsonl  # noqa: E402

STATS_PATH = Path(__file__).resolve().parents[1] / "data" / "stats.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    rows = list(read_jsonl())
    by_system = Counter(r["system"] for r in rows)
    ids = sorted(r["redump_id"] for r in rows)
    stats = {
        "updated_at": datetime.now(UTC).isoformat(),
        "row_count": len(rows),
        "by_system": dict(by_system),
        "max_redump_id_by_system": {
            sys_: max((r["redump_id"] for r in rows if r["system"] == sys_), default=0)
            for sys_ in by_system
        },
        "ids": ids,
    }
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(stats, indent=2))
    logging.info("wrote %s (%d rows)", STATS_PATH, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
