#!/bin/bash
set -euo pipefail

# Build only the companion's explicit public resources; never package the
# repository, probe profiles, runtime logs, chat database, or private artifacts.
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/dist"
APP_PATH="$BUILD_DIR/WeChat Work Bridge.app"
XCODE_DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
if [[ ! -d "$XCODE_DEVELOPER_DIR" ]]; then
  echo "Full Xcode is required; set DEVELOPER_DIR to its Contents/Developer path." >&2
  exit 1
fi
mkdir -p "$BUILD_DIR"
STAGING="$(mktemp -d "$BUILD_DIR/.workbridge-build.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
APP="$STAGING/WeChat Work Bridge.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/web"
DEVELOPER_DIR="$XCODE_DEVELOPER_DIR" xcrun --sdk macosx swiftc -swift-version 5 -O \
  -framework AppKit -framework WebKit "$PROJECT_DIR/companion/Launcher.swift" \
  -o "$APP/Contents/MacOS/WeChatWorkBridge"
cp "$PROJECT_DIR/companion/service.py" "$APP/Contents/Resources/service.py"
cp "$PROJECT_DIR/LICENSE" "$APP/Contents/Resources/LICENSE"
for asset in index.html style.css app.js; do
  cp "$PROJECT_DIR/companion/web/$asset" "$APP/Contents/Resources/web/$asset"
done
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.wechat.workbridge</string>
  <key>CFBundleName</key><string>WeChat Work Bridge</string>
  <key>CFBundleDisplayName</key><string>微信工作桥</string>
  <key>CFBundleExecutable</key><string>WeChatWorkBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSHumanReadableCopyright</key><string>WeChat Work Bridge contributors · GPL-3.0</string>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$APP/Contents/Info.plist"
/usr/bin/codesign --force --sign - "$APP"
/usr/bin/codesign --verify --strict "$APP"
if [[ -e "$APP_PATH" ]]; then
  EXISTING_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_PATH/Contents/Info.plist" 2>/dev/null || true)
  if [[ "$EXISTING_ID" != "local.wechat.workbridge" || -L "$APP_PATH" ]]; then
    echo "Refusing to replace an unrelated application: $APP_PATH" >&2
    exit 1
  fi
  # Preserve the previous owned build for rollback; never modify installed apps.
  mv "$APP_PATH" "$BUILD_DIR/WeChat Work Bridge.previous.$(date +%Y%m%d-%H%M%S).app"
fi
mv "$APP" "$APP_PATH"
echo "$APP_PATH"
