# ODE-lookup-db

Auto-updated database of optical-disc identification metadata, scraped from [redump.org](http://redump.org) and curated for the USBODE ecosystem.

**Scope**: PC (IBM PC compatible) and Mac (Apple Macintosh) CD-ROM and DVD entries. Mac/PC hybrid discs are filed by redump under whichever system page is canonical (pc or mac) and are picked up automatically. Consoles, audio CDs, and video discs are out of scope.

## For consumers (apps that look up discs)

The latest database is published as a GitHub Release. Fetch it like this:

```bash
# Most recent build — stable URL
curl -L -o redump.sqlite \
  https://github.com/danifunker/ODE-lookup-db/releases/download/latest/redump.sqlite

# Specific dated build
curl -L -o redump.sqlite \
  https://github.com/danifunker/ODE-lookup-db/releases/download/db-2026-05-18/redump.sqlite
```

Use `If-Modified-Since` / `ETag` against the asset URL to avoid re-downloading unchanged builds.

### SQLite schema

| Table | Purpose |
|---|---|
| `meta` | `schema_version`, `built_at`, `source_commit`, `row_count` |
| `discs` | One row per redump disc, full JSON in the `json` column |
| `tracks` | One row per track. Indexed by `crc32`, `md5`, `sha1` |
| `serials` | Vendor serials, indexed |
| `regions` | Region tags |
| `languages` | ISO 639-1 codes (`zz` = unmapped) |

Query by hash:

```sql
SELECT redump_id, title FROM discs
JOIN tracks USING (redump_id)
WHERE tracks.sha1 = ?;
```

Query by PVD volume label (useful for mounted ISOs):

```sql
SELECT redump_id, title FROM discs WHERE pvd_volume_id = ?;
```

Always check `meta.schema_version` before parsing. v1 is the current schema; major-version bumps indicate breaking changes.

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

# Try a tiny scrape
uv run scripts/scrape.py --limit 5 --systems pc
uv run scripts/validate.py
uv run scripts/build_sqlite.py
sqlite3 data/redump.sqlite '.schema'
```

### Repo layout

```
data/redump.jsonl      source of truth (one disc per line, plain JSONL)
data/stats.json        row counts + all known IDs
data/redump.sqlite     built artifact (not committed; released)
schema/                JSON Schema + human field docs
src/ode_lookup_db/     parser, validator, db, scraper, http client
scripts/               CLI entry points: scrape, recheck, validate, build_sqlite, canary, stats
tests/                 pytest; fixtures hold cached HTML
.github/workflows/     daily.yml (cron), validate-pr.yml
```

### Daily pipeline

Triggered by cron at 06:17 UTC and via `workflow_dispatch`:

1. Process open `disc-recheck` issues (≤ 30/day)
2. Discover new disc IDs across PC and Mac system pages
3. Fetch each new disc (≤ 1 req/sec, polite User-Agent)
4. Parse → validate against `schema/disc.schema.json`
5. Run canary (re-parse pinned discs, assert exact expected output)
6. Append/update `data/redump.jsonl`, refresh `stats.json`
7. Commit; tag previous HEAD as `db-good-YYYY-MM-DD` for rollback
8. Build `redump.sqlite` and publish as a GitHub Release

Failures file a GitHub issue and abort the commit. Warnings file an issue but proceed.

## Schema

See [`schema/README.md`](schema/README.md) for field-by-field docs. The schema is **forward-compatible v1** — new optional fields may be added; existing fields will not be renamed or removed without a major-version bump.

The `artwork` field is reserved for future use (archived cover/disc/manual scans). It is always `null` or omitted in v1.

## License

TBD.
