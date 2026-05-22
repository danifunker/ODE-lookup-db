"""Centralized path helpers.

By default everything lives at <repo>/winworld/data/. Override the data tree
with the WINWORLD_DATA_DIR env var — useful for putting raw HTML, archives,
and extracted images on a NAS while keeping the code in the repo.

  export WINWORLD_DATA_DIR=/Volumes/Software/winworld-pc
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WW_ROOT = REPO_ROOT / "winworld"
DATA = Path(os.environ.get("WINWORLD_DATA_DIR") or (WW_ROOT / "data")).resolve()
RAW = DATA / "raw"
PAGES = RAW / "pages"
LISTINGS = PAGES / "listings"
PRODUCT_PAGES = PAGES / "product"
RELEASE_PAGES = PAGES / "release"
SCREENSHOTS = RAW / "screenshots"
ERRORS = RAW / "errors"
ARCHIVES = DATA / "archives"
EXTRACTED = DATA / "extracted"

DISCOVERY_JSON = DATA / "discovery.json"
WINWORLD_JSONL = DATA / "winworld.jsonl"
FULLLOG_JSON = DATA / "fulllog.json"
STATS_JSON = DATA / "stats.json"


def ensure_dirs() -> None:
    for d in (
        LISTINGS,
        PRODUCT_PAGES,
        RELEASE_PAGES,
        SCREENSHOTS,
        ERRORS,
        ARCHIVES,
        EXTRACTED,
    ):
        d.mkdir(parents=True, exist_ok=True)


def listing_html(category: str, page: int) -> Path:
    return LISTINGS / f"{category}-page-{page:03d}.html"


def listing_fetch(category: str, page: int) -> Path:
    return LISTINGS / f"{category}-page-{page:03d}.fetch.json"


def product_html(slug: str) -> Path:
    return PRODUCT_PAGES / f"{slug}.html"


def product_fetch(slug: str) -> Path:
    return PRODUCT_PAGES / f"{slug}.fetch.json"


def product_parse(slug: str) -> Path:
    return PRODUCT_PAGES / f"{slug}.parse.json"


def release_html(product: str, release: str) -> Path:
    d = RELEASE_PAGES / product
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{release}.html"


def release_fetch(product: str, release: str) -> Path:
    return RELEASE_PAGES / product / f"{release}.fetch.json"


def release_parse(product: str, release: str) -> Path:
    return RELEASE_PAGES / product / f"{release}.parse.json"


def screenshot_dir(product: str) -> Path:
    d = SCREENSHOTS / product
    d.mkdir(parents=True, exist_ok=True)
    return d
