"""Archive download phase.

WinWorld enforces a per-IP daily quota (default 25/day visible on landing page).
This module is built around that: each run pulls up to N files and exits cleanly,
designed to be scheduled daily (cron, launchd, etc).

Layout:
  archives/<product>/<release>/<filename>           — the artifact bytes
  archives/<product>/<release>/<filename>.dl.json    — sidecar (status, mirror, hash, timings)
  archives/<product>/<release>/<filename>.part       — in-progress download (range-resume target)
  archives/<product>/<release>/<download_id>.landing.html  — mirror selection page
  archives/<product>/<release>/<download_id>.landing.json  — mirror list extracted

Resume model:
  - Sidecar status="ok"            → fully done, skip
  - Sidecar status="hash_mismatch" → skip (don't waste quota), needs manual look
  - Sidecar status="quota"         → from a prior run; eligible to retry next day
  - .part file present, no final  → range-resume
  - No sidecar, no .part           → fresh download

Quota detection: any response containing "limit" / "quota" / "exceeded" in a small
HTML body (i.e. non-binary content where we expected a binary), or HTTP 429.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import httpx
from selectolax.parser import HTMLParser

from . import BASE_URL, USER_AGENT
from . import paths as P
from .fetch import RateLimiter


DEFAULT_DAILY_CAP = 25
DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0)
DEFAULT_RPS = float(os.environ.get("WINWORLD_DOWNLOAD_RPS", "1.0"))
_LIMITER = RateLimiter(DEFAULT_RPS)

# Priority ordering. Earlier = higher.
MEDIA_PRIORITY = [
    "CD",
    "DVD",
    "DVD-DL",
    "DVD-9",
    "3½ Floppy",
    "5¼ Floppy",
    "8 Floppy",
    "Archive",
    "Document",
    "Tape",
    "Virtual PC",
    "VMware",
]
MEDIA_SKIP: set[str] = set()  # nothing skipped — full archive snapshot is the goal

# Default product-slug substring priorities. Used as a tiebreaker WITHIN a media
# tier (e.g. all CDs are pulled before any DVDs, but Windows CDs come before
# other CDs). Earlier = higher. Substrings, case-insensitive.
DEFAULT_PRODUCT_PRIORITY = [
    "windows-95",
    "windows-98",
    "windows-me",
    "windows-nt",
    "windows-xp",
    "windows-2000",
    "windows-longhorn",
    "windows-",     # any remaining windows-*
    "ms-dos",
    "mac-os",
    "microsoft-office",
    "microsoft-",   # any other Microsoft product
]

QUOTA_MARKERS = re.compile(r"(daily limit|quota|exceeded|too many requests)", re.I)


@dataclass
class QueueItem:
    product_slug: str
    release_slug: str
    download_id: str
    download_url: str           # /download/<uuid>
    filename: str
    media_kind: str
    language: str
    architecture: Optional[str]
    file_size_text: str
    archive_hash: Optional[str]
    archive_hash_alg: Optional[str]
    priority: int               # media tier; smaller = sooner
    product_priority: int = 0   # within-tier product preference; smaller = sooner


@dataclass
class DownloadResult:
    status: str                 # ok | hash_mismatch | quota | http_error | exception | skipped
    bytes_written: int = 0
    elapsed_ms: int = 0
    mirror_id: Optional[str] = None
    mirror_url: Optional[str] = None
    actual_hash: Optional[str] = None
    expected_hash: Optional[str] = None
    hash_alg: Optional[str] = None
    http_status: Optional[int] = None
    detail: Optional[str] = None
    completed_at: str = ""


# ───────────────────────────────────────────── queue construction


def _media_priority(kind: str) -> int:
    try:
        return MEDIA_PRIORITY.index(kind)
    except ValueError:
        return len(MEDIA_PRIORITY) + 1  # unknown sinks below all known


def _product_priority(slug: str, patterns: list[str]) -> int:
    """Index of the first pattern matched as a substring (case-insensitive),
    else len(patterns) so unmatched products sink to the bottom of the tier."""
    s = slug.lower()
    for i, p in enumerate(patterns):
        if p and p.lower() in s:
            return i
    return len(patterns)


def _iter_inventory_records() -> Iterable[dict]:
    """Prefer assembled winworld.jsonl; fall back to per-release parse.json files
    so the downloader doesn't require an assemble step before running."""
    if P.WINWORLD_JSONL.exists():
        with P.WINWORLD_JSONL.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        return
    if not P.RELEASE_PAGES.exists():
        return
    for pp in sorted(P.RELEASE_PAGES.glob("*/*.parse.json")):
        try:
            yield json.loads(pp.read_text())
        except (OSError, json.JSONDecodeError):
            continue


def iter_queue_from_jsonl(
    jsonl_path: Path,
    *,
    languages: set[str] | None = None,
    product_priority: list[str] | None = None,
) -> list[QueueItem]:
    patterns = list(product_priority) if product_priority is not None else list(DEFAULT_PRODUCT_PRIORITY)
    items: list[QueueItem] = []
    for rec in _iter_inventory_records():
        product_slug = rec.get("product_slug") or ""
        prod_pri = _product_priority(product_slug, patterns)
        for d in rec.get("downloads", []):
            media = d.get("media_kind") or ""
            if media in MEDIA_SKIP:
                continue
            lang = d.get("language") or ""
            if languages and lang not in languages:
                continue
            items.append(QueueItem(
                product_slug=rec["product_slug"],
                release_slug=rec["release_slug"],
                download_id=d.get("download_id") or "",
                download_url=d.get("download_url") or "",
                filename=d.get("filename") or "",
                media_kind=media,
                language=lang,
                architecture=d.get("architecture"),
                file_size_text=d.get("file_size_text") or "",
                archive_hash=d.get("archive_hash"),
                archive_hash_alg=d.get("archive_hash_alg"),
                priority=_media_priority(media),
                product_priority=prod_pri,
            ))
    items.sort(key=lambda it: (
        it.priority, it.product_priority, it.product_slug, it.release_slug, it.filename
    ))
    return items


# ───────────────────────────────────────────── paths


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-+()\[\] ]", "_", s).strip(" .") or "_"


def archive_dir(item: QueueItem) -> Path:
    d = P.ARCHIVES / _safe_name(item.product_slug) / _safe_name(item.release_slug)
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_path(item: QueueItem) -> Path:
    fn = item.filename or f"{item.download_id}.bin"
    return archive_dir(item) / _safe_name(fn)


def sidecar_path(item: QueueItem) -> Path:
    return artifact_path(item).with_suffix(artifact_path(item).suffix + ".dl.json")


def landing_html_path(item: QueueItem) -> Path:
    return archive_dir(item) / f"{item.download_id}.landing.html"


def landing_meta_path(item: QueueItem) -> Path:
    return archive_dir(item) / f"{item.download_id}.landing.json"


# ───────────────────────────────────────────── mirror resolution


def _parse_mirrors(html: bytes) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    ul = tree.css_first("#mirrorsList")
    out: list[dict[str, str]] = []
    if ul is None:
        return out
    for li in ul.css("li"):
        a = li.css_first("a[href]")
        if not a:
            continue
        href = a.attributes.get("href") or ""
        name = a.text(strip=True)
        # Location is the trailing "(Kansas City, US)" text after the link
        loc_match = re.search(r"\(([^)]+)\)\s*$", li.text())
        out.append({
            "url": BASE_URL + href if href.startswith("/") else href,
            "name": name,
            "location": loc_match.group(1) if loc_match else "",
        })
    return out


def resolve_mirrors(item: QueueItem, client: httpx.Client) -> list[dict[str, str]]:
    """Fetch /download/<uuid>, cache landing HTML, return mirror list."""
    if landing_meta_path(item).exists():
        try:
            cached = json.loads(landing_meta_path(item).read_text())
            if cached.get("mirrors"):
                return cached["mirrors"]
        except json.JSONDecodeError:
            pass
    _LIMITER.wait()
    resp = client.get(item.download_url)
    landing_html_path(item).write_bytes(resp.content)
    mirrors = _parse_mirrors(resp.content) if resp.status_code == 200 else []
    landing_meta_path(item).write_text(json.dumps({
        "url": item.download_url,
        "status": resp.status_code,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mirrors": mirrors,
    }, indent=2, sort_keys=True))
    return mirrors


# ───────────────────────────────────────────── streaming download


def _hash_state(alg: Optional[str]):
    if alg == "sha1":
        return hashlib.sha1()
    if alg == "sha256":
        return hashlib.sha256()
    if alg == "sha512":
        return hashlib.sha512()
    if alg == "md5":
        return hashlib.md5()
    return None


def _looks_like_quota_html(body_head: bytes) -> bool:
    return bool(QUOTA_MARKERS.search(body_head.decode("utf-8", errors="ignore")))


def _stream_to_part(
    client: httpx.Client,
    url: str,
    dest_final: Path,
    expected_hash: Optional[str],
    expected_alg: Optional[str],
) -> DownloadResult:
    part = dest_final.with_suffix(dest_final.suffix + ".part")
    headers: dict[str, str] = {}
    written = part.stat().st_size if part.exists() else 0
    if written > 0:
        headers["Range"] = f"bytes={written}-"

    # Hash needs to be computed over full content; resuming means we can't trust
    # an in-progress hash state. Always hash the final assembled file at the end.
    _LIMITER.wait()
    t0 = time.monotonic()
    with client.stream("GET", url, headers=headers) as resp:
        ct = (resp.headers.get("content-type") or "").lower()
        # If server didn't honor range and content is HTML, probably quota.
        if "html" in ct and resp.status_code != 200:
            body = resp.read()
            if _looks_like_quota_html(body[:8192]):
                return DownloadResult(
                    status="quota", http_status=resp.status_code,
                    detail="HTML response with quota marker",
                )
        if resp.status_code in (200, 206):
            mode = "ab" if (resp.status_code == 206 and written > 0) else "wb"
            if mode == "wb":
                written = 0
            with part.open(mode) as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
        elif resp.status_code == 429:
            return DownloadResult(status="quota", http_status=429, detail="HTTP 429")
        else:
            body_head = resp.read()[:8192]
            if _looks_like_quota_html(body_head):
                return DownloadResult(
                    status="quota", http_status=resp.status_code,
                    detail="non-200 HTML with quota marker",
                )
            return DownloadResult(
                status="http_error", http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
            )

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Verify hash if we have one
    actual = None
    if expected_hash and expected_alg:
        h = _hash_state(expected_alg)
        if h is not None:
            with part.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            actual = h.hexdigest()
            if actual.lower() != expected_hash.lower():
                # Keep the .part for forensics; sidecar marks mismatch.
                return DownloadResult(
                    status="hash_mismatch",
                    bytes_written=written,
                    elapsed_ms=elapsed_ms,
                    actual_hash=actual,
                    expected_hash=expected_hash,
                    hash_alg=expected_alg,
                )

    # Atomic finalize
    part.replace(dest_final)
    return DownloadResult(
        status="ok",
        bytes_written=written,
        elapsed_ms=elapsed_ms,
        actual_hash=actual,
        expected_hash=expected_hash,
        hash_alg=expected_alg,
    )


def is_done(item: QueueItem) -> bool:
    side = sidecar_path(item)
    if not side.exists():
        return False
    try:
        data = json.loads(side.read_text())
    except json.JSONDecodeError:
        return False
    # Sidecar status lives under "result.status" (both download.write_sidecar
    # and seed_from_local.py write it nested).
    return data.get("result", {}).get("status") == "ok"


def download_one(item: QueueItem, client: httpx.Client) -> DownloadResult:
    if is_done(item):
        return DownloadResult(status="skipped", detail="already downloaded")

    try:
        mirrors = resolve_mirrors(item, client)
    except httpx.HTTPError as e:
        return DownloadResult(status="exception", detail=f"mirror resolve: {e!r}")

    if not mirrors:
        return DownloadResult(status="http_error", detail="no mirrors found on landing page")

    final = artifact_path(item)
    last: Optional[DownloadResult] = None
    for mirror in mirrors:
        try:
            res = _stream_to_part(
                client, mirror["url"], final,
                item.archive_hash, item.archive_hash_alg,
            )
        except httpx.HTTPError as e:
            res = DownloadResult(status="exception", detail=f"stream: {e!r}")
        res.mirror_id = mirror.get("name")
        res.mirror_url = mirror.get("url")
        res.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        last = res
        if res.status in ("ok", "hash_mismatch", "quota"):
            break  # Don't try other mirrors on terminal states
        # else: http_error / exception → try next mirror
    return last or DownloadResult(status="exception", detail="no result")


def write_sidecar(item: QueueItem, res: DownloadResult) -> None:
    side = sidecar_path(item)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps({
        **asdict(item),
        "result": asdict(res),
    }, indent=2, sort_keys=True))


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
    )
