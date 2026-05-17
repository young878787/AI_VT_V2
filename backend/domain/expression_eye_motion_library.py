"""Eye-gaze motion presets for expression plans."""

import random


EYE_MOTION_STYLES = {
    "none",
    "soft_saccade",
    "nervous_tremor",
    "alert_scan",
    "locked_stare",
    "dizzy_dart",
}


EYE_MOTION_PRESETS = {
    "none": {
        "amplitudeX": 0.0,
        "amplitudeY": 0.0,
        "frequencyHz": 0.5,
        "blendInMs": 120,
        "blendOutMs": 180,
    },
    "soft_saccade": {
        "amplitudeX": 0.12,
        "amplitudeY": 0.04,
        "frequencyHz": 0.62,
        "blendInMs": 260,
        "blendOutMs": 520,
    },
    "nervous_tremor": {
        "amplitudeX": 0.08,
        "amplitudeY": 0.035,
        "frequencyHz": 6.8,
        "blendInMs": 180,
        "blendOutMs": 420,
    },
    "alert_scan": {
        "amplitudeX": 0.22,
        "amplitudeY": 0.06,
        "frequencyHz": 1.45,
        "blendInMs": 140,
        "blendOutMs": 360,
    },
    "locked_stare": {
        "amplitudeX": 0.045,
        "amplitudeY": 0.018,
        "frequencyHz": 7.2,
        "blendInMs": 220,
        "blendOutMs": 520,
    },
    "dizzy_dart": {
        "amplitudeX": 0.24,
        "amplitudeY": 0.09,
        "frequencyHz": 2.15,
        "blendInMs": 160,
        "blendOutMs": 440,
    },
}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _coerce_float(value: object, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        return default


def resolve_eye_motion_style(emotion: str, performance_mode: str, intent: dict) -> str:
    override = str(intent.get("eye_motion_style") or "").strip()
    if override in EYE_MOTION_STYLES:
        return override

    if performance_mode == "goofy_face":
        return "dizzy_dart"
    if emotion == "angry" or performance_mode in {"meltdown", "volatile"}:
        return "locked_stare"
    if emotion == "surprised" or performance_mode == "shock_recoil":
        return "alert_scan"
    if emotion in {"conflicted", "shy"} or performance_mode in {"tense_hold", "awkward"}:
        return "nervous_tremor"
    if emotion in {"happy", "playful", "teasing", "neutral"}:
        return "soft_saccade"
    return "none"


def build_eye_motion_plan(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    intent: dict,
    action_duration_ms: int | None = None,
) -> dict:
    style = resolve_eye_motion_style(emotion, performance_mode, intent)
    preset = EYE_MOTION_PRESETS[style]
    hold_ms = int(_clamp(_coerce_float(intent.get("hold_ms", 1600), 1600.0), 400.0, 12000.0))
    duration_ms = max(900, hold_ms + 900, int(action_duration_ms or 0))
    duration_ms = min(14000, duration_ms)

    resolved_intensity = _coerce_float(intent.get("eye_motion_intensity", None), -1.0)
    if resolved_intensity < 0:
        resolved_intensity = 0.22 + (intensity * 0.55) + (energy * 0.18)
    if style == "locked_stare":
        resolved_intensity *= 0.78
    elif style == "dizzy_dart":
        resolved_intensity = max(resolved_intensity, 0.68)
    elif style == "none":
        resolved_intensity = 0.0

    energy_scale = 0.82 + (_clamp(energy, 0.0, 1.0) * 0.28)
    return {
        "style": style,
        "intensity": round(_clamp(resolved_intensity, 0.0, 1.0), 3),
        "durationMs": duration_ms,
        "blendInMs": preset["blendInMs"],
        "blendOutMs": min(preset["blendOutMs"], max(160, duration_ms // 3)),
        "amplitudeX": round(_clamp(preset["amplitudeX"] * energy_scale, -1.0, 1.0), 3),
        "amplitudeY": round(_clamp(preset["amplitudeY"] * energy_scale, -1.0, 1.0), 3),
        "frequencyHz": round(max(0.05, preset["frequencyHz"]), 3),
        "phaseSeed": round(random.random(), 3),
    }
