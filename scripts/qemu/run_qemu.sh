#!/bin/zsh
# Own QEMU through the runtime supervisor; never fall back to generic virt/shim.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
if (( $# < 1 )); then
    print -u2 'Usage: run_qemu.sh VM_BUNDLE [--headless] [--probe-seconds N]'
    exit 1
fi
exec "${PROJECT_ROOT}/.venv/bin/python" "${SCRIPT_DIR}/runtime.py" "$@"
