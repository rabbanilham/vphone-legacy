"""Test Swift GUI contract decoding against docs/gui-contract.md and tests/fixtures/gui/*."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_CACHE = Path(tempfile.gettempdir()) / f"vphone-mc-{ROOT.name}"
MODULE_CACHE.mkdir(exist_ok=True)


class GUIContractDecodingTests(unittest.TestCase):
    def test_swift_contract_decoding_and_fixture_client(self):
        """Compile Contract.swift, CLIClient.swift, FixtureCLIClient.swift and verify decoding of all fixtures."""
        contract_source = (ROOT / "sources/vphone-legacy-gui/Models/Contract.swift").read_text()
        cli_client_source = (ROOT / "sources/vphone-legacy-gui/Services/CLIClient.swift").read_text()
        fixture_client_source = (ROOT / "sources/vphone-legacy-gui/Services/FixtureCLIClient.swift").read_text()

        fixtures_dir = ROOT / "tests/fixtures/gui"
        fixtures_dir_escaped = str(fixtures_dir).replace('\\', '\\\\').replace('"', '\\"')

        harness = f"""
import Foundation

func runContractTests() async throws {{
    let fixturesDir = URL(fileURLWithPath: "{fixtures_dir_escaped}")

    // 1. Test progress-ok.ndjson decoding
    let okPath = fixturesDir.appendingPathComponent("progress-ok.ndjson")
    let okText = try String(contentsOf: okPath, encoding: .utf8)
    let okEvents = ProgressEvent.decodeLines(from: okText)
    assert(okEvents.count == 3, "Expected 3 events in progress-ok.ndjson, got \\(okEvents.count)")

    assert(okEvents[0].schema == 1)
    assert(okEvents[0].event == "stage")
    assert(okEvents[0].id == "extract")
    assert(okEvents[0].status == "done")
    assert(okEvents[0].fraction == 1.0)
    assert(okEvents[0].message == "Extracted 5 files")
    assert(okEvents[0].hint == nil)

    assert(okEvents[1].schema == 1)
    assert(okEvents[1].event == "stage")
    assert(okEvents[1].id == "manifest")
    assert(okEvents[1].status == "done")
    assert(okEvents[1].fraction == 1.0)
    assert(okEvents[1].message == "Wrote vm_manifest.json")
    assert(okEvents[1].hint == nil)

    assert(okEvents[2].schema == 1)
    assert(okEvents[2].event == "result")
    assert(okEvents[2].ok == true)
    assert(okEvents[2].data?["name"] == .string("ios712"))
    assert(okEvents[2].data?["path"] == .string("/Users/me/.vphone-legacy/VMs/ios712"))

    // 2. Test progress-error.ndjson decoding (including null hint and error event)
    let errPath = fixturesDir.appendingPathComponent("progress-error.ndjson")
    let errText = try String(contentsOf: errPath, encoding: .utf8)
    let errEvents = ProgressEvent.decodeLines(from: errText)
    assert(errEvents.count == 6, "Expected 6 events in progress-error.ndjson, got \\(errEvents.count)")

    assert(errEvents[0].event == "stage")
    assert(errEvents[0].id == "preflight")
    assert(errEvents[0].status == "start")
    assert(errEvents[0].fraction == nil)
    assert(errEvents[0].hint == nil)

    assert(errEvents[1].event == "stage")
    assert(errEvents[1].id == "preflight")
    assert(errEvents[1].status == "done")
    assert(errEvents[1].fraction == 1.0)

    assert(errEvents[2].event == "stage")
    assert(errEvents[2].id == "decrypt-rootfs")
    assert(errEvents[2].status == "progress")
    assert(errEvents[2].fraction == 0.4)

    assert(errEvents[3].event == "log")
    assert(errEvents[3].message == "vfdecrypt: 1.46 GB processed")

    assert(errEvents[4].event == "stage")
    assert(errEvents[4].id == "convert-rootfs")
    assert(errEvents[4].status == "error")
    assert(errEvents[4].isError == true)
    assert(errEvents[4].fraction == nil)
    assert(errEvents[4].message == "qemu-img failed")
    assert(errEvents[4].hint == "Install qemu with: brew install qemu")

    assert(errEvents[5].event == "result")
    assert(errEvents[5].ok == false)
    assert(errEvents[5].data?["exit_code"] == .int(1))

    // 3. Test setup-check.json decoding
    let setupPath = fixturesDir.appendingPathComponent("setup-check.json")
    let setupData = try Data(contentsOf: setupPath)
    let setup = try SetupCheckResponse.decode(from: setupData)
    assert(setup.schema == 1)
    assert(setup.ok == false)
    assert(setup.items.count == 5)

    assert(setup.items[0].id == "xcode-clt")
    assert(setup.items[0].status == "ok")
    assert(setup.items[0].isOk == true)
    assert(setup.items[0].fix == nil)

    assert(setup.items[1].id == "homebrew")
    assert(setup.items[1].status == "ok")
    assert(setup.items[1].isOk == true)

    assert(setup.items[2].id == "brew-packages")
    assert(setup.items[2].status == "fixable")
    assert(setup.items[2].isFixable == true)
    assert(setup.items[2].fix?.kind == "auto")
    assert(setup.items[2].fix?.command == "brew install keystone libslirp")
    assert(setup.items[2].fix?.instructions == nil)

    assert(setup.items[3].id == "backend")
    assert(setup.items[3].status == "missing")
    assert(setup.items[3].isMissing == true)
    assert(setup.items[3].fix?.kind == "auto")
    assert(setup.items[3].fix?.command == "scripts/qemu/build_backend.py --fetch --install-build-deps")

    assert(setup.items[4].id == "wifi-helper")
    assert(setup.items[4].status == "missing")
    assert(setup.items[4].fix?.kind == "auto")
    assert(setup.items[4].fix?.command == "make wifi-helper")

    // 4. Test vm-list.json decoding
    let vmListPath = fixturesDir.appendingPathComponent("vm-list.json")
    let vmListData = try Data(contentsOf: vmListPath)
    let vmList = try VMListResponse.decode(from: vmListData)
    assert(vmList.schema == 1)
    assert(vmList.root == "/Users/me/.vphone-legacy/VMs")
    assert(vmList.vms.count == 3)

    let vm0 = vmList.vms[0]
    assert(vm0.name == "ios712")
    assert(vm0.ios == "7.1.2")
    assert(vm0.build == "11D257")
    assert(vm0.device == "iPhone6,2")
    assert(vm0.profile == "ui")
    assert(vm0.isUiProfile == true)
    assert(vm0.memory_mb == 1024)
    assert(vm0.disk_bytes == 7300000000)
    assert(vm0.phase == "stopped")
    assert(vm0.isRunning == false)
    assert(vm0.pid == nil)
    assert(vm0.boot_stage == "none")
    assert(vm0.apps.count == 1)
    assert(vm0.apps[0].bundle_id == "com.vphone.hello")
    assert(vm0.apps[0].bundleId == "com.vphone.hello")
    assert(vm0.apps[0].path == "/Applications/Hello.app")

    assert(vmList.vms[0].isLegacy == false)
    assert(vmList.vms[2].name == "old-ios712" && vmList.vms[2].isLegacy == true)
    let vm1 = vmList.vms[1]
    assert(vm1.name == "baseline")
    assert(vm1.profile == "baseline")
    assert(vm1.isUiProfile == false)
    assert(vm1.phase == "running")
    assert(vm1.isRunning == true)
    assert(vm1.pid == 4242)
    assert(vm1.boot_stage == "userspace")
    assert(vm1.apps.isEmpty)

    // 5. Test vm-status.json decoding
    let vmStatusPath = fixturesDir.appendingPathComponent("vm-status.json")
    let vmStatusData = try Data(contentsOf: vmStatusPath)
    let status = try VMStatus.decode(from: vmStatusData)
    assert(status.schema == 1)
    assert(status.session_id == "7e0b")
    assert(status.sessionId == "7e0b")
    assert(status.process?.phase == "running")
    assert(status.process?.pid == 4242)
    assert(status.process?.exit_code == nil)
    assert(status.shutdown?.clean == false)
    assert(status.shutdown?.reason == nil)
    assert(status.log_directory == "logs/20261102-0001")
    assert(status.logDirectory == "logs/20261102-0001")
    assert(status.control?.qmp == "/tmp/vpl-7e0b/qmp")
    assert(status.boot_stage == "home")
    assert(status.bootStage == "home")
    assert(status.display == "player")
    assert(status.lock == "held")
    assert(status.isProcessRunning == true)

    // 6. Test FixtureCLIClient loads and streams all fixtures
    let fixtureClient = FixtureCLIClient(fixturesDirectory: fixturesDir)

    let loadedSetup = try await fixtureClient.setupCheck()
    assert(loadedSetup.items.count == 5)

    let loadedList = try await fixtureClient.vmList()
    assert(loadedList.vms.count == 3)

    let loadedStatus = try await fixtureClient.vmStatus(name: "ios712")
    assert(loadedStatus.boot_stage == "home")

    var streamedOkCount = 0
    for await _ in fixtureClient.setupInstall(id: "homebrew") {{
        streamedOkCount += 1
    }}
    assert(streamedOkCount == 3, "Expected 3 events streamed from FixtureCLIClient, got \\(streamedOkCount)")

    let errorFixtureClient = FixtureCLIClient(fixturesDirectory: fixturesDir, simulateErrors: true)
    var streamedErrCount = 0
    var sawErrorEvent = false
    for await event in errorFixtureClient.vmCreate(name: "test", ios: "7.1.2", ipswPath: "/tmp/test.ipsw", force: false) {{
        streamedErrCount += 1
        if event.isError {{
            sawErrorEvent = true
            assert(event.hint == "Install qemu with: brew install qemu")
        }}
    }}
    assert(streamedErrCount == 6, "Expected 6 events from error stream, got \\(streamedErrCount)")
    assert(sawErrorEvent, "Expected error event with hint")

    print("ALL_CONTRACT_TESTS_PASSED")
}}

let sema = DispatchSemaphore(value: 0)
Task {{
    do {{
        try await runContractTests()
        sema.signal()
    }} catch {{
        fputs("Test failure: \\(error)\\n", stderr)
        exit(1)
    }}
}}
sema.wait()
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            source_file = tmp / "main.swift"
            source_file.write_text(contract_source + "\n" + cli_client_source + "\n" + fixture_client_source + "\n" + harness)

            bin_file = tmp / "contract_test"
            subprocess.run(
                [
                    "swiftc",
                    "-module-cache-path", str(MODULE_CACHE),
                    str(source_file),
                    "-o", str(bin_file),
                ],
                check=True,
            )

            result = subprocess.run([str(bin_file)], capture_output=True, text=True, check=True)
            self.assertIn("ALL_CONTRACT_TESTS_PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
