# Makefile for vphone iOS 7 guest Hello application
CC ?= xcrun clang
SDK_IOS ?= $(shell xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)
PYTHON ?= ../../.venv/bin/python
ifeq ($(wildcard $(PYTHON)),)
PYTHON := python3
endif

TARGET_IOS ?= -target arm64-apple-ios7.0
# Note: Modern Xcode SDKs dropped libarclite, so we use MRC (-fno-objc-arc)
# to avoid runtime dependency on missing libarclite.
CFLAGS ?= -Wall -O2 -fno-objc-arc
FRAMEWORKS ?= -framework UIKit -framework Foundation -framework CoreGraphics -framework QuartzCore

BUILD_DIR ?= ../../build/sideload
PAYLOAD_DIR ?= $(BUILD_DIR)/Payload
APP_DIR ?= $(PAYLOAD_DIR)/Hello.app
IPA ?= $(BUILD_DIR)/Hello.ipa
LDID ?= $(shell which ldid 2>/dev/null || echo "/opt/homebrew/bin/ldid")

SRCS := src/main.m src/AppDelegate.m src/ViewController.m

.PHONY: all app ipa icons clean

all: ipa

icons:
	@mkdir -p assets
	$(PYTHON) generate_icons.py --output assets

app: icons $(SRCS) Info.plist entitlements.plist
	@if [ -z "$(SDK_IOS)" ]; then \
		echo "Error: iPhoneOS SDK not available" >&2; exit 1; \
	fi
	@mkdir -p $(APP_DIR)
	@echo "Compiling Hello for iOS arm64..."
	$(CC) $(CFLAGS) $(TARGET_IOS) -isysroot "$(SDK_IOS)" $(FRAMEWORKS) -o $(APP_DIR)/Hello $(SRCS)
	@cp Info.plist $(APP_DIR)/Info.plist
	@cp assets/AppIcon60x60@2x.png $(APP_DIR)/
	@cp assets/Icon-60@2x.png $(APP_DIR)/
	@cp assets/AppIcon57x57.png $(APP_DIR)/
	@cp assets/Icon.png $(APP_DIR)/
	@printf 'APPL????' > $(APP_DIR)/PkgInfo
	@if [ -x "$(LDID)" ]; then \
		echo "Signing Hello with ldid..."; \
		$(LDID) -Sentitlements.plist $(APP_DIR)/Hello; \
	fi
	@xattr -cr $(APP_DIR) 2>/dev/null || true

ipa:
	@../../scripts/build_hello_ipa.sh

clean:
	rm -rf $(BUILD_DIR) assets/*.png
