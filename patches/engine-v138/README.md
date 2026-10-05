# Strata v0.1.38 engine source patch

This is an engine-source patch, separate from the API patch in `../api-v31`. Its exact base is the `Strata-0.1.38/` tree inside `strata-v38.tar.gz`, whose SHA-256 is recorded in `metadata.json`. The patch changes four source files and adds a sized-cache regression test. It does not copy the full engine repository or any compiled assets into this toolkit.

The changes harden expert-cache slot capacity checks and failed cache-copy/reporting paths. `src/program/generate.cpp` also adds `INFO` fields for total and primary cache bytes, single-step verification, pool affinity, and adaptive cache settings. The current external `strata-v138/build-lead/strata.exe` has SHA-256 `a0fdf2b8e1228c80b8c1fcd721e9f67a8240d8ac7cee88edccfbe8d00db6e772`; it predates the `generate.cpp` edit by its recorded file times (binary 14:27, source 14:48 on 2026-10-04). Therefore its startup output does not attest the new INFO fields, and applying this source patch does not reproduce or certify that binary.

Run `verify-engine-patch.py <source-tar> <extracted-source-root>` to confirm the archive identity, apply the patch to the exact archived base in a temporary directory, and compare the resulting source hashes with the extracted v138 tree. `apply-engine-v138.ps1` requires the recorded archive SHA-256 and applies the source diff only. It does not compile or use a GPU.

The source archive carries the Strata MIT license; a copy is included here. Keep runtime engine binaries and model assets external and pin them by their own hashes on each target machine.
