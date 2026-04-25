import asyncio
import copy
import json
import os
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.agent_b_prompts import build_memory_prompt
from domain.tools import get_memory_tools
from services.chat_service import call_memory_agent
from api.routes.chat_ws import _execute_memory_tool_calls, websocket_endpoint
from services.memory_service import execute_profile_update
from domain.tools.schema_loader import DEFAULT_MODEL, _TOOLS_DIR, invalidate_cache, load_schema
from domain.agent_a_prompts import build_agent_a_prompt
from domain.jpaf import JPAFSession


class MemoryModelWiringTests(unittest.TestCase):
    class _FakeWebSocket:
        def __init__(self):
            self.payloads: list[dict] = []
            self._received: list[str] = []

        def queue_received_text(self, payload: str):
            self._received.append(payload)

        async def accept(self):
            return None

        async def receive_text(self):
            if self._received:
                return self._received.pop(0)
            await asyncio.sleep(0)
            raise WebSocketDisconnect()

        async def send_json(self, payload: dict):
            self.payloads.append(payload)

    def test_build_memory_prompt_uses_requested_model_schema_with_loader_fallback(self):
        fake_schema = {
            "prompt_config": {
                "memory": {
                    "system_role": "測試記憶代理",
                    "task_description": "依照模型專屬設定判斷要不要記憶。",
                    "update_user_profile": {
                        "description": "使用模型專屬的 profile 規則。",
                        "field_guide": [
                            {"field": "custom_notes", "description": "模型專屬欄位指南"}
                        ],
                        "examples": [
                            {
                                "input": "我最近迷上手沖咖啡",
                                "action": "add",
                                "field": "custom_notes",
                                "value": "最近迷上手沖咖啡",
                            }
                        ],
                    },
                    "save_memory_note": {
                        "description": "使用模型專屬的 note 規則。",
                        "format": "模型專屬格式",
                    },
                    "principles": ["模型專屬原則"],
                }
            }
        }

        with patch("domain.agent_b_prompts.load_schema", return_value=fake_schema) as mock_load_schema:
            prompt = build_memory_prompt(
                user_message="記住我喜歡咖啡",
                ai_role_reply="好，我會記得。",
                model_name="CustomModel",
            )

        mock_load_schema.assert_called_once_with("CustomModel")
        self.assertIn("你是測試記憶代理", prompt)
        self.assertIn("依照模型專屬設定判斷要不要記憶。", prompt)
        self.assertIn("使用模型專屬的 profile 規則。", prompt)
        self.assertIn("模型專屬格式", prompt)
        self.assertIn("模型專屬原則", prompt)

    def test_get_memory_tools_uses_requested_model_schema_with_loader_fallback(self):
        fake_schema = {
            "openai_tools": {
                "memory": [
                    {"type": "function", "function": {"name": "custom_memory_tool"}}
                ]
            }
        }

        with patch("domain.tools.load_schema", return_value=fake_schema) as mock_load_schema:
            tools = get_memory_tools("CustomModel")

        mock_load_schema.assert_called_once_with("CustomModel")
        self.assertEqual(tools, fake_schema["openai_tools"]["memory"])

    def test_call_memory_agent_uses_requested_model_memory_tools(self):
        captured_kwargs: dict = {}

        async def _fake_chat_create_with_fallback(**kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(choices=[])

        with patch("domain.tools.get_memory_tools", return_value=[{"function": {"name": "custom_memory_tool"}}]) as mock_get_memory_tools, \
            patch("services.chat_service.chat_create_with_fallback", side_effect=_fake_chat_create_with_fallback):
            asyncio.run(call_memory_agent([{"role": "system", "content": "memory"}], model_name="CustomModel"))

        mock_get_memory_tools.assert_called_once_with("CustomModel")
        self.assertEqual(captured_kwargs["tools"], [{"function": {"name": "custom_memory_tool"}}])

    def test_websocket_endpoint_passes_model_name_to_memory_prompt_and_agent(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "CustomModel"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        live2d_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"calm","intensity":0.35,"energy":0.35,"arc":"steady","hold_ms":2000}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "memory model wiring", None, None

        async def _fake_call_expression_agent(messages, model_name):
            return live2d_response

        async def _fake_call_memory_agent(messages, model_name):
            self.assertEqual(model_name, "CustomModel")
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="live2d-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system") as mock_build_memory_prompt, \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

            mock_build_memory_prompt.assert_called_once_with(
                "hello",
                "memory model wiring",
                "CustomModel",
            )

        asyncio.run(_run_route())

    def test_websocket_endpoint_passes_model_name_to_agent_a_prompt(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "CustomModel"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        live2d_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"calm","intensity":0.35,"energy":0.35,"arc":"steady","hold_ms":2000}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "agent-a text", None, None

        async def _fake_call_expression_agent(messages, model_name):
            return live2d_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system") as mock_build_agent_a_prompt, \
                patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="live2d-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

            mock_build_agent_a_prompt.assert_called_once_with(
                {},
                [],
                unittest.mock.ANY,
                model_name="CustomModel",
            )

        asyncio.run(_run_route())

    def test_websocket_endpoint_falls_back_to_default_model_for_invalid_model_name(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "../escape"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        live2d_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"calm","intensity":0.35,"energy":0.35,"arc":"steady","hold_ms":2000}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))
        observed_models: list[str] = []

        async def _fake_collect_agent_a(messages):
            return "memory model wiring", None, None

        async def _fake_call_expression_agent(messages, model_name):
            observed_models.append(model_name)
            return live2d_response

        async def _fake_call_memory_agent(messages, model_name):
            observed_models.append(model_name)
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="live2d-system") as mock_build_live2d_prompt, \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system") as mock_build_memory_prompt, \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

            mock_build_live2d_prompt.assert_called_once_with(
                "hello",
                "memory model wiring",
                None,
                None,
                DEFAULT_MODEL,
            )
            mock_build_memory_prompt.assert_called_once_with(
                "hello",
                "memory model wiring",
                DEFAULT_MODEL,
            )

        asyncio.run(_run_route())

        self.assertEqual(observed_models, [DEFAULT_MODEL, DEFAULT_MODEL])

    def test_websocket_endpoint_accepts_expression_intent_response_without_tool_calls(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"playful","intensity":0.7,"energy":0.6,"arc":"steady","hold_ms":1800}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "好呀，今天就輕鬆一點聊。", None, {"primary_emotion": "playful", "intensity": 0.6}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())

        self.assertTrue(any(payload.get("type") == "behavior" for payload in websocket.payloads))

    def test_websocket_endpoint_compiles_expression_intent_to_legacy_payloads(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "裝可愛一下", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"shy","intensity":0.66,"energy":0.42,"arc":"steady","hold_ms":2000,"blink_style":"shy_fast"}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "欸，不要這樣看我啦。", None, {"primary_emotion": "shy", "intensity": 0.6, "energy": 0.4}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())

        behavior_payload = next(
            payload for payload in websocket.payloads if payload.get("type") == "behavior"
        )
        blink_payload = next(
            payload for payload in websocket.payloads if payload.get("type") == "blink_control"
        )
        text_payload = next(
            payload for payload in websocket.payloads if payload.get("type") == "text_stream"
        )

        self.assertGreater(behavior_payload["headIntensity"], 0.18)
        self.assertEqual(behavior_payload["blushLevel"], 0.18)
        self.assertEqual(behavior_payload["eyeSync"], False)
        self.assertGreaterEqual(behavior_payload["eyeLOpen"], 0.78)
        self.assertGreater(behavior_payload["eyeROpen"], 0.75)
        self.assertNotEqual(behavior_payload["eyeROpen"], behavior_payload["eyeLOpen"])
        self.assertGreater(behavior_payload["mouthForm"], 0.10)
        self.assertEqual(behavior_payload["durationSec"], 2.0)

        self.assertEqual(
            blink_payload,
            {
                "type": "blink_control",
                "action": "set_interval",
                "intervalMin": 0.8,
                "intervalMax": 1.5,
            },
        )
        self.assertEqual(text_payload, {"type": "text_stream", "content": "欸，不要這樣看我啦。"})

    def test_websocket_endpoint_sends_expression_plan_before_legacy_behavior(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "做鬼臉", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"playful","intensity":0.8,"energy":0.75,"arc":"pop_then_settle","hold_ms":1600,"blink_style":"teasing_pause","must_include":["smirk_left"]}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "欸嘿，才不給你猜到我在想什麼。", None, {"primary_emotion": "playful", "intensity": 0.7, "energy": 0.7}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays") as mock_broadcast, \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

            return mock_broadcast

        mock_broadcast = asyncio.run(_run_route())

        payload_types = [payload.get("type") for payload in websocket.payloads]
        self.assertIn("expression_plan", payload_types)
        self.assertIn("blink_control", payload_types)
        self.assertIn("behavior", payload_types)
        self.assertLess(payload_types.index("expression_plan"), payload_types.index("blink_control"))
        self.assertLess(payload_types.index("expression_plan"), payload_types.index("behavior"))

        broadcast_types = [call.args[0].get("type") for call in mock_broadcast.await_args_list]
        self.assertIn("expression_plan", broadcast_types)
        self.assertIn("blink_control", broadcast_types)
        self.assertIn("behavior", broadcast_types)
        self.assertLess(broadcast_types.index("expression_plan"), broadcast_types.index("blink_control"))
        self.assertLess(broadcast_types.index("expression_plan"), broadcast_types.index("behavior"))

    def test_websocket_endpoint_fails_fast_on_legacy_expression_tool_call_shape_without_json_content(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="set_ai_behavior",
                            arguments='{"duration_sec": 2.0}',
                        )
                    )
                ],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "memory model wiring", None, {"primary_emotion": "calm", "intensity": 0.35, "energy": 0.35}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="live2d-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        with self.assertRaisesRegex(ValueError, "Expression Agent returned legacy tool-call output without JSON intent content"):
            asyncio.run(_run_route())

        self.assertIn(
            {
                "type": "error",
                "content": "API 錯誤: Expression Agent returned legacy tool-call output without JSON intent content",
            },
            websocket.payloads,
        )

    def test_websocket_endpoint_reraises_turn_processing_errors_after_sending_error_payload(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "Hiyori"}')

        async def _fake_collect_agent_a(messages):
            raise ValueError("boom")

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        with self.assertRaisesRegex(ValueError, "boom"):
            asyncio.run(_run_route())

        self.assertIn(
            {"type": "error", "content": "API 錯誤: boom"},
            websocket.payloads,
        )

    def test_chat_orchestrator_persists_model_specific_schema_field_through_real_execute_profile_update(self):
        custom_schema = {
            "openai_tools": {
                "memory": [
                    {
                        "type": "function",
                        "function": {
                            "name": "update_user_profile",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["add", "remove", "update"],
                                    },
                                    "field": {
                                        "type": "string",
                                        "enum": ["favorite_drinks", "communication_style"],
                                    },
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    }
                ]
            }
        }
        starting_profile = {
            "core_traits": [],
            "dislikes": [],
            "recent_interests": [],
            "communication_style": "",
            "custom_notes": [],
            "favorite_drinks": [],
        }
        saved_profiles: list[dict] = []

        with patch("services.agent_tool_pipeline.load_schema", return_value=custom_schema), \
            patch("services.memory_service.load_schema", return_value=custom_schema), \
            patch("services.memory_service.load_user_profile", return_value=starting_profile.copy()), \
            patch("services.memory_service.save_user_profile", side_effect=lambda profile: saved_profiles.append(profile.copy())):
            asyncio.run(
                _execute_memory_tool_calls(
                    memory_calls=[
                        {
                            "name": "update_user_profile",
                            "arguments": {
                                "action": "add",
                                "field": "favorite_drinks",
                                "value": "手沖咖啡",
                            },
                        }
                    ],
                    websocket=self._FakeWebSocket(),
                    broadcast_func=lambda payload: None,
                    execute_profile_update_fn=execute_profile_update,
                    append_memory_note_fn=lambda note: None,
                    model_name="CustomModel",
                )
            )

        self.assertEqual(saved_profiles, [{**starting_profile, "favorite_drinks": ["手沖咖啡"]}])

    def test_execute_profile_update_uses_schema_type_for_custom_scalar_field(self):
        custom_schema = {
            "openai_tools": {
                "memory": [
                    {
                        "type": "function",
                        "function": {
                            "name": "update_user_profile",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["add", "remove", "update"],
                                    },
                                    "field": {
                                        "type": "string",
                                        "enum": ["status_message"],
                                    },
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    }
                ]
            },
            "prompt_config": {
                "memory": {
                    "update_user_profile": {
                        "field_guide": [
                            {
                                "field": "status_message",
                                "description": "目前狀態文字",
                                "value_shape": "string",
                            }
                        ]
                    }
                }
            },
        }
        starting_profile = {
            "core_traits": [],
            "dislikes": [],
            "recent_interests": [],
            "communication_style": "",
            "custom_notes": [],
            "status_message": [],
        }
        saved_profiles: list[dict] = []

        with patch("services.memory_service.load_schema", return_value=custom_schema), \
            patch("services.memory_service.load_user_profile", return_value=starting_profile.copy()), \
            patch("services.memory_service.save_user_profile", side_effect=lambda profile: saved_profiles.append(profile.copy())):
            result = execute_profile_update(
                action="update",
                field="status_message",
                value="想喝咖啡",
                model_name="CustomModel",
            )

        self.assertEqual(result["status_message"], "想喝咖啡")
        self.assertEqual(saved_profiles, [{**starting_profile, "status_message": "想喝咖啡"}])

    def test_build_agent_a_prompt_surfaces_schema_driven_custom_profile_fields(self):
        user_profile = {
            "core_traits": ["細心"],
            "communication_style": "直接",
            "favorite_drinks": ["手沖咖啡"],
            "status_message": "今天想慢慢聊",
        }
        custom_schema = {
            "openai_tools": {
                "memory": [
                    {
                        "type": "function",
                        "function": {
                            "name": "update_user_profile",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "field": {
                                        "type": "string",
                                        "enum": [
                                            "core_traits",
                                            "communication_style",
                                            "favorite_drinks",
                                            "status_message",
                                        ],
                                    }
                                },
                            },
                        },
                    }
                ]
            },
            "prompt_config": {
                "memory": {
                    "update_user_profile": {
                        "field_guide": [
                            {
                                "field": "favorite_drinks",
                                "description": "最喜歡的飲料",
                            },
                            {
                                "field": "status_message",
                                "description": "目前狀態",
                                "value_shape": "string",
                            },
                        ]
                    }
                }
            },
        }

        with patch("domain.agent_a_prompts.load_schema", return_value=custom_schema):
            prompt = build_agent_a_prompt(
                user_profile=user_profile,
                memory_notes="",
                session=JPAFSession(),
                model_name="CustomModel",
            )

        self.assertIn("- 特徵：細心", prompt)
        self.assertIn("- 溝通風格：直接", prompt)
        self.assertIn("- 最喜歡的飲料：手沖咖啡", prompt)
        self.assertIn("- 目前狀態：今天想慢慢聊", prompt)


class SchemaLoaderSafetyTests(unittest.TestCase):
    def tearDown(self):
        invalidate_cache()

    def test_load_schema_falls_back_to_default_for_invalid_model_name(self):
        invalid_model_name = "..\\escape"
        invalid_path = os.path.join(_TOOLS_DIR, f"{invalid_model_name}.json")

        if os.path.exists(invalid_path):
            os.remove(invalid_path)

        schema = load_schema(invalid_model_name)

        self.assertFalse(os.path.exists(invalid_path))
        self.assertEqual(schema, load_schema(DEFAULT_MODEL))

    def test_load_schema_falls_back_to_default_for_unknown_safe_model_name_without_creating_file(self):
        unknown_model_name = "TotallyUnknownModel"
        unknown_path = os.path.join(_TOOLS_DIR, f"{unknown_model_name}.json")

        if os.path.exists(unknown_path):
            os.remove(unknown_path)

        default_schema = copy.deepcopy(load_schema(DEFAULT_MODEL))
        invalidate_cache()

        schema = load_schema(unknown_model_name)

        self.assertFalse(os.path.exists(unknown_path))
        self.assertEqual(schema, default_schema)

    def test_load_schema_backfills_missing_nested_prompt_and_tool_properties_from_default(self):
        model_name = "SchemaLoaderDeepMergeRegression"
        custom_path = os.path.join(_TOOLS_DIR, f"{model_name}.json")

        try:
            with open(custom_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "openai_tools": {
                            "memory": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "update_user_profile",
                                        "parameters": {
                                            "properties": {
                                                "field": {
                                                    "enum": ["favorite_drinks"]
                                                }
                                            }
                                        },
                                    },
                                }
                            ]
                        },
                        "prompt_config": {
                            "memory": {
                                "update_user_profile": {
                                    "field_guide": [
                                        {
                                            "field": "favorite_drinks",
                                            "description": "最喜歡的飲料",
                                        }
                                    ]
                                }
                            }
                        },
                    },
                    f,
                    ensure_ascii=False,
                )

            schema = load_schema(model_name)

            memory_tool = next(
                tool
                for tool in schema["openai_tools"]["memory"]
                if tool["function"]["name"] == "update_user_profile"
            )
            properties = memory_tool["function"]["parameters"]["properties"]

            self.assertIn("action", properties)
            self.assertIn("value", properties)
            self.assertEqual(properties["field"]["enum"], ["favorite_drinks"])
            self.assertIn("save_memory_note", schema["prompt_config"]["memory"])
            self.assertEqual(
                schema["prompt_config"]["memory"]["update_user_profile"]["field_guide"],
                [
                    {
                        "field": "favorite_drinks",
                        "description": "最喜歡的飲料",
                    }
                ],
            )
        finally:
            invalidate_cache(model_name)
            if os.path.exists(custom_path):
                os.remove(custom_path)


if __name__ == "__main__":
    unittest.main()
