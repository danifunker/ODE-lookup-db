"""Discovery: walk winworldpc.com/library, find products, find releases.

Writes:
  raw/pages/listings/<category>-page-001.html (+ .fetch.json)
  raw/pages/product/<slug>.html               (+ .fetch.json)
  data/discovery.json                         (resume cache, derivable)

Output schema (discovery.json):
  {
    "generated_at": "...",
    "categories": {
      "operating-systems": ["windows-95", "mac-os-x", ...],
      ...
    },
    "products": {
      "windows-95": {
        "category": "operating-systems",
        "url": "https://winworldpc.com/product/windows-95",
        "releases": ["chicago", "95-demo", "rtm", "osr-1", ...]
      },
      ...
    }
  }
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Iterable

import httpx
from selectolax.parser import HTMLParser

from . import BASE_URL
from .fetch import fetch, make_client
from . import paths as P


CATEGORIES = {
    "operating-systems": "/library/operating-systems",
    "applications": "/library/applications",
    "games": "/library/games",
    "dev": "/library/dev",
    "sys": "/library/sys",
}

PRODUCT_HREF = re.compile(r"^/product/([a-z0-9][a-z0-9\-]*)/?$")
RELEASE_HREF = re.compile(r"^/product/([a-z0-9][a-z0-9\-]*)/([a-z0-9][a-z0-9\-]*)/?$")
PAGE_HREF = re.compile(r"\?page=(\d+)")


def _extract_links(html: bytes, pattern: re.Pattern) -> list[tuple[str, ...]]:
    tree = HTMLParser(html)
    out: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        m = pattern.match(href)
        if not m:
            continue
        key = m.groups()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _max_page(html: bytes) -> int:
    """Scan pagination block for the highest ?page=N reference."""
    tree = HTMLParser(html)
    pagination = tree.css_first("#libraryPagination")
    if pagination is None:
        return 1
    max_n = 1
    for a in pagination.css("a[href]"):
        href = a.attributes.get("href") or ""
        m = PAGE_HREF.search(href)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def discover_category(category: str, client: httpx.Client, force: bool = False) -> list[str]:
    """Walk a category across all paginated pages, return ordered list of product slugs."""
    base_url = f"{BASE_URL}{CATEGORIES[category]}"
    # Page 1 — first to learn how many pages exist
    html_p = P.listing_html(category, 1)
    side_p = P.listing_fetch(category, 1)
    res = fetch(base_url, html_p, side_p, client=client, force=force)
    if res.status != 200:
        print(f"  ! {category}: HTTP {res.status}", file=sys.stderr)
        return []
    body = html_p.read_bytes()
    total_pages = _max_page(body)
    slugs: list[str] = []
    seen: set[str] = set()
    for (slug,) in _extract_links(body, PRODUCT_HREF):
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    if total_pages > 1:
        print(f"  paginated: {total_pages} pages")
    for page in range(2, total_pages + 1):
        url = f"{base_url}?page={page}"
        html_p = P.listing_html(category, page)
        side_p = P.listing_fetch(category, page)
        res = fetch(url, html_p, side_p, client=client, force=force)
        if res.status != 200:
            print(f"  ! {category} page {page}: HTTP {res.status}", file=sys.stderr)
            continue
        body = html_p.read_bytes()
        for (slug,) in _extract_links(body, PRODUCT_HREF):
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
    return slugs


def discover_product_releases(slug: str, client: httpx.Client, force: bool = False) -> list[str]:
    """Fetch a product page, return list of release slugs."""
    url = f"{BASE_URL}/product/{slug}"
    html_p = P.product_html(slug)
    side_p = P.product_fetch(slug)
    res = fetch(url, html_p, side_p, client=client, force=force)
    if res.status != 200:
        print(f"  ! product/{slug}: HTTP {res.status}", file=sys.stderr)
        return []
    body = html_p.read_bytes()
    releases: list[str] = []
    for prod, rel in _extract_links(body, RELEASE_HREF):
        if prod == slug:
            releases.append(rel)
    return releases


def run(
    only_categories: Iterable[str] | None = None,
    refresh: bool = False,
    limit_products: int | None = None,
) -> dict:
    P.ensure_dirs()
    cats = list(only_categories) if only_categories else list(CATEGORIES.keys())

    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": {},
        "products": {},
    }

    with make_client() as client:
        for cat in cats:
            print(f"[discover] category={cat}")
            slugs = discover_category(cat, client, force=refresh)
            out["categories"][cat] = slugs
            print(f"  found {len(slugs)} products")

        all_products: list[tuple[str, str]] = []  # (category, slug)
        seen_slugs: set[str] = set()
        for cat, slugs in out["categories"].items():
            for s in slugs:
                if s in seen_slugs:
                    continue
                seen_slugs.add(s)
                all_products.append((cat, s))

        if limit_products is not None:
            all_products = all_products[:limit_products]

        total = len(all_products)
        for i, (cat, slug) in enumerate(all_products, 1):
            if i % 25 == 0 or i == total:
                print(f"[discover] product {i}/{total}  {slug}")
            releases = discover_product_releases(slug, client, force=refresh)
            out["products"][slug] = {
                "category": cat,
                "url": f"{BASE_URL}/product/{slug}",
                "releases": releases,
            }

    P.DISCOVERY_JSON.parent.mkdir(parents=True, exist_ok=True)
    P.DISCOVERY_JSON.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"[discover] wrote {P.DISCOVERY_JSON}")
    print(f"[discover] products={len(out['products'])} "
          f"releases={sum(len(p['releases']) for p in out['products'].values())}")
    return out
