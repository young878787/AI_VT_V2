import asyncio
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.chat_service import _VisibleTextFilter, build_chat_context, estimate_token_count
from domain.agent_a_prompts import build_agent_a_prompt, build_turn_scope_hint
from domain.emotion_state import EMOTION_FIELDS
from api.routes.chat_ws import websocket_endpoint
from backend.tests.test_emotion_chat_ws import jev_answers
from core.config import role_model_config, provider_from_url
from domain.input_event import normalize_chat_input
from backend.tests.chat_session_fakes import make_chat_session_service


class ArchitectureIntegrationTests(unittest.TestCase):
    def test_input_normalization_ignores_client_session_and_user_identity(self):
        ignored = normalize_chat_input({"content": "hello", "session_id": "../bad"}, "server_session")
        self.assertEqual(ignored["session_id"], "server_session")
        event = normalize_chat_input({"content": " 你好 ", "source": "voice", "turn_id": "bad/id", "user_id": "other"})
        self.assertEqual(event["text"], "你好")
        self.assertEqual(event["source"], "voice")
        self.assertEqual(event["user_id"], "default_user")
        self.assertNotEqual(event["turn_id"], "bad/id")

    def test_role_model_uses_only_its_triplet(self):
        with patch.dict("os.environ", {
            "CHAT_AI_API_KEY": "chat-key",
            "CHAT_AI_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "CHAT_AI_MODEL": "test-chat",
        }):
            provider, key, url, model = role_model_config("CHAT")
            self.assertEqual((provider, key, url, model), (
                "nvidia", "chat-key", "https://integrate.api.nvidia.com/v1", "test-chat"
            ))
            with patch.dict("os.environ", {"CHAT_AI_API_KEY": ""}):
                with self.assertRaisesRegex(RuntimeError, "CHAT_AI_API_KEY"):
                    role_model_config("CHAT")
        self.assertEqual(provider_from_url("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"), "qwen")
        self.assertEqual(provider_from_url("https://gateway.example/v1"), "custom")

    def test_chat_context_has_fixed_budget_and_keeps_latest_input(self):
        history = [{"role": "user", "content": "很久以前" * 8000},
                   {"role": "assistant", "content": "舊回覆" * 8000}]
        context = build_chat_context("角色設定" * 3000, history, "本輪問題" * 3000, budget=512)
        self.assertLessEqual(estimate_token_count(context), 512)
        self.assertEqual(context[-1]["role"], "user")
        self.assertTrue(context[-1]["content"].startswith("本輪問題"))

    def test_chat_context_trims_dynamic_sections_before_fixed_prompt_rules(self):
        emotion = {field: 0.0 for field in EMOTION_FIELDS}
        prompt = build_agent_a_prompt({}, "m" * 2000, emotion)
        prompt += "\n\n本 session 已完成的對話摘要：\n" + "s" * 4000
        context = build_chat_context(prompt, [], "本輪問題", budget=2400)
        self.assertLessEqual(estimate_token_count(context), 2400)
        self.assertIn("固定性格", context[0]["content"])
        self.assertIn("<untrusted_long_term_memory>", context[0]["content"])
        self.assertIn("此資料區段依 token 預算裁切", context[0]["content"])

    def test_turn_scope_hint_keeps_third_party_preference_separate(self):
        hint = build_turn_scope_hint(
            "照我的口味，你會建議我怎麼選？",
            [{"role": "user", "content": "我今天要替朋友挑生日蛋糕。"}],
        )
        self.assertIn("第三方偏好", hint)
        self.assertIn("對方偏好未知", hint)
        self.assertEqual(build_turn_scope_hint("我今天想吃蛋糕", []), "")

    def test_turn_scope_hint_requires_explicit_supported_reference(self):
        hint = build_turn_scope_hint(
            "可是那部分還是有點抖，你知道我說哪裡嗎？",
            [{"role": "user", "content": "我昨天把角色的頭髮物理調好了。"}],
        )
        self.assertIn("第一句必須先", hint)
        self.assertIn("不得縮小成來源未提及的部位", hint)
        advice_hint = build_turn_scope_hint(
            "那個快畫完了，你覺得最後上光應該注意什麼？",
            [{"role": "user", "content": "我最近在嘗試練習數位油畫。"}],
        )
        self.assertIn("不得只給建議而省略指代對象", advice_hint)

    def test_chat_context_strips_interruption_metadata_from_provider_messages(self):
        context = build_chat_context(
            "角色設定",
            [{"role": "assistant", "content": "只送出的半句", "status": "interrupted"}],
            "插話",
            budget=512,
        )
        self.assertEqual(context[1], {"role": "assistant", "content": "只送出的半句"})

    def test_stream_filter_hides_split_tags_and_reasoning(self):
        filtered = _VisibleTextFilter()
        chunks = ["你好<thi", "nk>秘密", "</think>，", "今天<shy_sta", "te>0.8</shy_state>很開心"]
        output = "".join(filtered.feed(chunk) for chunk in chunks) + filtered.finish()
        self.assertEqual(output, "你好，今天很開心")

    def test_new_turn_cancels_stale_jev_result(self):
        started = asyncio.Event()
        completed = asyncio.Event()
        payloads = []

        class Socket:
            index = 0

            async def accept(self):
                pass

            async def receive_text(self):
                self.index += 1
                if self.index == 1:
                    return json.dumps({"content": "第一句", "turn_id": "turn_1"})
                if self.index == 2:
                    await started.wait()
                    return json.dumps({"content": "第二句", "turn_id": "turn_2"})
                await completed.wait()
                raise WebSocketDisconnect()

            async def send_json(self, payload):
                payloads.append(payload)
                if payload.get("type") == "stream_end" and payload.get("turn_id") == "turn_2":
                    completed.set()

        async def fake_jev(context, questions):
            if context["current_user_input"] == "第一句":
                started.set()
                await asyncio.Event().wait()
            return jev_answers(0.7)

        async def fake_chat(messages, send_chunk):
            await send_chunk("第二句回覆")
            return "第二句回覆"

        runtime = SimpleNamespace(
            accept=AsyncMock(side_effect=[uuid4(), uuid4()]),
            retrieve=AsyncMock(return_value=({}, "")),
            route_background=Mock(),
            reset=AsyncMock(),
        )
        socket = Socket()
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime, chat_session_service=make_chat_session_service(),
        ))

        async def run():
            with patch("api.routes.chat_ws.call_jev", side_effect=fake_jev), \
                 patch("api.routes.chat_ws.stream_agent_a", side_effect=fake_chat), \
                 patch("api.routes.chat_ws.broadcast_to_displays"), \
                 patch("api.routes.chat_ws.log_turn"), \
                 patch("api.routes.chat_ws.synthesize_and_send_voice"):
                await websocket_endpoint(socket)

        asyncio.run(run())
        self.assertEqual([p["turn_id"] for p in payloads if p["type"] == "text_stream"], ["turn_2"])
        self.assertIn("turn_1", [p["turn_id"] for p in payloads if p["type"] == "turn_cancelled"])

    def test_interrupt_persists_only_sent_partial_as_interrupted(self):
        jev_started = asyncio.Event()
        release_jev = asyncio.Event()
        partial_sent = asyncio.Event()
        completed = asyncio.Event()
        payloads = []
        contexts = []

        class Socket:
            index = 0

            async def accept(self):
                pass

            async def receive_text(self):
                self.index += 1
                if self.index == 1:
                    return json.dumps({"content": "第一句", "turn_id": "turn_1", "session_id": "session_1"})
                if self.index == 2:
                    await jev_started.wait()
                    release_jev.set()
                    await partial_sent.wait()
                    return json.dumps({"content": "插話", "turn_id": "turn_2", "session_id": "session_1"})
                await completed.wait()
                raise WebSocketDisconnect()

            async def send_json(self, payload):
                payloads.append(payload)
                if payload.get("type") == "stream_end" and payload.get("turn_id") == "turn_2":
                    completed.set()

        async def fake_jev(context, questions):
            contexts.append(context)
            if context["current_user_input"] == "第一句":
                jev_started.set()
                await release_jev.wait()
            return jev_answers(0.7)

        async def fake_chat(messages, send_chunk):
            current_input = messages[-1]["content"]
            if current_input == "第一句":
                await send_chunk("只送出的半句")
                partial_sent.set()
                await asyncio.Event().wait()
            await send_chunk("第二句完整回覆")
            return "第二句完整回覆"

        runtime = SimpleNamespace(
            accept=AsyncMock(side_effect=[uuid4(), uuid4()]),
            retrieve=AsyncMock(return_value=({}, "")),
            route_background=Mock(),
            reset=AsyncMock(),
        )
        socket = Socket()
        chat_sessions = make_chat_session_service()
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime, chat_session_service=chat_sessions,
        ))

        async def run():
            with patch("api.routes.chat_ws.call_jev", side_effect=fake_jev), \
                 patch("api.routes.chat_ws.stream_agent_a", side_effect=fake_chat), \
                 patch("api.routes.chat_ws.broadcast_to_displays"), \
                 patch("api.routes.chat_ws.log_turn"), \
                 patch("api.routes.chat_ws.synthesize_and_send_voice"):
                await websocket_endpoint(socket)

        asyncio.run(run())
        cancelled = next(payload for payload in payloads if payload["type"] == "turn_cancelled")
        self.assertEqual(cancelled["status"], "interrupted")
        self.assertEqual(cancelled["partial_text"], "只送出的半句")
        self.assertEqual(
            contexts[1]["recent_dialogue"],
            [
                {"role": "user", "text": "第一句"},
                {"role": "assistant", "text": "只送出的半句"},
            ],
        )
        self.assertIn(
            {"role": "assistant", "content": "只送出的半句", "status": "interrupted", "turn_id": "turn_1"},
            chat_sessions.repository.messages,
        )

if __name__ == "__main__":
    unittest.main()
