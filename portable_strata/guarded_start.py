"""Fail-closed startup; direct Task Scheduler supervisor, no 40 GiB restart loop.
The on-demand task uses the interactive user. Logout/reboot are not certified.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent

def key_value(auth_file=None):
    key = os.environ.get('STRATA_API_KEY')
    if not key and auth_file:
        try:
            key = json.loads(Path(auth_file).read_text(encoding='utf-8-sig')).get('api_key')
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError('Could not read the explicitly configured external auth file.') from exc
    if not isinstance(key, str) or not key.strip():
        raise ValueError('Set STRATA_API_KEY or provide an external --auth-file containing api_key.')
    return key

def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--server-root', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--gpu-uuid', required=True)
    ap.add_argument('--python', required=True)
    ap.add_argument('--task-name', required=True)
    ap.add_argument('--auth-file')
    ap.add_argument('--state', required=True)
    ap.add_argument('--min-commit-gib', type=float, default=48)
    ap.add_argument('--ready-timeout', type=float, default=360)
    mode = ap.add_mutually_exclusive_group()
    for name in ('check-only', 'foreground', 'supervise'): mode.add_argument('--'+name, action='store_true')
    return ap.parse_args(argv)

def validate(a):
    if not (1 <= a.port <= 65535): raise ValueError('Port must be between 1 and 65535.')
    path = Path(a.config).resolve(strict=True)
    toolkit_root = ROOT.parent.resolve()
    for label, value in (('config', path), ('state', Path(a.state).resolve(strict=False))):
        if value == toolkit_root or toolkit_root in value.parents:
            raise ValueError(f'{label} must be stored outside the toolkit Git repository.')
    if a.auth_file:
        auth_path = Path(a.auth_file).resolve(strict=False)
        if auth_path == toolkit_root or toolkit_root in auth_path.parents:
            raise ValueError('auth file must remain outside the toolkit Git repository.')
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    Path(cfg['exe']).resolve(strict=True)
    Path(a.server_root, 'serve', 'server.py').resolve(strict=True)
    if cfg.get('gpu') is not None or 'CUDA_VISIBLE_DEVICES' in {str(k).upper() for k in cfg.get('env', {})}:
        raise ValueError('Config must not override managed GPU UUID selection.')
    return path, cfg

def contain_server(proc, server_root):
    sys.path.insert(0, str(Path(server_root).resolve()))
    from serve.winjob import contain
    return contain(proc)

def physical_gpu_index(gpu_uuid):
    result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,name', '--format=csv,noheader,nounits'],
                            capture_output=True, text=True, check=True, timeout=10)
    rows = [[v.strip() for v in line.split(',')] for line in result.stdout.splitlines()]
    matches = [int(row[0]) for row in rows if len(row) == 3 and row[1] == gpu_uuid and row[2]]
    if len(matches) != 1: raise ValueError('GPU UUID must map to exactly one physical monitor index.')
    return matches[0]

def supervised(a):
    from service_control import preflight, register_service
    path, cfg = validate(a)
    secret = key_value(a.auth_file)
    runtime_root = Path(a.state).resolve().parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / 'service.lock').open('a+b') as lock:
        if lock.tell() == 0: lock.write(b'0'); lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        gate = preflight(path, a.port, a.gpu_uuid, a.min_commit_gib)
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        run = runtime_root / 'service-runs' / run_id
        run.mkdir(parents=True)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=a.gpu_uuid, STRATA_API_KEY=secret, STRATA_DECODE_TIMING='1',
                   STRATA_MONITOR_GPU_INDEX=str(physical_gpu_index(a.gpu_uuid)),
                   STRATA_TOKEN_USAGE_DB=str(runtime_root / 'usage' / 'token-usage.sqlite3'))
        python = str(Path(a.python).resolve(strict=True))
        cmd = [python, '-m', 'serve.server', '--engine', 'strata', '--config', str(path), '--port', str(a.port), '--host', '0.0.0.0']
        with (run / 'server.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(cmd, cwd=a.server_root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            # The supervisor's job owns the root and every later descendant, even
            # if the server crashes before it manages to contain its own children.
            if not contain_server(proc, a.server_root):
                if proc.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=30)
                raise RuntimeError('Could not contain the server in the supervisor Windows Job; refusing startup.')
            manifest = dict(run_id=run_id, supervisor_pid=os.getpid(), root_pid=proc.pid,
                            config_path=str(path), config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            exe_sha256=hashlib.sha256(Path(cfg['exe']).read_bytes()).hexdigest(),
                            server_root=str(Path(a.server_root).resolve()),
                            server_sha256=hashlib.sha256(Path(a.server_root, 'serve', 'server.py').read_bytes()).hexdigest(),
                            gate=gate, status='starting', log=str(run / 'server.log'))
            def save(): (run / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            save()
            try:
                deadline = time.monotonic() + a.ready_timeout
                while time.monotonic() < deadline:
                    if proc.poll() is not None: raise RuntimeError('Server exited before ready: '+str(proc.returncode))
                    try:
                        req = urllib.request.Request(f'http://127.0.0.1:{a.port}/v1/models', headers={'Authorization': 'Bearer '+secret})
                        with urllib.request.urlopen(req, timeout=2) as response: models = json.load(response).get('data', [])
                    except (OSError, ValueError): models = []
                    if models and models[0].get('status', {}).get('value') == 'loaded':
                        state = register_service(proc.pid, path, a.port, a.gpu_uuid, a.state, cwd=a.server_root)
                        manifest.update(status='ready', ready_utc=datetime.now(timezone.utc).isoformat(), state=state)
                        save(); break
                    time.sleep(2)
                else: raise TimeoutError('Service startup deadline exceeded.')
                code = proc.wait()
                # A web shutdown exits cleanly without service_control deleting
                # its registration. Remove only this run's exact record.
                state_path = Path(a.state)
                try:
                    if json.loads(state_path.read_text(encoding='utf-8')) == manifest.get('state'):
                        state_path.unlink()
                except (OSError, ValueError):
                    pass
                manifest.update(status='stopped' if code == 0 else 'exited', exit_code=code, stopped_utc=datetime.now(timezone.utc).isoformat())
                save()
            except BaseException as exc:
                manifest.update(status='failed', error=str(exc)); save()
                if proc.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=30)
                raise

def background(a):
    from service_control import preflight
    path, _ = validate(a)
    if not a.auth_file or not Path(a.auth_file).is_file():
        raise ValueError('Background Task Scheduler does not inherit this shell environment; provide --auth-file outside the toolkit.')
    auth_path = Path(a.auth_file).resolve(strict=True)
    if ROOT.resolve() == auth_path or ROOT.resolve() in auth_path.parents:
        raise ValueError('The auth file must remain outside the toolkit Git repository.')
    key_value(a.auth_file); preflight(path, a.port, a.gpu_uuid, a.min_commit_gib)
    pythonw = Path(a.python).resolve(strict=True).with_name('pythonw.exe')
    if not pythonw.exists(): raise FileNotFoundError('pythonw.exe required.')
    argv = [str(Path(__file__).resolve()), '--supervise', '--config', str(path), '--server-root', str(Path(a.server_root).resolve()),
            '--port', str(a.port), '--gpu-uuid', a.gpu_uuid, '--python', str(Path(a.python).resolve()),
            '--task-name', a.task_name, '--auth-file', str(Path(a.auth_file).resolve()), '--state', str(Path(a.state).resolve()),
            '--min-commit-gib', str(a.min_commit_gib), '--ready-timeout', str(a.ready_timeout)]
    args_json = Path(a.state).resolve().with_name('managed_task_args.json')
    args_json.write_text(json.dumps(dict(execute=str(pythonw), arguments=subprocess.list2cmdline(argv),
                                        taskName=a.task_name, workingDirectory=str(Path(a.server_root).resolve())), ensure_ascii=False), encoding='utf-8')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'install_managed_task.ps1'),
                             '-ArgumentsPath', str(args_json)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    if result.returncode: raise RuntimeError('Task registration/start failed: '+result.stderr.strip()[:500])
    print('Background service dispatched; readiness is recorded in the configured state file.', flush=True)

def main():
    a = parse_args()
    try:
        if a.check_only:
            from service_control import preflight
            path, _ = validate(a)
            key_value(a.auth_file)
            print(json.dumps(preflight(path, a.port, a.gpu_uuid, a.min_commit_gib), ensure_ascii=False))
        elif a.foreground or a.supervise: supervised(a)
        else: background(a)
    except Exception as exc:
        if sys.stderr: print('[startup refused] '+str(exc), file=sys.stderr, flush=True)
        return 2
    return 0

if __name__ == '__main__': raise SystemExit(main())
