# Disc schema (v3)

Human documentation of every field in `disc.schema.json`. The JSON Schema is authoritative; this file explains *why* each field exists.

## Versioning

- `schema_version` is an integer pinned to `3` on every row.
- **v3 is breaking** (redump.info migration). redump.info splits tracks and files into separate tables, so the row shape follows: `tracks[]` is now physical layout only (no hashes, and **empty for DVDs**) and a new required `files[]` array carries per-file hashes. `catalog` is no longer populated (redump.info doesn't expose it cleanly). See `../MIGRATION-unified-db.md` for the consumer impact.
- v2 was additive over v1 (`catalog`, `tracks[].sectors`, `pvd.volume_identifier`).
- Forward-compatible within a major version: new optional fields may be added; existing fields are not renamed or removed. Breaking changes require a major-version bump.
- Consumers should ignore unknown fields.

## Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | yes | Always `3`. |
| `redump_id` | int | yes | Unique key. Same disc on multiple system pages is deduped on this ID. |
| `system` | enum | yes | One of `pc`, `mac`. Allowlist enforced by validator. |
| `title` | string | yes | Primary title as shown by redump. |
| `foreign_title` | string\|null | no | Localized title if the entry has one. |
| `region` | string[] | no | Redump's region tags, e.g. `["USA", "Europe"]`. Stored verbatim. |
| `languages` | string[] | no | ISO 639-1 lowercase codes. Use `"zz"` for unmapped — the parser logs a warning so we can extend the mapping. |
| `languages_raw` | string[] | no | Original redump language strings, kept so we can re-derive `languages` if the mapping changes. |
| `edition` | string\|null | no | Edition label (e.g. "Original", "Game of the Year"). |
| `version` | string\|null | no | Disc-side version string. |
| `serials` | string[] | no | Vendor serial numbers printed on the disc. |
| `barcode` | string\|null | no | Retail barcode. |
| `catalog` | string\|null | no | **Deprecated in v3** — not populated from redump.info. Retained for back-compat; older v2 rows may still carry a value. |
| `category` | string\|null | no | Redump category, e.g. "Games", "Applications". |
| `pvd` | object\|null | no | Primary Volume Descriptor — ISO 9660 metadata. Useful for identifying mounted images even without hashes. |
| `tracks` | object[] | yes | Physical track layout: `number, type, pregap, length, sectors`. **May be empty** (DVDs have no track table). No hashes — those live in `files`. |
| `files` | object[] | yes | Dumped files with hashes: `filename, size_bytes, crc32, md5, sha1`. At least one (the `.cue` plus one file per track/image). This is where hash-based disc matching happens in v3. |
| `cuesheet_sha1` | string\|null | no | SHA-1 of the `.cue` file, lifted from `files` for convenience. |
| `disc_structure` | object\|null | no | Physical disc identifiers (ring codes, IFPI, mould SID). |
| `dumpers` | string[] | no | Credits. |
| `date_added` | string\|null | no | Redump's "Added" date. |
| `date_last_modified` | string\|null | no | Redump's "Last modified" date. |
| `redump_url` | string | yes | Canonical disc page URL. |
| `scraped_at` | string | no | When we last fetched and parsed this row (ISO 8601). |
| `artwork` | object\|null | no | **Reserved.** Will hold archived cover/disc/manual scans in a future revision. Always `null` or omitted in v1. |

## Why no `page_sha256`?

Earlier drafts hashed the raw HTML to detect upstream edits across full refreshes. Since there is no scheduled full refresh — rechecks are user-initiated via GitHub issues — change detection is unnecessary and the field is omitted from v1.

## File hashes (v3)

Hashes live on `files[]`, not tracks. Each file may carry `crc32` (8 hex chars), `md5` (32), and `sha1` (40) — all lowercase, regex-enforced by the validator. redump.info sometimes omits `md5`/`sha1` on duplicate images (e.g. an `.img` mirroring a `.bin`), so any single hash field may be `null`; match on whichever is present. To map a file back to its track, parse the `(Track NN)` suffix in `filename`.

## Track layout

`tracks[].sectors` is the per-track sector count taken directly from the Sectors column of redump.info's track table — the canonical duration value. `pregap`/`length` are MSF timecodes (`mm:ss:ff`) as strings. All are `null` when the cell is empty or malformed. `tracks` is empty for media with no track table (DVDs). Consumers use sectors for track-signature fuzzy matching.
