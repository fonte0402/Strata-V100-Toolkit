#!/usr/bin/env python3
"""Fail-closed Windows gates and ownership-aware Strata service control.

This module intentionally never stores or prints API credentials or process command
lines. A service state file identifies only the Python server root and its assets.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

DEFAULT_STATE = Path(__file__).with_name("service_state.json")
DEFAULT_GPU_UUID = ""
DEFAULT_PORT = None
DEFAULT_MIN_COMMIT_GIB = 48.0
FORCED_EXIT_WAIT_SECONDS = 8.0


class GateError(RuntimeError):
    """A preflight or ownership check failed; callers must refuse the action."""


def _run(args: list[str], *, timeout: int = 15) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"无法运行 {Path(args[0]).name}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
        raise GateError(f"{Path(args[0]).name} 查询失败: {detail[:300]}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    except OSError as exc:
        raise GateError(f"无法读取指纹文件 {path}: {exc}") from exc
    return h.hexdigest()


def _netstat_listeners(port: int) -> set[int]:
    out = _run(["netstat", "-ano", "-p", "tcp"])
    owners: set[int] = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[3].upper() != "LISTENING":
            continue
        local = fields[1]
        if local.rsplit(":", 1)[-1] == str(port):
            try:
                owners.add(int(fields[-1]))
            except ValueError as exc:
                raise GateError(f"端口 {port} 的监听PID无法解析") from exc
    return owners


def _gpu_memory_by_uuid() -> dict[str, dict[str, int | str]]:
    out = _run(["nvidia-smi", "--query-gpu=uuid,name,memory.used",
                "--format=csv,noheader,nounits"])
    result: dict[str, int] = {}
    for line in out.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 3 or not fields[0] or not fields[1]:
            raise GateError("nvidia-smi 返回格式无效")
        try:
            used = int(fields[2])
        except ValueError as exc:
            raise GateError(f"GPU {fields[0]} 显存读数非数值") from exc
        if fields[0] in result:
            raise GateError(f"GPU UUID 重复: {fields[0]}")
        result[fields[0]] = {"name": fields[1], "memory_used_mib": used}
    if not result:
        raise GateError("nvidia-smi 未返回GPU")
    return result


def _free_commit_gib() -> float:
    ps = ("$ErrorActionPreference='Stop'; "
          "$v=(Get-CimInstance Win32_OperatingSystem).FreeVirtualMemory; "
          "if ($null -eq $v) { throw 'FreeVirtualMemory unavailable' }; "
          "$n=([double]$v / 1048576.0); "
          "[Console]::Write($n.ToString([System.Globalization.CultureInfo]::InvariantCulture))")
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    try:
        value = float(out)
    except ValueError as exc:
        raise GateError("系统commit余量读数非数值") from exc
    if not (value >= 0.0 and value < float("inf")):
        raise GateError("系统commit余量读数无效")
    return value


def _processes() -> dict[int, dict]:
    ps = ("$ErrorActionPreference='Stop'; "
          "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate,CommandLine | "
          "ConvertTo-Json -Compress")
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20)
    try:
        items = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GateError("Windows进程清单格式无效") from exc
    if isinstance(items, dict):
        items = [items]
    try:
        return {int(p["ProcessId"]): p for p in items}
    except (TypeError, KeyError, ValueError) as exc:
        raise GateError("Windows进程清单缺少PID") from exc


def _descendants(processes: dict[int, dict], root_pid: int) -> set[int]:
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, proc in processes.items():
            try:
                parent = int(proc.get("ParentProcessId", -1))
            except (TypeError, ValueError):
                continue
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def _assert_config(config_path: str | os.PathLike) -> Path:
    path = Path(config_path).resolve(strict=True)
    if not path.is_file():
        raise GateError(f"配置不是文件: {path}")
    try:
        json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"配置无法读取或不是有效JSON: {path}") from exc
    return path


def preflight(config_path: str | os.PathLike, port: int = DEFAULT_PORT,
              expected_gpu_uuid: str = DEFAULT_GPU_UUID,
              min_commit_gib: float = DEFAULT_MIN_COMMIT_GIB) -> dict:
    """Check the target config, port, exact GPU UUID and system commit headroom."""
    config = _assert_config(config_path)
    if port is None or not (1 <= int(port) <= 65535):
        raise GateError("端口范围无效")
    if not expected_gpu_uuid:
        raise GateError("必须指定目标GPU UUID")
    listeners = _netstat_listeners(int(port))
    if listeners:
        raise GateError(f"端口 {port} 已被监听，PID={','.join(map(str, sorted(listeners)))}")
    processes = _processes()
    engine_pids = sorted(pid for pid, proc in processes.items()
                         if str(proc.get("Name", "")).casefold() in
                         {"strata.exe", "strata-vision.exe"})
    if engine_pids:
        raise GateError("检测到正在运行的Strata引擎PID=" + ",".join(map(str, engine_pids)))
    gpu = _gpu_memory_by_uuid()
    if expected_gpu_uuid not in gpu:
        raise GateError(f"目标GPU UUID不存在: {expected_gpu_uuid}")
    gpu_name = str(gpu[expected_gpu_uuid]["name"])
    used = int(gpu[expected_gpu_uuid]["memory_used_mib"])
    if used > 1024:
        raise GateError(f"目标GPU显存占用 {used} MiB > 1024 MiB")
    commit = _free_commit_gib()
    if commit < float(min_commit_gib):
        raise GateError(f"系统commit余量 {commit:.1f} GiB < {min_commit_gib:.1f} GiB")
    return {"ok": True, "config_path": str(config), "port": int(port),
            "gpu_uuid": expected_gpu_uuid, "gpu_name": gpu_name, "gpu_used_mib": used,
            "free_commit_gib": commit, "listeners": sorted(listeners)}


def _root_record(pid: int, config: Path, port: int, gpu_uuid: str,
                 cwd: str | os.PathLike | None = None) -> dict:
    processes = _processes()
    proc = processes.get(int(pid))
    if proc is None:
        raise GateError(f"服务根PID不存在: {pid}")
    exe_raw = proc.get("ExecutablePath")
    if not exe_raw:
        raise GateError("服务根进程没有可验证的可执行文件路径")
    exe = Path(str(exe_raw)).resolve(strict=True)
    created = str(proc.get("CreationDate") or "")
    if not created:
        raise GateError("服务根进程没有可验证的创建时间")
    if "python" not in exe.name.casefold():
        raise GateError("服务根进程不是Python服务进程")
    cmd = str(proc.get("CommandLine") or "").casefold()
    if "serve.server" not in cmd or str(config).casefold() not in cmd:
        raise GateError("服务进程命令未匹配预期serve.server与配置")
    listeners = _netstat_listeners(int(port))
    tree = _descendants(processes, int(pid))
    if not listeners or not listeners.issubset(tree):
        raise GateError(f"端口 {port} 监听者不属于服务PID树")
    if cwd is None:
        raise GateError("必须明确提供服务工作目录")
    workdir = Path(cwd).resolve(strict=True)
    if not workdir.is_dir():
        raise GateError("服务工作目录无法验证")
    return {"schema_version": 1, "root_pid": int(pid),
            "root_created_utc": created, "port": int(port), "gpu_uuid": gpu_uuid,
            "config_path": str(config), "config_sha256": _sha256(config),
            "cwd": str(workdir), "exe_path": str(exe), "exe_sha256": _sha256(exe),
            "registered_utc": datetime.now(timezone.utc).isoformat()}


def register_service(pid: int, config_path: str | os.PathLike,
                     port: int,
                     gpu_uuid: str,
                     state_path: str | os.PathLike = DEFAULT_STATE,
                     cwd: str | os.PathLike | None = None) -> dict:
    """Persist a verified server-root identity; credentials and command lines are excluded."""
    config = _assert_config(config_path)
    record = _root_record(int(pid), config, int(port), gpu_uuid, cwd)
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return record


def _load_state(state_path: str | os.PathLike) -> dict:
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"服务状态文件无法读取: {state_path}") from exc
    required = {"schema_version", "root_pid", "root_created_utc", "port", "gpu_uuid",
                "config_path", "config_sha256", "cwd", "exe_path", "exe_sha256"}
    if not isinstance(state, dict) or not required.issubset(state) or state["schema_version"] != 1:
        raise GateError("服务状态文件字段不完整或版本不支持")
    return state


def _verified_tree(state: dict, require_listener: bool = True) -> tuple[dict[int, dict], set[int]]:
    pid = int(state["root_pid"])
    processes = _processes()
    proc = processes.get(pid)
    if proc is None:
        raise GateError("状态记录的服务根进程已退出")
    if str(proc.get("CreationDate") or "") != str(state["root_created_utc"]):
        raise GateError("PID创建时间不匹配，可能已复用；拒绝停止")
    exe = Path(str(proc.get("ExecutablePath") or "")).resolve(strict=True)
    if str(exe).casefold() != str(state["exe_path"]).casefold() or _sha256(exe) != state["exe_sha256"]:
        raise GateError("服务可执行文件指纹不匹配；拒绝停止")
    cmd = str(proc.get("CommandLine") or "").casefold()
    if "serve.server" not in cmd or str(state["config_path"]).casefold() not in cmd:
        raise GateError("服务根进程命令与登记的server/config路径不匹配；拒绝停止")
    tree = _descendants(processes, pid)
    if require_listener:
        listeners = _netstat_listeners(int(state["port"]))
        if not listeners or not listeners.issubset(tree):
            raise GateError("目标端口当前监听者与已登记服务树不匹配；拒绝停止")
    return processes, tree


def _request_shutdown(state: dict, auth_file: str | os.PathLike | None = None) -> bool:
    """Called only AFTER PID/listener identity verification. No key in argv/logs."""
    try:
        from guarded_start import key_value
        req = urllib.request.Request(f"http://127.0.0.1:{int(state['port'])}/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key_value(auth_file)})
        with urllib.request.urlopen(req, timeout=3) as response:
            body = json.load(response)
            return response.status == 202 and body.get("status") == "stopping"
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (OSError, ValueError, KeyError):
        return False  # Old server: use the same ownership-checked fallback.


def stop_service(state_path: str | os.PathLike = DEFAULT_STATE,
                 timeout: float = 45.0, dry_run: bool = False,
                 port: int | None = None,
                 auth_file: str | os.PathLike | None = None) -> dict:
    if not Path(state_path).exists():
        if port is None or not (1 <= int(port) <= 65535):
            raise GateError("状态文件缺失时必须明确指定端口")
        if _netstat_listeners(int(port)):
            raise GateError("端口已被未登记实例占用，拒绝停止其他服务")
        return {"ok": True, "already_stopped": True, "port": int(port)}
    state = _load_state(state_path)
    try:
        processes, tree = _verified_tree(state)
    except GateError:
        if not _netstat_listeners(int(state["port"])) and int(state["root_pid"]) not in _processes():
            if not dry_run:
                Path(state_path).unlink(missing_ok=True)
            return {"ok": True, "already_stopped": True, "port": int(state["port"])}
        raise
    pid = int(state["root_pid"])
    identities = {}
    creation_times = {}
    for owned_pid in tree:
        proc = processes.get(owned_pid, {})
        creation_times[owned_pid] = str(proc.get("CreationDate") or "")
        exe_raw = proc.get("ExecutablePath")
        if not exe_raw:
            continue
        exe = Path(str(exe_raw)).resolve(strict=True)
        identities[owned_pid] = (str(proc.get("CreationDate") or ""),
                                 str(exe).casefold(), _sha256(exe))
    if dry_run:
        return {"ok": True, "dry_run": True, "root_pid": pid,
                "tree_pids": sorted(tree), "port": int(state["port"])}
    # NO_WINDOW services have no console to receive CTRL_BREAK. Use the server's
    # own shutdown path; fall back to exact PIDs immediately for older servers.
    sent = _request_shutdown(state, auth_file)
    deadline = time.monotonic() + (max(0.0, float(timeout)) if sent else 0.0)
    while time.monotonic() < deadline:
        current = _processes()
        if not (tree & current.keys()):
            Path(state_path).unlink(missing_ok=True)
            return {"ok": True, "graceful_signal_sent": sent, "forced": False,
                    "root_pid": pid, "port": int(state["port"])}
        time.sleep(0.5)
    # Revalidate immediately before escalation. If the root already exited,
    # terminate only the exact descendants captured before the graceful signal.
    current = _processes()
    if pid in current:
        _verified_tree(state, require_listener=False)
        targets = [pid]
        tree_kill = True
    else:
        targets = []
        for child in sorted(tree - {pid}):
            proc = current.get(child)
            identity = identities.get(child)
            if proc is None:
                continue
            if identity is None:
                raise GateError(f"子进程PID {child} 身份无法复核；拒绝强制停止")
            exe_raw = proc.get("ExecutablePath")
            if not exe_raw:
                raise GateError(f"子进程PID {child} 可执行路径缺失；拒绝强制停止")
            exe = Path(str(exe_raw)).resolve(strict=True)
            observed = (str(proc.get("CreationDate") or ""), str(exe).casefold(), _sha256(exe))
            if observed != identity:
                raise GateError(f"子进程PID {child} 身份发生变化；拒绝强制停止")
            targets.append(child)
        tree_kill = False
    errors = []
    for target in targets:
        command = ["taskkill", "/PID", str(target)]
        if tree_kill:
            command.append("/T")
        command.append("/F")
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20)
        if result.returncode != 0:
            errors.append(result.stderr.strip()[:200] or result.stdout.strip()[:200])
    # taskkill reports before Windows has necessarily finished tearing down all
    # descendants. Wait briefly and identify each remaining PID by its original
    # creation time and executable fingerprint so PID reuse is not misreported.
    settle_deadline = time.monotonic() + FORCED_EXIT_WAIT_SECONDS
    survivors = []
    while True:
        after = _processes()
        survivors = []
        for owned_pid in sorted(tree):
            proc = after.get(owned_pid)
            if proc is None:
                continue
            if str(proc.get("CreationDate") or "") != creation_times.get(owned_pid):
                continue  # PID has exited and been reused by another process.
            identity = identities.get(owned_pid)
            if identity is not None:
                exe_raw = proc.get("ExecutablePath")
                if not exe_raw:
                    survivors.append(owned_pid)
                    continue
                exe = Path(str(exe_raw)).resolve(strict=True)
                if (str(exe).casefold(), _sha256(exe)) != identity[1:]:
                    continue  # Executable identity changed; it is not our process.
            survivors.append(owned_pid)
        if not survivors or time.monotonic() >= settle_deadline:
            break
        time.sleep(0.25)
    if survivors:
        raise GateError("优雅停止超时，以下已登记进程仍在运行: " + ",".join(map(str, survivors)) +
                        ("；" + "; ".join(errors) if errors else ""))
    Path(state_path).unlink(missing_ok=True)
    return {"ok": True, "graceful_signal_sent": sent, "forced": True,
            "root_pid": pid, "forced_pids": targets, "port": int(state["port"])}


def adopt_service(pid: int, config_path: str | os.PathLike, port: int,
                  gpu_uuid: str, state_path: str | os.PathLike = DEFAULT_STATE,
                  cwd: str | os.PathLike | None = None) -> dict:
    """Register an already-running server only after strict process and port checks."""
    config = _assert_config(config_path)
    if cwd is None:
        raise GateError("adopt必须提供并验证实际服务工作目录")
    _root_record(int(pid), config, int(port), gpu_uuid, cwd)
    return register_service(pid, config, port, gpu_uuid, state_path, cwd=cwd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strata服务资源门禁与精确生命周期控制")
    sub = ap.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--config", required=True)
    pre.add_argument("--port", type=int, required=True)
    pre.add_argument("--gpu-uuid", required=True)
    pre.add_argument("--min-commit-gib", type=float, default=DEFAULT_MIN_COMMIT_GIB)
    pre.add_argument("--json", action="store_true")
    adopt = sub.add_parser("adopt")
    adopt.add_argument("--pid", required=True, type=int)
    adopt.add_argument("--config", required=True)
    adopt.add_argument("--port", type=int, required=True)
    adopt.add_argument("--gpu-uuid", required=True)
    adopt.add_argument("--state", required=True)
    adopt.add_argument("--cwd", required=True)
    stop = sub.add_parser("stop")
    stop.add_argument("--state", required=True)
    stop.add_argument("--timeout", type=float, default=45)
    stop.add_argument("--dry-run", action="store_true")
    stop.add_argument("--port", type=int, required=True)
    stop.add_argument("--auth-file")
    args = ap.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(args.config, args.port, args.gpu_uuid, args.min_commit_gib)
        elif args.command == "adopt":
            result = adopt_service(args.pid, args.config, args.port, args.gpu_uuid,
                                  args.state, args.cwd)
        else:
            if not args.dry_run:
                print("正在关闭当前 Strata 并等待资源释放……", flush=True)
            result = stop_service(args.state, args.timeout, args.dry_run, args.port, args.auth_file)
    except (GateError, OSError, ValueError) as exc:
        print("[拒绝] " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
