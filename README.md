# Portable Strata toolkit

This small Windows/Python toolkit generates a service configuration and PowerShell start/stop scripts for a relocated Strata checkout. It accepts explicit paths for the API source, engine source/binary, Python, model assets, GPU UUID, port, and protected auth-file location. Paths with spaces are supported. It never bundles models, tokenizers, packs, credentials, databases, generated run records, or source logs.

The lifecycle controller targets Windows with Python 3.10 or newer, PowerShell, Task Scheduler, WMI/CIM access, `netstat`, and NVIDIA `nvidia-smi`. Offline config and qualification checks use the Python standard library. No engine compilation is included.

The source is split into three independent pieces. `portable_strata/` contains the path-driven config builder and the ownership-checked service lifecycle. `qualification/` contains the allowlisted synthetic quality taskset and offline tools. `patches/` records source changes that can be applied to the exact API base; the v138 engine patch has a separate identity and directory. API-server worktree edits are not the v138 engine source.

## Generate a service setup

Use paths from the target machine. Example paths below are illustrative and may contain spaces:

```powershell
$Root = '<ROOT>'
$RuntimeRoot = '<RUNTIME_ROOT>'
$PythonExe = '<PYTHON_EXE>'
python .\portable_strata\config.py generate `
  --output (Join-Path $RuntimeRoot 'service.json') `
  --engine-root (Join-Path $Root 'engine source\build-lead') `
  --engine-exe (Join-Path $Root 'engine source\build-lead\strata.exe') `
  --python $PythonExe `
  --api-root (Join-Path $Root 'API source') `
  --auth-file (Join-Path $RuntimeRoot 'private\service_auth.json') `
  --gpu-uuid 'GPU-REPLACE-WITH-THIS-MACHINE-UUID' `
  --port 18100 `
  --asset "pack=$(Join-Path $Root 'assets\pack')" `
  --asset "native=$(Join-Path $Root 'assets\models\model.gguf')" `
  --asset "expert_profile=$(Join-Path $Root 'assets\expert profile.bin')" `
  --asset "mtp=$(Join-Path $Root 'assets\mtp')" `
  --asset "tokenizer=$(Join-Path $Root 'assets\tokenizer')"
```

The `--auth-file` argument is a pointer only. Store the JSON file outside this repository with an `api_key` field and Windows ACLs limited to the service user. The secret itself is never read or copied during config generation. Background Task Scheduler services require this explicit file because they do not inherit the launching shell's environment. Foreground/check-only use can read `STRATA_API_KEY` from that process or the explicit file.

Optional asset mappings are `mmproj`, `vision_exe`, and `esp`. Set `--vision` only when both vision assets are mapped; the builder places `mmproj` in the API vision config and does not pass it as an engine CLI option. Set `--esp` only with an external `esp` asset. ESP adds the control-vector flags to the engine configuration and does not set reasoning parameters; Thinking remains controlled by each API request. The default expert cache is `auto`; override with a positive integer only when the chosen engine and workload have been checked.

The command writes `service.json`, `start.ps1`, and `stop.ps1` beside the chosen output file. To inspect the launch plan without checking hardware, touching a port, or starting a process:

```powershell
python .\portable_strata\config.py show-command --config (Join-Path $RuntimeRoot 'service.json')
```

Review the paths, then run the generated PowerShell start or stop script. Startup gates require Windows, Task Scheduler, Python, and `nvidia-smi`. They check the selected GPU UUID, memory headroom, system commit and free port, and register the exact service process identity. Stop checks the recorded PID creation time, executable fingerprint, and port ownership before requesting shutdown; it verifies identities again before force termination. It never finds processes by a broad image-name kill. The GPU query retains UUID, NVIDIA-reported name, and used memory.

The state file, Task Scheduler arguments, service logs, and usage database live beside the selected state file/config output. Keep that runtime directory outside the Git repository. `service_control.py` has no local GPU UUID or port defaults. Managed Task Scheduler task names are unique by API root and port by default; an unrelated same-name task is refused.

Generated PowerShell wrappers use quoted literal paths, and the Python launcher passes arguments as a vector. GPU preflight reads `uuid,name,memory.used` and selects by UUID while retaining NVIDIA's reported name in the readiness record.

## Qualification tools

See [qualification/README.md](qualification/README.md). The taskset is synthetic; no historical run files or original user logs are included. To create calibrated long-context material, pass both the exact serving tokenizer directory and the tokenizer implementation path from the serving source. The exported instructions have no fixed workstation drive paths. API credentials are read from the current environment only.

Offline checks use only Python's standard library:

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s qualification -p 'test_*.py' -v
python qualification\validate.py
```

These checks do not launch a model or use a GPU. Qualification collection is a separate, explicit live-endpoint operation; its JSONL and summary outputs can contain prompts or model reasoning and should stay outside Git.

## Source provenance

`portable_strata/service_control.py`, `guarded_start.py`, and `install_managed_task.ps1` are adapted from the frozen operational files under `phase0/`; `SOURCE_PROVENANCE.md` records their source hashes and the portable edits. They are kept separate from the API patch. API patch metadata names the base commit and hashes. v138 engine provenance is recorded independently so a dirty API Git tree is never represented as a clean v138 repository.
