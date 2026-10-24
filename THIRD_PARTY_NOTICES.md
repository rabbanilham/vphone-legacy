# Third-party notices and license audit

## Existing declarations

- `backend/s5l8960x.c` declares **GPL-2.0-or-later**. `backend/source.lock.json` pins QEMU and declares the same license. Preserve these declarations and upstream notices when combining or distributing sources/binaries.
- `scripts/patchers/vfdecrypt.c` retains a 2006 MIT-style grant and copyright notices for Ralf-Philipp Weinmann, Jacob Appelbaum, and Christian Fromme. Its adjacent `bswap.h` has no license header and remains part of the provenance review.
- Backend register contracts cite Linux A7 DeviceTree, AIC and Samsung UART sources in [backend references](backend/README.md). Those references do not confer a license on unrelated project files.
- A related upstream candidate was inspected: the adjacent `Lakr233/vphone-cli` checkout carries an MIT license; the exact ancestry of this repository's remaining Python/Swift/helpers still needs a file-level provenance review. Its license must not be applied wholesale based on similarity alone. A comparison by matching source basenames found no files above the 0.55 text-similarity threshold; that limited comparison neither proves nor excludes ancestry.
- Python dependencies (`requirements.txt`) and pinned QEMU build dependencies (`backend/source.lock.json`) retain their own licenses. They are installed locally, not vendored here.
- IPSWs, extracted Apple binaries, guest artwork, and screenshots of Apple UI are not covered by a project source-code license. Review publication rights for committed guest-derived evidence separately.

## Recommendation and outstanding decision

Use GPL-2.0-or-later for the QEMU overlay as already declared. For original standalone tooling, select a license only after confirming authorship and any upstream-derived files; MIT may be appropriate for independently licensed tooling, but this audit does not grant rights or establish that provenance. Include all required upstream copyright texts before distribution. There is intentionally no repository-wide LICENSE yet.

The existing repository history begins with a large bring-up snapshot rather than a complete upstream import trail. SPDX on the backend is clear; ownership/license coverage for the rest is a **publication blocker** pending maintainer review. This cleanup does not remove or replace existing notices and does not claim that a top-level license alone would resolve the audit.

Reference license texts: [QEMU GPL v2](LICENSES/QEMU-GPL-2.0.txt) and [vphone-cli MIT](LICENSES/vphone-cli-MIT.txt). These preserve upstream terms; their inclusion does not license all files in this repository.
