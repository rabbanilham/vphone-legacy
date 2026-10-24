#!/usr/bin/env python3
"""Read-only publication triage. Emits locations/categories, never matched secrets.

Scans working tracked files and unique reachable Git blobs. This bounded pattern
scan is not a credential clearance or a substitute for provenance review.
"""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'service_token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{32,})'),
    'credential_assignment': re.compile(rb'(?i)(?:api[_-]?key|access[_-]?token|password|secret[_-]?key)\s*[=:]\s*[\"\x27][^\"\x27\r\n]{8,}[\"\x27]'),
    'personal_path': re.compile(rb'/Users/[A-Za-z0-9_.-]+/'),
    'firmware_key_field': re.compile(rb'(?:rootfs_key|kernel_key|iboot_key|devicetree_key)'),
}
MEDIA = {'.ipsw', '.dmg', '.raw', '.qcow2', '.im4p', '.img4', '.bin', '.pcap', '.pcapng'}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def scan(data):
    return [name for name, pattern in PATTERNS.items() if pattern.search(data)]


def main():
    working = []
    for raw in git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0'):
        if not raw:
            continue
        path = raw.decode()
        file = ROOT / path
        if not file.is_file():
            continue
        categories = scan(file.read_bytes())
        if file.suffix.lower() in MEDIA:
            categories.append('guest_or_capture_extension')
        if file.stat().st_size > 10 * 1024 * 1024:
            categories.append('large_blob')
        if categories:
            working.append({'path': path, 'categories': categories})
    objects = git('rev-list', '--objects', '--all').decode().splitlines()
    names = dict(line.split(' ', 1) for line in objects if ' ' in line)
    metadata = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'],
                              input='\n'.join(names).encode(), capture_output=True, check=True).stdout.decode().splitlines()
    blobs = [line.split() for line in metadata if ' blob ' in line]
    process = subprocess.Popen(['git', '-C', str(ROOT), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    history = []
    for oid, _, size in blobs:
        process.stdin.write((oid + '\n').encode())
        process.stdin.flush()
        process.stdout.readline()
        data = process.stdout.read(int(size))
        process.stdout.read(1)
        categories = scan(data)
        if Path(names[oid]).suffix.lower() in MEDIA:
            categories.append('guest_or_capture_extension')
        if int(size) > 10 * 1024 * 1024:
            categories.append('large_blob')
        if categories:
            history.append({'blob': oid, 'path': names[oid], 'bytes': int(size), 'categories': categories})
    process.stdin.close()
    process.wait()
    identities = set(git('log', '--all', '--format=%ae%n%ce').decode().splitlines())
    report = {'reachable_commits': len(git('rev-list', '--all').splitlines()), 'unique_blobs': len(blobs),
              'commit_email_identity_count': len(identities), 'working_findings': working, 'history_findings': history}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
