# winworld/ — WinWorld scraper & lookup DB

Sibling dataset to the redump DB in this repo. Scrapes winworldpc.com,
downloads archives, extracts ISO/disc images, and builds a lookup DB keyed
on cooked-ISO fingerprints (PVD + file-tree hash) plus original artifact hashes.

## Status

- [x] Metadata scrape (product + release pages, comments, screenshots)
- [x] Archive download (daily 25-cap, Windows-first priority)
- [x] Extraction + ISO/PVD/El-Torito fingerprinting (optical only)
- [ ] Cross-ref to redump (only feasible for `.cue/.bin` archives)
- [ ] `.cue/.bin` parser (currently only `.iso/.img` are inspected)

## Quick start

```bash
brew install uv 7-zip                                # 7zz binary required by extract phase
uv sync --extra dev

export WINWORLD_DATA_DIR=/Volumes/Software/winworld-pc   # where heavy data lives

# Phase 1: metadata (one big resumable scrape, hours at 1 req/sec)
uv run winworld/scripts/scrape_metadata.py
uv run winworld/scripts/assemble.py

# Phase 2: daily, 25 downloads (matches WinWorld's per-IP quota)
uv run winworld/scripts/download.py

# Phase 3: extract each .7z, fingerprint the disc image(s) inside, delete .7z
uv run winworld/scripts/extract_and_hash.py

# Re-run assemble.py + scripts/build_sqlite.py whenever you want a fresh DB.
```

The daily download+extract chain is meant to run on a schedule. macOS:

```bash
./winworld/launchd/install.sh        # install daily-at-06:00 LaunchAgent
launchctl kickstart gui/$(id -u)/com.danifunker.winworld-pipeline  # run now
./winworld/launchd/uninstall.sh      # remove
```

Logs land in `~/Library/Logs/winworld-pipeline.{out,err}.log`.

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
