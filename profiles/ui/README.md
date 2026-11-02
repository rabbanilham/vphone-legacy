# UI regeneration gate (offline)

`ui_profile.build_boot` regenerates from the user's decoded baseline firmware
using the v5 boot arguments and chosen/debug-enabled. It does not depend on the
missing research keybag boot directory. No VM was booted for this comparison.

Reproduce with an existing baseline bundle (reads firmware only, writes a new
output directory):

```sh
PYTHONPATH=scripts python3 -c 'from ui_profile import build_boot; build_boot("/path/to/baseline", "/tmp/new-ui-boot")'
python3 scripts/ui_profile_oracle.py /tmp/new-ui-boot /path/to/virtual-activated-profile-v5/boot
```

`oracle-report.json` records the 2026-11-02 read-only comparison against the local
v5 oracle. Absolute candidate paths refer to the temporary output used for this
audit and are not portable launch inputs. The report includes every differing
plan field, every differing runtime DeviceTree property (size/hash), all required
file hashes, and differing byte counts inside/outside the boot-args/DT regions.
The comparator exits 1 because the candidate is **not identical**.

Assessment of all differences:

- `image` and `auxiliary_images[0].path`: relocation to the generated output.
  The new ANS record otherwise matches v5, including its e45348cd… SHA-256.
  Paths are functionally equivalent when the actual named files are present.
  The oracle's external auxiliary path is not required for regeneration.
- `validation`: different wording; both declare execution unverified. This is
  descriptive metadata with no guest execution effect.
- `image_sha256`, KernelMemory.bin, DeviceTree.runtime and boot_args.bin hashes:
  genuinely different bytes, not simply renamed artifacts. KernelMemory has the
  same length, with **no differences outside boot_args and runtime DeviceTree**.
  Thus kernel instructions, monitor, framebuffer allocation and RAM placement
  match this oracle; the complete image does not.
- Removed `/device-tree/arm-io/uart2/oscar` subtree and
  `/device-tree/arm-io/isp`: every removed property is enumerated in the report.
  The local v5 oracle still contains both despite its historical source name
  “nooscar”. The current loader intentionally omits absent devices to remove
  their firmware/driver waits (see `omit_unpopulated_devices`). These are behavior
  changes and may improve boot latency, but are not proven equivalent by this
  static comparison.
- `/device-tree/chosen:nvram-proxy-data`: boot arguments match, but v5 additionally
  contains a persisted `com.apple.System.sep.art` variable. Fresh regeneration
  has no persisted ART. Its first-boot creation/guest keybag consequences have
  not been verified here. **Functional equivalence is unresolved**; no arbitrary
  ART bytes are fabricated or copied from the oracle into the default pipeline.
- `/device-tree/chosen/memory-map:DeviceTree`: the advertised length shrinks from
  142016 to 138792 bytes after omission. boot_args.deviceTreeLength changes to
  match. Property insertion order also differs (`debug-enabled` is created in
  the builder, rather than appended by the old postprocessor). These structural
  changes explain the runtime DT/boot_args/image hashes; pointer addresses and
  boot arguments remain equal. Property ordering should be semantically neutral
  to the Apple DT parser, but byte identity is not claimed.

The import-profile fallback preserves the source DT/NVRAM and clones source disks;
it also requires and copies every actual auxiliary image named by that plan.
Missing oracle auxiliary bytes cause a clear failure rather than an incomplete
import. Home acceptance still requires three cold boots by the orchestrator.

`home-reference.json` is a compact RGB reference derived from the screen crop
of `research/evidence/public-readme-20261029/home.png` (crop coordinates and source
are embedded). It supports conservative profile-specific icon/dock matching,
not a claim that the new pipeline has booted. The archived Settings frame is a
negative test. No true lockscreen reference exists in that evidence directory;
lockscreen classification remains a separately tested heuristic.
