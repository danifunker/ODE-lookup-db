#!/usr/bin/env -S uv run python
"""Build flat filename/category indexes from current scrape state.

Two outputs (under WINWORLD_DATA_DIR):

  inventory.tsv
    Machine-readable. One row per download.
    Columns: category, product_slug, release_slug, product_title, media_kind,
             language, architecture, file_size_text, archive_hash_alg,
             archive_hash, download_id, filename

  inventory_by_category/<category>.txt
    Human-readable. One line per download, padded for scan/grep:
      [Product Title / Release Name]  MEDIA  LANG  SIZE  FILENAME

Reads from per-release .parse.json files (so it works mid-scrape) and uses
discovery.json for the product -> category mapping.

  uv run winworld/scripts/build_inventory_index.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402


TSV_COLS = [
    "category", "product_slug", "release_slug", "product_title", "release_name",
    "media_kind", "language", "architecture", "file_size_text",
    "archive_hash_alg", "archive_hash", "download_id", "filename",
]


def load_product_to_category() -> dict[str, str]:
    if not P.DISCOVERY_JSON.exists():
        print(f"[index] warning: {P.DISCOVERY_JSON} missing; category will be '?'",
              file=sys.stderr)
        return {}
    disc = json.loads(P.DISCOVERY_JSON.read_text())
    out: dict[str, str] = {}
    for slug, info in disc.get("products", {}).items():
        out[slug] = info.get("category") or "?"
    return out


def iter_parsed_releases() -> list[dict]:
    out: list[dict] = []
    if not P.RELEASE_PAGES.exists():
        return out
    for pp in sorted(P.RELEASE_PAGES.glob("*/*.parse.json")):
        try:
            out.append(json.loads(pp.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def main() -> int:
    P.ensure_dirs()
    cat_of = load_product_to_category()
    releases = iter_parsed_releases()
    print(f"[index] releases: {len(releases)}")

    rows: list[dict] = []
    by_category: dict[str, list[str]] = defaultdict(list)

    for rec in releases:
        product_slug = rec.get("product_slug") or ""
        release_slug = rec.get("release_slug") or ""
        category = cat_of.get(product_slug, "?")
        # Page meta title looks like "WinWorld: Windows 95 OSR 2" -> strip prefix
        page_meta = rec.get("page_meta") or {}
        og_title = page_meta.get("og:title") or page_meta.get("title") or ""
        product_title = rec.get("title") or og_title or product_slug
        release_name = rec.get("subtitle") or release_slug

        for d in rec.get("downloads", []):
            row = {
                "category": category,
                "product_slug": product_slug,
                "release_slug": release_slug,
                "product_title": product_title,
                "release_name": release_name,
                "media_kind": d.get("media_kind") or "",
                "language": d.get("language") or "",
                "architecture": d.get("architecture") or "",
                "file_size_text": d.get("file_size_text") or "",
                "archive_hash_alg": d.get("archive_hash_alg") or "",
                "archive_hash": d.get("archive_hash") or "",
                "download_id": d.get("download_id") or "",
                "filename": d.get("filename") or "",
            }
            rows.append(row)
            human = (
                f"[{product_title} / {release_name}]"
                f"  {row['media_kind']:<12}"
                f"  {row['language']:<12}"
                f"  {row['file_size_text']:<10}"
                f"  {row['filename']}"
            )
            by_category[category].append(human)

    # Write TSV
    tsv_path = P.DATA / "inventory.tsv"
    with tsv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TSV_COLS, delimiter="\t")
        w.writeheader()
        # Sort for stable diffs: category, then product, then release, then filename
        rows.sort(key=lambda r: (r["category"], r["product_slug"], r["release_slug"], r["filename"]))
        for r in rows:
            w.writerow(r)
    print(f"[index] wrote {tsv_path}  rows={len(rows)}")

    # Write per-category human files
    out_dir = P.DATA / "inventory_by_category"
    out_dir.mkdir(parents=True, exist_ok=True)
    for cat, lines in sorted(by_category.items()):
        lines.sort()
        cat_path = out_dir / f"{cat}.txt"
        cat_path.write_text(f"# {cat}  ({len(lines)} downloads)\n" + "\n".join(lines) + "\n")
        print(f"  {cat:24}  {len(lines):>6} downloads  -> {cat_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
