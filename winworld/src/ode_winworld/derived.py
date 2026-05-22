"""Extract a downloaded .7z, inspect the disc image(s) inside, hash everything,
write a `.derived.json` sidecar next to the original `.dl.json`.

The contract (per user):
  - WinWorld's published archive_hash is informational.
  - Ground truth = "7z extracted cleanly AND the disc image inspects cleanly."
  - If both hold, the disc's own bytes are hashed and recorded as canonical.

Sidecar layout (`<filename>.derived.json`):
  {
    "status": "ok" | "no_disc_image" | "extract_failed" | "inspect_failed"
              | "source_unavailable",
    "extracted_at": "...",
    "extracted_dir": ".../extracted/<product>/<release>/<download_id>/",
    "extract": { "ok": bool, "exit_code": int, "binary": "/opt/.../7zz",
                 "stdout_tail": "...", "stderr_tail": "..." },
    "disc_images": [ {<IsoInspection-as-dict>}, ... ],
    "deleted_archive": true/false,
    "errors": [ "..." ]
  }
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths as P
from . import extract
from . import iso as iso_mod


MIN_DISC_IMAGE_SIZE = 1 * 1024 * 1024  # 1 MB; tiny .iso files are usually boot stubs
DISC_IMAGE_EXTS = {".iso", ".img"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_sidecar(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def derived_sidecar_path(dl_sidecar: Path) -> Path:
    """Given a `<filename>.dl.json`, return the `<filename>.derived.json`."""
    return dl_sidecar.with_name(dl_sidecar.name.replace(".dl.json", ".derived.json"))


def extracted_dir_for(dl_sidecar: Path) -> Path:
    """Where the .7z gets extracted to. Lives under EXTRACTED so the archive
    tree stays clean and we can blow away the extraction without losing
    sidecars."""
    rel = dl_sidecar.relative_to(P.ARCHIVES)
    # rel = <product>/<release>/<filename>.dl.json
    parts = rel.parts
    product = parts[0]
    release = parts[1]
    filename = parts[2].removesuffix(".dl.json")
    return P.EXTRACTED / product / release / filename


def find_disc_images(root: Path) -> list[Path]:
    """Walk an extracted-archive directory and return paths of plausible disc
    images. Heuristic only — caller's inspector will reject if a file isn't
    really an ISO."""
    out: list[Path] = []
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in DISC_IMAGE_EXTS:
            continue
        try:
            if p.stat().st_size < MIN_DISC_IMAGE_SIZE:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def _resolve_source_file(dl_sidecar: Path) -> tuple[Optional[Path], Optional[str]]:
    """Return (path_to_7z_or_image, error_reason). Honors local_origin from
    seeded sidecars; returns (None, "...") if the source can't be reached."""
    side = _read_sidecar(dl_sidecar)
    if side is None:
        return None, "sidecar missing/unreadable"
    if side.get("source") == "local_match":
        origin = side.get("local_origin")
        if not origin:
            return None, "seeded sidecar missing local_origin"
        p = Path(origin)
        if not p.exists():
            return None, f"local_origin not mounted/accessible: {origin}"
        return p, None
    # Downloaded artifact: same dir as sidecar, name = sidecar name minus .dl.json
    p = dl_sidecar.with_name(dl_sidecar.name.removesuffix(".dl.json"))
    if not p.exists():
        return None, f"archive missing: {p}"
    return p, None


def _media_kind_from_sidecar(dl_sidecar: Path) -> Optional[str]:
    side = _read_sidecar(dl_sidecar) or {}
    # Both flavors of sidecar carry media_kind at top level
    return side.get("media_kind")


def is_optical(dl_sidecar: Path) -> bool:
    return (_media_kind_from_sidecar(dl_sidecar) or "") in {"CD", "DVD"}


def already_processed(dl_sidecar: Path) -> bool:
    """True if a derived sidecar already exists with a final status."""
    d = derived_sidecar_path(dl_sidecar)
    if not d.exists():
        return False
    rec = _read_sidecar(d) or {}
    return rec.get("status") in {"ok", "no_disc_image"}


def process_one(dl_sidecar: Path, *, dry_run: bool = False) -> dict:
    """Extract + inspect + hash a single downloaded archive. Always writes
    a .derived.json on a real run (any status). Returns the sidecar dict."""
    derived_path = derived_sidecar_path(dl_sidecar)
    started_at = _now()

    src, err = _resolve_source_file(dl_sidecar)
    if err is not None:
        rec = {
            "status": "source_unavailable",
            "started_at": started_at,
            "completed_at": _now(),
            "errors": [err],
            "deleted_archive": False,
        }
        if not dry_run:
            _write_atomic_json(derived_path, rec)
        return rec

    is_seeded = (_read_sidecar(dl_sidecar) or {}).get("source") == "local_match"
    out_dir = extracted_dir_for(dl_sidecar)
    if not dry_run:
        # Clean any prior partial extraction
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        return {
            "status": "would_process",
            "src": str(src),
            "out": str(out_dir),
            "seeded": is_seeded,
        }

    extract_res = extract.extract(src, out_dir)
    rec: dict[str, Any] = {
        "started_at": started_at,
        "completed_at": _now(),
        "extracted_dir": str(out_dir),
        "extract": {
            "ok": extract_res.ok,
            "exit_code": extract_res.exit_code,
            "binary": extract_res.binary,
            "stdout_tail": extract_res.stdout_tail,
            "stderr_tail": extract_res.stderr_tail,
        },
        "disc_images": [],
        "errors": [],
        "deleted_archive": False,
    }

    if not extract_res.ok:
        rec["status"] = "extract_failed"
        rec["errors"].append(f"7z exit {extract_res.exit_code}")
        _write_atomic_json(derived_path, rec)
        return rec

    disc_paths = find_disc_images(out_dir)
    if not disc_paths:
        rec["status"] = "no_disc_image"
        rec["errors"].append("no .iso/.img >=1MB inside archive")
        _write_atomic_json(derived_path, rec)
        return rec

    any_clean = False
    for dp in disc_paths:
        insp = iso_mod.inspect_iso(dp)
        rec["disc_images"].append(asdict(insp))
        # "Clean" = we successfully fingerprinted the contents.
        # Walk-fallback warnings still land in insp.errors but don't disqualify
        # the disc; the real signal is that we computed a filetree_sha256.
        if insp.filetree_sha256:
            any_clean = True

    if any_clean:
        rec["status"] = "ok"
        # Delete the .7z on full success — but never delete a user's local-origin file
        if not is_seeded and src.suffix.lower() == ".7z":
            try:
                src.unlink()
                rec["deleted_archive"] = True
            except OSError as exc:
                rec["errors"].append(f"could not delete .7z: {exc!r}")
    else:
        rec["status"] = "inspect_failed"
        rec["errors"].append("every disc image had inspection errors")

    rec["completed_at"] = _now()
    _write_atomic_json(derived_path, rec)
    return rec


def iter_dl_sidecars() -> Iterable[Path]:
    """Yield every <filename>.dl.json under archives/."""
    if not P.ARCHIVES.exists():
        return
    yield from P.ARCHIVES.glob("*/*/*.dl.json")


def is_download_ok(dl_sidecar: Path) -> bool:
    rec = _read_sidecar(dl_sidecar) or {}
    return rec.get("result", {}).get("status") == "ok"
