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

SWIFT_SOURCES=("${(@f)$(find "${PROJECT_ROOT}/sources/vphone-legacy-gui" -name '*.swift' | sort)}")
BUILD_CACHE="$(mktemp -d "${TMPDIR:-/private/tmp/}vphone-gui.XXXXXX")"
trap 'rm -rf "${BUILD_CACHE}"' EXIT

echo "  Copying resources..."
if [ -d "${PROJECT_ROOT}/sources/vphone-legacy-gui/Resources" ]; then
    cp -R "${PROJECT_ROOT}/sources/vphone-legacy-gui/Resources/." "${RESOURCES_DIR}/"
fi

echo "  Typechecking Swift GUI sources..."
swiftc -sdk "${SDK_PATH}" -typecheck -parse-as-library \
    -target "$(uname -m)-apple-macosx14.0" -module-cache-path "${BUILD_CACHE}" \
    "${SWIFT_SOURCES[@]}"

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
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>iOS Software Update Package</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
            <key>LSHandlerRank</key>
            <string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>com.apple.itunes.ipsw</string>
            </array>
        </dict>
        <dict>
            <key>CFBundleTypeName</key>
            <string>iOS Application Archive</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
            <key>LSHandlerRank</key>
            <string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>com.apple.itunes.ipa</string>
            </array>
        </dict>
    </array>
    <key>UTImportedTypeDeclarations</key>
    <array>
        <dict>
            <key>UTTypeIdentifier</key>
            <string>com.apple.itunes.ipsw</string>
            <key>UTTypeDescription</key>
            <string>iOS Software Update Package</string>
            <key>UTTypeConformsTo</key>
            <array>
                <string>public.data</string>
                <string>public.archive</string>
            </array>
            <key>UTTypeTagSpecification</key>
            <dict>
                <key>public.filename-extension</key>
                <array>
                    <string>ipsw</string>
                </array>
            </dict>
        </dict>
        <dict>
            <key>UTTypeIdentifier</key>
            <string>com.apple.itunes.ipa</string>
            <key>UTTypeDescription</key>
            <string>iOS Application Archive</string>
            <key>UTTypeConformsTo</key>
            <array>
                <string>public.data</string>
                <string>public.archive</string>
            </array>
            <key>UTTypeTagSpecification</key>
            <dict>
                <key>public.filename-extension</key>
                <array>
                    <string>ipa</string>
                </array>
            </dict>
        </dict>
    </array>
</dict>
</plist>
EOF

echo "  Ad-hoc signing .app bundle..."
codesign --force --deep --sign - "${APP_DIR}"

echo "[+] Built successfully: ${APP_DIR}"
