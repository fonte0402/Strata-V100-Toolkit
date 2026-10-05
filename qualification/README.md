# Qwen3.8 replacement qualification

This directory contains a balanced, repeatable 40-task quality set and an OpenAI-compatible HTTP collector. It is designed to compare the existing `llama.cpp` Qwen3.8 service with a Strata candidate. It does not start, stop, or configure either service. Run it only after the operator has prepared a dedicated endpoint and confirmed which model/configuration is serving it.

## Scope and status

There are 10 tasks each for Chinese Q&A, code, long-document understanding, and tool use. Tasks include expected answers or grading rubrics, mandatory pass/fail conditions, and severity labels. The assets are **not a quality pass**. Human blind grading is still required, especially for code quality, reasoning fidelity, instruction following, and harmful hallucinations. Use equivalent model versions, prompts, sampling settings, context limits, and tool fixtures for both arms.

The collector saves full prompts and full API responses, including any reasoning fields, finish reason, usage, timing, tool round-trips, and deterministic checks. Run outputs may contain private prompts or model reasoning; keep them outside Git and review before sharing. Completion is labeled natural, truncated, or unknown; `finish_reason=length`/`max_tokens` fails the screen even if a parseable prefix was returned. Tool cases pass their automatic screen on the expected successful function-call sequence and recovered/absent schema errors; they do not require an ordinary text body for that screen.

## Offline checks

From this directory:

```powershell
python validate.py
python -m unittest -v
```

These checks do not contact a model, start a process, or use a GPU. Model-generated code is never executed. Code cases are graded from the saved answer using their rubric; any optional static inspection must parse source only and must never import or execute it.

## Offline rescore

When a grading rule changes, use `rescore.py input.jsonl output.jsonl`. It refuses to overwrite either file. The new artifact begins with the exact original bytes, including the original manifest and taskset hash, then appends a separately versioned rescore manifest and grading records. For `tool01`, only the explicitly listed Tokyo spellings are canonicalized; other cities remain failures. The old `tool07` fixture is archived under `task_history/` and historical rows that omitted the draft content are labeled `manual_review_invalid_fixture`, never converted into model passes.

## Collecting baseline and candidate

Set `STRATA_API_KEY` in the current process environment when the endpoint requires bearer auth. The collector reads no credential files; the key is used only for the HTTP Authorization header and is never copied into manifests or printed. No credential is created by this tool.

Generate the archive with the included adapter, pointing it at the tokenizer data and implementation used by the exact serving build. It calls `ST.Tokenizer.encode(text, parse_special=False)`, reports the hashes of `vocab.json`, `merges.txt`, `token_type.json`, and the implementation in the manifest, and keeps the tokenizer loaded through calibration. No model is started. Replace `<TOKENIZER_DIR>` and `<STRATA_TOKENIZER_SCRIPT>` with paths on the current machine:

```powershell
$PythonExe = (Get-Command python).Source
$TokenizerDir = 'C:\path\to\serving-tokenizer'
$TokenizerImpl = 'C:\path\to\strata-v138\tools\strata_tokenizer.py'
python make_longdoc.py --target-tokens 256000 --tokenizer-adapter $PythonExe --tokenizer-script tokenizer_count.py --tokenizer-dir $TokenizerDir --tokenizer-implementation $TokenizerImpl --out runs/archive-256k.txt
```

The archive manifest is written as `archive-256k.txt.manifest.json`. For a first small batch, use category, exact task IDs, or a stable-order limit. This example runs two Chinese Q&A cases without sending long-document prompts:

```powershell
python collect.py --arm candidate --base-url http://127.0.0.1:18100 --category zh_qa --max-cases 2 --out runs/candidate-qa-smoke.jsonl
```

Then create a new, empty run path for each arm. The collector refuses to overwrite either the JSONL evidence or summary file:

```powershell
python collect.py --arm baseline --base-url http://127.0.0.1:18099 --allow-no-auth --max-tokens 4096 --long-max-tokens 1024 --long-prompt-overhead 1024 --max-context-tokens 262144 --long-document runs/archive-256k.txt --long-document-manifest runs/archive-256k.txt.manifest.json --out runs/baseline-2026-10-04.jsonl
python collect.py --arm candidate --base-url http://127.0.0.1:18100 --effort none --max-tokens 4096 --long-max-tokens 1024 --long-prompt-overhead 1024 --max-context-tokens 262144 --long-document runs/archive-256k.txt --long-document-manifest runs/archive-256k.txt.manifest.json --out runs/candidate-2026-10-04.jsonl
```

Use the actual dedicated endpoint for each prepared service. The sample ports are illustrative; confirm them before dispatch. Do not run both arms against a shared, changing endpoint. Each request is sequential, has temperature 0, seed 42, and `reasoning_effort=none` by default; an optional `--reasoning-budget` is explicit. Usage, effective request parameters, finish reason, and natural/truncated/unknown completion state are saved. HTTP errors and malformed outputs are retained as failures; there is no silent retry. Tool calls are interpreted by a deterministic local fixture runner and are never sent to external systems. Long-document tasks use a separate 1024-token output budget and 1024-token overhead by default, and preflight checks measured document + reserved overhead + output against the supplied effective context limit. Server chat-template overhead still needs operator confirmation. Baseline services that do not use bearer auth must pass `--allow-no-auth`; that option sends no key even if a Strata key is present in the environment.

Use `--category` (repeatable), `--case-id` (repeatable), and `--max-cases` to run a small subset first. For example, parenthesize multiple categories as repeated flags. The manifest lists the exact selected task IDs and sampling settings.

For each arm, record the running binary/model/tokenizer hashes, effective server arguments, quantization, context size, speculative decoding settings, and hardware in the run notes. The collector can record caller-supplied labels, but a config file snapshot alone does not prove what a process actually loaded.

## Long-context assets

`make_longdoc.py` generates a deterministic, layered fictional archive with cross-section facts, decoys, deliberately absent facts, and retrieval markers at the beginning, middle, and end. It can create 128K or 256K token targets **only when supplied with a tokenizer adapter for the actual serving tokenizer**. Character count is never presented as token count. The adapter receives UTF-8 text on stdin and must print either an integer token count or JSON `{"tokens": N}` on stdout. The script records adapter command, output length, SHA-256, measured token count, and seed. Do not use a tokenizer from a different model as a substitute.

The generated archive has a stable seed and explicit source manifest. For a length target, the script grows or trims whole deterministic sections and measures again until it reaches the requested tolerance; inspect the manifest and ensure the actual runtime context can accept the prompt plus answer budget before testing. Since tokenizer adapters vary, long-context calibration is an operator-supplied integration step.

## Decision rule

Compare paired task IDs and blind the grader to arm labels. A critical task failure, fabricated fact on an explicit abstention task, unsafe code execution recommendation, invalid tool/schema behavior, or incomplete answer due to truncation blocks replacement until fixed and re-run. For non-critical tasks, report pass counts by category and paired disagreements; do not hide failures in a single aggregate score. Decide separately on quality and decode speed. Faster decode is acceptable only after category-level quality is no worse under the pre-agreed blind rubric, with confidence limits appropriate to the small sample.
