"""遠端 vLLM 的 thinking off 與既有端點參數回歸。"""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.ai_request_params import no_thinking_extra_body, provider_from_url
from infrastructure import ai_client
from services.memory_agent_client import MemoryAgentClient


class AIRequestParamsTests(unittest.IsolatedAsyncioTestCase):
    def test_endpoint_thinking_parameters(self):
        self.assertEqual(provider_from_url("https://remote.example/v1"), "custom")
        for provider in ("custom", "nvidia"):
            self.assertEqual(no_thinking_extra_body(provider),
                             {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(no_thinking_extra_body("qwen"), {"enable_thinking": False})
        for provider in ("openai", "openrouter", "google"):
            self.assertEqual(no_thinking_extra_body(provider), {})

    async def test_chat_and_summary_disable_thinking_without_losing_parameters(self):
        create = AsyncMock(return_value=object())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        extra = {"chat_template_kwargs": {"enable_thinking": True, "other": "keep"}, "top_k": 20}
        with patch.object(ai_client, "CHAT_PROVIDER", "custom"), patch.dict(ai_client._role_clients, chat=client):
            await ai_client.chat_create_with_fallback(role="chat", model="Gemma4-31B",
                messages=[], temperature=.85, max_tokens=400, stream=True, extra_body=extra)
            request = create.call_args.kwargs
            self.assertEqual(request["extra_body"], {"chat_template_kwargs": {
                "enable_thinking": False, "other": "keep"}, "top_k": 20})
            self.assertEqual((request["temperature"], request["max_tokens"], request["stream"]), (.85, 400, True))
            self.assertTrue(extra["chat_template_kwargs"]["enable_thinking"])
            await ai_client.chat_create_with_fallback(role="chat", model="Gemma4-31B", messages=[], temperature=.3)
            self.assertEqual(create.call_args.kwargs["extra_body"], no_thinking_extra_body("custom"))

    async def test_openai_keeps_token_parameter_compatibility(self):
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(ai_client, "CHAT_PROVIDER", "openai"), patch.dict(ai_client._role_clients, chat=client):
            await ai_client.chat_create_with_fallback(role="chat", model="test", max_tokens=400)
        self.assertEqual(create.call_args.kwargs["max_completion_tokens"], 400)
        self.assertNotIn("max_tokens", create.call_args.kwargs)
        self.assertNotIn("extra_body", create.call_args.kwargs)

    async def test_both_memory_roles_disable_thinking_and_keep_required_tools(self):
        settings = SimpleNamespace(memory_base_url="https://remote.example/v1",
                                   memory_api_key="test-key", memory_model="Gemma4-31B")
        tools = [{"type": "function", "function": {"name": "result", "parameters": {
            "type": "object", "properties": {"ok": {"type": "boolean"}}}}}]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name="result", arguments='{"ok": true}'))]))], usage=None)
        for role in ("intake", "librarian"):
            agent = MemoryAgentClient(settings, role)
            try:
                with patch.object(agent.client.chat.completions, "create", AsyncMock(return_value=response)) as create:
                    results, diagnostic = await agent.call("test", {"text": "synthetic"}, tools)
                self.assertEqual(results, [("result", {"ok": True})])
                self.assertEqual(diagnostic["role"], role)
                self.assertIsNone(diagnostic["actual_model"])
                self.assertIsNone(diagnostic["finish_reason"])
                request = create.call_args.kwargs
                self.assertEqual(request["extra_body"], no_thinking_extra_body("custom"))
                self.assertEqual(request["tool_choice"], "required")
                self.assertEqual(request["max_completion_tokens"], 4000)
                self.assertEqual(request["tools"], tools)
            finally:
                await agent.client.close()
