#!/usr/bin/env python3
"""Offline structural checks for the qualification task set."""
import json
from collections import Counter
from pathlib import Path

TASKS = Path(__file__).with_name('tasks.json')
EXPECTED = {'zh_qa': 10, 'code': 10, 'long_document': 10, 'tool': 10}


def load_and_validate(path=TASKS):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    assert data.get('schema_version') == 1
    items = data.get('items')
    assert isinstance(items, list) and len(items) == 40, 'expected exactly 40 tasks'
    ids = [x.get('id') for x in items]
    assert len(ids) == len(set(ids)), 'task ids must be unique'
    counts = Counter(x.get('category') for x in items)
    assert counts == EXPECTED, f'category balance mismatch: {counts}'
    for task in items:
        for key in ('id', 'category', 'severity', 'prompt', 'rubric', 'hard_fail'):
            assert task.get(key), f'{task.get("id")}: missing {key}'
        if task['category'] in ('zh_qa', 'long_document'):
            assert isinstance(task.get('expected'), dict), f'{task["id"]}: missing expected answer'
        if task['category'] == 'long_document':
            assert '{{LONG_DOCUMENT}}' in task['prompt'], f'{task["id"]}: must use the calibrated long-document asset'
        if task['category'] == 'code':
            assert task.get('expected', {}).get('human_review_required') is True
        if task['category'] == 'tool':
            assert len(task.get('tools', [])) == 1
            assert len(task.get('expected_tool_calls', [])) >= 1
            fn = task['tools'][0]['function']
            schema = fn['parameters']
            assert schema.get('type') == 'object' and schema.get('additionalProperties') is False
            assert set(schema.get('required', [])) <= set(schema.get('properties', {}))
            assert fn['name'] in task.get('tool_fixtures', {})
            for alias in task.get('tool_argument_aliases', []):
                assert alias['tool_name'] in {item['function']['name'] for item in task['tools']}
                assert alias['parameter'] in schema['properties']
                assert schema['properties'][alias['parameter']].get('type') == 'string'
                assert alias['canonical_value'] in alias['accepted_values']
                assert len(alias['accepted_values']) == len(set(alias['accepted_values']))
                assert all(isinstance(value, str) for value in alias['accepted_values'])
                assert any(call['name'] == alias['tool_name'] and
                           call['arguments'].get(alias['parameter']) == alias['canonical_value']
                           for call in task['expected_tool_calls'])
            for call in task['expected_tool_calls']:
                fn_schema = next(item['function'] for item in task['tools']
                                 if item['function']['name'] == call['name'])
                for parameter, value in call['arguments'].items():
                    evidence = str(value).casefold() in task['prompt'].casefold()
                    if not evidence:
                        evidence = any(alias['tool_name'] == call['name'] and alias['parameter'] == parameter
                                       and alias['canonical_value'] == value
                                       and any(alias_value.casefold() in task['prompt'].casefold()
                                               for alias_value in alias['accepted_values'])
                                       for alias in task.get('tool_argument_aliases', []))
                    enum = fn_schema['parameters']['properties'][parameter].get('enum')
                    if not evidence and enum == [value]:
                        evidence = True
                    assert evidence, (f"{task['id']}: expected argument {parameter}={value!r} has no literal "
                                     'prompt, explicit alias, or unique-enum basis')
    return data


if __name__ == '__main__':
    data = load_and_validate()
    print(f"OK: {len(data['items'])} tasks; 10 per category; schemas and rubrics present.")
    print('This verifies asset structure only; it does not grade model quality.')
