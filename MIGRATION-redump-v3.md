# Consumer migration guide — redump **schema v3** (redump.info)

**Audience:** anyone querying `ode-lookup.sqlite` (primarily `ODE-artwork-downloader`).
**Status:** breaking for redump hash lookups. Read the [30-second version](#tldr) and the [checklist](#checklist).

---

## TL;DR

The redump source moved from the retired **redump.org** to **redump.info**. Disc
IDs are unchanged, but redump.info separates a disc's **tracks** (physical
layout) from its **files** (hashes), and the DB now mirrors that split:

- **Per-file hashes moved off `redump_track` into a new `redump_file` table.**
  This is the one change that breaks existing queries.
- `redump_track` is now geometry-only, and is **empty for DVDs**.
- New `redump_disc.cuesheet_sha1` column.
- `catalog` (MCN) is no longer populated.

`meta` reports `schema_version = 3` for source `redump`. Everything else
(serials, regions, languages, PVD, ring codes, title/edition/version/media/
barcode) is semantically unchanged.

Still on v2 and not ready to move? A frozen final v2 build is archived — see
[Staying on v2](#staying-on-v2).

---

## The one required change: hash lookups

Hashes are how you match a dumped file to a disc, and they moved tables.

```sql
-- BEFORE (v2):
SELECT redump_id FROM redump_track WHERE sha1 = :sha1;

-- AFTER (v3):
SELECT redump_id FROM redump_file  WHERE sha1 = :sha1;
```

Same for `md5` and `crc32`. `redump_file` is indexed on all three plus
`filename`. If you were joining `redump_track` to get a hash *and* track number,
note that a file maps to its track via the `(Track NN)` suffix in `filename`
(single-track discs and DVDs have no such suffix).

### New table: `redump_file`

```sql
CREATE TABLE redump_file (
    redump_id   INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,      -- 0-based order on the page (.cue first, then tracks/image)
    filename    TEXT    NOT NULL,
    size_bytes  INTEGER,
    crc32       TEXT,                  -- lowercase hex, may be NULL
    md5         TEXT,                  -- lowercase hex, may be NULL
    sha1        TEXT,                  -- lowercase hex, may be NULL
    PRIMARY KEY (redump_id, seq)
);
-- indexes: sha1, md5, crc32 (partial, non-null), filename COLLATE NOCASE
```

One row per dumped file: the `.cue` plus one file per track (CD) or the single
image (DVD/`.iso`). redump.info sometimes omits `md5`/`sha1` on a duplicate
image (e.g. an `.img` mirroring a `.bin`), so **any single hash column may be
`NULL` — match on whichever is present.**

### Changed table: `redump_track`

```sql
CREATE TABLE redump_track (
    redump_id INTEGER NOT NULL REFERENCES redump_disc(redump_id) ON DELETE CASCADE,
    number    INTEGER NOT NULL,
    kind      TEXT,          -- 'data' | 'audio'
    pregap    TEXT,          -- MSF timecode "mm:ss:ff"  (NEW)
    length    TEXT,          -- MSF timecode "mm:ss:ff"  (NEW)
    sectors   INTEGER,
    PRIMARY KEY (redump_id, number)
);
```

**Removed columns:** `size_bytes`, `crc32`, `md5`, `sha1` (all now on
`redump_file`). **`redump_track` has zero rows for DVDs** — their image is
described only by `redump_file` — so don't assume every disc has tracks.

### New column: `redump_disc.cuesheet_sha1`

SHA-1 of the disc's `.cue` file, lifted onto the disc row for convenient
exact-disc matching. `NULL` for discs with no `.cue` (e.g. DVDs).

### Deprecated: `catalog`

redump.info doesn't expose the Media Catalog Number cleanly, so
`redump_disc.catalog` is no longer populated (it was on ~0.1% of rows and never
a primary match key). The column remains for back-compat; re-scraped rows have
it `NULL`. Match on hashes, serials, PVD volume label, or barcode instead.

---

## If you read the JSONL directly (`data/redump.jsonl`)

The row shape follows the same split:

```jsonc
{
  "schema_version": 3,
  "redump_id": 133379,
  "tracks": [                                   // geometry only; [] for DVDs
    {"number": 1, "type": "data", "pregap": "00:00:00", "length": "69:22:11", "sectors": 312161}
  ],
  "files": [                                    // NEW — hashes live here
    {"filename": "Myst (UK) (Rerelease).cue", "size_bytes": 87,  "crc32": "b19fdd9b", "md5": "...", "sha1": "..."},
    {"filename": "Myst (UK) (Rerelease).bin", "size_bytes": 734202672, "crc32": "9c5bfbb9", "md5": "...", "sha1": "..."}
  ],
  "cuesheet_sha1": "9675c0cd35945a1e50224cb17db53e8b01eb5413",
  "redump_url": "https://redump.info/disc/133379"
  // ... region, languages, serials, edition, version, media, barcode, pvd, disc_structure ...
}
```

Track objects no longer carry `crc32`/`md5`/`sha1`/`size_bytes`.

---

## Deleted discs

3 IDs present in v2 no longer exist on redump.info (deleted upstream, confirmed
via a full parity audit) and are gone from v3: **9206** (Stephen King's F13),
**48158** (Grim Fandango, Disc A), **125596** (Söldner: Secret Wars). If they
were merely renumbered upstream they will reappear under new IDs via normal
discovery. Handle a missing `redump_id` as "not found," as you already do.

---

## Staying on v2

A frozen, final v2 database is archived and will not be updated:

- **Release:** `db-schema-v2-final`
  (`https://github.com/danifunker/ODE-lookup-db/releases/tag/db-schema-v2-final`)
- Contents: redump schema_version 2 (60,216 discs), winworld v2 (642 releases).
- Decompress: `zstd -d ode-lookup.sqlite.zst`

Pin here only as a stopgap — it is a static snapshot and receives no new discs
or corrections. The moving `latest` release tag now serves v3.

---

## Checklist

- [ ] Point hash lookups at `redump_file` instead of `redump_track`.
- [ ] Treat `redump_track` as geometry-only; stop reading hashes/`size_bytes` from it.
- [ ] Handle discs with **zero** track rows (DVDs).
- [ ] Treat any single hash column as possibly `NULL`; match on whichever is present.
- [ ] Optionally adopt `redump_disc.cuesheet_sha1` for exact-disc matching.
- [ ] Stop relying on `catalog` (now always `NULL` for redump rows).
- [ ] Gate on `SELECT schema_version FROM meta WHERE source='redump'` — expect `3`; warn/branch on `2`.
- [ ] Pin `db-schema-v2-final` if you need more time before migrating.
```

See also `MIGRATION-unified-db.md` (the earlier single-file→unified-DB change,
table renames, and asset URL) and `schema/README.md` (per-field reference).
