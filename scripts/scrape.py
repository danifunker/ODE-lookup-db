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
            known_ids = set(rows_by_id.keys())
            # When --limit is set, stop discovery early once we have enough
            # unknown IDs. Otherwise (bulk run), walk until the listing ends.
            # Add a small buffer so failed parses don't shortchange the budget.
            target = (args.limit + 50) if args.limit is not None else None
            for system in args.systems:
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
        log.info("fetching %d new disc pages (workers=%d, flush every %d rows)",
                 total, args.workers, args.flush_every)

        # --- Fetch + parse + checkpoint ----------------------------------------
        # Workers run client.get_disc() concurrently. The HTTP client's internal
        # lock + 1s throttle guarantees request *starts* are still spaced 1/sec,
        # so redump's load is unchanged — we just stop blocking on slow responses.
        started = time.monotonic()
        last_flush = started
        fetched = parsed = failed = 0
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
