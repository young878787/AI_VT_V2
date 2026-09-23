import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.chat_ws import _execute_memory_tool_calls
from services.agent_tool_pipeline import (
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    extract_agent_tool_calls,
    filter_tool_calls_for_pool,
    summarize_tool_names,
)


def response(content="", tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=tool_calls or [],
    ))])


def native_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


class MemoryToolPipelineTests(unittest.TestCase):
    def test_memory_tool_pool_is_explicit(self):
        self.assertEqual(MEMORY_AGENT_ALLOWED_TOOL_NAMES, {"update_user_profile", "save_memory_note"})

    def test_extracts_native_memory_call(self):
        calls = extract_agent_tool_calls(
            response(tool_calls=[native_call("save_memory_note", '{"content":"喜歡拿鐵"}')]),
            model_name="Hiyori", label="Memory Agent",
        )
        self.assertEqual(calls, [{"name": "save_memory_note", "arguments": {"content": "喜歡拿鐵"}}])

    def test_xml_fallback_preserves_memory_call(self):
        calls = extract_agent_tool_calls(
            response(content="<tool_call><function=save_memory_note><parameter=content>記得貓咪</parameter></tool_call>"),
            model_name="Hiyori", label="Memory Agent",
        )
        self.assertEqual(calls, [{"name": "save_memory_note", "arguments": {"content": "記得貓咪"}}])

    def test_native_call_takes_priority_over_duplicate_xml(self):
        calls = extract_agent_tool_calls(
            response(
                content="<tool_call><function=save_memory_note><parameter=content>舊內容</parameter></tool_call>",
                tool_calls=[native_call("save_memory_note", '{"content":"新內容"}')],
            ), model_name="Hiyori", label="Memory Agent",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["content"], "新內容")

    def test_expression_tool_cannot_enter_memory_execution(self):
        calls = extract_agent_tool_calls(
            response(tool_calls=[native_call("set_ai_behavior", '{"duration_sec":5}')]),
            model_name="Hiyori", label="Memory Agent",
        )
        self.assertEqual(calls, [])
        self.assertEqual(filter_tool_calls_for_pool(
            [{"name": "set_ai_behavior", "arguments": {}}],
            MEMORY_AGENT_ALLOWED_TOOL_NAMES, "Memory Agent",
        ), [])

    def test_incomplete_memory_call_is_rejected(self):
        calls = extract_agent_tool_calls(
            response(tool_calls=[native_call("save_memory_note", '{"content":"  "}')]),
            model_name="Hiyori", label="Memory Agent",
        )
        self.assertEqual(calls, [])

    def test_execute_memory_calls_uses_validated_arguments(self):
        notes = []
        result = asyncio.run(_execute_memory_tool_calls(
            [{"name": "save_memory_note", "arguments": {"content": "  記得今天  "}}],
            websocket=None, broadcast_func=None,
            execute_profile_update_fn=lambda *args, **kwargs: None,
            append_memory_note_fn=notes.append,
        ))
        self.assertEqual(notes, ["記得今天"])
        self.assertEqual(summarize_tool_names(result["memory_calls"]), ["save_memory_note"])


if __name__ == "__main__":
    unittest.main()
