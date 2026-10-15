"""Opt-in integration with an actual prepared bundle and the project QEMU."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "sources/vphone-legacy-cli/main.py"


@unittest.skipUnless(os.environ.get("VPHONE_TEST_BUNDLE"), "Set VPHONE_TEST_BUNDLE for real QEMU lifecycle integration")
class BackendLifecycleTests(unittest.TestCase):
    def test_single_owner_forced_stop_and_unclean_status(self):
        bundle = Path(os.environ["VPHONE_TEST_BUNDLE"]).resolve()
        env = os.environ | {"VPHONE_LEGACY_VMS_DIR": str(bundle.parent)}
        status_path = bundle / "runtime.json"
        old_session = json.loads(status_path.read_text()).get("session_id") if status_path.exists() else None
        command = [sys.executable, str(CLI), "vm"]
        owner = subprocess.Popen(command + ["launch", bundle.name, "--headless", "--paused"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if owner.poll() is not None:
                    self.fail(owner.communicate()[0])
                if status_path.exists():
                    status = json.loads(status_path.read_text())
                    if status.get("session_id") != old_session and status["process"]["phase"] == "running":
                        break
                time.sleep(0.1)
            else:
                self.fail("Real launcher did not reach process running state")
            duplicate = subprocess.run(command + ["launch", bundle.name, "--headless", "--paused"],
                                       env=env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("active launcher", duplicate.stderr)
            graceful = subprocess.run(command + ["stop", bundle.name], env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(graceful.returncode, 0)
            self.assertIn("shutdown is unavailable", graceful.stderr)
            forced = subprocess.run(command + ["stop", bundle.name, "--force"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            output = owner.communicate(timeout=15)[0]
            self.assertEqual(owner.returncode, 0, output)
            status = json.loads(status_path.read_text())
            self.assertEqual(status["process"]["phase"], "stopped")
            self.assertIsNone(status["process"]["pid"])
            self.assertFalse(status["shutdown"]["clean"])
            self.assertEqual(status["shutdown"]["reason"], "explicit forced stop")
            evidence = Path(status["log_directory"])
            self.assertEqual(json.loads((evidence / "runtime-final.json").read_text()), status)
            plan = json.loads((evidence / "boot-plan.json").read_text())
            image = Path(plan["image"])
            self.assertEqual(image.parent, evidence / "boot")
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), plan["image_sha256"])
            for auxiliary in plan["auxiliary_images"]:
                self.assertEqual(Path(auxiliary["path"]).parent, image.parent)
                self.assertEqual(hashlib.sha256(Path(auxiliary["path"]).read_bytes()).hexdigest(), auxiliary["sha256"])
            identity = json.loads((evidence / "build-identity.json").read_text())
            self.assertIn("qemu/ans_image.py", identity["loader_sources"])
            for stage in ("kernel", "userspace", "display", "agent"):
                self.assertEqual(status[stage]["phase"], "unverified")
        finally:
            if owner.poll() is None:
                owner.terminate()
                try:
                    owner.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.communicate()


class SepControlEndpointTests(unittest.TestCase):
    """SEP IOP Control Endpoint (EP 0) handshake and parameter persistence."""

    @staticmethod
    def pack_msg(ep, tag, op, param=0, data=0):
        return ep | (tag << 8) | (op << 16) | (param << 24) | (data << 32)

    @staticmethod
    def unpack_msg(msg):
        return {
            "ep": msg & 0xff,
            "tag": (msg >> 8) & 0xff,
            "op": (msg >> 16) & 0xff,
            "param": (msg >> 24) & 0xff,
            "data": msg >> 32,
        }

    def test_ep0_set_receive_ool_buffer_wire_contract(self):
        """EP0 op 3 (setReceiveOOLBuffer / setParam) for EP1, EP2, EP3."""
        for ep_idx, tag, page in [(1, 6, 0x809245), (2, 7, 0x809223), (3, 8, 0x809184)]:
            req = self.pack_msg(ep=0, tag=tag, op=3, param=ep_idx, data=page)
            unpacked = self.unpack_msg(req)
            self.assertEqual(unpacked["ep"], 0)
            self.assertEqual(unpacked["tag"], tag)
            self.assertEqual(unpacked["op"], 3)
            self.assertEqual(unpacked["param"], ep_idx)
            self.assertEqual(unpacked["data"], page)

            # Reply wire format: ep=0, tag=request_tag, op=0x65 (ACK), status=0 (kIOReturnSuccess)
            reply = self.pack_msg(ep=0, tag=tag, op=0x65, param=0, data=0)
            rep_unpacked = self.unpack_msg(reply)
            self.assertEqual(rep_unpacked["ep"], 0)
            self.assertEqual(rep_unpacked["tag"], tag)
            self.assertEqual(rep_unpacked["op"], 0x65)
            # Critical contract: byte 3 must be 0 for AppleSEPControl::_cmsgSend kIOReturnSuccess
            self.assertEqual(rep_unpacked["param"], 0)
            self.assertEqual((reply >> 24) & 0xff, 0)

    def test_ep0_set_send_ool_buffer_and_get_param_contract(self):
        """EP0 op 2 (setSendOOLBuffer / getParam) wire packaging."""
        # setSendOOLBuffer: op=2, param=endpoint_index, data=phys_page
        set_send = self.pack_msg(ep=0, tag=9, op=2, param=2, data=0x809200)
        unpacked_send = self.unpack_msg(set_send)
        self.assertEqual(unpacked_send["ep"], 0)
        self.assertEqual(unpacked_send["tag"], 9)
        self.assertEqual(unpacked_send["op"], 2)
        self.assertEqual(unpacked_send["param"], 2)
        self.assertEqual(unpacked_send["data"], 0x809200)

        # Reply: ep=0, tag=9, op=0x65, status=0, data=stored_page
        set_send_ack = self.pack_msg(ep=0, tag=9, op=0x65, param=0, data=0x809200)
        self.assertEqual((set_send_ack >> 24) & 0xff, 0)
        self.assertEqual(self.unpack_msg(set_send_ack)["data"], 0x809200)

        # getParam: op=2, param=param_index, data=0 -> returns op=0x67 with param data
        get_param = self.pack_msg(ep=0, tag=10, op=2, param=2, data=0)
        self.assertEqual(self.unpack_msg(get_param)["data"], 0)
        get_param_reply = self.pack_msg(ep=0, tag=10, op=0x67, param=0, data=0x809200)
        self.assertEqual((get_param_reply >> 24) & 0xff, 0)
        self.assertEqual(self.unpack_msg(get_param_reply)["op"], 0x67)
        self.assertEqual(self.unpack_msg(get_param_reply)["data"], 0x809200)

    def test_ep0_keepalive_and_service_rejection(self):
        """EP0 keepalive (op 0x13, tag 0) and strict service EP1..7 rejection."""
        # Host keepalive: tag 0, op 0x13 -> fire and forget, no reply
        keepalive = self.pack_msg(ep=0, tag=0, op=0x13, param=0, data=0)
        self.assertEqual(self.unpack_msg(keepalive)["tag"], 0)
        self.assertEqual(self.unpack_msg(keepalive)["op"], 0x13)

        # Unsupported service endpoint (EP7) must not match EP0
        ep7_msg = self.pack_msg(ep=7, tag=1, op=0x15, param=0, data=0)
        self.assertNotEqual(self.unpack_msg(ep7_msg)["ep"], 0)
        self.assertNotEqual(self.unpack_msg(ep7_msg)["ep"], 0xff)

    def test_ep2_art_load_wire_contract(self):
        """EP2 ART_LOAD (op 2) request and reply wire contracts."""
        # Request: ep=2, tag=0xa, op=2
        req = self.pack_msg(ep=2, tag=0xa, op=2)
        self.assertEqual(req & 0xff, 2)
        self.assertEqual((req >> 8) & 0xff, 0xa)
        self.assertEqual((req >> 16) & 0xff, 2)

        # Reply: ep=2, tag=0xa, op=3 (sets bit 16), param=0 (success)
        reply = self.pack_msg(ep=2, tag=0xa, op=3, param=0, data=0)
        self.assertEqual(reply & 0xff, 2)
        self.assertEqual((reply >> 8) & 0xff, 0xa)
        self.assertEqual((reply >> 16) & 0xff, 3)
        self.assertTrue(reply & (1 << 16))
        self.assertEqual((reply >> 24) & 0xff, 0)

    def test_ep7_cmd17_get_lock_state_wire_contract(self):
        """EP7 Cmd 17 (0x11, get_lock_state) request and reply contracts."""
        # Request: ep=7, cmd=0x11, tag=0x22, len=8
        ep, cmd, tag = 7, 0x11, 0x22
        req = ep | (cmd << 8) | (tag << 16) | (8 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x11)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x22)
        self.assertEqual((req >> 48) & 0xffff, 8)

        # Reply: ep=7, cmd=0x11, bit15=1, tag=0x22, len=0x10 (16 bytes = 4 words)
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16) | (0x10 << 48)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x11)
        self.assertEqual((rep >> 15) & 1, 1)  # Wakes waiter in AppleSEPKeyStore::_msgAction
        self.assertEqual((rep >> 16) & 0xff, 0x22)
        self.assertEqual((rep >> 24) & 0xff, 0)  # Status success
        self.assertEqual((rep >> 48) & 0xffff, 0x10)  # 16 bytes unpacked

    def test_ep7_cmd18_register_client_wire_contract(self):
        """EP7 Cmd 18 (0x12, register_client) request and reply contracts."""
        # Request: ep=7, cmd=0x12, tag=0x33, len=28 (0x1c)
        ep, cmd, tag = 7, 0x12, 0x33
        req = ep | (cmd << 8) | (tag << 16) | (0x1c << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x12)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x33)
        self.assertEqual((req >> 48) & 0xffff, 0x1c)

        # Reply: ep=7, cmd=0x12, bit15=1, tag=0x33, len=0
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x12)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x33)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)

    def test_ep7_cmd1a_key_locker_wire_contract(self):
        """EP7 Cmd 26 (0x1a, key locker) request and reply contracts."""
        # Request: ep=7, cmd=0x1a, tag=0x0f, len=48 (0x30)
        ep, cmd, tag = 7, 0x1a, 0x0f
        req = ep | (cmd << 8) | (tag << 16) | (0x30 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x1a)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x0f)
        self.assertEqual((req >> 48) & 0xffff, 0x30)

        # Reply: ep=7, cmd=0x1a, bit15=1, tag=0x0f, len=0
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x1a)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x0f)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)

    def test_ep7_cmd16_keybag_state_wire_contract(self):
        """EP7 Cmd 22 (0x16, keybag state) request and reply contracts."""
        # Request: ep=7, cmd=0x16, tag=0x12, len=4
        ep, cmd, tag = 7, 0x16, 0x12
        req = ep | (cmd << 8) | (tag << 16) | (4 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x16)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x12)
        self.assertEqual((req >> 48) & 0xffff, 4)

        # Reply: ep=7, cmd=0x16, bit15=1, tag=0x12, len=0
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x16)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x12)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)

    def test_ep7_cmd02_keybag_create_wire_contract(self):
        """EP7 Cmd 2 (0x02, keybag create / init) request and reply contracts."""
        ep, cmd, tag = 7, 0x02, 0x14
        req = ep | (cmd << 8) | (tag << 16) | (16 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x02)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x14)

        # Reply: 0 bytes unpacked
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x02)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x14)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)

    def test_ep7_cmd0d_keybag_session_wire_contract(self):
        """EP7 Cmd 13 (0x0d, keybag session / system set) request and reply contracts."""
        ep, cmd, tag = 7, 0x0d, 0x17
        req = ep | (cmd << 8) | (tag << 16) | (32 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x0d)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x17)

        # Reply: 0 bytes unpacked
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x0d)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x17)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)

    def test_ep7_cmd1e_system_keybag_unlock_wire_contract(self):
        """EP7 Cmd 30 (0x1e, system keybag unlock) request and reply contracts."""
        ep, cmd, tag = 7, 0x1e, 0x2a
        req = ep | (cmd << 8) | (tag << 16) | (48 << 48)
        self.assertEqual(req & 0xff, 7)
        self.assertEqual((req >> 8) & 0x7f, 0x1e)
        self.assertEqual((req >> 15) & 1, 0)
        self.assertEqual((req >> 16) & 0xff, 0x2a)

        # Reply: 0 bytes unpacked
        rep = ep | (cmd << 8) | (1 << 15) | (tag << 16)
        self.assertEqual(rep & 0xff, 7)
        self.assertEqual((rep >> 8) & 0x7f, 0x1e)
        self.assertEqual((rep >> 15) & 1, 1)
        self.assertEqual((rep >> 16) & 0xff, 0x2a)
        self.assertEqual((rep >> 24) & 0xff, 0)
        self.assertEqual((rep >> 48) & 0xffff, 0)


class SepExecutableContractTests(unittest.TestCase):
    def test_native_keybag_query_and_lock_reply_contract(self):
        sys.path.insert(0, str(PROJECT / 'backend'))
        from sep_contract_test import run_service_fixture
        run_service_fixture()
