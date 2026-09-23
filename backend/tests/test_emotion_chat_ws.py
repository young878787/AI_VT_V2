import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.chat_ws import websocket_endpoint
from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import EMOTION_FIELDS, NEUTRAL_EMOTION_STATE


def emotion_answers(score=0.6):
    return {field: {"type": "noul", "noul": score} for field in EMOTION_FIELDS}


def action_answers():
    return {
        "emotion": {"type": "choice", "choice": "shy", "confidence": 0.9},
        "secondary_emotion": {"type": "choice", "choice": "none", "confidence": 0.9},
        "performance_mode": {"type": "choice", "choice": "awkward", "confidence": 0.9},
        "arc": {"type": "choice", "choice": "steady", "confidence": 0.9},
        "intensity": {"type": "score", "score": 2.4, "confidence": 0.9},
        "energy": {"type": "score", "score": 2, "confidence": 0.9},
        "wants_goofy": {"type": "noul", "noul": 0.1},
        "needs_special_blink": {"type": "noul", "noul": 0.2},
    }


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = [json.dumps(frame) for frame in frames]
        self.payloads = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self.frames:
            return self.frames.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, payload):
        self.payloads.append(payload)


class EmotionWebSocketTests(unittest.TestCase):
    def _run(self, frames, jev_responses, persistence=False, storage=None):
        socket = FakeWebSocket(frames)
        captured = {"jev_states": [], "chat_states": [], "prompts": []}
        responses = iter(jev_responses)

        async def fake_call_jev(state, questions):
            captured["jev_states"].append(state)
            return next(responses)

        def fake_prompt(profile, notes, state, model_name):
            captured["chat_states"].append(state)
            prompt = build_agent_a_prompt(profile, notes, state, model_name)
            captured["prompts"].append(prompt)
            return prompt

        async def fake_chat(messages):
            await asyncio.sleep(0)
            return "露西亞的回覆"

        async def fake_memory(messages, model_name):
            return SimpleNamespace(choices=[])

        async def run():
            with patch("api.routes.chat_ws.call_jev", side_effect=fake_call_jev), \
                patch("api.routes.chat_ws.collect_agent_a", side_effect=fake_chat), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=fake_memory), \
                patch("api.routes.chat_ws.build_agent_a_prompt", side_effect=fake_prompt), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=""), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", persistence), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                if storage is None:
                    await websocket_endpoint(socket)
                else:
                    with patch("infrastructure.memory_store.EMOTION_STATE_DIR", storage + "/emotions"), \
                        patch("infrastructure.memory_store.CHAT_SESSION_DIR", storage + "/sessions"):
                        await websocket_endpoint(socket)

        asyncio.run(run())
        return socket, captured

    def test_jev_emotion_precedes_chat_and_action_and_is_shared(self):
        socket, captured = self._run(
            [{"content": "妳今天好可愛"}], [emotion_answers(0.8), action_answers()],
        )
        types = [payload["type"] for payload in socket.payloads]
        self.assertEqual(types[0], "emotion_update")
        self.assertEqual(types.count("expression_plan"), 1)
        self.assertEqual(types.count("stream_end"), 1)
        self.assertNotIn("jpaf_update", types)
        self.assertIs(captured["chat_states"][0], captured["jev_states"][1]["current_emotion_state"])
        self.assertEqual(captured["jev_states"][0]["personality"]["name"], "露西亞")
        self.assertNotIn("memory", str(captured["jev_states"][0]))
        self.assertIn("shy: 0.80", captured["prompts"][0])
        self.assertIn("只輸出使用者會聽見的純文字", captured["prompts"][0])
        self.assertEqual(socket.payloads[-1]["type"], "stream_end")

    def test_emotion_failure_uses_previous_state_without_partial_merge(self):
        invalid = emotion_answers(0.1)
        invalid.pop("shy")
        socket, captured = self._run(
            [{"content": "第一句"}, {"content": "第二句"}],
            [emotion_answers(0.8), action_answers(), invalid, action_answers()],
        )
        updates = [item for item in socket.payloads if item["type"] == "emotion_update"]
        self.assertEqual([item["source"] for item in updates], ["jev", "previous_fallback"])
        self.assertEqual(updates[0]["state"], updates[1]["state"])
        self.assertEqual(captured["jev_states"][2]["previous_emotion_state"], updates[0]["state"])
        self.assertEqual(len(captured["jev_states"][2]["recent_dialogue"]), 2)

    def test_action_failure_uses_neutral_plan_and_chat_still_finishes(self):
        socket, _ = self._run([{"content": "嗨"}], [None, None])
        updates = [item for item in socket.payloads if item["type"] == "emotion_update"]
        self.assertEqual(updates[0]["source"], "neutral_fallback")
        self.assertEqual(updates[0]["state"], NEUTRAL_EMOTION_STATE)
        plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
        self.assertEqual(plan["carryState"]["emotion"], "neutral")
        self.assertIn("text_stream", [item["type"] for item in socket.payloads])

    def test_persisted_session_is_restored_and_isolated(self):
        with tempfile.TemporaryDirectory() as storage:
            first, _ = self._run(
                [{"content": "hi", "session_id": "session_a"}],
                [emotion_answers(0.8), action_answers()], True, storage,
            )
            first_state = next(item["state"] for item in first.payloads if item["type"] == "emotion_update")
            second, captured = self._run(
                [{"type": "sync", "session_id": "session_a"},
                 {"content": "back", "session_id": "session_a"},
                 {"type": "sync", "session_id": "session_b"}],
                [None, action_answers()], True, storage,
            )
            updates = [item for item in second.payloads if item["type"] == "emotion_update"]
            self.assertEqual(updates[0]["state"], first_state)
            self.assertEqual(updates[1]["source"], "previous_fallback")
            self.assertEqual(updates[2]["state"], NEUTRAL_EMOTION_STATE)
            self.assertEqual(captured["jev_states"][0]["previous_emotion_state"], first_state)


if __name__ == "__main__":
    unittest.main()
