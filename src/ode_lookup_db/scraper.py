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


def discover_disc_ids(client: RedumpClient, system: str, max_pages: int = 50) -> list[tuple[int, str]]:
    """Walk the system listing pages and return [(redump_id, system), ...].

    Stops after `max_pages` or when a page yields no new IDs.
    """
    slug = SYSTEM_SLUGS[system]
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    for page in range(1, max_pages + 1):
        resp = client.get_system_page(slug, page=page)
        if resp.status_code != 200:
            log.warning("system %s page %d returned HTTP %d — stopping", system, page, resp.status_code)
            break
        ids = extract_disc_ids_from_system_page(resp.text)
        new = [i for i in ids if i not in seen]
        if not new:
            break
        for rid in new:
            seen.add(rid)
            found.append((rid, system))
    log.info("discovered %d disc IDs for system=%s", len(found), system)
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
