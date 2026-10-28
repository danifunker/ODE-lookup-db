"""Canary: re-parse pinned known-stable disc pages against the *live* redump.info site.

Purpose: catch the case where redump.info changes their HTML in a way that breaks the
parser, before bad data lands in the JSONL.

The subtlety: a live page can differ from its stored fixture for two very
different reasons.

  1. The parser broke (redump.info changed their HTML structure). This is what the
     canary exists to catch, and it must fail loudly.
  2. A redump.info editor legitimately edited the disc's metadata (new title, extra
     ring code, re-dump, etc.). The parser is fine; our frozen fixture is just
     stale. This must NOT kill the daily scrape.

We tell them apart by re-running the parser + schema validator on the live page:
  - parse raises / schema validation fails  -> hard fail (exit 1): real break.
  - parses & validates but differs from fixture -> soft warning (exit 0): the
    upstream data was edited. Refresh the fixture with `--update` when convenient.

Use `--strict` to treat *any* diff as a failure (handy for a manual check), and
`--update [--ids 16345,...]` to re-fetch and rewrite the fixtures.

See README.md "Canary fixtures" for the full runbook.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.parser import ParseError, parse_disc_page  # noqa: E402
from ode_lookup_db.validator import validate_rows  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "canary"

CANARY_IDS: list[tuple[int, str]] = [
    (133379, "pc"),    # Myst (SoftKey UK) — single data track, PVD present, ring codes
    (99835, "mac"),    # MechWarrior 2 (Mac) — 28 tracks, no PVD, audio-heavy
    (44803, "pc"),     # MechWarrior 2 (PC EU) — multi-dumper, zero-date PVD
    (27832, "pc"),     # Super Street Fighter II Turbo — 45 tracks, no Serial field, has Version
    (16345, "pc"),     # American McGee's Alice — 5 languages, multi-ring, Comments w/ HTML
    (92225, "pc"),     # 007 Legends — DVD-9, 6-col track table, has Layerbreak
]

log = logging.getLogger("canary")


def _normalize(row: dict) -> dict:
    """Drop fields expected to vary run-to-run before comparing."""
    row = dict(row)
    row.pop("scraped_at", None)
    return row


def _write_fixture(rid: int, html: str, row: dict) -> None:
    """Refresh both the frozen HTML and the expected JSON for a disc.

    Both must move together: tests/test_parser.py parses the frozen .html offline
    and compares to .expected.json, so updating one without the other breaks it.
    """
    (FIXTURES / f"{rid}.html").write_text(html)
    payload = json.dumps(_normalize(row), indent=2, sort_keys=True) + "\n"
    (FIXTURES / f"{rid}.expected.json").write_text(payload)


def _changed_keys(expected: dict, actual: dict) -> list[str]:
    return sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))


def _summary(line: str) -> None:
    """Surface drift in the GitHub Actions step summary, if running in CI."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="re-fetch live pages and rewrite the .html + .expected.json fixtures",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat any difference from the fixture as a failure (exit 1)",
    )
    parser.add_argument(
        "--ids",
        help="comma-separated redump_ids to act on (default: all canary discs)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    if not CANARY_IDS:
        log.warning("canary list is empty — skipping (populate CANARY_IDS to enable)")
        return 0

    wanted: set[int] | None = None
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if x.strip()}

    discs = [(rid, sys_) for rid, sys_ in CANARY_IDS if wanted is None or rid in wanted]

    hard_failures: list[str] = []
    drifted: list[str] = []
    updated: list[int] = []

    with RedumpClient() as client:
        for rid, system in discs:
            expected_path = FIXTURES / f"{rid}.expected.json"
            resp = client.get_disc(rid)

            # Does the live page still parse + validate? That's the real test.
            try:
                actual = _normalize(parse_disc_page(resp.text, redump_id=rid, system=system))
            except ParseError as exc:
                hard_failures.append(f"disc {rid}: parser raised on live page: {exc}")
                continue

            report = validate_rows([actual])
            if not report.ok:
                hard_failures.append(f"disc {rid}: live page fails schema validation: {report.hard_errors}")
                continue

            if args.update:
                _write_fixture(rid, resp.text, actual)
                updated.append(rid)
                continue

            if not expected_path.exists():
                hard_failures.append(f"disc {rid}: missing fixture {expected_path} (run --update)")
                continue

            expected = _normalize(json.loads(expected_path.read_text()))
            if actual == expected:
                continue

            keys = _changed_keys(expected, actual)
            msg = f"disc {rid}: parsed output differs from fixture (changed: {', '.join(keys)})"
            if args.strict:
                hard_failures.append(msg)
            else:
                # Parser is fine; redump.info edited the data. Don't break the run.
                drifted.append(msg)

    if args.update:
        log.info("updated %d fixture(s): %s", len(updated), ", ".join(map(str, updated)) or "(none)")
        return 0

    for msg in drifted:
        log.warning("%s", msg)
        _summary(f"⚠️ canary drift — {msg}")
    if drifted:
        log.warning(
            "%d canary disc(s) drifted but still parse + validate. Refresh with: "
            "uv run scripts/canary.py --update --ids %s",
            len(drifted),
            ",".join(m.split()[1].rstrip(":") for m in drifted),
        )

    if hard_failures:
        for msg in hard_failures:
            log.error("%s", msg)
        return 1

    log.info("canary OK (%d discs, %d drifted)", len(discs), len(drifted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
