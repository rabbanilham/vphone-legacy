# Contributing

Start with [setup](docs/setup.md), [architecture](docs/architecture.md), and [status](docs/status.md). The project currently targets one firmware/device pair. Keep changes focused and preserve original firmware and VM data.

Run `make test` and `.venv/bin/python scripts/check_docs.py`. Backend changes also need the relevant synthetic device contracts; UI claims need native guest evidence rather than source-string tests or an offline replay alone. Describe exactly which checks ran, skipped, or could not run. Use new output directories for probes and disposable disks for lifecycle integration.

Document public CLI/configuration changes, source provenance, and new local artifact requirements. Do not commit firmware, disk images, memory dumps, SDKs, binaries, credentials, personal paths, or agent handovers. Keep reusable findings in docs or dated technical reports. Historical research is retained for attribution and evidence, not as current setup instructions.

Open issues with a minimal command and sanitized error output. Distinguish baseline firmware preparation from the local UI profile. See [third-party notices](THIRD_PARTY_NOTICES.md): repository-wide licensing remains unresolved, and existing copyright/license notices must be preserved.

## Reviews and merging

All changes to `main` must go through a pull request. The `checks` status from GitHub Actions must pass against the current base, and review conversations must be resolved. Direct pushes, force pushes and deletion of `main` are blocked, including for the owner.

`@rabbanilham` owns every file through `.github/CODEOWNERS`; other reviews do not replace the required code-owner approval. New reviewable commits invalidate earlier approvals. Only `@rabbanilham` can merge into `main` or bypass the approval requirement, and that exception is limited to pull requests. It permits owner-authored PRs, since GitHub does not allow self-approval. The owner exception cannot bypass required CI or unresolved review conversations.
