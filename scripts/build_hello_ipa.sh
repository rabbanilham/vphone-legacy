#!/usr/bin/env bash
set -euo pipefail

# Build script for Hello iOS 7 guest application and Hello.ipa
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUEST_DIR="$ROOT_DIR/sources/vphone-guest"
OUTPUT_DIR="$ROOT_DIR/build/sideload"
PAYLOAD_DIR="$OUTPUT_DIR/Payload"
APP_DIR="$PAYLOAD_DIR/Hello.app"
IPA_PATH="$OUTPUT_DIR/Hello.ipa"

PYTHON="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

SDK_IOS="$(xcrun --sdk iphoneos --show-sdk-path 2>/dev/null || true)"
if [ -z "$SDK_IOS" ]; then
    echo "Error: iPhoneOS SDK not found. Please ensure Xcode with iOS SDK is installed." >&2
    exit 1
fi

echo "Using iOS SDK: $SDK_IOS"

# 1. Generate icons
echo "Generating app icons..."
"$PYTHON" "$GUEST_DIR/generate_icons.py" --output "$GUEST_DIR/assets"

# 2. Prepare staging directory
rm -rf "$OUTPUT_DIR"
mkdir -p "$APP_DIR"

# 3. Compile arm64 binary targeting iOS 7.0
# Note: Modern Xcode SDKs dropped libarclite, so we use -fno-objc-arc (MRC).
echo "Compiling Hello binary for arm64 iOS 7.0..."
xcrun clang \
    -target arm64-apple-ios7.0 \
    -isysroot "$SDK_IOS" \
    -fno-objc-arc \
    -Wall -O2 \
    -framework UIKit \
    -framework Foundation \
    -framework CoreGraphics \
    -framework QuartzCore \
    -o "$APP_DIR/Hello" \
    "$GUEST_DIR/src/main.m" \
    "$GUEST_DIR/src/AppDelegate.m" \
    "$GUEST_DIR/src/ViewController.m"

# 4. Copy assets and metadata
cp "$GUEST_DIR/Info.plist" "$APP_DIR/Info.plist"
cp "$GUEST_DIR/assets/AppIcon60x60@2x.png" "$APP_DIR/"
cp "$GUEST_DIR/assets/Icon-60@2x.png" "$APP_DIR/"
cp "$GUEST_DIR/assets/AppIcon57x57.png" "$APP_DIR/"
cp "$GUEST_DIR/assets/Icon.png" "$APP_DIR/"
printf 'APPL????' > "$APP_DIR/PkgInfo"

# 5. Ad-hoc sign executable
LDID="$(which ldid 2>/dev/null || echo "/opt/homebrew/bin/ldid")"
if [ -x "$LDID" ]; then
    echo "Signing binary with ldid..."
    "$LDID" -S"$GUEST_DIR/entitlements.plist" "$APP_DIR/Hello"
else
    echo "Warning: ldid not found. Binary will remain unsigned." >&2
fi

# 6. Remove extended attributes (prevent quarantine / resource forks in guest image)
xattr -cr "$APP_DIR" 2>/dev/null || true

# 7. Package Payload/Hello.app into Hello.ipa
echo "Packaging Hello.ipa..."
"$PYTHON" - <<EOF
import os
import zipfile
from pathlib import Path

payload_dir = Path("$OUTPUT_DIR") / "Payload"
ipa_path = Path("$IPA_PATH")

with zipfile.ZipFile(ipa_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(payload_dir):
        for f in files:
            full_path = Path(root) / f
            rel_path = full_path.relative_to(Path("$OUTPUT_DIR"))
            # Strip extended attributes and preserve unix file permissions (0755 for exec, 0644 for others)
            st = os.stat(full_path)
            zinfo = zipfile.ZipInfo(str(rel_path))
            zinfo.external_attr = (st.st_mode & 0xFFFF) << 16
            with open(full_path, "rb") as fp:
                zf.writestr(zinfo, fp.read())

print("Created " + str(ipa_path) + " (" + str(ipa_path.stat().st_size) + " bytes)")
EOF

xattr -c "$IPA_PATH" 2>/dev/null || true
echo "Build complete: $IPA_PATH"
