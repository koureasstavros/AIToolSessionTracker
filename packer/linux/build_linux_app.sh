#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "Checking for PyInstaller..."
if ! python3 -m pip show pyinstaller >/dev/null 2>&1; then
    echo "PyInstaller is not installed. Installing it for the current Python environment..."
    python3 -m pip install pyinstaller
fi

echo "Building AI Tool Session Tracker for Linux..."
python3 -m PyInstaller --clean --noconfirm \
    --distpath "dist/linux" \
    "packer/linux/AI-Tool-Session-Tracker.spec"

echo
echo "Build complete:"
echo "dist/linux/AI-Tool-Session-Tracker"