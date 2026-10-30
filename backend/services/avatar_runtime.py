"""Connection-scoped, model-independent autonomous Live2D targets."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import math
import random


TARGET_FIELDS = ("headX", "headY", "headZ", "bodyX", "bodyY", "bodyZ", "gazeX", "gazeY")
GAZE_DIRECTION_RANGES = {
    "left": ((-0.75, -0.45), (-0.04, 0.04)),
    "right": ((0.45, 0.75), (-0.04, 0.04)),
    "up": ((-0.04, 0.04), (0.90, 1.0)),
    "down": ((-0.04, 0.04), (-1.0, -0.95)),
    "up_left": ((-0.80, -0.55), (0.85, 1.0)),
    "up_right": ((0.55, 0.80), (0.85, 1.0)),
    "down_left": ((-0.80, -0.55), (-1.0, -0.85)),
    "down_right": ((0.55, 0.80), (-1.0, -0.85)),
}
# Idle behaviours: base weight and minimum planned seconds between two starts.
IDLE_ACTIONS = {
    "rest_front": (3.0, 0.0),
    "glance_aside": (2.6, 0.0),
    "weight_shift": (1.8, 6.0),
    "look_down": (1.2, 12.0),
    "think_up": (1.1, 12.0),
    "curious_tilt": (0.8, 14.0),
    "hum_sway": (0.7, 30.0),
    "hop": (0.6, 40.0),
    "stretch": (0.35, 60.0),
}
LIVELY_ACTIONS = frozenset({"hum_sway", "hop", "stretch"})
# 哼歌搖擺在上一拍走到這個比例時就送出下一拍，折返點像鐘擺而不是停車再起步。
HUM_BEAT_OVERLAP = 0.55
# 好奇歪頭相對目前歪斜（headZ，正規化）的最大變化量，約 18 度。
TILT_MAX_DELTA = 0.78


def _bounded(value: float, limit: float = 1.0) -> float:
    return round(max(-limit, min(limit, value)), 4)


class AvatarRuntime:
    """Choose targets in Python; the renderer owns interpolation and actual pose.

    This runtime has no turn ID, model client, DB access or global owner state.
    Only ``run`` sends messages, so state changes never spawn competing writers.
    """

    def __init__(self, send: Callable[[dict], Awaitable[None]], *, rng: random.Random | None = None) -> None:
        self._send = send
        self._rng = rng if rng is not None else random.Random()
        self._changed = asyncio.Event()
        self._recent: deque[str] = deque(maxlen=4)
        self._recent_gaze: deque[str] = deque(maxlen=2)
        self._steps: deque[dict] = deque()
        self._started_at: dict[str, float] = {}
        # Planned seconds, so cooldowns stay reproducible without a wall clock.
        self._clock = 0.0
        self._last_side = 0.0
        self._sequence = 0
        self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
        self._period = 4.8
        self._idle_breath = {"periodSec": 4.8, "amplitude": 0.25, "rootAmplitude": 0.006}
        self._move_ms = 1000
        self._activity = 1.0
        self._speed = 1.0
        self._enabled = True
        self._listening = False
        self._disabled_sent = False

    def update_state(self, payload: dict) -> None:
        """Accept only local recording/playback controls, never identity or targets."""
        if (set(payload) - {"type", "listening", "enabled"}
                or payload.get("type") != "avatar_state"
                or type(payload.get("listening")) is not bool
                or ("enabled" in payload and type(payload["enabled"]) is not bool)):
            raise ValueError("avatar_state 需要 listening 布林值及可選的 enabled 布林值")
        enabled = payload.get("enabled", self._enabled)
        listening = payload["listening"]
        if (enabled, listening) != (self._enabled, self._listening):
            self._enabled, self._listening = enabled, listening
            self._steps.clear()
            self._disabled_sent = False
            self._changed.set()

    def adopt_expression_plan(self, plan: dict) -> None:
        """Use the last valid settle style without replaying or inventing emotion."""
        idle = plan.get("idlePlan")
        settle = idle.get("settlePose") if isinstance(idle, dict) else None
        profile = settle.get("bodyMotionProfile") if isinstance(settle, dict) else None
        if not isinstance(profile, dict):
            return
        style = profile.get("style")
        activity = {"locked_tense": 0.55, "heavy_slow_sink": 0.5, "quick_recoil": 0.75}.get(style, 1.0)
        speed = profile.get("speed")
        if isinstance(speed, (int, float)) and not isinstance(speed, bool) and math.isfinite(speed):
            speed = max(0.65, min(1.25, speed / 0.62))
        else:
            speed = 1.0
        if (activity, speed) != (self._activity, self._speed):
            self._activity, self._speed = activity, speed
            self._steps.clear()
            self._changed.set()

    def reset(self) -> None:
        """A new session has no inherited expression or recording operation."""
        self._activity, self._speed = 1.0, 1.0
        self._listening = False
        self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
        self._period = 4.8
        self._idle_breath = {"periodSec": 4.8, "amplitude": 0.25, "rootAmplitude": 0.006}
        self._move_ms = 1000
        self._steps.clear()
        self._started_at.clear()
        self._last_side = 0.0
        self._recent.clear()
        self._recent_gaze.clear()
        self._disabled_sent = False
        self._changed.set()

    def _choose(self, choices: dict[str, float]) -> str:
        names = list(choices)
        weights = [choices[name] * (0.3 ** self._recent.count(name)) for name in names]
        choice = self._rng.choices(names, weights=weights, k=1)[0]
        self._recent.append(choice)
        return choice

    def _choose_action(self) -> str:
        """Cooldowns and run limits keep large actions rare and tilts from repeating."""
        recent = list(self._recent)
        choices = {}
        for name, (weight, cooldown) in IDLE_ACTIONS.items():
            started = self._started_at.get(name)
            if ((started is not None and self._clock - started < cooldown)
                    or recent[-2:] == [name, name]
                    or (name == "curious_tilt" and recent[-1:] == [name])):
                continue
            choices[name] = weight * (self._activity if name in LIVELY_ACTIONS else 1.0)
        action = self._choose(choices)
        self._started_at[action] = self._clock
        return action

    def _sample_gaze(self) -> tuple[float, float]:
        """Keep eye direction history separate from body direction and front holds."""
        directions = list(GAZE_DIRECTION_RANGES)
        weights = [0.3 ** self._recent_gaze.count(direction) for direction in directions]
        direction = self._rng.choices(directions, weights=weights, k=1)[0]
        self._recent_gaze.append(direction)
        x_range, y_range = GAZE_DIRECTION_RANGES[direction]
        return self._rng.uniform(*x_range), self._rng.uniform(*y_range)

    def _side(self) -> float:
        """Prefer the opposite side without forbidding a repeat."""
        if self._last_side and self._rng.random() < 0.5:
            side = -self._last_side
        else:
            side = self._rng.choice((-1.0, 1.0))
        self._last_side = side
        return side

    def _saccade(self) -> tuple[float, float]:
        """A visible refocus near the viewer; Y is wider because the rig moves less vertically."""
        angle, radius = self._rng.uniform(0, math.tau), self._rng.uniform(0.12, 0.26)
        return math.cos(angle) * radius, math.sin(angle) * radius * 1.3

    def _pose(self, head: tuple[float, float, float], body: tuple[float, float, float],
              gaze: tuple[float, float] = (0.0, 0.0)) -> dict:
        values = [value * self._activity for value in (*head, *body)] + list(gaze)
        return {field: _bounded(value) for field, value in zip(TARGET_FIELDS, values)}

    def _settled(self) -> dict:
        """Near-neutral pose with independent axes, used for rests and returns."""
        u = self._rng.uniform
        return self._pose((u(-0.18, 0.18), u(-0.15, 0.15), u(-0.15, 0.15)),
                          (u(-0.2, 0.2), u(-0.15, 0.15), u(-0.2, 0.2)))

    def _move(self, target: dict, move_ms: tuple[int, int], hold: tuple[float, float],
              hop: dict | None = None) -> dict:
        move = max(300, min(3000, round(self._rng.randint(*move_ms) / self._speed)))
        return {"target": target, "moveMs": move, "delay": move / 1000 + self._rng.uniform(*hold), "hop": hop}

    def _look(self, target: dict, gaze: tuple[float, float], hold: tuple[float, float]) -> dict:
        """Change only the eyes; the renderer keeps the unchanged head/body trajectory."""
        return {"target": {**target, "gazeX": _bounded(gaze[0]), "gazeY": _bounded(gaze[1])},
                "moveMs": None, "delay": self._rng.uniform(*hold), "hop": None}

    def _drift_breath(self) -> None:
        """Breathing period drifts slowly instead of jumping on every pose."""
        rng = self._rng
        self._period = max(4.2, min(5.6, self._period + rng.uniform(-0.25, 0.25)))
        self._idle_breath = {"periodSec": round(self._period, 2),
                             "amplitude": round(rng.uniform(0.45, 0.8) * self._activity, 3),
                             "rootAmplitude": round(rng.uniform(0.018, 0.05) * self._activity, 4)}

    def _plan_action(self, action: str) -> list[dict]:
        """Expand one idle behaviour into eye-led steps that end back on the viewer."""
        rng, u = self._rng, self._rng.uniform
        current = dict(self._target)
        if action == "rest_front":
            pose = self._settled()
            steps = [self._move(pose, (1100, 2000), (0.5, 1.1))]
            steps += [self._look(pose, self._saccade(), (0.6, 1.2)) for _ in range(rng.randint(1, 3))]
            return [*steps, self._look(pose, (0.0, 0.0), (1.1, 2.0))]
        if action == "glance_aside":
            gaze_x, gaze_y = self._sample_gaze()
            turned = self._pose((gaze_x * u(0.25, 0.45), gaze_y * u(0.2, 0.35), u(-0.15, 0.15) + gaze_x * 0.12),
                                (gaze_x * u(0.1, 0.25), u(-0.1, 0.1), gaze_x * u(0.05, 0.2)),
                                (gaze_x * 0.85, gaze_y * 0.85))
            steps = [self._look(current, (gaze_x, gaze_y), (0.22, 0.4)),
                     self._move(turned, (800, 1400), (0.5, 1.4))]
            if rng.random() < 0.35:
                steps.append(self._look(turned, (gaze_x * u(0.4, 0.7), gaze_y * u(0.4, 0.7) + u(-0.2, 0.2)),
                                        (0.6, 1.2)))
            return [*steps, self._look(turned, (0.0, 0.0), (0.3, 0.6)),
                    self._move(self._settled(), (1000, 1600), (0.6, 1.4))]
        if action == "look_down":
            gaze = (u(-0.3, 0.3), u(-0.95, -0.7))
            down = self._pose((gaze[0] * 0.4 + u(-0.1, 0.1), u(-0.65, -0.4), u(-0.25, 0.25)),
                              (u(-0.15, 0.15), u(-0.3, -0.1), u(-0.15, 0.15)), (gaze[0], gaze[1] * 0.8))
            steps = [self._look(current, gaze, (0.2, 0.4)), self._move(down, (1200, 1800), (0.9, 1.9))]
            if rng.random() < 0.6:
                steps.append(self._look(down, (u(-0.45, 0.45), u(-0.9, -0.6)), (0.7, 1.3)))
            # The head is still lowered, so meeting the viewer means raising the eyes first.
            return [*steps, self._look(down, (0.0, u(0.35, 0.55)), (0.35, 0.6)),
                    self._move(self._settled(), (1000, 1500), (0.6, 1.4))]
        if action == "think_up":
            side = self._side()
            gaze = (side * u(0.45, 0.7), u(0.75, 0.95))
            up = self._pose((side * u(0.1, 0.25), u(0.25, 0.45), side * u(0.15, 0.35)),
                            (u(-0.15, 0.15), u(0.05, 0.2), side * u(0.05, 0.15)), (gaze[0] * 0.85, gaze[1] * 0.85))
            steps = [self._look(current, gaze, (0.25, 0.45)), self._move(up, (1100, 1600), (1.0, 2.0))]
            if rng.random() < 0.5:
                steps.append(self._look(up, (-gaze[0] * u(0.3, 0.6), u(0.7, 0.95)), (0.6, 1.2)))
            return [*steps, self._look(up, (0.0, -u(0.2, 0.35)), (0.3, 0.55)),
                    self._move(self._settled(), (1000, 1500), (0.6, 1.4))]
        if action == "weight_shift":
            side = self._side()
            # The torso leans while the head counter-balances and keeps facing the viewer.
            shifted = self._pose((-side * u(0.08, 0.22), u(-0.12, 0.12), -side * u(0.1, 0.3)),
                                 (side * u(0.5, 0.85), u(-0.2, 0.2), side * u(0.45, 0.8)))
            steps = [self._move(shifted, (1400, 2200), (0.8, 1.8))]
            if rng.random() < 0.8:
                steps += [self._look(shifted, self._saccade(), (0.7, 1.4)),
                          self._look(shifted, (0.0, 0.0), (0.6, 1.2))]
            return steps
        if action == "curious_tilt":
            side = self._side()
            tilted = self._pose((u(-0.2, 0.2), u(0.0, 0.2), side * u(0.45, 0.72)),
                                (u(-0.15, 0.15), u(-0.1, 0.1), side * u(0.1, 0.3)))
            # 從另一側歪過來時，單次歪斜變化量不超過上限。
            tilted["headZ"] = _bounded(max(current["headZ"] - TILT_MAX_DELTA,
                                           min(current["headZ"] + TILT_MAX_DELTA, tilted["headZ"])))
            return [self._move(tilted, (900, 1400), (0.9, 1.8)),
                    self._look(tilted, self._saccade(), (0.5, 1.0)),
                    self._look(tilted, (0.0, 0.0), (0.4, 0.8)),
                    self._move(self._settled(), (1000, 1500), (0.5, 1.0))]
        if action == "hum_sway":
            side, beat = self._side(), rng.randint(1200, 1500)
            steps = []
            for _ in range(rng.randint(4, 6)):
                beat_pose = self._pose((-side * u(0.04, 0.1), u(0.0, 0.12), side * u(0.3, 0.42)),
                                       (side * u(0.28, 0.45), u(-0.05, 0.1), side * u(0.4, 0.58)))
                step = self._move(beat_pose, (beat, beat), (0.0, 0.0))
                # 不等到這拍走完：前端從目前位置與速度接續，折返點自然圓滑。
                step["delay"] = step["moveMs"] / 1000 * HUM_BEAT_OVERLAP
                steps.append(step)
                side = -side
            self._last_side = -side
            settled = self._settled()
            return [*steps, self._move(settled, (1000, 1400), (0.4, 0.8)),
                    self._look(settled, self._saccade(), (0.5, 1.0)), self._look(settled, (0.0, 0.0), (0.6, 1.2))]
        if action == "hop":
            pose = self._pose((u(-0.15, 0.15), u(0.05, 0.25), u(-0.2, 0.2)),
                              (u(-0.15, 0.15), u(0.0, 0.2), u(-0.15, 0.15)))
            count = 2 if rng.random() < 0.3 else 1
            steps = []
            for index in range(count):
                duration = rng.randint(700, 1000) if count == 2 else rng.randint(800, 1400)
                hop = {"height": round(u(0.045, 0.105) * self._activity * (0.7 if index else 1.0), 4),
                       "durationMs": duration}
                step = (self._move(pose, (800, 1200), (0.0, 0.0)) if index == 0
                        else self._look(pose, (0.0, 0.0), (0.0, 0.0)))
                landing = u(0.08, 0.2) if index + 1 < count else u(0.6, 1.2)
                step.update(hop=hop, delay=max(step["delay"], duration / 1000 + landing))
                steps.append(step)
            return steps
        stretched = self._pose((u(-0.15, 0.15), u(0.35, 0.55), u(-0.15, 0.15)),
                               (u(-0.1, 0.1), u(0.4, 0.7), u(-0.15, 0.15)), (u(-0.2, 0.2), u(0.5, 0.8)))
        deep = {"periodSec": round(u(6.4, 7.4), 2), "amplitude": round(0.95 * self._activity, 3),
                "rootAmplitude": round(u(0.04, 0.055) * self._activity, 4)}
        return [{**self._move(stretched, (1600, 2200), (1.2, 2.0)), "breath": deep},
                self._move(self._settled(), (1400, 1800), (0.6, 1.2))]

    def _next_plan(self) -> tuple[dict, float]:
        rng = self._rng
        self._sequence += 1
        if not self._enabled:
            self._steps.clear()
            self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
            return {
                "type": "ambient_plan", "sequence": self._sequence, "state": "idle", "moveMs": 800,
                "target": dict(self._target),
                "breath": {"periodSec": 5.0, "amplitude": 0.0, "rootAmplitude": 0.0},
            }, 0.0

        listening = self._listening
        activity = self._activity
        hop = None
        if listening:
            # Retain part of the current lean instead of snapping every channel to zero.
            self._target = {
                key: _bounded(self._target[key] * 0.6 + rng.uniform(-0.065, 0.065) * activity)
                for key in TARGET_FIELDS
            }
            self._target.update(gazeX=0.0, gazeY=0.0)
            move_ms = rng.randint(900, 1500)
            breath = {"periodSec": round(rng.uniform(4.8, 6.5), 2),
                      "amplitude": round(rng.uniform(0.25, 0.4), 3), "rootAmplitude": 0.006}
            delay = move_ms / 1000 + rng.uniform(2.5, 4.8)
        else:
            if not self._steps:
                self._drift_breath()
                self._steps.extend(self._plan_action(self._choose_action()))
            step = self._steps.popleft()
            self._target = dict(step["target"])
            move_ms = step["moveMs"] or self._move_ms
            breath = dict(step.get("breath") or self._idle_breath)
            hop = step["hop"]
            delay = step["delay"]

        self._clock += delay
        self._move_ms = move_ms
        plan = {"type": "ambient_plan", "sequence": self._sequence,
                "state": "listening" if listening else "idle", "moveMs": move_ms,
                "target": dict(self._target), "breath": breath}
        if hop is not None:
            plan["hop"] = dict(hop)
        return plan, delay

    async def run(self) -> None:
        """Send immediately, then wait for the next deadline or a local state change."""
        while True:
            self._changed.clear()
            if not self._enabled and self._disabled_sent:
                await self._changed.wait()
                continue
            plan, delay = self._next_plan()
            await self._send(plan)
            if self._changed.is_set():
                continue
            if not self._enabled:
                self._disabled_sent = True
                await self._changed.wait()
                continue
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
            except TimeoutError:
                pass
