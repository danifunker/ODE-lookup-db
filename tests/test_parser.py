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
    [(133379, "pc", "MYST_UK"), (92225, "pc", "007LEGENDS")],
)
def test_volume_label_positive(redump_id: int, system: str, expected: str):
    row = _parse(redump_id, system)
    assert row["pvd"]["volume_identifier"] == expected


@pytest.mark.parametrize("redump_id,system", [(99835, "mac"), (16345, "pc"), (27832, "pc")])
def test_volume_label_absent(redump_id: int, system: str):
    row = _parse(redump_id, system)
    assert (row.get("pvd") or {}).get("volume_identifier") is None


def test_catalog_all_zero_placeholder_is_none():
    # 27832 carries "CATALOG 0000000000000" — a placeholder, not a real catalog.
    row = _parse(27832, "pc")
    assert row.get("catalog") is None


def test_catalog_real_value_parses():
    html = """
    <html><body><div id="main"><h1>Synthetic</h1>
      <table class="gamecomments">
        <tr><th>Metadata</th></tr>
        <tr><td>CATALOG 1234567890123</td></tr>
      </table>
      <table class="tracks">
        <tr><th>#</th><th>Type</th><th>Pregap</th><th>Length</th><th>Sectors</th>
            <th>Size</th><th>CRC-32</th><th>MD5</th><th>SHA-1</th></tr>
        <tr><td>1</td><td>Data/Mode 1</td><td>00:00:00</td><td>10:00:00</td>
            <td>1000</td><td>2352000</td><td>deadbeef</td>
            <td>%s</td><td>%s</td></tr>
      </table>
    </div></body></html>
    """ % ("a" * 32, "b" * 40)
    row = parse_disc_page(html, redump_id=1, system="pc")
    assert row["catalog"] == "1234567890123"


def test_volume_label_first_wins():
    html = """
    <html><body><div id="main"><h1>Synthetic</h1>
      <table class="gamecomments">
        <tr><th>Comments</th></tr>
        <tr><td><b>Volume Label</b>: FIRST_LABEL<br />
                <b>Volume Label</b>: SECOND_LABEL</td></tr>
      </table>
      <table class="tracks">
        <tr><th>#</th><th>Type</th><th>Pregap</th><th>Length</th><th>Sectors</th>
            <th>Size</th><th>CRC-32</th><th>MD5</th><th>SHA-1</th></tr>
        <tr><td>1</td><td>Data/Mode 1</td><td>00:00:00</td><td>10:00:00</td>
            <td>1000</td><td>2352000</td><td>deadbeef</td>
            <td>%s</td><td>%s</td></tr>
      </table>
    </div></body></html>
    """ % ("a" * 32, "b" * 40)
    row = parse_disc_page(html, redump_id=1, system="pc")
    assert row["pvd"]["volume_identifier"] == "FIRST_LABEL"


def test_track_sectors_captured():
    # 133379 single data track lists 312161 sectors in the HTML.
    row = _parse(133379, "pc")
    assert row["tracks"][0]["sectors"] == 312161
