#!/usr/bin/env python3
"""Removed: the old shim printed synthetic boot logs and never executed XNU."""
import sys

if __name__ == '__main__':
    print('Simulated iBoot shim removed. Use vm create and scripts/qemu/boot_image.py for a real direct-kernel image.', file=sys.stderr)
    sys.exit(1)
