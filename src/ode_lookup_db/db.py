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

import logging
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

WINWORLD_SCHEMA_VERSION = 2

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

-- Per-disc inspection rows, sourced from `.derived.json` sidecars on the
-- archives volume. Populated only for downloads that have been fetched +
-- extracted locally. image_sha256 is NULLable: future iterations may record
-- partial rows where extraction succeeded but hashing didn't.
CREATE TABLE winworld_disc_image (
    product_slug             TEXT NOT NULL,
    release_slug             TEXT NOT NULL,
    archive_filename         TEXT NOT NULL,    -- the .7z basename (matches winworld_download.filename)
    iso_filename             TEXT,             -- basename of the .iso inside the archive
    image_path               TEXT NOT NULL,    -- archive-relative .iso path; PK component
    image_sha256             TEXT,             -- nullable; high-confidence content match when present
    size_bytes               INTEGER,
    filetree_sha256          TEXT,
    file_count               INTEGER,
    dir_count                INTEGER,
    volume_identifier        TEXT,
    system_identifier        TEXT,
    publisher_identifier     TEXT,
    preparer_identifier      TEXT,
    application_identifier   TEXT,
    volume_creation_date     TEXT,
    volume_modification_date TEXT,
    logical_block_size       INTEGER,
    has_joliet               INTEGER,
    has_rock_ridge           INTEGER,
    has_udf                  INTEGER,
    has_eltorito             INTEGER,
    el_torito_platform       TEXT,
    el_torito_image_sha256   TEXT,
    el_torito_image_size     INTEGER,
    inspect_errors_json      TEXT,             -- non-null when inspection had errors
    inspected_at             TEXT,
    PRIMARY KEY (product_slug, release_slug, archive_filename, image_path)
);
CREATE INDEX idx_wwdi_image_sha256       ON winworld_disc_image(image_sha256)       WHERE image_sha256 IS NOT NULL;
CREATE INDEX idx_wwdi_filetree_sha256    ON winworld_disc_image(filetree_sha256)    WHERE filetree_sha256 IS NOT NULL;
CREATE INDEX idx_wwdi_volume_identifier  ON winworld_disc_image(volume_identifier)  WHERE volume_identifier IS NOT NULL;
CREATE INDEX idx_wwdi_iso_filename       ON winworld_disc_image(iso_filename COLLATE NOCASE) WHERE iso_filename IS NOT NULL;
CREATE INDEX idx_wwdi_archive_filename   ON winworld_disc_image(archive_filename COLLATE NOCASE);
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
    winworld_disc_images: Iterable[dict[str, Any]] | None = None,
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
        disc_image_count = 0
        if winworld_records is not None:
            winworld_count = _insert_winworld(conn, winworld_records)
            if winworld_disc_images is not None:
                disc_image_count = _insert_winworld_disc_images(conn, winworld_disc_images)
            _write_meta_row(
                conn,
                source="winworld",
                schema_version=WINWORLD_SCHEMA_VERSION,
                source_commit=source_commit,
                row_count=winworld_count,
            )
            if disc_image_count:
                logging.info("winworld_disc_image: %d rows", disc_image_count)

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


WINWORLD_ARCHIVES_DIR = WINWORLD_DATA_DIR / "archives"
# Consolidated, committed JSONL — one line per disc_image row. Generated from
# the NAS sidecars via `winworld/scripts/build_disc_images_jsonl.py` and
# committed so the GH runner can build the DB without NAS access.
WINWORLD_DISC_IMAGES_JSONL = REPO_ROOT / "winworld" / "data" / "disc_images.jsonl"


def read_winworld_disc_images(
    archives_dir: Path = WINWORLD_ARCHIVES_DIR,
    jsonl_path: Path = WINWORLD_DISC_IMAGES_JSONL,
) -> Iterator[dict[str, Any]]:
    """Yield one row per inspected disc image, schema matching
    `winworld_disc_image` table columns.

    Prefers the committed `disc_images.jsonl` when present (this is what the
    GH runner reads). Falls back to walking the NAS sidecars at
    `WINWORLD_DATA_DIR/archives/<product>/<release>/<file>.7z.derived.json`
    so locally-mounted archives still work.
    """
    if jsonl_path.exists():
        with jsonl_path.open("rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield orjson.loads(line)
        return
    if not archives_dir.is_dir():
        return
    for sidecar in archives_dir.glob("*/*/*.derived.json"):
        try:
            doc = orjson.loads(sidecar.read_bytes())
        except (OSError, ValueError):
            continue
        rel = sidecar.relative_to(archives_dir)
        parts = rel.parts  # (product_slug, release_slug, "<archive>.7z.derived.json")
        if len(parts) != 3 or not parts[2].endswith(".7z.derived.json"):
            continue
        product_slug = parts[0]
        release_slug = parts[1]
        archive_filename = parts[2][: -len(".derived.json")]  # strip → "<name>.7z"

        inspected_at = doc.get("completed_at")
        for img in doc.get("disc_images") or []:
            abs_path = img.get("path") or ""
            # Extract path relative to the archive's extracted root, e.g.
            #   .../extracted/<product>/<release>/<archive_filename>/<...>/<iso>
            # We want the part after `<archive_filename>/`.
            image_path = abs_path
            marker = f"/{archive_filename}/"
            idx = abs_path.find(marker)
            if idx >= 0:
                image_path = abs_path[idx + len(marker):]
            iso_filename = image_path.rsplit("/", 1)[-1] if image_path else None

            errs = img.get("errors") or []
            yield {
                "product_slug":             product_slug,
                "release_slug":             release_slug,
                "archive_filename":         archive_filename,
                "iso_filename":             iso_filename,
                "image_path":               image_path,
                "image_sha256":             _lower_hex(_nn(img.get("image_sha256"))),
                "size_bytes":               img.get("size_bytes"),
                "filetree_sha256":          _lower_hex(_nn(img.get("filetree_sha256"))),
                "file_count":               img.get("file_count"),
                "dir_count":                img.get("dir_count"),
                "volume_identifier":        _nn(img.get("volume_identifier")),
                "system_identifier":        _nn(img.get("system_identifier")),
                "publisher_identifier":     _nn(img.get("publisher_identifier")),
                "preparer_identifier":      _nn(img.get("preparer_identifier")),
                "application_identifier":   _nn(img.get("application_identifier")),
                "volume_creation_date":     _nn(img.get("volume_creation_date")),
                "volume_modification_date": _nn(img.get("volume_modification_date")),
                "logical_block_size":       img.get("logical_block_size"),
                "has_joliet":               int(bool(img.get("has_joliet")))     if img.get("has_joliet")     is not None else None,
                "has_rock_ridge":           int(bool(img.get("has_rock_ridge"))) if img.get("has_rock_ridge") is not None else None,
                "has_udf":                  int(bool(img.get("has_udf")))        if img.get("has_udf")        is not None else None,
                "has_eltorito":             int(bool(img.get("has_eltorito")))   if img.get("has_eltorito")   is not None else None,
                "el_torito_platform":       _nn(img.get("el_torito_platform")),
                "el_torito_image_sha256":   _lower_hex(_nn(img.get("el_torito_image_sha256"))),
                "el_torito_image_size":     img.get("el_torito_image_size"),
                "inspect_errors_json":      orjson.dumps(errs).decode() if errs else None,
                "inspected_at":             inspected_at,
            }


_WINWORLD_DISC_IMAGE_COLS = (
    "product_slug", "release_slug", "archive_filename", "iso_filename", "image_path",
    "image_sha256", "size_bytes", "filetree_sha256", "file_count", "dir_count",
    "volume_identifier", "system_identifier", "publisher_identifier", "preparer_identifier",
    "application_identifier", "volume_creation_date", "volume_modification_date",
    "logical_block_size", "has_joliet", "has_rock_ridge", "has_udf", "has_eltorito",
    "el_torito_platform", "el_torito_image_sha256", "el_torito_image_size",
    "inspect_errors_json", "inspected_at",
)


def _insert_winworld_disc_images(
    conn: sqlite3.Connection, images: Iterable[dict[str, Any]]
) -> int:
    sql = (
        "INSERT OR REPLACE INTO winworld_disc_image ("
        + ",".join(_WINWORLD_DISC_IMAGE_COLS)
        + ") VALUES ("
        + ",".join("?" * len(_WINWORLD_DISC_IMAGE_COLS))
        + ")"
    )
    count = 0
    for row in images:
        conn.execute(sql, tuple(row.get(c) for c in _WINWORLD_DISC_IMAGE_COLS))
        count += 1
    return count
