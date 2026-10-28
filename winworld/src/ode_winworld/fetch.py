"""Polite HTTP fetcher with sidecar capture.

Every successful GET writes two files atomically:
  <path>.html          — the raw response body bytes
  <path>.fetch.json    — sidecar with url, status, headers, sha256, fetched_at, elapsed_ms

If the sidecar already exists and (a) status was 200 and (b) age < ttl_days,
the fetch is skipped — resume is just "does the sidecar exist."
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from . import USER_AGENT


DEFAULT_TTL_DAYS = 365  # raw HTML is basically permanent; only refetch on demand
DEFAULT_RPS = float(os.environ.get("WINWORLD_RPS", "1.0"))
DEFAULT_TIMEOUT = 60.0


@dataclass
class FetchResult:
    url: str
    status: int
    fetched_at: str
    elapsed_ms: int
    sha256: str
    bytes_len: int
    content_type: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    final_url: Optional[str] = None
    from_cache: bool = False


class RateLimiter:
    """Simple token-bucket: one slot every 1/rps seconds, thread-safe."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / max(rps, 0.01)
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_ok - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_ok = now + self._interval


_LIMITER = RateLimiter(DEFAULT_RPS)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def _sidecar_is_fresh(sidecar: Path, ttl_days: int) -> bool:
    if not sidecar.exists():
        return False
    try:
        meta = json.loads(sidecar.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if meta.get("status") != 200:
        return False
    fetched = meta.get("fetched_at")
    if not fetched:
        return False
    try:
        ts = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - ts).days
    return age_days < ttl_days


def fetch(
    url: str,
    html_path: Path,
    sidecar_path: Path,
    *,
    client: Optional[httpx.Client] = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    force: bool = False,
    accept: str = "text/html,application/xhtml+xml,*/*;q=0.8",
) -> FetchResult:
    """Fetch URL, write html + sidecar atomically. Skip if cached and fresh."""
    if not force and _sidecar_is_fresh(sidecar_path, ttl_days):
        meta = json.loads(sidecar_path.read_text())
        return FetchResult(
            url=meta["url"],
            status=meta["status"],
            fetched_at=meta["fetched_at"],
            elapsed_ms=meta.get("elapsed_ms", 0),
            sha256=meta["sha256"],
            bytes_len=meta["bytes_len"],
            content_type=meta.get("content_type", ""),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            final_url=meta.get("final_url"),
            from_cache=True,
        )

    own_client = client is None
    if own_client:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": accept},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    try:
        _LIMITER.wait()
        t0 = time.monotonic()
        resp = client.get(url)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        body = resp.content
        sha = hashlib.sha256(body).hexdigest()

        result = FetchResult(
            url=url,
            status=resp.status_code,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            elapsed_ms=elapsed_ms,
            sha256=sha,
            bytes_len=len(body),
            content_type=resp.headers.get("content-type", ""),
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
            final_url=str(resp.url) if str(resp.url) != url else None,
        )

        if resp.status_code == 200:
            _atomic_write_bytes(html_path, body)

        sidecar = asdict(result)
        sidecar["headers"] = dict(resp.headers)
        sidecar.pop("from_cache", None)
        _atomic_write_json(sidecar_path, sidecar)

        return result
    finally:
        if own_client:
            client.close()


def make_client() -> httpx.Client:
    """Build a reusable client for batch operations."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
    )
