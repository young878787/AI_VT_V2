"""Two-stage expression coordination uses the original JEV decision and one audio result."""
import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.routes.chat_ws import websocket_endpoint
from backend.tests.chat_session_fakes import make_chat_session_service
from backend.tests.test_emotion_chat_ws import FakeWebSocket, jev_answers
from services.chat_service import synthesize_and_send_voice
from services.expression_compiler import compile_expression_plan


class SpeechTurnTests(unittest.IsolatedAsyncioTestCase):
    async def context(self):
        intent = {"emotion": "sad", "arc": "shrink_then_recover", "energy": 0.3}
        return {"intent": intent, "plan": compile_expression_plan(intent, "Rushia", None, seed=1)}

    async def test_speech_plan_precedes_audio_and_retains_original_emotion(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                service = SimpleNamespace(is_enabled=lambda: enabled, synthesize=AsyncMock(return_value={
                    "audio_base64": "test", "duration_ms": 11000, "format": "wav",
                    "segments": [{"id": 0, "startMs": 0, "endMs": 11000}],
                }))
                send = AsyncMock()
                with patch("services.chat_service._load_tts_service", return_value=service):
                    await synthesize_and_send_voice(None, "嗯……我懂。我們慢慢來。", 1, "t1", send,
                                                    expression_context=asyncio.create_task(self.context()))
                plan, terminal = [call.args[0] for call in send.await_args_list]
                self.assertEqual(plan["stage"], "speech")
                self.assertEqual(plan["turn_id"], "t1")
                self.assertEqual(plan["debug"]["expressionFamily"], "sad")
                self.assertLess(plan["basePose"]["params"]["mouthForm"], 0)
                self.assertEqual(plan["speech"]["timingSource"], "audio" if enabled else "estimated")
                self.assertEqual(terminal["type"], "voice" if enabled else "voice_unavailable")
                if enabled:
                    self.assertEqual(plan["speech"]["durationMs"], 11000)
                    service.synthesize.assert_awaited_once()
                else:
                    self.assertLessEqual(plan["speech"]["durationMs"], 8000)
                    service.synthesize.assert_not_awaited()

    async def test_expression_failure_explicitly_releases_plan_wait(self):
        send = AsyncMock()
        context = await self.context()
        async def ready():
            return context
        with patch("services.chat_service._load_tts_service", return_value=SimpleNamespace(is_enabled=lambda: False)), \
                patch("services.expression_compiler.compile_expression_plan", side_effect=ValueError("bad expression")):
            await synthesize_and_send_voice(None, "測試", 1, "t1", send,
                                            expression_context=asyncio.create_task(ready()))
        send.assert_awaited_once_with({"type": "voice_unavailable", "reason": "disabled",
                                     "turn_id": "t1", "speech_unavailable": True})

    async def test_slow_reaction_task_cannot_hold_prepared_voice(self):
        service = SimpleNamespace(is_enabled=lambda: True, synthesize=AsyncMock(return_value={
            "audio_base64": "prepared", "duration_ms": 11000, "format": "wav",
        }))
        context = asyncio.create_task(asyncio.Event().wait())
        send = AsyncMock()
        try:
            with patch("services.chat_service._load_tts_service", return_value=service), \
                    patch("services.chat_service._SPEECH_PLAN_TIMEOUT_SEC", 0.01):
                await asyncio.wait_for(synthesize_and_send_voice(
                    None, "測試", 1, "t1", send, expression_context=context,
                ), timeout=0.5)
            send.assert_awaited_once_with({"type": "voice", "audio": "prepared", "durationMs": 11000,
                                         "format": "wav", "turn_id": "t1", "speech_unavailable": True})
            self.assertFalse(context.cancelled())
        finally:
            context.cancel()
            await asyncio.gather(context, return_exceptions=True)

    async def test_late_reaction_ack_cannot_adopt_speech_carry(self):
        class Socket(FakeWebSocket):
            async def send_json(self, payload):
                self.payloads.append(payload)
                if payload.get("type") in {"voice_unavailable", "error"}:
                    self._turn_finished.set()

        socket = Socket([
            {"content": "第一句", "turn_id": "t1"},
            {"type": "action_state", "turn_id": "t1", "stage": "speech", "status": "started", "action_id": "s1"},
            {"type": "action_state", "turn_id": "t1", "stage": "reaction", "status": "started", "action_id": "r1"},
            {"content": "第二句", "turn_id": "t2"},
        ])
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=SimpleNamespace(accept=AsyncMock(side_effect=[uuid4(), uuid4()]),
                                           retrieve=AsyncMock(return_value=({}, "")), route_background=Mock()),
            chat_session_service=make_chat_session_service(),
        ))
        contexts = []
        async def jev(context, questions):
            contexts.append(context)
            return jev_answers()
        async def chat(messages, send):
            await send("我懂，我們慢慢來。")
            return "我懂，我們慢慢來。"
        def compile_speech(*args, **kwargs):
            plan = compile_expression_plan(*args, **kwargs)
            plan["carryState"]["test_stage"] = "speech"
            return plan
        with patch("api.routes.chat_ws.call_jev", side_effect=jev), \
                patch("api.routes.chat_ws.stream_agent_a", side_effect=chat), \
                patch("api.routes.chat_ws.broadcast_to_displays"), patch("api.routes.chat_ws.log_turn"), \
                patch("services.chat_service._load_tts_service", return_value=SimpleNamespace(is_enabled=lambda: False)), \
                patch("services.expression_compiler.compile_expression_plan", side_effect=compile_speech):
            await asyncio.wait_for(websocket_endpoint(socket), timeout=3)
        self.assertEqual(len(contexts), 2)  # One JEV per turn, no speech model call.
        self.assertEqual(contexts[1]["previous_expression_carry_state"]["test_stage"], "speech")
        for turn_id in ("t1", "t2"):
            plans = [item for item in socket.payloads if item.get("turn_id") == turn_id
                     and item["type"] == "expression_plan"]
            self.assertEqual([item["stage"] for item in plans], ["reaction", "speech"])
        self.assertTrue(all(item["speech_expected"] for item in socket.payloads if item["type"] == "stream_end"))

    async def test_arc_raw_choice_survives_confidence_fallback(self):
        from api.routes.chat_ws import _produce_and_send_action_plan
        answers = jev_answers()
        answers["arc"] = {"choice": "widen_then_tease", "confidence": 0.44,
                          "probabilities": {"widen_then_tease": 0.54, "steady": 0.16}}
        send = AsyncMock()
        with patch("api.routes.chat_ws.broadcast_to_displays"):
            result = await _produce_and_send_action_plan(None, "Rushia", answers, None, 0, "hash", "t1", send_func=send)
        debug = result["plan"]["debug"]
        self.assertEqual(debug["jevArcChoice"], "widen_then_tease")
        self.assertEqual(debug["jevArcConfidence"], 0.44)
        self.assertEqual(debug["jevArcProbability_widen_then_tease"], 0.54)
        self.assertEqual(debug["jevArcFallbackReason"], "low_confidence")
        self.assertEqual(debug["arc"], "steady")
