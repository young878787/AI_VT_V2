from copy import deepcopy

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_intent_schema import DEFAULT_INTENT
from domain.expression_presets import BASE_POSE_PRESETS, PRESET_VARIATION_RULES
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY, SEQUENCE_LIBRARY


EMOTION_PERFORMANCE_PRESET_MAP = {
    ("happy", "smile"): "happy_smile_soft",
    ("happy", "bright_talk"): "happy_bright_talk",
    ("playful", "smile"): "happy_smile_soft",
    ("playful", "bright_talk"): "happy_bright_talk",
    ("playful", "goofy_face"): "playful_goofy_face",
    ("playful", "cheeky_wink"): "teasing_cheeky_wink",
    ("teasing", "smile"): "teasing_smug",
    ("teasing", "cheeky_wink"): "teasing_cheeky_wink",
    ("teasing", "smug"): "teasing_smug",
    ("teasing", "bright_talk"): "teasing_smug",
    ("gloomy", "gloomy"): "gloomy_deadpan",
    ("gloomy", "deadpan"): "gloomy_deadpan",
    ("gloomy", "awkward"): "gloomy_deadpan",
    ("sad", "sad"): "sad_tense_hold",
    ("sad", "tense_hold"): "sad_tense_hold",
    ("sad", "awkward"): "awkward_stuck",
    ("sad", "gloomy"): "gloomy_deadpan",
    ("angry", "angry"): "angry_meltdown",
    ("angry", "meltdown"): "angry_meltdown",
    ("angry", "volatile"): "conflicted_volatile",
    ("angry", "deadpan"): "gloomy_deadpan",
    ("conflicted", "volatile"): "conflicted_volatile",
    ("conflicted", "meltdown"): "angry_meltdown",
    ("conflicted", "awkward"): "awkward_stuck",
    ("shy", "awkward"): "awkward_stuck",
    ("shy", "shy"): "shy_tucked",
    ("surprised", "shock_recoil"): "surprised_open",
    ("surprised", "bright_talk"): "surprised_open",
    ("neutral", "smile"): "calm_soft",
    ("neutral", "deadpan"): "gloomy_deadpan",
    ("neutral", "bright_talk"): "happy_bright_talk",
    ("neutral", "awkward"): "awkward_stuck",
}


TOPIC_GUARD_RULES = {
    "crying": {
        "forbid_modes": {"goofy_face", "bright_talk", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "awkward",
            "bright_talk": "awkward",
            "cheeky_wink": "awkward",
            "smug": "tense_hold",
        },
    },
    "gloomy": {
        "forbid_modes": {"goofy_face", "bright_talk", "smile", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "deadpan",
            "bright_talk": "deadpan",
            "smile": "deadpan",
            "cheeky_wink": "deadpan",
            "smug": "gloomy",
        },
    },
    "serious_argument": {
        "forbid_modes": {"goofy_face", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "deadpan",
            "cheeky_wink": "smug",
            "bright_talk": "smile",
        },
    },
}


SIGNATURE_TO_PRESET = {
    "happy_soft": "happy_smile_soft",
    "bright_talk": "happy_bright_talk",
    "goofy_asym": "playful_goofy_face",
    "cheeky_wink": "teasing_cheeky_wink",
    "smug_tease": "teasing_smug",
    "gloomy_deadpan": "gloomy_deadpan",
    "sad_tense": "sad_tense_hold",
    "angry_meltdown": "angry_meltdown",
    "volatile_unstable": "conflicted_volatile",
    "awkward_stuck": "awkward_stuck",
    "surprised_shock": "surprised_open",
    "shy_tucked": "shy_tucked",
    "calm_soft": "calm_soft",
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


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


CONTINUITY_FAMILY_MAP = {
    "happy": "warm",
    "playful": "warm",
    "teasing": "warm",
    "angry": "tension",
    "conflicted": "tension",
    "sad": "low",
    "gloomy": "low",
    "shy": "shy",
    "surprised": "reactive",
    "neutral": "neutral",
}

CONTINUITY_PARAM_WEIGHTS = {
    "headIntensity": 0.25,
    "blushLevel": 0.65,
    "eyeLOpen": 0.38,
    "eyeROpen": 0.38,
    "mouthForm": 0.55,
    "browLY": 0.45,
    "browRY": 0.45,
    "browLAngle": 0.48,
    "browRAngle": 0.48,
    "browLForm": 0.52,
    "browRForm": 0.52,
    "eyeLSmile": 0.5,
    "eyeRSmile": 0.5,
    "browLX": 0.42,
    "browRX": 0.42,
}


IDLE_PLAN_LOOP_EVENTS = {
    "happy_idle": [
        {
            "kind": "happy_idle_soft_smile",
            "durationMs": 760,
            "patch": {"mouthForm": 0.26, "eyeLSmile": 0.42, "eyeRSmile": 0.42},
            "returnToBase": True,
        },
    ],
    "crying_idle": [
        {
            "kind": "crying_idle_sad_breath",
            "durationMs": 920,
            "patch": {"eyeLOpen": 0.60, "eyeROpen": 0.62, "mouthForm": -0.30, "browLY": -0.04, "browRY": -0.04},
            "returnToBase": True,
        },
    ],
    "angry_glare_idle": [
        {
            "kind": "angry_idle_glare_hold",
            "durationMs": 820,
            "patch": {"eyeLOpen": 1.08, "eyeROpen": 1.08, "browLAngle": 0.48, "browRAngle": -0.48, "mouthForm": -0.16},
            "returnToBase": True,
        },
    ],
    "shy_idle": [
        {
            "kind": "shy_idle_quick_peek",
            "durationMs": 700,
            "patch": {"eyeLOpen": 0.74, "eyeROpen": 0.86, "mouthForm": 0.12, "blushLevel": 0.22},
            "returnToBase": True,
        },
    ],
    "gloomy_idle": [
        {
            "kind": "gloomy_idle_downcast",
            "durationMs": 1040,
            "patch": {"eyeLOpen": 0.56, "eyeROpen": 0.58, "mouthForm": -0.16, "browLY": -0.16, "browRY": -0.16},
            "returnToBase": True,
        },
    ],
}


IDLE_PLAN_SETTLE_PATCHES = {
    "happy_idle": {
        "headIntensity": 0.04,
        "mouthForm": 0.22,
        "eyeLOpen": 0.88,
        "eyeROpen": 0.88,
        "eyeSync": True,
        "eyeLSmile": 0.34,
        "eyeRSmile": 0.34,
        "blushLevel": 0.04,
    },
    "crying_idle": {
        "headIntensity": 0.02,
        "mouthForm": -0.26,
        "eyeLOpen": 0.66,
        "eyeROpen": 0.68,
        "eyeSync": True,
        "browLY": -0.02,
        "browRY": -0.02,
        "browLAngle": -0.16,
        "browRAngle": 0.16,
        "browLForm": -0.12,
        "browRForm": -0.12,
        "blushLevel": -0.35,
    },
    "angry_glare_idle": {
        "headIntensity": 0.06,
        "mouthForm": -0.14,
        "eyeLOpen": 1.02,
        "eyeROpen": 1.02,
        "eyeSync": True,
        "browLY": -0.08,
        "browRY": -0.08,
        "browLAngle": 0.44,
        "browRAngle": -0.44,
        "browLForm": -0.22,
        "browRForm": -0.22,
        "blushLevel": -0.45,
    },
    "shy_idle": {
        "headIntensity": 0.03,
        "mouthForm": 0.10,
        "eyeLOpen": 0.76,
        "eyeROpen": 0.82,
        "eyeSync": False,
        "eyeLSmile": 0.18,
        "eyeRSmile": 0.10,
        "browLY": 0.06,
        "browRY": 0.02,
        "blushLevel": 0.20,
    },
    "gloomy_idle": {
        "headIntensity": 0.02,
        "mouthForm": -0.14,
        "eyeLOpen": 0.66,
        "eyeROpen": 0.68,
        "eyeSync": True,
        "browLY": -0.14,
        "browRY": -0.14,
        "browLAngle": -0.10,
        "browRAngle": 0.10,
        "browLForm": -0.08,
        "browRForm": -0.08,
        "blushLevel": -0.20,
    },
}


def _read_previous_state_string(previous_state: dict | None, *keys: str) -> str | None:
    if not isinstance(previous_state, dict):
        return None

    for key in keys:
        value = previous_state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _read_previous_state_float(previous_state: dict | None, *keys: str, default: float = 0.0) -> float:
    if not isinstance(previous_state, dict):
        return default

    for key in keys:
        value = previous_state.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)

    return default


def _read_previous_state_bool(previous_state: dict | None, *keys: str, default: bool = True) -> bool:
    if not isinstance(previous_state, dict):
        return default

    for key in keys:
        value = previous_state.get(key)
        if isinstance(value, bool):
            return value

    return default


def _emotion_family(emotion: str) -> str:
    return CONTINUITY_FAMILY_MAP.get(emotion, "neutral")


def _resolve_previous_state_residue(previous_state: dict | None) -> float:
    residue = _read_previous_state_float(previous_state, "residue", "continuity", "carryResidue", default=0.0)
    if residue > 0.0:
        return _clamp(residue, 0.0, 1.0)

    if not isinstance(previous_state, dict):
        return 0.0

    if _read_previous_state_string(previous_state, "summary"):
        return 0.25

    has_pose_data = any(
        key in previous_state
        for key in (
            "mouthForm",
            "eyeLOpen",
            "eyeROpen",
            "browLY",
            "browRY",
            "browLAngle",
            "browRAngle",
            "browLForm",
            "browRForm",
        )
    )
    return 0.22 if has_pose_data else 0.0


def _resolve_continuity_blend(
    previous_state: dict | None,
    emotion: str,
    performance_mode: str,
    intensity: float,
    signature_name: str,
) -> float:
    if not isinstance(previous_state, dict):
        return 0.0

    residue = _resolve_previous_state_residue(previous_state)
    if residue <= 0.0:
        return 0.0

    previous_emotion = _read_previous_state_string(
        previous_state,
        "emotion",
        "intentEmotion",
        "intentPrimaryEmotion",
        "primary_emotion",
    )
    previous_mode = _read_previous_state_string(
        previous_state,
        "performanceMode",
        "intentPerformanceMode",
        "performance_mode",
    )
    previous_signature = _read_previous_state_string(previous_state, "signature", "signature_name")

    contextual_blend = 0.08
    if previous_emotion == emotion:
        contextual_blend = 0.32
    elif previous_emotion and _emotion_family(previous_emotion) == _emotion_family(emotion):
        contextual_blend = 0.18
    elif previous_emotion:
        contextual_blend = 0.10

    if previous_mode == performance_mode:
        contextual_blend += 0.08
    if previous_signature == signature_name:
        contextual_blend += 0.05

    if emotion == "angry" and intensity >= 0.78:
        contextual_blend = min(contextual_blend, 0.08)
    elif performance_mode in {"goofy_face", "meltdown", "shock_recoil"}:
        contextual_blend = min(contextual_blend, 0.12)

    return _clamp(min(residue, contextual_blend), 0.0, 0.35)


def _apply_previous_state_continuity(params: dict, previous_state: dict | None, blend: float) -> dict:
    if blend <= 0.0 or not isinstance(previous_state, dict):
        return params

    for key, weight in CONTINUITY_PARAM_WEIGHTS.items():
        previous_value = previous_state.get(key)
        if isinstance(previous_value, bool) or not isinstance(previous_value, (int, float)):
            continue
        if key == "eyeLOpen" and not bool(params.get("eyeSync", True)):
            effective_blend = blend * min(weight, 0.25)
        elif key == "eyeROpen" and not bool(params.get("eyeSync", True)):
            effective_blend = blend * min(weight, 0.25)
        else:
            effective_blend = blend * weight
        params[key] = (params[key] * (1.0 - effective_blend)) + (float(previous_value) * effective_blend)

    previous_eye_sync = _read_previous_state_bool(previous_state, "eyeSync", "eye_sync", default=True)
    if previous_eye_sync is False and bool(params.get("eyeSync", True)) is False:
        params["eyeSync"] = False

    return params


def _build_carry_state(
    intent: dict,
    signature: dict,
    params: dict,
    continuity_blend: float,
) -> dict:
    emotion = intent.get("emotion", intent.get("primary_emotion", "neutral"))
    performance_mode = intent.get("performance_mode", "smile")
    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)
    residue = _clamp(0.14 + (continuity_blend * 0.55) + (intensity * 0.10) + (energy * 0.06), 0.08, 0.45)
    if emotion in {"sad", "gloomy", "angry"}:
        residue = _clamp(residue + 0.05, 0.08, 0.5)
    if performance_mode in {"meltdown", "goofy_face", "shock_recoil"}:
        residue = _clamp(residue + 0.03, 0.08, 0.5)

    return {
        "emotion": emotion,
        "performanceMode": performance_mode,
        "signature": signature.get("signature_name", "calm_soft"),
        "residue": residue,
        "headIntensity": params["headIntensity"],
        "blushLevel": params["blushLevel"],
        "eyeSync": params["eyeSync"],
        "eyeLOpen": params["eyeLOpen"],
        "eyeROpen": params["eyeROpen"],
        "mouthForm": params["mouthForm"],
        "browLY": params["browLY"],
        "browRY": params["browRY"],
        "browLAngle": params["browLAngle"],
        "browRAngle": params["browRAngle"],
        "browLForm": params["browLForm"],
        "browRForm": params["browRForm"],
        "eyeLSmile": params["eyeLSmile"],
        "eyeRSmile": params["eyeRSmile"],
        "browLX": params["browLX"],
        "browRX": params["browRX"],
    }


def _clamp_expression_params(params: dict) -> dict:
    params["blushLevel"] = _clamp(params["blushLevel"], -1.0, 1.0)
    params["eyeLOpen"] = _clamp(params["eyeLOpen"], 0.0, 1.25)
    params["eyeROpen"] = _clamp(params["eyeROpen"], 0.0, 1.25)
    params["mouthForm"] = _clamp(params["mouthForm"], -2.0, 0.95)
    params["browLY"] = _clamp(params["browLY"], -1.0, 1.0)
    params["browRY"] = _clamp(params["browRY"], -1.0, 1.0)
    params["browLAngle"] = _clamp(params["browLAngle"], -1.0, 1.0)
    params["browRAngle"] = _clamp(params["browRAngle"], -1.0, 1.0)
    params["browLForm"] = _clamp(params["browLForm"], -1.0, 1.0)
    params["browRForm"] = _clamp(params["browRForm"], -1.0, 1.0)
    params["eyeLSmile"] = _clamp(params["eyeLSmile"], 0.0, 1.0)
    params["eyeRSmile"] = _clamp(params["eyeRSmile"], 0.0, 1.0)
    params["browLX"] = _clamp(params["browLX"], -1.0, 1.0)
    params["browRX"] = _clamp(params["browRX"], -1.0, 1.0)
    params["headIntensity"] = _clamp(params["headIntensity"], 0.0, 0.95)
    return params


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
            # High-intensity anger should read as a direct "stare down" instead of contempt squint.
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


def _apply_blush_policy(params: dict, blush_policy: str, intensity: float, warmth: float) -> None:
    if blush_policy == "boost":
        params["blushLevel"] += 0.12 + warmth * 0.16 + intensity * 0.08
    elif blush_policy == "keep":
        params["blushLevel"] += 0.04 + warmth * 0.05
    elif blush_policy == "drop":
        params["blushLevel"] -= 0.10 + (1.0 - warmth) * 0.20 + intensity * 0.10
    else:
        params["blushLevel"] *= 0.6


def apply_visual_signature(
    params: dict,
    signature: dict,
    intensity: float,
    energy: float,
    warmth: float,
) -> dict:
    eye_sync = bool(params.get("eyeSync", True))
    asymmetry_strength = _clamp(float(signature.get("asymmetry_strength", 0.0)), 0.0, 1.0)
    eye_alignment = signature.get("eye_alignment", "inherit")

    if eye_alignment == "sync_stare":
        neutral_eye = (params["eyeLOpen"] + params["eyeROpen"]) * 0.5
        params["eyeSync"] = True
        params["eyeLOpen"] = neutral_eye
        params["eyeROpen"] = neutral_eye
        eye_sync = True

    _apply_blush_policy(params, signature.get("blush_policy", "neutralize"), intensity, warmth)

    eye_shape = signature.get("eye_shape", "open")
    if eye_shape == "soft_squint":
        delta = 0.08 + intensity * 0.12
        params["eyeLOpen"] -= delta
        params["eyeROpen"] -= delta * (0.9 if eye_sync else 1.0)
    elif eye_shape == "hard_squint":
        delta = 0.15 + intensity * 0.20
        params["eyeLOpen"] -= delta
        params["eyeROpen"] -= delta * (0.88 if eye_sync else 1.0)
    elif eye_shape == "wide":
        delta = 0.10 + energy * 0.18
        params["eyeLOpen"] += delta
        params["eyeROpen"] += delta * 0.95
    elif eye_shape == "fierce_wide":
        delta = 0.16 + intensity * 0.18 + energy * 0.16
        params["eyeLOpen"] += delta
        params["eyeROpen"] += delta * (0.98 if eye_sync else 0.95)

    brow_pattern = signature.get("brow_pattern", "calm")
    if brow_pattern == "frown":
        params["browLForm"] -= 0.18 + intensity * 0.22
        params["browLAngle"] += 0.10 + intensity * 0.18
        params["browLY"] -= 0.05 + energy * 0.06
        params["browLX"] += 0.06 + intensity * 0.10
        if not eye_sync:
            params["browRForm"] -= 0.14 + intensity * 0.16
            params["browRAngle"] += 0.08 + intensity * 0.14
            params["browRY"] -= 0.03 + energy * 0.05
            params["browRX"] -= 0.05 + intensity * 0.08
    elif brow_pattern == "one_up_one_down":
        params["eyeSync"] = False
        params["browLY"] += 0.14 + energy * 0.18
        params["browRY"] -= 0.08 + energy * 0.12
        params["browLAngle"] += 0.12 + intensity * 0.16
        params["browRAngle"] -= 0.06 + intensity * 0.12
        params["browLX"] -= 0.06 + intensity * 0.04
        params["browRX"] += 0.05 + intensity * 0.04
    elif brow_pattern == "sad_inner":
        params["browLAngle"] -= 0.08 + intensity * 0.12
        params["browLX"] += 0.04 + intensity * 0.08
        params["browLY"] -= 0.02 + energy * 0.04
        if not eye_sync:
            params["browRAngle"] += 0.06 + intensity * 0.10
            params["browRX"] -= 0.04 + intensity * 0.08
            params["browRY"] -= 0.02 + energy * 0.03
    elif brow_pattern == "asymmetric_tense":
        params["eyeSync"] = False
        params["browLY"] += 0.08 + energy * 0.10
        params["browRY"] -= 0.06 + energy * 0.08
        params["browLForm"] -= 0.06 + intensity * 0.08
        params["browRForm"] += 0.02 + intensity * 0.04
        params["browLAngle"] += 0.08 + intensity * 0.10
        params["browRAngle"] -= 0.05 + intensity * 0.08

    mouth_pattern = signature.get("mouth_pattern", "flat")
    if mouth_pattern == "smile":
        params["mouthForm"] += 0.08 + intensity * 0.12 + warmth * 0.06
    elif mouth_pattern == "smirk":
        params["mouthForm"] += 0.04 + intensity * 0.08
    elif mouth_pattern == "downturned":
        params["mouthForm"] -= 0.12 + intensity * 0.20
    elif mouth_pattern == "open_shock":
        params["mouthForm"] = max(params["mouthForm"], 0.10 + intensity * 0.08)
    else:
        params["mouthForm"] *= 0.55

    if eye_alignment != "sync_stare" and asymmetry_strength >= 0.6:
        params["eyeSync"] = False
        eye_delta = 0.05 + asymmetry_strength * 0.08
        params["eyeLOpen"] += eye_delta
        params["eyeROpen"] -= eye_delta
        params["eyeLSmile"] += 0.04 + asymmetry_strength * 0.08
        params["eyeRSmile"] -= 0.03 + asymmetry_strength * 0.05
    elif eye_alignment != "sync_stare" and asymmetry_strength >= 0.3 and not bool(params.get("eyeSync", True)):
        eye_delta = 0.02 + asymmetry_strength * 0.04
        params["eyeLOpen"] += eye_delta
        params["eyeROpen"] -= eye_delta * 0.8

    return _clamp_expression_params(params)


def apply_model_adapter(
    params: dict,
    signature: dict,
    intensity: float,
    energy: float,
    model_name: str,
) -> dict:
    if model_name.lower() != "hiyori":
        return _clamp_expression_params(params)

    eye_scale = 1.0 + (energy * 0.35)
    params["eyeLOpen"] = 1.0 + (params["eyeLOpen"] - 1.0) * eye_scale
    params["eyeROpen"] = 1.0 + (params["eyeROpen"] - 1.0) * eye_scale

    mouth_scale = 1.0 + (intensity * 0.40)
    params["mouthForm"] *= mouth_scale

    brow_scale = 1.0 + (intensity * 0.45)
    params["browLY"] *= brow_scale
    params["browLAngle"] *= brow_scale
    params["browLForm"] *= brow_scale
    params["browLX"] *= brow_scale

    eye_sync = bool(params.get("eyeSync", True))
    if not eye_sync:
        params["browRY"] *= brow_scale
        params["browRAngle"] *= brow_scale
        params["browRForm"] *= brow_scale
        params["browRX"] *= brow_scale

    signature_name = signature.get("signature_name", "")
    if signature_name in {"angry_meltdown", "sad_tense"}:
        params["browLForm"] -= 0.08 + intensity * 0.08
        params["mouthForm"] -= 0.04 + intensity * 0.10
        if not eye_sync:
            params["browRForm"] -= 0.06 + intensity * 0.06
    elif signature_name == "goofy_asym":
        params["eyeSync"] = False
        params["browLY"] += 0.06 + energy * 0.06
        params["browRY"] -= 0.06 + energy * 0.06
        params["mouthForm"] += 0.06 + intensity * 0.08

    blush_policy = signature.get("blush_policy", "neutralize")
    raw_blush = params["blushLevel"]
    if blush_policy == "drop":
        if signature_name == "angry_meltdown":
            target_blush = -0.20 - (intensity * 0.80)
        elif signature_name == "sad_tense":
            target_blush = -0.20 - (intensity * 0.75)
        elif signature_name == "gloomy_deadpan":
            target_blush = -0.20 - (intensity * 0.70)
        else:
            target_blush = -0.20 - (intensity * 0.60)
        params["blushLevel"] = _clamp(target_blush, -1.0, -0.2)
    elif blush_policy == "keep":
        positive_blush = max(raw_blush, 0.0) * 0.24
        params["blushLevel"] = _clamp(positive_blush, 0.0, 0.10)
    elif blush_policy == "boost":
        positive_blush = max(raw_blush, 0.0) * 0.30
        params["blushLevel"] = _clamp(positive_blush, 0.04, 0.16)
    else:
        params["blushLevel"] = _clamp(raw_blush * 0.20, -0.08, 0.06)

    return _clamp_expression_params(params)


def apply_base_pose_modifiers(
    intent: dict,
    base_pose: dict,
    signature: dict | None = None,
    model_name: str = "Hiyori",
    previous_state: dict | None = None,
    continuity_blend: float = 0.0,
) -> dict:
    params = deepcopy(base_pose["params"])

    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)
    playfulness = _coerce_float(intent.get("playfulness", 0.3), 0.3)
    warmth = _coerce_float(intent.get("warmth", 0.5), 0.5)
    dominance = _coerce_float(intent.get("dominance", 0.5), 0.5)
    asymmetry_bias = intent.get("asymmetry_bias", "auto")
    emotion = intent.get("emotion", intent.get("primary_emotion", "neutral"))
    performance_mode = intent.get("performance_mode", "smile")

    if signature is None:
        signature = resolve_visual_signature(emotion, performance_mode, intent)

    eye_sync = bool(params.get("eyeSync", True))

    positive = intensity * 0.14 + playfulness * 0.12 + warmth * 0.08
    negative = 0.0
    if emotion == "sad":
        negative = intensity * 0.32 + (1.0 - warmth) * 0.18
    elif emotion == "gloomy":
        negative = intensity * 0.24 + (1.0 - warmth) * 0.12
    elif emotion == "angry":
        negative = intensity * 0.12 + dominance * 0.06
    params["mouthForm"] += positive - negative

    params["headIntensity"] += (intensity * 0.18) + (energy * 0.10)
    params["eyeLSmile"] += (playfulness * 0.18) + (warmth * 0.12)
    params["eyeRSmile"] += (playfulness * 0.10) + (warmth * 0.08)
    params["browLY"] += (energy * 0.12) + ((1.0 - dominance) * 0.04)
    params["browLAngle"] += (dominance - 0.5) * 0.18 + playfulness * 0.08
    params["eyeLOpen"] += (energy * 0.08) - (warmth * 0.02)
    params["eyeROpen"] += (energy * 0.02) - (warmth * 0.04)

    if eye_sync:
        params["browRAngle"] = params["browRAngle"]
        params["browRY"] = params["browRY"]
    else:
        params["browRY"] += (energy * 0.08) + ((1.0 - dominance) * 0.02)
        params["browRAngle"] += -(dominance - 0.5) * 0.16 - playfulness * 0.05

    blush_delta = 0.0
    if emotion == "shy":
        blush_delta += 0.30 + warmth * 0.30
    elif emotion == "teasing":
        blush_delta += 0.10 + playfulness * 0.15
    elif emotion == "angry":
        blush_delta -= (1.0 - warmth) * 0.35
    elif emotion == "sad":
        blush_delta -= intensity * 0.20
    elif emotion == "surprised":
        blush_delta += 0.05
    params["blushLevel"] += blush_delta

    form_delta = 0.0
    if emotion == "angry":
        form_delta -= intensity * 0.30
    elif emotion == "sad":
        form_delta -= intensity * 0.18
    elif emotion == "surprised":
        form_delta += energy * 0.12
    elif emotion == "conflicted":
        form_delta -= intensity * 0.10
    params["browLForm"] += form_delta
    if not eye_sync:
        params["browRForm"] += form_delta + playfulness * 0.02

    inward = dominance * 0.10
    if emotion == "angry":
        inward += intensity * 0.15
    elif emotion == "sad":
        inward += intensity * 0.07
    elif emotion == "conflicted":
        inward += intensity * 0.05
    params["browLX"] += inward + playfulness * 0.02
    if not eye_sync:
        params["browRX"] -= inward + playfulness * 0.02

    if emotion == "angry":
        params["browLY"] -= energy * 0.10
        params["browLAngle"] += intensity * 0.12
        if not eye_sync:
            params["browRY"] -= energy * 0.06
            params["browRAngle"] += intensity * 0.10
    elif emotion == "sad":
        params["browLAngle"] -= intensity * 0.10
        if not eye_sync:
            params["browRAngle"] -= intensity * 0.06
    elif emotion == "surprised":
        params["browLY"] += energy * 0.12
        if not eye_sync:
            params["browRY"] += energy * 0.08

    if asymmetry_bias == "strong":
        params["eyeSync"] = False
        params["eyeLOpen"] += 0.08
        params["eyeROpen"] -= 0.10
        params["browLY"] += 0.10
        params["browRY"] -= 0.08
        params["browLX"] -= 0.06
        params["browRX"] += 0.06

    params = _clamp_expression_params(params)
    params = _apply_previous_state_continuity(params, previous_state, continuity_blend)
    params = apply_visual_signature(params, signature=signature, intensity=intensity, energy=energy, warmth=warmth)
    params = apply_model_adapter(params, signature=signature, intensity=intensity, energy=energy, model_name=model_name)

    return {
        **base_pose,
        "params": params,
    }


def build_micro_events(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    intent: dict,
    signature: dict | None = None,
    model_name: str = "Hiyori",
) -> list[dict]:
    if signature is None:
        signature = resolve_visual_signature(emotion, performance_mode, intent)

    avoid = set(intent.get("avoid") or [])
    must_include = intent.get("must_include") or []
    if must_include:
        events = []
        for name in must_include:
            if name in MICRO_EVENT_LIBRARY and name not in avoid:
                events.append(deepcopy(MICRO_EVENT_LIBRARY[name]))
        if events:
            return events

    candidates: list[str] = []
    mode_event_map = {
        "goofy_face": "goofy_eye_cross_bias",
        "cheeky_wink": "wink_left",
        "volatile": "volatile_twitch",
        "meltdown": "meltdown_warp",
        "shock_recoil": "shock_pop",
        "tense_hold": "tense_squeeze",
        "gloomy": "gloom_drop",
        "deadpan": "gloom_drop",
        "awkward": "awkward_freeze",
        "bright_talk": "uneven_brow_pop",
        "smug": "smirk_left",
    }

    event_name = mode_event_map.get(performance_mode)
    if event_name:
        _append_unique(candidates, event_name)

    for name in signature.get("event_bias", []):
        _append_unique(candidates, name)

    arc = intent.get("arc", "steady")
    if arc in {"pop_then_settle", "widen_then_tease"}:
        _append_unique(candidates, "shock_pop")

    if not candidates:
        return []

    events = []
    duration_scale = 1.0 + max(0.0, intensity - 0.55) * 0.60 + max(0.0, energy - 0.55) * 0.30
    if model_name.lower() == "hiyori":
        duration_scale += 0.15

    for name in candidates:
        if name in avoid or name not in MICRO_EVENT_LIBRARY:
            continue
        event = deepcopy(MICRO_EVENT_LIBRARY[name])
        scaled = int(event["durationMs"] * duration_scale)
        if name in {"wink_left", "wink_right"}:
            scaled = int(event["durationMs"] * (1.0 + max(0.0, intensity - 0.5) * 0.5))
            scaled = max(180, min(420, scaled))
        else:
            scaled = max(180, min(1000, scaled))
        event["durationMs"] = scaled
        events.append(event)
        if len(events) >= 2:
            break

    return events


def build_expression_sequence(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    intent: dict,
    signature: dict | None = None,
    model_name: str = "Hiyori",
) -> list[dict]:
    del emotion
    if signature is None:
        signature = resolve_visual_signature(intent.get("emotion", "neutral"), performance_mode, intent)

    arc = intent.get("arc", "steady")
    if arc == "pause_then_smirk":
        event = deepcopy(MICRO_EVENT_LIBRARY["smirk_left"])
        event["durationMs"] = max(420, int(event["durationMs"] * (1.0 + intensity * 0.2)))
        return [event]

    mode_sequence_map = {
        "bright_talk": "bright_talk_bounce",
        "goofy_face": "pause_then_goofy",
        "smug": "smirk_then_flat",
        "gloomy": "drop_then_gloom",
        "deadpan": "drop_then_gloom",
        "tense_hold": "tense_then_break",
        "meltdown": "burst_then_unstable",
        "volatile": "burst_then_unstable",
    }

    if arc == "widen_then_tease":
        seq_name = "pause_then_goofy"
    elif arc == "glare_then_flatten":
        seq_name = "smirk_then_flat"
    elif arc == "pop_then_settle":
        seq_name = "bright_talk_bounce"
    else:
        seq_name = mode_sequence_map.get(performance_mode)

    if not seq_name or seq_name not in SEQUENCE_LIBRARY or energy <= 0.35:
        return []

    duration_scale = 1.0 + max(0.0, intensity - 0.55) * 0.35
    if model_name.lower() == "hiyori":
        duration_scale += 0.10

    sequence = []
    for step in SEQUENCE_LIBRARY[seq_name]:
        seq_step = deepcopy(step)
        seq_step["durationMs"] = max(160, min(1200, int(seq_step["durationMs"] * duration_scale)))
        sequence.append(seq_step)

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


def build_timing_hints(intent: dict, base_pose: dict, sequence: list[dict]) -> dict:
    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    return {
        "holdMs": hold_ms,
        "basePoseDurationSec": base_pose["durationSec"],
        "sequenceSteps": len(sequence),
    }


def estimate_dialogue_hold_ms(intent: dict) -> int:
    spoken_text = str(intent.get("spoken_text") or intent.get("dialogue_text") or "").strip()
    if not spoken_text:
        return 0

    speaking_rate = _clamp(_coerce_float(intent.get("speaking_rate", 1.0), 1.0), 0.65, 1.6)
    visible_chars = [char for char in spoken_text if not char.isspace()]
    punctuation_count = sum(1 for char in visible_chars if char in "，。！？!?、,.…")

    estimated_ms = (len(visible_chars) * 95) + (punctuation_count * 180) + 650
    return int(_clamp(estimated_ms / speaking_rate, 1800, 14000))


def resolve_idle_plan_name(emotion: str, performance_mode: str, topic_guard: dict) -> str:
    source_theme = topic_guard.get("source_theme", "daily_talk")
    if source_theme == "crying" or emotion == "sad":
        return "crying_idle"
    if emotion == "angry" or performance_mode in {"meltdown", "volatile"}:
        return "angry_glare_idle"
    if emotion == "shy" or performance_mode == "awkward":
        return "shy_idle"
    if emotion == "gloomy" or performance_mode in {"gloomy", "deadpan"}:
        return "gloomy_idle"
    return "happy_idle"


def build_idle_plan(
    emotion: str,
    performance_mode: str,
    topic_guard: dict,
    base_pose: dict,
    sequence: list[dict],
    micro_events: list[dict],
    intent: dict,
) -> dict:
    idle_name = resolve_idle_plan_name(emotion, performance_mode, topic_guard)
    settle_params = deepcopy(base_pose["params"])
    settle_params.update(IDLE_PLAN_SETTLE_PATCHES[idle_name])
    settle_params = _clamp_expression_params(settle_params)

    action_enter_after_ms = int(base_pose["durationSec"] * 1000)
    action_enter_after_ms += sum(int(step.get("durationMs", 0)) for step in sequence)
    action_enter_after_ms += max((int(event.get("durationMs", 0)) for event in micro_events), default=0)
    speaking_enter_after_ms = estimate_dialogue_hold_ms(intent)
    enter_after_ms = max(400, action_enter_after_ms, speaking_enter_after_ms)

    return {
        "name": idle_name,
        "mode": "loop",
        "enterAfterMs": enter_after_ms,
        "loopIntervalMs": 2600,
        "interruptible": True,
        "source": {
            "actionEnterAfterMs": action_enter_after_ms,
            "speakingEnterAfterMs": speaking_enter_after_ms,
        },
        "settlePose": {
            "preset": idle_name,
            "params": settle_params,
            "durationSec": 12.0,
        },
        "loopEvents": deepcopy(IDLE_PLAN_LOOP_EVENTS[idle_name]),
    }


def build_model_hints(intent: dict, preset_name: str, model_name: str) -> dict:
    return {
        "modelName": model_name,
        "preset": preset_name,
        "variationRuleCount": len(PRESET_VARIATION_RULES.get(preset_name, {})),
        "asymmetryBias": intent.get("asymmetry_bias", "auto"),
    }


def compile_expression_plan(intent: dict, model_name: str, previous_state: dict | None) -> dict:
    emotion = intent.get("emotion", intent.get("primary_emotion", DEFAULT_INTENT["emotion"]))
    if emotion not in {
        "neutral",
        "happy",
        "playful",
        "teasing",
        "angry",
        "sad",
        "gloomy",
        "shy",
        "surprised",
        "conflicted",
    }:
        emotion = "neutral"

    performance_mode = intent.get("performance_mode", DEFAULT_INTENT["performance_mode"])
    original_mode = performance_mode
    topic_guard = intent.get("topic_guard", DEFAULT_INTENT["topic_guard"])
    if not isinstance(topic_guard, dict):
        topic_guard = dict(DEFAULT_INTENT["topic_guard"])

    performance_mode = resolve_effective_performance_mode(emotion, performance_mode, topic_guard)
    signature = resolve_visual_signature(emotion, performance_mode, intent)
    continuity_blend = _resolve_continuity_blend(
        previous_state,
        emotion,
        performance_mode,
        _coerce_float(intent.get("intensity", 0.35), 0.35),
        signature.get("signature_name", "calm_soft"),
    )
    preset_name = select_base_pose(emotion, performance_mode, model_name=model_name, signature=signature)

    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    speaking_rate = _coerce_float(intent.get("speaking_rate", 1.0), 1.0)
    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)

    base_pose = {
        "preset": preset_name,
        "params": deepcopy(BASE_POSE_PRESETS[preset_name]),
        "durationSec": max(0.3, hold_ms / 1000.0),
    }
    base_pose = apply_base_pose_modifiers(
        intent,
        base_pose,
        signature=signature,
        model_name=model_name,
        previous_state=previous_state,
        continuity_blend=continuity_blend,
    )

    micro_events = build_micro_events(
        emotion,
        performance_mode,
        intensity,
        energy,
        intent,
        signature=signature,
        model_name=model_name,
    )
    sequence = build_expression_sequence(
        emotion,
        performance_mode,
        intensity,
        energy,
        intent,
        signature=signature,
        model_name=model_name,
    )
    blink_plan = build_blink_plan(intent, model_name=model_name)
    idle_plan = build_idle_plan(
        emotion,
        performance_mode,
        topic_guard,
        base_pose=base_pose,
        sequence=sequence,
        micro_events=micro_events,
        intent=intent,
    )

    return {
        "type": "expression_plan",
        "basePose": base_pose,
        "microEvents": micro_events,
        "sequence": sequence,
        "idlePlan": idle_plan,
        "blinkPlan": blink_plan,
        "speakingRate": speaking_rate,
        "timingHints": build_timing_hints(intent, base_pose=base_pose, sequence=sequence),
        "modelHints": build_model_hints(intent, preset_name=preset_name, model_name=model_name),
        "carryState": _build_carry_state(intent, signature, base_pose["params"], continuity_blend),
        "debug": {
            "intentPrimaryEmotion": emotion,
            "intentEmotion": emotion,
            "intentPerformanceMode": performance_mode,
            "originalPerformanceMode": original_mode,
            "selectedBasePreset": preset_name,
            "sourceTheme": topic_guard.get("source_theme", "daily_talk"),
            "guardActive": topic_guard.get("must_preserve_theme", True),
            "modeDowngraded": original_mode != performance_mode,
            "arc": intent.get("arc", "steady"),
            "signature": signature.get("signature_name", "calm_soft"),
            "blushPolicy": signature.get("blush_policy", "neutralize"),
            "idlePlan": idle_plan["name"],
        },
    }
