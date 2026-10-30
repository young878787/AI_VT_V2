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

from services.expression.avatar_runtime import (
    AvatarRuntime, HUM_BEAT_OVERLAP, IDLE_ACTIONS, LIVELY_ACTIONS, TARGET_FIELDS, TILT_MAX_DELTA,
)
from api.routes.chat_ws import websocket_endpoint
from backend.tests.chat_session_fakes import make_chat_session_service
from backend.tests.test_emotion_chat_ws import jev_answers


POSE_FIELDS = TARGET_FIELDS[:6]


def track_actions(runtime: AvatarRuntime) -> list[tuple[str, float]]:
    """Record each idle action with the planned clock at which it started."""
    actions = []
    choose = runtime._choose_action

    def tracked():
        action = choose()
        actions.append((action, runtime._clock))
        return action

    runtime._choose_action = tracked
    return actions


def simulate(seed: int, count: int, *, activity: float = 1.0):
    runtime = AvatarRuntime(AsyncMock(), rng=random.Random(seed))
    runtime._activity = activity
    actions = track_actions(runtime)
    return runtime, [runtime._next_plan() for _ in range(count)], actions


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
        first._recent.extend(["weight_shift", "weight_shift", "hop", "rest_front"])
        second._recent.extend(["curious_tilt", "think_up", "hum_sway", "glance_aside"])
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
        for action in ("rest_front", "weight_shift", "curious_tilt", "hum_sway", "hop", "stretch"):
            first._plan_action(action)
        self.assertEqual(list(first._recent_gaze), history)

    def test_idle_schedule_meets_viewing_targets(self):
        holds, gaze_intervals, names = [], [], []
        tilts = same_side = poses = 0
        away_time = total_time = 0.0
        away = returned = 0
        for seed in range(20):
            _, plans, actions = simulate(seed, 1000)
            run = [name for name, _ in actions]
            self.assertFalse(any(a == b == c for a, b, c in zip(run, run[1:], run[2:])))
            names += run
            previous = dict.fromkeys(TARGET_FIELDS, 0.0)
            clock = last_gaze = 0.0
            for index, (plan, delay) in enumerate(plans):
                target = plan["target"]
                pose_changed = any(target[field] != previous[field] for field in POSE_FIELDS)
                gaze_changed = (target["gazeX"], target["gazeY"]) != (previous["gazeX"], previous["gazeY"])
                motion = plan["moveMs"] / 1000 if pose_changed else 0.25 if gaze_changed else 0.0
                if "hop" in plan:
                    motion = max(motion, plan["hop"]["durationMs"] / 1000)
                holds.append(delay - motion)
                if pose_changed:
                    poses += 1
                    tilts += abs(target["headZ"]) > 0.3
                    same_side += target["headX"] * target["headZ"] > 0
                if gaze_changed:
                    if clock:
                        gaze_intervals.append(clock - last_gaze)
                    last_gaze = clock
                    if math.hypot(target["gazeX"], target["gazeY"]) > 0.3:
                        away += 1
                        returned += any(math.hypot(later["target"]["gazeX"], later["target"]["gazeY"]) < 0.3
                                        for later, _ in plans[index + 1:index + 4])
                if math.hypot(target["gazeX"], target["gazeY"]) > 0.06:
                    away_time += delay
                total_time += delay
                clock += delay
                previous = target
        # Nothing on the head, body or eyes stays unchanged for long.
        self.assertLessEqual(max(holds), 4.0)
        # Clear sideways tilts are occasional and not tied to the turn direction.
        self.assertLessEqual(tilts / poses, 0.25)
        self.assertLessEqual(same_side / poses, 0.55)
        self.assertTrue(0.25 <= away_time / total_time <= 0.40)
        gaze_intervals.sort()
        self.assertTrue(1.0 <= gaze_intervals[len(gaze_intervals) // 2] <= 3.0)
        self.assertGreaterEqual(returned / away, 0.7)
        self.assertEqual(set(names), set(IDLE_ACTIONS))

    def test_listening_clears_pending_actions_and_reset_clears_history(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(17))
        runtime._next_plan()
        runtime._steps.extend(runtime._plan_action("glance_aside"))
        gaze_history, action_history = list(runtime._recent_gaze), list(runtime._recent)
        runtime.update_state({"type": "avatar_state", "listening": True})
        self.assertFalse(runtime._steps)
        for _ in range(30):
            runtime._next_plan()
        self.assertEqual(list(runtime._recent_gaze), gaze_history)
        self.assertEqual(list(runtime._recent), action_history)
        runtime.update_state({"type": "avatar_state", "listening": False})
        runtime._next_plan()
        self.assertEqual(len(runtime._recent), min(4, len(action_history) + 1))
        runtime.reset()
        for history in (runtime._recent_gaze, runtime._recent, runtime._steps, runtime._started_at):
            self.assertFalse(history)
        self.assertEqual(runtime._last_side, 0.0)

    def test_seeded_targets_are_bounded_and_have_varied_amplitudes_and_deadlines(self):
        _, plans, actions = simulate(42, 300)
        self.assertEqual([plan["sequence"] for plan, _ in plans], list(range(1, 301)))
        previous = dict.fromkeys(TARGET_FIELDS, 0.0)
        clock = 0.0
        for plan, delay in plans:
            action = [name for name, started in actions if started <= clock + 1e-9][-1]
            clock += delay
            self.assertEqual(plan["state"], "idle")
            self.assertNotIn("turn_id", plan)
            self.assertEqual(set(plan["target"]), set(TARGET_FIELDS))
            self.assertTrue(all(math.isfinite(value) and -1 <= value <= 1
                                for value in plan["target"].values()))
            self.assertTrue(300 <= plan["moveMs"] <= 3000)
            if any(plan["target"][field] != previous[field] for field in POSE_FIELDS):
                # 哼歌搖擺刻意在上一拍走完前送出下一拍；其餘動作仍須等移動完成。
                if action == "hum_sway":
                    self.assertGreaterEqual(delay, plan["moveMs"] / 1000 * HUM_BEAT_OVERLAP - 1e-9)
                else:
                    self.assertGreater(delay, plan["moveMs"] / 1000)
            self.assertGreater(delay, 0)
            self.assertTrue(3 <= plan["breath"]["periodSec"] <= 8)
            self.assertTrue(0 <= plan["breath"]["amplitude"] <= 1)
            self.assertTrue(0 <= plan["breath"]["rootAmplitude"] <= 0.08)
            if "hop" in plan:
                self.assertTrue(0 <= plan["hop"]["height"] <= 0.12)
                self.assertTrue(400 <= plan["hop"]["durationMs"] <= 1600)
                self.assertGreater(delay, plan["hop"]["durationMs"] / 1000)
            previous = plan["target"]
        self.assertGreater(max(abs(plan["target"]["headZ"]) for plan, _ in plans), 0.5)
        self.assertGreater(max(-plan["target"]["headY"] for plan, _ in plans), 0.4)
        self.assertGreater(max(plan["target"]["headY"] for plan, _ in plans), 0.25)
        self.assertTrue(any(abs(plan["target"]["headZ"]) < 0.1 for plan, _ in plans))
        self.assertTrue(any("hop" in plan for plan, _ in plans))
        self.assertGreater(len({plan["moveMs"] for plan, _ in plans}), 60)
        self.assertGreater(len({round(delay, 2) for _, delay in plans}), 100)

    def test_hum_sway_beats_overlap_and_curious_tilt_is_limited(self):
        beats = tilts = 0
        for seed in range(40):
            runtime = AvatarRuntime(AsyncMock(), rng=random.Random(seed))
            runtime._target["headZ"] = round(random.Random(seed).uniform(-0.7, 0.7), 4)
            before = runtime._target["headZ"]
            for step in runtime._plan_action("curious_tilt"):
                self.assertLessEqual(abs(step["target"]["headZ"] - before), TILT_MAX_DELTA + 1e-6)
                tilts += 1
                break
            moves = [step for step in runtime._plan_action("hum_sway") if step["moveMs"]]
            for step in moves[:-1]:
                self.assertAlmostEqual(step["delay"], step["moveMs"] / 1000 * HUM_BEAT_OVERLAP, places=6)
                self.assertGreaterEqual(step["moveMs"], 1200)
                beats += 1
            last_beat = moves[-2]["target"]["headZ"]
            self.assertNotEqual(math.copysign(1, moves[-3]["target"]["headZ"]), math.copysign(1, last_beat),
                                "hum beats alternate sides")
        self.assertGreater(beats, 100)
        self.assertEqual(tilts, 40)

    def test_eye_steps_keep_pose_breath_and_every_action_ends_on_the_viewer(self):
        runtime = AvatarRuntime(AsyncMock(), rng=random.Random(29))
        actions = track_actions(runtime)
        previous, eye_steps, endings = None, 0, 0
        for _ in range(1500):
            started = len(actions)
            plan, _ = runtime._next_plan()
            if previous and all(plan["target"][field] == previous["target"][field] for field in POSE_FIELDS):
                if (plan["target"]["gazeX"], plan["target"]["gazeY"]) != (previous["target"]["gazeX"],
                                                                           previous["target"]["gazeY"]):
                    eye_steps += 1
                    # Breath drifts only when a new action begins.
                    if len(actions) == started:
                        self.assertEqual(plan["breath"], previous["breath"])
                    self.assertEqual(plan["moveMs"], previous["moveMs"])
                    self.assertNotIn("hop", plan)
            if not runtime._steps:
                endings += 1
                self.assertEqual((plan["target"]["gazeX"], plan["target"]["gazeY"]), (0, 0))
            previous = plan
        self.assertGreater(eye_steps, 500)
        self.assertGreater(endings, 200)

    def test_large_actions_respect_cooldowns_and_controls_clear_pending(self):
        runtime, _, actions = simulate(4, 3000)
        started: dict[str, float] = {}
        for (name, clock), (following, _) in zip(actions, actions[1:] + [(None, 0)]):
            if name in started:
                self.assertGreaterEqual(clock - started[name], IDLE_ACTIONS[name][1])
            started[name] = clock
            if name == "curious_tilt":
                self.assertNotEqual(following, name)
        for control in ({"type": "avatar_state", "listening": True},
                        {"type": "avatar_state", "listening": False, "enabled": False}):
            runtime._steps.extend(runtime._plan_action("hop"))
            runtime.update_state(control)
            self.assertFalse(runtime._steps)
            self.assertNotIn("hop", runtime._next_plan()[0])
        runtime._steps.extend(runtime._plan_action("hop"))
        runtime.reset()
        self.assertFalse(runtime._steps)

    def test_settle_activity_scales_pose_breath_and_lively_actions(self):
        _, plans, actions = simulate(11, 1500, activity=0.5)
        _, _, normal = simulate(11, 1500)

        def lively_share(history):
            return sum(name in LIVELY_ACTIONS for name, _ in history) / len(history)

        self.assertLess(lively_share(actions), lively_share(normal))
        for plan, _ in plans:
            self.assertTrue(all(abs(plan["target"][field]) <= 0.5 for field in POSE_FIELDS))
            self.assertLessEqual(plan["breath"]["amplitude"], 0.5)
            if "hop" in plan:
                self.assertLessEqual(plan["hop"]["height"], 0.06)

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
