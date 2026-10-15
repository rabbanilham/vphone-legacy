#!/bin/zsh
# build_app.sh — Build vphone-legacy.app macOS application bundle

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
APP_DIR="${PROJECT_ROOT}/dist/vphone-legacy.app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"
RESOURCES_DIR="${APP_DIR}/Contents/Resources"

echo "=== Building vphone-legacy.app ==="
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"

SWIFT_SOURCES=("${(@f)$(rg --files "${PROJECT_ROOT}/sources/vphone-legacy-gui" -g '*.swift')}")
BUILD_CACHE="$(mktemp -d "${TMPDIR:-/private/tmp/}vphone-gui.XXXXXX")"
trap 'rm -rf "${BUILD_CACHE}"' EXIT

echo "  Copying resources..."
if [ -d "${PROJECT_ROOT}/sources/vphone-legacy-gui/Resources" ]; then
    cp -R "${PROJECT_ROOT}/sources/vphone-legacy-gui/Resources/." "${RESOURCES_DIR}/"
fi

echo "  Compiling Swift GUI sources with swiftc (${#SWIFT_SOURCES[@]} files)..."
swiftc -sdk "${SDK_PATH}" -O -parse-as-library \
    -target "$(uname -m)-apple-macosx14.0" -module-cache-path "${BUILD_CACHE}" \
    "${SWIFT_SOURCES[@]}" \
    -o "${MACOS_DIR}/vphone-legacy-gui"

echo "  Generating Info.plist..."
cat << 'EOF' > "${APP_DIR}/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>vphone-legacy-gui</string>
    <key>CFBundleIdentifier</key>
    <string>com.vphone.legacy.gui</string>
    <key>CFBundleName</key>
    <string>vphone-legacy</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

echo "  Ad-hoc signing .app bundle..."
codesign --force --deep --sign - "${APP_DIR}"

echo "[+] Built successfully: ${APP_DIR}"
