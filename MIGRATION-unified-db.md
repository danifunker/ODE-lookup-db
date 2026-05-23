# Migration: unified `ode-lookup.sqlite`

**Status:** landed in repo on `schema-v2` branch. Pending: cut a release.
**Breaking for:** ODE-artwork-downloader and any other consumer that downloads
`redump.sqlite` from a GitHub Release of this repo.

## What's changing

This repo now produces **one** SQLite per release instead of one per data source.

| Before | After |
|---|---|
| `redump.sqlite` (release asset) | `ode-lookup.sqlite` (release asset) |
| Table `discs` | Table `redump_disc` |
| Table `tracks` | Table `redump_track` |
| Table `serials` | Table `redump_serial` |
| Table `regions` | Table `redump_region` |
| Table `languages` | Table `redump_language` |
| Table `artwork` | Table `redump_artwork` |
| `meta(schema_version, built_at, source_commit, row_count)` (single row) | `meta(source, schema_version, built_at, source_commit, row_count)` — one row per source (`redump`, `winworld`) |

New tables (WinWorld dataset added):

- `winworld_product` — one row per product slug
- `winworld_release` — one row per release page
- `winworld_download` — one row per downloadable file (filename, media_kind, language, architecture, file_size_text, archive_hash, archive_hash_alg)
- `winworld_serial`, `winworld_screenshot` — side tables

Source-of-truth files (`data/redump.jsonl`, `winworld/data/winworld.jsonl`)
remain per-source and are unchanged in shape.

## What consumers need to do

1. Update the release-asset download URL: `redump.sqlite` → `ode-lookup.sqlite`.
2. Update table names in queries: `discs` → `redump_disc`, etc. The column
   schemas inside each table are **unchanged**.
3. Update `meta` reads if you read schema_version directly — it's now keyed
   by `source`, so query e.g. `SELECT schema_version FROM meta WHERE source='redump'`.

That's it. No data semantics change for the redump side.

## Why

We're adding a second data source (WinWorld). Rather than ship two parallel
SQLite files, one bundle is easier for consumers (lookup falls through:
redump → winworld → not found) and enables native cross-source joins where
both sources cover the same artifact.

## Schedule

- ☑ Refactor lands in `ODE-lookup-db`
- ☐ Cut a tagged release that publishes `ode-lookup.sqlite` (and optionally
  `redump.sqlite` for one transitional cycle — let danifunker know if you need
  this)
- ☐ Consumer (`ODE-artwork-downloader`) updates queries + asset URL
- ☐ Stop publishing `redump.sqlite`

## Sanity numbers (initial unified build)

```
$ sqlite3 data/ode-lookup.sqlite "SELECT * FROM meta;"
redump  |2|2026-05-22T02:17:45+00:00||59899
winworld|1|2026-05-22T02:17:45+00:00||3123
```

Total file size ~62 MB. Tables: 7 `redump_*` + 5 `winworld_*` + 2 FTS5 +
`meta`. See `src/ode_lookup_db/db.py` for the full schema.
