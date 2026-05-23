"""Shell out to the system 7z binary for archive extraction.

Auto-detects the binary name: modern `7zz` (Homebrew 7-zip on macOS) or the
older `7z` (p7zip on Linux / macports). On Mac:

    brew install 7-zip       # provides /opt/homebrew/bin/7zz

If neither is on PATH we raise ExtractError at use time.

Why shell out instead of py7zr: speed, format coverage, and we don't want to
debug an in-process .7z parser for the long tail of weird WinWorld archives.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_CANDIDATES = ("7zz", "7z")


class ExtractError(RuntimeError):
    """Raised when the 7z binary is missing or extraction fails."""


@dataclass
class ExtractResult:
    ok: bool
    exit_code: int
    binary: str
    stdout_tail: str
    stderr_tail: str


def find_binary() -> str:
    for name in _CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise ExtractError(
        f"7z binary not found on PATH (tried {_CANDIDATES!r}). "
        "Install with: brew install 7-zip"
    )


def extract(archive: Path, dest_dir: Path, *, timeout: float = 1800.0) -> ExtractResult:
    """Extract `archive` into `dest_dir`. Returns ExtractResult; never raises
    on extraction failure (caller decides what to do)."""
    binary = find_binary()
    dest_dir.mkdir(parents=True, exist_ok=True)
    # -y: assume yes to prompts; -bd: no progress; -o: output dir
    proc = subprocess.run(
        [binary, "x", "-y", "-bd", f"-o{dest_dir}", str(archive)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return ExtractResult(
        ok=(proc.returncode == 0),
        exit_code=proc.returncode,
        binary=binary,
        stdout_tail=(proc.stdout or "")[-2000:],
        stderr_tail=(proc.stderr or "")[-2000:],
    )


def list_archive(archive: Path, *, timeout: float = 120.0) -> Optional[list[dict]]:
    """Run `7z l -slt` and parse it into [{path, size, attr, ...}, ...].
    Returns None if 7z isn't available or the listing failed.
    """
    try:
        binary = find_binary()
    except ExtractError:
        return None
    proc = subprocess.run(
        [binary, "l", "-slt", str(archive)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        return None

    entries: list[dict] = []
    current: dict = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            current[k.strip().lower()] = v.strip()
    if current:
        entries.append(current)
    # First block in -slt output is metadata about the archive itself; filter
    # it out by requiring a "path" key (file entries always have one).
    return [e for e in entries if "path" in e and "size" in e]
