# Strata API patch

This patch applies only to the API checkout identified in `metadata.json`. Its base is the exact `Strata` Git commit `9259cad4cfa3543cd3b8decab5962672b968c649` (v0.1.31-era API source), not a v138 engine repository. It contains the current API server/UI diff plus the explicitly listed API source and regression-test additions. The API tree was dirty while the patch was captured; the patch does not claim otherwise.

Upstream project attribution is Niko1221 and the Strata contributors; the corresponding MIT license is included beside this patch.

The checked patch is `api-v31.patch`. Before application, inspect it and verify the target commit. `apply-api-v31.ps1` refuses a different HEAD or a patch that fails `git apply --check`, then applies it. `verify-api-patch.py` builds a temporary tree from the base commit's exact preimage blobs, applies the patch there, and compares output hashes with the captured sources. This verifies applicability without changing the source checkout.

To refresh after another API source change, use `python create-api-patch.py --repo <clean-or-frozen-Strata-checkout>`. It only reads the explicit allowlist in the script, requires the same base commit, and writes this patch plus metadata. Do not add logs, run JSONL, credentials, models, or unrelated engine edits.
