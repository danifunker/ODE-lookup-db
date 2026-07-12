"""Validator failsafe tests."""

from __future__ import annotations


from ode_lookup_db.validator import MAX_NEW_ROWS_PER_RUN, validate_rows


def _row(rid: int, **overrides) -> dict:
    base = {
        "schema_version": 3,
        "redump_id": rid,
        "system": "pc",
        "title": f"Test Disc {rid}",
        "redump_url": f"https://redump.info/disc/{rid}",
        "tracks": [{"number": 1, "type": "data", "sectors": 42}],
        "files": [{"filename": f"Test Disc {rid}.bin", "crc32": "deadbeef"}],
    }
    base.update(overrides)
    return base


def test_valid_row_passes():
    report = validate_rows([_row(1), _row(2)])
    assert report.ok
    assert not report.warnings


def test_duplicate_redump_id_is_hard_failure():
    report = validate_rows([_row(1), _row(1)])
    assert not report.ok
    assert any("duplicate" in e for e in report.hard_errors)


def test_system_not_in_allowlist_is_hard_failure():
    report = validate_rows([_row(1, system="psx")])
    assert not report.ok


def test_bad_hash_format_is_hard_failure():
    bad = _row(1, files=[{"filename": "x.bin", "crc32": "nothex!!"}])
    report = validate_rows([bad])
    assert not report.ok


def test_missing_required_field_is_hard_failure():
    bad = _row(1)
    del bad["title"]
    report = validate_rows([bad])
    assert not report.ok


def test_growth_cap_exceeded():
    rows = [_row(i) for i in range(1, MAX_NEW_ROWS_PER_RUN + 5)]
    report = validate_rows(rows, previous_ids=set())
    assert not report.ok
    assert any("growth cap" in e for e in report.hard_errors)


def test_shrink_is_warning_not_failure():
    report = validate_rows([_row(1)], previous_ids={1, 2, 3})
    assert report.ok
    assert any("shrank" in w for w in report.warnings)


def test_files_are_required():
    bad = _row(1)
    del bad["files"]
    report = validate_rows([bad])
    assert not report.ok
    assert any("files" in e for e in report.hard_errors)
