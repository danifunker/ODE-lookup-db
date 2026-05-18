"""Canary: re-parse pinned known-stable disc pages and assert exact expected output.

If redump changes their HTML in a way that breaks parsing, canary fails before
bad data lands in the JSONL.

Populate CANARY_IDS with 3-5 stable, popular discs once we have a working parser
and corresponding expected-output fixtures in tests/fixtures/canary/.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ode_lookup_db.http_client import RedumpClient  # noqa: E402
from ode_lookup_db.parser import parse_disc_page  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "canary"

CANARY_IDS: list[tuple[int, str]] = [
    (133379, "pc"),    # Myst (SoftKey UK) — single data track, PVD present, ring codes
    (99835, "mac"),    # MechWarrior 2 (Mac) — 28 tracks, no PVD, audio-heavy
    (44803, "pc"),     # MechWarrior 2 (PC EU) — multi-dumper, zero-date PVD
    (27832, "pc"),     # Super Street Fighter II Turbo — 45 tracks, no Serial field, has Version
    (16345, "pc"),     # American McGee's Alice — 5 languages, multi-ring, Comments w/ HTML
    (92225, "pc"),     # 007 Legends — DVD-9, 6-col track table, has Layerbreak
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    log = logging.getLogger("canary")

    if not CANARY_IDS:
        log.warning("canary list is empty — skipping (populate CANARY_IDS to enable)")
        return 0

    failures: list[str] = []
    with RedumpClient() as client:
        for rid, system in CANARY_IDS:
            expected_path = FIXTURES / f"{rid}.expected.json"
            if not expected_path.exists():
                failures.append(f"missing fixture: {expected_path}")
                continue
            expected = json.loads(expected_path.read_text())
            resp = client.get_disc(rid)
            actual = parse_disc_page(resp.text, redump_id=rid, system=system)
            # Drop scraped_at; it's expected to vary.
            actual.pop("scraped_at", None)
            expected.pop("scraped_at", None)
            if actual != expected:
                failures.append(f"disc {rid}: parsed output differs from fixture")

    if failures:
        for f in failures:
            log.error(f)
        return 1
    log.info("canary OK (%d discs)", len(CANARY_IDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
