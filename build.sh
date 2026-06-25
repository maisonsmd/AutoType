#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "→ Generating icon..."
uv run python scripts/make_icon.py

echo "→ Building app bundle..."
uv run pyinstaller AutoType.spec --noconfirm --clean

echo ""
echo "✓  dist/AutoType.app is ready"
echo "   To install: cp -r dist/AutoType.app /Applications/"
