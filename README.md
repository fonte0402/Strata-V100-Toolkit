# Strata V100 Toolkit

[中文](README.zh-CN.md) · [Benchmark notes](docs/BENCHMARKS.md) · [Upstream Strata](https://github.com/Niko1221/Strata)

**Qwen3.8-Flash-Next on one Tesla V100 32GB: MTP acceleration, 8K–256K contexts, Vision, an OpenAI-compatible API, and Windows web service controls.**

This project publishes measurements from an **i9-13900F + DDR4 + PCIe Gen3 x4 + Windows** workstation and packages portable service tools and source patches. Strata provides the inference engine. v0.1.38 is the current baseline; pinned v0.1.39 is under qualification.

## Measured results

### Historical context matrix

v0.1.38, original GSQ-RCO IQ3_XXS, MTP T=3, automatic expert cache:

| Configured context | Decode (tok/s) | Expert cache slots |
|---|---:|---:|
| 8K | **91.4** | 15,375 |
| 32K | **88.3** | 14,927 |
| 64K | **84.5** | 14,403 |
| 128K | **80.8** | 13,317 |
| 256K · RoPE extrapolation | **70.9** | 11,191 |

Protocol: medium reasoning, 512 generated tokens, second sequential repeat; temperature 1.0, top_p 0.95, top_k 20, seed 42. Generated tokens may include reasoning. These are warm-cache fixed-output measurements, not a speed promise for every request. 256K uses extrapolation. Selected raw fields are available in the sanitized [CSV](docs/context-matrix.csv).

### Latest 128K short-request checks · 2026-10-05

v0.1.38, Vision/ESP ON, static 13,317 resident experts, adapt0, MTP T=3, 23 workers, temperature 0. One warmup per task followed by three measured repetitions: **15/15 valid records**.

| Task | Output tokens | Median decode (range), tok/s | Finish |
|---|---:|---:|---|
| Chinese answer | 98 | **30.73** (29.82–31.11) | Natural |
| Code answer | 216 | **43.01** (41.18–45.09) | Natural |
| Tool call | 50 | **49.63** (41.60–51.27) | tool_calls |
| Chinese narrative | 512 | **21.07** (20.86–21.26) | Output cap |
| Longer code | 512 | **44.13** (43.06–45.19) | Output cap |

The two tables use different tasks, sampling and cache policies, so their difference does not establish a performance regression or gain. The latest checks retain complete request and generation-segment accounting. They are single-version observations; no v0.1.39 speedup is claimed yet. See [benchmark notes](docs/BENCHMARKS.md).

## Tested hardware and model

| Component | Configuration |
|---|---|
| Inference GPU | **1× Tesla V100-PCIE-32GB / sm_70 / 32,768 MiB** |
| PCIe | **Measured Gen3 x4** |
| CPU | Intel Core i9-13900F · 24 cores / 32 threads (8P + 16E) |
| RAM | **64 GiB DDR4-3600** · 4×16 GiB · dual channel |
| Storage | Model, pack and PLE assets on NVMe |
| OS / driver | Windows 11 Pro build 26200 / NVIDIA 581.15 |
| Build toolchain | CUDA 12.8.61 / MSVC 14.43 |
| Model | Qwen3.8-Flash-Next · GSQ-RCO IQ3_XXS |
| Acceleration / API | MTP T=3 / expert cache / OpenAI-compatible API |

An RTX 3080 handles desktop work and is excluded from this inference lane. GPU, PCIe, CPU, RAM and OS facts were rechecked during the current test window.

## Working capabilities

- **Text / Vision profiles:** 8K, 32K, 64K, 128K and 256K. Context and vision loading are fixed at startup.
- **Request-level Thinking and ESP:** client-selected reasoning effort; ESP loaded through the startup profile and switchable through web/API requests. Explicit request parameters override shared defaults.
- **Web operations:** Chat, shared Sampling defaults, Monitor and **Shutdown**.
- **Persistent token usage:** current run, today and all time; restart-safe storage and separate initial-input versus internal-continuation cache accounting.
- **Portable service management:** relocated roots, external assets/auth files, and process-creation, fingerprint and listener ownership checks before stopping a service.

ESP is experimental and may change output behavior. The measurements do not establish zero quality impact.

## Qualification status

A ten-task **128K long-document retrieval screen** has been recorded, alongside Chinese, code and tool baseline/candidate reviews. Manual code review identified a missing requested complexity explanation; general quality acceptance remains open. Pinned v0.1.39 source has built, with controlled pairing, longer soak and real rollback checks still in progress.

## Repository contents

| Directory | Contents |
|---|---|
| `portable_strata/` | Config generation and ownership-checked service controls |
| `qualification/` | Synthetic quality fixtures and screening tools |
| `patches/` | Pinned API-base and separate v138 engine patches |
| `docs/` | Benchmark notes, sanitized CSV and service-tool usage |

For usage, see [service tools](docs/TOOL_USAGE.md). Model weights, packs, tokenizers, credentials, databases and private run logs are excluded.
