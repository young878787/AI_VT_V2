from copy import deepcopy
import random

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_continuity import (
    apply_previous_state_continuity,
    build_carry_state,
    resolve_continuity_blend,
)
from domain.expression_compiler_rules import MOTION_PARAM_DEFAULTS
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
from domain.expression_presets import BASE_POSE_PRESETS, PRESET_VARIATION_RULES
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY, SEQUENCE_LIBRARY
from domain.expression_visual_signature import (
    resolve_effective_performance_mode,
    resolve_topic_guard,
    resolve_visual_signature,
    select_base_pose,
)


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
        params["bodyAngleX"] += 0.06 + warmth * 0.06
        params["bodyAngleY"] += (playfulness - 0.25) * 0.14
        params["bodyAngleZ"] += (0.5 - dominance) * 0.12
        params["breathLevel"] += breath + 0.12
        params["physicsImpulse"] += impulse + (playfulness * 0.16)
    elif emotion == "angry":
        params["bodyAngleX"] += 0.05 + dominance * 0.10
        params["bodyAngleY"] -= 0.04 + intensity * 0.05
        params["bodyAngleZ"] += (dominance - 0.5) * 0.16
        params["breathLevel"] += 0.22 + (intensity * 0.20)
        params["physicsImpulse"] += 0.30 + (intensity * 0.38) + (energy * 0.14)
    elif emotion == "sad":
        params["bodyAngleX"] -= 0.10 + intensity * 0.06
        params["bodyAngleY"] -= 0.04
        params["bodyAngleZ"] -= (1.0 - warmth) * 0.05
        params["breathLevel"] += 0.18 + (intensity * 0.08)
        params["physicsImpulse"] += 0.06 + (energy * 0.08)
    elif emotion == "gloomy":
        params["bodyAngleX"] -= 0.08 + intensity * 0.04
        params["bodyAngleY"] -= 0.06
        params["bodyAngleZ"] += 0.02
        params["breathLevel"] += 0.16 + (energy * 0.08)
        params["physicsImpulse"] += 0.05 + (energy * 0.06)
    elif emotion == "shy":
        params["bodyAngleX"] -= 0.05
        params["bodyAngleY"] += 0.05 + warmth * 0.06
        params["bodyAngleZ"] -= 0.06 + playfulness * 0.05
        params["breathLevel"] += 0.28 + (intensity * 0.12)
        params["physicsImpulse"] += 0.14 + (energy * 0.14)
    elif emotion == "surprised":
        params["bodyAngleX"] += 0.10 + intensity * 0.10
        params["bodyAngleY"] += 0.06 + energy * 0.08
        params["bodyAngleZ"] += 0.04
        params["breathLevel"] += 0.42 + (energy * 0.16)
        params["physicsImpulse"] += 0.36 + (energy * 0.24)
    elif emotion == "conflicted":
        params["bodyAngleX"] += 0.02
        params["bodyAngleY"] += 0.05
        params["bodyAngleZ"] -= 0.08 + intensity * 0.05
        params["breathLevel"] += 0.28 + (energy * 0.12)
        params["physicsImpulse"] += 0.20 + (energy * 0.16)
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
        "carryState": build_carry_state(intent, signature, base_pose["params"], continuity_blend),
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
