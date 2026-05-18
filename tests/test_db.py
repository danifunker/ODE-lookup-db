"""JSONL <-> SQLite round-trip and lookup tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ode_lookup_db.db import build_sqlite, read_jsonl, write_jsonl


def _row(rid: int) -> dict:
    return {
        "schema_version": 1,
        "redump_id": rid,
        "system": "pc",
        "title": f"Disc {rid}",
        "redump_url": f"http://redump.org/disc/{rid}/",
        "serials": [f"SER-{rid}"],
        "region": ["USA"],
        "languages": ["en"],
        "languages_raw": ["English"],
        "pvd": {"volume_identifier": f"VOL_{rid}", "system_identifier": "WIN32"},
        "tracks": [
            {"number": 1, "type": "data", "crc32": "deadbeef",
             "md5": "a" * 32, "sha1": "b" * 40, "size_bytes": 100},
        ],
    }


def test_jsonl_roundtrip(tmp_path: Path):
    path = tmp_path / "out.jsonl.gz"
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
        cur = conn.execute("SELECT redump_id FROM tracks WHERE sha1=?", ("b" * 40,))
        assert sorted(r[0] for r in cur.fetchall()) == [1, 2]

        # lookup by serial
        cur = conn.execute("SELECT redump_id FROM serials WHERE serial=?", ("SER-2",))
        assert cur.fetchone()[0] == 2

        # lookup by PVD
        cur = conn.execute("SELECT redump_id FROM discs WHERE pvd_volume_id=?", ("VOL_1",))
        assert cur.fetchone()[0] == 1

        # meta
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "1"
        assert meta["source_commit"] == "abc123"
        assert meta["row_count"] == "2"
    finally:
        conn.close()
