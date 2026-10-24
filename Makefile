# Experimental iOS 7.1.2 emulator bring-up

PYTHON ?= .venv/bin/python

.PHONY: all setup test check-docs backend vphoned gui clean help

all: setup test

setup:
	@echo "Setting up vphone-legacy virtual environment..."
	./scripts/setup_venv.sh

test:
	@echo "Running test suite..."
	$(PYTHON) -m unittest discover -s tests -v

check-docs:
	$(PYTHON) scripts/check_docs.py

backend:
	$(PYTHON) scripts/qemu/build_backend.py

vphoned:
	@echo "Building vphoned-legacy..."
	make -C scripts/vphoned-legacy host_test
	make -C scripts/vphoned-legacy ios

gui:
	@echo "Building and launching vphone-legacy.app..."
	./scripts/build_app.sh
	open dist/vphone-legacy.app

clean:
	rm -rf .venv/build build/swift-module-cache
	make -C scripts/vphoned-legacy clean

help:
	@echo "vphone-legacy — Experimental n53ap iOS 7.1.2 emulator bring-up"
	@echo ""
	@echo "Targets:"
	@echo "  make setup     - Create Python venv and compile keystone dylib"
	@echo "  make test      - Run automated test suite"
	@echo "  make check-docs - Check maintained documentation links"
	@echo "  make backend   - Build pinned experimental s5l8960x QEMU"
	@echo "  make vphoned   - Build vphoned-legacy daemon"
	@echo "  make clean     - Remove build artifacts"
	@echo ""
	@echo "CLI Usage:"
	@echo "  ./vphone-legacy fw list"
	@echo "  ./vphone-legacy vm create my_ios7 --ios 7.1.2 --ipsw FILE"
	@echo "  ./vphone-legacy vm doctor my_ios7"
	@echo "  ./vphone-legacy vm list"
	@echo "  ./vphone-legacy vm launch my_ios7"
