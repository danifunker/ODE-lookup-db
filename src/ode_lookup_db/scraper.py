"""High-level scraping orchestration.

Discovers new disc IDs from system listing pages, fetches each one, and parses
into schema-conformant rows. Rate limiting is handled by `RedumpClient`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .http_client import RedumpClient
from .parser import ParseError, extract_disc_ids_from_system_page, parse_disc_page

log = logging.getLogger(__name__)

# Redump system slugs we scrape. Keep this list authoritative and short.
SYSTEM_SLUGS: dict[str, str] = {
    "pc": "pc",
    "mac": "mac",
    "hybrid": "hybrid",
}


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
