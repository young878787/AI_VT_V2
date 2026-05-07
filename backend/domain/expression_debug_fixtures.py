"""Backend-owned fake AI replies for expression plan debug tests."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from typing import Any


DEBUG_EXPRESSION_KINDS = (
    "happy",
    "playful",
    "teasing",
    "angry",
    "sad",
    "gloomy",
    "shy",
    "surprised",
    "conflicted",
)

DEBUG_INTENSITY_SCALE = {
    "soft": 0.72,
    "normal": 1.0,
    "strong": 1.28,
}

DEBUG_EXPRESSION_RULES: dict[str, dict[str, Any]] = {
    "neutral": {
        "label": "中性",
        "primary_emotion": "neutral",
        "emotion": "neutral",
        "performance_mode": "smile",
        "intensity": 0.28,
        "energy": 0.32,
        "playfulness": 0.18,
        "warmth": 0.45,
        "dominance": 0.42,
        "arc": "steady",
        "hold_ms": 1500,
        "speaking_rate": 1.0,
        "blink_style": "normal",
    },
    "happy": {
        "label": "開心",
        "primary_emotion": "happy",
        "emotion": "happy",
        "performance_mode": "bright_talk",
        "intensity": 0.62,
        "energy": 0.72,
        "playfulness": 0.45,
        "warmth": 0.82,
        "dominance": 0.34,
        "arc": "steady",
        "hold_ms": 1800,
        "speaking_rate": 1.04,
        "blink_style": "normal",
    },
    "playful": {
        "label": "調皮",
        "primary_emotion": "playful",
        "emotion": "playful",
        "performance_mode": "goofy_face",
        "intensity": 0.78,
        "energy": 0.82,
        "playfulness": 0.9,
        "warmth": 0.62,
        "dominance": 0.42,
        "arc": "pop_then_settle",
        "hold_ms": 1700,
        "speaking_rate": 1.1,
        "blink_style": "teasing_pause",
    },
    "teasing": {
        "label": "挑釁",
        "primary_emotion": "teasing",
        "emotion": "teasing",
        "performance_mode": "smug",
        "intensity": 0.58,
        "energy": 0.54,
        "playfulness": 0.72,
        "warmth": 0.42,
        "dominance": 0.58,
        "arc": "steady",
        "hold_ms": 1900,
        "speaking_rate": 1.0,
        "blink_style": "teasing_pause",
    },
    "angry": {
        "label": "生氣",
        "primary_emotion": "angry",
        "emotion": "angry",
        "performance_mode": "meltdown",
        "intensity": 0.86,
        "energy": 0.76,
        "playfulness": 0.06,
        "warmth": 0.12,
        "dominance": 0.76,
        "arc": "glare_then_flatten",
        "hold_ms": 2100,
        "speaking_rate": 1.02,
        "blink_style": "focused_pause",
    },
    "sad": {
        "label": "難過",
        "primary_emotion": "sad",
        "emotion": "sad",
        "performance_mode": "tense_hold",
        "intensity": 0.72,
        "energy": 0.28,
        "playfulness": 0.04,
        "warmth": 0.3,
        "dominance": 0.16,
        "arc": "shrink_then_recover",
        "hold_ms": 2300,
        "speaking_rate": 0.9,
        "blink_style": "sleepy_slow",
    },
    "gloomy": {
        "label": "陰沉",
        "primary_emotion": "gloomy",
        "emotion": "gloomy",
        "performance_mode": "deadpan",
        "intensity": 0.64,
        "energy": 0.18,
        "playfulness": 0.02,
        "warmth": 0.12,
        "dominance": 0.28,
        "arc": "steady",
        "hold_ms": 2400,
        "speaking_rate": 0.88,
        "blink_style": "sleepy_slow",
    },
    "shy": {
        "label": "害羞",
        "primary_emotion": "shy",
        "emotion": "shy",
        "performance_mode": "awkward",
        "intensity": 0.58,
        "energy": 0.42,
        "playfulness": 0.18,
        "warmth": 0.58,
        "dominance": 0.16,
        "arc": "shrink_then_recover",
        "hold_ms": 2100,
        "speaking_rate": 0.96,
        "blink_style": "shy_fast",
    },
    "surprised": {
        "label": "驚訝",
        "primary_emotion": "surprised",
        "emotion": "surprised",
        "performance_mode": "shock_recoil",
        "intensity": 0.82,
        "energy": 0.86,
        "playfulness": 0.2,
        "warmth": 0.32,
        "dominance": 0.28,
        "arc": "pop_then_settle",
        "hold_ms": 1500,
        "speaking_rate": 1.08,
        "blink_style": "surprised_hold",
    },
    "conflicted": {
        "label": "糾結",
        "primary_emotion": "conflicted",
        "emotion": "conflicted",
        "performance_mode": "volatile",
        "intensity": 0.68,
        "energy": 0.58,
        "playfulness": 0.12,
        "warmth": 0.24,
        "dominance": 0.46,
        "arc": "steady",
        "hold_ms": 2200,
        "speaking_rate": 0.98,
        "blink_style": "focused_pause",
    },
}

DEBUG_MOTION_RULES: dict[str, dict[str, str]] = {
    "buoyant_bounce": {
        "label": "上浮彈跳",
        "expression": "happy",
        "motion_theme": "happy_bright_talk",
        "motion_variant": "buoyant_bounce",
    },
    "side_sway_bounce": {
        "label": "左右彈跳",
        "expression": "happy",
        "motion_theme": "happy_bright_talk",
        "motion_variant": "side_sway_bounce",
    },
    "lean_in_pop": {
        "label": "前傾彈出",
        "expression": "happy",
        "motion_theme": "happy_bright_talk",
        "motion_variant": "lean_in_pop",
    },
    "swing_tease": {
        "label": "調皮擺動",
        "expression": "playful",
        "motion_theme": "playful_tease",
        "motion_variant": "swing_tease",
    },
    "locked_glare": {
        "label": "瞪視鎖定",
        "expression": "angry",
        "motion_theme": "angry_tension",
        "motion_variant": "locked_glare",
    },
    "tuck_side_sway": {
        "label": "縮肩側擺",
        "expression": "shy",
        "motion_theme": "shy_tucked",
        "motion_variant": "tuck_side_sway",
    },
}


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _resolve_intensity(intensity: str | None) -> str:
    if intensity in DEBUG_INTENSITY_SCALE:
        return intensity
    return "normal"


def _resolve_expression_kind(kind: str | None, randomize: bool) -> str:
    if randomize or not kind or kind == "random":
        return random.choice(DEBUG_EXPRESSION_KINDS)
    if kind not in DEBUG_EXPRESSION_RULES:
        raise ValueError(f"Unknown debug expression kind: {kind}")
    return kind


def build_fake_expression_debug_case(
    *,
    kind: str | None = None,
    motion_kind: str | None = None,
    intensity: str | None = None,
    randomize: bool = False,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Build a fake Expression Agent JSON reply plus fake spoken text."""

    selected_intensity = _resolve_intensity(intensity)
    selected_motion = DEBUG_MOTION_RULES.get(motion_kind or "")
    selected_kind = selected_motion["expression"] if selected_motion else _resolve_expression_kind(kind, randomize)
    rule = deepcopy(DEBUG_EXPRESSION_RULES[selected_kind])
    scale = DEBUG_INTENSITY_SCALE[selected_intensity]

    label = str(rule.pop("label"))
    intent = {
        **rule,
        "intensity": _clamp(float(rule["intensity"]) * scale),
        "energy": _clamp(float(rule["energy"]) * (0.9 + (scale * 0.12))),
        "playfulness": _clamp(float(rule["playfulness"]) * scale),
        "warmth": _clamp(float(rule["warmth"]) * (0.92 + (scale * 0.08))),
        "dominance": _clamp(float(rule["dominance"]) * (0.94 + (scale * 0.08))),
        "hold_ms": round(float(rule["hold_ms"]) * (1.12 if selected_intensity == "strong" else 0.88 if selected_intensity == "soft" else 1.0)),
        "topic_guard": {
            "source_theme": "daily_talk",
            "must_preserve_theme": True,
        },
    }

    if selected_intensity == "strong":
        intent["speaking_rate"] = min(1.18, float(rule["speaking_rate"]) + 0.06)
    elif selected_intensity == "soft":
        intent["speaking_rate"] = max(0.84, float(rule["speaking_rate"]) - 0.05)

    spoken_text = f"後端假 AI 回覆：正在測試 {label} 動作，請保留主要表情並讓動作連續。"

    if selected_motion:
        intent["motion_theme"] = selected_motion["motion_theme"]
        intent["motion_variant"] = selected_motion["motion_variant"]
        label = str(selected_motion["label"])
        spoken_text = f"後端假 AI 回覆：正在測試 {label} motionPlan，請保留主要表情並讓動作連續。"

    if scenario == "speaking_micro":
        intent.update(
            {
                "emotion": "happy",
                "primary_emotion": "happy",
                "performance_mode": "bright_talk",
                "arc": "steady",
                "hold_ms": 1800,
                "must_include": [
                    "brow_micro_dual_lift",
                    "brow_micro_curve_smile",
                    "brow_micro_understand_lift",
                ],
            }
        )
        label = "說話微表情"
        spoken_text = (
            "後端假 AI 回覆：測試說話期間微表情，主要開心表情要維持，"
            "同時平滑穿插眉毛、笑眼、嘴角、臉紅與呼吸小變化。"
        )
    elif scenario == "brow_eye_micro":
        intent.update(
            {
                "emotion": "teasing",
                "primary_emotion": "teasing",
                "performance_mode": "smug",
                "must_include": [
                    "brow_micro_soft_question",
                    "brow_micro_dual_lift",
                    "brow_micro_curve_smile",
                ],
            }
        )
        label = "眉眼微動"
        spoken_text = "後端假 AI 回覆：測試眉毛與眼神微動，表情保持挑釁但不要突然歸零。"

    raw_reply = json.dumps(intent, ensure_ascii=False)
    return {
        "rawReply": raw_reply,
        "spokenText": spoken_text,
        "label": label,
        "kind": selected_kind,
        "motionKind": motion_kind or "",
        "scenario": scenario or "",
        "intensity": selected_intensity,
    }
