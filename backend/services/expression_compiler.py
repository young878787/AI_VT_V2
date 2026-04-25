from copy import deepcopy

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_intent_schema import DEFAULT_INTENT
from domain.expression_presets import BASE_POSE_PRESETS, PRESET_VARIATION_RULES
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY


def select_base_pose(intent: dict, model_name: str = "Hiyori") -> str:
    del model_name
    primary = intent.get("primary_emotion")
    if primary == "playful":
        return "playful_smirk"
    if primary == "shy":
        return "shy_tucked"
    if primary == "surprised":
        return "surprised_open"
    return "calm_soft"


def build_micro_events(intent: dict, base_pose: dict | None = None, model_name: str = "Hiyori") -> list[dict]:
    del base_pose, model_name
    must_include = intent.get("must_include") or []
    if must_include:
        events = [deepcopy(MICRO_EVENT_LIBRARY[name]) for name in must_include if name in MICRO_EVENT_LIBRARY]
        if events:
            return events
    if intent.get("arc") == "pop_then_settle":
        return [deepcopy(MICRO_EVENT_LIBRARY["surprised_pop"])]
    if intent.get("arc") == "widen_then_tease":
        return [deepcopy(MICRO_EVENT_LIBRARY["surprised_pop"])]
    return []


def build_expression_sequence(intent: dict, base_pose: dict, model_name: str) -> list[dict]:
    del model_name
    arc = intent.get("arc", "steady")
    sequence: list[dict] = []

    if arc == "pause_then_smirk":
        smirk = deepcopy(MICRO_EVENT_LIBRARY["smirk_left"])
        if base_pose.get("preset") == "playful_smirk":
            smirk["durationMs"] = max(smirk["durationMs"], 640)
        sequence.append(smirk)

    return sequence


def build_blink_plan(intent: dict, model_name: str) -> dict:
    del model_name
    blink_style = intent.get("blink_style", DEFAULT_INTENT["blink_style"])
    if blink_style not in BLINK_STRATEGIES:
        blink_style = DEFAULT_INTENT["blink_style"]
    return {
        "style": blink_style,
        "commands": deepcopy(BLINK_STRATEGIES.get(blink_style, [])),
    }


def apply_base_pose_modifiers(intent: dict, base_pose: dict) -> dict:
    params = deepcopy(base_pose["params"])

    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)
    playfulness = _coerce_float(intent.get("playfulness", 0.3), 0.3)
    warmth = _coerce_float(intent.get("warmth", 0.5), 0.5)
    dominance = _coerce_float(intent.get("dominance", 0.5), 0.5)
    asymmetry_bias = intent.get("asymmetry_bias", "auto")

    params["mouthForm"] = min(0.95, params["mouthForm"] + (intensity * 0.14) + (playfulness * 0.12) + (warmth * 0.08))
    params["headIntensity"] = min(0.95, params["headIntensity"] + (intensity * 0.18) + (energy * 0.08))
    params["eyeLSmile"] = min(1.0, params["eyeLSmile"] + (playfulness * 0.18) + (warmth * 0.12))
    params["eyeRSmile"] = min(1.0, params["eyeRSmile"] + (playfulness * 0.1) + (warmth * 0.08))
    params["browLY"] = min(1.0, params["browLY"] + (energy * 0.12) + ((1.0 - dominance) * 0.04))
    params["browRY"] = min(1.0, params["browRY"] + (energy * 0.08) + ((1.0 - dominance) * 0.02))
    params["browLAngle"] = max(-1.0, min(1.0, params["browLAngle"] + (dominance - 0.5) * 0.18 + playfulness * 0.08))
    params["browRAngle"] = max(-1.0, min(1.0, params["browRAngle"] - (dominance - 0.5) * 0.16 - playfulness * 0.05))
    params["eyeLOpen"] = max(0.45, min(1.25, params["eyeLOpen"] + (energy * 0.08) - (warmth * 0.02)))
    params["eyeROpen"] = max(0.4, min(1.25, params["eyeROpen"] + (energy * 0.02) - (warmth * 0.04)))

    if asymmetry_bias == "strong":
        params["eyeSync"] = False
        params["eyeLOpen"] = max(0.4, min(1.25, params["eyeLOpen"] + 0.06))
        params["eyeROpen"] = max(0.35, min(1.25, params["eyeROpen"] - 0.08))
        params["browLX"] = max(-1.0, min(1.0, params["browLX"] - (0.06 + playfulness * 0.06)))
        params["browRX"] = max(-1.0, min(1.0, params["browRX"] + (0.03 + playfulness * 0.03)))

    return {
        **base_pose,
        "params": params,
    }


def build_timing_hints(intent: dict, base_pose: dict, sequence: list[dict]) -> dict:
    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    return {
        "holdMs": hold_ms,
        "basePoseDurationSec": base_pose["durationSec"],
        "sequenceSteps": len(sequence),
    }


def build_model_hints(intent: dict, preset_name: str, model_name: str) -> dict:
    return {
        "modelName": model_name,
        "preset": preset_name,
        "variationRuleCount": len(PRESET_VARIATION_RULES.get(preset_name, {})),
        "asymmetryBias": intent.get("asymmetry_bias", "auto"),
    }


def _coerce_float(value: object, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        return default


def compile_expression_plan(intent: dict, model_name: str, previous_state: dict | None) -> dict:
    del previous_state
    preset_name = select_base_pose(intent, model_name=model_name)
    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    speaking_rate = _coerce_float(intent.get("speaking_rate", 1.0), 1.0)
    base_pose = {
        "preset": preset_name,
        "params": deepcopy(BASE_POSE_PRESETS[preset_name]),
        "durationSec": max(0.3, hold_ms / 1000.0),
    }
    base_pose = apply_base_pose_modifiers(intent, base_pose)
    micro_events = build_micro_events(intent, base_pose=base_pose, model_name=model_name)
    sequence = build_expression_sequence(intent, base_pose=base_pose, model_name=model_name)
    blink_plan = build_blink_plan(intent, model_name=model_name)
    return {
        "type": "expression_plan",
        "basePose": base_pose,
        "microEvents": micro_events,
        "sequence": sequence,
        "blinkPlan": blink_plan,
        "speakingRate": speaking_rate,
        "timingHints": build_timing_hints(intent, base_pose=base_pose, sequence=sequence),
        "modelHints": build_model_hints(intent, preset_name=preset_name, model_name=model_name),
        "debug": {
            "intentPrimaryEmotion": intent.get("primary_emotion", "calm"),
            "intentArc": intent.get("arc", "steady"),
            "selectedBasePreset": preset_name,
        },
    }
