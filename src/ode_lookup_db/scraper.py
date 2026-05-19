"""High-level scraping orchestration.

Discovers new disc IDs from system listing pages, fetches each one, and parses
into schema-conformant rows. Rate limiting is handled by `RedumpClient`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .http_client import RedumpClient
from .parser import (
    ParseError,
    extract_disc_ids_from_system_page,
    extract_rows_from_added_desc_page,
    parse_disc_page,
)

log = logging.getLogger(__name__)

# Redump system slugs we scrape. Keep this list authoritative and short.
SYSTEM_SLUGS: dict[str, str] = {
    "pc": "pc",
    "mac": "mac",
}

# Map raw System-column labels on the added-desc listing to our system slugs.
ADDED_DESC_SYSTEM_MAP: dict[str, str] = {
    "PC": "pc",
    "MAC": "mac",
}


def discover_recent_added(
    client: RedumpClient,
    *,
    systems: set[str],
    checkpoint_id: int | None,
    max_pages: int = 50,
    overlap_pages: int = 1,
) -> tuple[list[tuple[int, str]], int | None]:
    """Walk /discs/sort/added/dir/desc/ newest-first; return (new_targets, new_checkpoint_id).

    `new_targets` are (redump_id, system) tuples for in-scope systems only.
    `new_checkpoint_id` is the topmost disc id on page 1 (across all systems) —
    a stable anchor for the next run. Returns (targets, None) if page 1 was empty.

    Walks until we either:
      - encounter `checkpoint_id` on a page (then walk `overlap_pages` more for safety), or
      - hit `max_pages` (warn: checkpoint may be stale or invalid).
    """
    new_targets: list[tuple[int, str]] = []
    seen: set[int] = set()
    new_checkpoint: int | None = None
    found_checkpoint_on_page: int | None = None

    for page in range(1, max_pages + 1):
        resp = client.get_added_desc_page(page)
        if resp.status_code != 200:
            log.warning("added-desc page %d returned HTTP %d — stopping", page, resp.status_code)
            break
        rows = extract_rows_from_added_desc_page(resp.text)
        if not rows:
            log.info("added-desc page %d: no rows — stopping", page)
            break

        if page == 1:
            new_checkpoint = rows[0][0]

        in_scope_this_page = 0
        hit_checkpoint_here = False
        for rid, label in rows:
            if checkpoint_id is not None and rid == checkpoint_id:
                hit_checkpoint_here = True
                break  # everything below is older than the checkpoint
            if rid in seen:
                continue
            seen.add(rid)
            slug = ADDED_DESC_SYSTEM_MAP.get(label)
            if slug is None or slug not in systems:
                continue
            new_targets.append((rid, slug))
            in_scope_this_page += 1

        log.info("added-desc page %d: %d rows, %d in-scope new%s",
                 page, len(rows), in_scope_this_page,
                 " (checkpoint reached)" if hit_checkpoint_here else "")

        if hit_checkpoint_here:
            found_checkpoint_on_page = page
            break

    # Walk overlap_pages more pages past the checkpoint hit to absorb minor reorders.
    if found_checkpoint_on_page is not None and overlap_pages > 0:
        for page in range(found_checkpoint_on_page + 1,
                          found_checkpoint_on_page + 1 + overlap_pages):
            resp = client.get_added_desc_page(page)
            if resp.status_code != 200:
                break
            rows = extract_rows_from_added_desc_page(resp.text)
            extra = 0
            for rid, label in rows:
                if rid in seen:
                    continue
                seen.add(rid)
                slug = ADDED_DESC_SYSTEM_MAP.get(label)
                if slug is None or slug not in systems:
                    continue
                new_targets.append((rid, slug))
                extra += 1
            log.info("added-desc overlap page %d: %d extra in-scope", page, extra)

    if checkpoint_id is not None and found_checkpoint_on_page is None:
        log.warning("checkpoint id %d not found within %d pages — checkpoint may be stale",
                    checkpoint_id, max_pages)

    log.info("discovered %d new in-scope disc IDs via added-desc (new checkpoint=%s)",
             len(new_targets), new_checkpoint)
    return new_targets, new_checkpoint


def discover_disc_ids(
    client: RedumpClient,
    system: str,
    *,
    max_pages: int = 500,
    known_ids: set[int] | None = None,
    target_unknown: int | None = None,
) -> list[tuple[int, str]]:
    """Walk the system listing pages and return [(redump_id, system), ...].

    Stops as soon as one of these is true:
      - `max_pages` pages have been read,
      - the current page yielded no IDs we haven't already seen *this walk*,
      - `target_unknown` is set and we've found at least that many IDs that
        are not in `known_ids` (i.e. enough new work for the caller).

    `known_ids` (typically the redump_ids already in JSONL) lets the daily
    cron stop after one or two pages: as soon as a page yields no IDs the
    caller would need to fetch, there's nothing more to discover.
    """
    slug = SYSTEM_SLUGS[system]
    known = known_ids or set()
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    unknown_count = 0
    for page in range(1, max_pages + 1):
        resp = client.get_system_page(slug, page=page)
        if resp.status_code != 200:
            log.warning("system %s page %d returned HTTP %d — stopping", system, page, resp.status_code)
            break
        ids = extract_disc_ids_from_system_page(resp.text)
        new_this_walk = [i for i in ids if i not in seen]
        unknown_on_page = sum(1 for i in new_this_walk if i not in known)
        unknown_count += unknown_on_page
        log.info(
            "discovery %s page %d: %d new (%d unknown to JSONL) — totals: %d ids / %d unknown",
            system, page, len(new_this_walk), unknown_on_page,
            len(found) + len(new_this_walk), unknown_count,
        )
        if not new_this_walk:
            break
        for rid in new_this_walk:
            seen.add(rid)
            found.append((rid, system))
        if target_unknown is not None and unknown_count >= target_unknown:
            log.info("discovery %s: reached target_unknown=%d, stopping", system, target_unknown)
            break
    log.info("discovered %d disc IDs for system=%s (%d unknown to JSONL)",
             len(found), system, unknown_count)
    return found


def fetch_and_parse(
    client: RedumpClient,
    targets: list[tuple[int, str]],
) -> Iterator[dict[str, Any]]:
    """Fetch each (redump_id, system) and yield parsed rows. Skips on parse errors with a warning."""
    for rid, system in targets:
        resp = client.get_disc(rid)
        if resp.status_code == 404:
            log.warning("disc %d returned 404 — skipping", rid)
            continue
        if resp.status_code != 200:
            log.warning("disc %d returned HTTP %d — skipping", rid, resp.status_code)
            continue
        try:
            yield parse_disc_page(resp.text, redump_id=rid, system=system)
        except ParseError as e:
            log.warning("disc %d parse failed: %s", rid, e)
