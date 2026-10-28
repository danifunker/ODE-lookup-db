"""JSONL <-> SQLite round-trip and lookup tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path


from ode_lookup_db.db import build_sqlite, read_jsonl, write_jsonl


def _row(rid: int) -> dict:
    return {
        "schema_version": 3,
        "redump_id": rid,
        "system": "pc",
        "title": f"Disc {rid}",
        "redump_url": f"https://redump.info/disc/{rid}",
        "serials": [f"SER-{rid}"],
        "region": ["USA"],
        "languages": ["en"],
        "languages_raw": ["English"],
        "catalog": f"CAT-{rid}",
        "cuesheet_sha1": "c" * 40,
        "pvd": {"volume_identifier": f"VOL_{rid}", "system_identifier": "WIN32"},
        "tracks": [
            {"number": 1, "type": "data", "pregap": "00:00:00", "length": "10:00:00", "sectors": 42},
        ],
        "files": [
            {"filename": f"Disc {rid}.cue", "size_bytes": 100, "crc32": "deadbeef",
             "md5": "a" * 32, "sha1": "b" * 40},
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
        # lookup by hash (hashes live in redump_file as of v3)
        cur = conn.execute("SELECT redump_id FROM redump_file WHERE sha1=?", ("b" * 40,))
        assert sorted(r[0] for r in cur.fetchall()) == [1, 2]

        # tracks renamed type -> kind
        cur = conn.execute("SELECT kind FROM redump_track WHERE redump_id=1")
        assert cur.fetchone()[0] == "data"

        # tracks.sectors + timecodes (v3 physical layout)
        cur = conn.execute(
            "SELECT sectors, pregap, length FROM redump_track WHERE redump_id=1 AND number=1"
        )
        assert cur.fetchone() == (42, "00:00:00", "10:00:00")

        # files carry the filename + hashes (v3)
        cur = conn.execute("SELECT filename FROM redump_file WHERE redump_id=1 AND seq=0")
        assert cur.fetchone()[0] == "Disc 1.cue"

        # cuesheet_sha1 promoted to a disc column (v3)
        cur = conn.execute("SELECT cuesheet_sha1 FROM redump_disc WHERE redump_id=1")
        assert cur.fetchone()[0] == "c" * 40

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
        assert row == (3, "abc123", 2)

        # FTS5 over titles
        cur = conn.execute(
            "SELECT rowid FROM redump_disc_fts WHERE redump_disc_fts MATCH ? ORDER BY rank",
            ("Disc",),
        )
        assert sorted(r[0] for r in cur.fetchall()) == [1, 2]
    finally:
        conn.close()
