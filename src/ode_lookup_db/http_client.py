"""Polite HTTP client for redump.org.

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

REDUMP_BASE = "http://redump.org"
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
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def get(self, path: str) -> httpx.Response:
        self._throttle()
        log.debug("GET %s", path)
        resp = self._client.get(path)
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    def get_disc(self, redump_id: int) -> httpx.Response:
        return self.get(f"/disc/{redump_id}/")

    def get_system_page(self, system_slug: str, page: int = 1) -> httpx.Response:
        # Redump system listings: http://redump.org/discs/system/<slug>/?page=N
        return self.get(f"/discs/system/{system_slug}/?page={page}")
