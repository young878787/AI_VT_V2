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

from api.routes.chat_ws import websocket_endpoint
from domain.agent_a_prompts import build_agent_a_prompt
from domain.jev_questions import JEV_DECISION_CRITERIA_VERSION, JEV_ROLES, build_role_questions, question_hash
from domain.emotion_state import (
    CHARACTER_EXPRESSION_PROFILE,
    EMOTION_FIELDS,
    NEUTRAL_EMOTION_STATE,
    PERSONALITY,
)
from domain.memory_source import MemoryEventConflict, MemoryEventReplay
from backend.tests.chat_session_fakes import FakeChatSessionRepository, make_chat_session_service
from services.chat_session_service import ChatSessionService


def emotion_answers(score=0.6):
    return {field: {"type": "noul", "noul": score} for field in EMOTION_FIELDS}


def action_answers():
    return {
        "base_emotion": {"type": "choice", "choice": "shy", "confidence": 0.9},
        "interaction_attitude": {"type": "choice", "choice": "awkward", "confidence": 0.9},
        "arc": {"type": "choice", "choice": "steady", "confidence": 0.9},
        "intensity": {"type": "score", "score": 2.4, "confidence": 0.9},
        "energy": {"type": "score", "score": 2, "confidence": 0.9},
        "wants_goofy": {"type": "noul", "noul": 0.1},
        "needs_special_blink": {"type": "noul", "noul": 0.2},
    }


def jev_answers(score=0.6):
    return {**emotion_answers(score), **action_answers()}


def jev_role(questions):
    return next(role for role, role_questions in build_role_questions().items() if set(role_questions) == set(questions))


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = [json.dumps(frame) for frame in frames]
        self.payloads = []
        self._turn_finished = asyncio.Event()
        self._wait_for_turn = False

    async def accept(self):
        pass

    async def receive_text(self):
        if self._wait_for_turn:
            await self._turn_finished.wait()
            self._turn_finished.clear()
            self._wait_for_turn = False
        if self.frames:
            frame = self.frames.pop(0)
            if "\"content\"" in frame:
                self._wait_for_turn = True
            return frame
        raise WebSocketDisconnect()

    async def send_json(self, payload):
        self.payloads.append(payload)
        if payload.get("type") in {"stream_end", "error"} or payload.get("code") in {
            "turn_id_conflict", "turn_id_replayed",
        }:
            self._turn_finished.set()


class EmotionWebSocketTests(unittest.TestCase):
    def _run(self, frames, jev_responses, persistence=False, storage=None, chat_sessions=None, chat=None,
             jev_hook=None):
        socket = FakeWebSocket(frames)
        runtime = SimpleNamespace(
            retrieve=AsyncMock(return_value=({}, "")),
            accept=AsyncMock(return_value=uuid4()),
            route_background=Mock(),
            reset=AsyncMock(),
        )
        chat_sessions = chat_sessions or make_chat_session_service()
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime, chat_session_service=chat_sessions,
        ))
        captured = {"jev_states": [], "jev_calls": [], "chat_states": [], "prompts": [], "contexts": [],
                    "traces": [], "runtime": runtime, "chat_sessions": chat_sessions}
        # 每輪一份合併答案；三角色各自依序取用並只回傳自己題目的欄位。
        responses = {role: iter(jev_responses) for role in JEV_ROLES}

        async def fake_call_jev(state, questions):
            role = jev_role(questions)
            captured["jev_calls"].append((role, state))
            if role == "emotion":
                captured["jev_states"].append(state)
            answers = next(responses[role])
            if jev_hook is not None:
                await jev_hook(role, state)
            return None if answers is None else {key: answers[key] for key in questions if key in answers}

        def fake_prompt(profile, notes, state, model_name, **kwargs):
            captured["chat_states"].append(state)
            prompt = build_agent_a_prompt(profile, notes, state, model_name, **kwargs)
            captured["prompts"].append(prompt)
            return prompt

        async def fake_chat(messages, send_chunk, **kwargs):
            captured["contexts"].append(messages)
            await asyncio.sleep(0)
            await send_chunk("露西亞的回覆")
            return "露西亞的回覆"

        async def run():
            with patch("api.routes.chat_ws.call_jev", side_effect=fake_call_jev), \
                patch("api.routes.chat_ws.stream_agent_a", side_effect=chat or fake_chat), \
                patch("api.routes.chat_ws.build_agent_a_prompt", side_effect=fake_prompt), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.reset_log"), \
                patch("api.routes.chat_ws.trace", side_effect=lambda stage, data: captured["traces"].append(
                    {"stage": stage, **data})), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"):
                await websocket_endpoint(socket)

        asyncio.run(run())
        return socket, captured

    def _run_interrupt(self, memory_hook):
        """第一句在 Chat 串流中被第二句打斷；第一句記憶角色的行為由 memory_hook 控制。"""
        first_event, second_event = uuid4(), uuid4()
        partial_sent = asyncio.Event()
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
                    await partial_sent.wait()
                    return json.dumps({"content": "插話", "turn_id": "turn_2"})
                await completed.wait()
                raise WebSocketDisconnect()

            async def send_json(self, payload):
                payloads.append(payload)
                if payload.get("type") == "stream_end" and payload.get("turn_id") == "turn_2":
                    completed.set()

        async def fake_jev(state, questions):
            if jev_role(questions) == "memory" and state["current_user_input"] == "第一句":
                await memory_hook()
            return {key: value for key, value in jev_answers().items() if key in questions}

        async def fake_chat(messages, send_chunk, **kwargs):
            if messages[-1]["content"] == "第一句":
                await send_chunk("半句")
                partial_sent.set()
                await asyncio.Event().wait()
            await send_chunk("完整回覆")
            return "完整回覆"

        runtime = SimpleNamespace(
            retrieve=AsyncMock(return_value=({}, "")),
            accept=AsyncMock(side_effect=[first_event, second_event]),
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
        routes = [call for call in runtime.route_background.call_args_list if call.args[0] == first_event]
        return payloads, routes

    def test_runtime_snapshots_are_rebuilt_and_do_not_become_memory_sources(self):
        from services.context_tools import ContextTools
        from domain.runtime_context import make_turn_snapshot, result
        tools = ContextTools()
        tools.snapshot = AsyncMock(side_effect=[make_turn_snapshot(str(index), index, result("ok", {
            "foreground": {"status": "ok", "app": app},
            "open_apps": {"status": "ok", "apps": [app], "truncated": False},
        })) for index, app in enumerate(("first.exe", "second.exe"))])
        with patch("api.routes.chat_ws.ContextTools", return_value=tools):
            _, captured = self._run([{"content": "你好"}, {"content": "再聊一下"}], [jev_answers(), jev_answers()])
        self.assertEqual(tools.snapshot.await_count, 2)
        self.assertIn("first.exe", captured["contexts"][0][-2]["content"])
        self.assertIn("second.exe", captured["contexts"][1][-2]["content"])
        self.assertNotIn("first.exe", json.dumps(captured["contexts"][1], ensure_ascii=False))
        memory_args = str(captured["runtime"].accept.await_args_list)
        self.assertNotIn("first.exe", memory_args)
        self.assertNotIn("second.exe", memory_args)
        self.assertNotIn("first.exe", str(captured["jev_states"]))
        self.assertNotIn("second.exe", str(captured["jev_states"]))
        self.assertNotIn("first.exe", captured["prompts"][0])

    def test_three_role_jev_calls_precede_chat_and_drive_both_axes(self):
        answers = jev_answers(0.8)
        answers["base_emotion"]["probabilities"] = {"shy": 0.75, "neutral": 0.2, "happy": 0.05}
        answers["interaction_attitude"]["probabilities"] = {"awkward": 0.8, "smile": 0.2}
        socket, captured = self._run(
            [{"content": "妳今天好可愛"}], [answers],
        )
        # Connection-scoped ambient updates may interleave independently of this turn.
        types = [payload["type"] for payload in socket.payloads if payload["type"] != "ambient_plan"]
        self.assertEqual(types[:5], ["session_ready", "context_status", "context_status", "input_accepted", "emotion_update"])
        self.assertEqual([payload["status"] for payload in socket.payloads if payload["type"] == "context_status"],
                         ["reading", "unavailable"])
        self.assertEqual(types.count("expression_plan"), 1)
        self.assertNotIn("behavior", types)
        self.assertNotIn("blink_control", types)
        self.assertEqual(types.count("stream_end"), 1)
        self.assertNotIn("jpaf_update", types)
        states = dict(captured["jev_calls"])
        self.assertEqual(sorted(role for role, _ in captured["jev_calls"]), sorted(JEV_ROLES))
        self.assertEqual(states["emotion"]["character_expression_profile"], CHARACTER_EXPRESSION_PROFILE)
        self.assertNotIn("interaction_personality", states["emotion"])
        self.assertEqual(states["action"]["interaction_personality"], PERSONALITY)
        self.assertNotIn("character_expression_profile", states["action"])
        self.assertEqual(set(states["memory"]), {"current_user_input", "recent_dialogue"})
        self.assertNotIn("memory", str(states["emotion"]))
        self.assertIn("shy: 0.80", captured["prompts"][0])
        self.assertIn("只輸出使用者會聽見的純文字", captured["prompts"][0])
        plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
        self.assertEqual(plan["debug"]["jevBaseEmotionChoice"], "shy")
        self.assertEqual(plan["debug"]["jevInteractionAttitudeChoice"], "awkward")
        self.assertEqual(plan["debug"]["jevResolvedEmotion"], "shy")
        self.assertEqual(plan["debug"]["jevResolvedAttitude"], "awkward")
        self.assertEqual(plan["debug"]["jevBaseEmotionConfidence"], 0.9)
        self.assertEqual(plan["debug"]["jevBaseEmotionProbability_happy"], 0.05)
        self.assertEqual(plan["debug"]["jevInteractionAttitudeProbability_smile"], 0.2)
        self.assertEqual(plan["debug"]["jevDecisionSource"], "jev")
        self.assertEqual(plan["debug"]["jevDecisionHistoryMessages"], 0)
        self.assertEqual(len(plan["debug"]["jevDecisionQuestionHash"]), 12)
        self.assertEqual(types[-1], "stream_end")

    def test_each_role_trace_records_its_own_questions_and_input(self):
        _, captured = self._run([{"content": "嗨"}], [jev_answers()])
        traces = {item["role"]: item for item in captured["traces"] if item["stage"] == "jev"}
        self.assertEqual(set(traces), JEV_ROLES)
        for role, questions in build_role_questions().items():
            self.assertEqual(traces[role]["question_keys"], list(questions))
            self.assertEqual(traces[role]["question_hash"], question_hash(questions))
            self.assertEqual(traces[role]["criteria_version"], JEV_DECISION_CRITERIA_VERSION)
            self.assertIsNone(traces[role]["error"])
            self.assertGreaterEqual(traces[role]["duration_sec"], 0)
        for role, state in captured["jev_calls"]:
            self.assertIs(traces[role]["state"], state)
        for role, questions in build_role_questions().items():
            self.assertEqual(traces[role]["answers"],
                             {key: value for key, value in jev_answers().items() if key in questions})
        self.assertEqual(set(traces["emotion"]["answers"]), set(EMOTION_FIELDS))
        self.assertIn("policy", traces["memory"])
        self.assertIn("policy_version", traces["memory"])
        self.assertNotIn("policy", traces["emotion"])

    def test_chat_streams_while_action_and_memory_roles_are_pending(self):
        pending = set()
        both_pending = asyncio.Event()
        release = asyncio.Event()
        observed = []

        async def hook(role, state):
            if role == "emotion":
                # 情緒角色要等另外兩個呼叫都已開始才完成，證明三者同時在途。
                await asyncio.wait_for(both_pending.wait(), 1)
                return
            pending.add(role)
            if len(pending) == 2:
                both_pending.set()
            await release.wait()

        async def chat(messages, send_chunk, **kwargs):
            await asyncio.wait_for(both_pending.wait(), 1)
            observed.append(release.is_set())
            await send_chunk("先回覆")
            release.set()
            return "先回覆"

        socket, captured = self._run([{"content": "嗨"}], [jev_answers()], chat=chat, jev_hook=hook)
        self.assertEqual(observed, [False])
        types = [payload["type"] for payload in socket.payloads]
        self.assertLess(types.index("text_stream"), types.index("expression_plan"))
        captured["runtime"].route_background.assert_called_once()

    def test_role_failures_are_isolated(self):
        for failed in sorted(JEV_ROLES):
            with self.subTest(failed=failed):
                async def hook(role, state):
                    if role == failed:
                        raise TimeoutError("jev timeout")

                socket, captured = self._run([{"content": "嗨"}], [jev_answers(0.8)], jev_hook=hook)
                update = next(item for item in socket.payloads if item["type"] == "emotion_update")
                plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
                self.assertEqual(update["source"], "neutral_fallback" if failed == "emotion" else "jev")
                self.assertEqual(plan["debug"]["jevDecisionSource"], "fallback" if failed == "action" else "jev")
                route_answers = captured["runtime"].route_background.call_args.args[2]
                self.assertEqual(route_answers is None, failed == "memory")
                self.assertIn("stream_end", [item["type"] for item in socket.payloads])
                errors = {item["role"]: item["error"] for item in captured["traces"] if item["stage"] == "jev"}
                self.assertEqual(errors, {role: "jev_call_failed" if role == failed else None for role in JEV_ROLES})

    def test_cancel_while_memory_role_is_pending_routes_once_without_answers(self):
        payloads, routes = self._run_interrupt(lambda: asyncio.Event().wait())
        self.assertIn("turn_1", [item["turn_id"] for item in payloads if item["type"] == "turn_cancelled"])
        self.assertEqual(len(routes), 1)
        self.assertIsNone(routes[0].args[2])

    def test_cancel_after_memory_role_routed_does_not_route_again(self):
        async def answer_immediately():
            pass

        payloads, routes = self._run_interrupt(answer_immediately)
        self.assertIn("turn_1", [item["turn_id"] for item in payloads if item["type"] == "turn_cancelled"])
        self.assertEqual(len(routes), 1)
        self.assertIsNotNone(routes[0].args[2])

    def test_stream_end_reports_chat_performance_metrics(self):
        socket, _ = self._run([{"content": "測試效能"}], [jev_answers()])
        stream_end = next(item for item in socket.payloads if item["type"] == "stream_end")
        metrics = stream_end["metrics"]
        self.assertGreaterEqual(metrics["first_token_latency_ms"], 0)
        self.assertGreaterEqual(metrics["generation_ms"], 0)
        self.assertGreater(metrics["output_tokens"], 0)
        self.assertGreater(metrics["tokens_per_second"], 0)

    def test_persistence_failure_stops_session_before_next_turn(self):
        class FailingRepository(FakeChatSessionRepository):
            async def append_user(self, session_id, generation, message):
                raise RuntimeError("database unavailable")

        service = ChatSessionService(FailingRepository())
        socket, captured = self._run(
            [{"content": "第一句", "turn_id": "turn_1"},
             {"content": "第二句", "turn_id": "turn_2"}],
            [jev_answers()], chat_sessions=service,
        )
        self.assertEqual(len(captured["jev_states"]), 0)
        self.assertNotIn("stream_end", [item["type"] for item in socket.payloads])
        self.assertIn("session_persistence_failed", [item.get("code") for item in socket.payloads])

    def test_rest_followup_reset_session_does_not_reset_long_term_memory_again(self):
        socket, captured = self._run([{"type": "reset_session", "session_id": "session_a"}], [])
        captured["runtime"].reset.assert_not_awaited()
        self.assertEqual(
            [payload["type"] for payload in socket.payloads if payload["type"] != "ambient_plan"],
            ["session_ready", "emotion_update", "reset_done"],
        )

    def test_reset_session_clears_current_websocket_closure_state(self):
        _, captured = self._run(
            [
                {"content": "第一句", "session_id": "session_a", "turn_id": "turn_1"},
                {"type": "reset_session", "session_id": "session_a"},
                {"content": "第二句", "session_id": "session_a", "turn_id": "turn_2"},
            ],
            [jev_answers(0.8), jev_answers(0.2)],
        )
        self.assertEqual(len(captured["jev_states"]), 2)
        self.assertEqual(captured["jev_states"][1]["recent_dialogue"], [])
        self.assertNotIn("previous_emotion_state", captured["jev_states"][1])
        captured["runtime"].reset.assert_not_awaited()

    def test_legacy_websocket_reset_keeps_owner_reset_semantics(self):
        _, captured = self._run([{"type": "reset", "session_id": "session_a"}], [])
        captured["runtime"].reset.assert_awaited_once_with()

    def test_same_turn_id_replay_and_conflict_are_rejected_without_second_turn(self):
        for repeated, expected in (("第一句", "turn_id_replayed"), ("不同內容", "turn_id_conflict")):
            with self.subTest(expected=expected):
                socket, captured = self._run(
                    [
                        {"content": "第一句", "session_id": "session_a", "turn_id": "turn_1"},
                        {"content": repeated, "session_id": "session_a", "turn_id": "turn_1"},
                    ],
                    [jev_answers(0.8)],
                )
                self.assertEqual(len(captured["jev_states"]), 1)
                error = next(payload for payload in socket.payloads if payload.get("code") == expected)
                self.assertEqual(error["turn_id"], "turn_1")

    def test_database_replay_and_conflict_stop_before_jev(self):
        for error_type, expected in (
            (MemoryEventReplay, "turn_id_replayed"),
            (MemoryEventConflict, "turn_id_conflict"),
        ):
            with self.subTest(expected=expected):
                socket = FakeWebSocket([
                    {"content": "第一句", "session_id": "session_a", "turn_id": "turn_1"},
                ])
                runtime = SimpleNamespace(
                    retrieve=AsyncMock(return_value=({}, "")),
                    accept=AsyncMock(side_effect=error_type("duplicate")),
                    route_background=Mock(),
                    reset=AsyncMock(),
                )
                socket.app = SimpleNamespace(state=SimpleNamespace(
                    memory_runtime=runtime, chat_session_service=make_chat_session_service(),
                ))

                async def run():
                    with patch("api.routes.chat_ws.call_jev", new=AsyncMock()) as jev, \
                            patch("api.routes.chat_ws.synthesize_and_send_voice"):
                        await websocket_endpoint(socket)
                    jev.assert_not_awaited()

                asyncio.run(run())
                self.assertIn(expected, [payload.get("code") for payload in socket.payloads])

    def test_database_runtime_routes_without_file_memory_calls(self):
        socket = FakeWebSocket([{"type": "chat", "content": "請記住我喜歡茶", "session_id": "test-session",
                                 "turn_id": "test-turn"}])
        event_id = uuid4()
        runtime = SimpleNamespace(
            retrieve=AsyncMock(return_value=({}, "")),
            accept=AsyncMock(return_value=event_id),
            route_background=Mock(),
        )
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime, chat_session_service=make_chat_session_service(),
        ))

        async def fake_chat(messages, send_chunk, **kwargs):
            await send_chunk("知道了")
            return "知道了"

        async def run():
            with patch("api.routes.chat_ws.call_jev", new=AsyncMock(return_value=jev_answers())), \
                 patch("api.routes.chat_ws.stream_agent_a", side_effect=fake_chat), \
                 patch("api.routes.chat_ws.broadcast_to_displays"), \
                 patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                 patch("api.routes.chat_ws.log_turn"):
                await websocket_endpoint(socket)

        asyncio.run(run())
        runtime.retrieve.assert_awaited_once_with("請記住我喜歡茶", event_id=event_id, recent_dialogue=[], summary="")
        accept_args = runtime.accept.await_args.args
        self.assertEqual(accept_args[:4], ("server_session", "test-turn", "請記住我喜歡茶", []))
        self.assertEqual(accept_args[4]["content"], "請記住我喜歡茶")
        runtime.route_background.assert_called_once()
        self.assertEqual(runtime.route_background.call_args.args[0], event_id)
        accepted = next(item for item in socket.payloads if item["type"] == "input_accepted")
        self.assertEqual(accepted["event_id"], event_id.hex)

    def test_emotion_failure_uses_previous_state_without_partial_merge(self):
        invalid = jev_answers(0.1)
        invalid.pop("shy")
        socket, captured = self._run(
            [{"content": "第一句"}, {"content": "第二句"}],
            [jev_answers(0.8), invalid],
        )
        updates = [item for item in socket.payloads if item["type"] == "emotion_update"]
        self.assertEqual([item["source"] for item in updates], ["jev", "previous_fallback"])
        self.assertEqual(updates[0]["state"], updates[1]["state"])
        self.assertEqual(captured["jev_states"][1]["previous_emotion_state"], updates[0]["state"])
        self.assertEqual(len(captured["jev_states"][1]["recent_dialogue"]), 2)
        plans = [item for item in socket.payloads if item["type"] == "expression_plan"]
        self.assertEqual(plans[-1]["debug"]["jevDecisionSource"], "jev")

    def test_action_failure_uses_neutral_plan_and_chat_still_finishes(self):
        socket, _ = self._run([{"content": "嗨"}], [None])
        updates = [item for item in socket.payloads if item["type"] == "emotion_update"]
        self.assertEqual(updates[0]["source"], "neutral_fallback")
        self.assertEqual(updates[0]["state"], NEUTRAL_EMOTION_STATE)
        plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
        self.assertEqual(plan["carryState"]["emotion"], "neutral")
        self.assertEqual(plan["debug"]["jevDecisionSource"], "fallback")
        self.assertEqual(plan["debug"]["jevBaseEmotionFallbackReason"], "missing_answer")
        self.assertEqual(plan["debug"]["jevInteractionAttitudeFallbackReason"], "missing_answer")
        self.assertIn("text_stream", [item["type"] for item in socket.payloads])

    def test_low_confidence_base_keeps_valid_attitude(self):
        answers = jev_answers(0.2)
        answers["base_emotion"]["confidence"] = 0.4
        socket, _ = self._run([{"content": "嗨"}], [answers])
        plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
        self.assertEqual(plan["debug"]["jevBaseEmotionChoice"], "shy")
        self.assertEqual(plan["debug"]["jevBaseEmotionFallbackReason"], "low_confidence")
        self.assertEqual(plan["debug"]["jevInteractionAttitudeFallbackReason"], "none")
        self.assertEqual(plan["debug"]["jevDecisionSource"], "partial_fallback")
        self.assertEqual(plan["debug"]["jevResolvedEmotion"], "neutral")
        self.assertEqual(plan["debug"]["jevResolvedAttitude"], "awkward")

    def test_low_confidence_attitude_keeps_valid_base(self):
        answers = jev_answers(0.8)
        answers["base_emotion"]["choice"] = "happy"
        answers["interaction_attitude"]["confidence"] = 0.4
        socket, _ = self._run([{"content": "今天聊得很開心"}], [answers])
        plan = next(item for item in socket.payloads if item["type"] == "expression_plan")
        self.assertEqual(plan["debug"]["jevBaseEmotionFallbackReason"], "none")
        self.assertEqual(plan["debug"]["jevInteractionAttitudeFallbackReason"], "low_confidence")
        self.assertEqual(plan["debug"]["jevResolvedEmotion"], "happy")
        self.assertEqual(plan["debug"]["jevResolvedAttitude"], "smile")

    def test_persisted_session_is_restored_and_isolated(self):
        chat_sessions = make_chat_session_service()
        first, _ = self._run(
            [{"content": "hi", "session_id": "client_a"}],
            [jev_answers(0.8)], chat_sessions=chat_sessions,
        )
        first_state = next(item["state"] for item in first.payloads if item["type"] == "emotion_update")
        second, captured = self._run(
            [{"type": "sync", "session_id": "client_b"},
             {"content": "back", "session_id": "client_b"}],
            [None], chat_sessions=chat_sessions,
        )
        updates = [item for item in second.payloads if item["type"] == "emotion_update"]
        self.assertEqual(updates[0]["state"], first_state)
        self.assertEqual(updates[1]["source"], "previous_fallback")
        self.assertEqual(captured["jev_states"][0]["previous_emotion_state"], first_state)
        ready = next(item for item in second.payloads if item["type"] == "session_ready")
        self.assertEqual(ready["session_id"], "server_session")


if __name__ == "__main__":
    unittest.main()
