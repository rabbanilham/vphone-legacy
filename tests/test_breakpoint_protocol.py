"""Debugger transport failures must terminate instead of hanging a probe."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from breakpoint_probe import rsp, receive_rsp
from display_probe import memory


class FragmentedSocket:
    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = bytearray()

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, size):
        # A stream may split even the two checksum bytes into separate reads.
        result = bytes(self.data[:1])
        del self.data[:1]
        return result


class BreakpointProtocolTests(unittest.TestCase):
    def test_pending_packet_with_consumed_start_is_fragment_safe(self):
        sock=FragmentedSocket(b'T05#b9')
        self.assertEqual(receive_rsp(sock,started=True),'T05');self.assertEqual(sock.sent,b'+')
    def test_fragmented_stop_reply_and_ack(self):
        sock = FragmentedSocket(b"+$T05#b9")
        self.assertEqual(rsp(sock, "c"), "T05")
        self.assertEqual(sock.sent, b"$c#63+")

    def test_disconnect_before_packet_terminates(self):
        with self.assertRaisesRegex(RuntimeError, "before response"):
            rsp(FragmentedSocket(b"+"), "c")

    def test_disconnect_in_packet_or_checksum_terminates(self):
        for data in (b"$T05", b"$T05#b"):
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, "disconnected"):
                rsp(FragmentedSocket(data), "c")

    def test_bad_checksum_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            rsp(FragmentedSocket(b"$T05#00"), "c")

    def test_memory_consumes_queued_stop_without_duplicate_guest_read(self):
        payload=b'01020304'
        packet=b'$'+payload+b'#'+f'{sum(payload)&255:02x}'.encode()
        sock=FragmentedSocket(b'+$T05#b9'+packet)
        self.assertEqual(memory(sock,0x1000,4),bytes([1,2,3,4]))
        self.assertEqual(sock.sent,b'$m1000,4#8e++')

    def test_memory_queued_stop_then_disconnect_terminates(self):
        with self.assertRaisesRegex(RuntimeError,'disconnected'):
            memory(FragmentedSocket(b'$T05#b9'),0x1000,4)
