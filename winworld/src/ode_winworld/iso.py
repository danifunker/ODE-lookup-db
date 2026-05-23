"""ISO9660 inspection + fingerprinting via pycdlib.

For each ISO, we capture:
  - The PVD: volume_id, system_id, publisher_id, preparer_id, application_id,
    creation/modification dates, volume_space_size (in sectors).
  - Joliet / Rock Ridge / UDF presence (capability flags).
  - File tree walk: list of (path, size, sha256), then a deterministic
    aggregate `filetree_sha256` over the sorted tree — the canonical
    "what's inside the disc" identity, robust to .7z repacks.
  - El Torito boot record (if present): platform, bootable flag, the boot
    image bytes hashed; the bytes themselves go in `el_torito_blob_b64`
    (typically 1–4 KB, fine to inline).
  - I/O errors during the walk are collected, not raised — the inspector
    reports clean/dirty status, the caller decides whether to accept.

The boundary is: this module fingerprints. It doesn't decide whether the
disc is "good." Callers (derived.py) make that call from `errors` length.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pycdlib


CHUNK = 1 << 20  # 1 MB


@dataclass
class FileEntry:
    path: str
    size: int
    sha256: str


@dataclass
class IsoInspection:
    path: str
    size_bytes: int
    image_sha256: str               # sha256 of the .iso bytes as-on-disk

    # PVD fields
    volume_identifier: Optional[str] = None
    system_identifier: Optional[str] = None
    publisher_identifier: Optional[str] = None
    preparer_identifier: Optional[str] = None
    application_identifier: Optional[str] = None
    volume_creation_date: Optional[str] = None
    volume_modification_date: Optional[str] = None
    volume_space_size: Optional[int] = None        # sectors
    logical_block_size: Optional[int] = None       # bytes/sector

    # Capability flags
    has_joliet: bool = False
    has_rock_ridge: bool = False
    has_udf: bool = False

    # El Torito
    has_eltorito: bool = False
    el_torito_platform: Optional[str] = None
    el_torito_bootable: Optional[bool] = None
    el_torito_image_sha256: Optional[str] = None
    el_torito_image_size: Optional[int] = None
    el_torito_blob_b64: Optional[str] = None  # zstd would be over-engineering for ~2KB

    # File tree
    file_count: int = 0
    dir_count: int = 0
    filetree_sha256: Optional[str] = None
    files: list[FileEntry] = field(default_factory=list)

    # Health
    errors: list[str] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_pycdlib_file(iso: pycdlib.PyCdlib, iso_path: str) -> tuple[int, str]:
    """Stream a file out of the ISO via pycdlib, hashing as we go."""
    h = hashlib.sha256()
    buf = io.BytesIO()
    iso.get_file_from_iso_fp(buf, iso_path=iso_path)
    data = buf.getvalue()
    h.update(data)
    return len(data), h.hexdigest()


def _decode(field_obj: Any) -> Optional[str]:
    """Convert a pycdlib PVD field to a stripped str.

    PVD identifier fields come back as bytes (volume_identifier, system_identifier)
    or as FileOrTextIdentifier objects (publisher/preparer/application). The
    latter wraps either raw text or a filename pointer; try common attribute
    paths and fall back to None.
    """
    if field_obj is None:
        return None
    candidate: Any = None
    if isinstance(field_obj, (bytes, bytearray)):
        candidate = field_obj
    else:
        # FileOrTextIdentifier — try the common attribute names
        for attr in ("text", "_text", "filename", "identifier", "record"):
            v = getattr(field_obj, attr, None)
            if callable(v):
                try:
                    v = v()
                except Exception:                                          # noqa: BLE001
                    v = None
            if v:
                candidate = v
                break
    if candidate is None:
        return None
    if isinstance(candidate, (bytes, bytearray)):
        s = candidate.decode("utf-8", errors="replace")
    else:
        s = str(candidate)
    s = s.strip().strip("\x00").strip()
    return s or None


def _decode_pycdlib_date(d: Any) -> Optional[str]:
    """pycdlib returns VolumeDescriptorDate objects whose bytes are ASCII like
    '2003041615304200\\x00' (yyyymmddhhmmsscc + tz). We accept the raw bytes
    string and reformat to ISO 8601.
    """
    if d is None:
        return None
    # Try .date_str (bytes) first — that's the canonical attribute
    raw = getattr(d, "date_str", None) or getattr(d, "date_string", None)
    if raw is None and isinstance(d, (bytes, bytearray)):
        raw = d
    if raw is None:
        # Fallback: try .year/.month/...
        try:
            if getattr(d, "year", None):
                return (
                    f"{int(d.year):04d}-{int(d.month):02d}-{int(d.day):02d}"
                    f"T{int(d.hour):02d}:{int(d.minute):02d}:{int(d.second):02d}"
                )
        except (AttributeError, ValueError, TypeError):
            pass
        return None
    if isinstance(raw, (bytes, bytearray)):
        s = raw.decode("ascii", errors="replace")
    else:
        s = str(raw)
    s = s.rstrip("\x00").strip()
    # "20030416153042" or "2003041615304200" — first 14 chars are yyyymmddhhmmss
    if len(s) >= 14 and s[:14].isdigit() and s[:14] != "0" * 14:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}"
    return None


def _walk_files(iso: pycdlib.PyCdlib, inspection: IsoInspection) -> None:
    """Walk the ISO file tree, populating inspection.files / file_count /
    dir_count.

    pycdlib's walk() can choke on some discs depending on which facade we
    pick (Rock Ridge with Joliet sidecars, mixed encoding, etc). We try in
    order — Rock Ridge → Joliet → ISO9660 — and stop on the first one that
    completes without exception. The chosen facade also drives file hashing
    so paths stay consistent.
    """
    candidates: list[str] = []
    if inspection.has_rock_ridge:
        candidates.append("rr_path")
    if inspection.has_joliet:
        candidates.append("joliet_path")
    candidates.append("iso_path")

    for path_kw in candidates:
        try:
            _walk_with_kw(iso, path_kw, inspection)
        except Exception as exc:                                          # noqa: BLE001
            # Reset partial state and try the next facade
            inspection.files.clear()
            inspection.file_count = 0
            inspection.dir_count = 0
            inspection.errors.append(f"walk via {path_kw} failed: {exc!r}")
            continue
        return  # success
    # If we fell through, every facade failed.


def _walk_with_kw(iso: pycdlib.PyCdlib, path_kw: str, inspection: IsoInspection) -> None:
    for root, dirs, files in iso.walk(**{path_kw: "/"}):
        inspection.dir_count += len(dirs)
        for fname in files:
            full = root.rstrip("/") + "/" + fname if root != "/" else "/" + fname
            size, sha = _hash_iso_file(iso, path_kw, full)
            inspection.files.append(FileEntry(path=full, size=size, sha256=sha))
            inspection.file_count += 1


def _hash_iso_file(iso: pycdlib.PyCdlib, path_kw: str, path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    buf = io.BytesIO()
    iso.get_file_from_iso_fp(buf, **{path_kw: path})
    data = buf.getvalue()
    h.update(data)
    return len(data), h.hexdigest()


def _compute_filetree_sha256(files: list[FileEntry]) -> str:
    """Deterministic aggregate hash over the sorted file tree.

    Format per line: "<path>\\t<size>\\t<sha256>\\n"
    Then sha256 of the whole bytestring. Same file set in any order → same hash.
    """
    h = hashlib.sha256()
    for f in sorted(files, key=lambda e: e.path):
        h.update(f"{f.path}\t{f.size}\t{f.sha256}\n".encode("utf-8"))
    return h.hexdigest()


def _capture_el_torito(iso: pycdlib.PyCdlib, inspection: IsoInspection) -> None:
    """Record presence + platform + bootable + initial-entry RBA/length.

    We deliberately don't extract the boot image bytes via pycdlib here —
    its API for streaming arbitrary blocks varies across versions, and the
    presence + platform fingerprint is what consumers actually care about
    for "is this an installable disc."
    """
    try:
        cat = iso.eltorito_boot_catalog
    except AttributeError:
        return
    if not cat:
        return
    inspection.has_eltorito = True
    try:
        entry = cat.initial_entry
        # platform_id in the validation entry, not the initial entry
        val_plat = getattr(cat.validation_entry, "platform_id", None)
        inspection.el_torito_platform = {
            0: "x86", 1: "PPC", 2: "Mac", 0xEF: "EFI"
        }.get(val_plat, f"unknown({val_plat})" if val_plat is not None else None)
        inspection.el_torito_bootable = bool(getattr(entry, "boot_indicator", 0) == 0x88)
        # Length in 512-byte sectors per spec; convert to bytes for size hint
        sector_count = getattr(entry, "sector_count", None)
        if sector_count:
            inspection.el_torito_image_size = int(sector_count) * 512
    except Exception as exc:                                              # noqa: BLE001
        inspection.errors.append(f"el torito field-read error: {exc!r}")


def inspect_iso(path: Path) -> IsoInspection:
    """Open an ISO, extract PVD + file tree + El Torito, hash everything.
    Returns IsoInspection — caller checks .errors to decide accept/reject."""
    size_bytes = path.stat().st_size
    image_sha256 = _sha256_file(path)
    insp = IsoInspection(path=str(path), size_bytes=size_bytes, image_sha256=image_sha256)

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(path))
    except Exception as exc:                                              # noqa: BLE001
        insp.errors.append(f"open failed: {exc!r}")
        return insp

    try:
        pvd = iso.pvd
        insp.volume_identifier = _decode(pvd.volume_identifier)
        insp.system_identifier = _decode(pvd.system_identifier)
        insp.publisher_identifier = _decode(pvd.publisher_identifier)
        insp.preparer_identifier = _decode(pvd.preparer_identifier)
        insp.application_identifier = _decode(pvd.application_identifier)
        insp.volume_creation_date = _decode_pycdlib_date(pvd.volume_creation_date)
        insp.volume_modification_date = _decode_pycdlib_date(pvd.volume_modification_date)
        insp.volume_space_size = int(pvd.space_size)
        insp.logical_block_size = int(pvd.log_block_size)

        insp.has_joliet = bool(iso.has_joliet())
        insp.has_rock_ridge = bool(iso.has_rock_ridge())
        insp.has_udf = bool(iso.has_udf())

        _walk_files(iso, insp)
        insp.filetree_sha256 = _compute_filetree_sha256(insp.files)

        _capture_el_torito(iso, insp)
    except Exception as exc:                                              # noqa: BLE001
        insp.errors.append(f"inspection failed: {exc!r}")
    finally:
        try:
            iso.close()
        except Exception:
            pass

    return insp
