#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$PROJECT_DIR/dist/WeChat Work Bridge.app"
OUTPUT="$PROJECT_DIR/dist/WeChat-Work-Bridge-0.1.0.dmg"
if [[ ! -d "$APP" ]]; then
  bash "$PROJECT_DIR/scripts/build_companion.sh"
fi
/usr/bin/codesign --verify --strict "$APP"
STAGING="$(mktemp -d "$PROJECT_DIR/dist/.workbridge-package.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
/usr/bin/ditto "$APP" "$STAGING/WeChat Work Bridge.app"
cp "$PROJECT_DIR/LICENSE" "$STAGING/LICENSE"
cp "$PROJECT_DIR/docs/morning-preview.md" "$STAGING/使用说明.md"
# Explicit resources only; never package the repository or local evidence.
/usr/bin/hdiutil create -quiet -ov -format UDZO -volname "WeChat Work Bridge" \
  -srcfolder "$STAGING" "$OUTPUT"
/usr/bin/hdiutil verify -quiet "$OUTPUT"
/usr/bin/shasum -a 256 "$OUTPUT"
