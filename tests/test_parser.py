"""Parser tests.

Most tests are fixture-based: real redump HTML in tests/fixtures/canary/ is
parsed and compared to the expected JSON. This both proves the parser correct
and locks behavior so HTML regressions on redump's side show up loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ode_lookup_db.parser import (
    ParseError,
    _extract_volume_label,
    extract_disc_ids_from_system_page,
    parse_disc_page,
)
from ode_lookup_db.validator import validate_rows

FIXTURES = Path(__file__).parent / "fixtures" / "canary"

# (redump_id, system) — matches scripts/canary.py CANARY_IDS
CANARY: list[tuple[int, str]] = [
    (133379, "pc"),
    (99835, "mac"),
    (44803, "pc"),
    (27832, "pc"),
    (16345, "pc"),
    (92225, "pc"),  # DVD-9 — 6-col track table
]


def test_extract_disc_ids_from_system_page():
    html = """
    <html><body>
      <a href="/disc/100/">A</a>
      <a href="http://redump.org/disc/200/">B</a>
      <a href="/disc/100/">dup</a>
      <a href="/elsewhere/">no</a>
      <a href="/disc/300">C</a>
    </body></html>
    """
    assert extract_disc_ids_from_system_page(html) == [100, 200, 300]


def test_parse_rejects_unknown_system():
    with pytest.raises(ParseError, match="system"):
        parse_disc_page("<html><h1>x</h1></html>", redump_id=1, system="psx")


def test_parse_requires_title():
    with pytest.raises(ParseError):
        parse_disc_page("<html></html>", redump_id=1, system="pc")


@pytest.mark.parametrize("redump_id,system", CANARY)
def test_fixture_matches_expected(redump_id: int, system: str):
    html = (FIXTURES / f"{redump_id}.html").read_text()
    expected = json.loads((FIXTURES / f"{redump_id}.expected.json").read_text())
    actual = parse_disc_page(html, redump_id=redump_id, system=system)
    actual.pop("scraped_at", None)
    assert actual == expected


@pytest.mark.parametrize("redump_id,system", CANARY)
def test_fixture_passes_schema_validation(redump_id: int, system: str):
    html = (FIXTURES / f"{redump_id}.html").read_text()
    row = parse_disc_page(html, redump_id=redump_id, system=system)
    report = validate_rows([row])
    assert report.ok, report.hard_errors


def _parse(redump_id: int, system: str) -> dict:
    html = (FIXTURES / f"{redump_id}.html").read_text()
    return parse_disc_page(html, redump_id=redump_id, system=system)


@pytest.mark.parametrize(
    "redump_id,system,expected",
    [(133379, "pc", "MYST_UK"), (92225, "pc", "007LEGENDS"), (16345, "pc", "ALICE00A")],
)
def test_volume_label_positive(redump_id: int, system: str, expected: str):
    row = _parse(redump_id, system)
    assert row["pvd"]["volume_identifier"] == expected


@pytest.mark.parametrize("redump_id,system", [(99835, "mac"), (27832, "pc")])
def test_volume_label_absent(redump_id: int, system: str):
    row = _parse(redump_id, system)
    assert (row.get("pvd") or {}).get("volume_identifier") is None


def test_volume_label_first_wins():
    # redump.info renders comments as a pre-wrap paragraph; a repeated label or a
    # second "<b>...</b>: ..." line on the next row must not bleed into the value.
    html = (
        '<p class="pre-wrap disc-comments">'
        "<b>Volume Label</b>: FIRST_LABEL\n"
        "<b>Volume Label</b>: SECOND_LABEL</p>"
    )
    assert _extract_volume_label(html) == "FIRST_LABEL"


def test_volume_label_stops_at_newline():
    html = '<p class="disc-comments"><b>Volume Label</b>: MYST_UK\nCover variant notes</p>'
    assert _extract_volume_label(html) == "MYST_UK"


def test_files_populated_with_hashes():
    # 133379 lists a .cue plus the .bin/.img; hashes now live on files.
    row = _parse(133379, "pc")
    assert len(row["files"]) >= 2
    binfile = next(f for f in row["files"] if f["filename"].lower().endswith(".bin"))
    assert binfile["md5"] and binfile["sha1"]
    # cuesheet_sha1 is lifted from the .cue file
    assert row["cuesheet_sha1"] == next(
        f["sha1"] for f in row["files"] if f["filename"].lower().endswith(".cue")
    )


def test_dvd_has_no_tracks_but_has_file():
    # 92225 is a DVD-9: no track table, a single .iso file, and a layerbreak.
    row = _parse(92225, "pc")
    assert row["tracks"] == []
    assert len(row["files"]) == 1
    assert row["disc_structure"]["layer_break"] == 2008656


def test_track_sectors_captured():
    # 133379 single data track lists 312161 sectors in the HTML.
    row = _parse(133379, "pc")
    assert row["tracks"][0]["sectors"] == 312161
