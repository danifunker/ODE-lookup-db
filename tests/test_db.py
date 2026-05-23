"""JSONL <-> SQLite round-trip and lookup tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path


from ode_lookup_db.db import build_sqlite, read_jsonl, write_jsonl


def _row(rid: int) -> dict:
    return {
        "schema_version": 2,
        "redump_id": rid,
        "system": "pc",
        "title": f"Disc {rid}",
        "redump_url": f"http://redump.org/disc/{rid}/",
        "serials": [f"SER-{rid}"],
        "region": ["USA"],
        "languages": ["en"],
        "languages_raw": ["English"],
        "catalog": f"CAT-{rid}",
        "pvd": {"volume_identifier": f"VOL_{rid}", "system_identifier": "WIN32"},
        "tracks": [
            {"number": 1, "type": "data", "sectors": 42, "crc32": "deadbeef",
             "md5": "a" * 32, "sha1": "b" * 40, "size_bytes": 100},
        ],
    }


def test_jsonl_roundtrip(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    rows = [_row(2), _row(1), _row(3)]
    n = write_jsonl(rows, path=path)
    assert n == 3
    loaded = list(read_jsonl(path=path))
    # Must be sorted by redump_id for deterministic diffs
    assert [r["redump_id"] for r in loaded] == [1, 2, 3]


def test_sqlite_build_and_lookup(tmp_path: Path):
    path = tmp_path / "test.sqlite"
    rows = [_row(1), _row(2)]
    n = build_sqlite(rows, path=path, source_commit="abc123")
    assert n == 2

    conn = sqlite3.connect(path)
    try:
        # lookup by hash
        cur = conn.execute("SELECT redump_id FROM redump_track WHERE sha1=?", ("b" * 40,))
        assert sorted(r[0] for r in cur.fetchall()) == [1, 2]

        # tracks renamed type -> kind
        cur = conn.execute("SELECT kind FROM redump_track WHERE redump_id=1")
        assert cur.fetchone()[0] == "data"

        # tracks.sectors promoted column (v2)
        cur = conn.execute("SELECT sectors FROM redump_track WHERE redump_id=1 AND number=1")
        assert cur.fetchone()[0] == 42

        # lookup by serial
        cur = conn.execute("SELECT redump_id FROM redump_serial WHERE serial=?", ("SER-2",))
        assert cur.fetchone()[0] == 2

        # lookup by PVD
        cur = conn.execute("SELECT redump_id FROM redump_disc WHERE pvd_volume_id=?", ("VOL_1",))
        assert cur.fetchone()[0] == 1

        # lookup by catalog (v2)
        cur = conn.execute("SELECT redump_id FROM redump_disc WHERE catalog=?", ("CAT-2",))
        assert cur.fetchone()[0] == 2

        # languages renamed code -> lang
        cur = conn.execute("SELECT lang FROM redump_language WHERE redump_id=1")
        assert cur.fetchone()[0] == "en"

        # meta has one row per source with named columns
        row = conn.execute(
            "SELECT schema_version, source_commit, row_count FROM meta WHERE source='redump'"
        ).fetchone()
        assert row == (2, "abc123", 2)

        # FTS5 over titles
        cur = conn.execute(
            "SELECT rowid FROM redump_disc_fts WHERE redump_disc_fts MATCH ? ORDER BY rank",
            ("Disc",),
        )
        assert sorted(r[0] for r in cur.fetchall()) == [1, 2]
    finally:
        conn.close()
