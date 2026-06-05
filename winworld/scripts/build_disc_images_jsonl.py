#!/usr/bin/env -S uv run python
"""Walk WINWORLD_DATA_DIR/archives/**/*.derived.json and emit one JSON line per
inspected disc image to winworld/data/disc_images.jsonl, restricted to the
columns ingested by `winworld_disc_image`.

This consolidated file is committed to the repo so the GH-Actions runner can
build the DB without NAS access. The full .derived.json sidecars (228 MB+,
with per-file hashes) stay on the NAS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import orjson

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ode_lookup_db.db import (                                          # noqa: E402
    WINWORLD_ARCHIVES_DIR,
    WINWORLD_DISC_IMAGES_JSONL,
    _WINWORLD_DISC_IMAGE_COLS,
    read_winworld_disc_images,
)


def main() -> int:
    if not WINWORLD_ARCHIVES_DIR.is_dir():
        print(f"no archives dir at {WINWORLD_ARCHIVES_DIR}; set WINWORLD_DATA_DIR")
        return 1

    # Bypass the JSONL preference by passing a non-existent path; force the
    # walker to actually traverse the NAS sidecars.
    out = WINWORLD_DISC_IMAGES_JSONL
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jsonl.tmp")
    n = 0
    with tmp.open("wb") as f:
        for row in read_winworld_disc_images(jsonl_path=Path("/nonexistent")):
            slim = {c: row.get(c) for c in _WINWORLD_DISC_IMAGE_COLS}
            f.write(orjson.dumps(slim))
            f.write(b"\n")
            n += 1
    tmp.replace(out)
    print(f"wrote {n} disc images -> {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
