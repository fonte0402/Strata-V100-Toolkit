"""Create an external, relocation-safe Strata service configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

SECRET_FIELDS = {"apikey", "accesstoken", "refreshtoken", "authorization",
                 "auth", "password", "secret", "credential"}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _absolute(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _outside_toolkit(value: str, label: str) -> str:
    path = Path(value).expanduser().resolve(strict=False)
    toolkit_root = Path(__file__).resolve().parents[1]
    if path == toolkit_root or toolkit_root in path.parents:
        raise ValueError(f"{label} must be outside the toolkit Git repository")
    return str(path)


def _asset_map(items: list[str]) -> dict[str, str]:
    result = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"asset must be NAME=PATH: {item}")
        name, value = item.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError(f"invalid asset name: {name}")
        if name in result:
            raise ValueError(f"duplicate asset name: {name}")
        result[name] = _absolute(value)
    return result


def build_config(args: argparse.Namespace) -> dict:
    assets = _asset_map(args.asset)
    required = {"pack", "native", "expert_profile", "mtp", "tokenizer"}
    missing = sorted(required - assets.keys())
    if missing:
        raise ValueError("missing asset mappings: " + ", ".join(missing))
    if not args.gpu_uuid.strip():
        raise ValueError("--gpu-uuid is required and must be non-empty")
    if not (1 <= args.port <= 65535):
        raise ValueError("--port must be between 1 and 65535")
    engine_root = _absolute(args.engine_root)
    exe = _absolute(args.engine_exe)
    python = _absolute(args.python)
    auth_file = _outside_toolkit(args.auth_file, "--auth-file")
    api_root = _absolute(args.api_root)
    default_task = f"StrataPortable-{args.port}-{hashlib.sha256(api_root.casefold().encode()).hexdigest()[:8]}"
    cache = str(args.expert_cache).strip().lower()
    if cache != "auto":
        try:
            if int(cache) <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("expert cache must be 'auto' or a positive integer") from exc
    if args.context <= 0 or args.spec < 0:
        raise ValueError("context must be positive and spec cannot be negative")

    engine_args = ["--pack", assets["pack"], "--native", assets["native"],
                   "--spec", str(args.spec), "--prefill", args.prefill,
                   "--expert-cache", cache,
                   "--expert-profile", assets["expert_profile"],
                   "--max-context", str(args.context), "--mtp", assets["mtp"]]
    if args.vision:
        if not {"mmproj", "vision_exe"}.issubset(assets):
            raise ValueError("--vision requires --asset mmproj=... and --asset vision_exe=...")
        engine_args.append("--vision")
    if args.esp:
        if "esp" not in assets:
            raise ValueError("--esp requires --asset esp=...")
        engine_args += ["--control-vector-scaled", assets["esp"] + ":1.0",
                        "--control-vector-layer-range", "4", "44",
                        "--cvec-mode", "project", "--cvec-dir", "per-layer"]
    engine_args += ["--adapt-every", str(args.adapt_every)]

    config = {
        "exe": exe,
        "args": engine_args,
        "cwd": engine_root,
        "tokenizer": assets["tokenizer"],
        "model_name": args.model_name,
        "log": _absolute(str(Path(args.output).resolve().parent / "logs" / "engine.log")),
        "env": {"STRATA_DECODE_TIMING": "1", "STRATA_ADAPT_NOWAIT": "0",
                "STRATA_ADAPT_VERIFY": "1"},
        "managed": {"python": python, "api_root": api_root,
                    "port": args.port, "gpu_uuid": args.gpu_uuid,
                    "auth_file": auth_file,
                    "task_name": args.task_name or default_task,
                    "state": _absolute(str(Path(args.output).resolve().parent / "service_state.json"))},
        "assets": assets,
        "client_policy": {"thinking": "client-controlled", "reasoning_parameters": "not set by launcher"},
        "esp_policy": "added only when explicitly requested; client thinking parameters remain unchanged",
    }
    if args.vision:
        config["vision"] = {"exe": assets["vision_exe"], "mmproj": assets["mmproj"],
                            "model": assets["native"]}
        config["mmproj"] = assets["mmproj"]
    return config


def _walk_secret_keys(value, where="config"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in SECRET_FIELDS:
                raise ValueError(f"refusing secret-like config field at {where}.{key}")
            _walk_secret_keys(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_secret_keys(child, f"{where}[{index}]")


def _command(config_path: Path, operation: str) -> list[str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    managed = config["managed"]
    module_root = Path(__file__).resolve().parent
    common = [managed["python"], str(module_root / "guarded_start.py"),
              "--config", str(config_path.resolve()), "--server-root", managed["api_root"],
              "--port", str(managed["port"]), "--gpu-uuid", managed["gpu_uuid"],
              "--python", managed["python"], "--auth-file", managed["auth_file"],
              "--state", managed["state"]]
    if operation == "start":
        return common + ["--task-name", managed["task_name"]]
    if operation == "stop":
        return [managed["python"], str(module_root / "service_control.py"), "stop",
                "--state", managed["state"], "--port", str(managed["port"]),
                "--auth-file", managed["auth_file"]]
    if operation == "dry-run":
        return []
    raise ValueError(operation)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render a portable Strata service config and command wrappers")
    sub = ap.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--output", required=True)
    gen.add_argument("--engine-root", required=True)
    gen.add_argument("--engine-exe", required=True)
    gen.add_argument("--python", required=True)
    gen.add_argument("--api-root", required=True)
    gen.add_argument("--gpu-uuid", required=True)
    gen.add_argument("--port", type=int, required=True)
    gen.add_argument("--auth-file", required=True,
                     help="external JSON path with api_key; the file and key are never copied")
    gen.add_argument("--task-name")
    gen.add_argument("--asset", action="append", default=[])
    gen.add_argument("--model-name", default="Strata")
    gen.add_argument("--context", type=int, default=131072)
    gen.add_argument("--spec", type=int, default=3)
    gen.add_argument("--prefill", default="auto")
    gen.add_argument("--expert-cache", default="auto")
    gen.add_argument("--adapt-every", type=int, default=4)
    gen.add_argument("--vision", action="store_true")
    gen.add_argument("--esp", action="store_true")
    run = sub.add_parser("show-command")
    run.add_argument("--config", required=True)
    run.add_argument("--operation", choices=("dry-run", "start", "stop"), default="dry-run")
    execute = sub.add_parser("run")
    execute.add_argument("--config", required=True)
    execute.add_argument("--operation", choices=("start", "stop"), required=True)
    args = ap.parse_args(argv)
    if args.command == "generate":
        output = Path(_outside_toolkit(args.output, "--output"))
        config = build_config(args)
        _walk_secret_keys(config)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        module = Path(__file__).resolve()
        managed_python = config["managed"]["python"]
        wrappers = {
            "start.ps1": f"$ErrorActionPreference='Stop'\n& {_ps_quote(managed_python)} {_ps_quote(str(module))} run --config {_ps_quote(str(output))} --operation start\nexit $LASTEXITCODE\n",
            "stop.ps1": f"$ErrorActionPreference='Stop'\n& {_ps_quote(managed_python)} {_ps_quote(str(module))} run --config {_ps_quote(str(output))} --operation stop\nexit $LASTEXITCODE\n",
        }
        for name, content in wrappers.items():
            (output.parent / name).write_text(content, encoding="utf-8")
        print(output)
        return 0
    config_path = Path(args.config).expanduser().resolve(strict=True)
    operation = args.operation
    if operation == "dry-run":
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        print(json.dumps({"python": cfg["managed"]["python"], "api_root": cfg["managed"]["api_root"],
                          "port": cfg["managed"]["port"], "gpu_uuid": cfg["managed"]["gpu_uuid"],
                          "engine_exe": cfg["exe"], "engine_args": cfg["args"], "spawned": False},
                         ensure_ascii=False, indent=2))
        return 0
    cmd = _command(config_path, operation)
    if args.command == "run":
        return subprocess.run(cmd, check=False).returncode
    print(subprocess.list2cmdline(cmd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
