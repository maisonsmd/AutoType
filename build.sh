#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "→ Generating icon..."
uv run python scripts/make_icon.py

echo "→ Building app bundle..."
uv run pyinstaller AutoType.spec --noconfirm --clean

echo "→ Signing app (plain ad-hoc, no hardened runtime)..."
# NOTE: do NOT use --options runtime here. Hardened runtime enables strict
# arm64e PAC enforcement of CoreFoundation's embedded objects, which
# intermittently crashes Qt's static initializers in __CFCheckCFInfoPACSignature.
# Plain ad-hoc --deep signs all nested dylibs in the correct order.
codesign --force --deep --sign - dist/AutoType.app
codesign --verify --verbose dist/AutoType.app

echo ""
echo "✓  dist/AutoType.app is ready"
echo ""
echo "   Install:"
echo "     cp -R dist/AutoType.app /Applications/"
