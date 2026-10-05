#!/usr/bin/env python3
"""Offline tool-call rescore. Copies all original JSONL lines verbatim, then appends grades."""
import argparse
import hashlib
import json
from pathlib import Path

from collect import normalize_tool_calls
from validate import load_and_validate

HERE = Path(__file__).resolve().parent
VERSION = 'tool-arguments-v2'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def recoverable_errors(errors):
    return all(error == 'retryable fixture error' or
               error.startswith(('missing required argument', 'unknown argument', 'arguments must',
                                  'unexpected tool name', 'must be ', 'does not match',
                                  'is shorter', 'is outside')) for error in errors)


def grade_tool_row(row, task, invalid_historical_prompts=()):
    if row.get('prompt') in invalid_historical_prompts:
        return {'status': 'manual_review_invalid_fixture',
                'checks': [{'name': 'input_fixture_was_complete', 'pass': None}],
                'reason': 'The original prompt omitted required argument values; this historical task cannot be graded.'}
    calls = row.get('tool_calls') or []
    normalized = normalize_tool_calls(task, calls)
    sequence_ok = normalized == task['expected_tool_calls']
    errors = row.get('protocol_errors') or []
    errors_ok = recoverable_errors(errors)
    execution_ok = row.get('execution_status') == 'complete'
    not_truncated = row.get('completion_state') != 'truncated' and row.get('finish_reason') not in ('length', 'max_tokens')
    checks = [
        {'name': 'successful_tool_call_sequence', 'pass': sequence_ok},
        {'name': 'errors_recovered_or_absent', 'pass': errors_ok},
        {'name': 'execution_completed', 'pass': execution_ok},
        {'name': 'not_truncated', 'pass': not_truncated},
    ]
    return {'status': 'pass' if all(item['pass'] for item in checks) else 'fail',
            'checks': checks, 'observed_tool_calls': calls,
            'normalized_tool_calls_for_grading': normalized,
            'protocol_errors': errors,
            'note': 'Tool-call grading does not require ordinary assistant text; human safety/content review remains pending.'}


def rescore(input_path, output_path):
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    if source == target:
        raise ValueError('input and output must be different paths')
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        raise FileExistsError(f'output exists; choose a new path: {target}')
    tasks_data = load_and_validate()
    tasks = {task['id']: task for task in tasks_data['items']}
    history_path = HERE / 'task_history' / 'tool07-v1-invalid_fixture.json'
    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else None
    invalid_prompts = ((history['original_task']['prompt'],) if history else ())
    history_sha = file_sha(history_path) if history_path.exists() else None
    original_sha = file_sha(source)
    grading_sha = sha(Path(__file__).read_bytes() + (HERE / 'tasks.json').read_bytes() +
                      (history_path.read_bytes() if history_path.exists() else b''))
    pending_grades = []
    source_manifest = None
    last_byte = b''
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as inp, target.open('xb') as out:
        for line_number, raw_line in enumerate(inp, 1):
            out.write(raw_line)  # preserve the old manifest and source records byte-for-byte
            last_byte = raw_line[-1:]
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f'invalid JSONL at line {line_number}') from exc
            if line_number == 1:
                source_manifest = record
            if record.get('kind') != 'result' or record.get('category') != 'tool':
                continue
            task = tasks.get(record.get('task_id'))
            if task is None or task['category'] != 'tool':
                continue
            pending_grades.append({'kind': 'rescore', 'task_id': task['id'],
                                   'run_id': record.get('run_id'), 'arm': record.get('arm'),
                                   'grading_version': VERSION,
                                   'original_automatic_screen': record.get('automatic_screen'),
                                   'rescore': grade_tool_row(record, task, invalid_prompts)})
        if source_manifest is None or source_manifest.get('kind') != 'manifest':
            raise ValueError('input JSONL must begin with its original manifest')
        if target.stat().st_size and last_byte != b'\n':
            out.write(b'\n')
        metadata = {'kind': 'rescore_manifest', 'grading_version': VERSION,
                    'created_note': 'Offline regrade; source run data and source manifest remain unchanged above.',
                    'source_path': str(source), 'source_jsonl_sha256': original_sha,
                    'original_manifest_taskset_sha256': source_manifest.get('taskset_sha256'),
                    'current_taskset_sha256': sha((HERE / 'tasks.json').read_bytes()),
                    'grading_rules_sha256': grading_sha, 'historical_fixture_sha256': history_sha,
                    'rescore_rows': len(pending_grades)}
        out.write((json.dumps(metadata, ensure_ascii=False) + '\n').encode('utf-8'))
        for grade in pending_grades:
            out.write((json.dumps(grade, ensure_ascii=False) + '\n').encode('utf-8'))
    counts = {}
    for grade in pending_grades:
        status = grade['rescore']['status']
        counts[status] = counts.get(status, 0) + 1
    return {'path': target, 'counts': counts, 'metadata': metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='existing run JSONL; never modified')
    parser.add_argument('output', type=Path, help='new JSONL containing source bytes plus appended grades')
    args = parser.parse_args()
    result = rescore(args.input, args.output)
    print(f"Wrote {result['path']}")
    print(f"Rescore counts: {json.dumps(result['counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"Grading version: {VERSION}; rules SHA256: {result['metadata']['grading_rules_sha256']}")


if __name__ == '__main__':
    main()
