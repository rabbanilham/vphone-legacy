#!/usr/bin/env python3
"""Build the pinned, experimental A7 QEMU fork without global installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT / 'backend'

def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='Download pinned upstream source')
    parser.add_argument('--install-build-deps', action='store_true', help='Install Python tools in backend/.venv')
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument('--sdk', type=Path, help='Matching macOS SDK, if host SDK/linker mismatch')
    args = parser.parse_args()
    lock = json.loads((BACKEND / 'source.lock.json').read_text())
    source, build, venv = [BACKEND / x for x in ('upstream', 'build', '.venv')]
    if not source.exists():
        if not args.fetch:
            parser.error('Pinned source missing; run with --fetch (network required)')
        run(['git', 'clone', '--depth', '1', '--branch', lock['tag'], lock['repository'], source])
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != lock['commit']:
        parser.error(f'Upstream commit mismatch: expected {lock["commit"]}, got {actual}')
    if args.install_build_deps:
        if not (venv / 'bin/python3').exists():
            run([sys.executable, '-m', 'venv', venv])
        run([venv / 'bin/python3', '-m', 'pip', 'install', *lock['build_dependencies']])
    if not (venv / 'bin/meson').exists():
        parser.error('Build tools missing; run with --install-build-deps')
    shutil.copyfile(BACKEND / 's5l8960x.c', source / 'hw/arm/s5l8960x.c')
    additions = {
        'hw/arm/meson.build': "\narm_common_ss.add(when: 'CONFIG_S5L8960X', if_true: files('s5l8960x.c'))\n",
        'hw/arm/Kconfig': '\nconfig S5L8960X\n    bool\n    default y\n    depends on TCG && AARCH64\n    select USB_EHCI_SYSBUS\n    select USB_OHCI_SYSBUS\n    select USB_OHCI_SYSBUS\n',
    }
    for relative, addition in additions.items():
        path = source / relative
        original = subprocess.check_output(['git', '-C', str(source), 'show', f'HEAD:{relative}'])
        path.write_bytes(original + addition.encode())
    # Upstream fixes kept in the repo: restore every touched file to the
    # pinned commit first, then apply in order, so rebuilds stay idempotent
    # and later patches may stack on a file an earlier one changed.
    patches = sorted((BACKEND / 'patches').glob('*.patch'))
    touched = sorted({line[6:].strip() for patch in patches
                      for line in patch.read_text().splitlines()
                      if line.startswith('+++ b/')})
    if touched:
        run(['git', '-C', source, 'checkout', '--', *touched])
    for patch in patches:
        run(['git', '-C', source, 'apply', patch])
    env = os.environ.copy()
    env['PATH'] = str(venv / 'bin') + os.pathsep + env.get('PATH', '')
    brew = shutil.which('brew')
    brew_prefix = Path(subprocess.check_output([brew, '--prefix'], text=True).strip()) if brew else None
    pcs = [brew_prefix / 'opt' / lib / 'lib/pkgconfig' for lib in ('glib', 'pixman', 'pcre2')] if brew_prefix else []
    env['PKG_CONFIG_PATH'] = os.pathsep.join(str(p) for p in pcs if p.exists()) + os.pathsep + env.get('PKG_CONFIG_PATH', '')
    configure = [source / 'configure', '--target-list=aarch64-softmmu', '--disable-docs',
        '--disable-user', '--disable-tools', '--disable-werror', '--disable-slirp',
        '--enable-cocoa', '--disable-gtk', '--disable-sdl', '--disable-vnc',
        '--disable-hvf', '--enable-tcg', '--disable-plugins', '--disable-pvg']
    sdk = args.sdk or (BACKEND / 'macos-sdk' if (BACKEND / 'macos-sdk').exists() else None)
    if sdk:
        if not sdk.is_dir():
            parser.error(f'SDK does not exist: {sdk}')
        link = BACKEND / 'macos-sdk'
        if link.resolve() != sdk.resolve():
            if link.is_symlink(): link.unlink()
            if link.exists(): parser.error(f'SDK alias is not a symlink: {link}')
            link.symlink_to(sdk.resolve())
        configure += [f'--extra-cflags=-isysroot {link}', f'--extra-ldflags=-isysroot {link}']
    build.mkdir(exist_ok=True)
    run(configure, cwd=build, env=env)
    run([venv / 'bin/ninja', '-C', build, f'-j{args.jobs}', 'qemu-system-aarch64'], env=env)
    binary = BACKEND / 'bin/qemu-system-aarch64'
    binary.parent.mkdir(exist_ok=True)
    if binary.exists():
        binary.unlink()
    shutil.copy2(build / 'qemu-system-aarch64', binary)
    machines = subprocess.check_output([binary, '-machine', 'help'], text=True)
    if 's5l8960x' not in machines:
        raise RuntimeError('Built binary does not register the s5l8960x machine')
    manifest = {
        'schema_version': 1, 'id': lock['machine'], 'machine': lock['machine'], 'source_commit': actual,
        'source_tag': lock['tag'], 'binary': str(binary.relative_to(PROJECT)),
        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'machine_source_sha256': hashlib.sha256((BACKEND / 's5l8960x.c').read_bytes()).hexdigest(),
        'cpu': 'cortex-a57 prototype; not Apple Cyclone', 'accelerator': 'tcg',
        'boot_levels': [1, 3], 'timebase_frequency': 24000000,
        'ans_storage_model': {
            'explicit_image_properties': True,
            'utility_image_property': True,
            'utility_profile': 'n53ap-11D257-ASP10-utility-v1',
            'utility_format': 'committed model-private logical namespace; native NAND FORMAT15/16 unsupported',
            'utility_commands': ['LLB 30/40', 'firmware 31/41', 'control 34/44',
                                 'effaceable 35/45', 'NVRAM 36/46', 'SysCfg 37/47'],
            'kind': 'experimental synchronous ASP software model, not native IOP execution',
            'images': ['system-image', 'data-image', 'utility-image'],
            'system_writes': 'system-writable=1 requires mutable derivative',
            'commands': ['geometry', 'read', 'write', 'synchronize-cache',
                         'get-time', 'set-timestamp', 'get-initialization-status'],
            'clock': 'calendar/elapsed seconds advance with QEMU virtual time and native timestamp rules; utility epoch from validated committed header, zero when absent',
            'initialization': 'owned backing lengths and boundary-sector reads verified on request; utility readiness requires the committed profile/header and exact container length',
            'unknown_commands': 'native general-error completion',
            'durability': 'successful writes and synchronize-cache fsync owned images',
            'disabled_with_iop_diagnostic': True,
        },
        'ans_iop_diagnostic': {
            'enabled_by_default': False,
            'machine_property': 'iop-diagnostic=1',
            'accelerator': 'tcg,thread=single',
            'cpu': 'cortex-a7 prototype; exact native core unknown',
            'bootstrap_tables': 'provisional scratch at PA 0x04000000..0x04002000',
            'peripherals': 'unsupported accesses fail; no handshake synthesis',
            'timebase_frequency': 24000000,
        },
        'experimental': True, 'capabilities': {
            'raw_kernel_loader': True, 'guest_framebuffer_scanout': True,
            'uart_polled': True, 'aic_v1_single_cpu': True, 'aic_v1_smp': True,
            'max_application_cpus': 2, 'architected_timer': True,
            'cyclone_cpu': False, 'hid_hardware_effects': False, 'storage': False,
            'touch': False, 'buttons': False, 'agx': False, 'ios_boot': False,
            'springboard': False, 'guest_shutdown': False,
            'userspace': False, 'graphics': False, 'input': False,
        },
    }
    (BACKEND / 'backend.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built experimental backend: {binary}\nNo iOS boot compatibility is claimed.')

if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        print(f'Backend build failed: {exc}', file=sys.stderr)
        sys.exit(1)
