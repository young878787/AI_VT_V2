import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.chat_ws import _execute_memory_tool_calls
from domain.agent_a_prompts import build_agent_a_prompt
from domain.agent_b_prompts import build_memory_prompt
from domain.emotion_state import NEUTRAL_EMOTION_STATE
from domain.tools import get_memory_tools
from domain.tools.schema_loader import DEFAULT_MODEL, load_schema
from services.chat_service import call_memory_agent


class MemoryModelWiringTests(unittest.TestCase):
    def test_memory_prompt_uses_requested_model_schema(self):
        schema = load_schema("Hiyori")
        with patch("domain.agent_b_prompts.load_schema", return_value=schema) as loader:
            prompt = build_memory_prompt("記住我喜歡咖啡", "好，我會記得。", "CustomModel")
        loader.assert_called_once_with("CustomModel")
        self.assertIn("記住我喜歡咖啡", prompt)
        self.assertIn("好，我會記得。", prompt)

    def test_memory_tools_use_requested_model(self):
        schema = {"openai_tools": {"memory": [{"type": "function", "function": {"name": "custom_memory_tool"}}]}}
        with patch("domain.tools.load_schema", return_value=schema) as loader:
            self.assertEqual(get_memory_tools("CustomModel"), schema["openai_tools"]["memory"])
        loader.assert_called_once_with("CustomModel")

    def test_memory_agent_receives_model_specific_tools(self):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[])

        with patch("domain.tools.get_memory_tools", return_value=[{"function": {"name": "custom_memory_tool"}}]), \
            patch("services.chat_service.chat_create_with_fallback", side_effect=fake_create):
            asyncio.run(call_memory_agent([{"role": "system", "content": "memory"}], "CustomModel"))
        self.assertEqual(captured["tools"][0]["function"]["name"], "custom_memory_tool")

    def test_chat_prompt_keeps_user_profile_separate_from_fixed_personality(self):
        prompt = build_agent_a_prompt(
            {"core_traits": ["喜歡咖啡"]}, "一起去過書店", NEUTRAL_EMOTION_STATE,
        )
        self.assertIn("露西亞", prompt)
        self.assertIn("喜歡咖啡", prompt)
        self.assertIn("一起去過書店", prompt)
        self.assertIn("wants_continue_interaction: 0.50", prompt)
        self.assertNotIn("JPAF", prompt)

    def test_memory_tool_execution_only_accepts_memory_pool(self):
        updates = []
        notes = []
        result = asyncio.run(_execute_memory_tool_calls(
            [
                {"name": "update_user_profile", "arguments": {"action": "add", "field": "custom_notes", "value": "喜歡貓"}},
                {"name": "save_memory_note", "arguments": {"content": "記得今天"}},
                {"name": "set_ai_behavior", "arguments": {"duration_sec": 10}},
            ],
            websocket=None,
            broadcast_func=None,
            execute_profile_update_fn=lambda action, field, value, model_name: updates.append((action, field, value)),
            append_memory_note_fn=notes.append,
        ))
        self.assertEqual([call["name"] for call in result["memory_calls"]], ["update_user_profile", "save_memory_note"])
        self.assertEqual(updates, [("add", "custom_notes", "喜歡貓")])
        self.assertEqual(notes, ["記得今天"])

    def test_unknown_model_schema_uses_default(self):
        self.assertEqual(load_schema("DefinitelyUnknownModel"), load_schema(DEFAULT_MODEL))


if __name__ == "__main__":
    unittest.main()
