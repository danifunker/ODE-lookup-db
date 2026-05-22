# winworld/ — WinWorld scraper & lookup DB

Sibling dataset to the redump DB in this repo. Scrapes winworldpc.com,
downloads archives, extracts ISO/disc images, and builds a lookup DB keyed
on cooked-ISO fingerprints (PVD + file-tree hash) plus original artifact hashes.

## Status

- [x] Metadata scrape (product + release pages, comments, screenshots)
- [ ] Archive download
- [ ] Extraction + ISO/PVD parsing
- [ ] Cross-ref to redump (only feasible for `.cue/.bin` archives)

## Quick start

```bash
uv sync --extra dev
uv run winworld/scripts/scrape_metadata.py          # discover + fetch + parse, resumable
uv run winworld/scripts/assemble.py                  # fold sidecars -> winworld.jsonl + fulllog.json
```

## Layout

```
winworld/
  src/ode_winworld/        # importable package
  scripts/                 # CLI entry points
  schema/                  # JSON schema (when stable)
  data/
    raw/                   # gitignored — HTML, screenshots, fetch sidecars
      pages/
        listings/<cat>-page-<n>.{html,fetch.json}
        product/<slug>.{html,fetch.json,parse.json}
        release/<product>/<release>.{html,fetch.json,parse.json}
      screenshots/<product>/<sha>.{ext,json}
      errors/<iso>-<short>.json
    archives/              # gitignored — downloaded .7z etc
    extracted/             # gitignored — opened images
    discovery.json         # gitignored — resume cache
    winworld.jsonl         # committed — source of truth
    fulllog.json           # committed — denormalized everything
    stats.json             # committed — counts
```

Each fetched URL leaves a `<name>.fetch.json` sidecar; each parsed page leaves
a `<name>.parse.json`. The filesystem is the resume ledger — no DB needed for
"have we done this URL." `assemble.py` is a pure fold over the raw tree.

## Conventions

- Rate limit: 1 req/sec to winworldpc.com (configurable via `WINWORLD_RPS`).
- UA: `ODE-winworld-db/0.1 (+https://github.com/danifunker/ODE-lookup-db)`.
- Never throw away bytes we paid network for — raw HTML is always written
  before parsing, so reparse is always offline.
