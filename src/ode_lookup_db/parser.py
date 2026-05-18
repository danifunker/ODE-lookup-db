"""Parse a redump.org disc page into a schema-conformant dict.

Structural patterns (as of 2026-05):
  - <h1> holds the disc title.
  - table.gameinfo:  rows of <th>label</th><td>value</td>
  - table.dumpinfo:  one row, "Dumpers" -> <a> links
  - table.gamecomments: alternating <th>label</th> and <td>value</td> rows.
                     Labels seen: Metadata, Barcode, Comments, Protection, Contents
  - table.tracks:    data rows of [#, Type, Pregap, Length, Sectors, Size, CRC-32, MD5, SHA-1]
                     plus a "Total" summary row (skip).
  - table.rings:     mastering / SID / mould codes per ring.
  - table.pvd:       Creation / Modification / Expiration / Effective dates.
                     All-zero (0000-00-00) rows are absent.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from . import ALLOWED_SYSTEMS, SCHEMA_VERSION
from .languages import to_iso_code

log = logging.getLogger(__name__)

DISC_URL_RE = re.compile(r"https?://redump\.org/disc/(\d+)/?")
# An absent PVD date renders as "0000-00-00" OR as nbsp-padded dashes
# (e.g. "    -  -  " after entity-decoding and whitespace-stripping).
ABSENT_DATE_RE = re.compile(r"^(?:0{4}-0{2}-0{2}|[\s\-]*)$")

TRACK_TYPE_MAP = {
    "Data/Mode 1": "data",
    "Data/Mode 2": "data",
    "Data/Mode 2 Form 1": "data",
    "Data/Mode 2 Form 2": "data",
    "Audio": "audio",
}


class ParseError(ValueError):
    """Raised when a disc page cannot be parsed into a valid row."""


def parse_disc_page(html: str, redump_id: int, system: str) -> dict[str, Any]:
    """Parse one disc page. Returns a dict matching disc.schema.json.

    The caller supplies `system` because the disc page itself doesn't always
    cleanly identify its system bucket — the system page that linked here does.
    """
    if system not in ALLOWED_SYSTEMS:
        raise ParseError(f"system {system!r} not in allowlist {ALLOWED_SYSTEMS}")

    tree = HTMLParser(html)

    gameinfo = _parse_label_table(tree, "table.gameinfo")
    comments = _parse_gamecomments(tree)

    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "redump_id": redump_id,
        "system": system,
        "title": _extract_title(tree),
        "redump_url": f"http://redump.org/disc/{redump_id}/",
        "scraped_at": dt.datetime.now(dt.UTC).isoformat(),
        "tracks": _extract_tracks(tree),
    }

    optional = {
        "region": _extract_imgs_titles(gameinfo.get("Region")),
        "languages_raw": _extract_imgs_titles(gameinfo.get("Languages")),
        "edition": _text(gameinfo.get("Edition")),
        "version": _text(gameinfo.get("Version")),
        "serials": _extract_serials(gameinfo),
        "barcode": _normalize_barcode(comments.get("Barcode")),
        "category": _text(gameinfo.get("Category")),
        "pvd": _extract_pvd(tree),
        "disc_structure": _extract_disc_structure(tree),
        "dumpers": _extract_dumpers(tree),
        "date_added": _text(gameinfo.get("Added")),
        "date_last_modified": _text(gameinfo.get("Last modified")),
    }
    for field, value in optional.items():
        if value not in (None, "", [], {}):
            row[field] = value

    if "languages_raw" in row:
        row["languages"] = [to_iso_code(raw) for raw in row["languages_raw"]]

    return row


def extract_disc_ids_from_system_page(html: str) -> list[int]:
    """Return all redump disc IDs linked from a system listing page, in document order."""
    tree = HTMLParser(html)
    ids: list[int] = []
    seen: set[int] = set()
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        m = DISC_URL_RE.search(href) or re.match(r"^/disc/(\d+)/?$", href)
        if m:
            rid = int(m.group(1))
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def _parse_label_table(tree: HTMLParser, selector: str) -> dict[str, Node]:
    """For tables of <tr><th>label</th><td>value</td></tr>, return {label: td_node}."""
    out: dict[str, Node] = {}
    table = tree.css_first(selector)
    if table is None:
        return out
    for tr in table.css("tr"):
        ths = tr.css("th")
        tds = tr.css("td")
        if len(ths) == 1 and len(tds) == 1:
            label = ths[0].text(strip=True)
            if label:
                out[label] = tds[0]
    return out


def _parse_gamecomments(tree: HTMLParser) -> dict[str, Node]:
    """Game-comments table uses alternating th-only and td-only rows."""
    out: dict[str, Node] = {}
    table = tree.css_first("table.gamecomments")
    if table is None:
        return out
    current_label: str | None = None
    for tr in table.css("tr"):
        ths = tr.css("th")
        tds = tr.css("td")
        if ths and not tds:
            current_label = ths[0].text(strip=True) or None
        elif tds and current_label is not None:
            if current_label not in out:
                out[current_label] = tds[0]
    return out


def _extract_imgs_titles(cell: Node | None) -> list[str]:
    """For a cell containing <img title="X" /> tags, return the titles in order."""
    if cell is None:
        return []
    return [img.attributes.get("title", "") for img in cell.css("img") if img.attributes.get("title")]


def _extract_title(tree: HTMLParser) -> str:
    node = tree.css_first("#main h1")
    title = _text(node)
    if not title:
        raise ParseError("title not found on disc page")
    return title


def _extract_serials(gameinfo: dict[str, Node]) -> list[str]:
    cell = gameinfo.get("Serial")
    if cell is None:
        return []
    raw = cell.text(strip=True)
    if not raw:
        return []
    # Redump occasionally lists multiple serials comma-separated.
    return [s.strip() for s in raw.split(",") if s.strip()]


def _normalize_barcode(cell: Node | None) -> str | None:
    if cell is None:
        return None
    text = cell.text(strip=True)
    return text or None


def _extract_dumpers(tree: HTMLParser) -> list[str]:
    table = tree.css_first("table.dumpinfo")
    if table is None:
        return []
    return [a.text(strip=True) for a in table.css("a") if a.text(strip=True)]


def _extract_tracks(tree: HTMLParser) -> list[dict[str, Any]]:
    table = tree.css_first("table.tracks")
    if table is None:
        raise ParseError("no tracks table on disc page")
    tracks: list[dict[str, Any]] = []
    for tr in table.css("tr"):
        tds = tr.css("td")
        if len(tds) < 9:
            continue
        first_cell = tds[0].text(strip=True)
        if not first_cell.isdigit():  # skips header (no <td>) and "Total" row (empty first cell)
            continue
        type_raw = tds[1].text(strip=True)
        track_type = TRACK_TYPE_MAP.get(type_raw)
        if track_type is None:
            log.warning("unknown track type %r — skipping track", type_raw)
            continue
        size_text = tds[5].text(strip=True)
        try:
            size_bytes = int(size_text)
        except ValueError:
            size_bytes = None
        track: dict[str, Any] = {
            "number": int(first_cell),
            "type": track_type,
            "size_bytes": size_bytes,
            "crc32": _hex_or_none(tds[6].text(strip=True), 8),
            "md5": _hex_or_none(tds[7].text(strip=True), 32),
            "sha1": _hex_or_none(tds[8].text(strip=True), 40),
        }
        tracks.append(track)
    if not tracks:
        raise ParseError("no tracks parsed from tracks table")
    return tracks


def _hex_or_none(text: str, length: int) -> str | None:
    text = text.strip().lower()
    if len(text) != length:
        return None
    if not all(c in "0123456789abcdef" for c in text):
        return None
    return text


def _extract_pvd(tree: HTMLParser) -> dict[str, Any] | None:
    table = tree.css_first("table.pvd")
    if table is None:
        return None
    out: dict[str, Any] = {
        "volume_identifier": None,
        "system_identifier": None,
        "creation_date": None,
        "modification_date": None,
        "expiration_date": None,
        "effective_date": None,
    }
    label_to_field = {
        "Creation": "creation_date",
        "Modification": "modification_date",
        "Expiration": "expiration_date",
        "Effective": "effective_date",
    }
    for tr in table.css("tr"):
        tds = tr.css("td")
        if len(tds) < 5:
            continue
        label = tds[0].text(strip=True)
        field = label_to_field.get(label)
        if field is None:
            continue
        date_str = tds[2].text(strip=True)
        time_str = tds[3].text(strip=True)
        gmt_str = tds[4].text(strip=True)
        out[field] = _format_pvd_datetime(date_str, time_str, gmt_str)
    if all(v is None for v in out.values()):
        return None
    return out


def _format_pvd_datetime(date_str: str, time_str: str, gmt_str: str) -> str | None:
    if not date_str or ABSENT_DATE_RE.match(date_str):
        return None
    parts = [date_str]
    if time_str and not time_str.startswith(":"):
        parts.append("T" + time_str)
        if gmt_str and ":" in gmt_str:
            parts.append(gmt_str)
    return "".join(parts)


def _extract_disc_structure(tree: HTMLParser) -> dict[str, Any] | None:
    table = tree.css_first("table.rings")
    if table is None:
        return None
    # Locate header row to learn column order — varies by disc.
    header_row = None
    for tr in table.css("tr"):
        if tr.css("th") and not tr.css("td"):
            header_row = tr
            break
    if header_row is None:
        return None
    headers = [th.text(strip=True) for th in header_row.css("th")]

    def idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    mastering_i = idx("Mastering Code (laser branded/etched)")
    msid_i = idx("Mastering SID Code")
    mould_i = idx("Mould SID Code")

    mastering_codes: list[str] = []
    mould_sid: str | None = None
    ifpi: str | None = None

    for tr in table.css("tr"):
        tds = tr.css("td")
        if not tds:
            continue
        first = tds[0].text(strip=True)
        if not first.isdigit():
            continue

        def cell_text(i: int | None) -> str | None:
            if i is None or i >= len(tds):
                return None
            text = tds[i].text(strip=True)
            if not text or text == "NULL":
                return None
            return re.sub(r"\s+", " ", text)

        m = cell_text(mastering_i)
        if m:
            mastering_codes.append(m)
        if mould_sid is None:
            mould_sid = cell_text(mould_i)
        if ifpi is None:
            ifpi = cell_text(msid_i)

    if not mastering_codes and mould_sid is None and ifpi is None:
        return None
    return {
        "ring_mastering_codes": mastering_codes,
        "mould_sid": mould_sid,
        "ifpi": ifpi,
        "layer_break": None,
    }
