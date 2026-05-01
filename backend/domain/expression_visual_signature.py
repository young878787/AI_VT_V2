"""Visual signature selection for compiled expression plans."""

from domain.expression_compiler_rules import (
    EMOTION_PERFORMANCE_PRESET_MAP,
    SIGNATURE_TO_PRESET,
    TOPIC_GUARD_RULES,
)
from domain.expression_presets import BASE_POSE_PRESETS
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY


def _coerce_float(value: object, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        return default


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def resolve_topic_guard(emotion: str, performance_mode: str, topic_guard: dict) -> str:
    if topic_guard.get("allow_style_override", False):
        return performance_mode

    if not topic_guard.get("must_preserve_theme", True):
        return performance_mode

    source_theme = topic_guard.get("source_theme", "daily_talk")
    if source_theme not in TOPIC_GUARD_RULES:
        return performance_mode

    rules = TOPIC_GUARD_RULES[source_theme]
    if performance_mode in rules.get("forbid_modes", set()):
        return rules.get("downgrade_map", {}).get(performance_mode, "deadpan")

    return performance_mode


def resolve_effective_performance_mode(emotion: str, performance_mode: str, topic_guard: dict) -> str:
    return resolve_topic_guard(emotion, performance_mode, topic_guard)


def resolve_visual_signature(emotion: str, performance_mode: str, intent: dict) -> dict:
    signature = {
        "signature_name": "calm_soft",
        "blush_policy": "neutralize",
        "eye_shape": "open",
        "eye_alignment": "inherit",
        "brow_pattern": "calm",
        "mouth_pattern": "flat",
        "asymmetry_strength": 0.0,
        "event_bias": [],
    }

    mode_defaults = {
        "smile": ("happy_soft", "keep", "open", "calm", "smile", 0.0, []),
        "bright_talk": ("bright_talk", "keep", "open", "calm", "smile", 0.1, ["uneven_brow_pop"]),
        "goofy_face": ("goofy_asym", "boost", "soft_squint", "one_up_one_down", "smile", 0.85, ["goofy_eye_cross_bias"]),
        "cheeky_wink": ("cheeky_wink", "keep", "soft_squint", "one_up_one_down", "smirk", 0.65, ["wink_left"]),
        "smug": ("smug_tease", "neutralize", "soft_squint", "asymmetric_tense", "smirk", 0.45, ["smirk_left"]),
        "deadpan": ("gloomy_deadpan", "drop", "soft_squint", "calm", "flat", 0.05, ["gloom_drop"]),
        "gloomy": ("gloomy_deadpan", "drop", "soft_squint", "sad_inner", "downturned", 0.1, ["gloom_drop"]),
        "volatile": ("volatile_unstable", "neutralize", "soft_squint", "asymmetric_tense", "flat", 0.7, ["volatile_twitch"]),
        "meltdown": ("angry_meltdown", "drop", "hard_squint", "frown", "downturned", 0.9, ["meltdown_warp"]),
        "awkward": ("awkward_stuck", "keep", "open", "one_up_one_down", "flat", 0.35, ["awkward_freeze"]),
        "tense_hold": ("sad_tense", "drop", "hard_squint", "sad_inner", "downturned", 0.2, ["tense_squeeze"]),
        "shock_recoil": ("surprised_shock", "neutralize", "wide", "calm", "open_shock", 0.15, ["shock_pop"]),
        "shy": ("shy_tucked", "boost", "soft_squint", "sad_inner", "smile", 0.2, ["awkward_freeze"]),
    }

    mode_default = mode_defaults.get(performance_mode)
    if mode_default:
        (
            signature["signature_name"],
            signature["blush_policy"],
            signature["eye_shape"],
            signature["brow_pattern"],
            signature["mouth_pattern"],
            signature["asymmetry_strength"],
            event_bias,
        ) = mode_default
        signature["event_bias"] = list(event_bias)

    if emotion == "angry":
        angry_intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
        signature["signature_name"] = "angry_meltdown"
        signature["blush_policy"] = "drop"
        signature["brow_pattern"] = "frown"
        signature["mouth_pattern"] = "downturned" if performance_mode != "deadpan" else "flat"
        if angry_intensity >= 0.78:
            signature["eye_shape"] = "fierce_wide"
            signature["eye_alignment"] = "sync_stare"
            signature["asymmetry_strength"] = min(signature["asymmetry_strength"], 0.2)
        else:
            signature["eye_shape"] = "hard_squint"
            signature["asymmetry_strength"] = max(
                signature["asymmetry_strength"],
                0.55 if performance_mode == "meltdown" else 0.35,
            )
        _append_unique(signature["event_bias"], "tense_squeeze")
    elif emotion == "sad":
        signature["signature_name"] = "sad_tense" if performance_mode in {"tense_hold", "sad"} else signature["signature_name"]
        signature["blush_policy"] = "drop"
        signature["eye_shape"] = "soft_squint"
        signature["brow_pattern"] = "sad_inner"
        signature["mouth_pattern"] = "downturned"
        _append_unique(signature["event_bias"], "tense_squeeze")
    elif emotion == "gloomy":
        signature["signature_name"] = "gloomy_deadpan"
        signature["blush_policy"] = "drop"
        signature["eye_shape"] = "soft_squint"
        signature["brow_pattern"] = "sad_inner"
        signature["mouth_pattern"] = "downturned"
        _append_unique(signature["event_bias"], "gloom_drop")
    elif emotion == "happy":
        signature["signature_name"] = "bright_talk" if performance_mode == "bright_talk" else "happy_soft"
        signature["blush_policy"] = "keep"
        signature["mouth_pattern"] = "smile"
    elif emotion == "playful":
        if performance_mode == "goofy_face":
            signature["signature_name"] = "goofy_asym"
            signature["brow_pattern"] = "one_up_one_down"
            signature["asymmetry_strength"] = max(signature["asymmetry_strength"], 0.8)
            signature["blush_policy"] = "boost"
        else:
            signature["blush_policy"] = "keep"
    elif emotion == "teasing":
        signature["signature_name"] = "cheeky_wink" if performance_mode == "cheeky_wink" else "smug_tease"
        signature["blush_policy"] = "keep"
    elif emotion == "surprised":
        signature["signature_name"] = "surprised_shock"
        signature["eye_shape"] = "wide"
        signature["mouth_pattern"] = "open_shock"
        _append_unique(signature["event_bias"], "shock_pop")
    elif emotion == "neutral":
        if performance_mode == "smile":
            signature["signature_name"] = "calm_soft"
            signature["blush_policy"] = "neutralize"
            signature["eye_shape"] = "open"
            signature["brow_pattern"] = "calm"
            signature["mouth_pattern"] = "flat"
            signature["asymmetry_strength"] = 0.0
            signature["event_bias"] = []
        elif performance_mode == "bright_talk":
            signature["signature_name"] = "bright_talk"
            signature["blush_policy"] = "neutralize"
    elif emotion == "conflicted":
        signature["signature_name"] = "volatile_unstable"
        signature["brow_pattern"] = "asymmetric_tense"
        signature["asymmetry_strength"] = max(signature["asymmetry_strength"], 0.65)
        _append_unique(signature["event_bias"], "volatile_twitch")
    elif emotion == "shy":
        signature["signature_name"] = "shy_tucked"
        signature["blush_policy"] = "boost"
        signature["eye_shape"] = "soft_squint"

    must_include = intent.get("must_include") or []
    for event_name in must_include:
        if event_name in MICRO_EVENT_LIBRARY:
            _append_unique(signature["event_bias"], event_name)

    return signature


def select_base_pose(
    emotion: str,
    performance_mode: str,
    model_name: str = "Hiyori",
    signature: dict | None = None,
) -> str:
    del model_name
    if signature:
        preset_from_signature = SIGNATURE_TO_PRESET.get(signature.get("signature_name", ""))
        if preset_from_signature in BASE_POSE_PRESETS:
            return preset_from_signature

    key = (emotion, performance_mode)
    if key in EMOTION_PERFORMANCE_PRESET_MAP:
        return EMOTION_PERFORMANCE_PRESET_MAP[key]

    emotion_only_map = {
        "happy": "happy_smile_soft",
        "playful": "happy_smile_soft",
        "teasing": "teasing_smug",
        "angry": "angry_meltdown",
        "sad": "sad_tense_hold",
        "gloomy": "gloomy_deadpan",
        "shy": "shy_tucked",
        "surprised": "surprised_open",
        "conflicted": "conflicted_volatile",
        "neutral": "calm_soft",
    }
    return emotion_only_map.get(emotion, "calm_soft")
