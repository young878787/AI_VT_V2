"""Turn-to-turn expression continuity helpers."""

from domain.expression_compiler_rules import CONTINUITY_FAMILY_MAP, CONTINUITY_PARAM_WEIGHTS


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _coerce_float(value: object, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        return default


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


def resolve_continuity_blend(
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


def apply_previous_state_continuity(params: dict, previous_state: dict | None, blend: float) -> dict:
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


def build_carry_state(
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
        "bodyAngleX": params["bodyAngleX"],
        "bodyAngleY": params["bodyAngleY"],
        "bodyAngleZ": params["bodyAngleZ"],
        "breathLevel": params["breathLevel"],
        "physicsImpulse": params["physicsImpulse"],
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
