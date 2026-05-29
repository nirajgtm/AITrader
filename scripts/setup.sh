#!/usr/bin/env bash
# Create local venv and install deps for the trader arsenal.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  echo "Creating venv at $SCRIPT_DIR/.venv"
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install -r requirements.txt

echo
echo "Arsenal ready. Python: $(.venv/bin/python3 --version)"
echo "Try: .venv/bin/python3 $SCRIPT_DIR/price.py SPY"
