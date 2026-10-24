import asyncio
import json
import math
import pathlib
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.avatar_runtime import AvatarRuntime, TARGET_FIELDS
from api.routes.chat_ws import websocket_endpoint
from backend.tests.chat_session_fakes import make_chat_session_service
from backend.tests.test_emotion_chat_ws import jev_answers


class AvatarTargetTests(unittest.TestCase):
    def test_glances_cover_eight_directions_with_visible_vertical_and_diagonal_motion(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(20261023))
        samples = {}
        order = []
        for _ in range(400):
            x, y = runtime._sample_gaze()
            direction = runtime._recent_gaze[-1]
            samples.setdefault(direction, []).append((x, y))
            order.append(direction)
            self.assertTrue(all(math.isfinite(value) and -1.0 <= value <= 1.0 for value in (x, y)))
            self.assertGreaterEqual(max(abs(x), abs(y)), 0.45)
            if direction in {"left", "right"}:
                self.assertTrue(0.45 <= abs(x) <= 0.75)
                self.assertLessEqual(abs(y), 0.04)
            elif direction in {"up", "down"}:
                self.assertLessEqual(abs(x), 0.04)
                self.assertTrue(0.90 <= y <= 1.0 if direction == "up" else -1.0 <= y <= -0.95)
            else:
                self.assertTrue(0.55 <= abs(x) <= 0.80)
                self.assertTrue(0.85 <= y <= 1.0 if direction.startswith("up_") else -1.0 <= y <= -0.85)
            if "left" in direction:
                self.assertLess(x, 0)
            elif "right" in direction:
                self.assertGreater(x, 0)
        self.assertEqual(set(samples), {"left", "right", "up", "down", "up_left", "up_right",
                                       "down_left", "down_right"})
        for direction, values in samples.items():
            with self.subTest(direction=direction):
                self.assertGreater(len(values), 20)
                self.assertGreater(len({round(x, 3) for x, _ in values}), 15)
                self.assertGreater(len({round(y, 3) for _, y in values}), 15)
        self.assertGreater(len(set(zip(order, order[1:]))), 40)
        self.assertTrue(any(left == right for left, right in zip(order, order[1:])))

    def test_gaze_repeat_weights_are_independent_of_body_history(self):
        first = AvatarRuntime(AsyncMock(), rng=random.Random(7))
        second = AvatarRuntime(AsyncMock(), rng=random.Random(7))
        first._recent.extend(["left", "left", "nearby", "pose"])
        second._recent.extend(["right", "right", "sway", "hop"])
        self.assertEqual([first._sample_gaze() for _ in range(30)],
                         [second._sample_gaze() for _ in range(30)])
        self.assertEqual(len(first._recent_gaze), 2)
        first._recent_gaze.clear()
        first._recent_gaze.extend(["up", "down"])
        with patch.object(first._rng, "choices", wraps=first._rng.choices) as choose:
            first._sample_gaze()
        weights = dict(zip(choose.call_args.args[0], choose.call_args.kwargs["weights"]))
        self.assertEqual(weights["up"], 0.3)
        self.assertEqual(weights["down"], 0.3)
        self.assertTrue(all(weight == 1.0 for name, weight in weights.items() if name not in {"up", "down"}))
        history = list(first._recent_gaze)
        first._gaze_front_remaining = 1
        forward, _ = first._next_plan()
        self.assertEqual((forward["target"]["gazeX"], forward["target"]["gazeY"]), (0, 0))
        self.assertEqual(list(first._recent_gaze), history)

    def test_idle_glances_have_bounded_varied_waits_visible_amplitude_and_short_holds(self):
        forward_holds, glance_holds, run_lengths, run_times = [], [], [], []
        forward_time, total_time = 0.0, 0.0
        for seed in range(100):
            runtime = AvatarRuntime(AsyncMock(), rng=random.Random(seed))
            first_glance, run_count, run_time = True, 0, 0.0
            for _ in range(100):
                plan, delay = runtime._next_plan()
                hold = delay - plan["moveMs"] / 1000
                total_time += delay
                x, y = plan["target"]["gazeX"], plan["target"]["gazeY"]
                if x == y == 0:
                    forward_holds.append(hold)
                    forward_time += delay
                    run_count += 1
                    run_time += delay
                    # Even slow poses cannot leave the eyes fixed for a minute.
                    self.assertLessEqual(run_time, (2 if first_glance else 4) * 7.2)
                else:
                    self.assertTrue(1 <= run_count <= 2 if first_glance else 2 <= run_count <= 4)
                    self.assertGreaterEqual(max(abs(x), abs(y)), 0.28)
                    if not first_glance:
                        run_lengths.append(run_count)
                        run_times.append(round(run_time, 2))
                    glance_holds.append(hold)
                    first_glance, run_count, run_time = False, 0, 0.0
        self.assertGreater(forward_time / total_time, 0.8)
        self.assertLess(forward_time / total_time, 0.93)
        self.assertEqual(set(run_lengths), {2, 3, 4})
        self.assertGreater(len(set(run_times)), 100)
        self.assertLess(max(glance_holds), 0.8)
        self.assertGreater(min(forward_holds), 1.0)
        self.assertLess(sum(glance_holds) / len(glance_holds),
                        sum(forward_holds) / len(forward_holds) * 0.25)

    def test_listening_does_not_consume_idle_gaze_countdown_and_reset_restarts_it(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(17))
        runtime._next_plan()
        runtime._sample_gaze()
        gaze_history = list(runtime._recent_gaze)
        remaining = runtime._gaze_front_remaining
        runtime.update_state({"type": "avatar_state", "listening": True})
        for _ in range(30):
            runtime._next_plan()
        self.assertEqual(runtime._gaze_front_remaining, remaining)
        self.assertEqual(list(runtime._recent_gaze), gaze_history)
        runtime.update_state({"type": "avatar_state", "listening": False})
        after_listening, _ = runtime._next_plan()
        self.assertEqual(max(abs(after_listening["target"]["gazeX"]),
                             abs(after_listening["target"]["gazeY"])) == 0, remaining > 0)
        runtime.reset()
        self.assertTrue(1 <= runtime._gaze_front_remaining <= 2)
        self.assertEqual(list(runtime._recent_gaze), [])
        after_reset, _ = runtime._next_plan()
        self.assertEqual((after_reset["target"]["gazeX"], after_reset["target"]["gazeY"]), (0, 0))

    def test_seeded_targets_are_bounded_and_have_varied_amplitudes_and_deadlines(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(42))
        plans = [runtime._next_plan() for _ in range(300)]
        self.assertEqual([plan["sequence"] for plan, _ in plans], list(range(1, 301)))
        for plan, delay in plans:
            self.assertEqual(plan["state"], "idle")
            self.assertNotIn("turn_id", plan)
            self.assertEqual(set(plan["target"]), set(TARGET_FIELDS))
            self.assertTrue(all(math.isfinite(value) and -1 <= value <= 1
                                for value in plan["target"].values()))
            self.assertTrue(300 <= plan["moveMs"] <= 3000)
            self.assertGreater(delay, plan["moveMs"] / 1000)
            self.assertTrue(3 <= plan["breath"]["periodSec"] <= 8)
            self.assertTrue(0 <= plan["breath"]["amplitude"] <= 1)
            self.assertTrue(0 <= plan["breath"]["rootAmplitude"] <= 0.08)
            if "hop" in plan:
                self.assertTrue(0 <= plan["hop"]["height"] <= 0.12)
                self.assertTrue(400 <= plan["hop"]["durationMs"] <= 1600)
        self.assertGreater(max(abs(plan["target"]["headZ"]) for plan, _ in plans), 0.7)
        self.assertTrue(any(abs(plan["target"]["headZ"]) < 0.1 for plan, _ in plans))
        self.assertTrue(any("hop" in plan for plan, _ in plans))
        self.assertGreater(len({plan["moveMs"] for plan, _ in plans}), 100)
        self.assertGreater(len({round(delay, 2) for _, delay in plans}), 100)

    def test_listening_keeps_some_lean_returns_gaze_to_front_and_stops_hops(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(7))
        runtime._target = dict.fromkeys(TARGET_FIELDS, 0.8)
        runtime.update_state({"type": "avatar_state", "listening": True})
        first, _ = runtime._next_plan()
        self.assertTrue(0.3 < first["target"]["headZ"] < 0.6)
        for _ in range(50):
            plan, _ = runtime._next_plan()
            self.assertEqual(plan["state"], "listening")
            self.assertEqual((plan["target"]["gazeX"], plan["target"]["gazeY"]), (0, 0))
            self.assertNotIn("hop", plan)
            self.assertLessEqual(plan["breath"]["rootAmplitude"], 0.01)

    def test_controls_reject_coercion_and_identity_without_partial_update(self):
        runtime = AvatarRuntime(AsyncMock())
        for invalid in (
            {"type": "avatar_state", "listening": 1},
            {"type": "avatar_state", "listening": "false"},
            {"type": "avatar_state", "listening": None},
            {"type": "avatar_state", "enabled": True},
            {"type": "avatar_state", "listening": True, "enabled": "false"},
            {"type": "avatar_state", "listening": True, "owner": "another"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                runtime.update_state(invalid)
            self.assertFalse(runtime._listening)
            self.assertTrue(runtime._enabled)

    def test_last_settle_style_is_retained_until_reset_without_changing_expression(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(9))
        quiet = {"type": "expression_plan", "idlePlan": {"settlePose": {
            "params": {"mouthForm": -0.7, "eyeBallX": 0.4},
            "bodyMotionProfile": {"style": "heavy_slow_sink", "speed": 0.62},
        }}}
        original = json.loads(json.dumps(quiet))
        runtime.adopt_expression_plan(quiet)
        self.assertEqual(runtime._activity, 0.5)
        self.assertEqual(quiet, original)
        runtime.update_state({"type": "avatar_state", "listening": True})
        runtime.adopt_expression_plan({"type": "expression_plan", "idlePlan": None})
        self.assertEqual(runtime._activity, 0.5)
        previous, _ = runtime._next_plan()
        runtime.reset()
        self.assertEqual(runtime._activity, 1.0)
        self.assertFalse(runtime._listening)
        self.assertEqual(list(runtime._recent), [])
        self.assertGreater(runtime._next_plan()[0]["sequence"], previous["sequence"])


class AvatarLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_keeps_disabled_until_explicit_reenable(self):
        output = asyncio.Queue()
        runtime = AvatarRuntime(output.put, rng=random.Random(3))
        runtime.update_state({"type": "avatar_state", "listening": True, "enabled": False})
        task = asyncio.create_task(runtime.run())
        try:
            stopped = await asyncio.wait_for(output.get(), 1)
            runtime.reset()
            reset = await asyncio.wait_for(output.get(), 0.5)
            self.assertGreater(reset["sequence"], stopped["sequence"])
            self.assertEqual(reset["breath"]["amplitude"], 0)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(output.get(), 0.04)
            runtime.update_state({"type": "avatar_state", "listening": True, "enabled": True})
            self.assertEqual((await asyncio.wait_for(output.get(), 0.5))["state"], "listening")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_immediate_plan_controls_interrupt_wait_and_disabled_is_quiet(self):
        output = asyncio.Queue()
        runtime = AvatarRuntime(output.put, rng=random.Random(42))
        task = asyncio.create_task(runtime.run())
        try:
            first = await asyncio.wait_for(output.get(), 1)
            self.assertEqual(first["state"], "idle")
            runtime.update_state({"type": "avatar_state", "listening": True})
            focused = await asyncio.wait_for(output.get(), 0.5)
            self.assertEqual(focused["state"], "listening")
            runtime.update_state({"type": "avatar_state", "listening": False, "enabled": False})
            stopped = await asyncio.wait_for(output.get(), 0.5)
            self.assertTrue(all(value == 0 for value in stopped["target"].values()))
            self.assertEqual(stopped["breath"]["rootAmplitude"], 0)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(output.get(), 0.04)
            runtime.update_state({"type": "avatar_state", "listening": False, "enabled": True})
            resumed = await asyncio.wait_for(output.get(), 0.5)
            self.assertGreater(resumed["sequence"], stopped["sequence"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_state_change_during_send_is_not_lost(self):
        output = asyncio.Queue()

        async def send(plan):
            await output.put(plan)
            if plan["sequence"] == 1:
                runtime.update_state({"type": "avatar_state", "listening": False, "enabled": False})

        runtime = AvatarRuntime(send, rng=random.Random(2))
        task = asyncio.create_task(runtime.run())
        try:
            await asyncio.wait_for(output.get(), 1)
            stopped = await asyncio.wait_for(output.get(), 0.5)
            self.assertEqual(stopped["breath"]["amplitude"], 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_send_failure_terminates_instead_of_retrying_a_dead_connection(self):
        send = AsyncMock(side_effect=RuntimeError("socket closed"))
        runtime = AvatarRuntime(send)
        with self.assertRaisesRegex(RuntimeError, "socket closed"):
            await runtime.run()
        self.assertEqual(send.await_count, 1)


class ControlledSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.output = asyncio.Queue()
        self.payloads = []
        self.memory = SimpleNamespace(
            accept=AsyncMock(return_value=uuid4()), retrieve=AsyncMock(return_value=({}, "")),
            route_background=Mock(), reset=AsyncMock(),
        )
        self.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=self.memory, chat_session_service=make_chat_session_service(),
        ))

    async def accept(self):
        pass

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect()
        return json.dumps(message)

    async def send_json(self, payload):
        self.payloads.append(payload)
        await self.output.put(payload)

    async def next_type(self, kind):
        async with asyncio.timeout(2):
            while True:
                payload = await self.output.get()
                if payload.get("type") == kind:
                    return payload


class AvatarWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_controls_do_not_restore_short_term_or_switch_test_session(self):
        socket = ControlledSocket()
        repository = socket.app.state.chat_session_service.repository
        with patch.dict("os.environ", {"AI_VT_TEST_MODE": "true"}), \
                patch.object(repository, "load_context", new=AsyncMock(wraps=repository.load_context)) as load, \
                patch("api.routes.chat_ws.call_jev", new_callable=AsyncMock) as jev:
            task = asyncio.create_task(websocket_endpoint(socket))
            try:
                initial = await socket.next_type("ambient_plan")
                await socket.incoming.put({"type": "sync", "test_mode": "memory_probe",
                                           "test_session_alias": "isolated_probe"})
                ready = await socket.next_type("session_ready")
                self.assertEqual(ready["session_id"], "isolated_probe")
                reset = await socket.next_type("ambient_plan")
                self.assertGreater(reset["sequence"], initial["sequence"])
                await socket.incoming.put({"type": "avatar_state", "listening": True, "enabled": True})
                self.assertEqual((await socket.next_type("ambient_plan"))["state"], "listening")
                self.assertEqual(repository.session_id, "isolated_probe")
                load.assert_not_awaited()
                jev.assert_not_awaited()
                socket.memory.accept.assert_not_awaited()
                socket.memory.retrieve.assert_not_awaited()
            finally:
                await socket.incoming.put(None)
                await task

    async def test_sent_expression_adopts_its_settle_style_and_reset_clears_it(self):
        socket = ControlledSocket()
        runtimes = []

        def create_runtime(send):
            runtime = AvatarRuntime(send, rng=random.Random(17))
            runtimes.append(runtime)
            return runtime

        async def chat(messages, send_chunk, **kwargs):
            await asyncio.sleep(0)
            await send_chunk("我陪著你。")
            return "我陪著你。"

        answers = jev_answers()
        answers["base_emotion"]["choice"] = "sad"
        with patch("api.routes.chat_ws.AvatarRuntime", side_effect=create_runtime), \
                patch("api.routes.chat_ws.call_jev", new=AsyncMock(return_value=answers)), \
                patch("api.routes.chat_ws.stream_agent_a", side_effect=chat), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.log_turn"), patch("api.routes.chat_ws.reset_log"):
            task = asyncio.create_task(websocket_endpoint(socket))
            try:
                await socket.next_type("ambient_plan")
                await socket.incoming.put({"content": "陪我一下", "turn_id": "sad-turn"})
                expression = await socket.next_type("expression_plan")
                self.assertEqual(expression["idlePlan"]["settlePose"]["bodyMotionProfile"]["style"],
                                 "heavy_slow_sink")
                self.assertEqual(runtimes[0]._activity, 0.5)
                await socket.next_type("stream_end")
                await socket.incoming.put({"type": "reset_session"})
                await socket.next_type("reset_done")
                self.assertEqual(runtimes[0]._activity, 1.0)
            finally:
                await socket.incoming.put(None)
                await task

    async def test_no_chat_controls_reset_connection_isolation_and_shutdown(self):
        sockets = [ControlledSocket(), ControlledSocket()]
        runtimes = []

        class TrackedRuntime(AvatarRuntime):
            def __init__(self, send):
                super().__init__(send, rng=random.Random(42))
                self.closed = asyncio.Event()
                runtimes.append(self)

            async def run(self):
                try:
                    await super().run()
                finally:
                    self.closed.set()

        with patch("api.routes.chat_ws.AvatarRuntime", TrackedRuntime), \
                patch("api.routes.chat_ws.call_jev", new_callable=AsyncMock) as jev, \
                patch("api.routes.chat_ws.reset_log"):
            tasks = [asyncio.create_task(websocket_endpoint(socket)) for socket in sockets]
            try:
                initial = [await socket.next_type("ambient_plan") for socket in sockets]
                self.assertEqual([plan["sequence"] for plan in initial], [1, 1])
                await sockets[0].incoming.put({"type": "avatar_state", "listening": True})
                focused = await sockets[0].next_type("ambient_plan")
                self.assertEqual(focused["state"], "listening")
                self.assertFalse(runtimes[1]._listening)
                await sockets[0].incoming.put({"type": "avatar_state", "listening": True, "owner": "other"})
                error = await sockets[0].next_type("error")
                self.assertEqual(error["code"], "invalid_avatar_state")
                await sockets[0].incoming.put({"type": "reset_session"})
                await sockets[0].next_type("reset_done")
                reset = await sockets[0].next_type("ambient_plan")
                self.assertEqual(reset["state"], "idle")
                self.assertGreater(reset["sequence"], focused["sequence"])
                jev.assert_not_awaited()
                for socket in sockets:
                    socket.memory.accept.assert_not_awaited()
                    socket.memory.retrieve.assert_not_awaited()
                    socket.memory.reset.assert_not_awaited()
            finally:
                for socket in sockets:
                    await socket.incoming.put(None)
                await asyncio.gather(*tasks)
            self.assertTrue(all(runtime.closed.is_set() for runtime in runtimes))

    async def test_cancelled_turn_does_not_stop_runtime(self):
        socket = ControlledSocket()
        jev_started = asyncio.Event()

        async def jev(context, questions):
            jev_started.set()
            await asyncio.Event().wait()

        with patch("api.routes.chat_ws.call_jev", side_effect=jev), \
                patch("api.routes.chat_ws.log_turn"):
            task = asyncio.create_task(websocket_endpoint(socket))
            try:
                await socket.next_type("ambient_plan")
                await socket.incoming.put({"content": "第一句", "turn_id": "first"})
                await asyncio.wait_for(jev_started.wait(), 2)
                await socket.incoming.put({"content": "第二句", "turn_id": "second"})
                cancelled = await socket.next_type("turn_cancelled")
                self.assertEqual(cancelled["turn_id"], "first")
                await socket.incoming.put({"type": "avatar_state", "listening": True})
                self.assertEqual((await socket.next_type("ambient_plan"))["state"], "listening")
            finally:
                await socket.incoming.put(None)
                await task


if __name__ == "__main__":
    unittest.main()
