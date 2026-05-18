# Disc schema (v1)

Human documentation of every field in `disc.schema.json`. The JSON Schema is authoritative; this file explains *why* each field exists.

## Versioning

- `schema_version` is an integer pinned to `1` on every row in v1.
- v1 is **forward-compatible only**: new optional fields may be added; existing fields will not be renamed or removed. Breaking changes require a major-version bump and a parallel rebuild.
- Consumers should ignore unknown fields.

## Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | yes | Always `1` in v1. |
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
| `category` | string\|null | no | Redump category, e.g. "Games", "Applications". |
| `pvd` | object\|null | no | Primary Volume Descriptor — ISO 9660 metadata. Useful for identifying mounted images even without hashes. |
| `tracks` | object[] | yes | At least one track. Each track must have at least one of crc32/md5/sha1. |
| `cuesheet_sha1` | string\|null | no | SHA-1 of the .cue file (for multi-track discs). |
| `disc_structure` | object\|null | no | Physical disc identifiers (ring codes, IFPI, mould SID). |
| `dumpers` | string[] | no | Credits. |
| `date_added` | string\|null | no | Redump's "Added" date. |
| `date_last_modified` | string\|null | no | Redump's "Last modified" date. |
| `redump_url` | string | yes | Canonical disc page URL. |
| `scraped_at` | string | no | When we last fetched and parsed this row (ISO 8601). |
| `artwork` | object\|null | no | **Reserved.** Will hold archived cover/disc/manual scans in a future revision. Always `null` or omitted in v1. |

## Why no `page_sha256`?

Earlier drafts hashed the raw HTML to detect upstream edits across full refreshes. Since there is no scheduled full refresh — rechecks are user-initiated via GitHub issues — change detection is unnecessary and the field is omitted from v1.

## Track hashes

At least one of `crc32` (8 hex chars), `md5` (32), or `sha1` (40) must be present on each track. All hex is lowercase. Regex-enforced by the validator.
