#!/usr/bin/env -S uv run python
"""Daily download driver. Pull up to N files (default 25) and exit.

Designed to be scheduled (cron / launchd):

  0 5 * * *  WINWORLD_DATA_DIR=/Volumes/Software/winworld-pc \\
             uv run --project ~/repos/ODE-lookup-db \\
             ~/repos/ODE-lookup-db/winworld/scripts/download.py

Priority order: CD → DVD → 3½ Floppy → 5¼ Floppy → 8 Floppy → Archive.
English only by default. Already-downloaded files are skipped. Hash-mismatches
keep the .part for forensics and don't retry. Quota-detected → exit immediately
so we don't waste IP reputation hammering a blocked endpoint.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402
from ode_winworld import download as DL                                  # noqa: E402


def _humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser(description="WinWorld archive downloader (daily-capped)")
    ap.add_argument("--max", type=int, default=DL.DEFAULT_DAILY_CAP,
                    help=f"Cap downloads per run (default {DL.DEFAULT_DAILY_CAP})")
    ap.add_argument("--languages", nargs="+", default=["English"],
                    help="Languages to include (default English). Pass 'any' to include all.")
    ap.add_argument("--media", nargs="+", default=None,
                    help=f"Media kinds to include. Default = all except {sorted(DL.MEDIA_SKIP)}. "
                         f"Known order: {DL.MEDIA_PRIORITY}")
    ap.add_argument("--products", nargs="+", default=None,
                    help="Restrict to these product slugs")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the would-be queue and exit; no network")
    args = ap.parse_args()

    P.ensure_dirs()

    languages = None if (args.languages and args.languages[0] == "any") else set(args.languages)
    queue = DL.iter_queue_from_jsonl(P.WINWORLD_JSONL, languages=languages)

    if args.media:
        allow = set(args.media)
        queue = [q for q in queue if q.media_kind in allow]
    if args.products:
        allow = set(args.products)
        queue = [q for q in queue if q.product_slug in allow]

    # Filter out already-done
    pending = [q for q in queue if not DL.is_done(q)]

    print(f"[download] queue total={len(queue)}  pending={len(pending)}  cap={args.max}")
    if pending:
        # Show first few for sanity
        for q in pending[:5]:
            print(f"  [{q.priority}] {q.media_kind:12} {q.language:10} "
                  f"{q.product_slug}/{q.release_slug}  {q.filename}")

    if args.dry_run:
        return 0

    if not pending:
        print("[download] nothing to do.")
        return 0

    done_ok = 0
    done_err = 0
    bytes_total = 0
    t0 = time.monotonic()

    with DL.make_client() as client:
        for item in pending:
            if done_ok >= args.max:
                print(f"[download] reached cap ({args.max}). Stopping.")
                break
            print(f"  → {item.product_slug}/{item.release_slug}  {item.filename}  "
                  f"({item.file_size_text})", flush=True)
            res = DL.download_one(item, client)
            DL.write_sidecar(item, res)
            status = res.status
            if status == "ok":
                done_ok += 1
                bytes_total += res.bytes_written
                print(f"    ok  {_humanize_bytes(res.bytes_written)}  "
                      f"{res.elapsed_ms / 1000:.1f}s  via {res.mirror_id or '?'}")
            elif status == "skipped":
                print(f"    skipped")
            elif status == "hash_mismatch":
                done_err += 1
                print(f"    ! hash_mismatch  expected={res.expected_hash[:16]}...  "
                      f"actual={res.actual_hash[:16] if res.actual_hash else '?'}...")
            elif status == "quota":
                print(f"    ! quota: {res.detail}. Exiting cleanly to retry tomorrow.")
                DL.write_sidecar(item, res)
                break
            else:
                done_err += 1
                print(f"    ! {status}: {res.detail}")

    elapsed = time.monotonic() - t0
    print(f"[download] done ok={done_ok} err={done_err} "
          f"bytes={_humanize_bytes(bytes_total)} elapsed={elapsed/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
