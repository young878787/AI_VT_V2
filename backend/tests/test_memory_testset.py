"""案例、生成、隔離執行與 trace 的硬性契約，不判定聊天語意品質。"""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import memory_testset as testset
from tools import chat_test_cli as cli
from core import prompt_logger
from services.chat_service import build_chat_context, retained_prompt_ranges
from backend.tests.test_chat_test_cli import make_record, FakeSocket


def core_cases():
    return json.loads(testset.CORE_PATH.read_text(encoding='utf-8'))


def generated_cases(core):
    result = []
    for i, (kind, bases, route) in enumerate(testset.GENERATED_TYPES, 21):
        case = copy.deepcopy(core[bases[0] - 1])
        case.update(case_id=f'random_{i:03}', case_type=kind, source='generated',
                    base_case=case['case_id'], case_group=route)
        result.append(case)
    return result


class MemoryTestsetTests(unittest.TestCase):
    def test_core_is_complete_and_has_explicit_controls_and_dates(self):
        core = testset.validate_cases(core_cases(), source='Gold', count=23)
        self.assertEqual(len(core), 23)
        self.assertEqual(sum('input' in s for c in core for s in c['conversation']), 86)
        self.assertIn('compress', [s.get('action') for s in core[2]['conversation']])
        self.assertTrue(core[16]['event_date'].endswith('+08:00'))
        self.assertEqual(len([s for s in core[19]['conversation'] if s['phase'] == 'memory_setup']), 6)

    def test_validator_rejects_answers_unknown_tools_and_unsafe_session_structure(self):
        for change in ('assistant', 'input', 'session', 'action'):
            core = core_cases()
            step = core[0]['conversation'][0]
            if change == 'assistant': step['assistant'] = 'fixed answer'
            elif change == 'input': step['input'] = ''
            elif change == 'session': core[10]['conversation'][1]['session'] = 'seed'
            else: core[2]['conversation'][-2]['action'] = 'execute_sql'
            with self.subTest(change=change), self.assertRaises(ValueError):
                testset.validate_cases(core, source='Gold', count=23)

    def test_generated_intent_and_required_setup_count(self):
        core = core_cases()
        generated = generated_cases(core)
        specs = [{k: case[k] for k in ('case_id', 'case_type', 'base_case', 'case_group')} for case in generated]
        testset.validate_generated(generated, specs)
        for key, value in (('base_case', 'case_019'), ('case_type', 'anything'), ('case_group', None)):
            invalid = copy.deepcopy(generated)
            invalid[0][key] = value
            with self.assertRaises(ValueError): testset.validate_generated(invalid, specs)
        generated[-1]['conversation'] = generated[-1]['conversation'][:1]
        generated[-1]['conversation'].append(dict(phase='probe',session='fresh',input='query',evidence=[dict(source_step=1,source='db',fragments=['Live2D'])]))
        with self.assertRaisesRegex(ValueError, '前置事實不足'):
            testset.validate_generated(generated, specs)

    def test_snapshot_is_exact_replay_and_incomplete_snapshot_rejected(self):
        cases = core_cases()
        cases += generated_cases(cases)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.json'
            path.write_text(json.dumps(cases), encoding='utf-8')
            self.assertEqual(testset.load_snapshot(str(path)), cases)
            path.write_text(json.dumps(cases[:20]), encoding='utf-8')
            with self.assertRaises(ValueError): testset.load_snapshot(str(path))

    def test_generator_repairs_invalid_output_with_bounded_existing_chat_client(self):
        core = core_cases()
        generated = generated_cases(core)
        response = lambda content: SimpleNamespace(model='actual-chat', usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        diagnostics = {}
        with mock.patch.object(testset.random, 'choice', side_effect=lambda items: items[0]), \
             mock.patch('infrastructure.ai_client.chat_create_with_fallback', new=mock.AsyncMock(
                 side_effect=[response('[]'), response(json.dumps(generated))])) as create:
            accepted = asyncio.run(testset.generate_cases(core, diagnostics))
        self.assertEqual(accepted, generated)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args.kwargs['role'], 'chat')
        self.assertEqual(diagnostics['attempts'][-1]['model'], 'actual-chat')
        self.assertIsNone(diagnostics['attempts'][-1]['usage'])
        self.assertEqual(diagnostics['status'], 'completed')
        self.assertIn('prompt_sha256', diagnostics)

    def test_generator_never_substitutes_templates_after_failure(self):
        diagnostics = {}
        with mock.patch('infrastructure.ai_client.chat_create_with_fallback', new=mock.AsyncMock(
            side_effect=TimeoutError)) as create:
            with self.assertRaises(RuntimeError):
                asyncio.run(testset.generate_cases(core_cases(), diagnostics))
        self.assertEqual(create.call_count, 3)
        self.assertEqual(diagnostics['status'], 'failed')

    def test_prompt_log_and_trace_are_isolated_and_reset_preserves_case_evidence(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            'AI_VT_TEST_MODE': 'true', 'AI_VT_MEMORY_DIR': directory}), \
             mock.patch.object(prompt_logger, '_LOG_FILE', Path(directory) / 'production.log'):
            event_id = cli.uuid.uuid4()
            prompt_logger.trace('probe', {'value': 'evidence'}, event_id)
            prompt_logger.log_turn(1, 'system', 'user', 'reply', [], 1)
            prompt_logger.reset_log()
            self.assertFalse((Path(directory) / 'production.log').exists())
            self.assertIn('evidence', (Path(directory) / 'trace.jsonl').read_text())
            self.assertIn('system', (Path(directory) / 'prompt.log').read_text())

    def test_trace_is_disabled_in_production(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            'AI_VT_TEST_MODE': 'false', 'AI_VT_MEMORY_DIR': directory}):
            prompt_logger.trace('chat_context', {'messages': 'secret'}, cli.uuid.uuid4())
            self.assertFalse((Path(directory) / 'trace.jsonl').exists())

    def test_trimmed_context_records_only_surviving_memory_fragments(self):
        prompt = 'x' * 2000 + 'memory in removed middle' + 'y' * 2000
        actual = build_chat_context(prompt, [], 'query', budget=80)[0]['content']
        context = {'profile': {}, 'profile_section_start': 0,
                   'memory_section_start': 2000, 'system_retained_ranges': retained_prompt_ranges(prompt, actual),
                   'memory_fragments': [], 'injected_memory_ids': []}
        cli.locate_injected_fragments(context, [{'id': 'removed', 'destination': 'memory', 'text': 'memory in removed middle'}])
        self.assertEqual(context['injected_memory_ids'], [])
        context['memory_section_start'] = 0
        cli.locate_injected_fragments(context, [{'id': 'partial', 'destination': 'memory', 'text': 'x' * 2000}])
        self.assertEqual(context['injected_memory_ids'], ['partial'])
        self.assertLess(len(context['memory_fragments'][0]['text']), 2000)
        self.assertIn(context['memory_fragments'][0]['text'], actual)

    def test_successful_send_without_ack_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = root / 'scenario.txt'
            scenario.write_text('one\ntwo\n', encoding='utf-8')
            record = make_record(1, 'connection closed')
            record['request_sent'] = True
            args = argparse.Namespace(scenario=str(scenario), max_turns=0, retries=2, startup_timeout=1, turn_timeout=1)
            with mock.patch.object(cli, 'RUNS_DIR', root / 'runs'), \
                 mock.patch.object(cli, 'test_database_url', return_value='postgresql://test/db'), \
                 mock.patch.object(cli.MemoryRunStore, 'open'), mock.patch.object(cli.MemoryRunStore, 'close'), \
                 mock.patch.object(cli, 'start_backend', return_value=(mock.Mock(), mock.Mock())), \
                 mock.patch.object(cli, 'connect_backend', return_value=FakeSocket()), \
                 mock.patch.object(cli, 'stop_backend'), \
                 mock.patch.object(cli, 'run_turn', return_value=record) as run_turn:
                output, status = asyncio.run(cli.run(args))
            self.assertEqual(status, 'failed')
            self.assertEqual(run_turn.call_count, 1)
            self.assertEqual(json.loads((output / 'run.json').read_text())['executed_turns'], 1)

    def test_cancelled_turn_preserves_sent_reply_and_event_without_resending(self):
        event_id = str(cli.uuid.uuid4())
        class Socket:
            async def send(self, payload):
                self.turn_id = json.loads(payload)['turn_id']
                self.index = 0
            async def recv(self):
                self.index += 1
                if self.index == 1:
                    return json.dumps({'type': 'input_accepted', 'event_id': event_id, 'turn_id': self.turn_id})
                if self.index == 2:
                    return json.dumps({'type': 'text_stream', 'content': '已送出的半句', 'turn_id': self.turn_id})
                raise asyncio.CancelledError()
        store = mock.Mock()
        store.snapshot.return_value = {}
        store.audit.return_value = []
        record = asyncio.run(cli.run_turn(Socket(), 1, 'question', 'Rushia', 'test_session', store, 1))
        self.assertTrue(record['interrupted'])
        self.assertTrue(record['request_sent'])
        self.assertEqual(record['reply'], '已送出的半句')
        self.assertEqual(record['memory_event_id'], event_id)

    def test_stream_diagnostics_keep_api_usage_distinct_from_estimate(self):
        from services import chat_service
        class Stream:
            async def __aiter__(self):
                yield SimpleNamespace(model='actual-chat', usage=None, choices=[SimpleNamespace(
                    finish_reason=None, delta=SimpleNamespace(content='回覆'))])
                yield SimpleNamespace(model='actual-chat', usage=SimpleNamespace(model_dump=lambda: {
                    'prompt_tokens': 11, 'completion_tokens': 3}), choices=[SimpleNamespace(
                    finish_reason='stop', delta=SimpleNamespace(content=None))])
            async def close(self): pass
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
                'AI_VT_TEST_MODE': 'true', 'AI_VT_MEMORY_DIR': directory}), \
                mock.patch.object(chat_service, 'chat_create_with_fallback', new=mock.AsyncMock(return_value=Stream())):
            token = prompt_logger.trace_event.set(cli.uuid.uuid4())
            try:
                reply = asyncio.run(chat_service.stream_agent_a([{'role': 'user', 'content': 'hi'}], mock.AsyncMock()))
            finally:
                prompt_logger.trace_event.reset(token)
            self.assertEqual(reply, '回覆')
            diagnostic = json.loads((Path(directory) / 'trace.jsonl').read_text())
            self.assertEqual(diagnostic['model'], 'actual-chat')
            self.assertEqual(diagnostic['usage'], {'prompt_tokens': 11, 'completion_tokens': 3})
            self.assertEqual(diagnostic['finish_reason'], 'stop')
            self.assertIn('output_token_estimate', diagnostic)

    def test_case_execution_resets_and_uses_fresh_sessions_and_preserves_both_reports(self):
        core = core_cases()
        cases = core + generated_cases(core)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = root / 'cases.json'
            scenario.write_text(json.dumps(cases), encoding='utf-8')
            args = argparse.Namespace(scenario=str(scenario), max_turns=0, retries=0, startup_timeout=1, turn_timeout=1)
            calls = []
            async def turn(ws, number, user, model, session, store, timeout, test_mode=None):
                calls.append((number, user, model, session, test_mode))
                return {**make_record(number), 'user': user, 'session_id': session}
            with mock.patch.object(cli, 'RUNS_DIR', root / 'runs'), \
                 mock.patch.object(cli, 'test_database_url', return_value='postgresql://test/db'), \
                 mock.patch.object(cli.MemoryRunStore, 'open'), mock.patch.object(cli.MemoryRunStore, 'close'), \
                 mock.patch.object(cli.MemoryRunStore, 'case_state', return_value={'jobs': []}), \
                 mock.patch.object(cli, 'start_backend', return_value=(mock.Mock(), mock.Mock())), \
                 mock.patch.object(cli, 'connect_backend', side_effect=lambda *a: FakeSocket()), \
                 mock.patch.object(cli, 'stop_backend'), \
                 mock.patch.object(cli, 'control_step', new=mock.AsyncMock(return_value={'acknowledged': True})) as control, \
                 mock.patch.object(cli, 'read_turn_trace', return_value=[{'stage': 'chat_context'}]), \
                 mock.patch.object(cli, 'check_turn'), \
                 mock.patch.object(cli, 'evaluate_semantics', new=mock.AsyncMock(return_value=set())) as review, \
                 mock.patch.object(cli, 'generate_cases', new=mock.AsyncMock()) as generate, \
                 mock.patch.object(cli, 'run_turn', side_effect=turn), mock.patch.object(cli, 'print_turn'):
                output, status = asyncio.run(cli.run(args))
            self.assertEqual(status, 'completed')
            review.assert_awaited_once()
            generate.assert_not_called()
            self.assertEqual(sum(c.args[1] == 'reset' for c in control.call_args_list), 28)
            self.assertTrue(all(call[2] == 'Rushia' for call in calls))
            metadata = json.loads((output / 'run.json').read_text())
            self.assertEqual(len(metadata['completed_case_ids']), 28)
            self.assertEqual(metadata['cleanup']['schema'], 'removed')
            self.assertFalse((output / 'memory').exists())
            self.assertEqual(len((output / 'case_states.jsonl').read_text().splitlines()), 28)
            records = [json.loads(line) for line in (output / 'turns.jsonl').read_text().splitlines()]
            self.assertTrue(all('memory_case_state' not in record for record in records))
            long_probe = [r for r in records if r.get('case_group') == 'long_term' and r['phase'] == 'probe']
            self.assertEqual(len({r['session_id'] for r in long_probe}), len(long_probe))
            memory_report = (output / 'memory_report.md').read_text()
            expression_report = (output / 'expression_report.md').read_text()
            self.assertIn('expression_report.md', memory_report)
            self.assertIn('case_020', expression_report)
            self.assertIn('(memory_report.md)', expression_report)
            self.assertIn('case_states.jsonl', memory_report)
            self.assertNotIn('```json', memory_report)
            self.assertNotIn('```json', expression_report)
            self.assertFalse((output / 'report.md').exists())
            self.assertEqual(json.loads((output / 'cases.json').read_text()), cases)
            self.assertEqual(json.loads(scenario.read_text()), cases)

    def test_generation_failure_keeps_core_and_cleanup_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(scenario=None, max_turns=0, retries=0, startup_timeout=1, turn_timeout=1)
            with mock.patch.object(cli, 'RUNS_DIR', root / 'runs'), \
                 mock.patch.object(cli, 'test_database_url', return_value='postgresql://test/db'), \
                 mock.patch.object(cli.MemoryRunStore, 'close'), \
                 mock.patch.object(cli, 'generate_cases', new=mock.AsyncMock(side_effect=RuntimeError('generation error'))), \
                 mock.patch('infrastructure.ai_client._role_clients', {}), \
                 mock.patch.object(cli, 'start_backend') as start:
                output, status = asyncio.run(cli.run(args))
            start.assert_not_called()
            self.assertEqual(status, 'failed')
            self.assertEqual(len(json.loads((output / 'cases.json').read_text())), 23)
            metadata = json.loads((output / 'run.json').read_text())
            self.assertEqual(metadata['executed_cases'], 0)
            self.assertEqual(metadata['completed_case_ids'], [])


if __name__ == '__main__':
    unittest.main()
