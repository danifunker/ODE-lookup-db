# CLAUDE.md — context for future sessions

This repo is the **source of truth for the ODE lookup database**. It ships a unified SQLite (`data/ode-lookup.sqlite`) built from multiple per-source JSONLs:

- `data/redump.jsonl` — **redump.info** disc metadata (tables `redump_*`)
- `winworld/data/winworld.jsonl` — winworldpc.com archive metadata (tables `winworld_*`)

**Source site (2026-07):** migrated from the retired `redump.org` to **`redump.info`** (relaunched mid-2026). Disc IDs are identical/stable across both sites, so the migration is a parser + config change, **not** a re-key. Disc pages live at `https://redump.info/disc/<id>` (no trailing slash); discovery walks `https://redump.info/discs?sort=added&order=desc&page=N`; per-system filter is `?system=PC|MAC`. The new HTML is a full rewrite (Pico/htmx), so the redump parser was rewritten for schema **v3** — see decision #7.

Consumed primarily by `ODE-artwork-downloader`. See `MIGRATION-unified-db.md` for the consumer migration notes (table renames + new file name).

## Resolved design decisions (do not relitigate)

1. **SQLite is release-only.** `data/ode-lookup.sqlite` is built from the per-source JSONLs during the workflow and attached to a GitHub Release (`db-YYYY-MM-DD` plus a moving `latest` tag). It is **not** committed; `.gitignore` excludes it.
2. **No *recurring* scheduled full refresh.** The daily cron only discovers and fetches *new* disc IDs. Updates to existing rows happen via user-filed GitHub issues with the `disc-recheck` label (template in `.github/ISSUE_TEMPLATE/disc-recheck.yml`). **Exception (one-time):** the redump.info/schema-v3 migration required a single full re-scrape of every existing row via `uv run scripts/scrape.py --backfill-all` (resumable; skips rows already at the current schema version). During backfill, an id that now 404s on redump.info (renumbered/deleted upstream) is **dropped** from the JSONL rather than left as a version-mismatched tombstone — the validator's row-count shrink warning (decision #5) surfaces the net removals. A parity audit (HEAD every known id) found only a tiny fraction gone. This is a one-off, not a new recurring policy — daily stays new-IDs-only afterward.
3. **Recheck rate limit: 30 per UTC day** (env var `MAX_RECHECKS_PER_DAY`). Overflow issues get a queued comment, not a manual approval gate. The user explicitly does not want approval friction.
4. **No `page_sha256` field.** Without scheduled refreshes there is nothing to compare against.
5. **Row-count shrink is a soft warning, not a hard failure.** Legitimate upstream deletions are expected to be rare; we log an issue but still commit.
6. **Dedup on `redump_id` only.** Each disc has exactly one canonical redump_id and appears in exactly one system bucket (`pc` or `mac`). There is no separate "hybrid" listing on redump — that was a mistake in the original brief.
7. **Schema versioning is per-source.** Each source row in `meta` carries its own `schema_version` (**redump=3**, winworld=1). Adding optional fields is fine; renames/removals require a new major version for that source. `meta` shape: `(source TEXT PK, schema_version, built_at, source_commit, row_count)`. **redump v3 (breaking, redump.info):** redump.info splits tracks and files into separate tables, so the row shape follows suit — `tracks[]` now carries physical layout only (`number, type, pregap, length, sectors`, and is **empty for DVDs**), and per-file hashes move to a new required `files[]` array (`filename, size_bytes, crc32, md5, sha1`). SQLite gains a `redump_file` table (hash indexes moved off `redump_track`); `cuesheet_sha1` is now populated from the `.cue` file. `catalog` is no longer extracted (redump.info doesn't expose it cleanly) — the column/field remain but go null on re-scrape.
8. **DB is optical-only.** This database powers Optical Disc Emulator (ODE) lookups, so the WinWorld build step filters `winworld_download` to `media_kind IN ('CD','DVD')` and skips any release/product with no optical downloads. The JSONL source-of-truth keeps **everything** (floppies, tapes, archives, manuals, VM images) so we can revisit this without re-scraping. The filter lives in `WINWORLD_OPTICAL_MEDIA` in `src/ode_lookup_db/db.py`.
8. **Languages**: store both ISO 639-1 code (`languages`) and the raw redump string (`languages_raw`). Unmapped → `"zz"`, with a log warning so we can extend `LANGUAGE_MAP` in `src/ode_lookup_db/languages.py`.
9. **User-Agent**: `ODE-lookup-db/1.0 (+https://github.com/danifunker/ODE-lookup-db)`. Owner is `danifunker`.
10. **Stack**: Python 3.12, `uv` for deps, `httpx` + `selectolax` for scraping, `jsonschema` for validation, stdlib `sqlite3`. No scraping framework — single-host, 1 req/sec, hand-written.

## Out of scope (do not add without explicit ask)

- Console systems (PSX, Saturn, etc.)
- Audio CDs (those go to MusicBrainz in the consumer)
- DVD-Video / BD-Video
- Artwork archival plumbing — the `artwork` field is **reserved** in the schema but not populated. Don't build storage yet.
- Public API / hosted query endpoint — consumers query SQLite directly.
- Web UI

## Repository layout

```
data/redump.jsonl                # redump source of truth (committed)
data/stats.json                  # redump row counts, by-system tallies, known IDs
data/ode-lookup.sqlite           # unified release artifact (gitignored, built per release)
schema/                          # JSON Schema + human docs (redump)
src/ode_lookup_db/               # importable modules: db.py, parser.py, scraper.py, ...
scripts/                         # CLI entry points for redump (scrape.py, build_sqlite.py, validate.py, ...)
tests/                           # pytest; tests/fixtures/ holds cached HTML
.github/workflows/               # daily.yml, validate-pr.yml
.github/ISSUE_TEMPLATE/disc-recheck.yml

winworld/                        # winworldpc.com subtree
  src/ode_winworld/              # fetch/parse/download modules
  scripts/                       # scrape_metadata, assemble, download, match_local,
                                 #   seed_from_local, build_inventory_index
  data/winworld.jsonl            # winworld source of truth (committed when assembled locally;
                                 #   primary copy usually lives under WINWORLD_DATA_DIR=NAS)
  data/raw/                      # gitignored: HTML cache, fetch sidecars, screenshots
  data/archives/                 # gitignored: downloaded .7z + .dl.json sidecars
```

`WINWORLD_DATA_DIR` env var overrides `winworld/data/` so heavy artifacts (raw HTML, archives, extracted images) can live on a NAS while the code stays in the repo.

## Known TODOs

- **`recheck.py`**: system-hint parsing from the issue body is a TODO; currently defaults to the existing row's system, or `"pc"` if unknown.
- **PVD `volume_identifier` / `system_identifier`**: not extracted — the disc page doesn't expose them cleanly. Sometimes appears in `gamecomments` as `<b>Volume Label</b>: XXX`. Leaving null until we have a use case.
- **Initial seed run**: the daily cron is off until we've done a seed scrape and confirmed the pipeline end-to-end.

## Canary discs (pinned in `scripts/canary.py` and `tests/test_parser.py`)

| ID | System | Why it's there |
|---|---|---|
| 133379 | pc  | Myst — single data track, PVD present, ring codes |
| 99835  | mac | MechWarrior 2 (Mac) — 28 tracks, no PVD, audio-heavy |
| 44803  | pc  | MechWarrior 2 (PC EU) — multi-dumper, zero-date PVD |
| 27832  | pc  | Super Street Fighter II Turbo — 45 tracks, no Serial, has Version |
| 16345  | pc  | American McGee's Alice — 5 languages, multi-ring, HTML in Comments |
| 92225  | pc  | 007 Legends — DVD-9, **no track table** (single `.iso` in files[]), has Layerbreak |

## Resumable bulk scrape

`scripts/scrape.py` is checkpointed in two ways:
- **Discovery cache** at `data/discovery.json` (gitignored). Listing pages are walked once; restart reuses the cached `{system: [redump_id, ...]}` map. Force a rewalk with `--refresh-discovery`.
- **JSONL flush** every `--flush-every` rows (default 100). Crash/Ctrl-C loses at most that many in-flight rows; restart skips already-stored IDs.

ETA + parsed/failed counters log every 50 fetches.

## Media field & DVDs

The schema includes an optional `media` field (e.g. "CD", "DVD-9"). DVDs are **kept**, not filtered — consumers can `WHERE media LIKE 'CD%'` to get CD-only. On redump.info a CD page has a tracks table (`# / Type / Pregap / Length / Sectors`) plus a files table; a **DVD page has no tracks table at all** — just the single `.iso` row in the files table — so `tracks[]` is empty and the image's hashes come from `files[]`. Both the tracks and files parsers are header-driven.

## How to run locally (macOS)

```bash
brew install uv
uv sync --extra dev
uv run pytest -q

# redump pipeline
uv run scripts/scrape.py --limit 5 --systems pc   # seed test
uv run scripts/scrape.py --backfill-all           # one-time redump.info v3 re-scrape (resumable)
uv run scripts/validate.py

# winworld pipeline (heavy data goes to NAS via WINWORLD_DATA_DIR)
export WINWORLD_DATA_DIR=/Volumes/Software/winworld-pc
uv run winworld/scripts/scrape_metadata.py
uv run winworld/scripts/assemble.py
uv run winworld/scripts/build_inventory_index.py
uv run winworld/scripts/match_local.py --verify   # find local files already on disk
uv run winworld/scripts/seed_from_local.py        # mark them done
uv run winworld/scripts/download.py               # daily 25/file trickle

# build unified release artifact (reads both JSONLs)
uv run scripts/build_sqlite.py                    # -> data/ode-lookup.sqlite
```

## Failsafes (validator.py)

Hard failures (abort commit): schema violation, missing required field, bad hash format, duplicate `redump_id`, system outside allowlist, growth cap (>500 new rows/run by default).

Soft warnings (issue filed, commit proceeds): row-count shrink.

Network/HTTP errors fail loudly — never silently treated as "no new discs."
