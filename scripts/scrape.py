"""Daily scrape: discover new disc IDs across in-scope systems and append to JSONL.

Usage:
    uv run scripts/scrape.py                  # full daily run
    uv run scripts/scrape.py --limit 50       # cap discoveries (seed/dev)
    uv run scripts/scrape.py --systems pc     # restrict systems
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import JSONL_PATH, read_jsonl, write_jsonl  # noqa: E402
from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.scraper import SYSTEM_SLUGS, discover_disc_ids, fetch_and_parse  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max new IDs to fetch this run")
    ap.add_argument(
        "--systems",
        nargs="+",
        default=list(SYSTEM_SLUGS),
        choices=list(SYSTEM_SLUGS),
    )
    ap.add_argument("--max-pages", type=int, default=50)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("scrape")

    existing_rows = list(read_jsonl())
    existing_ids = {r["redump_id"] for r in existing_rows}
    log.info("loaded %d existing rows", len(existing_rows))

    with RedumpClient() as client:
        targets: list[tuple[int, str]] = []
        for system in args.systems:
            for rid, sys_ in discover_disc_ids(client, system, max_pages=args.max_pages):
                if rid not in existing_ids:
                    targets.append((rid, sys_))

        if args.limit is not None:
            targets = targets[: args.limit]

        log.info("fetching %d new disc pages", len(targets))
        new_rows = list(fetch_and_parse(client, targets))

    log.info("parsed %d new rows", len(new_rows))

    all_rows_by_id = {r["redump_id"]: r for r in existing_rows}
    for r in new_rows:
        all_rows_by_id[r["redump_id"]] = r

    count = write_jsonl(all_rows_by_id.values())
    log.info("wrote %d rows to %s", count, JSONL_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
