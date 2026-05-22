"""JSONL <-> SQLite I/O for the unified ODE lookup database.

Source-of-truth files (per source, committed, plain text for diffs):
  data/redump.jsonl                 — redump.org disc metadata
  winworld/data/winworld.jsonl      — winworldpc.com archive metadata

Released artifact (rebuilt from JSONLs every run, not committed):
  data/ode-lookup.sqlite

Tables are namespaced per source (`redump_*`, `winworld_*`) so consumers can
attach one database and query any source. The `meta` table has one row per
source with its independent schema_version.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Iterable, Iterator

import orjson

from . import SCHEMA_VERSION

import os

REPO_ROOT = Path(__file__).resolve().parents[2]
JSONL_PATH = REPO_ROOT / "data" / "redump.jsonl"
# Winworld JSONL: prefer WINWORLD_DATA_DIR (e.g. NAS mount), else the in-repo path.
WINWORLD_DATA_DIR = Path(os.environ["WINWORLD_DATA_DIR"]).resolve() if os.environ.get(
    "WINWORLD_DATA_DIR"
) else REPO_ROOT / "winworld" / "data"
WINWORLD_JSONL_PATH = WINWORLD_DATA_DIR / "winworld.jsonl"
SQLITE_PATH = REPO_ROOT / "data" / "ode-lookup.sqlite"

WINWORLD_SCHEMA_VERSION = 1

# This DB exists to power Optical Disc Emulator lookups. WinWorld carries floppies,
# tapes, VM images, manuals, etc. — they're filtered out at build time. The source
# JSONL keeps everything so we can revisit this choice without re-scraping.
WINWORLD_OPTICAL_MEDIA = frozenset({"CD", "DVD"})


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


_COMMON_SCHEMA = """
CREATE TABLE meta (
    source          TEXT    NOT NULL PRIMARY KEY,
    schema_version  INTEGER NOT NULL,
    built_at        TEXT    NOT NULL,
    source_commit   TEXT,
    row_count       INTEGER NOT NULL
);
"""

_REDUMP_SCHEMA = """
CREATE TABLE redump_disc (
    redump_id          INTEGER PRIMARY KEY,
    system             TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    foreign_title      TEXT,
    edition            TEXT,
    version            TEXT,
    category           TEXT,
    media              TEXT,
    barcode            TEXT,
    catalog            TEXT,
    pvd_volume_id      TEXT,
    pvd_system_id      TEXT,
    pvd_creation_date  TEXT,
    date_added         TEXT,
    date_last_modified TEXT,
    redump_url         TEXT    NOT NULL
);
CREATE INDEX idx_redump_disc_barcode       ON redump_disc(barcode)       WHERE barcode IS NOT NULL;
CREATE INDEX idx_redump_disc_catalog       ON redump_disc(catalog)       WHERE catalog IS NOT NULL;
CREATE INDEX idx_redump_disc_pvd_volume_id ON redump_disc(pvd_volume_id) WHERE pvd_volume_id IS NOT NULL;
CREATE INDEX idx_redump_disc_title_nocase  ON redump_disc(title COLLATE NOCASE);
CREATE INDEX idx_redump_disc_system        ON redump_disc(system);

CREATE TABLE redump_track (
    redump_id   INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    kind        TEXT,
    sectors     INTEGER,
    size_bytes  INTEGER,
    crc32       TEXT,
    md5         TEXT,
    sha1        TEXT,
    PRIMARY KEY (redump_id, number)
) WITHOUT ROWID;
CREATE INDEX idx_redump_track_sha1  ON redump_track(sha1)  WHERE sha1  IS NOT NULL;
CREATE INDEX idx_redump_track_md5   ON redump_track(md5)   WHERE md5   IS NOT NULL;
CREATE INDEX idx_redump_track_crc32 ON redump_track(crc32) WHERE crc32 IS NOT NULL;

CREATE TABLE redump_serial (
    redump_id INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    serial    TEXT    NOT NULL,
    PRIMARY KEY (redump_id, serial)
) WITHOUT ROWID;
CREATE INDEX idx_redump_serial_serial ON redump_serial(serial);

CREATE TABLE redump_region (
    redump_id INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    region    TEXT    NOT NULL,
    PRIMARY KEY (redump_id, region)
) WITHOUT ROWID;

CREATE TABLE redump_language (
    redump_id INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    lang      TEXT    NOT NULL,
    PRIMARY KEY (redump_id, lang)
) WITHOUT ROWID;

CREATE TABLE redump_artwork (
    redump_id    INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
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
CREATE INDEX idx_redump_artwork_sha256 ON redump_artwork(sha256) WHERE sha256 IS NOT NULL;

CREATE VIRTUAL TABLE redump_disc_fts USING fts5(
    title, foreign_title, pvd_volume_id,
    content='redump_disc',
    content_rowid='redump_id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_WINWORLD_SCHEMA = """
CREATE TABLE winworld_product (
    product_slug  TEXT PRIMARY KEY,
    category      TEXT,
    title         TEXT,
    url           TEXT
);
CREATE INDEX idx_winworld_product_title_nocase ON winworld_product(title COLLATE NOCASE);
CREATE INDEX idx_winworld_product_category     ON winworld_product(category);

CREATE TABLE winworld_release (
    product_slug              TEXT NOT NULL,
    release_slug              TEXT NOT NULL,
    title                     TEXT,
    subtitle                  TEXT,
    url                       TEXT NOT NULL,
    description_text          TEXT,
    release_notes_text        TEXT,
    installation_text         TEXT,
    info_json                 TEXT,    -- {Product type, Vendor, Release date, ...}
    comments_provider         TEXT,
    comments_identifier       TEXT,
    comments_forum_url        TEXT,
    comments_category_id      TEXT,
    html_sha256               TEXT,
    fetched_at                TEXT,
    PRIMARY KEY (product_slug, release_slug),
    FOREIGN KEY (product_slug) REFERENCES winworld_product(product_slug) ON DELETE CASCADE
);

CREATE TABLE winworld_download (
    download_id        TEXT PRIMARY KEY,
    product_slug       TEXT NOT NULL,
    release_slug       TEXT NOT NULL,
    filename           TEXT NOT NULL,
    display_name       TEXT,
    media_kind         TEXT,
    language           TEXT,
    architecture       TEXT,
    version            TEXT,
    file_size_text     TEXT,
    archive_hash       TEXT,
    archive_hash_alg   TEXT,
    download_count     INTEGER,
    tags_json          TEXT,
    download_url       TEXT,
    FOREIGN KEY (product_slug, release_slug)
        REFERENCES winworld_release(product_slug, release_slug) ON DELETE CASCADE
);
CREATE INDEX idx_winworld_download_archive_hash ON winworld_download(archive_hash) WHERE archive_hash IS NOT NULL;
CREATE INDEX idx_winworld_download_filename     ON winworld_download(filename COLLATE NOCASE);
CREATE INDEX idx_winworld_download_media_kind   ON winworld_download(media_kind);
CREATE INDEX idx_winworld_download_language     ON winworld_download(language);
CREATE INDEX idx_winworld_download_release      ON winworld_download(product_slug, release_slug);

CREATE TABLE winworld_serial (
    product_slug TEXT NOT NULL,
    release_slug TEXT NOT NULL,
    serial       TEXT NOT NULL,
    PRIMARY KEY (product_slug, release_slug, serial),
    FOREIGN KEY (product_slug, release_slug)
        REFERENCES winworld_release(product_slug, release_slug) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE winworld_screenshot (
    product_slug    TEXT NOT NULL,
    release_slug    TEXT NOT NULL,
    src             TEXT NOT NULL,
    alt             TEXT,
    title           TEXT,
    screenshot_page TEXT,
    PRIMARY KEY (product_slug, release_slug, src),
    FOREIGN KEY (product_slug, release_slug)
        REFERENCES winworld_release(product_slug, release_slug) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE VIRTUAL TABLE winworld_release_fts USING fts5(
    title, subtitle, description_text,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def _category_from_release(rec: dict[str, Any]) -> str | None:
    """Pull category slug from info_links['Product type'][0].href = '/library/<cat>'."""
    info_links = rec.get("info_links") or {}
    pt = info_links.get("Product type") or []
    if pt:
        href = pt[0].get("href", "")
        if "/library/" in href:
            return href.rsplit("/library/", 1)[-1].rstrip("/")
    return None


def _insert_redump(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        pvd = row.get("pvd") or {}
        conn.execute(
            "INSERT INTO redump_disc VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                _nn(row.get("catalog")),
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
                "INSERT INTO redump_track VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["redump_id"],
                    t["number"],
                    _nn(t.get("type")),
                    t.get("sectors"),
                    t.get("size_bytes"),
                    _lower_hex(_nn(t.get("crc32"))),
                    _lower_hex(_nn(t.get("md5"))),
                    _lower_hex(_nn(t.get("sha1"))),
                ),
            )
        for s in row.get("serials") or []:
            if _nn(s) is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO redump_serial VALUES (?,?)", (row["redump_id"], s)
                )
        for r in row.get("region") or []:
            if _nn(r) is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO redump_region VALUES (?,?)", (row["redump_id"], r)
                )
        for code in row.get("languages") or []:
            if _nn(code) is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO redump_language VALUES (?,?)",
                    (row["redump_id"], code),
                )
        count += 1

    conn.execute(
        """
        INSERT INTO redump_disc_fts(rowid, title, foreign_title, pvd_volume_id)
        SELECT redump_id,
               title,
               COALESCE(foreign_title, ''),
               COALESCE(pvd_volume_id, '')
        FROM redump_disc
        """
    )
    return count


def _insert_winworld(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> int:
    """Returns the number of releases ingested.

    This is an ODE (Optical Disc Emulator) lookup DB, so only CD/DVD downloads
    are ingested. Releases with no optical downloads are skipped entirely, and
    products that thereby get no releases are skipped too.
    """
    seen_products: set[str] = set()
    count = 0
    for rec in records:
        product_slug = rec.get("product_slug")
        release_slug = rec.get("release_slug")
        if not product_slug or not release_slug:
            continue

        optical_downloads = [
            d for d in (rec.get("downloads") or [])
            if d.get("download_id") and (d.get("media_kind") in WINWORLD_OPTICAL_MEDIA)
        ]
        if not optical_downloads:
            continue  # nothing useful for this DB

        if product_slug not in seen_products:
            seen_products.add(product_slug)
            conn.execute(
                "INSERT OR IGNORE INTO winworld_product VALUES (?,?,?,?)",
                (
                    product_slug,
                    _category_from_release(rec),
                    _nn(rec.get("title")),
                    f"https://winworldpc.com/product/{product_slug}",
                ),
            )

        comments = rec.get("comments") or {}
        info = rec.get("info") or {}
        conn.execute(
            "INSERT OR IGNORE INTO winworld_release VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                product_slug,
                release_slug,
                _nn(rec.get("title")),
                _nn(rec.get("subtitle")),
                rec.get("url", ""),
                _nn(rec.get("description_text")),
                _nn(rec.get("release_notes_text")),
                _nn(rec.get("installation_text")),
                orjson.dumps(info).decode() if info else None,
                _nn(comments.get("provider")),
                _nn(comments.get("identifier")),
                _nn(comments.get("forum_url")),
                _nn(comments.get("category_id")),
                _nn(rec.get("html_sha256")),
                _nn(rec.get("fetched_at")),
            ),
        )

        for d in optical_downloads:
            did = d.get("download_id")
            tags = d.get("tags") or []
            conn.execute(
                """
                INSERT OR REPLACE INTO winworld_download VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    did,
                    product_slug,
                    release_slug,
                    d.get("filename") or "",
                    _nn(d.get("display_name")),
                    _nn(d.get("media_kind")),
                    _nn(d.get("language")),
                    _nn(d.get("architecture")),
                    _nn(d.get("version")),
                    _nn(d.get("file_size_text")),
                    _lower_hex(_nn(d.get("archive_hash"))),
                    _nn(d.get("archive_hash_alg")),
                    d.get("download_count"),
                    orjson.dumps(tags).decode() if tags else None,
                    _nn(d.get("download_url")),
                ),
            )

        for s in rec.get("serials") or []:
            if _nn(s):
                conn.execute(
                    "INSERT OR IGNORE INTO winworld_serial VALUES (?,?,?)",
                    (product_slug, release_slug, s),
                )

        for sh in rec.get("screenshots") or []:
            src = sh.get("src")
            if not src:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO winworld_screenshot VALUES (?,?,?,?,?,?)",
                (
                    product_slug,
                    release_slug,
                    src,
                    _nn(sh.get("alt")),
                    _nn(sh.get("title")),
                    _nn(sh.get("screenshot_page")),
                ),
            )

        count += 1

    conn.execute(
        """
        INSERT INTO winworld_release_fts(rowid, title, subtitle, description_text)
        SELECT rowid,
               COALESCE(title, ''),
               COALESCE(subtitle, ''),
               COALESCE(description_text, '')
        FROM winworld_release
        """
    )
    return count


def _write_meta_row(
    conn: sqlite3.Connection,
    *,
    source: str,
    schema_version: int,
    source_commit: str | None,
    row_count: int,
) -> None:
    conn.execute(
        "INSERT INTO meta(source, schema_version, built_at, source_commit, row_count) VALUES (?,?,?,?,?)",
        (source, schema_version, datetime.now(UTC).isoformat(), source_commit or None, row_count),
    )


def build_sqlite(
    rows: Iterable[dict[str, Any]] | None = None,
    *,
    path: Path = SQLITE_PATH,
    source_commit: str | None = None,
    winworld_records: Iterable[dict[str, Any]] | None = None,
) -> int:
    """Rebuild the unified SQLite from per-source records.

    Args:
        rows: redump rows from data/redump.jsonl (optional — pass None to skip)
        winworld_records: winworld release records (optional)
        path: output sqlite file (default data/ode-lookup.sqlite)
        source_commit: GITHUB_SHA-style identifier for the build provenance

    Returns: total row count (redump discs + winworld releases) for logging.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.executescript(_COMMON_SCHEMA + _REDUMP_SCHEMA + _WINWORLD_SCHEMA)

        conn.execute("BEGIN")

        redump_count = 0
        if rows is not None:
            redump_count = _insert_redump(conn, rows)
            _write_meta_row(
                conn,
                source="redump",
                schema_version=SCHEMA_VERSION,
                source_commit=source_commit,
                row_count=redump_count,
            )

        winworld_count = 0
        if winworld_records is not None:
            winworld_count = _insert_winworld(conn, winworld_records)
            _write_meta_row(
                conn,
                source="winworld",
                schema_version=WINWORLD_SCHEMA_VERSION,
                source_commit=source_commit,
                row_count=winworld_count,
            )

        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    return redump_count + winworld_count


def read_winworld_jsonl(path: Path = WINWORLD_JSONL_PATH) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if line:
                yield orjson.loads(line)
