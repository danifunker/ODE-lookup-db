#!/usr/bin/env -S uv run python
"""Fold all sidecars under winworld/data/raw/ into committed artifacts.

Writes:
  winworld/data/winworld.jsonl   — one row per release (lean, committed)
  winworld/data/fulllog.json     — fat denormalized records (committed; debugging)
  winworld/data/stats.json       — counts, fetch timings, error tally

The raw tree is the source of truth; this script is a pure fold and can be
rerun at any time. Blow away the JSONL and rebuild — same result.

  uv run winworld/scripts/assemble.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import paths as P                                      # noqa: E402


def _load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _iter_release_parses() -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    if not P.RELEASE_PAGES.exists():
        return out
    for parse_path in sorted(P.RELEASE_PAGES.glob("*/*.parse.json")):
        data = _load_json(parse_path)
        if data is None:
            continue
        out.append((parse_path, data))
    return out


def _fetch_sidecar_for(parse_path: Path) -> dict | None:
    return _load_json(parse_path.with_name(parse_path.name.replace(".parse.json", ".fetch.json")))


def _product_fetch_for(slug: str) -> dict | None:
    return _load_json(P.product_fetch(slug))


def _gather_errors() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not P.ERRORS.exists():
        return out
    for p in sorted(P.ERRORS.glob("*.json")):
        data = _load_json(p)
        if data:
            data["_file"] = p.name
            out.append(data)
    return out


def build_release_row(parse: dict, fetch: dict | None) -> dict:
    """Lean row for winworld.jsonl — structured fields, no raw HTML."""
    return {
        "product_slug": parse["product_slug"],
        "release_slug": parse["release_slug"],
        "url": parse["url"],
        "title": parse["title"],
        "subtitle": parse["subtitle"],
        "info": parse.get("info", {}),
        "available_releases": parse.get("available_releases", []),
        "screenshots": parse.get("screenshots", []),
        "serials": parse.get("serials", []),
        "downloads": parse.get("downloads", []),
        "comments": parse.get("comments", {}),
        "page_meta": parse.get("page_meta", {}),
        "release_notes_text": parse.get("release_notes_text", ""),
        "installation_text": parse.get("installation_text", ""),
        "description_text": parse.get("description_text", ""),
        "fetched_at": (fetch or {}).get("fetched_at"),
        "html_sha256": parse.get("raw", {}).get("html_sha256"),
        "parser_version": parse.get("parser_version"),
    }


def build_fulllog_record(parse: dict, fetch: dict | None) -> dict:
    """Fat record with everything we know about a release, including HTML hash + fetch meta."""
    rec = dict(parse)  # shallow copy
    rec["_fetch"] = fetch
    return rec


def main() -> int:
    P.ensure_dirs()

    discovery = _load_json(P.DISCOVERY_JSON) or {}
    releases = _iter_release_parses()
    errors = _gather_errors()

    # Build jsonl + fulllog
    P.WINWORLD_JSONL.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    fulllog: list[dict] = []
    media_counter: Counter[str] = Counter()
    lang_counter: Counter[str] = Counter()
    arch_counter: Counter[str] = Counter()

    for parse_path, parse in releases:
        fetch_meta = _fetch_sidecar_for(parse_path)
        rows.append(build_release_row(parse, fetch_meta))
        fulllog.append(build_fulllog_record(parse, fetch_meta))
        for d in parse.get("downloads", []):
            if d.get("media_kind"):
                media_counter[d["media_kind"]] += 1
            if d.get("language"):
                lang_counter[d["language"]] += 1
            if d.get("architecture"):
                arch_counter[d["architecture"]] += 1

    with P.WINWORLD_JSONL.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"[assemble] wrote {P.WINWORLD_JSONL}  rows={len(rows)}")

    P.FULLLOG_JSON.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "releases": fulllog,
        "errors": errors,
    }, indent=2, sort_keys=True))
    print(f"[assemble] wrote {P.FULLLOG_JSON}  errors={len(errors)}")

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "discovery": {
            "products": len(discovery.get("products", {})),
            "releases": sum(len(p["releases"]) for p in discovery.get("products", {}).values()),
            "by_category": {k: len(v) for k, v in discovery.get("categories", {}).items()},
        },
        "parsed_releases": len(rows),
        "downloads_total": sum(len(p.get("downloads", [])) for _, p in releases),
        "downloads_by_media": dict(media_counter.most_common()),
        "downloads_by_language": dict(lang_counter.most_common()),
        "downloads_by_architecture": dict(arch_counter.most_common()),
        "errors": len(errors),
    }
    P.STATS_JSON.write_text(json.dumps(stats, indent=2, sort_keys=True))
    print(f"[assemble] wrote {P.STATS_JSON}")
    print(f"  products={stats['discovery']['products']}  "
          f"releases_parsed={stats['parsed_releases']}  "
          f"downloads={stats['downloads_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
