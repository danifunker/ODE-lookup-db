# ODE-lookup-db

Auto-updated lookup database for optical-disc and software-archive identification, curated for the USBODE ecosystem.

**Sources:**
- [redump.org](http://redump.org) — PC and Mac CD/DVD metadata (tables `redump_*`)
- [winworldpc.com](https://winworldpc.com) — abandonware archives (tables `winworld_*`)

**Out of scope:** consoles, audio CDs, video discs. See `MIGRATION-unified-db.md` for the breaking-change migration notes if you previously consumed `redump.sqlite`.

## For consumers (apps that look up discs)

The latest database is published as a GitHub Release. Fetch it like this:

```bash
# Most recent build — stable URL
curl -L -o ode-lookup.sqlite \
  https://github.com/danifunker/ODE-lookup-db/releases/download/latest/ode-lookup.sqlite

# Specific dated build
curl -L -o ode-lookup.sqlite \
  https://github.com/danifunker/ODE-lookup-db/releases/download/db-2026-05-18/ode-lookup.sqlite
```

Use `If-Modified-Since` / `ETag` against the asset URL to avoid re-downloading unchanged builds.

### SQLite schema

`meta` has one row per source with its own `schema_version`:

```sql
SELECT * FROM meta;
-- redump  | 2 | 2026-05-22T... | <commit> | 59899
-- winworld| 1 | 2026-05-22T... | <commit> | 3123
```

Redump tables (PC / Mac CDs and DVDs):

| Table | Purpose |
|---|---|
| `redump_disc` | One row per redump disc |
| `redump_track` | One row per track. Indexed by `crc32`, `md5`, `sha1` |
| `redump_serial` | Vendor serials, indexed |
| `redump_region` | Region tags |
| `redump_language` | ISO 639-1 codes (`zz` = unmapped) |
| `redump_artwork` | Reserved; not populated yet |
| `redump_disc_fts` | FTS5 over title + foreign_title + pvd_volume_id |

WinWorld tables (abandonware archives):

| Table | Purpose |
|---|---|
| `winworld_product` | One row per product slug (windows-95, mac-os-x, …) |
| `winworld_release` | One row per release page; includes notes, install instructions, info JSON |
| `winworld_download` | One row per `.7z` download. Indexed by `archive_hash`, `media_kind`, `language` |
| `winworld_serial` | Per-release product keys when listed on the page |
| `winworld_screenshot` | Per-release screenshot URLs + alt text |
| `winworld_release_fts` | FTS5 over title + subtitle + description |

Query by track hash (redump):

```sql
SELECT redump_id, title FROM redump_disc
JOIN redump_track USING (redump_id)
WHERE redump_track.sha1 = ?;
```

Query by PVD volume label (useful for mounted ISOs):

```sql
SELECT redump_id, title FROM redump_disc WHERE pvd_volume_id = ?;
```

Query by WinWorld `.7z` hash (only matches the archive bytes, not the inner ISO):

```sql
SELECT filename, media_kind, language
FROM winworld_download
WHERE archive_hash = ?;
```

Always check `meta.schema_version` for the source you're querying.

## For contributors

### Found a wrong/outdated disc entry?

[Open a "Disc re-check request" issue](https://github.com/danifunker/ODE-lookup-db/issues/new?template=disc-recheck.yml). Include the `redump_id` (the number in the redump.org disc URL). The next daily run will re-scrape it and close the issue.

Recheck requests are processed up to **30 per UTC day**. If many are open, yours may roll over to the next run — no manual approval needed.

### Local development (macOS)

```bash
brew install uv
git clone https://github.com/danifunker/ODE-lookup-db.git
cd ODE-lookup-db
uv sync --extra dev
uv run pytest -q

# Try a tiny scrape (redump)
uv run scripts/scrape.py --limit 5 --systems pc
uv run scripts/validate.py
uv run scripts/build_sqlite.py
sqlite3 data/ode-lookup.sqlite '.schema'
```

WinWorld pipeline (heavy artifacts go on a NAS via `WINWORLD_DATA_DIR`):

```bash
export WINWORLD_DATA_DIR=/Volumes/Software/winworld-pc
uv run winworld/scripts/scrape_metadata.py
uv run winworld/scripts/assemble.py
uv run winworld/scripts/build_inventory_index.py
uv run scripts/build_sqlite.py        # unified build with both sources
```

### Seeding from scratch

A full re-seed is needed after a parser/schema change, because `scrape.py` skips
any `redump_id` already present in `data/redump.jsonl`. To re-fetch everything
through the current parser, start from an empty source file:

```bash
# 1. Clear the existing data (it's Git LFS-tracked; a content replacement needs
#    no LFS surgery — the .gitattributes rule re-applies on the next `git add`).
git rm data/redump.jsonl

# 2. Exhaustive walk of every PC/Mac listing page (not the added-desc checkpoint).
uv run scripts/scrape.py --full-discovery

# 3. Validate and build.
uv run scripts/validate.py
uv run scripts/build_sqlite.py
```

#### Manually setting the discovery checkpoint

The daily cron uses the **added-desc** discovery path, which walks redump's
newest-first listing and stops once it reaches `topmost_redump_id` in
`data/discovery_checkpoint.json`. A `--full-discovery` seed does **not** update
this file, so after a from-scratch seed you must set it by hand to the highest
`redump_id` you captured — otherwise the next cron run re-walks discs you already
have.

Set it to the max ID in the freshly seeded JSONL:

```bash
uv run python -c "
import json, pathlib
ids = [json.loads(l)['redump_id'] for l in pathlib.Path('data/redump.jsonl').read_text().splitlines() if l.strip()]
top = max(ids)
pathlib.Path('data/discovery_checkpoint.json').write_text(json.dumps({'topmost_redump_id': top}, indent=2) + chr(10))
print('checkpoint set to', top)
"
```

The file format is:

```json
{
  "topmost_redump_id": 133529,
  "updated_at": "2026-05-19T17:05:04.627539+00:00"
}
```

Only `topmost_redump_id` is required; `updated_at` is informational and is
refreshed automatically on the next successful cron run. Commit the checkpoint
alongside the seeded `redump.jsonl`.

### Repo layout

```
data/redump.jsonl      redump source of truth (one disc per line, JSONL)
data/stats.json        redump row counts + all known IDs
data/ode-lookup.sqlite unified built artifact (gitignored; released)
schema/                JSON Schema + human field docs (redump)
src/ode_lookup_db/     parser, validator, db, scraper, http client
scripts/               CLI entry points: scrape, recheck, validate, build_sqlite, canary, stats
tests/                 pytest; fixtures hold cached HTML
.github/workflows/     daily.yml (cron), validate-pr.yml

winworld/              winworldpc.com subtree (see winworld/README.md)
  src/ode_winworld/    fetch, parse, download modules
  scripts/             scrape_metadata, assemble, download, match_local, ...
  data/winworld.jsonl  winworld source of truth (committed when assembled locally)
```

### Daily pipeline

Triggered by cron at 06:17 UTC and via `workflow_dispatch`:

1. Process open `disc-recheck` issues (≤ 30/day)
2. Discover new disc IDs across PC and Mac system pages
3. Fetch each new disc (≤ 1 req/sec, polite User-Agent)
4. Parse → validate against `schema/disc.schema.json`
5. Run canary (re-parse pinned discs from the live site — see below)
6. Append/update `data/redump.jsonl`, refresh `stats.json`
7. Commit; tag previous HEAD as `db-good-YYYY-MM-DD` for rollback
8. Build `ode-lookup.sqlite` and publish as a GitHub Release

Failures file a GitHub issue and abort the commit. Warnings file an issue but proceed.

### Canary fixtures

`scripts/canary.py` guards against redump changing their HTML in a way that
breaks the parser. It fetches the live page for each pinned disc (see
`tests/fixtures/canary/*.html`) and compares the parse to the stored
`*.expected.json`.

A live page can differ from its fixture for two reasons, and the canary treats
them differently:

- **The parser broke** — the live page no longer parses, or no longer passes
  schema validation. This is a real regression: the canary **hard-fails**
  (exit 1) and the daily run aborts.
- **A redump editor edited the disc** — new ring code, extra dumper, a volume
  label that wasn't there before, a re-dump, etc. The parser is fine; our frozen
  fixture is just stale. The canary logs a **soft warning** ("drift"), notes it
  in the Actions step summary, and **lets the run proceed** (exit 0).

So an upstream metadata edit no longer kills the daily scrape. When you see a
drift warning, refresh the fixture at your convenience:

```bash
# Refresh just the disc(s) that drifted (the warning prints the exact command):
uv run scripts/canary.py --update --ids 16345

# Or refresh every canary fixture:
uv run scripts/canary.py --update
```

`--update` re-fetches the live page and rewrites **both** `<id>.html` and
`<id>.expected.json` together — they must stay in sync because
`tests/test_parser.py` parses the frozen HTML offline and compares it to the
expected JSON. After updating, run `uv run pytest tests/test_parser.py` and fix
any hand-written assertions that pinned the old data (e.g. a disc that gained a
volume label moves from `test_volume_label_absent` to `test_volume_label_positive`).

Other flags:

- `--strict` — treat *any* difference as a failure (exit 1). Useful for a manual
  end-to-end check that nothing at all has changed.
- `--ids 16345,99835` — limit any mode to specific discs.

## Schema

See [`schema/README.md`](schema/README.md) for field-by-field docs. The schema is **forward-compatible v1** — new optional fields may be added; existing fields will not be renamed or removed without a major-version bump.

The `artwork` field is reserved for future use (archived cover/disc/manual scans). It is always `null` or omitted in v1.

## License

TBD.
