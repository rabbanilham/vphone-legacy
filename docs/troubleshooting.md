# Troubleshooting

| Symptom | Action |
| --- | --- |
| Python or Keystone missing | Run the prerequisites in [setup](setup.md); use `VPHONE_PYTHON` or `VPHONE_KEYSTONE_PREFIX` for nonstandard installs. |
| pip cannot reach its package index | Restore network/proxy access and rerun `make setup`; a partially created `.venv` does not mean setup succeeded. |
| SDK `.tbd` linker error | Select compatible Xcode tools or pass `--sdk` to the backend builder. |
| Pinned source or build tools missing | Run the backend builder with `--fetch --install-build-deps`. |
| Backend manifest/source hash mismatch | Rebuild this worktree's backend; do not copy a manifest from another source revision. |
| Missing boot plan, seeded Data or auxiliary image | The UI profile requires additional artifacts; read [firmware](firmware.md). JSON snapshots cannot recreate missing bytes. |
| `doctor` succeeds but no Home screen | Artifact validation does not establish boot/UI acceptance. Baseline preparation differs from the UI research profile. |
| QMP socket permission error | Use a host/environment that permits local Unix sockets. |
| VM already exists | Choose a new name; do not delete a VM with data you need. |
| Normal stop rejected | Graceful shutdown is unsupported. Use a disposable VM and explicit `--force` when needed. |
| Optional tests skip | Read the reason; supply the specific corpus/tool/firmware only if you need that coverage. |

For bug reports include the source commit, host architecture/macOS/Python versions, backend source pin, command, and sanitized logs. Do not attach firmware, keys from a personal device, full guest memory, or private guest files.
