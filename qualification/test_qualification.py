import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect
import make_longdoc
import rescore
from validate import load_and_validate


def response(content=None, calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    return {'choices': [{'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}]}


class QualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = {task['id']: task for task in load_and_validate()['items']}

    def test_taskset_balanced_and_valid(self):
        self.assertEqual(len(self.items), 40)
        self.assertEqual(sum(t['category'] == 'code' for t in self.items.values()), 10)

    def test_json_screen_ignores_freeform_explanation(self):
        task = self.items['qa01']
        screen = collect.expected_answer_check(task, '{"answer":3,"reason":"计算结果为三件"}')
        self.assertEqual(screen['status'], 'pass')

    def test_json_screen_catches_wrong_value(self):
        screen = collect.expected_answer_check(self.items['qa01'], '{"answer":4,"reason":"错"}')
        self.assertEqual(screen['status'], 'fail')

    def test_task_selection_supports_category_id_and_limit(self):
        tasks = list(self.items.values())
        selected = collect.select_tasks(tasks, ['tool'], ['tool02', 'tool08'], 1)
        self.assertEqual([task['id'] for task in selected], ['tool02'])
        with self.assertRaises(ValueError):
            collect.select_tasks(tasks, case_ids=['missing'])

    def test_truncation_is_distinguished_and_fails_screen(self):
        task = self.items['qa01']
        truncated = response('{"answer":3,"reason":"计算结果为三件"}')
        truncated['choices'][0]['finish_reason'] = 'length'
        truncated['usage'] = {'completion_tokens': 32}
        with patch('collect.post_json', return_value=(200, truncated, json.dumps(truncated), .01)) as mocked:
            row = collect.run_task(task, 'http://127.0.0.1:1', 'test-model', None, 1, 32, 42)
        self.assertEqual(row['completion_state'], 'truncated')
        self.assertEqual(row['automatic_screen']['status'], 'fail')
        self.assertEqual(row['trace'][0]['usage']['completion_tokens'], 32)

    def test_reasoning_budget_uses_server_field_only_when_explicit(self):
        task = self.items['qa01']
        answer = response('{"answer":3,"reason":"计算结果为三件"}')
        with patch('collect.post_json', return_value=(200, answer, json.dumps(answer), .01)) as mocked:
            collect.run_task(task, 'http://127.0.0.1:1', 'test-model', None, 1, 256, 42,
                             reasoning_effort='medium', reasoning_budget=64)
        budget_body = mocked.call_args.args[1]
        self.assertEqual(budget_body.get('reasoning_budget_tokens'), 64)
        self.assertNotIn('reasoning_budget', budget_body)

        with patch('collect.post_json', return_value=(200, answer, json.dumps(answer), .01)) as mocked:
            collect.run_task(task, 'http://127.0.0.1:1', 'test-model', None, 1, 256, 42,
                             reasoning_effort='none', reasoning_budget=None)
        no_budget_body = mocked.call_args.args[1]
        self.assertEqual(no_budget_body.get('reasoning_effort'), 'none')
        self.assertNotIn('reasoning_budget_tokens', no_budget_body)
        self.assertNotIn('reasoning_budget', no_budget_body)

    def test_code_never_claims_automatic_pass(self):
        screen = collect.expected_answer_check(self.items['code01'], 'print("hello")')
        self.assertEqual(screen['status'], 'human_review')

    def test_schema_validation(self):
        schema = self.items['tool03']['tools'][0]['function']['parameters']
        self.assertIn('does not match', collect.schema_error(schema, {'order_id': 'bad'}))
        self.assertIsNone(collect.schema_error(schema, {'order_id': 'A-204'}))

    def test_weather_tokyo_chinese_alias_passes_but_wrong_city_fails(self):
        task = self.items['tool01']
        base = {'task_id': 'tool01', 'execution_status': 'complete', 'completion_state': 'natural',
                'finish_reason': 'stop', 'protocol_errors': []}
        base['tool_calls'] = [{'name': 'get_weather', 'arguments': {'city': '东京', 'unit': '摄氏度'}}]
        self.assertEqual(rescore.grade_tool_row(base, task)['status'], 'pass')
        base['tool_calls'] = [{'name': 'get_weather', 'arguments': {'city': '大阪', 'unit': '摄氏度'}}]
        self.assertEqual(rescore.grade_tool_row(base, task)['status'], 'fail')

    def test_missing_tool_argument_evidence_fails_task_preflight(self):
        data = load_and_validate()
        for task in data['items']:
            if task['id'] == 'tool07':
                task['prompt'] = '只检查用户邮件草稿。'
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'tasks.json'
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(AssertionError):
                load_and_validate(path)

    def test_old_tool07_invalid_fixture_is_manual_review(self):
        history = json.loads((Path(__file__).parent / 'task_history' / 'tool07-v1-invalid_fixture.json')
                             .read_text(encoding='utf-8'))
        row = {'task_id': 'tool07', 'prompt': history['original_task']['prompt'],
               'tool_calls': history['original_task']['expected_tool_calls'],
               'execution_status': 'complete', 'protocol_errors': []}
        grade = rescore.grade_tool_row(row, self.items['tool07'], (history['original_task']['prompt'],))
        self.assertEqual(grade['status'], 'manual_review_invalid_fixture')

    def test_rescore_preserves_source_jsonl_and_appends_new_grade_records(self):
        task = self.items['tool01']
        history = json.loads((Path(__file__).parent / 'task_history' / 'tool07-v1-invalid_fixture.json')
                             .read_text(encoding='utf-8'))
        manifest = {'kind': 'manifest', 'taskset_sha256': 'old-taskset-hash'}
        tokyo = {'kind': 'result', 'task_id': 'tool01', 'category': 'tool', 'run_id': 'test-run',
                 'arm': 'candidate', 'prompt': task['prompt'],
                 'tool_calls': [{'name': 'get_weather', 'arguments': {'city': '东京', 'unit': '摄氏度'}}],
                 'protocol_errors': [], 'execution_status': 'complete', 'completion_state': 'natural',
                 'finish_reason': 'stop', 'automatic_screen': {'status': 'fail'}}
        old_tool07 = {'kind': 'result', 'task_id': 'tool07', 'category': 'tool', 'run_id': 'test-run',
                      'arm': 'candidate', 'prompt': history['original_task']['prompt'],
                      'tool_calls': history['original_task']['expected_tool_calls'],
                      'protocol_errors': [], 'execution_status': 'complete', 'finish_reason': 'stop',
                      'automatic_screen': {'status': 'fail'}}
        source_bytes = ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                               for row in (manifest, tokyo, old_tool07)).encode('utf-8')
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'run.jsonl'
            target = Path(temp_dir) / 'run-rescored.jsonl'
            source.write_bytes(source_bytes)
            result = rescore.rescore(source, target)
            actual = target.read_bytes()
        self.assertTrue(actual.startswith(source_bytes))
        self.assertEqual(result['metadata']['original_manifest_taskset_sha256'], 'old-taskset-hash')
        appended = [json.loads(line) for line in actual[len(source_bytes):].splitlines() if line.strip()]
        grades = {row['task_id']: row['rescore']['status'] for row in appended if row.get('kind') == 'rescore'}
        self.assertEqual(grades['tool01'], 'pass')
        self.assertEqual(grades['tool07'], 'manual_review_invalid_fixture')

    def test_tool_fixture_round_trip_and_recovery(self):
        task = self.items['tool10']
        args = json.dumps({'city': '苏州', 'scale': 'CN'}, ensure_ascii=False)
        call = {'id': 'call-a', 'type': 'function', 'function': {'name': 'air_quality', 'arguments': args}}
        final = response('苏州空气质量指数为42，等级为优。')
        with patch('collect.post_json', side_effect=[(200, response(calls=[call]), '{}', .01),
                                                     (200, response(calls=[call]), '{}', .01),
                                                     (200, final, '{}', .01)]) as mocked:
            row = collect.run_task(task, 'http://127.0.0.1:1', 'test-model', None, 1, 256, 42)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(row['automatic_screen']['status'], 'pass')
        self.assertEqual(row['automatic_screen']['successful_tool_calls'], task['expected_tool_calls'])
        self.assertIn('retryable fixture error', row['protocol_errors'])

    def test_long_archive_is_deterministic_and_has_deep_markers(self):
        first = make_longdoc.archive(500, 3804)
        self.assertEqual(first, make_longdoc.archive(500, 3804))
        self.assertIn('ARC-7K2', first)
        self.assertIn('MID-4P9', first)
        self.assertIn('END-9R3', first)
        self.assertIn('审批人姓名、供应商银行账号均未记录', first)

    def test_long10_example_does_not_disclose_answers_and_archive_supports_expected(self):
        task = self.items['long10']
        expected = task['expected']
        prompt = task['prompt']
        for answer in expected['markers'] + expected['missing']:
            self.assertNotIn(answer, prompt)
        self.assertIn('{{LONG_DOCUMENT}}', prompt)

        archive = make_longdoc.archive(500, 3804)
        for marker in expected['markers']:
            self.assertIn(marker, archive)
        self.assertIn('审批人姓名、供应商银行账号均未记录', archive)
        self.assertEqual(expected['missing'], ['审批人姓名', '供应商银行账号'])

    def test_long_task_uses_supplied_archive(self):
        task = self.items['long02']
        expected = json.dumps(task['expected'], ensure_ascii=False)
        with patch('collect.post_json', return_value=(200, response(expected), '{}', .01)) as mocked:
            row = collect.run_task(task, 'http://127.0.0.1:1', 'test-model', None, 1, 256, 42,
                                   long_document='TEST LONG ARCHIVE')
        request_body = mocked.call_args.args[1]
        self.assertIn('TEST LONG ARCHIVE', request_body['messages'][1]['content'])
        self.assertEqual(row['automatic_screen']['status'], 'pass')

    def test_tokenizer_adapter_output_contract(self):
        class Proc:
            returncode = 0
            stdout = '{"tokens": 123}'
            stderr = ''
        with patch('make_longdoc.subprocess.run', return_value=Proc()):
            self.assertEqual(make_longdoc.count_tokens('adapter.exe', 'sample', 1), 123)

    def test_real_tokenizer_child_accepts_utf8_special_characters(self):
        tokenizer_dir = Path(os.environ.get('STRATA_TOKENIZER_DIR', ''))
        implementation = Path(os.environ.get('STRATA_TOKENIZER_IMPLEMENTATION', ''))
        if not tokenizer_dir.is_dir() or not implementation.is_file():
            self.skipTest('Strata tokenizer assets are not available on this host')
        worker = make_longdoc.PersistentTokenizer(
            Path(sys.executable), Path(__file__).with_name('tokenizer_count.py'), tokenizer_dir, implementation)
        try:
            count = worker.count('中文回归，含符号✅、𠮷、组合字符 e\u0301。')
            self.assertGreater(count, 0)
        finally:
            worker.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
