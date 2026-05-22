#!/usr/bin/env -S uv run python
"""Match local files on a search root against WinWorld's download inventory.

WinWorld's published `archive_hash` is the hash of the .7z bytes — so hash
verification only works when the local file IS the original .7z. For local
files already extracted to .iso/.bin/etc, we have no winworld-published hash
to compare against (we'd need to download the .7z, extract, and byte-compare).

So this matcher does:
  pass 1  filename match  — fast (just stat). Both .7z and extracted images.
                            Heuristic only for non-.7z (likely the same disc,
                            but unverified).
  pass 2  hash verify     — opt-in (--verify), .7z files only. The only honest
                            hash-confirmed match we can do.

Output bucket meanings:
  matched_filename       local file's basename matches an inventory filename
  hash_confirmed         .7z file's bytes match the published archive_hash
  hash_mismatch          .7z file with matching name but wrong bytes (suspicious)
  filename_only          non-.7z candidates — heuristic; verification not possible
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402


DISC_EXTS = {".7z", ".iso", ".bin", ".cue", ".img", ".ima", ".zip", ".rar", ".tar", ".gz"}


def _norm(s: str) -> str:
    """Case-insensitive, whitespace-collapsing, punctuation-soft normalization."""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def iter_winworld_downloads(jsonl: Path) -> Iterable[dict]:
    """Yield one dict per winworld download, with product/release context."""
    if jsonl.exists():
        with jsonl.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for d in rec.get("downloads", []):
                    yield {
                        "product_slug": rec["product_slug"],
                        "release_slug": rec["release_slug"],
                        **d,
                    }
        return
    # Fallback: scrape parse.json files directly (jsonl not assembled yet)
    if not P.RELEASE_PAGES.exists():
        return
    for pp in sorted(P.RELEASE_PAGES.glob("*/*.parse.json")):
        try:
            rec = json.loads(pp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for d in rec.get("downloads", []):
            yield {
                "product_slug": rec.get("product_slug"),
                "release_slug": rec.get("release_slug"),
                **d,
            }


def walk_local(root: Path, exclude: set[Path]) -> list[tuple[Path, int]]:
    out: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune excluded subtrees
        dp = Path(dirpath)
        if any(dp == ex or ex in dp.parents for ex in exclude):
            dirnames[:] = []
            continue
        for fn in filenames:
            p = dp / fn
            ext = p.suffix.lower()
            if ext not in DISC_EXTS:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append((p, size))
    return out


def hash_file(path: Path, alg: str, *, chunk: int = 1 << 20) -> str | None:
    h = getattr(hashlib, alg, None)
    if h is None:
        return None
    state = h()
    try:
        with path.open("rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                state.update(buf)
    except OSError:
        return None
    return state.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Match local files to WinWorld inventory")
    ap.add_argument("--root", default="/Volumes/Software", help="Local root to scan")
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="Subdirectory paths to skip (in addition to winworld data dir)")
    ap.add_argument("--out", default=None,
                    help="Output report path (default: <data>/local_match_report.json)")
    ap.add_argument("--verify", action="store_true",
                    help="Hash-verify matched candidates against published archive_hash")
    ap.add_argument("--verify-max-size-mb", type=int, default=2048,
                    help="Skip hash verify on candidates larger than this (default 2048MB)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    exclude = {Path(e).resolve() for e in args.exclude}
    exclude.add(P.DATA.resolve())  # never scan our own archives back into the report

    print(f"[match] inventory: {P.WINWORLD_JSONL if P.WINWORLD_JSONL.exists() else P.RELEASE_PAGES}")
    # Build: name → list[download dicts] (filename can repeat across releases).
    # Also index the winworld filename with the .7z stripped, because almost
    # everything users have locally is the extracted disc image (e.g. "Foo.iso"
    # matches winworld's "Foo.iso.7z"). Treat that as the same identity.
    by_name: dict[str, list[dict]] = defaultdict(list)
    inv_count = 0
    for d in iter_winworld_downloads(P.WINWORLD_JSONL):
        fn = d.get("filename")
        if not fn:
            continue
        by_name[_norm(fn)].append(d)
        if fn.lower().endswith(".7z"):
            by_name[_norm(fn[:-3])].append(d)
        inv_count += 1
    print(f"[match] winworld downloads in inventory: {inv_count}")

    print(f"[match] scanning {root} (exclude={[str(e) for e in exclude]})")
    t0 = time.monotonic()
    local = walk_local(root, exclude)
    print(f"[match] local archive/disc files: {len(local)}  "
          f"({(time.monotonic()-t0):.1f}s)")

    # Pass 1: filename match
    matches: dict[str, list[dict]] = defaultdict(list)
    # key: "<product>/<release>/<filename>" — uniqueness of inventory item
    for path, size in local:
        key = _norm(path.name)
        for d in by_name.get(key, ()):
            inv_key = f"{d['product_slug']}/{d['release_slug']}/{d['filename']}"
            matches[inv_key].append({
                "local_path": str(path),
                "local_size_bytes": size,
                "archive_hash": d.get("archive_hash"),
                "archive_hash_alg": d.get("archive_hash_alg"),
                "media_kind": d.get("media_kind"),
                "language": d.get("language"),
                "filename": d.get("filename"),
            })

    print(f"[match] pass1 inventory items with at least one local candidate: {len(matches)}")

    confirmed: list[dict] = []
    mismatched: list[dict] = []
    filename_only: list[dict] = []

    # Split candidates into "hash-verifiable" (.7z) and "filename-only" (everything else).
    # The published archive_hash is only meaningful for the .7z archive itself.
    verifiable: list[tuple[str, dict]] = []
    for inv_key, cands in matches.items():
        for c in cands:
            if Path(c["local_path"]).suffix.lower() == ".7z":
                verifiable.append((inv_key, c))
            else:
                filename_only.append({**c, "inventory_key": inv_key})

    if args.verify and verifiable:
        max_bytes = args.verify_max_size_mb * 1024 * 1024
        print(f"[match] pass2 hash-verifying {len(verifiable)} .7z candidates "
              f"(limit {args.verify_max_size_mb}MB)")
        for i, (inv_key, c) in enumerate(verifiable, 1):
            expected = c.get("archive_hash")
            alg = c.get("archive_hash_alg")
            if not expected or not alg:
                continue
            if c["local_size_bytes"] > max_bytes:
                continue
            actual = hash_file(Path(c["local_path"]), alg)
            if actual is None:
                continue
            rec = {**c, "inventory_key": inv_key,
                   "expected_hash": expected, "actual_hash": actual}
            (confirmed if actual.lower() == expected.lower() else mismatched).append(rec)
            if i % 10 == 0 or i == len(verifiable):
                print(f"  hashed {i}/{len(verifiable)}  confirmed={len(confirmed)} "
                      f"mismatch={len(mismatched)}")
    elif verifiable:
        print(f"[match] {len(verifiable)} .7z candidates eligible for hash-verify "
              f"(rerun with --verify)")

    out_path = Path(args.out) if args.out else P.DATA / "local_match_report.json"
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "search_root": str(root),
        "winworld_inventory_count": inv_count,
        "local_archive_count": len(local),
        "matched_inventory_items": len(matches),
        "matched_filename": matches,           # all candidates (heuristic)
        "hash_confirmed": confirmed,           # .7z bytes-verified
        "hash_mismatch": mismatched,           # .7z same name, wrong bytes
        "filename_only": filename_only,        # extracted images, can't verify
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"[match] wrote {out_path}")
    print(f"  matches={len(matches)}  confirmed={len(confirmed)}  mismatch={len(mismatched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
