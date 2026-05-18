"""JSONL <-> SQLite I/O for the disc database.

`data/redump.jsonl` is the source of truth (committed, plain text for diffs).
`data/redump.sqlite` is the released artifact, rebuilt from JSONL every run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Iterable, Iterator

import orjson

from . import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
JSONL_PATH = REPO_ROOT / "data" / "redump.jsonl"
SQLITE_PATH = REPO_ROOT / "data" / "redump.sqlite"


def read_jsonl(path: Path = JSONL_PATH) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if line:
                yield orjson.loads(line)


def write_jsonl(rows: Iterable[dict[str, Any]], path: Path = JSONL_PATH) -> int:
    """Write rows sorted by redump_id for deterministic diffs. Returns row count."""
    sorted_rows = sorted(rows, key=lambda r: r["redump_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("wb") as f:
        for row in sorted_rows:
            f.write(orjson.dumps(row, option=orjson.OPT_SORT_KEYS))
            f.write(b"\n")
            count += 1
    return count


def build_sqlite(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path = SQLITE_PATH,
    source_commit: str | None = None,
) -> int:
    """Rebuild SQLite from rows. Returns row count.

    Schema is denormalized for fast lookup: one row per disc, plus a tracks table
    with hash indexes. Designed for read-only consumers querying by hash, serial,
    or PVD volume label.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE discs (
                redump_id          INTEGER PRIMARY KEY,
                system             TEXT NOT NULL,
                title              TEXT NOT NULL,
                foreign_title      TEXT,
                edition            TEXT,
                version            TEXT,
                barcode            TEXT,
                category           TEXT,
                cuesheet_sha1      TEXT,
                redump_url         TEXT NOT NULL,
                date_added         TEXT,
                date_last_modified TEXT,
                pvd_volume_id      TEXT,
                pvd_system_id      TEXT,
                json               TEXT NOT NULL
            );

            CREATE TABLE tracks (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id),
                number    INTEGER NOT NULL,
                type      TEXT,
                size_bytes INTEGER,
                crc32     TEXT,
                md5       TEXT,
                sha1      TEXT,
                PRIMARY KEY (redump_id, number)
            );

            CREATE TABLE serials (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id),
                serial    TEXT NOT NULL
            );

            CREATE TABLE regions (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id),
                region    TEXT NOT NULL
            );

            CREATE TABLE languages (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id),
                code      TEXT NOT NULL
            );

            CREATE INDEX idx_tracks_crc32 ON tracks(crc32);
            CREATE INDEX idx_tracks_md5   ON tracks(md5);
            CREATE INDEX idx_tracks_sha1  ON tracks(sha1);
            CREATE INDEX idx_serials      ON serials(serial);
            CREATE INDEX idx_disc_pvd     ON discs(pvd_volume_id);
            CREATE INDEX idx_disc_system_title ON discs(system, title);
            """
        )

        count = 0
        for row in rows:
            pvd = row.get("pvd") or {}
            conn.execute(
                "INSERT INTO discs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["redump_id"],
                    row["system"],
                    row["title"],
                    row.get("foreign_title"),
                    row.get("edition"),
                    row.get("version"),
                    row.get("barcode"),
                    row.get("category"),
                    row.get("cuesheet_sha1"),
                    row["redump_url"],
                    row.get("date_added"),
                    row.get("date_last_modified"),
                    pvd.get("volume_identifier"),
                    pvd.get("system_identifier"),
                    orjson.dumps(row).decode("utf-8"),
                ),
            )
            for t in row.get("tracks", []):
                conn.execute(
                    "INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                    (
                        row["redump_id"],
                        t["number"],
                        t.get("type"),
                        t.get("size_bytes"),
                        t.get("crc32"),
                        t.get("md5"),
                        t.get("sha1"),
                    ),
                )
            for s in row.get("serials", []):
                conn.execute("INSERT INTO serials VALUES (?,?)", (row["redump_id"], s))
            for r in row.get("region", []):
                conn.execute("INSERT INTO regions VALUES (?,?)", (row["redump_id"], r))
            for code in row.get("languages", []):
                conn.execute("INSERT INTO languages VALUES (?,?)", (row["redump_id"], code))
            count += 1

        conn.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("built_at", datetime.now(UTC).isoformat()),
                ("source_commit", source_commit or ""),
                ("row_count", str(count)),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return count
