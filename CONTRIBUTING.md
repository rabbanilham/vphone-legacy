# Contributing

Start with [setup](docs/setup.md), [architecture](docs/architecture.md), and [status](docs/status.md). The project currently targets one firmware/device pair. Keep changes focused and preserve original firmware and VM data.

Run `make test` and `.venv/bin/python scripts/check_docs.py`. Backend changes also need the relevant synthetic device contracts; UI claims need native guest evidence rather than source-string tests or an offline replay alone. Describe exactly which checks ran, skipped, or could not run. Use new output directories for probes and disposable disks for lifecycle integration.

Document public CLI/configuration changes, source provenance, and new local artifact requirements. Do not commit firmware, disk images, memory dumps, SDKs, binaries, credentials, personal paths, or agent handovers. Keep reusable findings in docs or dated technical reports. Historical research is retained for attribution and evidence, not as current setup instructions.

Open issues with a minimal command and sanitized error output. Distinguish baseline firmware preparation from the local UI profile. See [third-party notices](THIRD_PARTY_NOTICES.md): repository-wide licensing remains unresolved, and existing copyright/license notices must be preserved.
