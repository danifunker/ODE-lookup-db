#!/usr/bin/env -S uv run python
"""Seed download sidecars from hash-confirmed local matches.

Reads local_match_report.json (hash_confirmed list) and, for each entry,
writes archives/<product>/<release>/<filename>.dl.json with status=ok and
the original local path recorded in `local_origin`.

We do NOT symlink the local file into the archive tree. Many local copies
live on removable/network volumes (NAS, SDcards) that aren't always mounted,
so symlinks would go dangling. Downstream tools that need the bytes should
read `local_origin` from the sidecar and dereference it themselves, checking
mount status as needed.

Downstream effect: download.py's is_done() returns True for these items, so
they're skipped from the daily quota.

Idempotent: re-running over the same report is fine. Symlinks already in place
are left alone; sidecars are rewritten so provenance stays up to date.

  uv run winworld/scripts/seed_from_local.py
  uv run winworld/scripts/seed_from_local.py --dry-run
  uv run winworld/scripts/seed_from_local.py --report /path/to/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402
from ode_winworld import download as DL                                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed archives/ from local-match report")
    ap.add_argument("--report", default=None,
                    help=f"Path to report (default: {P.DATA}/local_match_report.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show planned actions, don't touch the filesystem")
    args = ap.parse_args()

    report_path = Path(args.report) if args.report else P.DATA / "local_match_report.json"
    if not report_path.exists():
        print(f"[seed] no report at {report_path}", file=sys.stderr)
        return 2

    report = json.loads(report_path.read_text())
    confirmed = report.get("hash_confirmed", [])
    print(f"[seed] hash_confirmed entries: {len(confirmed)}")
    if not confirmed:
        print("[seed] nothing to seed.")
        return 0

    sidecared = 0
    skipped = 0
    for c in confirmed:
        inv_key = c.get("inventory_key", "")
        try:
            product_slug, release_slug, filename = inv_key.split("/", 2)
        except ValueError:
            print(f"  ! malformed inventory_key: {inv_key!r}", file=sys.stderr)
            continue

        # Reconstruct a QueueItem just for path generation
        item = DL.QueueItem(
            product_slug=product_slug,
            release_slug=release_slug,
            download_id="",
            download_url="",
            filename=filename,
            media_kind=c.get("media_kind") or "",
            language=c.get("language") or "",
            architecture=None,
            file_size_text="",
            archive_hash=c.get("expected_hash"),
            archive_hash_alg=c.get("archive_hash_alg"),
            priority=0,
        )
        side = DL.sidecar_path(item)
        local = Path(c["local_path"])

        print(f"  {inv_key}")
        print(f"    local   : {local}")
        print(f"    sidecar : {side}")

        if args.dry_run:
            continue

        # Skip rewriting if a sidecar is already there pointing at the same path.
        if side.exists():
            try:
                prior = json.loads(side.read_text())
                if (prior.get("source") == "local_match"
                        and prior.get("local_origin") == str(local)
                        and prior.get("result", {}).get("status") == "ok"):
                    skipped += 1
                    continue
            except json.JSONDecodeError:
                pass

        side.parent.mkdir(parents=True, exist_ok=True)
        # Sidecar
        res = DL.DownloadResult(
            status="ok",
            bytes_written=c.get("local_size_bytes", 0),
            elapsed_ms=0,
            mirror_id=None,
            mirror_url=None,
            actual_hash=c.get("actual_hash"),
            expected_hash=c.get("expected_hash"),
            hash_alg=c.get("archive_hash_alg"),
            http_status=None,
            detail="seeded from local hash-confirmed match",
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        side.write_text(json.dumps({
            "product_slug": product_slug,
            "release_slug": release_slug,
            "filename": filename,
            "source": "local_match",
            "local_origin": str(local),
            "media_kind": c.get("media_kind"),
            "language": c.get("language"),
            "result": {
                "status": res.status,
                "bytes_written": res.bytes_written,
                "elapsed_ms": res.elapsed_ms,
                "actual_hash": res.actual_hash,
                "expected_hash": res.expected_hash,
                "hash_alg": res.hash_alg,
                "detail": res.detail,
                "completed_at": res.completed_at,
            },
        }, indent=2, sort_keys=True))
        sidecared += 1

    print(f"[seed] sidecars_written={sidecared} skipped_existing={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
