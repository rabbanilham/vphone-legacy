"""Synthetic registry/queue fixtures, with missing memory kept distinct from zero."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from iokit_probe_state import (REGISTRY_ROOT, SERVICE_PLANE, NAME_KEY,
                               capture_iokit, capture_kernel_threads, ABI_SIGNATURES)


class Memory:
    def __init__(self):
        self.bytes = {}
        self.next = 0xffffff8081000000
        self.reads = []

    def alloc(self, size=128):
        a = self.next
        self.next += (size + 255) & ~255
        self.put(a, bytes(size))
        return a

    def put(self, a, b):
        self.bytes.update((a + i, v) for i, v in enumerate(b))

    def number(self, a, v, size=8):
        self.put(a, v.to_bytes(size, 'little'))

    def read(self, a, n):
        self.reads.append((a, n))
        b = bytearray()
        for i in range(n):
            if a+i not in self.bytes:
                break
            b.append(self.bytes[a+i])
        return bytes(b)

    def string(self, s):
        obj, data = self.alloc(), self.alloc()
        self.put(data, s.encode() + b'\0')
        self.number(obj + 0x18, data)
        return obj

    def dictionary(self, pairs):
        obj, data = self.alloc(), self.alloc(max(16, len(pairs) * 16))
        self.number(obj+0x18, data)
        self.number(obj+0x20, len(pairs), 4)
        for i, pair in enumerate(pairs):
            self.put(data+i*16, struct.pack('<QQ', *pair))
        return obj

    def array(self, values):
        obj, data = self.alloc(), self.alloc()
        self.number(obj+0x18, data)
        self.number(obj+0x20, len(values), 4)
        for i, v in enumerate(values):
            self.number(data+i*8, v)
        return obj

    def fixture(self):
        for address, expected in ABI_SIGNATURES.items():
            self.put(address, expected)
        plane = self.alloc()
        child_key, name_key = self.string('IOServiceChildLinks'), self.string('IOServiceName')
        self.number(SERVICE_PLANE, plane)
        self.number(plane+0x20, child_key)
        self.number(plane+0x28, name_key)
        self.number(NAME_KEY, name_key)
        vtable, getter = self.alloc(), self.alloc()
        self.number(vtable+0x38, getter)  # unknown getter must leave class unknown
        root, parent, leaf, idle = [self.alloc() for _ in range(4)]
        for obj, name, children, busy in (
                (root, 'Root', [parent], None),
                (parent, 'AppleARMPE', [leaf, idle], 1),
                (leaf, 'blocked-device', [], 1), (idle, 'idle-device', [], 0)):
            table = self.dictionary([(name_key, self.string(name)),
                                     (child_key, self.array(children))])
            self.number(obj, vtable)
            self.number(obj+0x18, table)
            if busy is not None:
                self.number(obj+0x4c, busy, 1)
        self.number(REGISTRY_ROOT, root)
        return root, parent, leaf, idle


class IOKitStateTests(unittest.TestCase):
    def test_busy_ancestors_and_quiet_sibling(self):
        m = Memory(); root, parent, leaf, idle = m.fixture()
        state = capture_iokit(m.read)
        self.assertTrue(state['complete'], state['errors'])
        self.assertEqual([n['object'] for n in state['busy']], [hex(parent), hex(leaf)])
        self.assertTrue(state['busy'][0]['busy_child'])
        self.assertFalse(state['busy'][1]['busy_child'])
        self.assertEqual(state['busy'][1]['path'], '/Root/AppleARMPE/blocked-device')
        self.assertNotIn((root+0x4c, 1), m.reads)
        self.assertIsNone(state['nodes'][0]['busy'])
        self.assertIsNone(state['busy'][1]['class'])

    def test_unmapped_and_short_reads_cannot_report_quiet(self):
        self.assertFalse(capture_iokit(lambda a, n: b'')['complete'])
        m = Memory(); _, _, leaf, _ = m.fixture()
        del m.bytes[leaf+0x4c]
        state = capture_iokit(m.read)
        self.assertFalse(state['complete'])
        self.assertTrue(state['errors'])
        self.assertNotIn('busy', next(n for n in state['nodes'] if n['object'] == hex(leaf)))

    def test_wrong_kernel_is_rejected_before_walking_registry(self):
        m = Memory(); m.fixture()
        address = next(iter(ABI_SIGNATURES))
        m.put(address, b'\xff')
        state = capture_iokit(m.read)
        self.assertFalse(state['complete'])
        self.assertEqual(state['nodes'], [])
        self.assertIn('ABI mismatch', state['errors'][0])

    def test_limits_prevent_unbounded_debugger_reads(self):
        m = Memory(); m.fixture()
        state = capture_iokit(m.read, max_nodes=2)
        self.assertFalse(state['complete'])
        self.assertEqual(len(state['nodes']), 2)
        self.assertIn('node limit', state['errors'][0])
        self.assertFalse(capture_iokit(m.read, max_entries=1)['complete'])

    def test_kernel_thread_link_is_thread_base_and_frames_are_bounded(self):
        m = Memory(); task = m.alloc(); thread = m.alloc(0x780)
        context = m.alloc(0x110); frame = m.alloc()
        m.number(0xffffff8000694008, task)
        m.number(task+0x48, thread); m.number(thread+0x308, task+0x48)
        m.number(thread+0x740, 123); m.number(thread+0x20, 0xdead)
        m.number(thread+0x680, context); m.number(context+0xf0, frame)
        m.number(frame, frame)  # corrupt/self-linked frame chain must stop
        m.number(frame+8, 0xffffff8000f0eb58)
        state = capture_kernel_threads(m.read)
        self.assertTrue(state['complete'])
        self.assertEqual(state['threads'][0]['tid'], 123)
        self.assertEqual(len(state['threads'][0]['frames']), 1)
        self.assertFalse(capture_kernel_threads(m.read, max_threads=0)['complete'])
        m.number(thread+0x308, thread)
        self.assertFalse(capture_kernel_threads(m.read)['complete'])

    def test_metaclass_getter_decodes_only_known_instruction_shape(self):
        m = Memory(); _, _, leaf, _ = m.fixture()
        getter = m.alloc(); meta = m.alloc(); vtable = m.alloc()
        m.number(leaf, vtable); m.number(vtable+0x38, getter)
        delta = (meta >> 12) - (getter >> 12)
        adrp = 0x90000000 | ((delta & 3) << 29) | ((delta >> 2 & 0x7ffff) << 5)
        add = 0x91000000 | ((meta & 4095) << 10)
        m.put(getter, struct.pack('<III', adrp, add, 0xd65f03c0))
        m.number(meta+0x18, m.string('BlockedDriver'))
        state = capture_iokit(m.read)
        self.assertEqual(state['busy'][1]['class'], 'BlockedDriver')
