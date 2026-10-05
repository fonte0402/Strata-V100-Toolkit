#!/usr/bin/env python3
"""Collect the selected exact visual prompt for human review; never score or render output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES_FILE = HERE / 'visual_manual_cases.json'
SYSTEM_TEXT = 'Follow the user task exactly. Do not claim actions that were not performed.'
MODEL = 'Qwen3.8-Flash-Next'

def load_api_key():
    """Read the optional bearer key from the current process environment only."""
    return os.environ.get('STRATA_API_KEY', '').strip() or None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def path_fingerprint(value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    result = {'path': str(path)}
    if path.is_file():
        result.update({'exists': True, 'bytes': path.stat().st_size,
                       'sha256': sha256(path.read_bytes())})
    else:
        result['exists'] = False
    return result


def runtime_snapshot(manifest_path: Path, arm: str, base_url: str):
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw.decode('utf-8-sig'))
    state = manifest.get('state') if isinstance(manifest.get('state'), dict) else {}
    config_path = manifest.get('config_path') or state.get('config_path')
    config = {}
    if config_path and Path(config_path).is_file():
        config = read_json(Path(config_path))

    # Baseline manifests identify their actual server log. Candidate manifests
    # use the selected server config's log destination.
    if arm == 'baseline':
        log_path = manifest.get('log') or manifest.get('server_log') or state.get('server_log')
        log_source = 'runtime_manifest.server_log'
        if not log_path:
            fallback_log = manifest_path.parent / 'server.log'
            if fallback_log.is_file():
                log_path = fallback_log
                log_source = 'runtime_manifest_directory.server.log_fallback'
    else:
        log_path = config.get('log') or manifest.get('log') or state.get('server_log')
        log_source = 'candidate_config.log'

    executable = (config.get('exe') or manifest.get('server_executable') or
                  manifest.get('exe_path') or state.get('exe_path'))
    head_evidence_path = PHASE0 / 'evidence' / 'lead-20261004' / 'native-output-head-sha.json'
    head_evidence = None
    if head_evidence_path.is_file():
        head_raw = head_evidence_path.read_bytes()
        head_evidence = {'path': str(head_evidence_path), 'sha256': sha256(head_raw),
                         'evidence': json.loads(head_raw.decode('utf-8-sig'))}

    manifest_head = next((manifest.get(k) for k in ('git_head', 'head', 'commit', 'revision')
                          if manifest.get(k)), None)
    url = urllib.parse.urlsplit(base_url)
    return {
        'manifest_path': str(manifest_path.resolve()),
        'manifest_sha256': sha256(raw),
        'manifest_status': manifest.get('status'),
        'run_id': manifest.get('run_id'),
        'server_root': manifest.get('server_root'),
        'server_sha256': manifest.get('server_sha256'),
        'port': manifest.get('port') or (manifest.get('gate') or {}).get('port') or state.get('port'),
        'base_url_port': url.port,
        'base_url_port_matches': (url.port == (manifest.get('port') or
                                   (manifest.get('gate') or {}).get('port') or state.get('port'))),
        'config': path_fingerprint(config_path),
        'startup_reasoning_budget_tokens': config.get('reasoning_budget_tokens'),
        'configured_executable': path_fingerprint(executable),
        'supervisor_executable': path_fingerprint(state.get('exe_path')),
        'config_executable_sha256_expected': manifest.get('exe_sha256'),
        'config_sha256_expected': manifest.get('config_sha256'),
        'head': manifest_head,
        'head_evidence': head_evidence,
        'server_log_path': str(Path(log_path).resolve()) if log_path else None,
        'log_source': log_source,
        'note': 'Snapshot evidence only; no live-process assertion is made.'
    }


class LogWatcher:
    def __init__(self, log_path: str | None, out_path: Path, interval: float = 1.0):
        self.log_path = Path(log_path) if log_path else None
        self.out_path = out_path
        self.interval = interval
        self.start = time.perf_counter()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='visual-log-watch', daemon=True)
        self.offset = 0
        if self.log_path and self.log_path.is_file():
            self.offset = self.log_path.stat().st_size

    def start_watch(self):
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop_watch(self):
        self.stop_event.set()
        self.thread.join(timeout=max(2.0, self.interval + 1.0))

    def _sample(self, stream):
        now = datetime.now(timezone.utc).isoformat()
        record = {'observed_utc': now, 'elapsed_seconds': round(time.perf_counter() - self.start, 6),
                  'path': str(self.log_path) if self.log_path else None}
        try:
            if not self.log_path or not self.log_path.is_file():
                record.update({'available': False, 'new_text': ''})
            else:
                size = self.log_path.stat().st_size
                if size < self.offset:  # log rotation/truncation
                    self.offset = 0
                    record['rotated_or_truncated'] = True
                with self.log_path.open('rb') as log:
                    log.seek(self.offset)
                    fresh = log.read()
                    self.offset += len(fresh)
                record.update({'available': True, 'size_bytes': size,
                               'new_text': fresh.decode('utf-8', errors='replace')})
        except OSError as exc:
            record.update({'available': False, 'read_error': type(exc).__name__, 'new_text': ''})
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()

    def _run(self):
        with self.out_path.open('w', encoding='utf-8', newline='\n') as stream:
            self._sample(stream)
            while not self.stop_event.wait(self.interval):
                self._sample(stream)
            self._sample(stream)


def text_delta(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(item.get('text', '') for item in value if isinstance(item, dict))
    return ''


def extract_artifact(text: str, artifact_type: str):
    """Return an unchanged response substring and extraction offsets; never repairs markup."""
    if artifact_type == 'svg':
        start_re = re.compile(r'<svg(?=[\s>])', re.I)
        end_re = re.compile(r'</svg\s*>', re.I)
        fenced_type = re.compile(r'(?is)```[^\r\n]*\r?\n(.*?)```')
        valid_start, valid_end = start_re, end_re
    else:
        start_re = re.compile(r'(?is)<!doctype\s+html\b|<html(?=[\s>])')
        end_re = re.compile(r'(?is)</html\s*>')
        fenced_type = re.compile(r'(?is)```[^\r\n]*\r?\n(.*?)```')
        valid_start, valid_end = start_re, end_re

    for match in fenced_type.finditer(text):
        block = match.group(1)
        start = valid_start.search(block)
        if start:
            end = valid_end.search(block, start.end())
            if end:
                return {'text': block[start.start():end.end()], 'method': 'fenced_block',
                        'start_char': match.start(1) + start.start(),
                        'end_char': match.start(1) + end.end(), 'complete_markup': True}
            return {'text': block[start.start():], 'method': 'fenced_block_partial',
                    'start_char': match.start(1) + start.start(), 'end_char': match.end(1),
                    'complete_markup': False}

    start = valid_start.search(text)
    if not start:
        return None
    end = valid_end.search(text, start.end())
    if end:
        return {'text': text[start.start():end.end()], 'method': 'first_tag_to_closing_tag',
                'start_char': start.start(), 'end_char': end.end(), 'complete_markup': True}
    return {'text': text[start.start():], 'method': 'first_tag_partial_to_end',
            'start_char': start.start(), 'end_char': len(text), 'complete_markup': False}


def stream_one(case, args, run_dir: Path, log_path: str | None, api_key: str | None):
    case_dir = run_dir / case['id']
    case_dir.mkdir(parents=True, exist_ok=False)
    prompt = case['prompt']
    body = {
        'model': MODEL,
        'messages': [{'role': 'system', 'content': SYSTEM_TEXT},
                     {'role': 'user', 'content': prompt}],
        'max_tokens': args.max_tokens,
        'temperature': 0,
        'seed': 42,
        'top_p': 1,
        'top_k': 0,
        'reasoning_effort': args.effort,
        'stream': True,
        'stream_options': {'include_usage': True},
    }
    budget_disabled = args.effort == 'none' and args.reasoning_budget_tokens == -1
    if not budget_disabled:
        body['reasoning_budget_tokens'] = args.reasoning_budget_tokens
    (case_dir / 'request.json').write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n',
                                            encoding='utf-8')
    content_parts, reasoning_parts = [], []
    usage, finish_reason = None, None
    first_content_seconds = None
    first_reasoning_seconds = None
    answer_delta_events = 0
    reasoning_delta_events = 0
    request_started = time.perf_counter()
    watcher = LogWatcher(log_path, case_dir / 'log_interval.jsonl')
    watcher.start_watch()
    status = None
    failure = None
    raw_response_body = bytearray()
    event_path = case_dir / 'events.jsonl'
    sse_path = case_dir / 'response.sse'
    with event_path.open('w', encoding='utf-8', newline='\n') as events, sse_path.open('wb') as raw_sse:
        url = args.base_url.rstrip('/') + '/v1/chat/completions'
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                         headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                status = response.status
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    raw_response_body.extend(raw_line)
                    raw_sse.write(raw_line)
                    raw_sse.flush()
                    line = raw_line.decode('utf-8', errors='replace')
                    if not line.startswith('data:'):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == '[DONE]':
                        events.write(json.dumps({'received_utc': datetime.now(timezone.utc).isoformat(),
                                                 'elapsed_seconds': round(time.perf_counter() - request_started, 6),
                                                 'data': payload}, ensure_ascii=False) + '\n')
                        events.flush()
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        events.write(json.dumps({'received_utc': datetime.now(timezone.utc).isoformat(),
                                                 'elapsed_seconds': round(time.perf_counter() - request_started, 6),
                                                 'raw_data': payload, 'parse_error': True}, ensure_ascii=False) + '\n')
                        events.flush()
                        continue
                    elapsed = time.perf_counter() - request_started
                    events.write(json.dumps({'received_utc': datetime.now(timezone.utc).isoformat(),
                                             'elapsed_seconds': round(elapsed, 6), 'event': event},
                                            ensure_ascii=False) + '\n')
                    events.flush()
                    if isinstance(event, dict) and event.get('usage') is not None:
                        usage = event['usage']
                    for choice in (event.get('choices') or []) if isinstance(event, dict) else []:
                        if choice.get('finish_reason') is not None:
                            finish_reason = choice['finish_reason']
                        delta = choice.get('delta') or {}
                        visible = text_delta(delta.get('content'))
                        if visible:
                            if first_content_seconds is None:
                                first_content_seconds = elapsed
                            content_parts.append(visible)
                            answer_delta_events += 1
                        for key in ('reasoning', 'reasoning_content', 'analysis'):
                            reasoning = text_delta(delta.get(key))
                            if reasoning:
                                if first_reasoning_seconds is None:
                                    first_reasoning_seconds = elapsed
                                reasoning_parts.append(reasoning)
                                reasoning_delta_events += 1
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw_response_body.extend(exc.read())
            failure = f'HTTP {exc.code}'
        except Exception as exc:  # one attempt only; no retries
            failure = f'{type(exc).__name__}: {exc}'
        finally:
            watcher.stop_watch()

    raw_response_body_path = case_dir / 'http_body.sse'
    raw_response_body_path.write_bytes(bytes(raw_response_body))
    content = ''.join(content_parts)
    reasoning = ''.join(reasoning_parts)
    (case_dir / 'raw_response.txt').write_text(content, encoding='utf-8')
    (case_dir / 'reasoning.txt').write_text(reasoning, encoding='utf-8')
    natural = finish_reason == 'stop'
    completion_state = 'natural' if natural else ('truncated' if finish_reason == 'length' else 'unknown')
    extraction = extract_artifact(content, case['artifact_type']) if content else None
    artifact_name = f"model_output.{case['artifact_type']}"
    artifact_path = None
    if extraction:
        artifact_path = case_dir / artifact_name
        artifact_path.write_text(extraction['text'], encoding='utf-8')
    review_ready = bool(natural and extraction and extraction['complete_markup'] and not failure and
                        status is not None and 200 <= status < 300)
    result = {
        'case_id': case['id'], 'prompt': prompt, 'prompt_sha256_utf8': sha256(prompt.encode('utf-8')),
        'requested_effort': args.effort,
        'requested_reasoning_budget_tokens': args.reasoning_budget_tokens,
        'effective_reasoning_budget_tokens': body.get('reasoning_budget_tokens'),
        'reasoning_budget_mode': ('disabled' if budget_disabled else
                                  'unlimited' if args.reasoning_budget_tokens == -1 else 'bounded'),
        'max_tokens_total_completion_budget': args.max_tokens,
        'http_status': status, 'failure': failure, 'completion_state': completion_state,
        'finish_reason': finish_reason, 'usage': usage,
        'wall_seconds': round(time.perf_counter() - request_started, 6),
        'first_content_seconds': (round(first_content_seconds, 6)
                                  if first_content_seconds is not None else None),
        'first_reasoning_seconds': (round(first_reasoning_seconds, 6)
                                    if first_reasoning_seconds is not None else None),
        'stream_observation': {'answer_delta_events': answer_delta_events,
                               'reasoning_delta_events': reasoning_delta_events,
                               'answer_characters': len(content),
                               'reasoning_characters': len(reasoning)},
        'artifact_type': case['artifact_type'],
        'artifact_path': str(artifact_path.resolve()) if artifact_path else None,
        'artifact_extraction': ({k: v for k, v in extraction.items() if k != 'text'} if extraction else None),
        'review_status': 'user_manual_pending' if review_ready else 'not_review_ready',
        'files': {'request': 'request.json', 'events': 'events.jsonl', 'raw_sse': 'response.sse',
                  'http_body': 'http_body.sse', 'raw_response_text': 'raw_response.txt',
                  'reasoning': 'reasoning.txt', 'log_interval': 'log_interval.jsonl'},
    }
    (case_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                          encoding='utf-8')
    return result


def write_index(out_dir: Path):
    links = []
    prompts = {'visual-01': ('English prompt · SVG', 'svg')}
    for arm in ('baseline', 'candidate'):
        for effort in ('none', 'low', 'medium', 'high'):
            run_dir = out_dir / arm / effort
            links.append(f'<h2>{arm} / effort={effort}</h2>')
            for case_id, (label, ext) in prompts.items():
                case_dir = run_dir / case_id
                if not case_dir.is_dir():
                    links.append(f'<p>{label}: 等待运行；预期路径 '
                                 f'<code>{arm}/{effort}/{case_id}/</code></p>')
                    continue
                result_path = case_dir / 'result.json'
                try:
                    result = read_json(result_path)
                except (OSError, ValueError):
                    result = {}
                status = result.get('review_status', 'not_review_ready')
                links.append(f'<p>{label}: {status}</p><ul>')
                for filename, visible in ((f'model_output.{ext}', '模型产物'),
                                          ('raw_response.txt', '原始正文'),
                                          ('reasoning.txt', '原始思考'),
                                          ('response.sse', '原始SSE'),
                                          ('events.jsonl', '事件/usage'),
                                          ('log_interval.jsonl', '服务日志区间'),
                                          ('result.json', 'finish/usage/时间摘要')):
                    if (case_dir / filename).is_file():
                        target = (Path(arm) / effort / case_id / filename).as_posix()
                        links.append(f'<li><a href="{target}">{visible}</a></li>')
                links.append('</ul>')

    # Add the separately collected candidate/none run from the main controller,
    # if it has completed and its evidence directory is discoverable.
    cases_hash = sha256(CASES_FILE.read_bytes())
    external_roots = [PHASE0 / 'evidence' / 'lead-20261004',
                      HERE / 'visual_manual_runs']
    seen_external = set()
    for external_root in external_roots:
      if not external_root.is_dir():
        continue
      for manifest_path in external_root.glob('*/manifest.json'):
        try:
            ext_manifest = read_json(manifest_path)
            if ext_manifest.get('cases_sha256') != cases_hash or ext_manifest.get('arm') != 'candidate':
                continue
            evidence_dir = manifest_path.parent
            if evidence_dir in seen_external:
                continue
            request_path = evidence_dir / 'visual-01.request.json'
            req = read_json(request_path) if request_path.is_file() else {}
            if req.get('reasoning_effort') != 'none':
                continue
            rel_dir = Path(os.path.relpath(evidence_dir, out_dir)).as_posix()
            seen_external.add(evidence_dir)
            links.append('<h2>candidate / effort=none · 主控既有采集</h2>')
            for case_id, (label, ext) in prompts.items():
                links.append(f'<p>{label}</p><ul>')
                for filename, visible in ((f'{case_id}.{ext}', '模型产物'),
                                          (f'{case_id}.raw.txt', '原始正文'),
                                          (f'{case_id}.response.json', '原始事件/思考/usage'),
                                          (f'{case_id}.engine.log', '服务日志')):
                    if (evidence_dir / filename).is_file():
                        links.append(f'<li><a href="{rel_dir}/{filename}">{visible}</a></li>')
                links.append('</ul>')
        except (OSError, ValueError, TypeError):
            continue
    html = ('<!doctype html>\n<html lang="zh-CN"><meta charset="utf-8">'
            '<title>视觉生成人工评审</title><body><h1>视觉生成人工评审</h1>'
            '<p>人工评审待进行；none档关闭思考并省略预算字段，low/medium/high发送'
            'reasoning_budget_tokens=-1（不设思考预算上限）；max_tokens=16384（或请求指定值）'
            '仍是思考与正文共用的总 completion 上限。'
            '页面仅列出原始证据链接，不自动评分、渲染或执行生成内容。</p>' +
            ''.join(links) + '</body></html>\n')
    (out_dir / 'index.html').write_text(html, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=('candidate', 'baseline'), required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--runtime-manifest', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--max-tokens', type=int, default=16384)
    parser.add_argument('--case', choices=('visual-01',), default='visual-01',
                        help='Only the English SVG prompt is active for current comparisons.')
    parser.add_argument('--effort', choices=('none', 'low', 'medium', 'high'), default='none')
    parser.add_argument('--reasoning-budget-tokens', type=int, default=-1,
                        help='Use -1 (default) for unlimited thinking budget or a positive explicit limit; zero is invalid.')
    parser.add_argument('--allow-no-auth', action='store_true',
                        help='Only for a local unauthenticated llama.cpp baseline.')
    parser.add_argument('--timeout', type=float, default=1800)
    args = parser.parse_args(argv)
    url = urllib.parse.urlsplit(args.base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        parser.error('--base-url must be an HTTP(S) origin without embedded credentials')
    if args.max_tokens < 1 or args.timeout <= 0:
        parser.error('--max-tokens and --timeout must be positive')
    if args.reasoning_budget_tokens < -1 or args.reasoning_budget_tokens == 0:
        parser.error('--reasoning-budget-tokens must be -1 or a positive integer')
    if args.arm == 'baseline':
        if not args.allow_no_auth:
            parser.error('baseline requires explicit --allow-no-auth')
        if url.hostname.casefold() not in ('localhost', '127.0.0.1', '::1'):
            parser.error('--allow-no-auth is restricted to a local baseline endpoint')
    elif args.allow_no_auth:
        parser.error('--allow-no-auth is only valid for baseline')
    if args.arm == 'candidate' and args.effort == 'none':
        parser.error('candidate/none already exists with the exact prompt; reuse the linked original capture')

    cases_data = read_json(CASES_FILE)
    cases = [case for case in cases_data['cases'] if case['id'] == args.case]
    args.out_dir = args.out_dir.resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    run_dir = args.out_dir / args.arm / args.effort
    run_dir.mkdir(parents=True, exist_ok=True)
    if any((run_dir / case['id']).exists() for case in cases):
        parser.error(f'{run_dir} already contains artifacts; use a fresh --out-dir to preserve evidence')
    try:
        runtime = runtime_snapshot(args.runtime_manifest.resolve(strict=True), args.arm, args.base_url)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f'invalid runtime manifest: {type(exc).__name__}: {exc}')

    api_key = None
    if args.arm == 'candidate':
        try:
            api_key = load_api_key()
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f'credential helper could not load candidate authentication: {type(exc).__name__}')
        if not api_key:
            parser.error('candidate authentication unavailable through the existing auth helper')

    manifest = {
        'schema_version': 1, 'kind': 'visual_manual_run', 'run_id': run_id,
        'created_utc': datetime.now(timezone.utc).isoformat(), 'arm': args.arm,
        'base_url': args.base_url, 'model': MODEL, 'selected_case': args.case,
        'cases_file': str(CASES_FILE),
        'cases_sha256': sha256(CASES_FILE.read_bytes()), 'runtime': runtime,
        'request_defaults': {'system': SYSTEM_TEXT, 'temperature': 0, 'seed': 42,
                             'top_p': 1, 'top_k': 0, 'reasoning_effort': 'none',
                             'max_tokens': args.max_tokens, 'stream': True},
        'requested_effort': args.effort,
        'requested_reasoning_budget_tokens': args.reasoning_budget_tokens,
        'reasoning_budget_tokens': (None if args.effort == 'none' and args.reasoning_budget_tokens == -1
                                    else args.reasoning_budget_tokens),
        'reasoning_budget_mode': ('disabled' if args.effort == 'none' and args.reasoning_budget_tokens == -1
                                  else 'unlimited' if args.reasoning_budget_tokens == -1 else 'bounded'),
        'max_tokens_total_completion_budget': args.max_tokens,
        'auth_mode': 'none' if args.allow_no_auth else 'existing_helper',
        'api_key_recorded': False, 'review_status': 'user_manual_pending',
        'note': 'No automatic grading or rendering. Artifact extraction preserves response substrings unchanged.'
    }
    manifest['request_defaults']['reasoning_effort'] = args.effort
    (run_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                           encoding='utf-8')
    (run_dir / 'results.jsonl').write_text('', encoding='utf-8')
    write_index(args.out_dir)
    for case in cases:
        case_dir = run_dir / case['id']
        try:
            result = stream_one(case, args, run_dir, runtime.get('server_log_path'), api_key)
        except Exception as exc:  # fail closed and stop; never retry a visual request
            case_dir.mkdir(parents=True, exist_ok=True)
            result = {'case_id': case['id'], 'prompt': case['prompt'],
                      'prompt_sha256_utf8': sha256(case['prompt'].encode('utf-8')),
                      'failure': f'{type(exc).__name__}: {exc}', 'completion_state': 'unknown',
                      'finish_reason': None, 'review_status': 'not_review_ready'}
            (case_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                                  encoding='utf-8')
        with (run_dir / 'results.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
            stream.flush()
        print(f"{case['id']}: {result.get('review_status')} (finish={result.get('finish_reason')})", flush=True)
        if result.get('failure') or result.get('http_status') is None or not (200 <= result.get('http_status', 0) < 300):
            break
    write_index(args.out_dir)
    print(f'run evidence: {run_dir}', flush=True)
    print(f'index: {args.out_dir / "index.html"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
