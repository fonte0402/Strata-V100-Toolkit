# Benchmark evidence and limits

This page records selected, sanitized measurements from the V100 lane. It separates an older fixed-output cache-warm context matrix from the new short natural-request sample. Those older batches are separate from the new controlled v138/v139 pair below.

## Historical v0.1.38 context matrix

The sanitized CSV contains only the measured second-repeat rows from five `matrix_v38_ctx*.jsonl` records. Each selected row has `effort=medium`, `kind=decode`, `rep=2`, and `n=512`, so the result reflects a 512-token generated response rather than a short EOS completion. The original report specifies temperature 1.0, top_p 0.95, top_k 20, seed 42, and calls this a sampled 512-token decode matrix. The records show elevated cache-hit percentages on repeat two (91.7–97.1%), so these are sequential cache-warm results. The first and second repeats differ materially; do not use the table as a cache-cold estimate or as a natural-request speed promise.

| Context | Decode rate, repeat 2 |
|---:|---:|
| 8K | 91.4 tok/s |
| 32K | 88.3 tok/s |
| 64K | 84.5 tok/s |
| 128K | 80.8 tok/s |
| 256K | 70.9 tok/s |

The 256K point is a rope extrapolation beyond the trained context. It is included as an historical stress result, not evidence of reliable semantic recall at 256K. The corresponding `none` rows generated 494 tokens under a 512-token cap and are deliberately excluded from this fixed-length table. Per-row fields preserved in the CSV are context, effort, repeat index, generated token count, request wall time (the original `wall` field, including non-decode overhead), decode tok/s, and reported cache-hit percentage. Prompts, response text, local paths, identifiers, and credentials are excluded.

## Earlier v0.1.38 short batch

The latest exploratory static 128K run used Vision and ESP, 13,317 actual cache slots, adapt0, 23 pool workers, temperature 0, and seed 42. All 15 requests were valid: three repetitions each of natural Chinese, code, and tool-schema requests, plus three narrative and three code requests with an exact 512-token output. Per-request decode rates appear in the README. This is a single-version descriptive batch, not an A/B result or quality score. Do not compare its rates to the historical medium-effort matrix as if only one variable changed.

The attempted v0.1.39 start reported 13,316 actual cache slots against the v0.1.38 profile's 13,317. The strict pairing gate rejected that configuration. No upgrade speedup or regression is inferred; the profiles are being aligned before retrying.

## Quality evidence

- A recorded ten-item long-document retrieval screen used a 128K input. It is a narrow task-specific screen, not a general quality certification or blind benchmark.
- A 30-task baseline/candidate review was a manual, non-blind static review. Tool-schema cases passed the reviewed field/structure checks; the code-answer review identified a missing requested complexity explanation in `code08`. Generated code was not executed. This is evidence of a concrete limitation, not an overall pass/fail score.
- The independent 30-task candidate summary marks human blind review pending. Automatic screens do not substitute for blind quality review.

## v0.1.39 status and comparison protocol

The upstream v0.1.39 source is pinned and has been built for sm_70 with the same CUDA 12.8 toolchain as the v0.1.38 lane. A fair A/B must match the model and tokenizer/template, 131072 context, Vision/ESP, actual cache-slot count and profile, MTP maximum, pool workers, prompts, sampling, and seed. It must record each version's immutable source identity and binary hash, separate natural completion from exact-512 output, and distinguish cold, warm r1/r2, and prefix reuse. The one-GPU lane runs only one engine at a time. The new paired short observations below are descriptive speed evidence. They do not establish quality parity, long stability or production replacement.

The current benchmark contract requires per-request engine timing with complete generation-segment accounting. Missing or incomplete timings are not eligible for engine-speed comparison. Client wall time includes HTTP round trip. Diagnostic audit/verification overhead must remain a separate profile. Additional v139 maintenance-window results will be published only after the controlled paired run completes.

## Evidence provenance

The selected historical rows came from the five `matrix_v38_ctx{8192,32768,65536,131072,262144}.jsonl` captures; the run report describes the 512-token sampled settings. Hardware facts were rechecked for the current maintenance window. The quality notes derive from the ten-item long-document run record and the manual review notes. Private raw captures and prompts are not redistributed in this toolkit.


The 2026-10-05 exploratory static batch retained 15 valid rows. In addition to the three natural-request tasks, exact-512 narrative repetitions measured 21.07/20.86/21.26 tok/s, and code repetitions measured 45.19/43.06/44.13 tok/s. Both fixed-output cases finished at the cap and are not completed-answer quality checks. The first v139 load had 13,316 resident experts versus 13,317 in v138; strict version pairing was refused. The subsequent pair used a shared fixed top-13000 profile to equalize actual residency.

The sanitized [15-request CSV](short-requests.csv) includes request hashes, input/output counts, completion reasons and measured timings. Current binary: `a0fdf2b8e1228c80b8c1fcd721e9f67a8240d8ac7cee88edccfbe8d00db6e772`; canonical-LF API source: `b615f136db0a1aa141b46ee6a89e885564f1d6a2917a80ed205d031304df3a63`.

## Controlled 13,000-expert version pair · 2026-10-05

Both versions actually loaded the same first 13,000 ranked experts with adaptation and audit disabled, the same API source, 128K context, Vision/ESP ON, MTP maximum 3, 23 workers and explicit none-effort temperature-0 sampling. Actual model, tokenizer, template, expert-profile, binary and run fingerprints were retained privately. Both registered process trees used Below Normal priority and the same allowed CPU set; this does not prove where individual threads executed. The CUDA toolchain was 12.8 for both pinned builds; the candidate is built from the v0.1.39 source tag, not the incomplete downloaded CUDA12.9 release package.

One retained, unmeasured same-payload warmup preceded each task's three measured repetitions, labelled warm_repeat. This is not a cache-cold comparison or the historical adaptive second-repeat matrix. All 30 measured requests passed response/hash, generation-segment, runtime identity and actual-residency checks. All five task medians improved in this window by 4.5–20.7%; see the README table and [30-request CSV](version-pair-20261005.csv). No confidence interval or universal speedup is inferred from three observations. The nine natural output pairs matched after excluding random response/tool-call IDs. Fixed-512 responses have finish=length; five of six paired draft texts differ. They measure decode prefixes and do not certify complete programs or stories.

The earlier one-hour observation stopped at 49.1 minutes without a completion summary, despite 34 successful recorded requests. It did not pass one-hour acceptance. Full balanced quality and reasoning comparisons are kept separate from speed results, and v0.1.38 remains the baseline.
