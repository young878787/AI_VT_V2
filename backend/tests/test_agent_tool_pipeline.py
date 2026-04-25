import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.agent_tool_pipeline import (
    EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    extract_agent_tool_calls,
    filter_tool_calls_for_pool,
    has_required_expression_behavior,
    summarize_tool_names,
)
from api.routes.chat_ws import (
    EXPRESSION_AGENT_ALLOWED_TOOL_NAMES as CHAT_WS_EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
    MEMORY_AGENT_ALLOWED_TOOL_NAMES as CHAT_WS_MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    _execute_chat_orchestrator_tool_calls,
    websocket_endpoint,
)


class AgentToolPipelineTests(unittest.TestCase):
    class _FakeWebSocket:
        def __init__(self):
            self.payloads: list[dict] = []
            self._received = []
            self.accepted = False

        def queue_received_text(self, payload: str):
            self._received.append(payload)

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            if self._received:
                return self._received.pop(0)
            await asyncio.sleep(0)
            raise WebSocketDisconnect()

        async def send_json(self, payload: dict):
            self.payloads.append(payload)

    def test_extract_agent_tool_calls_collects_native_and_xml_calls(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<thinking>hidden</thinking>"
                            "<tool_call><function=save_memory_note>"
                            "<parameter=content>記住今天喜歡拿鐵</parameter>"
                            "</tool_call>"
                        ),
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
            ]
        )

        calls = extract_agent_tool_calls(
            response,
            model_name="Hiyori",
            label="Expression Agent",
        )

        self.assertEqual(
            calls,
            [
                {"name": "set_ai_behavior", "arguments": {"duration_sec": 2.0}},
                {"name": "save_memory_note", "arguments": {"content": "記住今天喜歡拿鐵"}},
            ],
        )

    def test_extract_agent_tool_calls_salvages_malformed_native_args(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="set_ai_behavior",
                                    arguments=(
                                        '{"head_intensity": 0.65, "blush_level": 0.45, '
                                        '"eye_sync": fal: 4.5, "mouth_form": 0.25, '
                                        '"brow_l_y": 00.15, "eye_l_smile": 0.35}'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )

        calls = extract_agent_tool_calls(
            response,
            model_name="Hiyori",
            label="Expression Agent",
        )

        self.assertEqual(
            calls,
            [
                {
                    "name": "set_ai_behavior",
                    "arguments": {
                        "head_intensity": 0.65,
                        "blush_level": 0.45,
                        "mouth_form": 0.25,
                        "brow_l_y": 0.15,
                        "eye_l_smile": 0.35,
                    },
                }
            ],
        )

    def test_extract_agent_tool_calls_prefers_native_tool_call_over_xml_fallback(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=save_memory_note>"
                            "<parameter=content>xml fallback should be ignored</parameter>"
                            "</tool_call>"
                            "<tool_call><function=update_user_profile>"
                            "<parameter=nickname>小洋</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="save_memory_note",
                                    arguments='{"content": "native wins"}',
                                )
                            )
                        ],
                    )
                )
            ]
        )

        calls = extract_agent_tool_calls(
            response,
            model_name="Hiyori",
            label="Memory Agent",
        )

        self.assertEqual(
            calls,
            [
                {"name": "save_memory_note", "arguments": {"content": "native wins"}},
            ],
        )

    def test_extract_agent_tool_calls_keeps_xml_fallback_when_native_memory_call_is_incomplete(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=update_user_profile>"
                            "<parameter=action>add</parameter>"
                            "<parameter=field>custom_notes</parameter>"
                            "<parameter=value>喜歡手沖咖啡</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="update_user_profile",
                                    arguments=(
                                        '{"field": "custom_notes", "value": "喜歡手沖咖啡", '
                                        '"action": tru: }'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )

        calls = extract_agent_tool_calls(
            response,
            model_name="Hiyori",
            label="Memory Agent",
        )

        self.assertEqual(
            calls,
            [
                {
                    "name": "update_user_profile",
                    "arguments": {
                        "action": "add",
                        "field": "custom_notes",
                        "value": "喜歡手沖咖啡",
                    },
                }
            ],
        )

    def test_extract_agent_tool_calls_skips_update_user_profile_with_schema_invalid_enums(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="update_user_profile",
                                    arguments=(
                                        '{"action": "replace", "field": "nickname", '
                                        '"value": "Hiyori"}'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Memory Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Memory Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=update_user_profile")

    def test_extract_agent_tool_calls_and_execution_accept_model_specific_memory_field(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="update_user_profile",
                                    arguments=(
                                        '{"action": "add", "field": "favorite_drinks", '
                                        '"value": "手沖咖啡"}'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )
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
                                        "enum": ["favorite_drinks", "custom_notes"],
                                    },
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    }
                ]
            }
        }

        with patch("services.agent_tool_pipeline.load_schema", return_value=custom_schema):
            calls = extract_agent_tool_calls(
                response,
                model_name="CustomModel",
                label="Memory Agent",
            )

        self.assertEqual(
            calls,
            [
                {
                    "name": "update_user_profile",
                    "arguments": {
                        "action": "add",
                        "field": "favorite_drinks",
                        "value": "手沖咖啡",
                    },
                }
            ],
        )

        profile_updates: list[tuple[str, str, str]] = []
        with patch("services.agent_tool_pipeline.load_schema", return_value=custom_schema):
            result = asyncio.run(
                _execute_chat_orchestrator_tool_calls(
                    expression_calls=[],
                    memory_calls=calls,
                    websocket=self._FakeWebSocket(),
                    broadcast_func=lambda payload: None,
                    execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": profile_updates.append((action, field, value)),
                    append_memory_note_fn=lambda note: None,
                    model_name="CustomModel",
                )
            )

        self.assertEqual(profile_updates, [("add", "favorite_drinks", "手沖咖啡")])
        self.assertEqual(result["memory_calls"], calls)

    def test_extract_agent_tool_calls_keeps_same_name_xml_fallback_when_native_memory_call_has_schema_invalid_enums(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=update_user_profile>"
                            "<parameter=action>add</parameter>"
                            "<parameter=field>custom_notes</parameter>"
                            "<parameter=value>喜歡手沖咖啡</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="update_user_profile",
                                    arguments=(
                                        '{"action": "replace", "field": "nickname", '
                                        '"value": "喜歡手沖咖啡"}'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )

        calls = extract_agent_tool_calls(
            response,
            model_name="Hiyori",
            label="Memory Agent",
        )

        self.assertEqual(
            calls,
            [
                {
                    "name": "update_user_profile",
                    "arguments": {
                        "action": "add",
                        "field": "custom_notes",
                        "value": "喜歡手沖咖啡",
                    },
                }
            ],
        )

    def test_extract_agent_tool_calls_keeps_same_name_xml_fallback_when_native_blink_control_action_is_invalid(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=blink_control>"
                            "<parameter=action>force_blink</parameter>"
                            "<parameter=duration_sec>1.5</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="blink_control",
                                    arguments='{"action": "blink_now", "duration_sec": 4.0}',
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(
            calls,
            [
                {
                    "name": "blink_control",
                    "arguments": {"action": "force_blink", "duration_sec": 1.5},
                }
            ],
        )
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_keeps_same_name_xml_fallback_when_native_blink_control_action_is_missing(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=blink_control>"
                            "<parameter=action>pause</parameter>"
                            "<parameter=duration_sec>2.0</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="blink_control",
                                    arguments='{"duration_sec": 4.0}',
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(
            calls,
            [
                {
                    "name": "blink_control",
                    "arguments": {"action": "pause", "duration_sec": 2.0},
                }
            ],
        )
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_xml_only_invalid_blink_control_action(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=blink_control>"
                            "<parameter=action>blink_now</parameter>"
                            "<parameter=duration_sec>1.5</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_native_blink_control_set_interval_without_both_bounds(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="blink_control",
                                    arguments='{"action": "set_interval", "interval_max": 4.0}',
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_xml_blink_control_set_interval_without_both_bounds(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=blink_control>"
                            "<parameter=action>set_interval</parameter>"
                            "<parameter=interval_max>4.0</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_native_blink_control_set_interval_when_min_exceeds_max(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="blink_control",
                                    arguments=(
                                        '{"action": "set_interval", "interval_min": 5.0, "interval_max": 4.0}'
                                    ),
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_xml_blink_control_set_interval_when_min_exceeds_max(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<tool_call><function=blink_control>"
                            "<parameter=action>set_interval</parameter>"
                            "<parameter=interval_min>5.0</parameter>"
                            "<parameter=interval_max>4.0</parameter>"
                            "</tool_call>"
                        ),
                        tool_calls=[],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Expression Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call("[Expression Agent][SKIP_INCOMPLETE_TOOL_ARGS] name=blink_control")

    def test_extract_agent_tool_calls_skips_non_object_native_arguments_with_warning(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="update_user_profile",
                                    arguments='["add", "custom_notes", "喜歡手沖咖啡"]',
                                )
                            )
                        ],
                    )
                )
            ]
        )

        with patch("builtins.print") as mock_print:
            calls = extract_agent_tool_calls(
                response,
                model_name="Hiyori",
                label="Memory Agent",
            )

        self.assertEqual(calls, [])
        mock_print.assert_any_call(
            "[Memory Agent][SKIP_NON_OBJECT_TOOL_ARGS] name=update_user_profile, type=list"
        )

    def test_summarize_tool_names_flattens_groups_in_order(self):
        expression_calls = [{"name": "set_ai_behavior", "arguments": {}}]
        memory_calls = [
            {"name": "update_user_profile", "arguments": {}},
            {"name": "save_memory_note", "arguments": {}},
        ]

        self.assertEqual(
            summarize_tool_names(expression_calls, memory_calls),
            ["set_ai_behavior", "update_user_profile", "save_memory_note"],
        )

    def test_has_required_expression_behavior_checks_only_expression_pool(self):
        self.assertTrue(
            has_required_expression_behavior(
                [
                    {"name": "blink_control", "arguments": {}},
                    {"name": "set_ai_behavior", "arguments": {}},
                ]
            )
        )
        self.assertFalse(
            has_required_expression_behavior(
                [{"name": "blink_control", "arguments": {}}]
            )
        )

    def test_expression_agent_pool_contract_matches_task4_tool_set(self):
        self.assertEqual(
            EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
            {"set_ai_behavior", "blink_control"},
        )
        self.assertEqual(
            CHAT_WS_EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
            {"set_ai_behavior", "blink_control"},
        )

    def test_memory_agent_pool_contract_matches_task4_tool_set(self):
        self.assertEqual(
            MEMORY_AGENT_ALLOWED_TOOL_NAMES,
            {"update_user_profile", "save_memory_note"},
        )
        self.assertEqual(
            CHAT_WS_MEMORY_AGENT_ALLOWED_TOOL_NAMES,
            {"update_user_profile", "save_memory_note"},
        )

    def test_filter_tool_calls_for_expression_pool_warns_and_skips_wrong_pool_tools(self):
        calls = [
            {"name": "set_ai_behavior", "arguments": {"duration_sec": 2.0}},
            {"name": "update_user_profile", "arguments": {"field": "nickname"}},
            {"name": "save_memory_note", "arguments": {"content": "skip me"}},
        ]

        with patch("builtins.print") as mock_print:
            filtered = filter_tool_calls_for_pool(
                calls,
                allowed_tool_names=EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
                label="Expression Agent",
            )

        self.assertEqual(
            filtered,
            [{"name": "set_ai_behavior", "arguments": {"duration_sec": 2.0}}],
        )
        mock_print.assert_any_call("[Expression Agent][WARN] unexpected tool: update_user_profile")
        mock_print.assert_any_call("[Expression Agent][WARN] unexpected tool: save_memory_note")

    def test_filter_tool_calls_for_memory_pool_warns_and_skips_wrong_pool_tools(self):
        calls = [
            {"name": "update_user_profile", "arguments": {"field": "nickname"}},
            {"name": "set_ai_behavior", "arguments": {"duration_sec": 2.0}},
            {"name": "blink_control", "arguments": {"action": "force_blink"}},
        ]

        with patch("builtins.print") as mock_print:
            filtered = filter_tool_calls_for_pool(
                calls,
                allowed_tool_names=MEMORY_AGENT_ALLOWED_TOOL_NAMES,
                label="Memory Agent",
            )

        self.assertEqual(
            filtered,
            [{"name": "update_user_profile", "arguments": {"field": "nickname"}}],
        )
        mock_print.assert_any_call("[Memory Agent][WARN] unexpected tool: set_ai_behavior")
        mock_print.assert_any_call("[Memory Agent][WARN] unexpected tool: blink_control")

    def test_chat_orchestrator_executes_only_task4_pools_and_keeps_behavior_output_path(self):
        websocket = self._FakeWebSocket()
        broadcast_payloads: list[dict] = []
        profile_updates: list[tuple[str, str, str]] = []
        saved_notes: list[str] = []

        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "set_ai_behavior",
                        "arguments": {
                            "duration_sec": 2.5,
                            "mouth_form": 0.4,
                            "brow_l_y": 0.15,
                            "eye_l_smile": 0.35,
                        },
                    },
                    {
                        "name": "blink_control",
                        "arguments": {"action": "force_blink", "duration_sec": 1.5},
                    },
                    {
                        "name": "update_user_profile",
                        "arguments": {"action": "replace", "field": "nickname", "value": "wrong-pool"},
                    },
                    {
                        "name": "save_memory_note",
                        "arguments": {"content": "wrong-pool-note"},
                    },
                ],
                memory_calls=[
                    {
                        "name": "update_user_profile",
                        "arguments": {
                            "action": "add",
                            "field": "custom_notes",
                            "value": "暱稱：Hiyori",
                        },
                    },
                    {
                        "name": "save_memory_note",
                        "arguments": {"content": "remember this"},
                    },
                    {
                        "name": "set_ai_behavior",
                        "arguments": {"duration_sec": 99.0, "mouth_form": -1.0},
                    },
                    {
                        "name": "blink_control",
                        "arguments": {"action": "memory-pool-blink", "duration_sec": 9.0},
                    },
                ],
                websocket=websocket,
                broadcast_func=broadcast_payloads.append,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": profile_updates.append((action, field, value)),
                append_memory_note_fn=saved_notes.append,
            )
        )

        self.assertEqual(
            result["expression_calls"],
            [
                {
                    "name": "set_ai_behavior",
                    "arguments": {
                        "duration_sec": 2.5,
                        "mouth_form": 0.4,
                        "brow_l_y": 0.15,
                        "eye_l_smile": 0.35,
                    },
                },
                {
                    "name": "blink_control",
                    "arguments": {"action": "force_blink", "duration_sec": 1.5},
                },
            ],
        )
        self.assertEqual(
            result["memory_calls"],
            [
                {
                    "name": "update_user_profile",
                    "arguments": {
                        "action": "add",
                        "field": "custom_notes",
                        "value": "暱稱：Hiyori",
                    },
                },
                {
                    "name": "save_memory_note",
                    "arguments": {"content": "remember this"},
                },
            ],
        )
        self.assertEqual(
            result["behavior_payload"],
            {
                "type": "behavior",
                "headIntensity": 0.3,
                "blushLevel": 0.0,
                "eyeSync": True,
                "eyeLOpen": 1.0,
                "eyeROpen": 1.0,
                "durationSec": 2.5,
                "mouthForm": 0.4,
                "browLY": 0.15,
                "browRY": 0.0,
                "browLAngle": 0.0,
                "browRAngle": 0.0,
                "browLForm": 0.0,
                "browRForm": 0.0,
                "eyeLSmile": 0.35,
                "eyeRSmile": 0.0,
                "browLX": 0.0,
                "browRX": 0.0,
            },
        )
        self.assertEqual(
            websocket.payloads,
            [{"type": "blink_control", "action": "force_blink", "durationSec": 1.5}],
        )
        self.assertEqual(
            broadcast_payloads,
            [{"type": "blink_control", "action": "force_blink", "durationSec": 1.5}],
        )
        self.assertEqual(profile_updates, [("add", "custom_notes", "暱稱：Hiyori")])
        self.assertEqual(saved_notes, ["remember this"])

    def test_chat_orchestrator_drops_invalid_blink_numeric_args_without_crashing(self):
        websocket = self._FakeWebSocket()
        broadcast_payloads: list[dict] = []

        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "blink_control",
                        "arguments": {
                            "action": "set_interval",
                            "duration_sec": "oops",
                            "interval_min": "soon",
                            "interval_max": 4.5,
                        },
                    }
                ],
                memory_calls=[],
                websocket=websocket,
                broadcast_func=broadcast_payloads.append,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
            )
        )

        self.assertEqual(result["expression_calls"], [])
        self.assertEqual(websocket.payloads, [])
        self.assertEqual(broadcast_payloads, [])

    def test_chat_orchestrator_drops_incomplete_set_interval_blink_control_after_sanitization(self):
        websocket = self._FakeWebSocket()
        broadcast_payloads: list[dict] = []

        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "blink_control",
                        "arguments": {
                            "action": "set_interval",
                            "interval_min": "soon",
                            "interval_max": 4.5,
                        },
                    }
                ],
                memory_calls=[],
                websocket=websocket,
                broadcast_func=broadcast_payloads.append,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
            )
        )

        self.assertEqual(result["expression_calls"], [])
        self.assertEqual(websocket.payloads, [])
        self.assertEqual(broadcast_payloads, [])

    def test_chat_orchestrator_rejects_set_interval_when_min_exceeds_max(self):
        websocket = self._FakeWebSocket()
        broadcast_payloads: list[dict] = []

        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "blink_control",
                        "arguments": {
                            "action": "set_interval",
                            "interval_min": 5.0,
                            "interval_max": 4.0,
                        },
                    }
                ],
                memory_calls=[],
                websocket=websocket,
                broadcast_func=broadcast_payloads.append,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
            )
        )

        self.assertEqual(result["expression_calls"], [])
        self.assertEqual(websocket.payloads, [])
        self.assertEqual(broadcast_payloads, [])

    def test_chat_orchestrator_returns_speaking_rate_for_route_level_tts_handoff(self):
        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "set_ai_behavior",
                        "arguments": {
                            "duration_sec": 2.5,
                            "speaking_rate": 1.35,
                        },
                    }
                ],
                memory_calls=[],
                websocket=self._FakeWebSocket(),
                broadcast_func=lambda payload: None,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
            )
        )

        self.assertEqual(result["speaking_rate"], 1.35)

    def test_chat_orchestrator_carries_forward_prior_behavior_fields_for_partial_set_ai_behavior(self):
        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "set_ai_behavior",
                        "arguments": {
                            "duration_sec": 2.5,
                            "mouth_form": -0.2,
                        },
                    }
                ],
                memory_calls=[],
                websocket=self._FakeWebSocket(),
                broadcast_func=lambda payload: None,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
                last_behavior_payload={
                    "type": "behavior",
                    "headIntensity": 0.85,
                    "blushLevel": 0.35,
                    "eyeSync": False,
                    "eyeLOpen": 0.55,
                    "eyeROpen": 0.75,
                    "durationSec": 4.0,
                    "mouthForm": 0.45,
                    "browLY": 0.12,
                    "browRY": -0.18,
                    "browLAngle": 0.22,
                    "browRAngle": -0.24,
                    "browLForm": 0.16,
                    "browRForm": -0.14,
                    "eyeLSmile": 0.4,
                    "eyeRSmile": 0.3,
                    "browLX": 0.08,
                    "browRX": -0.09,
                    "speakingRate": 1.25,
                },
            )
        )

        self.assertEqual(
            result["behavior_payload"],
            {
                "type": "behavior",
                "headIntensity": 0.85,
                "blushLevel": 0.35,
                "eyeSync": False,
                "eyeLOpen": 0.55,
                "eyeROpen": 0.75,
                "durationSec": 2.5,
                "mouthForm": -0.2,
                "browLY": 0.12,
                "browRY": -0.18,
                "browLAngle": 0.22,
                "browRAngle": -0.24,
                "browLForm": 0.16,
                "browRForm": -0.14,
                "eyeLSmile": 0.4,
                "eyeRSmile": 0.3,
                "browLX": 0.08,
                "browRX": -0.09,
            },
        )
        self.assertEqual(result["speaking_rate"], 1.25)

    def test_chat_orchestrator_drops_wrong_typed_set_ai_behavior_values(self):
        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "set_ai_behavior",
                        "arguments": {
                            "duration_sec": "fast",
                            "mouth_form": 0.3,
                            "eye_sync": "false",
                        },
                    }
                ],
                memory_calls=[],
                websocket=self._FakeWebSocket(),
                broadcast_func=lambda payload: None,
                execute_profile_update_fn=lambda action, field, value: None,
                append_memory_note_fn=lambda note: None,
                last_behavior_payload={
                    "type": "behavior",
                    "durationSec": 4.0,
                    "mouthForm": 0.1,
                    "eyeSync": True,
                },
            )
        )

        self.assertEqual(result["behavior_payload"]["durationSec"], 4.0)
        self.assertEqual(result["behavior_payload"]["mouthForm"], 0.3)
        self.assertTrue(result["behavior_payload"]["eyeSync"])

    def test_websocket_endpoint_passes_expression_intent_speaking_rate_to_tts(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        live2d_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"calm","intensity":0.3,"energy":0.3,"arc":"steady","hold_ms":2000,"speaking_rate":1.4}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(
            SimpleNamespace(content="", tool_calls=[])
        )

        tts_calls: list[tuple[object, str, float]] = []

        async def _fake_collect_agent_a(messages):
            return "route level tts text", None, None

        async def _fake_call_expression_agent(messages, model_name):
            return live2d_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _fake_synthesize_and_send_voice(ws, text, speaking_rate):
            tts_calls.append((ws, text, speaking_rate))

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
                patch("api.routes.chat_ws.synthesize_and_send_voice", side_effect=_fake_synthesize_and_send_voice), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())

        self.assertTrue(websocket.accepted)
        self.assertEqual(len(tts_calls), 1)
        self.assertIs(tts_calls[0][0], websocket)
        self.assertEqual(tts_calls[0][1], "route level tts text")
        self.assertEqual(tts_calls[0][2], 1.4)

    def test_websocket_endpoint_defaults_speaking_rate_when_missing_from_expression_intent(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "first", "model_name": "Hiyori"}')
        websocket.queue_received_text('{"content": "second", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        live2d_responses = [
            _FakeResponse(
                SimpleNamespace(
                    content='{"primary_emotion":"playful","intensity":0.7,"energy":0.7,"arc":"steady","hold_ms":2000,"speaking_rate":1.4}',
                    tool_calls=[],
                )
            ),
            _FakeResponse(
                SimpleNamespace(
                    content='{"primary_emotion":"playful","intensity":0.7,"energy":0.7,"arc":"steady","hold_ms":1500}',
                    tool_calls=[],
                )
            ),
        ]
        memory_response = _FakeResponse(
            SimpleNamespace(content="", tool_calls=[])
        )

        tts_calls: list[tuple[object, str, float]] = []

        async def _fake_collect_agent_a(messages):
            user_content = messages[-1]["content"]
            return f"tts text for {user_content}", None, None

        async def _fake_call_expression_agent(messages, model_name):
            return live2d_responses.pop(0)

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _fake_synthesize_and_send_voice(ws, text, speaking_rate):
            tts_calls.append((ws, text, speaking_rate))

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
                patch("api.routes.chat_ws.synthesize_and_send_voice", side_effect=_fake_synthesize_and_send_voice), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())

        self.assertEqual(
            [(text, rate) for _, text, rate in tts_calls],
            [("tts text for first", 1.4), ("tts text for second", 1.0)],
        )

    def test_chat_orchestrator_skips_incomplete_memory_tool_calls(self):
        profile_updates: list[tuple[str, str, str]] = []
        saved_notes: list[str] = []

        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[],
                memory_calls=[
                    {
                        "name": "update_user_profile",
                        "arguments": {"field": "custom_notes", "value": "   "},
                    },
                    {
                        "name": "update_user_profile",
                        "arguments": {"action": "add", "field": "   ", "value": "喜歡貓"},
                    },
                    {
                        "name": "save_memory_note",
                        "arguments": {"content": "   "},
                    },
                ],
                websocket=self._FakeWebSocket(),
                broadcast_func=lambda payload: None,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": profile_updates.append((action, field, value)),
                append_memory_note_fn=saved_notes.append,
            )
        )

        self.assertEqual(profile_updates, [])
        self.assertEqual(saved_notes, [])
        self.assertEqual(result["memory_calls"], [])

    def test_chat_orchestrator_keeps_default_behavior_when_set_ai_behavior_is_only_in_memory_pool(self):
        result = asyncio.run(
            _execute_chat_orchestrator_tool_calls(
                expression_calls=[
                    {
                        "name": "save_memory_note",
                        "arguments": {"content": "wrong expression pool"},
                    }
                ],
                memory_calls=[
                    {
                        "name": "set_ai_behavior",
                        "arguments": {
                            "head_intensity": 0.9,
                            "duration_sec": 99.0,
                            "mouth_form": -0.8,
                        },
                    }
                ],
                websocket=self._FakeWebSocket(),
                broadcast_func=lambda payload: None,
                execute_profile_update_fn=lambda action, field, value, model_name="Hiyori": None,
                append_memory_note_fn=lambda note: None,
            )
        )

        self.assertEqual(result["expression_calls"], [])
        self.assertEqual(result["memory_calls"], [])
        self.assertEqual(result["behavior_payload"]["headIntensity"], 0.3)
        self.assertEqual(result["behavior_payload"]["durationSec"], 5.0)
        self.assertEqual(result["behavior_payload"]["mouthForm"], 0.0)


if __name__ == "__main__":
    unittest.main()
