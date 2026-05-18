"""Scrape redump for in-scope disc IDs and append to JSONL.

Designed to be resumable: a Ctrl-C or crash re-runs cleanly, picking up where
it left off. Two checkpoints make this work:

  - data/redump.jsonl.gz  is flushed every --flush-every rows (default 100).
  - data/discovery.json   caches discovered (id, system) pairs so we don't
                          re-walk listing pages on every restart. Pass
                          --refresh-discovery to force a re-walk.

Examples:
    uv run scripts/scrape.py                          # full daily run
    uv run scripts/scrape.py --limit 200              # cap fetches (trial)
    uv run scripts/scrape.py --systems pc             # restrict systems
    uv run scripts/scrape.py --refresh-discovery      # rebuild discovery cache
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import JSONL_PATH, read_jsonl, write_jsonl  # noqa: E402
from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.parser import ParseError, parse_disc_page  # noqa: E402
from ode_lookup_db.scraper import SYSTEM_SLUGS, discover_disc_ids  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = REPO_ROOT / "data" / "discovery.json"


def load_discovery() -> dict[str, list[int]] | None:
    if not DISCOVERY_PATH.exists():
        return None
    return json.loads(DISCOVERY_PATH.read_text())


def save_discovery(data: dict[str, list[int]]) -> None:
    DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_PATH.write_text(json.dumps(data, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max new discs to fetch this run")
    ap.add_argument(
        "--systems",
        nargs="+",
        default=list(SYSTEM_SLUGS),
        choices=list(SYSTEM_SLUGS),
    )
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--flush-every", type=int, default=100, help="JSONL flush cadence")
    ap.add_argument("--refresh-discovery", action="store_true", help="Rebuild discovery cache")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Quiet httpx — its INFO logs are very chatty for long runs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("scrape")

    rows_by_id = {r["redump_id"]: r for r in read_jsonl()}
    log.info("loaded %d existing rows", len(rows_by_id))

    with RedumpClient() as client:
        # --- Discovery (cached) -------------------------------------------------
        discovery = None if args.refresh_discovery else load_discovery()
        if discovery is None:
            discovery = {}
            for system in args.systems:
                ids = [rid for rid, _ in discover_disc_ids(client, system, max_pages=args.max_pages)]
                discovery[system] = ids
            save_discovery(discovery)
            log.info("discovery cached at %s", DISCOVERY_PATH)
        else:
            log.info("using cached discovery (%s); pass --refresh-discovery to rebuild",
                     {s: len(v) for s, v in discovery.items()})

        # --- Build target list (new IDs only) ----------------------------------
        targets: list[tuple[int, str]] = []
        for system in args.systems:
            for rid in discovery.get(system, []):
                if rid not in rows_by_id:
                    targets.append((rid, system))
        if args.limit is not None:
            targets = targets[: args.limit]
        total = len(targets)
        log.info("fetching %d new disc pages (flush every %d rows)", total, args.flush_every)

        # --- Fetch + parse + checkpoint ----------------------------------------
        started = time.monotonic()
        fetched = parsed = failed = 0
        dirty = 0
        for rid, system in targets:
            resp = client.get_disc(rid)
            fetched += 1
            if resp.status_code == 404:
                log.warning("disc %d returned 404 — skipping", rid)
            elif resp.status_code != 200:
                log.warning("disc %d returned HTTP %d — skipping", rid, resp.status_code)
            else:
                try:
                    row = parse_disc_page(resp.text, redump_id=rid, system=system)
                    rows_by_id[rid] = row
                    parsed += 1
                    dirty += 1
                except ParseError as e:
                    failed += 1
                    log.warning("disc %d parse failed: %s", rid, e)

            if dirty >= args.flush_every:
                write_jsonl(rows_by_id.values())
                log.info("checkpoint: wrote %d total rows", len(rows_by_id))
                dirty = 0

            if fetched % 50 == 0:
                elapsed = time.monotonic() - started
                rate = fetched / elapsed
                remaining = total - fetched
                eta_s = remaining / rate if rate else 0
                log.info(
                    "progress: %d/%d (parsed=%d, failed=%d) — %.2f/s, ETA %s",
                    fetched, total, parsed, failed, rate, _fmt_eta(eta_s),
                )

        if dirty:
            write_jsonl(rows_by_id.values())

    log.info(
        "done: fetched=%d parsed=%d failed=%d, total rows=%d",
        fetched, parsed, failed, len(rows_by_id),
    )
    return 0


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


if __name__ == "__main__":
    raise SystemExit(main())
