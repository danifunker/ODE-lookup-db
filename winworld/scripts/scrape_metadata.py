#!/usr/bin/env -S uv run python
"""Phase 1: discover + fetch + parse all winworld metadata.

Resumable: each step writes a sidecar on disk; reruns skip work already done.

  uv run winworld/scripts/scrape_metadata.py                 # full crawl
  uv run winworld/scripts/scrape_metadata.py --limit 20      # smoke test
  uv run winworld/scripts/scrape_metadata.py --categories operating-systems
  uv run winworld/scripts/scrape_metadata.py --refresh-discovery
  uv run winworld/scripts/scrape_metadata.py --reparse       # only re-run parsers
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make the package importable when running as a script.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "winworld" / "src"))

from ode_winworld import BASE_URL                                       # noqa: E402
from ode_winworld import discover, paths as P                            # noqa: E402
from ode_winworld.fetch import fetch, make_client                       # noqa: E402
from ode_winworld.parse_release import parse_release_file               # noqa: E402


def _log_error(phase: str, key: str, exc: BaseException) -> None:
    P.ERRORS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_key = key.replace("/", "_")[:80]
    path = P.ERRORS / f"{ts}-{phase}-{safe_key}.json"
    path.write_text(json.dumps({
        "phase": phase,
        "key": key,
        "exception_type": type(exc).__name__,
        "exception_msg": str(exc),
        "traceback": traceback.format_exc(),
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2))


def load_or_make_discovery(
    *, refresh: bool, only_cats: list[str] | None, limit_products: int | None
) -> dict:
    if P.DISCOVERY_JSON.exists() and not refresh:
        print(f"[scrape] reusing {P.DISCOVERY_JSON}")
        return json.loads(P.DISCOVERY_JSON.read_text())
    return discover.run(
        only_categories=only_cats,
        refresh=refresh,
        limit_products=limit_products,
    )


def fetch_and_parse_release(client, product: str, release: str, *, reparse: bool) -> None:
    url = f"{BASE_URL}/product/{product}/{release}"
    html_p = P.release_html(product, release)
    fetch_p = P.release_fetch(product, release)
    parse_p = P.release_parse(product, release)

    if not reparse:
        res = fetch(url, html_p, fetch_p, client=client)
        if res.status != 200:
            raise RuntimeError(f"HTTP {res.status} fetching {url}")

    if parse_p.exists() and not reparse:
        return
    if not html_p.exists():
        raise RuntimeError(f"missing html for {product}/{release}")
    parse_release_file(product, release, url, html_p, parse_p)


def main() -> int:
    ap = argparse.ArgumentParser(description="WinWorld metadata scraper")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of products processed (for smoke tests)")
    ap.add_argument("--categories", nargs="+", default=None,
                    help=f"Subset of: {', '.join(discover.CATEGORIES)}")
    ap.add_argument("--refresh-discovery", action="store_true",
                    help="Re-walk library/product index pages")
    ap.add_argument("--reparse", action="store_true",
                    help="Re-run parser on existing cached HTML (no network)")
    ap.add_argument("--products", nargs="+", default=None,
                    help="Process only these product slugs (skip discovery loop)")
    args = ap.parse_args()

    P.ensure_dirs()

    disc = load_or_make_discovery(
        refresh=args.refresh_discovery,
        only_cats=args.categories,
        limit_products=args.limit,
    )

    if args.products:
        products = {p: disc["products"][p] for p in args.products if p in disc["products"]}
    else:
        products = disc["products"]
        if args.limit is not None:
            products = dict(list(products.items())[: args.limit])

    total_releases = sum(len(p["releases"]) for p in products.values())
    print(f"[scrape] products={len(products)} releases={total_releases}")

    done = 0
    errors = 0
    t0 = time.monotonic()

    with make_client() as client:
        for slug, info in products.items():
            for rel in info["releases"]:
                done += 1
                try:
                    fetch_and_parse_release(client, slug, rel, reparse=args.reparse)
                except Exception as exc:                                  # noqa: BLE001
                    errors += 1
                    _log_error("release", f"{slug}/{rel}", exc)
                    print(f"  ! {slug}/{rel}: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
                if done % 25 == 0 or done == total_releases:
                    rate = done / max(time.monotonic() - t0, 0.001)
                    eta = (total_releases - done) / max(rate, 0.001)
                    print(f"[scrape] {done}/{total_releases} releases  "
                          f"errors={errors}  rate={rate:.2f}/s  eta={eta/60:.1f}min")

    print(f"[scrape] done. processed={done} errors={errors} "
          f"elapsed={(time.monotonic() - t0)/60:.1f}min")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
