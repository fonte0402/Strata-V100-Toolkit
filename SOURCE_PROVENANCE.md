# Source provenance

The lifecycle implementation was derived from these frozen source files in the original workspace and then parameterized for this toolkit:

| Exported file | Source path | Source SHA-256 |
|---|---|---|
| `portable_strata/service_control.py` | `phase0/service_control.py` | `3b840afea81f56822a7f69ac677dc666000a726c79dd4b8c2b705bd1b2b92edb` |
| `portable_strata/guarded_start.py` | `phase0/guarded_start.py` | `b0362110fdbc8b6a39ba059272bc784b668e40569a1cf87960265856ba45d62b` |
| `portable_strata/install_managed_task.ps1` | `phase0/install_managed_task.ps1` | `f1f251beb3ec52834b73de8092412685d5f413063bcc5d4230617380ed395204` |

The exported source removes workstation-specific defaults, requires explicit root/port/GPU/config/Python/task/auth-file paths, stores runtime output beside the selected state file, and refuses to replace an unrelated scheduled task. Service identity checks remain based on PID creation time, executable path and SHA-256, config SHA-256, process descendants, and owned listeners.

The historic batch menu used delayed expansion for validated menu selections, while the recovery diagnostic filtered NVIDIA output with `findstr` and retained the GPU name. The exported launch scripts keep paths quoted through PowerShell and pass an argument vector; the GPU gate queries UUID, name, and memory together and records the NVIDIA-reported name. No name-only process termination is used.

Upstream attribution: Strata, Copyright (c) 2026 Niko1221 and the Strata contributors. The MIT license is included at the repository root and alongside each source patch. The operational Python and PowerShell wrappers are adaptations of the named phase0 files; the config generator and packaging metadata are new toolkit code.

Qualification contents were copied by filename allowlist from `phase0/qualification`. Only synthetic task definitions, code, required historical invalid-fixture metadata, and offline tests are included. Run artifacts, visual comparison outputs, original logs, assets, credentials, and databases were excluded. The exported qualification instructions and tokenizer entry points were changed to remove fixed workstation paths and auth-file fallback.
