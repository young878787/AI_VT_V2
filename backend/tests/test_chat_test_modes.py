"""在實際 WS 協調邊界驗證來源關閉、正式模式與中斷；模型使用替身。"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from api.routes import chat_ws
from core import prompt_logger
from domain.chat_test_mode import ChatTestMode, resolve_test_mode
from backend.tests.test_emotion_chat_ws import FakeWebSocket, jev_answers


class ChatTestModeTests(unittest.TestCase):
    def run_modes(self, modes, test_instance=True, initial_history=None):
        socket = FakeWebSocket([dict(content=f'query {i}', session_id='shared_session', turn_id=f'turn_{i}', test_mode=m)
                                for i, m in enumerate(modes)])
        runtime = SimpleNamespace(accept=AsyncMock(side_effect=lambda *a: uuid4()),
            retrieve=AsyncMock(return_value=({}, 'DB fact')), route_background=Mock(), reset=AsyncMock())
        socket.app = SimpleNamespace(state=SimpleNamespace(memory_runtime=runtime))
        contexts, jev_contexts = [], []
        async def chat(messages, send):
            contexts.append(messages)
            await send('reply')
            return 'reply'
        async def jev(context, questions):
            jev_contexts.append(context)
            return jev_answers()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'AI_VT_TEST_MODE': 'true' if test_instance else 'false', 'AI_VT_MEMORY_DIR': directory}), \
                patch.object(chat_ws, 'CHAT_PERSISTENCE_ENABLED', True), \
                patch.object(chat_ws, 'load_session_messages', return_value=initial_history or []) as load_messages, \
                patch.object(chat_ws, 'load_session_summary', return_value='prior summary') as load_summary, \
                patch.object(chat_ws, 'load_session_emotion_state', return_value=None) as load_emotion, \
                patch.object(chat_ws, 'save_session_messages') as save_messages, \
                patch.object(chat_ws, 'save_session_emotion_state') as save_emotion, \
                patch.object(chat_ws, 'call_jev', side_effect=jev), \
                patch.object(chat_ws, 'stream_agent_a', side_effect=chat), \
                patch.object(chat_ws, 'broadcast_to_displays'), \
                patch.object(chat_ws, 'synthesize_and_send_voice', new=AsyncMock()), \
                patch.object(chat_ws, 'log_turn'):
            asyncio.run(chat_ws.websocket_endpoint(socket))
            trace_file = pathlib.Path(directory) / 'trace.jsonl'
            traces = [json.loads(line) for line in trace_file.read_text().splitlines()] if trace_file.exists() else []
        return SimpleNamespace(runtime=runtime, contexts=contexts, jev=jev_contexts, traces=traces,
            loads=[load_messages, load_summary, load_emotion], saves=[save_messages, save_emotion], socket=socket)

    def test_short_only_has_history_but_never_calls_long_term(self):
        result = self.run_modes(['short_only', 'short_only'])
        result.runtime.accept.assert_not_called()
        result.runtime.retrieve.assert_not_called()
        result.runtime.route_background.assert_not_called()
        self.assertEqual(len(result.contexts[1][1:-1]), 2)
        contexts = [t for t in result.traces if t['stage'] == 'chat_context']
        self.assertEqual([t['turn_id'] for t in contexts], ['turn_0', 'turn_1'])
        self.assertTrue(all('memory_event_id' not in t for t in contexts))

    def test_repeated_memory_probes_never_load_collect_or_persist_short_term(self):
        result = self.run_modes(['memory_probe', 'memory_probe'], initial_history=[dict(role='user', content='secret hint')])
        result.runtime.accept.assert_not_called()
        result.runtime.route_background.assert_not_called()
        self.assertEqual(result.runtime.retrieve.await_count, 2)
        self.assertTrue(all(c.kwargs['recent_dialogue'] == [] and c.kwargs['summary'] == ''
                            for c in result.runtime.retrieve.await_args_list))
        for method in result.loads + result.saves:
            method.assert_not_called()
        self.assertTrue(all(len(c) == 2 for c in result.contexts))
        self.assertTrue(all(not c['recent_dialogue'] for c in result.jev))
        self.assertTrue(all(c[-1]['content'].startswith('query') for c in result.contexts))

    def test_seed_disables_chat_retrieval_but_routes_background(self):
        result = self.run_modes(['memory_seed'])
        result.runtime.accept.assert_awaited_once()
        result.runtime.retrieve.assert_not_called()
        result.runtime.route_background.assert_called_once()
        self.assertNotIn('DB fact', result.contexts[0][0]['content'])

    def test_mixed_context_setup_reads_no_db_and_probe_receives_user_context(self):
        result = self.run_modes(['short_only', 'mixed_read'])
        result.runtime.retrieve.assert_awaited_once()
        result.runtime.accept.assert_not_called()
        result.runtime.route_background.assert_not_called()
        dialogue = result.runtime.retrieve.await_args.kwargs['recent_dialogue']
        self.assertEqual([m['content'] for m in dialogue if m['role'] == 'user'], ['query 0'])
        self.assertNotIn('DB fact', result.contexts[0][0]['content'])
        self.assertIn('DB fact', result.contexts[1][0]['content'])

    def test_mixed_read_and_update_use_fixed_write_boundaries(self):
        result = self.run_modes(['mixed_read', 'mixed_update', 'mixed_read'])
        self.assertEqual(result.runtime.retrieve.await_count, 3)
        result.runtime.accept.assert_awaited_once()
        result.runtime.route_background.assert_called_once()
        self.assertEqual(len(result.contexts[2][1:-1]), 4)
        self.assertIn('DB fact', result.contexts[2][0]['content'])

    def test_production_ignores_even_invalid_mode(self):
        result = self.run_modes(['short_only', 'invalid'], test_instance=False)
        self.assertEqual(result.runtime.accept.await_count, 2)
        self.assertEqual(result.runtime.retrieve.await_count, 2)
        self.assertEqual(result.runtime.route_background.call_count, 2)
        self.assertEqual(result.traces, [])

    def test_mode_transition_clears_prior_short_term_before_probe(self):
        result = self.run_modes(['short_only', 'memory_probe', 'memory_probe'])
        self.assertTrue(all(len(c) == 2 for c in result.contexts[1:]))
        self.assertTrue(all(not c['recent_dialogue'] for c in result.jev[1:]))

    def test_invalid_mode_rejected_in_test_instance(self):
        with patch.dict(os.environ, {'AI_VT_TEST_MODE': 'true'}):
            with self.assertRaises(ValueError):
                resolve_test_mode('arbitrary_flags')
        with patch.dict(os.environ, {'AI_VT_TEST_MODE': 'false'}):
            self.assertIsNone(resolve_test_mode('arbitrary_flags'))

    def test_interrupted_read_only_turn_never_enqueues_or_persists_probe_history(self):
        from fastapi import WebSocketDisconnect
        for mode in ('short_only', 'memory_probe'):
            with self.subTest(mode=mode):
                partial_sent, completed = asyncio.Event(), asyncio.Event()
                contexts, payloads = [], []
                class Socket:
                    index = 0
                    async def accept(self): pass
                    async def receive_text(self):
                        self.index += 1
                        if self.index == 1:
                            return json.dumps(dict(content='first', turn_id='turn_1', session_id='shared_session', test_mode=mode))
                        if self.index == 2:
                            await partial_sent.wait()
                            return json.dumps(dict(content='second', turn_id='turn_2', session_id='shared_session', test_mode=mode))
                        await completed.wait()
                        raise WebSocketDisconnect()
                    async def send_json(self, payload):
                        payloads.append(payload)
                        if payload.get('type') == 'stream_end': completed.set()
                async def chat(messages, send):
                    contexts.append(messages)
                    if messages[-1]['content'] == 'first':
                        await send('sent partial')
                        partial_sent.set()
                        await asyncio.Event().wait()
                    await send('completed')
                    return 'completed'
                runtime = SimpleNamespace(accept=AsyncMock(), retrieve=AsyncMock(return_value=({}, '')),
                    route_background=Mock(), reset=AsyncMock())
                socket = Socket();socket.app = SimpleNamespace(state=SimpleNamespace(memory_runtime=runtime))
                with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AI_VT_TEST_MODE': 'true', 'AI_VT_MEMORY_DIR': directory}), \
                        patch.object(chat_ws, 'CHAT_PERSISTENCE_ENABLED', True), \
                        patch.object(chat_ws, 'load_session_messages', return_value=[]), \
                        patch.object(chat_ws, 'load_session_summary', return_value=''), \
                        patch.object(chat_ws, 'load_session_emotion_state', return_value=None), \
                        patch.object(chat_ws, 'save_session_messages') as saved, \
                        patch.object(chat_ws, 'save_session_emotion_state'), \
                        patch.object(chat_ws, 'call_jev', new=AsyncMock(return_value=jev_answers())), \
                        patch.object(chat_ws, 'stream_agent_a', side_effect=chat), \
                        patch.object(chat_ws, 'broadcast_to_displays'), \
                        patch.object(chat_ws, 'synthesize_and_send_voice', new=AsyncMock()), \
                        patch.object(chat_ws, 'log_turn'):
                    asyncio.run(asyncio.wait_for(chat_ws.websocket_endpoint(socket), 2))
                runtime.accept.assert_not_called();runtime.route_background.assert_not_called()
                cancelled = next(p for p in payloads if p['type'] == 'turn_cancelled')
                self.assertEqual(cancelled['partial_text'], 'sent partial')
                if mode == 'memory_probe':
                    saved.assert_not_called()
                    self.assertEqual(len(contexts[1]), 2)
                else:
                    self.assertIn(dict(role='assistant', content='sent partial'), contexts[1])
                    self.assertTrue(any(any(m.get('status') == 'interrupted' for m in call.args[1]) for call in saved.call_args_list))

    def test_worker_trace_is_correlated_with_bound_turn(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AI_VT_TEST_MODE': 'true', 'AI_VT_MEMORY_DIR': directory}):
            event = uuid4()
            prompt_logger.bind_trace_event(event, 'background_turn')
            token = prompt_logger.trace_event.set(event)
            try:
                prompt_logger.trace('memory_candidates', {'candidates': []})
            finally:
                prompt_logger.trace_event.reset(token)
            trace = json.loads((pathlib.Path(directory) / 'trace.jsonl').read_text())
            self.assertEqual(trace['turn_id'], 'background_turn')
            self.assertEqual(trace['memory_event_id'], str(event))
