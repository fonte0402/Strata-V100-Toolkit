#!/usr/bin/env python3
"""Sequential, evidence-preserving Qwen3.8 qualification runner; never manages services."""
import argparse
import copy
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from validate import load_and_validate

HERE = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


_SENSITIVE_KEY = re.compile(r'(api[_-]?key|authorization|password|secret|credential|access[_-]?token|refresh[_-]?token|bearer)', re.I)
_SENSITIVE_ARG = re.compile(r'^--?(?:api[-_]?key|authorization|password|secret|token|credential)$', re.I)
_INLINE_SECRET_ARG = re.compile(r'(?i)(--?(?:api[-_]?key|authorization|password|secret|token|credential)=)[^\s]+')


def sanitize_runtime(value):
    """Copy public runtime metadata while removing credential-shaped fields/argv values."""
    if isinstance(value, dict):
        return {str(key): ('[REDACTED]' if _SENSITIVE_KEY.search(str(key))
                           else sanitize_runtime(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        result, redact_next = [], False
        for item in value:
            if redact_next:
                result.append('[REDACTED]')
                redact_next = False
                continue
            if isinstance(item, str) and _SENSITIVE_ARG.fullmatch(item):
                result.append(item)
                redact_next = True
            else:
                result.append(sanitize_runtime(item))
        return result
    if isinstance(value, str):
        value = re.sub(r'(?i)(Bearer\s+)[^\s,;]+', r'\1[REDACTED]', value)
        return _INLINE_SECRET_ARG.sub(r'\1[REDACTED]', value)
    return value


def file_sha256(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return path, digest.hexdigest()


def _manifest_port(manifest):
    candidates = [manifest.get('port'), (manifest.get('state') or {}).get('port'),
                  (manifest.get('gate') or {}).get('port')]
    values = []
    for value in candidates:
        if value is None:
            continue
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            raise ValueError('runtime manifest has a non-numeric port')
    distinct = sorted(set(values))
    if not distinct:
        raise ValueError('runtime manifest has no recorded port')
    if len(distinct) != 1:
        raise ValueError(f'runtime manifest contains conflicting ports: {distinct}')
    return distinct[0]


def load_runtime_manifest(path, base_url):
    """Validate and snapshot a ready supervisor/baseline manifest without contacting its endpoint."""
    manifest_path = Path(path).resolve(strict=True)
    raw_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw_bytes.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f'runtime manifest is not valid UTF-8 JSON: {exc}') from exc
    if not isinstance(manifest, dict):
        raise ValueError('runtime manifest must contain a JSON object')
    if manifest.get('status') != 'ready':
        raise ValueError(f'runtime manifest status must be ready (found {manifest.get("status")!r})')
    try:
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in ('http', 'https') or not parsed_url.hostname:
            raise ValueError('base URL must be an absolute HTTP(S) URL')
        url_port = parsed_url.port or (443 if parsed_url.scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError(f'invalid base URL: {exc}') from exc
    manifest_port = _manifest_port(manifest)
    if url_port != manifest_port:
        raise ValueError(f'base URL port {url_port} does not match ready manifest port {manifest_port}')

    config_path_value = manifest.get('config_path') or (manifest.get('state') or {}).get('config_path')
    expected_config_sha = manifest.get('config_sha256') or (manifest.get('state') or {}).get('config_sha256')
    expected_exe_sha = manifest.get('exe_sha256')
    files = [{'role': 'runtime_manifest', 'path': str(manifest_path),
              'sha256': sha(raw_bytes)}]
    config = None
    if config_path_value:
        if not expected_config_sha:
            raise ValueError('runtime manifest has config_path but no config_sha256')
        config_candidate = Path(config_path_value)
        if not config_candidate.is_absolute():
            config_candidate = manifest_path.parent / config_candidate
        config_path, actual_config_sha = file_sha256(config_candidate)
        if actual_config_sha.casefold() != str(expected_config_sha).casefold():
            raise ValueError('runtime config file SHA-256 changed since the ready manifest was written')
        try:
            config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'runtime config is not valid UTF-8 JSON: {exc}') from exc
        if not isinstance(config, dict):
            raise ValueError('runtime config must contain a JSON object')
        exe_value = config.get('exe')
        if not exe_value:
            raise ValueError('runtime config has no exe path')
        exe_candidate = Path(exe_value)
        if not exe_candidate.is_absolute():
            exe_candidate = config_path.parent / exe_candidate
        exe_path, actual_exe_sha = file_sha256(exe_candidate)
        if not expected_exe_sha:
            raise ValueError('runtime manifest has config_path but no exe_sha256')
        if actual_exe_sha.casefold() != str(expected_exe_sha).casefold():
            raise ValueError('configured executable SHA-256 changed since the ready manifest was written')
        files.extend([{'role': 'service_config', 'path': str(config_path), 'sha256': actual_config_sha},
                      {'role': 'server_executable', 'path': str(exe_path), 'sha256': actual_exe_sha}])
        runtime_config = {'source': 'config_path', 'path': str(config_path),
                          'sha256': actual_config_sha, 'configuration': sanitize_runtime(config),
                          'exe_path': str(exe_path), 'exe_sha256': actual_exe_sha,
                          'verified_against_ready_manifest': True}
    else:
        exe_value = manifest.get('exe_path')
        if not exe_value:
            raise ValueError('runtime manifest has neither config_path nor exe_path')
        exe_candidate = Path(exe_value)
        if not exe_candidate.is_absolute():
            exe_candidate = manifest_path.parent / exe_candidate
        exe_path, actual_exe_sha = file_sha256(exe_candidate)
        if not expected_exe_sha:
            raise ValueError('runtime manifest has exe_path but no exe_sha256')
        if actual_exe_sha.casefold() != str(expected_exe_sha).casefold():
            raise ValueError('manifest executable SHA-256 does not match the current file')
        files.append({'role': 'server_executable', 'path': str(exe_path), 'sha256': actual_exe_sha})
        runtime_config = {'source': 'manifest_launch_record', 'path': None, 'sha256': None,
                          'configuration': sanitize_runtime({
                              key: manifest.get(key) for key in ('model_path', 'context_size', 'n_cpu_moe',
                                  'model_alias', 'gpu_uuid', 'device', 'host', 'port', 'config_args',
                                  'config_args_sha256', 'working_directory') if key in manifest}),
                          'exe_path': str(exe_path), 'exe_sha256': actual_exe_sha,
                          'verified_against_ready_manifest': True}

    runtime_evidence = {
        'status': 'validated_ready_manifest',
        'manifest_path': str(manifest_path), 'manifest_sha256': sha(raw_bytes),
        'manifest': sanitize_runtime(manifest), 'files': files,
        'port_validation': {'base_url_port': url_port, 'manifest_port': manifest_port, 'matches': True},
        'validation': {'manifest_status_ready': True,
                       'config_hash_matches': True if config_path_value else None,
                       'executable_hash_matches': True},
        'note': 'This snapshots a ready manifest and matching files; it does not independently attest the live process.'}
    return runtime_evidence, runtime_config


def read_key():
    """Read the optional bearer key from this process only; never from a file."""
    key = os.environ.get('STRATA_API_KEY', '').strip()
    return key or None


def parse_json_content(content):
    if not isinstance(content, str):
        return None
    value = content.strip()
    match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', value, flags=re.I | re.S)
    if match:
        value = match.group(1)
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def schema_error(schema, value):
    if not isinstance(value, dict):
        return 'arguments must be a JSON object'
    props = schema.get('properties', {})
    if schema.get('additionalProperties') is False and set(value) - set(props):
        return 'unknown argument(s): ' + ', '.join(sorted(set(value) - set(props)))
    missing = set(schema.get('required', [])) - set(value)
    if missing:
        return 'missing required argument(s): ' + ', '.join(sorted(missing))
    for name, val in value.items():
        spec = props.get(name, {})
        typ = spec.get('type')
        ok = (typ == 'string' and isinstance(val, str) or
              typ == 'integer' and isinstance(val, int) and not isinstance(val, bool) or
              typ == 'number' and isinstance(val, (int, float)) and not isinstance(val, bool) or
              typ == 'boolean' and isinstance(val, bool) or
              typ == 'array' and isinstance(val, list) or typ == 'object' and isinstance(val, dict))
        if typ and not ok:
            return f'{name} must be {typ}'
        if 'enum' in spec and val not in spec['enum']:
            return f'{name} must be one of {spec["enum"]}'
        if typ == 'string' and 'pattern' in spec and not re.fullmatch(spec['pattern'], val):
            return f'{name} does not match required format'
        if typ == 'string' and len(val) < spec.get('minLength', 0):
            return f'{name} is shorter than minLength'
        if typ in ('integer', 'number'):
            if val < spec.get('minimum', float('-inf')) or val > spec.get('maximum', float('inf')):
                return f'{name} is outside allowed range'
    return None


def normalize_tool_calls(task, calls):
    """Canonicalize only task-declared exact string aliases; all other values stay literal."""
    normalized = json.loads(json.dumps(calls, ensure_ascii=False))
    for call in normalized:
        for alias in task.get('tool_argument_aliases', []):
            if call.get('name') != alias['tool_name']:
                continue
            arguments = call.get('arguments')
            if not isinstance(arguments, dict):
                continue
            value = arguments.get(alias['parameter'])
            if value in alias['accepted_values']:
                arguments[alias['parameter']] = alias['canonical_value']
    return normalized


def expected_answer_check(task, content):
    """Only deterministic core fields are checked; prose quality remains blind human review."""
    if task['category'] == 'code':
        return {'status': 'human_review', 'checks': [], 'note': 'Never execute model code.'}
    if task['category'] == 'tool':
        return {'status': 'pending_tool_check', 'checks': []}
    value = parse_json_content(content)
    if value is None or not isinstance(value, dict):
        return {'status': 'fail', 'checks': ['response is not the requested JSON object']}
    expected = task['expected']
    checks = []
    keys = ({k for k in expected if k not in ('reason', 'basis', 'evidence')}
            if task['category'] == 'zh_qa' else set(expected))
    for key in sorted(keys):
        ok = value.get(key, object()) == expected[key]
        checks.append({'field': key, 'pass': ok})
    passed = all(x['pass'] for x in checks)
    return {'status': 'pass' if passed else 'fail', 'checks': checks,
            'note': 'Automated field check is a screen; category rubric and blind human grading still apply.'}


def select_tasks(tasks, categories=None, case_ids=None, max_cases=None):
    selected = list(tasks)
    if categories:
        selected = [task for task in selected if task['category'] in categories]
    if case_ids:
        known = {task['id'] for task in tasks}
        unknown = set(case_ids) - known
        if unknown:
            raise ValueError('unknown case id(s): ' + ', '.join(sorted(unknown)))
        wanted = set(case_ids)
        selected = [task for task in selected if task['id'] in wanted]
    if max_cases is not None:
        if max_cases < 1:
            raise ValueError('--max-cases must be positive')
        selected = selected[:max_cases]
    if not selected:
        raise ValueError('task selection is empty')
    return selected


def post_json(url, body, api_key, timeout):
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    elapsed = time.perf_counter() - started
    try:
        decoded = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        decoded = None
    return status, decoded, raw.decode('utf-8', errors='replace'), elapsed


def assistant_message(choice):
    msg = choice.get('message') or {}
    item = {'role': 'assistant'}
    for key in ('content', 'tool_calls', 'function_call'):
        if key in msg:
            item[key] = msg[key]
    return item


def run_task(task, base_url, model, api_key, timeout, max_tokens, seed, long_document=None,
             reasoning_effort='none', reasoning_budget=None):
    prompt = task['prompt']
    if '{{LONG_DOCUMENT}}' in prompt:
        if long_document is None:
            raise ValueError('long-document tasks require a tokenizer-calibrated --long-document asset')
        prompt = prompt.replace('{{LONG_DOCUMENT}}', long_document)
    messages = [{'role': 'system', 'content': 'Follow the user task exactly. Do not claim actions that were not performed.'},
                {'role': 'user', 'content': prompt}]
    trace, successful_calls, attempted_calls, errors = [], [], [], []
    last_message = None
    total_wall = 0.0
    tool_rounds = 0
    for turn in range(4):
        body = {'model': model, 'messages': copy.deepcopy(messages), 'max_tokens': max_tokens,
                'temperature': 0, 'seed': seed, 'stream': False,
                'reasoning_effort': reasoning_effort}
        if reasoning_budget is not None:
            body['reasoning_budget_tokens'] = reasoning_budget
        if task.get('tools'):
            body['tools'] = copy.deepcopy(task['tools'])
            body['tool_choice'] = 'auto'
        code, response, raw, wall = post_json(base_url.rstrip('/') + '/v1/chat/completions', body, api_key, timeout)
        total_wall += wall
        trace.append({'turn': turn + 1, 'request': copy.deepcopy(body), 'http_status': code,
                      'response': response, 'raw_response': raw, 'wall_seconds': wall,
                      'usage': response.get('usage') if isinstance(response, dict) else None,
                      'finish_reason': ((response.get('choices') or [{}])[0].get('finish_reason')
                                        if isinstance(response, dict) else None)})
        if not isinstance(response, dict) or code < 200 or code >= 300:
            errors.append(f'HTTP/API failure on turn {turn + 1}')
            break
        choices = response.get('choices') or []
        if not choices:
            errors.append(f'No choices on turn {turn + 1}')
            break
        choice = choices[0]
        last_message = choice.get('message') or {}
        calls = last_message.get('tool_calls') or []
        if not calls:
            break
        if not task.get('tools'):
            errors.append('model attempted a tool call for a task without tools')
            break
        messages.append(assistant_message(choice))
        tool_rounds += 1
        if tool_rounds > 3:
            errors.append('tool round limit exceeded')
            break
        fixture_name = task['tools'][0]['function']['name']
        schema = task['tools'][0]['function']['parameters']
        for call in calls:
            call_id = call.get('id', f'local-{turn}-{len(successful_calls)}')
            fn = call.get('function') or {}
            name = fn.get('name')
            try:
                args = json.loads(fn.get('arguments', '{}'))
            except (ValueError, TypeError):
                args = None
            err = 'unexpected tool name' if name != fixture_name else schema_error(schema, args)
            attempted_calls.append({'name': name, 'arguments': args, 'schema_error': err})
            if err:
                errors.append(err)
                result = {'error': err, 'retryable': True, 'message': 'Correct the tool name/arguments and retry if appropriate.'}
            else:
                successful_calls.append({'name': name, 'arguments': args})
                fixture = task['tool_fixtures'][name]
                if isinstance(fixture, dict) and fixture.get('__transient_error_first') and not any(
                        call['name'] == name and call['schema_error'] is None for call in attempted_calls[:-1]):
                    result = {'error': fixture['__transient_error_first'], 'retryable': True}
                    successful_calls.pop()
                    errors.append('retryable fixture error')
                else:
                    result = fixture.get('__result', fixture) if isinstance(fixture, dict) else fixture
            messages.append({'role': 'tool', 'tool_call_id': call_id,
                             'name': name or 'unknown', 'content': json.dumps(result, ensure_ascii=False)})
    content = last_message.get('content') if isinstance(last_message, dict) else None
    finish_reason = (trace[-1].get('finish_reason') if trace else None)
    completion_state = ('truncated' if finish_reason in ('length', 'max_tokens') else
                        'natural' if finish_reason in ('stop', 'tool_calls', 'eos') else 'unknown')
    if task['category'] == 'tool':
        expected = task['expected_tool_calls']
        normalized_calls = normalize_tool_calls(task, successful_calls)
        tool_ok = normalized_calls == expected
        only_recoverable_errors = all(e in ('retryable fixture error',) or
                                      e.startswith(('missing required argument', 'unknown argument', 'arguments must',
                                                    'unexpected tool name', 'must be ', 'does not match',
                                                    'is shorter', 'is outside')) for e in errors)
        automatic = {'status': 'pass' if tool_ok and only_recoverable_errors else 'fail',
                     'checks': [{'name': 'successful_tool_call_sequence', 'pass': tool_ok},
                                {'name': 'errors_recovered_or_absent', 'pass': only_recoverable_errors}],
                     'attempted_tool_calls': attempted_calls,
                     'successful_tool_calls': successful_calls,
                     'normalized_tool_calls_for_grading': normalized_calls, 'protocol_errors': errors,
                     'note': 'Final answer and safety rubric still require blind human review.'}
    else:
        automatic = expected_answer_check(task, content)
    if completion_state == 'truncated':
        automatic = {'status': 'fail', 'checks': [{'name': 'not_truncated', 'pass': False}],
                     'note': 'The server stopped at the output token limit; review as an incomplete answer.'}
    complete = bool(trace) and all(200 <= x['http_status'] < 300 for x in trace) and isinstance(last_message, dict)
    return {'task_id': task['id'], 'category': task['category'], 'severity': task['severity'],
            'execution_status': 'complete' if complete else 'failed',
            'prompt': prompt, 'expected': task.get('expected'),
            'rubric': task['rubric'], 'hard_fail': task['hard_fail'],
            'trace': trace, 'final_content': content,
            'completion_state': completion_state,
            'final_reasoning': {k: last_message.get(k) for k in ('reasoning', 'reasoning_content', 'analysis')
                                if isinstance(last_message, dict) and k in last_message},
            'finish_reason': finish_reason,
            'total_wall_seconds': total_wall, 'tool_calls': successful_calls,
            'protocol_errors': errors, 'automatic_screen': automatic,
            'human_blind_review': 'pending'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--arm', choices=('baseline', 'candidate'), required=True)
    ap.add_argument('--base-url', required=True, help='Dedicated already-running endpoint, e.g. http://127.0.0.1:18099')
    ap.add_argument('--out', type=Path, required=True, help='New JSONL path; summary JSON is written beside it')
    ap.add_argument('--model', default='Qwen3.8-Flash-Next')
    ap.add_argument('--label', default='', help='Non-secret process/config label for this arm')
    ap.add_argument('--runtime-manifest', type=Path,
                    help='ready supervisor/baseline manifest; snapshots configuration evidence without probing runtime')
    ap.add_argument('--allow-no-auth', action='store_true',
                    help='explicitly omit Authorization (for unauthenticated llama.cpp baseline endpoints)')
    ap.add_argument('--long-document', type=Path, help='generated 128K/256K archive from make_longdoc.py')
    ap.add_argument('--long-document-manifest', type=Path,
                    help='matching .manifest.json emitted by make_longdoc.py')
    ap.add_argument('--max-context-tokens', type=int,
                    help='effective server context limit; required for calibrated long-document cases')
    ap.add_argument('--timeout', type=float, default=900)
    ap.add_argument('--max-tokens', type=int, default=2048)
    ap.add_argument('--long-max-tokens', type=int, default=1024)
    ap.add_argument('--long-prompt-overhead', type=int, default=1024)
    ap.add_argument('--effort', choices=('none', 'low', 'medium', 'high'), default='none')
    ap.add_argument('--reasoning-budget', type=int, help='optional explicit server reasoning token budget')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--category', action='append', choices=('zh_qa', 'code', 'long_document', 'tool'),
                    help='limit to a category; can be repeated')
    ap.add_argument('--case-id', action='append', help='limit to exact task ID; can be repeated')
    ap.add_argument('--max-cases', type=int, help='run only the first N selected tasks in stable dataset order')
    args = ap.parse_args(argv)
    if args.max_tokens < 32 or args.long_max_tokens < 32 or args.timeout <= 0:
        ap.error('--max-tokens and --long-max-tokens must be at least 32; timeout must be positive')
    if args.reasoning_budget is not None and args.reasoning_budget < 0:
        ap.error('--reasoning-budget must be nonnegative')
    if args.runtime_manifest:
        try:
            runtime_evidence, runtime_config = load_runtime_manifest(args.runtime_manifest, args.base_url)
        except (OSError, ValueError) as exc:
            ap.error(f'invalid --runtime-manifest: {exc}')
    else:
        runtime_evidence = {'status': 'unverified', 'manifest_path': None,
                            'note': 'No runtime manifest supplied; runtime conditions are unverified.'}
        runtime_config = {'status': 'unverified'}
    out = args.out.resolve()
    summary_path = out.with_suffix('.summary.json')
    if out.exists() or summary_path.exists():
        ap.error('output or summary already exists; choose a fresh path to preserve evidence')
    data = load_and_validate()
    try:
        selected_tasks = select_tasks(data['items'], args.category, args.case_id, args.max_cases)
    except ValueError as exc:
        ap.error(str(exc))
    needs_long_document = any('{{LONG_DOCUMENT}}' in t['prompt'] for t in selected_tasks)
    long_document = None
    long_manifest = None
    if needs_long_document:
        if not args.long_document or not args.long_document_manifest:
            ap.error('the 40-task set includes long-document cases; provide both --long-document and --long-document-manifest')
        long_path = args.long_document.resolve()
        long_manifest_path = args.long_document_manifest.resolve()
        long_document = long_path.read_text(encoding='utf-8')
        long_manifest = json.loads(long_manifest_path.read_text(encoding='utf-8'))
        actual_hash = sha(long_document.encode('utf-8'))
        if long_manifest.get('sha256') != actual_hash or long_manifest.get('utf8_bytes') != len(long_document.encode('utf-8')):
            ap.error('long-document asset does not match its calibration manifest')
        if long_manifest.get('target_tokens') not in (128000, 256000):
            ap.error('long-document manifest target must be 128K or 256K tokens')
        if not args.max_context_tokens or args.max_context_tokens <= 0:
            ap.error('--max-context-tokens is required when running long-document cases')
        required_context = (long_manifest['measured_tokens'] + args.long_prompt_overhead + args.long_max_tokens)
        if args.long_prompt_overhead < 0 or required_context > args.max_context_tokens:
            ap.error(f'context budget too small: need at least {required_context} tokens for document, overhead, and output')
    api_key = None if args.allow_no_auth else read_key()
    if not api_key:
        if not args.allow_no_auth:
            ap.error('No API key found in STRATA_API_KEY or configured auth file; use --allow-no-auth only for an unauthenticated endpoint.')
    manifest = {'kind': 'manifest', 'schema_version': 1, 'run_id': uuid.uuid4().hex,
                'created_utc': datetime.now(timezone.utc).isoformat(), 'arm': args.arm,
                'label': args.label, 'base_url': args.base_url, 'model': args.model,
                'taskset_sha256': sha((HERE / 'tasks.json').read_bytes()),
                'collector_sha256': sha(Path(__file__).read_bytes()), 'task_count': len(selected_tasks),
                'selection': {'categories': args.category, 'case_ids': args.case_id,
                              'max_cases': args.max_cases, 'selected_ids': [t['id'] for t in selected_tasks]},
                'long_document': ({'path': str(args.long_document.resolve()), 'manifest': long_manifest,
                                   'asset_sha256': sha(long_document.encode('utf-8')),
                                   'max_context_tokens': args.max_context_tokens,
                                   'prompt_overhead_tokens': args.long_prompt_overhead,
                                   'max_output_tokens': args.long_max_tokens}
                                  if long_document is not None else None),
                'sampling': {'temperature': 0, 'seed': args.seed,
                             'reasoning_effort': args.effort, 'reasoning_budget': args.reasoning_budget,
                             'max_tokens_default': args.max_tokens, 'max_tokens_long_document': args.long_max_tokens},
                'runtime_evidence': runtime_evidence, 'runtime_config': runtime_config,
                'runtime_mode_verified': False, 'runtime_conditions_verified': False,
                'auth_mode': 'none' if args.allow_no_auth else 'bearer', 'api_key_recorded': False,
                'evidence_note': 'Full API responses are retained, including any reasoning fields returned by the server.'}
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with out.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False) + '\n')
        stream.flush()
        for index, task in enumerate(selected_tasks, 1):
            try:
                task_max_tokens = args.long_max_tokens if task['category'] == 'long_document' else args.max_tokens
                row = run_task(task, args.base_url, args.model, api_key, args.timeout, task_max_tokens, args.seed,
                               long_document, args.effort, args.reasoning_budget)
            except Exception as exc:
                row = {'task_id': task['id'], 'category': task['category'], 'severity': task['severity'],
                       'execution_status': 'failed',
                       'error': f'{type(exc).__name__}: {exc}', 'automatic_screen': {'status': 'fail'},
                       'human_blind_review': 'pending'}
            row['kind'] = 'result'
            row['run_id'] = manifest['run_id']
            row['arm'] = args.arm
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            print(f"{index:02d}/{len(selected_tasks)} {task['id']}: {row.get('automatic_screen', {}).get('status', 'fail')}", flush=True)
            if row.get('execution_status') == 'failed':
                print('Stopping after the first execution failure; no automatic retry.', flush=True)
                break
    summary = {'run_id': manifest['run_id'], 'arm': args.arm, 'task_count': len(selected_tasks),
               'attempted_count': len(rows), 'unrun_count': len(selected_tasks) - len(rows),
               'automatic_screens': {}, 'execution_failures': sum(x.get('execution_status') == 'failed' or 'error' in x for x in rows),
               'human_blind_review': 'pending',
               'note': 'Structural/field screens only; no quality claim until paired blind rubric review.'}
    for category in sorted({x['category'] for x in rows}):
        cat = [x for x in rows if x['category'] == category]
        summary['automatic_screens'][category] = {
            'pass': sum(x.get('automatic_screen', {}).get('status') == 'pass' for x in cat),
            'fail': sum(x.get('automatic_screen', {}).get('status') == 'fail' for x in cat),
            'human_review': sum(x.get('automatic_screen', {}).get('status') == 'human_review' for x in cat),
        }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote evidence: {out}')
    print(f'Wrote summary: {summary_path}')
    print('Quality acceptance remains pending blind human review.')
    return 0 if summary['execution_failures'] == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
