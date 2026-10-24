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
        self._gaze_front_remaining = self._rng.randint(1, 2)
        self._changed = asyncio.Event()
        self._recent: deque[str] = deque(maxlen=4)
        self._recent_gaze: deque[str] = deque(maxlen=2)
        self._sequence = 0
        self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
        self._gaze_anchor = (0.0, 0.0)
        self._breath = {"periodSec": 4.8, "amplitude": 0.25, "rootAmplitude": 0.006}
        self._move_ms = 1000
        self._quiet_remaining = 0.0
        self._quiet_in_front_run = False
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
            self._quiet_remaining = 0.0
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
            self._quiet_remaining = 0.0
            self._changed.set()

    def reset(self) -> None:
        """A new session has no inherited expression or recording operation."""
        self._activity, self._speed = 1.0, 1.0
        self._listening = False
        self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
        self._gaze_anchor = (0.0, 0.0)
        self._breath = {"periodSec": 4.8, "amplitude": 0.25, "rootAmplitude": 0.006}
        self._move_ms = 1000
        self._quiet_remaining = 0.0
        self._quiet_in_front_run = False
        self._recent.clear()
        self._recent_gaze.clear()
        self._gaze_front_remaining = self._rng.randint(1, 2)
        self._disabled_sent = False
        self._changed.set()

    def _choose(self, choices: dict[str, float]) -> str:
        names = list(choices)
        weights = [choices[name] * (0.3 ** self._recent.count(name)) for name in names]
        choice = self._rng.choices(names, weights=weights, k=1)[0]
        self._recent.append(choice)
        return choice

    def _sample_gaze(self) -> tuple[float, float]:
        """Keep eye direction history separate from body direction and front holds."""
        directions = list(GAZE_DIRECTION_RANGES)
        weights = [0.3 ** self._recent_gaze.count(direction) for direction in directions]
        direction = self._rng.choices(directions, weights=weights, k=1)[0]
        self._recent_gaze.append(direction)
        x_range, y_range = GAZE_DIRECTION_RANGES[direction]
        return self._rng.uniform(*x_range), self._rng.uniform(*y_range)

    def _next_plan(self) -> tuple[dict, float]:
        rng = self._rng
        self._sequence += 1
        if not self._enabled:
            self._quiet_remaining = 0.0
            self._target = dict.fromkeys(TARGET_FIELDS, 0.0)
            return {
                "type": "ambient_plan", "sequence": self._sequence, "state": "idle", "moveMs": 800,
                "target": dict(self._target),
                "breath": {"periodSec": 5.0, "amplitude": 0.0, "rootAmplitude": 0.0},
            }, 0.0

        listening = self._listening
        activity = self._activity
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
            hold = rng.uniform(2.5, 4.8)
            hop = None
        elif self._quiet_remaining > 0:
            # Keep the pose and breath long enough to settle; only the eyes may refocus.
            gaze_x, gaze_y = self._gaze_anchor
            if rng.random() < 0.6:
                angle, radius = rng.uniform(0, math.tau), rng.uniform(0.015, 0.04)
                gaze_x += math.cos(angle) * radius
                gaze_y += math.sin(angle) * radius
            self._target.update(gazeX=_bounded(gaze_x), gazeY=_bounded(gaze_y))
            delay = (self._quiet_remaining if self._quiet_remaining < 3.8
                     else rng.uniform(1.3, 2.5))
            self._quiet_remaining = max(0.0, self._quiet_remaining - delay)
            return {"type": "ambient_plan", "sequence": self._sequence, "state": "idle",
                    "moveMs": self._move_ms, "target": dict(self._target),
                    "breath": dict(self._breath)}, delay
        else:
            action = self._choose({"nearby": 3.0, "pose": 2.6, "sway": 2.0, "hop": activity})
            direction = self._choose({"left": 1.0, "right": 1.0, "center": 0.8})
            sign = {"left": -1.0, "right": 1.0, "center": 0.0}[direction]
            extent = rng.uniform(0.14, 0.32) if action == "nearby" else rng.uniform(0.45, 0.9)
            extent *= activity
            # Count primary gaze changes separately from occasional quiet refocusing.
            gaze = "front" if self._gaze_front_remaining > 0 else "glance"
            self._gaze_front_remaining = (self._gaze_front_remaining - 1 if gaze == "front"
                                          else rng.randint(2, 4))
            self._recent.append(gaze)
            gaze_x, gaze_y = (0.0, 0.0) if gaze == "front" else self._sample_gaze()
            self._gaze_anchor = (gaze_x, gaze_y)
            if gaze == "glance":
                self._quiet_in_front_run = False
            head_x = sign * extent * 0.7 + gaze_x * 0.22
            self._target = {
                "headX": _bounded(head_x), "headY": _bounded(rng.uniform(-0.7, 0.7) * extent),
                "headZ": _bounded(sign * extent + rng.uniform(-0.12, 0.12)),
                "bodyX": _bounded(head_x * 0.5 + rng.uniform(-0.2, 0.2) * activity),
                "bodyY": _bounded(rng.uniform(-0.55, 0.55) * extent),
                "bodyZ": _bounded(sign * extent * rng.uniform(0.42, 0.75)),
                "gazeX": _bounded(gaze_x), "gazeY": _bounded(gaze_y),
            }
            move_ms = max(700, min(3000, round(rng.randint(1000, 2400) / self._speed)))
            breath = {"periodSec": round(rng.uniform(3.8, 6.7), 2),
                      "amplitude": round(rng.uniform(0.4, 0.95) * activity, 3),
                      "rootAmplitude": round(rng.uniform(0.018, 0.06) * activity, 4)}
            hold = rng.uniform(0.18, 0.65) if gaze == "glance" else rng.uniform(1.2, 4.2)
            hop = ({"height": round(rng.uniform(0.045, 0.105) * activity, 4),
                    "durationMs": rng.randint(700, 1400)} if action == "hop" else None)
            if gaze == "front" and not self._quiet_in_front_run and rng.random() < 0.35:
                self._quiet_remaining = rng.uniform(4.5, 6.5)
                self._quiet_in_front_run = True

        self._move_ms = move_ms
        self._breath = dict(breath)
        plan = {"type": "ambient_plan", "sequence": self._sequence,
                "state": "listening" if listening else "idle", "moveMs": move_ms,
                "target": dict(self._target), "breath": breath}
        if hop is not None:
            plan["hop"] = hop
        return plan, move_ms / 1000 + hold

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
