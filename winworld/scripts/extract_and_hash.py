#!/usr/bin/env -S uv run python
"""Walk archives/ for completed downloads, extract + inspect + hash each disc.

Targets:
  - .dl.json with result.status == "ok"
  - media_kind in {CD, DVD} (optical only; floppies/archives need different parsers)
  - no .derived.json yet (or one with non-final status)

For each: extracts the .7z to extracted/, inspects every .iso inside, writes
.derived.json sidecar, and deletes the .7z on full success (unless the entry
was seeded from a local file, which we never delete).

  uv run winworld/scripts/extract_and_hash.py             # process all eligible
  uv run winworld/scripts/extract_and_hash.py --limit 5
  uv run winworld/scripts/extract_and_hash.py --dry-run
  uv run winworld/scripts/extract_and_hash.py --product windows-95
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402
from ode_winworld import derived as D                                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="WinWorld extract + hash pipeline (optical only)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N archives (smoke testing)")
    ap.add_argument("--product", default=None,
                    help="Only process downloads under this product slug")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be processed, don't extract")
    ap.add_argument("--media", nargs="+", default=["CD", "DVD"],
                    help="Media kinds to include (default: CD DVD)")
    args = ap.parse_args()

    P.ensure_dirs()
    media_allow = set(args.media)

    queue: list[Path] = []
    for dl in D.iter_dl_sidecars():
        if not D.is_download_ok(dl):
            continue
        kind = D._media_kind_from_sidecar(dl)
        if kind not in media_allow:
            continue
        if args.product and dl.parts[-3] != args.product:
            continue
        if D.already_processed(dl):
            continue
        queue.append(dl)

    queue.sort()
    if args.limit is not None:
        queue = queue[: args.limit]

    print(f"[extract] queue: {len(queue)}  media={sorted(media_allow)}  "
          f"dry_run={args.dry_run}")
    if not queue:
        return 0

    ok = 0
    failed = 0
    skipped = 0
    t0 = time.monotonic()

    for i, dl in enumerate(queue, 1):
        # Trim path: archives/<product>/<release>/<file>.dl.json
        parts = dl.parts
        label = "/".join(parts[-3:-1]) + "/" + parts[-1].removesuffix(".dl.json")
        print(f"  {i}/{len(queue)}  {label}", flush=True)
        try:
            rec = D.process_one(dl, dry_run=args.dry_run)
        except Exception as exc:                                          # noqa: BLE001
            failed += 1
            print(f"    ! exception: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        status = rec.get("status", "?")
        n_imgs = len(rec.get("disc_images", []))
        if status == "ok":
            ok += 1
            sizes = sum(int(d.get("size_bytes", 0)) for d in rec["disc_images"])
            deleted = "deleted .7z" if rec.get("deleted_archive") else "kept source"
            print(f"    ok  images={n_imgs}  {sizes/1e6:.1f}MB  {deleted}")
        elif status == "no_disc_image":
            skipped += 1
            print("    skipped: no disc image in archive")
        elif status == "source_unavailable":
            skipped += 1
            print(f"    skipped: source not accessible "
                  f"({rec.get('errors', ['?'])[0]})")
        elif status == "would_process":
            print(f"    would extract {rec.get('src')} -> {rec.get('out')}")
        else:
            failed += 1
            print(f"    ! status={status} errors={rec.get('errors')}")

    elapsed = time.monotonic() - t0
    print(f"[extract] done. ok={ok} skipped={skipped} failed={failed} "
          f"elapsed={elapsed/60:.1f}min")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
