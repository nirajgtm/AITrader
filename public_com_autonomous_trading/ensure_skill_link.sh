#!/usr/bin/env bash
# Ensure ~/.claude/skills/trader-autonomous is a symlink into this repo's
# skills/trader-autonomous, so the setup is portable: clone the repo anywhere, run this,
# and the harness reads the skill straight from the repo. Idempotent and safe -- a
# pre-existing real directory is backed up (never deleted) before the link is created.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO/skills/trader-autonomous"
DEST_DIR="$HOME/.claude/skills"
DEST="$DEST_DIR/trader-autonomous"
if [ ! -d "$SRC" ]; then
  echo "FAIL: repo skill not found at $SRC" >&2
  exit 1
fi
mkdir -p "$DEST_DIR"
if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
  echo "OK: already linked ($DEST -> $SRC)"
  exit 0
fi
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  BAK="$DEST.bak.$(date +%Y%m%d%H%M%S)"
  mv "$DEST" "$BAK"
  echo "backed up existing $DEST -> $BAK"
fi
ln -s "$SRC" "$DEST"
echo "LINKED: $DEST -> $SRC"
