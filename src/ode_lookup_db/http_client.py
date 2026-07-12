"""Polite HTTP client for redump.info.

- Single-host, rate-limited to <=1 req/sec.
- Identifies itself via a custom User-Agent pointing at this repo.
- Retries with exponential backoff on transient errors.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import USER_AGENT

log = logging.getLogger(__name__)

REDUMP_BASE = "https://redump.info"
MIN_INTERVAL_SECONDS = 1.0


class RedumpClient:
    """Thread-safe rate-limited client. Use as a context manager."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        self._client = httpx.Client(
            base_url=REDUMP_BASE,
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        self._min_interval = min_interval
        self._last_request = 0.0
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _throttle(self) -> None:
        """Space requests by exactly `min_interval` from each other's start.

        Anchoring on the *start* of the previous request (rather than its end)
        means redump's response latency doesn't double our interval — if the
        server takes 1s and our interval is 1s, we still get 1 req/s, not 0.5.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
                self._last_request += self._min_interval
            else:
                self._last_request = now

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=2, min=2, max=300),
        stop=stop_after_attempt(10),
        reraise=True,
    )
    def get(self, path: str) -> httpx.Response:
        # Tolerant retries: 10 attempts with exponential backoff capped at 5 min.
        # Max total wait per request: ~25 minutes. A short redump outage rides
        # through transparently; a longer one will eventually raise, and the
        # caller is expected to log+skip rather than crash the run.
        self._throttle()
        log.debug("GET %s", path)
        resp = self._client.get(path)
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    def get_disc(self, redump_id: int) -> httpx.Response:
        # redump.info serves the disc page at the no-trailing-slash path; the
        # trailing-slash form 308-redirects here, so skip the extra hop.
        return self.get(f"/disc/{redump_id}")

    def get_system_page(self, system_slug: str, page: int = 1) -> httpx.Response:
        # redump.info per-system listing, newest-added first:
        #   https://redump.info/discs?system=<SLUG>&sort=added&order=desc&page=N
        return self.get(f"/discs?system={system_slug}&sort=added&order=desc&page={page}")

    def get_added_desc_page(self, page: int = 1) -> httpx.Response:
        # All-systems listing, newest-added first:
        #   https://redump.info/discs?sort=added&order=desc&page=N
        return self.get(f"/discs?sort=added&order=desc&page={page}")
