"""Schema 1 CLI events shared with the GUI. No human text belongs on stdout."""
import json
import sys
import threading
from enum import Enum


class StageID(str, Enum):
    PREFLIGHT = 'preflight'
    EXTRACT = 'extract'
    VERIFY = 'verify'
    KERNEL = 'decode-kernel'
    IBOOT = 'decode-ibot'
    DEVICETREE = 'decode-devicetree'
    DECRYPT = 'decrypt-rootfs'
    CONVERT = 'convert-rootfs'
    DATA = 'create-data'
    MANIFEST = 'manifest'


class Emitter:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self.finished = False

    def emit(self, event, **fields):
        with self._lock:
            if self.finished:
                raise ValueError('Cannot emit after final result')
            value = {'schema': 1, 'event': event, **fields}
            validate_event(value)
            self.stream.write(json.dumps(value) + '\n')
            self.stream.flush()
            self.finished = event == 'result'

    def stage(self, id, status, message='', fraction=None, hint=None):
        self.emit('stage', id=id.value if isinstance(id, Enum) else id,
                  status=status, fraction=fraction, message=message, hint=hint)

    def log(self, message):
        self.emit('log', message=message)

    def result(self, ok, data):
        self.emit('result', ok=ok, data=data)

    def error(self, message, hint):
        self.emit('error', message=message, hint=hint)


def validate_event(value):
    if value.get('schema') != 1 or value.get('event') not in ('stage', 'log', 'result', 'error'):
        raise ValueError('Invalid progress event')
    event = value['event']
    if event == 'stage':
        if not isinstance(value.get('id'), str) or value.get('status') not in ('start', 'progress', 'done', 'error', 'skipped'):
            raise ValueError('Invalid stage')
        fraction = value['fraction']
        if fraction is not None and (isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1):
            raise ValueError('Invalid fraction')
        if 'hint' not in value or (value['hint'] is not None and not isinstance(value['hint'], str)):
            raise ValueError('Invalid hint')
    if event in ('stage', 'log', 'error') and not isinstance(value.get('message'), str):
        raise ValueError('Missing message')
    if event == 'error' and not isinstance(value.get('hint'), str):
        raise ValueError('Missing error hint')
    if event == 'result' and (not isinstance(value.get('ok'), bool) or 'data' not in value):
        raise ValueError('Invalid result')
    return value


def parse_ndjson(text):
    return [validate_event(json.loads(line)) for line in text.splitlines() if line.strip()]


def error_hint(error):
    text = str(error).lower()
    if 'qemu' in text:
        return 'Install qemu with: brew install qemu'
    if any(word in text for word in ('openssl', 'brew', 'clang')):
        return 'Install build tools: xcode-select --install; brew install openssl@3'
    if 'xcode' in text or 'swift' in text:
        return 'Install Xcode Command Line Tools: xcode-select --install'
    if 'setup failed' in text or 'prerequisite check failed' in text:
        return 'Read the setup log, run setup check --json, and retry the indicated install command.'
    if 'lock' in text or 'running' in text:
        return 'Stop the VM and wait for its launcher to exit, then retry.'
    return 'Check the IPSW path, available disk space and setup check --json, then retry.'
