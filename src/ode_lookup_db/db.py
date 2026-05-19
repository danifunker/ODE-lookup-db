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


def _nn(v: Any) -> Any:
    """Coerce empty strings to NULL; pass everything else through."""
    if isinstance(v, str) and v == "":
        return None
    return v


def _lower_hex(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def build_sqlite(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path = SQLITE_PATH,
    source_commit: str | None = None,
) -> int:
    """Rebuild SQLite from rows. Returns row count.

    Schema matches the consumer-facing spec (v1): one row per disc, side tables
    for hashes/serials/regions/languages/artwork, FTS5 over titles + PVD volume id.
    Consumers query promoted columns + side tables only; the JSONL remains the
    source of truth for any future fields that need promotion.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.executescript(
            """
            CREATE TABLE meta (
                schema_version  INTEGER NOT NULL,
                built_at        TEXT    NOT NULL,
                source_commit   TEXT,
                row_count       INTEGER NOT NULL
            );

            CREATE TABLE discs (
                redump_id          INTEGER PRIMARY KEY,
                system             TEXT    NOT NULL,
                title              TEXT    NOT NULL,
                foreign_title      TEXT,
                edition            TEXT,
                version            TEXT,
                category           TEXT,
                media              TEXT,
                barcode            TEXT,
                pvd_volume_id      TEXT,
                pvd_system_id      TEXT,
                pvd_creation_date  TEXT,
                date_added         TEXT,
                date_last_modified TEXT,
                redump_url         TEXT    NOT NULL
            );
            CREATE INDEX idx_discs_barcode       ON discs(barcode)       WHERE barcode IS NOT NULL;
            CREATE INDEX idx_discs_pvd_volume_id ON discs(pvd_volume_id) WHERE pvd_volume_id IS NOT NULL;
            CREATE INDEX idx_discs_title_nocase  ON discs(title COLLATE NOCASE);
            CREATE INDEX idx_discs_system        ON discs(system);

            CREATE TABLE tracks (
                redump_id   INTEGER NOT NULL REFERENCES discs(redump_id) ON DELETE CASCADE,
                number      INTEGER NOT NULL,
                kind        TEXT,
                size_bytes  INTEGER,
                crc32       TEXT,
                md5         TEXT,
                sha1        TEXT,
                PRIMARY KEY (redump_id, number)
            ) WITHOUT ROWID;
            CREATE INDEX idx_tracks_sha1  ON tracks(sha1)  WHERE sha1  IS NOT NULL;
            CREATE INDEX idx_tracks_md5   ON tracks(md5)   WHERE md5   IS NOT NULL;
            CREATE INDEX idx_tracks_crc32 ON tracks(crc32) WHERE crc32 IS NOT NULL;

            CREATE TABLE serials (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id) ON DELETE CASCADE,
                serial    TEXT    NOT NULL,
                PRIMARY KEY (redump_id, serial)
            ) WITHOUT ROWID;
            CREATE INDEX idx_serials_serial ON serials(serial);

            CREATE TABLE regions (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id) ON DELETE CASCADE,
                region    TEXT    NOT NULL,
                PRIMARY KEY (redump_id, region)
            ) WITHOUT ROWID;

            CREATE TABLE languages (
                redump_id INTEGER NOT NULL REFERENCES discs(redump_id) ON DELETE CASCADE,
                lang      TEXT    NOT NULL,
                PRIMARY KEY (redump_id, lang)
            ) WITHOUT ROWID;

            CREATE TABLE artwork (
                redump_id    INTEGER NOT NULL REFERENCES discs(redump_id) ON DELETE CASCADE,
                kind         TEXT    NOT NULL CHECK (kind IN ('front','back','disc','manual','other')),
                seq          INTEGER NOT NULL DEFAULT 0,
                archive_url  TEXT    NOT NULL,
                source       TEXT,
                sha256       TEXT,
                width        INTEGER,
                height       INTEGER,
                format       TEXT,
                bytes        INTEGER,
                license      TEXT,
                added        TEXT,
                PRIMARY KEY (redump_id, kind, seq)
            ) WITHOUT ROWID;
            CREATE INDEX idx_artwork_sha256 ON artwork(sha256) WHERE sha256 IS NOT NULL;

            CREATE VIRTUAL TABLE discs_fts USING fts5(
                title, foreign_title, pvd_volume_id,
                content='discs',
                content_rowid='redump_id',
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )

        conn.execute("BEGIN")
        count = 0
        for row in rows:
            pvd = row.get("pvd") or {}
            conn.execute(
                "INSERT INTO discs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["redump_id"],
                    row["system"],
                    row["title"],
                    _nn(row.get("foreign_title")),
                    _nn(row.get("edition")),
                    _nn(row.get("version")),
                    _nn(row.get("category")),
                    _nn(row.get("media")),
                    _nn(row.get("barcode")),
                    _nn(pvd.get("volume_identifier")),
                    _nn(pvd.get("system_identifier")),
                    _nn(pvd.get("creation_date")),
                    _nn(row.get("date_added")),
                    _nn(row.get("date_last_modified")),
                    row["redump_url"],
                ),
            )
            for t in row.get("tracks") or []:
                conn.execute(
                    "INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                    (
                        row["redump_id"],
                        t["number"],
                        _nn(t.get("type")),
                        t.get("size_bytes"),
                        _lower_hex(_nn(t.get("crc32"))),
                        _lower_hex(_nn(t.get("md5"))),
                        _lower_hex(_nn(t.get("sha1"))),
                    ),
                )
            for s in row.get("serials") or []:
                if _nn(s) is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO serials VALUES (?,?)", (row["redump_id"], s)
                    )
            for r in row.get("region") or []:
                if _nn(r) is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO regions VALUES (?,?)", (row["redump_id"], r)
                    )
            for code in row.get("languages") or []:
                if _nn(code) is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO languages VALUES (?,?)",
                        (row["redump_id"], code),
                    )
            count += 1

        conn.execute(
            """
            INSERT INTO discs_fts(rowid, title, foreign_title, pvd_volume_id)
            SELECT redump_id,
                   title,
                   COALESCE(foreign_title, ''),
                   COALESCE(pvd_volume_id, '')
            FROM discs
            """
        )

        conn.execute(
            "INSERT INTO meta(schema_version, built_at, source_commit, row_count) VALUES (?,?,?,?)",
            (
                SCHEMA_VERSION,
                datetime.now(UTC).isoformat(),
                source_commit or None,
                count,
            ),
        )

        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    return count
