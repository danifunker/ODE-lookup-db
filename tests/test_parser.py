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
