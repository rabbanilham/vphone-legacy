#!/bin/zsh
# Create a project-local Python environment on macOS.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
fail() { print -u2 -- "Error: $*"; exit 1; }
[[ "$(uname -s)" == Darwin ]] || fail "macOS is required for this setup script."
command -v xcrun >/dev/null || fail "Install Xcode Command Line Tools: xcode-select --install"
xcrun --find clang >/dev/null || fail "Select a working Xcode/Command Line Tools installation."
SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
if [[ -n "${VPHONE_PYTHON:-}" ]]; then
    PYTHON="${VPHONE_PYTHON}"
elif command -v python3.13 >/dev/null; then
    PYTHON="$(command -v python3.13)"
else
    PYTHON="$(command -v python3 || true)"
fi
[[ -n "${PYTHON}" ]] || fail "Install Python 3.11 or newer."
"${PYTHON}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "Python 3.11+ is required; set VPHONE_PYTHON to its executable."
if [[ -n "${VPHONE_KEYSTONE_PREFIX:-}" ]]; then
    KEYSTONE_PREFIX="${VPHONE_KEYSTONE_PREFIX}"
else
    command -v brew >/dev/null || fail "Install Homebrew and run: brew install keystone"
    KEYSTONE_PREFIX="$(brew --prefix)/opt/keystone"
fi
KEYSTONE_STATIC="${KEYSTONE_PREFIX}/lib/libkeystone.a"
[[ -f "${KEYSTONE_STATIC}" ]] || fail "Missing ${KEYSTONE_STATIC}; run brew install keystone or set VPHONE_KEYSTONE_PREFIX."
"${PYTHON}" -m venv "${VENV_DIR}"
VENV_PY="${VENV_DIR}/bin/python"
"${VENV_PY}" -m pip install -r "${PROJECT_ROOT}/requirements.txt"
KS_PKG_DIR="$("${VENV_PY}" -c 'import importlib.util; from pathlib import Path; print(Path(importlib.util.find_spec("keystone").origin).parent)')"
xcrun clang -shared -isysroot "${SDK_PATH}" -o "${KS_PKG_DIR}/libkeystone.dylib" \
    -Wl,-all_load "${KEYSTONE_STATIC}" -lc++ -install_name @rpath/libkeystone.dylib
"${VENV_PY}" -c 'from capstone import Cs; from keystone import Ks; from pyimg4 import IM4P; import Crypto; print("Dependency imports OK")'
print -- "Environment ready: ${VENV_PY}"
