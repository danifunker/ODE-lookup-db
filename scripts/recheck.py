"""Process user-filed recheck requests.

Reads open GitHub issues with the `disc-recheck` label (via `gh` CLI), extracts
the redump_id from each, re-fetches the disc page, replaces the row in JSONL,
and closes the issue.

Rate-limited to MAX_RECHECKS_PER_DAY across all callers. Issues over the budget
are commented on and left open for the next run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.db import read_jsonl, write_jsonl  # noqa: E402
from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.scraper import fetch_and_parse  # noqa: E402

DEFAULT_MAX = int(os.environ.get("MAX_RECHECKS_PER_DAY", "30"))
LABEL = "disc-recheck"
ID_RE = re.compile(r"redump[_\s-]*id[^0-9]*([0-9]+)", re.IGNORECASE)

log = logging.getLogger("recheck")


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def list_open_recheck_issues() -> list[dict]:
    raw = gh(
        "issue", "list",
        "--label", LABEL,
        "--state", "open",
        "--json", "number,title,body,createdAt",
        "--limit", "200",
    )
    issues = json.loads(raw)
    issues.sort(key=lambda i: i["createdAt"])  # oldest first
    return issues


def extract_redump_id(issue: dict) -> int | None:
    for text in (issue.get("title") or "", issue.get("body") or ""):
        m = ID_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=DEFAULT_MAX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    issues = list_open_recheck_issues()
    log.info("%d open recheck issues", len(issues))

    to_process: list[tuple[int, dict]] = []
    overflow: list[dict] = []
    seen_ids: set[int] = set()
    for issue in issues:
        rid = extract_redump_id(issue)
        if rid is None:
            log.warning("issue #%s: no redump_id found — skipping", issue["number"])
            continue
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        if len(to_process) < args.max:
            to_process.append((rid, issue))
        else:
            overflow.append(issue)

    rows_by_id = {r["redump_id"]: r for r in read_jsonl()}

    with RedumpClient() as client:
        # We need to know each disc's system. Preserve from existing row if present;
        # otherwise default to whatever the issue body says (TODO: parse system hint).
        targets: list[tuple[int, str]] = []
        for rid, _ in to_process:
            system = rows_by_id.get(rid, {}).get("system", "pc")
            targets.append((rid, system))
        refreshed = list(fetch_and_parse(client, targets))

    for r in refreshed:
        rows_by_id[r["redump_id"]] = r

    if not args.dry_run:
        write_jsonl(rows_by_id.values())

    refreshed_ids = {r["redump_id"] for r in refreshed}
    for rid, issue in to_process:
        if rid in refreshed_ids:
            if not args.dry_run:
                subprocess.run(
                    ["gh", "issue", "close", str(issue["number"]),
                     "-c", f"Re-scraped redump_id {rid} and updated the database."],
                    check=False,
                )
        else:
            log.warning("disc %d failed to re-fetch — leaving issue #%s open", rid, issue["number"])

    for issue in overflow:
        if not args.dry_run:
            subprocess.run(
                ["gh", "issue", "comment", str(issue["number"]),
                 "-b", f"Queued: daily recheck budget ({args.max}) reached. Will retry in next run."],
                check=False,
            )

    log.info("processed %d / overflow %d", len(refreshed), len(overflow))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
