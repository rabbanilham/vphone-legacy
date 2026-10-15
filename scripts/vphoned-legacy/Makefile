# Makefile for vphoned-legacy
CC ?= xcrun clang
SDK_IOS ?= $(shell xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)
SDK_MAC ?= $(shell xcrun --sdk macosx --show-sdk-path 2>/dev/null)

TARGET_IOS ?= -target arm64-apple-ios7.0
CFLAGS ?= -Wall -O2 -fobjc-arc

all: ios

ios:
	@if [ -n "$(SDK_IOS)" ]; then \
		echo "Building vphoned-legacy for iOS arm64..."; \
		$(CC) $(CFLAGS) $(TARGET_IOS) -isysroot "$(SDK_IOS)" -framework Foundation -o vphoned_ios vphoned_legacy.m; \
		ldid -S vphoned_ios 2>/dev/null || true; \
	else \
		echo "iOS SDK not available, skipping iOS build"; \
	fi

host_test:
	@echo "Building vphoned-legacy for host testing (macOS)..."
	$(CC) $(CFLAGS) -isysroot "$(SDK_MAC)" -framework Foundation -o vphoned_host vphoned_legacy.m

clean:
	rm -f vphoned_ios vphoned_host
