#!/bin/zsh
# setup_venv.sh — Create a self-contained Python venv for vphone-legacy.
#
# Installs all dependencies including capstone, keystone, pyimg4, and pycryptodome.
# Requires: python3.13, clang, Homebrew keystone (brew install keystone)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
REQUIREMENTS="${PROJECT_ROOT}/requirements.txt"

# Prefer python3.13 if available, fallback to python3
if command -v /opt/homebrew/bin/python3.13 &>/dev/null; then
    PYTHON="/opt/homebrew/bin/python3.13"
else
    PYTHON="$(readlink -f "$(which python3)")"
fi

if [[ -z "${PYTHON}" ]]; then
    echo "Error: python3 not found in PATH"
    exit 1
fi

echo "=== Creating vphone-legacy venv ==="
echo "  Python:  ${PYTHON} ($(${PYTHON} --version 2>&1))"
echo "  venv:    ${VENV_DIR}"
echo "  deps:    ${REQUIREMENTS}"
echo ""

# Create venv
"${PYTHON}" -m venv "${VENV_DIR}"

# Activate and install pip packages
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r "${REQUIREMENTS}"

# --- Build keystone native library ---
echo ""
echo "=== Building keystone dylib ==="
KEYSTONE_DIR="/opt/homebrew/Cellar/keystone"
if [[ ! -d "${KEYSTONE_DIR}" ]]; then
    echo "Error: keystone not found. Install with: brew install keystone"
    exit 1
fi
KEYSTONE_STATIC="$(find "${KEYSTONE_DIR}" -name 'libkeystone.a' -type f 2>/dev/null | head -1)"
if [[ -z "${KEYSTONE_STATIC}" ]]; then
    echo "Error: libkeystone.a not found. Install with: brew install keystone"
    exit 1
fi

PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
KS_PKG_DIR="${VENV_DIR}/lib/python${PYVER}/site-packages/keystone"
KS_DYLIB="${KS_PKG_DIR}/libkeystone.dylib"

echo "  static lib: ${KEYSTONE_STATIC}"
echo "  dylib dest: ${KS_DYLIB}"

SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
clang -shared -isysroot "${SDK_PATH}" -o "${KS_DYLIB}" \
    -Wl,-all_load "${KEYSTONE_STATIC}" \
    -lc++ \
    -install_name @rpath/libkeystone.dylib

echo "  dylib built OK"

# --- Verify ---
echo ""
echo "=== Verifying imports ==="
python3 -c "
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN
from keystone import Ks, KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN
from pyimg4 import IM4P
import Crypto
print('  capstone      OK')
print('  keystone      OK')
print('  pyimg4        OK')
print('  pycryptodome  OK')
"

echo ""
echo "=== venv ready ==="
echo "  Activate:   source ${VENV_DIR}/bin/activate"
