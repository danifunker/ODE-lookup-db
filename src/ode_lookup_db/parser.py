"""Parse a redump.info disc page into a schema-conformant dict.

Structural patterns (redump.info, as of 2026-07):
  - `.disc-title-box h2` holds the disc title.
  - table.disc-info-table: rows of <th>label</th><td>value</td>. Labels seen:
    System, Media, Category, Region, Language(s), Disc Serial, Edition,
    Barcode, Version, Layerbreak, Errors. Region/Language render as
    <img title="..."> flag icons.
  - .dump-info-section table: Status, Added, Modified, Dumper(s).
  - section.section-collapsible / p.disc-comments: free-form comments; carries
    "<b>Volume Label</b>: XXX", protection notes, etc.
  - table.tracks-table:  [#, Type, Pregap, Length, Sectors]. Metadata only —
    per-file hashes live in the separate files table. DVDs have no tracks table.
  - table.files-table:   [Filename, Size, CRC-32, MD5, SHA-1].
  - table.ring-table:    [#, layer, Mastering Code, Mastering SID, Mould SIDs, ...].
  - table.binary-table:  PVD — rows of [label, Contents(hex), Date, Time, GMT].
                         All-zero (0000-00-00) rows are absent/placeholder.

Note the schema change from the old redump.org parser (schema v2 -> v3): tracks
carry only physical layout (no hashes), and hashes move to a first-class
`files` array. See schema/disc.schema.json and MIGRATION-unified-db.md.
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

DISC_URL_RE = re.compile(r"https?://redump\.(?:info|org)/disc/(\d+)/?")
# Volume label in a comments paragraph: "<b>Volume Label</b>: MYST_UK". The
# paragraph may carry several "label: value" lines; stop at the first newline or
# tag so we capture just this value, and take the first Volume Label if repeated.
VOLUME_LABEL_RE = re.compile(r"<b>\s*Volume Label\s*</b>\s*:\s*([^<\n]+)", re.IGNORECASE)
# An absent PVD date renders as "0000-00-00".
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

    The caller supplies `system` because the disc page itself identifies its
    system by long label (e.g. "IBM PC compatible") rather than our bucket slug;
    the listing that linked here is the authoritative source of the bucket.
    """
    if system not in ALLOWED_SYSTEMS:
        raise ParseError(f"system {system!r} not in allowlist {ALLOWED_SYSTEMS}")

    tree = HTMLParser(html)

    disc_info = _parse_label_table(tree, "table.disc-info-table")
    dump_info = _parse_dump_info(tree)

    files = _extract_files(tree)
    if not files:
        raise ParseError("no files table / no files on disc page")

    layer_break = _parse_int(_text(disc_info.get("Layerbreak")))

    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "redump_id": redump_id,
        "system": system,
        "title": _extract_title(tree),
        "redump_url": f"https://redump.info/disc/{redump_id}",
        "scraped_at": dt.datetime.now(dt.UTC).isoformat(),
        "tracks": _extract_tracks(tree),
        "files": files,
    }

    optional = {
        "region": _extract_imgs_titles(disc_info.get("Region")),
        "languages_raw": _extract_imgs_titles(
            disc_info.get("Languages") or disc_info.get("Language")
        ),
        "edition": _text(disc_info.get("Edition")),
        "version": _text(disc_info.get("Version")),
        "serials": _extract_serials(disc_info),
        "barcode": _text(disc_info.get("Barcode")),
        "category": _text(disc_info.get("Category")),
        "media": _text(disc_info.get("Media")),
        "pvd": _extract_pvd(tree, volume_identifier=_extract_volume_label(html)),
        "disc_structure": _extract_disc_structure(tree, layer_break=layer_break),
        "dumpers": _extract_dumpers(dump_info),
        "date_added": dump_info.get("Added"),
        "date_last_modified": dump_info.get("Modified"),
        "cuesheet_sha1": _extract_cuesheet_sha1(files),
    }
    for field, value in optional.items():
        if value not in (None, "", [], {}):
            row[field] = value

    if "languages_raw" in row:
        row["languages"] = [to_iso_code(raw) for raw in row["languages_raw"]]

    return row


def extract_disc_ids_from_system_page(html: str) -> list[int]:
    """Return all redump disc IDs linked from a listing page, in document order."""
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


def extract_rows_from_added_desc_page(html: str) -> list[tuple[int, str]]:
    """Return [(redump_id, system_label), ...] from the added-desc listing, newest-first.

    On redump.info the listing lives at /discs?sort=added&order=desc&page=N and
    its table has a System column carrying the short slug text (e.g. "PC", "MAC").
    Caller is responsible for filtering to in-scope systems.
    """
    tree = HTMLParser(html)
    rows: list[tuple[int, str]] = []
    seen: set[int] = set()
    for table in tree.css("table"):
        trs = table.css("tr")
        if not trs:
            continue
        headers = [th.text(strip=True) for th in trs[0].css("th")]
        if not headers or "System" not in headers:
            continue
        sys_idx = headers.index("System")
        for tr in trs[1:]:
            tds = tr.css("td")
            if len(tds) <= sys_idx:
                continue
            a = tr.css_first("a[href^='/disc/']") or tr.css_first("a")
            if not a:
                continue
            href = a.attributes.get("href") or ""
            m = re.match(r"^/disc/(\d+)/?$", href) or DISC_URL_RE.search(href)
            if not m:
                continue
            rid = int(m.group(1))
            if rid in seen:
                continue
            seen.add(rid)
            rows.append((rid, tds[sys_idx].text(strip=True)))
        break  # only the first matching table
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def _row_cells(tr: Node) -> list[Node]:
    """Return a two-column row's [label_cell, value_cell], else [].

    redump.info renders these tables as two <td>s per row with the label bolded
    in the first (`<td><strong>System</strong></td><td>...</td>`); older markup
    used <th>/<td>. Accept either.
    """
    cells = tr.css("th, td")
    return cells if len(cells) == 2 else []


def _parse_label_table(tree: HTMLParser, selector: str) -> dict[str, Node]:
    """For two-column label/value tables, return {label: value_node}."""
    out: dict[str, Node] = {}
    table = tree.css_first(selector)
    if table is None:
        return out
    for tr in table.css("tr"):
        cells = _row_cells(tr)
        if not cells:
            continue
        label = cells[0].text(strip=True)
        if label and label not in out:
            out[label] = cells[1]
    return out


def _parse_dump_info(tree: HTMLParser) -> dict[str, str]:
    """Return {label: value_text} for the dump-info section (Status/Added/Modified/Dumper(s))."""
    out: dict[str, str] = {}
    table = tree.css_first(".dump-info-section table")
    if table is None:
        return out
    for tr in table.css("tr"):
        cells = _row_cells(tr)
        if not cells:
            continue
        label = cells[0].text(strip=True)
        value = cells[1].text(strip=True)
        if label and label not in out:
            out[label] = value
    return out


def _extract_volume_label(html: str) -> str | None:
    m = VOLUME_LABEL_RE.search(html)
    if m is None:
        return None
    value = m.group(1).strip()
    return value or None


def _extract_imgs_titles(cell: Node | None) -> list[str]:
    """For a cell containing <img title="X" /> flag tags, return the titles in order."""
    if cell is None:
        return []
    return [
        img.attributes.get("title", "")
        for img in cell.css("img")
        if img.attributes.get("title")
    ]


def _extract_title(tree: HTMLParser) -> str:
    node = tree.css_first(".disc-title-box h2") or tree.css_first(".disc-view h2")
    title = _text(node)
    if not title:
        raise ParseError("title not found on disc page")
    return title


def _extract_serials(disc_info: dict[str, Node]) -> list[str]:
    cell = disc_info.get("Disc Serial") or disc_info.get("Serial")
    if cell is None:
        return []
    raw = cell.text(strip=True)
    if not raw:
        return []
    # redump occasionally lists multiple serials comma-separated.
    return [s.strip() for s in raw.split(",") if s.strip()]


def _extract_dumpers(dump_info: dict[str, str]) -> list[str]:
    raw = dump_info.get("Dumpers") or dump_info.get("Dumper")
    if not raw:
        return []
    return [d.strip() for d in raw.split(",") if d.strip()]


def _extract_tracks(tree: HTMLParser) -> list[dict[str, Any]]:
    """Header-driven track parsing: [#, Type, Pregap, Length, Sectors].

    Returns [] for media with no track table (e.g. DVDs, whose single image is
    described only by the files table).
    """
    table = tree.css_first("table.tracks-table")
    if table is None:
        return []

    headers: list[str] = []
    for tr in table.css("tr"):
        ths = tr.css("th")
        if ths and not tr.css("td"):
            candidate = [th.text(strip=True) for th in ths]
            if "#" in candidate:
                headers = candidate
                break
    if not headers:
        return []

    def col(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    i_num = col("#")
    i_type = col("Type")
    i_pregap = col("Pregap")
    i_length = col("Length")
    i_sectors = col("Sectors")
    if i_num is None:
        return []

    def cell(tds: list[Node], i: int | None) -> str | None:
        if i is None or i >= len(tds):
            return None
        return tds[i].text(strip=True) or None

    tracks: list[dict[str, Any]] = []
    for tr in table.css("tr"):
        tds = tr.css("td")
        if not tds:
            continue
        first = tds[i_num].text(strip=True) if i_num < len(tds) else ""
        if not first.isdigit():  # skips header and the "Total" summary row
            continue

        type_raw = cell(tds, i_type)
        track_type = TRACK_TYPE_MAP.get(type_raw) if type_raw is not None else "data"
        if track_type is None:
            log.warning("unknown track type %r — skipping track", type_raw)
            continue

        tracks.append({
            "number": int(first),
            "type": track_type,
            "pregap": cell(tds, i_pregap),
            "length": cell(tds, i_length),
            "sectors": _parse_int(cell(tds, i_sectors)),
        })
    return tracks


def _extract_files(tree: HTMLParser) -> list[dict[str, Any]]:
    """Header-driven file parsing: [Filename, Size, CRC-32, MD5, SHA-1]."""
    table = tree.css_first("table.files-table")
    if table is None:
        return []

    headers: list[str] = []
    for tr in table.css("tr"):
        ths = tr.css("th")
        if ths and not tr.css("td"):
            candidate = [th.text(strip=True) for th in ths]
            if "Filename" in candidate:
                headers = candidate
                break
    if not headers:
        return []

    def col(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    i_name = col("Filename")
    i_size = col("Size")
    i_crc = col("CRC-32")
    i_md5 = col("MD5")
    i_sha1 = col("SHA-1")
    if i_name is None:
        return []

    def cell(tds: list[Node], i: int | None) -> str | None:
        if i is None or i >= len(tds):
            return None
        return tds[i].text(strip=True) or None

    files: list[dict[str, Any]] = []
    for tr in table.css("tr"):
        tds = tr.css("td")
        if not tds or i_name >= len(tds):
            continue
        filename = tds[i_name].text(strip=True)
        if not filename or filename == "Total":
            continue
        files.append({
            "filename": filename,
            "size_bytes": _parse_int(cell(tds, i_size)),
            "crc32": _hex_or_none(cell(tds, i_crc) or "", 8),
            "md5": _hex_or_none(cell(tds, i_md5) or "", 32),
            "sha1": _hex_or_none(cell(tds, i_sha1) or "", 40),
        })
    return files


def _extract_cuesheet_sha1(files: list[dict[str, Any]]) -> str | None:
    for f in files:
        if f["filename"].lower().endswith(".cue"):
            return f.get("sha1")
    return None


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def _hex_or_none(text: str, length: int) -> str | None:
    text = text.strip().lower()
    if len(text) != length:
        return None
    if not all(c in "0123456789abcdef" for c in text):
        return None
    return text


def _extract_pvd(tree: HTMLParser, *, volume_identifier: str | None = None) -> dict[str, Any] | None:
    out: dict[str, Any] = {
        "volume_identifier": volume_identifier,
        "system_identifier": None,
        "creation_date": None,
        "modification_date": None,
        "expiration_date": None,
        "effective_date": None,
    }
    table = tree.css_first("table.binary-table")
    if table is not None:
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


def _extract_disc_structure(tree: HTMLParser, *, layer_break: int | None = None) -> dict[str, Any] | None:
    table = tree.css_first("table.ring-table")
    if table is None:
        if layer_break is None:
            return None
        return {"ring_mastering_codes": [], "mould_sid": None, "ifpi": None, "layer_break": layer_break}

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

    mastering_i = idx("Mastering Code")
    msid_i = idx("Mastering SID")
    mould_i = idx("Mould SIDs")

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
            # separator=' ' recovers spacing between per-segment spans that the
            # site renders without whitespace (e.g. "SATURN""SKM844AB-CD").
            text = tds[i].text(separator=" ", strip=True)
            if not text or text == "NULL":
                return None
            return re.sub(r"\s+", " ", text).strip()

        m = cell_text(mastering_i)
        if m:
            mastering_codes.append(m)
        if mould_sid is None:
            mould_sid = cell_text(mould_i)
        if ifpi is None:
            ifpi = cell_text(msid_i)

    if not mastering_codes and mould_sid is None and ifpi is None and layer_break is None:
        return None
    return {
        "ring_mastering_codes": mastering_codes,
        "mould_sid": mould_sid,
        "ifpi": ifpi,
        "layer_break": layer_break,
    }
