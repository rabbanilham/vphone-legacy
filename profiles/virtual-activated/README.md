# Virtual activation configuration snapshots

These JSON files record the validated iOS 7.1.2 / 11D257 testing profile and its source keybag SMP/nooscar boot plan. Firmware, disk images, keybag contents, runtime DeviceTree, auxiliary images and QEMU binaries are not included.

- `profile.json`: original preparation hashes, audited patch and historical validation results.
- `boot_plan.json`: derived virtual-activated plan.
- `keybag-boot-plan.json`: original keybag-contract `boot-plan-smp-nooscar` configuration.
- `source-hashes.json`: SHA-256 of the original local JSON files before path normalization.

`${REPO_ROOT}` represents the absolute checkout path. These are configuration templates, not directly executable plans: replace this token recursively in JSON keys and values when materializing locally. Paths refer to local artifacts which must already exist. Recorded validation applies to the original hashed artifacts, not an arbitrary rebuild; Home-screen acceptance was incomplete in that recorded validation.

To rebuild with locally supplied baseline firmware/disks and the original keybag boot directory (including KernelMemory.bin and DeviceTree.runtime):

```sh
.venv/bin/python scripts/prepare_virtual_activation.py \
  --output build/virtual-activated-rebuild \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json
```

Use a new output directory. The source boot plan requires all its referenced auxiliary images; JSON snapshots alone cannot reconstruct firmware or image contents. After rebuilding, launch with explicit `--plan`, `--system` and `--data` paths pointing at that output directory. The interactive default uses `build/virtual-activated-profile-v5/`.
