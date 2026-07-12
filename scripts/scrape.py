"""Scrape redump for in-scope disc IDs and append to JSONL.

Designed to be resumable: a Ctrl-C or crash re-runs cleanly, picking up where
it left off. Two checkpoints make this work:

  - data/redump.jsonl     is flushed every --flush-every rows (default 100).
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db import SCHEMA_VERSION  # noqa: E402
from ode_lookup_db.db import read_jsonl, write_jsonl  # noqa: E402
from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.parser import ParseError, parse_disc_page  # noqa: E402
from ode_lookup_db.scraper import (  # noqa: E402
    SYSTEM_SLUGS,
    discover_disc_ids,
    discover_recent_added,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = REPO_ROOT / "data" / "discovery.json"
CHECKPOINT_PATH = REPO_ROOT / "data" / "discovery_checkpoint.json"


def load_discovery() -> dict[str, list[int]] | None:
    if not DISCOVERY_PATH.exists():
        return None
    return json.loads(DISCOVERY_PATH.read_text())


def save_discovery(data: dict[str, list[int]]) -> None:
    DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_PATH.write_text(json.dumps(data, indent=2))


def load_checkpoint() -> int | None:
    if not CHECKPOINT_PATH.exists():
        return None
    data = json.loads(CHECKPOINT_PATH.read_text())
    return data.get("topmost_redump_id")


def save_checkpoint(topmost_redump_id: int) -> None:
    from datetime import datetime, timezone
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps({
        "topmost_redump_id": topmost_redump_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n")


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
    ap.add_argument("--flush-every", type=int, default=100, help="JSONL flush cadence (rows)")
    ap.add_argument(
        "--flush-idle-seconds", type=int, default=30,
        help="Also flush if this many seconds have elapsed since the last write "
             "(useful when redump is slow/flaky and rows accumulate slowly).",
    )
    ap.add_argument(
        "--workers", type=int, default=4,
        help="Concurrent in-flight requests. Start spacing is still rate-limited "
             "to 1/sec; workers just stop us blocking on slow responses.",
    )
    ap.add_argument(
        "--backfill-all", action="store_true",
        help="One-time redump.info migration: re-fetch EVERY known redump_id in the "
             "JSONL and overwrite it with the new (schema v3) parse. Skips discovery. "
             "Heavy: ~1 req/s over the whole DB. Resumable — rerun to continue.",
    )
    ap.add_argument("--refresh-discovery", action="store_true", help="Rebuild discovery cache")
    ap.add_argument(
        "--full-discovery", action="store_true",
        help="Use legacy per-system listing walk instead of the added-desc checkpoint. "
             "Slow (~120 pages); use only for seed runs or to rebuild from scratch.",
    )
    ap.add_argument(
        "--max-added-desc-pages", type=int, default=50,
        help="Cap on /discs?sort=added&order=desc pages walked when using checkpoint discovery.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Quiet httpx — its INFO logs are very chatty for long runs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("scrape")

    rows_by_id = {r["redump_id"]: r for r in read_jsonl()}
    log.info("loaded %d existing rows", len(rows_by_id))

    with RedumpClient() as client:
        # --- Discovery ----------------------------------------------------------
        targets: list[tuple[int, str]] = []
        new_checkpoint: int | None = None
        if args.backfill_all:
            # One-time migration: re-fetch every known id and overwrite it with a
            # fresh v3 parse. Resumable — rows already at the current schema
            # version (i.e. flushed on a previous run) are skipped, so a rerun
            # picks up where an interrupted run left off.
            targets = [
                (rid, row["system"])
                for rid, row in sorted(rows_by_id.items())
                if row.get("schema_version") != SCHEMA_VERSION
            ]
            log.info(
                "backfill-all: %d of %d rows still below schema v%d — re-fetching",
                len(targets), len(rows_by_id), SCHEMA_VERSION,
            )
        elif args.full_discovery:
            # Legacy path: per-system listing walk. Slow but exhaustive.
            cached = None if args.refresh_discovery else load_discovery()
            discovery: dict[str, list[int]] = {} if cached is None else dict(cached)
            known_ids = set(rows_by_id.keys())
            target = (args.limit + 50) if args.limit is not None else None
            missing = [s for s in args.systems if s not in discovery]
            if cached is not None and not missing:
                log.info("using cached discovery (%s); pass --refresh-discovery to rebuild",
                         {s: len(v) for s, v in discovery.items()})
            else:
                if cached is not None and missing:
                    log.info("cache has %s; walking listing pages for missing: %s",
                             list(cached.keys()), missing)
                for system in args.systems:
                    if system in discovery and cached is not None:
                        continue
                    ids = [
                        rid for rid, _ in discover_disc_ids(
                            client, system,
                            max_pages=args.max_pages,
                            known_ids=known_ids,
                            target_unknown=target,
                        )
                    ]
                    discovery[system] = ids
                save_discovery(discovery)
                log.info("discovery cached at %s", DISCOVERY_PATH)
            for system in args.systems:
                for rid in discovery.get(system, []):
                    if rid not in rows_by_id:
                        targets.append((rid, system))
        else:
            # Default path: walk added-desc newest-first until we hit checkpoint.
            checkpoint_id = load_checkpoint()
            log.info("discovery via added-desc (checkpoint=%s)", checkpoint_id)
            recent, new_checkpoint = discover_recent_added(
                client,
                systems=set(args.systems),
                checkpoint_id=checkpoint_id,
                max_pages=args.max_added_desc_pages,
            )
            # Filter to IDs not already in JSONL (defensive: handles overlap or
            # the bootstrap case where checkpoint is missing and we walk back
            # over already-known discs).
            for rid, system in recent:
                if rid not in rows_by_id:
                    targets.append((rid, system))

        if args.limit is not None:
            targets = targets[: args.limit]
        total = len(targets)
        log.info("fetching %d new disc pages (workers=%d, flush every %d rows)",
                 total, args.workers, args.flush_every)

        # --- Fetch + parse + checkpoint ----------------------------------------
        # Workers run client.get_disc() concurrently. The HTTP client's internal
        # lock + 1s throttle guarantees request *starts* are still spaced 1/sec,
        # so redump's load is unchanged — we just stop blocking on slow responses.
        started = time.monotonic()
        last_flush = started
        fetched = parsed = failed = dropped = 0
        dirty = 0

        def fetch_one(rid: int, system: str):
            # Returns (rid, system, resp_or_None, exc_or_None). Any exception
            # raised by the HTTP client (e.g. retries exhausted during a
            # sustained outage) is captured here so it never propagates into
            # the executor and kills the worker pool.
            try:
                resp = client.get_disc(rid)
                return rid, system, resp, None
            except Exception as e:  # noqa: BLE001 — intentional broad catch
                return rid, system, None, e

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(fetch_one, rid, sys_) for rid, sys_ in targets]
            for fut in as_completed(futures):
                rid, system, resp, exc = fut.result()
                fetched += 1
                ok = False
                if exc is not None:
                    failed += 1
                    log.warning("disc %d fetch failed after retries: %s — skipping. "
                                "Re-run scrape to retry this ID later.", rid, exc)
                elif resp.status_code == 404:
                    if args.backfill_all and rid in rows_by_id:
                        # Backfill: the id is gone from redump.info (renumbered or
                        # deleted upstream). We can't re-parse it to the current
                        # schema, so drop the stale row rather than leave a
                        # version-mismatched tombstone. The validator's shrink
                        # warning (decision #5) surfaces the net removals.
                        del rows_by_id[rid]
                        dropped += 1
                        dirty += 1
                        log.warning("disc %d returned 404 — dropping stale row (backfill)", rid)
                    else:
                        log.warning("disc %d returned 404 — skipping", rid)
                elif resp.status_code != 200:
                    log.warning("disc %d returned HTTP %d — skipping", rid, resp.status_code)
                else:
                    try:
                        row = parse_disc_page(resp.text, redump_id=rid, system=system)
                        rows_by_id[rid] = row
                        parsed += 1
                        dirty += 1
                        ok = True
                    except ParseError as e:
                        failed += 1
                        log.warning("disc %d parse failed: %s", rid, e)

                if fetched <= 5 and ok:
                    log.info("fetched disc %d (system=%s) — %d/%d", rid, system, fetched, total)

                # Flush on either: row count threshold, OR idle-time threshold
                # (so flaky periods with slow throughput still save progress).
                now = time.monotonic()
                if dirty > 0 and (
                    dirty >= args.flush_every
                    or now - last_flush >= args.flush_idle_seconds
                ):
                    write_jsonl(rows_by_id.values())
                    log.info("checkpoint: wrote %d total rows (dirty=%d, age=%.0fs)",
                             len(rows_by_id), dirty, now - last_flush)
                    dirty = 0
                    last_flush = now

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

    # Persist the added-desc checkpoint only after a clean run, so a partial
    # failure doesn't move the marker past unprocessed discs.
    if new_checkpoint is not None and failed == 0:
        save_checkpoint(new_checkpoint)
        log.info("checkpoint updated: topmost_redump_id=%d", new_checkpoint)
    elif new_checkpoint is not None:
        log.warning("not updating checkpoint (failed=%d): will retry next run", failed)

    log.info(
        "done: fetched=%d parsed=%d failed=%d dropped=%d, total rows=%d",
        fetched, parsed, failed, dropped, len(rows_by_id),
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
