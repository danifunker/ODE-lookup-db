"""Post-build checks for data/redump.sqlite.

Verifies meta integrity, FTS row parity, and a canary lookup (sha1 + FTS title)
against a pinned disc. Exit 0 = ok, 1 = failure.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import SQLITE_PATH, read_jsonl  # noqa: E402

# Pinned canary disc for SQLite-level checks. 007 Legends has a sufficiently
# distinctive first token ("007") that BM25 reliably ranks it inside the top 5
# matches, which generic franchise titles like "Myst" or "MechWarrior" don't.
CANARY_REDUMP_ID = 92225


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    log = logging.getLogger("validate_sqlite")

    if not SQLITE_PATH.exists():
        log.error("SQLite artifact missing: %s", SQLITE_PATH)
        return 1

    failures: list[str] = []
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        meta = conn.execute(
            "SELECT schema_version, built_at, source_commit, row_count FROM meta"
        ).fetchone()
        if meta is None:
            failures.append("meta row missing")
        else:
            sv, built_at, _src, row_count = meta
            if sv is None or built_at is None or row_count is None:
                failures.append(f"meta row has NULLs in required columns: {meta!r}")
            actual_rows = conn.execute("SELECT COUNT(*) FROM discs").fetchone()[0]
            if row_count != actual_rows:
                failures.append(
                    f"meta.row_count={row_count} but discs has {actual_rows} rows"
                )
            fts_rows = conn.execute("SELECT COUNT(*) FROM discs_fts").fetchone()[0]
            if fts_rows != actual_rows:
                failures.append(
                    f"discs_fts has {fts_rows} rows but discs has {actual_rows}"
                )

        # Canary spot-checks: pull the disc's data from the JSONL source of truth
        # and verify it round-trips through the built SQLite.
        canary = next(
            (r for r in read_jsonl() if r.get("redump_id") == CANARY_REDUMP_ID),
            None,
        )
        if canary is None:
            log.warning(
                "canary disc %d not in JSONL — skipping sha1/FTS spot-checks",
                CANARY_REDUMP_ID,
            )
        else:
            tracks = canary.get("tracks") or []
            canary_sha1 = next(
                (t.get("sha1") for t in tracks if t.get("sha1")), None
            )
            if canary_sha1:
                hit = conn.execute(
                    "SELECT redump_id FROM tracks WHERE sha1 = ?",
                    (canary_sha1.lower(),),
                ).fetchone()
                if hit is None or hit[0] != CANARY_REDUMP_ID:
                    failures.append(
                        f"sha1 lookup for canary {CANARY_REDUMP_ID} returned {hit!r}"
                    )
            else:
                log.warning("canary disc %d has no sha1 — skipping", CANARY_REDUMP_ID)

            first_word = (canary.get("title") or "").split()[:1]
            if first_word:
                rows = conn.execute(
                    """
                    SELECT discs_fts.rowid
                    FROM discs_fts
                    WHERE discs_fts MATCH ?
                    ORDER BY rank
                    LIMIT 5
                    """,
                    (first_word[0],),
                ).fetchall()
                ids = [r[0] for r in rows]
                if CANARY_REDUMP_ID not in ids:
                    failures.append(
                        f"FTS title search for {first_word[0]!r} did not return "
                        f"canary {CANARY_REDUMP_ID} in top 5: {ids}"
                    )
    finally:
        conn.close()

    if failures:
        for f in failures:
            log.error(f)
        log.error("FAILED: %d checks", len(failures))
        return 1
    log.info("OK: sqlite validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
