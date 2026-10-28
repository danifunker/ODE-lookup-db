#!/usr/bin/env bash
# Download the source-of-truth JSONLs from a GitHub Release.
#
# The JSONLs are not committed (they blew through the Git LFS quota). Each
# release carries redump.jsonl.gz + winworld.jsonl.gz next to the SQLite build;
# CI and local dev pull them from here before scraping/validating/building.
#
#   scripts/fetch_data.sh            # from the `latest` release
#   scripts/fetch_data.sh db-2026-10-27
#   scripts/fetch_data.sh --force    # overwrite existing local JSONLs
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WINWORLD_DIR="${WINWORLD_DATA_DIR:-$REPO_ROOT/winworld/data}"

TAG="latest"
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) TAG="$arg" ;;
  esac
done

REDUMP_OUT="$REPO_ROOT/data/redump.jsonl"
WINWORLD_OUT="$WINWORLD_DIR/winworld.jsonl"

# Refuse to clobber local work (e.g. a freshly assembled winworld.jsonl on the NAS).
if [ "$FORCE" -eq 0 ]; then
  for f in "$REDUMP_OUT" "$WINWORLD_OUT"; do
    if [ -e "$f" ]; then
      echo "refusing to overwrite existing $f (pass --force)" >&2
      exit 1
    fi
  done
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh release download "$TAG" -p redump.jsonl.gz -p winworld.jsonl.gz -D "$tmp"

mkdir -p "$(dirname "$REDUMP_OUT")" "$WINWORLD_DIR"
gunzip -c "$tmp/redump.jsonl.gz"   > "$REDUMP_OUT"
gunzip -c "$tmp/winworld.jsonl.gz" > "$WINWORLD_OUT"
wc -l "$REDUMP_OUT" "$WINWORLD_OUT"
