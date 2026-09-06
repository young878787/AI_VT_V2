from copy import deepcopy
import random

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_continuity import (
    apply_previous_state_continuity,
    build_carry_state,
    resolve_continuity_blend,
)
from domain.expression_compiler_rules import (
    BODY_MOTION_PROFILE_DEFAULTS,
    BODY_MOTION_PROFILE_RULES,
    MOTION_PARAM_DEFAULTS,
)
from domain.expression_idle_library import (
    AMBIENT_IDLE_ENTER_AFTER_MS,
    AMBIENT_IDLE_STATE_ORDER,
    AMBIENT_IDLE_STATE_TEMPLATES,
    AMBIENT_IDLE_SWITCH_INTERVAL_MS,
    IDLE_PLAN_LOOP_EVENTS,
    IDLE_PLAN_LOOP_INTERVAL_MS,
    IDLE_PLAN_SETTLE_PATCHES,
)
from domain.expression_intent_schema import DEFAULT_INTENT
from domain.expression_eye_motion_library import build_eye_motion_plan
from domain.expression_motion_library import build_motion_plan
from domain.expression_presets import BASE_POSE_PRESETS, PRESET_VARIATION_RULES
from domain.expression_sequence_library import (
    MICRO_EVENT_LIBRARY,
    MICRO_EXPRESSION_THEME_POOLS,
    SEQUENCE_LIBRARY,
    SPEAKING_SEQUENCE_POOLS,
)
from domain.expression_visual_signature import (
    resolve_effective_performance_mode,
    resolve_topic_guard,
    resolve_visual_signature,
    select_base_pose,
)


POST_SPEECH_MAIN_EXPRESSION_HOLD_MS = {"min": 6000, "max": 10000}
BODY_BOUNCE_MICRO_EVENT_KINDS = {
    "happy_body_bounce_pop",
    "happy_body_sway_bounce_left",
    "happy_body_sway_bounce_right",
    "playful_body_rebound",
    "tease_body_lean_rebound",
}
BODY_BOUNCE_POOL_KEYS = {"happy", "playful", "teasing", "neutral"}

EMOTION_MICRO_EVENT_POOLS: dict[str, list[str]] = {
    "happy": ["happy_smile_pulse", "happy_brow_lift", "brow_micro_curve_smile", "brow_micro_dual_lift"],
    "playful": ["playful_body_rebound", "playful_brow_spark", "brow_micro_shape_wave", "happy_smile_pulse"],
    "teasing": ["tease_body_lean_rebound", "playful_brow_spark", "smirk_left", "happy_brow_lift"],
    "angry": ["angry_brow_press", "angry_eye_narrow", "tense_squeeze", "angry_stare_flash"],
    "sad": ["sad_brow_waver", "brow_micro_inner_worry", "sad_eye_sink", "gloomy_flat_hold"],
    "gloomy": ["gloomy_flat_hold", "brow_micro_inner_worry", "gloom_drop", "sad_eye_sink"],
    "shy": ["shy_blush_pulse", "shy_peek_left", "awkward_freeze", "brow_micro_curve_smile"],
    "surprised": ["surprised_eye_pop", "brow_micro_surprise_arc", "shock_pop", "brow_micro_dual_lift"],
    "conflicted": ["conflicted_brow_tilt", "brow_micro_soft_question", "volatile_twitch", "brow_micro_inner_worry"],
    "neutral": ["brow_micro_understand_lift", "happy_brow_lift", "brow_micro_curve_smile", "brow_micro_dual_lift"],
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


def _ensure_motion_params(params: dict) -> None:
    for key, default in MOTION_PARAM_DEFAULTS.items():
        params.setdefault(key, default)


def _clamp_expression_params(params: dict) -> dict:
    _ensure_motion_params(params)
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
    params["eyeBallX"] = _clamp(params["eyeBallX"], -1.0, 1.0)
    params["eyeBallY"] = _clamp(params["eyeBallY"], -1.0, 1.0)
    params["headIntensity"] = _clamp(params["headIntensity"], 0.0, 0.95)
    params["bodyAngleX"] = _clamp(params["bodyAngleX"], -1.0, 1.0)
    params["bodyAngleY"] = _clamp(params["bodyAngleY"], -1.0, 1.0)
    params["bodyAngleZ"] = _clamp(params["bodyAngleZ"], -1.0, 1.0)
    params["breathLevel"] = _clamp(params["breathLevel"], 0.0, 1.0)
    params["physicsImpulse"] = _clamp(params["physicsImpulse"], 0.0, 1.0)
    return params


def _random_int(minimum: int, maximum: int) -> int:
    return random.randint(minimum, maximum)


def _build_ambient_state(base_params: dict, state_name: str) -> dict:
    template = AMBIENT_IDLE_STATE_TEMPLATES[state_name]
    params = deepcopy(base_params)
    params.update(template["params"])

    for key, jitter in template.get("jitter", {}).items():
        current_value = params.get(key)
        if isinstance(current_value, bool) or not isinstance(current_value, (int, float)):
            continue
        params[key] = current_value + random.uniform(-float(jitter), float(jitter))

    params = _clamp_expression_params(params)
    return {
        "kind": state_name,
        "params": params,
    }


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


def apply_body_motion_profile(
    params: dict,
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    playfulness: float,
    warmth: float,
    dominance: float,
) -> dict:
    activity = _clamp(0.16 + (energy * 0.42) + (intensity * 0.22) + (playfulness * 0.14), 0.0, 1.0)
    breath = 0.30 + (energy * 0.28) + (warmth * 0.10)
    impulse = 0.14 + (activity * 0.36)

    if emotion in {"happy", "playful", "teasing"}:
        params["bodyAngleX"] += 0.12 + warmth * 0.10 + playfulness * 0.06
        params["bodyAngleY"] += (playfulness - 0.10) * 0.22 + energy * 0.06
        params["bodyAngleZ"] += (0.5 - dominance) * 0.18 - playfulness * 0.05
        params["breathLevel"] += breath + 0.12
        params["physicsImpulse"] += impulse + (playfulness * 0.22) + (energy * 0.08)
    elif emotion == "angry":
        params["bodyAngleX"] += 0.08 + dominance * 0.12
        params["bodyAngleY"] -= 0.10 + intensity * 0.08
        params["bodyAngleZ"] += (dominance - 0.5) * 0.22
        params["breathLevel"] += 0.26 + (intensity * 0.22)
        params["physicsImpulse"] += 0.30 + (intensity * 0.38) + (energy * 0.14)
    elif emotion == "sad":
        params["bodyAngleX"] -= 0.10 + intensity * 0.06
        params["bodyAngleY"] -= 0.22 + intensity * 0.10
        params["bodyAngleZ"] -= (1.0 - warmth) * 0.05
        params["breathLevel"] += 0.18 + (intensity * 0.08)
        params["physicsImpulse"] += 0.18 + (energy * 0.12)
    elif emotion == "gloomy":
        params["bodyAngleX"] -= 0.14 + intensity * 0.06
        params["bodyAngleY"] -= 0.16 + intensity * 0.08
        params["bodyAngleZ"] += 0.03
        params["breathLevel"] += 0.20 + (energy * 0.10)
        params["physicsImpulse"] += 0.14 + (energy * 0.10)
    elif emotion == "shy":
        params["bodyAngleX"] -= 0.10
        params["bodyAngleY"] += 0.10 + warmth * 0.08
        params["bodyAngleZ"] -= 0.10 + playfulness * 0.07
        params["breathLevel"] += 0.30 + (intensity * 0.14)
        params["physicsImpulse"] += 0.20 + (energy * 0.18)
    elif emotion == "surprised":
        params["bodyAngleX"] += 0.14 + intensity * 0.12
        params["bodyAngleY"] += 0.10 + energy * 0.10
        params["bodyAngleZ"] += 0.07
        params["breathLevel"] += 0.42 + (energy * 0.16)
        params["physicsImpulse"] += 0.36 + (energy * 0.24)
    elif emotion == "conflicted":
        params["bodyAngleX"] += 0.06
        params["bodyAngleY"] += 0.10
        params["bodyAngleZ"] -= 0.12 + intensity * 0.08
        params["breathLevel"] += 0.30 + (energy * 0.14)
        params["physicsImpulse"] += 0.28 + (energy * 0.20)
    else:
        params["breathLevel"] += 0.26 + (energy * 0.10)
        params["physicsImpulse"] += 0.08 + (energy * 0.08)

    if performance_mode in {"bright_talk", "goofy_face", "shock_recoil"}:
        params["breathLevel"] += 0.10
        params["physicsImpulse"] += 0.16
    elif performance_mode in {"deadpan", "gloomy", "tense_hold"}:
        params["physicsImpulse"] *= 0.72
    elif performance_mode in {"meltdown", "volatile"}:
        params["physicsImpulse"] += 0.12
        params["bodyAngleZ"] += 0.05 if dominance >= 0.5 else -0.05

    return _clamp_expression_params(params)


def build_body_motion_profile(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    playfulness: float,
) -> dict:
    profile = deepcopy(BODY_MOTION_PROFILE_DEFAULTS)
    profile.update(BODY_MOTION_PROFILE_RULES.get(emotion, {}))

    if performance_mode in {"bright_talk", "goofy_face", "shock_recoil"}:
        profile["speed"] += 0.08
        profile["swayScale"] += 0.10
        profile["bobScale"] += 0.10
        profile["headScale"] += 0.08
    elif performance_mode in {"deadpan", "gloomy", "tense_hold"}:
        profile["speed"] -= 0.03 if emotion in {"sad", "gloomy"} else 0.08
        if emotion not in {"sad", "gloomy"}:
            profile["swayScale"] -= 0.08
            profile["headScale"] -= 0.08
    elif performance_mode in {"meltdown", "volatile"}:
        profile["twistScale"] += 0.16
        profile["headScale"] += 0.08

    if emotion in {"happy", "playful", "teasing"}:
        profile["speed"] += energy * 0.10
        profile["swayScale"] += playfulness * 0.10
    elif emotion in {"sad", "gloomy"}:
        profile["speed"] -= intensity * 0.06
        profile["swayScale"] -= intensity * 0.04
        profile["bobScale"] += intensity * 0.08
    elif emotion == "angry":
        profile["twistScale"] += intensity * 0.12
        profile["breathScale"] += intensity * 0.08

    for key in ("speed", "swayScale", "bobScale", "twistScale", "breathScale", "headScale"):
        profile[key] = round(_clamp(float(profile[key]), 0.15, 1.8), 3)

    return profile


def apply_base_pose_modifiers(
    intent: dict,
    base_pose: dict,
    signature: dict | None = None,
    model_name: str = "Hiyori",
    previous_state: dict | None = None,
    continuity_blend: float = 0.0,
) -> dict:
    params = deepcopy(base_pose["params"])
    _ensure_motion_params(params)

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
        params["eyeBallX"] += 0.08

    if emotion == "shy":
        params["eyeBallX"] -= 0.12 + intensity * 0.04
        params["eyeBallY"] -= 0.02
    elif emotion == "conflicted":
        params["eyeBallX"] += 0.05 + intensity * 0.03
    elif emotion == "surprised":
        params["eyeBallY"] += 0.04 + energy * 0.03
    elif performance_mode == "goofy_face":
        params["eyeBallX"] += 0.10 + playfulness * 0.05
        params["eyeBallY"] -= 0.03

    params = _clamp_expression_params(params)
    params = apply_previous_state_continuity(params, previous_state, continuity_blend)
    params = apply_visual_signature(params, signature=signature, intensity=intensity, energy=energy, warmth=warmth)
    params = apply_model_adapter(params, signature=signature, intensity=intensity, energy=energy, model_name=model_name)
    params = apply_body_motion_profile(
        params,
        emotion=emotion,
        performance_mode=performance_mode,
        intensity=intensity,
        energy=energy,
        playfulness=playfulness,
        warmth=warmth,
        dominance=dominance,
    )

    return {
        **base_pose,
        "params": params,
        "bodyMotionProfile": build_body_motion_profile(
            emotion=emotion,
            performance_mode=performance_mode,
            intensity=intensity,
            energy=energy,
            playfulness=playfulness,
        ),
    }


MAX_MICRO_EVENTS = 3


def _merge_micro_event_lists(primary: list[dict], secondary: list[dict]) -> list[dict]:
    result: list[dict] = list(primary)
    existing_kinds: set[str] = {str(e.get("kind") or "") for e in result}
    for event in secondary:
        if str(event.get("kind") or "") not in existing_kinds and len(result) < MAX_MICRO_EVENTS:
            result.append(event)
            existing_kinds.add(str(event.get("kind") or ""))
    return result[:MAX_MICRO_EVENTS]


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


def build_emotion_micro_events(
    emotion: str,
    intensity: float,
    energy: float,
    intent: dict,
    model_name: str = "Hiyori",
) -> list[dict]:
    if energy < 0.45:
        return []

    pool = EMOTION_MICRO_EVENT_POOLS.get(emotion)
    if not pool:
        return []

    avoid = set(intent.get("avoid") or [])
    available = [name for name in pool if name in MICRO_EVENT_LIBRARY and name not in avoid]
    if not available:
        return []

    if energy >= 0.72:
        target_count = 2
    elif energy >= 0.65:
        target_count = random.choice([1, 2])
    else:
        target_count = 1

    selected: list[str] = random.sample(available, min(target_count, len(available)))

    events: list[dict] = []
    duration_scale = 1.0 + max(0.0, intensity - 0.45) * 0.40 + max(0.0, energy - 0.50) * 0.25
    if model_name.lower() == "hiyori":
        duration_scale += 0.10

    for name in selected:
        event = deepcopy(MICRO_EVENT_LIBRARY[name])
        scaled = max(180, min(1200, int(event["durationMs"] * duration_scale)))
        event["durationMs"] = scaled
        events.append(event)

    return events


def build_expression_sequence(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    intent: dict,
    signature: dict | None = None,
    model_name: str = "Hiyori",
    action_duration_ms: int | None = None,
) -> list[dict]:
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

    duration_scale = 1.0 + max(0.0, intensity - 0.55) * 0.35
    if model_name.lower() == "hiyori":
        duration_scale += 0.10

    sequence = []
    if seq_name and seq_name in SEQUENCE_LIBRARY and energy > 0.35:
        for step in SEQUENCE_LIBRARY[seq_name]:
            seq_step = deepcopy(step)
            seq_step["durationMs"] = max(1000, min(3000, int(seq_step["durationMs"] * duration_scale)))
            sequence.append(seq_step)

    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    target_timeline_ms = _resolve_sequence_target_timeline_ms(intent, hold_ms, action_duration_ms)
    sequence.extend(
        build_speaking_micro_sequence(
            emotion,
            performance_mode,
            intensity,
            energy,
            intent,
            existing_sequence=sequence,
            model_name=model_name,
            target_timeline_ms=target_timeline_ms,
        )
    )

    return _cap_sequence_timeline(sequence, target_timeline_ms + 350)


def _resolve_sequence_target_timeline_ms(intent: dict, hold_ms: float, action_duration_ms: int | None = None) -> int:
    target_ms = max(
        int(hold_ms),
        int(action_duration_ms or 0),
        int(estimate_dialogue_hold_ms(intent) or 0),
    )
    return int(_clamp(target_ms, 1200, 14000))


def _resolve_speaking_sequence_count(speaking_ms: int, hold_ms: float, intensity: float, energy: float) -> int:
    budget_ms = int(_clamp(speaking_ms or hold_ms, 1200, 14000))
    count = int((budget_ms + 1599) // 1600)

    if intensity >= 0.72 or energy >= 0.72:
        count += 1

    return int(_clamp(count, 2, 12))


def _speaking_pool_key(emotion: str, performance_mode: str) -> str:
    if performance_mode in {"gloomy", "deadpan"}:
        return "gloomy"
    if performance_mode == "awkward":
        return "shy"
    if performance_mode in {"meltdown", "volatile"}:
        return "angry" if emotion == "angry" else "conflicted"
    if performance_mode in {"goofy_face", "smug", "cheeky_wink"} and emotion in {"playful", "teasing", "neutral"}:
        return "playful" if emotion != "teasing" else "teasing"
    if performance_mode == "shock_recoil":
        return "surprised"
    return emotion if emotion in SPEAKING_SEQUENCE_POOLS else "neutral"


def _apply_speaking_event_defaults(event: dict, index: int, intensity: float, energy: float) -> dict:
    seq_step = deepcopy(event)
    base_duration = int(seq_step.get("durationMs", 560))
    duration_scale = 1.55 + max(0.0, intensity) * 0.32 + max(0.0, energy - 0.45) * 0.22
    cadence_variation_ms = (index % 3) * 180
    seq_step["durationMs"] = max(1000, min(3000, int(base_duration * duration_scale) + cadence_variation_ms))
    seq_step.setdefault("fadeInMs", 180 + ((index % 3) * 40))
    seq_step.setdefault("fadeOutMs", 240 + ((index % 2) * 60))
    seq_step["fadeInMs"] = max(160, min(360, int(seq_step["fadeInMs"])))
    seq_step["fadeOutMs"] = max(220, min(480, int(seq_step["fadeOutMs"])))
    seq_step["returnToBase"] = bool(seq_step.get("returnToBase", True))
    _apply_body_bounce_event_variation(seq_step, index, intensity, energy)
    return seq_step


def _is_body_bounce_event_name(name: object) -> bool:
    return str(name or "") in BODY_BOUNCE_MICRO_EVENT_KINDS


def _apply_body_bounce_event_variation(seq_step: dict, index: int, intensity: float, energy: float) -> None:
    kind = str(seq_step.get("kind") or "")
    patch = seq_step.get("patch")
    if not _is_body_bounce_event_name(kind) or not isinstance(patch, dict):
        return

    seq_step["durationMs"] = int(_clamp(
        int(seq_step.get("durationMs", 1000)) * random.uniform(0.56, 0.78),
        620,
        1450,
    ))

    energy_boost = _clamp((energy - 0.45) * 0.24, 0.0, 0.16)
    intensity_boost = _clamp((intensity - 0.50) * 0.18, 0.0, 0.12)
    body_scale = random.uniform(0.96, 1.30) + energy_boost + intensity_boost
    bounce_scale = random.uniform(1.05, 1.42) + energy_boost
    impulse_scale = random.uniform(0.98, 1.22) + intensity_boost

    if "bodyAngleX" in patch:
        patch["bodyAngleX"] = round(_clamp(float(patch["bodyAngleX"]) * body_scale, -0.58, 0.58), 3)
    if "bodyAngleY" in patch:
        patch["bodyAngleY"] = round(_clamp(float(patch["bodyAngleY"]) * bounce_scale, 0.18, 0.68), 3)
    if "bodyAngleZ" in patch:
        patch["bodyAngleZ"] = round(_clamp(float(patch["bodyAngleZ"]) * random.uniform(0.96, 1.28), -0.55, 0.55), 3)
    if "breathLevel" in patch:
        patch["breathLevel"] = round(_clamp(float(patch["breathLevel"]) * random.uniform(1.00, 1.12), 0.72, 1.00), 3)
    if "physicsImpulse" in patch:
        patch["physicsImpulse"] = round(_clamp(float(patch["physicsImpulse"]) * impulse_scale, 0.70, 1.00), 3)

    duration_ms = max(1, int(seq_step.get("durationMs", 1000)))
    max_fade_in_ms = max(90, min(260, int(duration_ms * 0.22)))
    max_fade_out_ms = max(160, min(420, int(duration_ms * 0.36)))
    seq_step["fadeInMs"] = int(_clamp(int(seq_step.get("fadeInMs", 150)) - 55 + ((index % 2) * 12), 70, max_fade_in_ms))
    seq_step["fadeOutMs"] = int(_clamp(max(int(seq_step.get("fadeOutMs", 240)) - 80, int(duration_ms * 0.20)), 150, max_fade_out_ms))


def _allowed_secondary_micro_pool(pool_key: str, secondary_emotion: str) -> str | None:
    if not secondary_emotion or secondary_emotion == "neutral" or secondary_emotion == pool_key:
        return None
    if secondary_emotion not in MICRO_EXPRESSION_THEME_POOLS:
        return None

    if pool_key == "angry":
        return secondary_emotion if secondary_emotion == "conflicted" else None
    if pool_key in {"sad", "gloomy"}:
        return secondary_emotion if secondary_emotion in {"shy", "conflicted", "sad", "gloomy"} else None

    return secondary_emotion


def _micro_family_weight(family: str, pool_key: str, intent: dict, source_scale: float) -> float:
    secondary = str(intent.get("secondary_emotion") or "")
    arc = str(intent.get("arc") or "steady")
    performance_mode = str(intent.get("performance_mode") or "")
    asymmetry_bias = str(intent.get("asymmetry_bias") or "auto")
    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)
    playfulness = _coerce_float(intent.get("playfulness", 0.3), 0.3)
    warmth = _coerce_float(intent.get("warmth", 0.5), 0.5)
    dominance = _coerce_float(intent.get("dominance", 0.5), 0.5)

    weight = source_scale
    if family == "core":
        weight *= 1.05
    if family in {"brow_lift", "surprise"} and (
        secondary == "surprised" or arc in {"pop_then_settle", "widen_then_tease"} or pool_key == "surprised"
    ):
        weight *= 2.35
    if family in {"understanding", "brow_curve", "brow_shape"} and (warmth >= 0.55 or pool_key in {"happy", "neutral", "shy"}):
        weight *= 1.75
    if family == "brow_bounce" and (pool_key in {"happy", "playful", "teasing", "neutral"} or energy >= 0.55):
        weight *= 2.15
    if family == "body_bounce" and (pool_key in {"happy", "playful", "teasing", "neutral"} or energy >= 0.58):
        weight *= 1.50 + (energy * 0.35) + (playfulness * 0.25)
    if family == "asymmetry" and (
        asymmetry_bias in {"subtle", "strong"}
        or playfulness >= 0.58
        or performance_mode in {"goofy_face", "smug", "cheeky_wink"}
    ):
        weight *= 2.05
    if family in {"brow_press", "stare", "tension"} and (pool_key == "angry" or dominance >= 0.62 or intensity >= 0.68):
        weight *= 1.85
    if family in {"worry", "low_eye"} and (pool_key in {"sad", "gloomy", "shy"} or energy <= 0.35):
        weight *= 1.75

    return max(0.05, weight)


def _build_micro_expression_candidates(pool_key: str, intent: dict, avoid: set[str], existing_kinds: set[str]) -> list[dict]:
    candidates: list[dict] = []

    def add_pool(source_key: str, source_scale: float) -> None:
        for family, event_names in MICRO_EXPRESSION_THEME_POOLS.get(source_key, {}).items():
            weight = _micro_family_weight(family, source_key, intent, source_scale)
            for name in event_names:
                if name in avoid or name in existing_kinds or name not in MICRO_EVENT_LIBRARY:
                    continue
                candidates.append({"name": name, "family": family, "weight": weight})

    add_pool(pool_key, 1.0)
    secondary_key = _allowed_secondary_micro_pool(pool_key, str(intent.get("secondary_emotion") or ""))
    if secondary_key:
        add_pool(secondary_key, 0.65)

    if not candidates:
        for name in SPEAKING_SEQUENCE_POOLS.get(pool_key, SPEAKING_SEQUENCE_POOLS["neutral"]):
            if name in avoid or name in existing_kinds or name not in MICRO_EVENT_LIBRARY:
                continue
            candidates.append({"name": name, "family": "core", "weight": 1.0})

    return candidates


def _weighted_micro_pick(
    candidates: list[dict],
    selected_names: set[str],
    family_counts: dict[str, int],
    last_family: str | None,
    family: str | None = None,
) -> dict | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate["name"] not in selected_names
        and (family is None or candidate["family"] == family)
        and candidate["family"] != last_family
        and family_counts.get(candidate["family"], 0) < 3
    ]
    if not eligible:
        eligible = [
            candidate
            for candidate in candidates
            if candidate["name"] not in selected_names
            and (family is None or candidate["family"] == family)
            and family_counts.get(candidate["family"], 0) < 3
        ]
    if not eligible:
        eligible = [
            candidate
            for candidate in candidates
            if family is None or candidate["family"] == family
        ]
    if not eligible:
        return None
    if family is not None:
        brow_micro_eligible = [
            candidate for candidate in eligible if str(candidate["name"]).startswith("brow_micro_")
        ]
        if brow_micro_eligible:
            eligible = brow_micro_eligible
        if family == "brow_bounce":
            down_bounce = [
                candidate for candidate in eligible if candidate["name"] == "brow_micro_bounce_down"
            ]
            if down_bounce:
                eligible = down_bounce
        elif family == "brow_shape":
            shape_wave = [
                candidate for candidate in eligible if candidate["name"] == "brow_micro_shape_wave"
            ]
            if shape_wave:
                eligible = shape_wave
        elif family == "understanding":
            understand_lift = [
                candidate for candidate in eligible if candidate["name"] == "brow_micro_understand_lift"
            ]
            if understand_lift:
                eligible = understand_lift

    total_weight = sum(float(candidate["weight"]) for candidate in eligible)
    cursor = random.random() * total_weight
    for candidate in eligible:
        cursor -= float(candidate["weight"])
        if cursor <= 0:
            return candidate
    return eligible[-1]


def _required_micro_families(pool_key: str, intent: dict, remaining_count: int) -> list[str]:
    secondary = str(intent.get("secondary_emotion") or "")
    arc = str(intent.get("arc") or "steady")
    asymmetry_bias = str(intent.get("asymmetry_bias") or "auto")

    if pool_key == "angry":
        families = ["brow_press", "stare", "tension"]
    elif pool_key in {"sad", "gloomy"}:
        families = ["worry", "low_eye", "tension"]
    elif pool_key == "surprised":
        families = ["brow_lift", "surprise", "understanding"]
    elif pool_key in {"playful", "teasing"}:
        families = ["body_bounce", "asymmetry", "brow_shape", "brow_bounce", "brow_lift", "brow_curve"]
    elif pool_key == "shy":
        families = ["brow_curve", "worry", "asymmetry"]
    elif pool_key == "conflicted":
        families = ["asymmetry", "worry", "tension"]
    else:
        families = ["body_bounce", "brow_bounce", "brow_shape", "brow_curve", "brow_lift", "understanding"]

    if secondary == "surprised" or arc in {"pop_then_settle", "widen_then_tease"}:
        families.insert(0, "surprise")
        families.insert(1, "brow_lift")
        families.insert(2, "understanding")
    if asymmetry_bias == "strong" and pool_key not in {"angry", "sad", "gloomy"}:
        families.insert(0, "asymmetry")

    unique: list[str] = []
    for family in families:
        if family not in unique:
            unique.append(family)

    target = 1
    if remaining_count >= 2:
        target = 2
    if remaining_count >= 3:
        target = 3
    if remaining_count >= 5:
        target = 4
    if remaining_count >= 7:
        target = 4

    return unique[:target]


def _target_body_bounce_count(pool_key: str, remaining_count: int, intent: dict) -> int:
    if pool_key not in BODY_BOUNCE_POOL_KEYS:
        return 0

    secondary = str(intent.get("secondary_emotion") or "")
    arc = str(intent.get("arc") or "steady")
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)
    playfulness = _coerce_float(intent.get("playfulness", 0.3), 0.3)
    target = 1
    if remaining_count >= 4:
        target = 2
    if remaining_count >= 6 or (remaining_count >= 5 and (energy >= 0.78 or playfulness >= 0.68)):
        target = 3
    if secondary == "surprised" or arc in {"pop_then_settle", "widen_then_tease"}:
        target = min(target, 1)
    return min(target, remaining_count)


def _append_selected_micro_candidate(
    selected: list[str],
    selected_names: set[str],
    family_counts: dict[str, int],
    candidate: dict,
) -> str:
    selected.append(str(candidate["name"]))
    selected_names.add(str(candidate["name"]))
    family = str(candidate["family"])
    family_counts[family] = family_counts.get(family, 0) + 1
    return family


def _select_weighted_micro_events(pool_key: str, intent: dict, remaining_count: int, avoid: set[str], existing_kinds: set[str]) -> list[str]:
    candidates = _build_micro_expression_candidates(pool_key, intent, avoid, existing_kinds)
    if not candidates:
        return []

    selected: list[str] = []
    selected_names: set[str] = set()
    family_counts: dict[str, int] = {}
    last_family: str | None = None

    target_body_bounces = _target_body_bounce_count(pool_key, remaining_count, intent)
    while (
        len(selected) < remaining_count
        and family_counts.get("body_bounce", 0) < target_body_bounces
    ):
        candidate = _weighted_micro_pick(candidates, selected_names, family_counts, last_family, family="body_bounce")
        if not candidate:
            break
        last_family = _append_selected_micro_candidate(selected, selected_names, family_counts, candidate)

    for family in _required_micro_families(pool_key, intent, remaining_count):
        if family == "body_bounce" and family_counts.get("body_bounce", 0) >= target_body_bounces:
            continue
        candidate = _weighted_micro_pick(candidates, selected_names, family_counts, last_family, family=family)
        if not candidate:
            continue
        last_family = _append_selected_micro_candidate(selected, selected_names, family_counts, candidate)
        if len(selected) >= remaining_count:
            return selected

    while len(selected) < remaining_count:
        candidate = _weighted_micro_pick(candidates, selected_names, family_counts, last_family)
        if not candidate:
            break
        last_family = _append_selected_micro_candidate(selected, selected_names, family_counts, candidate)

    return selected


def _resolve_micro_motif_count(target_count: int, target_timeline_ms: int, existing_timeline_ms: int, pool_key: str) -> int:
    remaining_timeline_ms = max(0, target_timeline_ms - existing_timeline_ms)
    segment_ms = 850 if pool_key in BODY_BOUNCE_POOL_KEYS else 1800
    timeline_count = int((remaining_timeline_ms + segment_ms - 1) // segment_ms)
    max_count = 8 if pool_key in BODY_BOUNCE_POOL_KEYS else 6
    return int(_clamp(min(target_count, timeline_count), 2, max_count))


def _shuffled_micro_motif(motif: list[str], last_name: str | None) -> list[str]:
    if len(motif) <= 1:
        return list(motif)

    body_items = [name for name in motif if _is_body_bounce_event_name(name)]
    other_items = [name for name in motif if not _is_body_bounce_event_name(name)]
    if len(body_items) >= 2:
        rotated = []
        for index, name in enumerate(body_items):
            rotated.append(name)
            if index < len(other_items):
                rotated.append(other_items[index])
        rotated.extend(other_items[len(body_items):])
    else:
        rotated = list(motif)

    if last_name and rotated[0] == last_name:
        rotated = rotated[1:] + rotated[:1]
    return rotated


def _append_sequence_step_to_target(sequence: list[dict], step: dict, target_timeline_ms: int) -> bool:
    current_timeline_ms = _sequence_timeline_ms(sequence)
    if current_timeline_ms >= target_timeline_ms:
        return False

    candidate_timeline_ms = _sequence_timeline_ms(sequence + [step])
    if candidate_timeline_ms <= target_timeline_ms + 350:
        sequence.append(step)
        return True

    remaining_ms = target_timeline_ms - current_timeline_ms
    if remaining_ms < 650 and current_timeline_ms >= int(target_timeline_ms * 0.86):
        return False

    previous_step = sequence[-1] if sequence else None
    overlap_ms = min(
        max(0, int(previous_step.get("fadeOutMs", 0))) if isinstance(previous_step, dict) else 0,
        max(0, int(step.get("fadeInMs", 0))),
    )
    adjusted_duration_ms = remaining_ms + overlap_ms
    min_duration_ms = _sequence_step_min_duration_ms(step)
    if adjusted_duration_ms < min_duration_ms:
        adjusted_duration_ms = min_duration_ms

    if adjusted_duration_ms <= 3000:
        step["durationMs"] = int(adjusted_duration_ms)
        sequence.append(step)
        return True

    return False


def _sequence_step_min_duration_ms(step: dict) -> int:
    return 620 if _is_body_bounce_event_name(step.get("kind")) else 1000


def _spread_sequence_step_duration(sequence: list[dict], step: dict, target_timeline_ms: int, remaining_steps: int) -> dict:
    if remaining_steps <= 1:
        return step

    remaining_timeline_ms = max(0, target_timeline_ms - _sequence_timeline_ms(sequence))
    previous_step = sequence[-1] if sequence else None
    overlap_ms = min(
        max(0, int(previous_step.get("fadeOutMs", 0))) if isinstance(previous_step, dict) else 0,
        max(0, int(step.get("fadeInMs", 0))),
    )
    spread_duration_ms = int((remaining_timeline_ms / remaining_steps) + overlap_ms)
    min_duration_ms = _sequence_step_min_duration_ms(step)
    step["durationMs"] = max(min_duration_ms, min(int(step.get("durationMs", min_duration_ms)), min(3000, spread_duration_ms)))
    return step


def build_speaking_micro_sequence(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    intent: dict,
    existing_sequence: list[dict] | None = None,
    model_name: str = "Hiyori",
    target_timeline_ms: int | None = None,
) -> list[dict]:
    del model_name
    avoid = set(intent.get("avoid") or [])
    spoken_text = str(intent.get("spoken_text") or intent.get("dialogue_text") or "").strip()
    if not spoken_text:
        return []

    existing_sequence = existing_sequence or []
    existing_timeline_ms = _sequence_timeline_ms(existing_sequence)
    existing_kinds = {
        str(step.get("kind"))
        for step in existing_sequence
        if isinstance(step, dict) and step.get("kind")
    }
    speaking_ms = estimate_dialogue_hold_ms(intent)
    hold_ms = _coerce_float(intent.get("hold_ms", 1600), 1600.0)
    target_timeline_ms = int(target_timeline_ms or _resolve_sequence_target_timeline_ms(intent, hold_ms))
    if existing_timeline_ms >= target_timeline_ms:
        return []

    target_count = _resolve_speaking_sequence_count(target_timeline_ms, hold_ms, intensity, energy)
    pool_key = _speaking_pool_key(emotion, performance_mode)
    motif_count = _resolve_micro_motif_count(target_count, target_timeline_ms, existing_timeline_ms, pool_key)
    selected_motif = _select_weighted_micro_events(pool_key, intent, motif_count, avoid, existing_kinds)
    if not selected_motif:
        return []

    sequence: list[dict] = []
    scheduled: list[dict] = list(existing_sequence)
    last_name = str(existing_sequence[-1].get("kind")) if existing_sequence else None
    target_additional_ms = max(0, target_timeline_ms - existing_timeline_ms)
    desired_additional_steps = int(_clamp((target_additional_ms + 1399) // 1400, 1, 16))

    while _sequence_timeline_ms(scheduled) < target_timeline_ms and len(sequence) < 16:
        appended_in_cycle = False
        for name in _shuffled_micro_motif(selected_motif, last_name):
            step = _apply_speaking_event_defaults(
                MICRO_EVENT_LIBRARY[name],
                len(scheduled),
                intensity,
                energy,
            )
            step = _spread_sequence_step_duration(
                scheduled,
                step,
                target_timeline_ms,
                max(1, desired_additional_steps - len(sequence)),
            )
            if not _append_sequence_step_to_target(scheduled, step, target_timeline_ms):
                continue
            sequence.append(step)
            last_name = name
            appended_in_cycle = True
            if _sequence_timeline_ms(scheduled) >= int(target_timeline_ms * 0.96):
                break
            if len(sequence) >= 16:
                break
        if not appended_in_cycle:
            break
        if _sequence_timeline_ms(scheduled) >= int(target_timeline_ms * 0.96):
            break

    if sequence:
        return sequence

    fallback_name = selected_motif[0]
    return [
        _apply_speaking_event_defaults(
            MICRO_EVENT_LIBRARY[fallback_name],
            len(existing_sequence),
            intensity,
            energy,
        )
    ]


def _sequence_timeline_ms(sequence: list[dict]) -> int:
    if not sequence:
        return 0

    total_ms = 0
    for index, step in enumerate(sequence):
        duration_ms = max(1, int(step.get("durationMs", 0)))
        next_step = sequence[index + 1] if index + 1 < len(sequence) else None
        overlap_ms = min(
            max(0, int(step.get("fadeOutMs", 0))),
            max(0, int(next_step.get("fadeInMs", 0))) if isinstance(next_step, dict) else 0,
        )
        total_ms += max(1, duration_ms - overlap_ms)

    return total_ms


def _cap_sequence_timeline(sequence: list[dict], max_timeline_ms: int) -> list[dict]:
    if max_timeline_ms <= 0:
        return sequence

    capped: list[dict] = []
    for step in sequence:
        candidate = capped + [step]
        if _sequence_timeline_ms(candidate) > max_timeline_ms and capped:
            break
        capped = candidate

    return capped


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
    if emotion == "surprised" or performance_mode == "shock_recoil":
        return "surprised_idle"
    if emotion == "conflicted":
        return "conflicted_idle"
    if emotion == "neutral":
        return "neutral_idle"
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
    action_enter_after_ms += _sequence_timeline_ms(sequence)
    action_enter_after_ms += max((int(event.get("durationMs", 0)) for event in micro_events), default=0)
    speaking_enter_after_ms = estimate_dialogue_hold_ms(intent)
    post_speech_hold_ms = _random_int(
        POST_SPEECH_MAIN_EXPRESSION_HOLD_MS["min"],
        POST_SPEECH_MAIN_EXPRESSION_HOLD_MS["max"],
    )
    enter_after_ms = max(400, action_enter_after_ms, speaking_enter_after_ms) + post_speech_hold_ms
    ambient_enter_after_ms = enter_after_ms + _random_int(
        AMBIENT_IDLE_ENTER_AFTER_MS["min"],
        AMBIENT_IDLE_ENTER_AFTER_MS["max"],
    )
    ambient_switch_interval_ms = _random_int(
        AMBIENT_IDLE_SWITCH_INTERVAL_MS["min"],
        AMBIENT_IDLE_SWITCH_INTERVAL_MS["max"],
    )

    return {
        "name": idle_name,
        "mode": "loop",
        "enterAfterMs": enter_after_ms,
        "loopIntervalMs": IDLE_PLAN_LOOP_INTERVAL_MS[idle_name],
        "ambientEnterAfterMs": ambient_enter_after_ms,
        "ambientSwitchIntervalMs": ambient_switch_interval_ms,
        "interruptible": True,
        "source": {
            "actionEnterAfterMs": action_enter_after_ms,
            "speakingEnterAfterMs": speaking_enter_after_ms,
            "postSpeechHoldMs": post_speech_hold_ms,
        },
        "settlePose": {
            "preset": idle_name,
            "params": settle_params,
            "durationSec": 12.0,
        },
        "loopEvents": deepcopy(IDLE_PLAN_LOOP_EVENTS[idle_name]),
        "ambientPlan": {
            "states": [
                _build_ambient_state(settle_params, state_name)
                for state_name in AMBIENT_IDLE_STATE_ORDER
            ],
        },
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
    continuity_blend = resolve_continuity_blend(
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
    emotion_micro_events = build_emotion_micro_events(
        emotion,
        intensity,
        energy,
        intent,
        model_name=model_name,
    )
    micro_events = _merge_micro_event_lists(emotion_micro_events, micro_events)
    motion_plan = build_motion_plan(
        emotion,
        performance_mode,
        intensity,
        energy,
        _coerce_float(intent.get("playfulness", 0.3), 0.3),
        intent,
        previous_state,
    )
    eye_motion_plan = build_eye_motion_plan(
        emotion,
        performance_mode,
        intensity,
        energy,
        intent,
        action_duration_ms=int(motion_plan.get("durationMs", 0)),
    )
    sequence = build_expression_sequence(
        emotion,
        performance_mode,
        intensity,
        energy,
        intent,
        signature=signature,
        model_name=model_name,
        action_duration_ms=int(motion_plan.get("durationMs", 0)),
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

    carry_state = build_carry_state(intent, signature, base_pose["params"], continuity_blend)
    carry_state["motionTheme"] = motion_plan["theme"]
    carry_state["motionVariant"] = motion_plan["variant"]

    return {
        "type": "expression_plan",
        "basePose": base_pose,
        "microEvents": micro_events,
        "sequence": sequence,
        "motionPlan": motion_plan,
        "eyeMotionPlan": eye_motion_plan,
        "idlePlan": idle_plan,
        "blinkPlan": blink_plan,
        "speakingRate": speaking_rate,
        "timingHints": build_timing_hints(intent, base_pose=base_pose, sequence=sequence),
        "modelHints": build_model_hints(intent, preset_name=preset_name, model_name=model_name),
        "carryState": carry_state,
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
            "bodyMotionProfile": base_pose.get("bodyMotionProfile", {}).get("style", "calm_sway"),
            "bodyMotionProfileSource": "emotion_performance",
            "motionTheme": motion_plan["theme"],
            "motionVariant": motion_plan["variant"],
            "eyeMotionStyle": eye_motion_plan["style"],
            "motionThemeOverride": intent.get("motion_theme") == motion_plan["theme"],
            "motionThemeOverrideKeepsBodyProfile": intent.get("motion_theme") == motion_plan["theme"],
            "idlePlan": idle_plan["name"],
        },
    }
