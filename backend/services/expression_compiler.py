from copy import deepcopy

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_intent_schema import DEFAULT_INTENT
from domain.expression_presets import BASE_POSE_PRESETS
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY


def select_base_pose(intent: dict) -> str:
    primary = intent.get("primary_emotion")
    if primary == "playful":
        return "playful_smirk"
    if primary == "shy":
        return "shy_tucked"
    if primary == "surprised":
        return "surprised_open"
    return "calm_soft"


def build_micro_events(intent: dict) -> list[dict]:
    must_include = intent.get("must_include") or []
    if must_include:
        events = [deepcopy(MICRO_EVENT_LIBRARY[name]) for name in must_include if name in MICRO_EVENT_LIBRARY]
        if events:
            return events
    if intent.get("arc") == "pop_then_settle":
        return [deepcopy(MICRO_EVENT_LIBRARY["surprised_pop"])]
    return []


def _coerce_float(value: object, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        return default


def compile_expression_plan(intent: dict, model_name: str, previous_state: dict | None) -> dict:
    del model_name, previous_state
    preset_name = select_base_pose(intent)
    blink_style = intent.get("blink_style", DEFAULT_INTENT["blink_style"])
    if blink_style not in BLINK_STRATEGIES:
        blink_style = DEFAULT_INTENT["blink_style"]
    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    speaking_rate = _coerce_float(intent.get("speaking_rate", 1.0), 1.0)
    return {
        "type": "expression_plan",
        "basePose": {
            "preset": preset_name,
            "params": deepcopy(BASE_POSE_PRESETS[preset_name]),
            "durationSec": max(0.3, hold_ms / 1000.0),
        },
        "microEvents": build_micro_events(intent),
        "sequence": [],
        "blinkPlan": {
            "style": blink_style,
            "commands": deepcopy(BLINK_STRATEGIES.get(blink_style, [])),
        },
        "speakingRate": speaking_rate,
        "debug": {
            "intentPrimaryEmotion": intent.get("primary_emotion", "calm"),
            "intentArc": intent.get("arc", "steady"),
            "selectedBasePreset": preset_name,
        },
    }
