"""Rushia poses and bounded variations using the shared expression-plan contract."""

from copy import deepcopy
import math
import random

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_compiler_rules import MOTION_PARAM_DEFAULTS
from domain.expression_continuity import build_carry_state
from domain.expression_eye_motion_library import build_eye_motion_plan
from domain.expression_motion_library import build_motion_plan
from domain.expression_presets import BASE_POSE_PRESETS
from domain.expression_visual_signature import resolve_effective_performance_mode


# Rushia has no separate smile-eye parameter. A brief closed-eye pose supplies
# that silhouette; base and idle poses always reopen the eyes.
FAMILY_POSES = {
    "calm": {"mouthForm": 0.04, "eyeLOpen": 0.96, "eyeROpen": 0.96},
    "listening": {"mouthForm": 0.2, "eyeLOpen": 1.0, "eyeROpen": 1.0,
                  "browLY": 0.22, "browRY": 0.22, "bodyAngleY": 0.05},
    "thinking": {"mouthForm": -0.12, "eyeLOpen": 0.78, "eyeROpen": 0.95,
                 "eyeSync": False, "browLY": 0.4, "browRY": -0.18,
                 "browLAngle": -0.35, "browRAngle": 0.25, "eyeBallY": 0.12},
    "soft_smile": {"mouthForm": 0.72, "eyeLOpen": 0.88, "eyeROpen": 0.88,
                   "browLY": 0.13, "browRY": 0.13, "blushLevel": 0.15},
    "closed_smile": {"mouthForm": 0.88, "eyeLOpen": 0.88, "eyeROpen": 0.88,
                     "browLY": 0.18, "browRY": 0.18, "blushLevel": 0.24},
    "playful": {"mouthForm": 0.82, "eyeLOpen": 0.82, "eyeROpen": 0.94,
                "eyeSync": False, "browLY": 0.32, "browRY": 0.12, "blushLevel": 0.2},
    "teasing": {"mouthForm": 0.6, "eyeLOpen": 0.7, "eyeROpen": 0.9,
                "eyeSync": False, "browLY": 0.34, "browRY": -0.05},
    "angry": {"mouthForm": -0.9, "eyeLOpen": 0.74, "eyeROpen": 0.74,
              "browLY": -0.5, "browRY": -0.5, "browLAngle": 0.8,
              "browRAngle": -0.8, "browLForm": -0.85, "browRForm": -0.85},
    "sad": {"mouthForm": -0.72, "eyeLOpen": 0.7, "eyeROpen": 0.7,
            "browLY": 0.12, "browRY": 0.12, "browLAngle": -0.72,
            "browRAngle": 0.72, "browLForm": -0.6, "browRForm": -0.6,
            "eyeBallY": -0.16, "bodyAngleY": -0.06},
    "gloomy": {"mouthForm": -0.8, "eyeLOpen": 0.55, "eyeROpen": 0.55,
               "browLY": -0.25, "browRY": -0.25, "eyeBallY": -0.18,
               "bodyAngleY": -0.08},
    "shy": {"mouthForm": 0.36, "eyeLOpen": 0.8, "eyeROpen": 0.8,
            "browLAngle": -0.3, "browRAngle": 0.3, "blushLevel": 0.65,
            "eyeBallY": -0.14, "bodyAngleY": -0.04},
    "surprised": {"mouthForm": -0.25, "eyeLOpen": 1.0, "eyeROpen": 1.0,
                  "browLY": 0.75, "browRY": 0.75, "browLForm": 0.5,
                  "browRForm": 0.5, "bodyAngleY": -0.08},
    "conflicted": {"mouthForm": -0.35, "eyeLOpen": 0.76, "eyeROpen": 0.92,
                   "eyeSync": False, "browLY": 0.35, "browRY": -0.28,
                   "browLAngle": 0.45, "browRAngle": -0.3},
}

# Each variant has a distinct local gesture, not unrestricted parameter noise.
FAMILY_VARIANTS = {
    "calm": [
        ("small_nod", {"bodyAngleY": 0.09, "browLY": 0.1, "browRY": 0.1}),
        ("quiet_glance", {"eyeBallX": -0.2, "bodyAngleZ": -0.045}),
        ("gentle_acknowledgement", {"browLY": 0.2, "browRY": 0.2, "mouthForm": 0.2}),
    ],
    "listening": [
        ("attentive_nod", {"bodyAngleY": 0.14, "eyeBallY": -0.05}),
        ("listen_left", {"bodyAngleZ": -0.09, "eyeBallX": 0.12, "browLY": 0.23}),
        ("listen_right", {"bodyAngleZ": 0.09, "eyeBallX": -0.12, "browRY": 0.23}),
    ],
    "thinking": [
        ("look_up_left", {"eyeBallX": -0.6, "eyeBallY": 0.18, "bodyAngleZ": -0.08}),
        ("look_up_right", {"eyeBallX": 0.6, "eyeBallY": 0.18, "bodyAngleZ": 0.08}),
        ("consider_then_return", {"eyeBallX": 0.55, "eyeBallY": -0.2,
                                  "bodyAngleY": -0.09, "browLY": 0.45, "browRY": -0.18}),
    ],
    "soft_smile": [
        ("warm_nod", {"bodyAngleY": 0.12, "mouthForm": 0.87}),
        ("smile_left", {"bodyAngleZ": -0.1, "blushLevel": 0.25}),
        ("smile_right", {"bodyAngleZ": 0.1, "browLY": 0.24, "browRY": 0.24}),
    ],
    "closed_smile": [
        ("closed_smile_nod", {"bodyAngleY": 0.1}),
        ("closed_smile_left", {"bodyAngleZ": -0.09}),
        ("closed_smile_right", {"bodyAngleZ": 0.09}),
    ],
    "playful": [("playful_peek", {"eyeBallX": -0.24, "bodyAngleZ": 0.1}),
                ("playful_wink_left", {"eyeLOpen": 0.0, "eyeROpen": 0.92, "eyeSync": False}),
                ("playful_wink_right", {"eyeLOpen": 0.92, "eyeROpen": 0.0, "eyeSync": False})],
    "teasing": [("tease_left", {"eyeBallX": -0.22, "bodyAngleZ": -0.1}),
                ("tease_right", {"eyeBallX": 0.22, "bodyAngleZ": 0.1})],
    "angry": [("firm_glare", {"browLY": -0.68, "browRY": -0.68, "bodyAngleY": 0.1}),
              ("restrained_turn", {"bodyAngleX": -0.12, "eyeBallX": 0.14})],
    "sad": [("lower_gaze", {"eyeBallY": -0.32, "bodyAngleY": -0.12}),
            ("sad_look_back", {"eyeBallX": 0.18, "browLY": 0.25, "browRY": 0.25})],
    "gloomy": [("quiet_sink", {"eyeBallY": -0.3, "bodyAngleY": -0.12}),
               ("quiet_side_glance", {"eyeBallX": -0.2, "bodyAngleZ": -0.055})],
    "shy": [("shy_look_away", {"eyeBallX": -0.34, "bodyAngleZ": -0.1, "blushLevel": 0.8}),
            ("shy_peek_back", {"eyeBallX": 0.22, "bodyAngleZ": 0.07, "blushLevel": 0.75})],
    "surprised": [("small_gasp", {"mouthOpenBias": 0.48, "bodyAngleY": -0.12}),
                  ("startled_recoil", {"mouthOpenBias": 0.66, "bodyAngleY": -0.2, "browLY": 0.9, "browRY": 0.9})],
    "conflicted": [("question_left", {"eyeBallX": -0.22, "bodyAngleZ": -0.1}),
                   ("question_right", {"eyeBallX": 0.22, "bodyAngleZ": 0.1})],
}

QUIET_FAMILIES = {"calm", "listening", "thinking", "soft_smile", "closed_smile"}
NEGATIVE_FAMILIES = {"sad", "gloomy", "angry", "conflicted"}


def _number(value, default, minimum=0.0, maximum=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return max(minimum, min(maximum, float(value)))


def _bounded(params):
    result = {}
    for key, value in params.items():
        if key == "eyeSync":
            result[key] = bool(value)
        elif key in {"eyeLSmile", "eyeRSmile"}:
            result[key] = 0.0
        else:
            minimum = 0.0 if key in {"eyeLOpen", "eyeROpen", "mouthOpenBias", "headIntensity",
                                    "blushLevel", "breathLevel", "physicsImpulse"} else -1.0
            result[key] = round(_number(value, 0.0, minimum, 1.0), 3)
    return result


def _event(kind, patch, duration=800):
    return {"kind": kind, "durationMs": duration, "fadeInMs": min(240, duration // 3),
            "fadeOutMs": min(300, duration // 3), "patch": _bounded(patch), "returnToBase": True}


def _timeline_ms(sequence):
    total = 0
    for index, event in enumerate(sequence):
        next_event = sequence[index + 1] if index + 1 < len(sequence) else {}
        overlap = min(event.get("fadeOutMs", 0), next_event.get("fadeInMs", 0))
        total += max(1, event["durationMs"] - overlap)
    return total


def _family(intent, emotion, mode, rng):
    guard = intent.get("topic_guard") or {}
    if guard.get("must_preserve_theme", True) and not guard.get("allow_style_override", False):
        if guard.get("source_theme") == "crying":
            return "sad"
        if guard.get("source_theme") == "gloomy":
            return "gloomy"
        if guard.get("source_theme") == "serious_argument" and emotion not in NEGATIVE_FAMILIES:
            return "listening"
    if emotion in NEGATIVE_FAMILIES:
        return emotion
    requested = intent.get("expression_family")
    if requested in FAMILY_POSES:
        return requested
    if emotion in {"neutral", "happy", "playful", "teasing"}:
        attitude_family = {
            "smug": "teasing", "cheeky_wink": "playful", "goofy_face": "playful",
            "deadpan": "gloomy", "gloomy": "gloomy", "volatile": "conflicted",
            "meltdown": "angry", "shock_recoil": "surprised",
            "awkward": "listening", "tense_hold": "listening",
        }.get(mode)
        if attitude_family:
            return attitude_family
    if emotion == "happy":
        return rng.choice(["soft_smile", "closed_smile"]) if mode == "bright_talk" else "soft_smile"
    if emotion == "neutral":
        return "calm"
    return emotion if emotion in FAMILY_POSES else "calm"


def build_rushia_expression_plan(intent, previous_state, *, seed=None):
    rng = random.Random(seed)
    emotion = intent.get("emotion", intent.get("primary_emotion", "neutral"))
    emotion = emotion if emotion in {*FAMILY_POSES, "happy", "neutral"} else "neutral"
    original_mode = intent.get("performance_mode", "smile")
    guard = intent.get("topic_guard")
    guard = guard if isinstance(guard, dict) else {}
    intent = {**intent, "emotion": emotion, "topic_guard": guard}
    mode = resolve_effective_performance_mode(emotion, original_mode, guard)
    family = _family(intent, emotion, mode, rng)
    previous_variant = previous_state.get("expressionVariant") if isinstance(previous_state, dict) else None
    variants = FAMILY_VARIANTS[family]
    if mode == "cheeky_wink" and family == "playful":
        variants = [item for item in variants if "wink" in item[0]]
    variant, accent = rng.choice([item for item in variants if item[0] != previous_variant] or variants)
    intensity = _number(intent.get("intensity"), 0.35)
    strength = 0.75 + intensity * 0.3
    params = {**deepcopy(BASE_POSE_PRESETS["calm_soft"]), **MOTION_PARAM_DEFAULTS,
              "headIntensity": 0.14, "breathLevel": 0.22, "physicsImpulse": 0.03,
              "eyeLSmile": 0.0, "eyeRSmile": 0.0, "mouthOpenBias": 0.0,
              # Values are authored per eye/brow; legacy eyeSync also mirrors
              # the right brow angle and would overwrite Rushia's native pose.
              "eyeSync": False}
    for key, value in FAMILY_POSES[family].items():
        if key == "eyeSync":
            params[key] = value
        else:
            params[key] += (value - params[key]) * strength
    if emotion == "neutral" and mode == "deadpan" and family == "gloomy":
        params.update({"mouthForm": -0.08, "eyeLOpen": 0.72, "eyeROpen": 0.72,
                       "browLY": -0.08, "browRY": -0.08})
    params = _bounded(params)
    quiet = family in QUIET_FAMILIES
    profile = {"style": "calm_sway", "speed": 0.72 if quiet else 0.85,
               "swayScale": 0.36 if quiet else 0.55, "bobScale": 0.3 if quiet else 0.48,
               "twistScale": 0.35, "breathScale": 0.7, "headScale": 0.55}
    if family == "angry":
        profile["style"] = "locked_tense"
    elif family in {"sad", "gloomy"}:
        profile["style"] = "heavy_slow_sink"
    elif family == "surprised":
        profile["style"] = "quick_recoil"
    base = {"preset": f"rushia_{family}", "params": params, "durationSec": 1.6,
            "bodyMotionProfile": profile}

    accent = deepcopy(accent)
    if family == "closed_smile":
        accent.update({"eyeLOpen": 0.0, "eyeROpen": 0.0, "eyeSync": False, "mouthForm": 0.95})
    reaction_ms = 680 if family in {"closed_smile", "surprised", "playful"} else 1000
    reaction = _event(f"rushia_{variant}", accent, reaction_ms)
    deliberate_eye_close = accent.get("eyeLOpen") == 0.0 or accent.get("eyeROpen") == 0.0
    if deliberate_eye_close:
        # At the frontend's exponential smoothing rate (8/s), the longer
        # plateau reaches < 0.02 openness before the eyes begin reopening.
        reaction.update({"durationMs": 780, "fadeInMs": 100, "fadeOutMs": 150})
    sequence = [reaction]
    # A quiet gap is part of the timeline. It prevents repeated emphases from
    # becoming a continuous oscillation even without generated dialogue text.
    sequence.append(_event("rushia_rest", {}, 1300))
    if family in {"thinking", "listening"}:
        sequence.append(_event("rushia_return_attention", {"eyeBallX": 0.0, "eyeBallY": 0.0,
                                                          "bodyAngleY": 0.06}, 800))
    speaking_rate = _number(intent.get("speaking_rate"), 1.0, 0.65, 1.6)
    spoken_text = str(intent.get("spoken_text") or intent.get("dialogue_text") or "").strip()
    speaking_ms = min(14000, max(1800, int(len(spoken_text) * 95 / speaking_rate + 650))) if spoken_text else 0
    # Longer replies add one restrained emphasis, never a happy event to a low mood.
    if speaking_ms > 4500:
        sequence.append(_event("rushia_phrase_rest", {}, min(2200, speaking_ms // 3)))
        follow_patch = {"browLY": params["browLY"] + 0.08, "browRY": params["browRY"] + 0.08}
        sequence.append(_event("rushia_phrase_emphasis", follow_patch, 800))
    timeline_ms = _timeline_ms(sequence)
    settle_ms = rng.randint(500, 850)
    enter_ms = max(timeline_ms, speaking_ms, 1600) + settle_ms

    motion_theme = {
        "soft_smile": "happy_bright_talk", "closed_smile": "happy_bright_talk",
        "playful": "playful_tease", "teasing": "playful_tease", "angry": "angry_tension",
        "sad": "low_mood", "gloomy": "low_mood", "shy": "shy_tucked",
        "surprised": "surprised_recoil", "conflicted": "uneasy_shift",
    }.get(family)
    requested_motion = motion_theme is not None and intent.get("motion_theme") == motion_theme
    if quiet and not requested_motion:
        motion = {"theme": f"rushia_{family}", "variant": variant, "durationMs": timeline_ms,
                  "blendInMs": 300, "blendOutMs": 650, "phaseSeed": round(rng.uniform(0, 6.283), 3),
                  "body": {"sway": 0.65, "bob": 0.55, "twist": 0.6, "spring": 0.0},
                  "head": {"yaw": 0.65, "pitch": 0.65, "roll": 0.65, "lagMs": 100}}
    else:
        motion_intent = {"motion_theme": motion_theme}
        if requested_motion:
            motion_intent["motion_variant"] = intent.get("motion_variant")
        motion = build_motion_plan(family, mode, intensity, 0.35, 0.2, motion_intent, previous_state, rng=rng)
        motion["durationMs"] = min(timeline_ms, 3200)
        motion["body"]["spring"] = min(motion["body"]["spring"], 0.25)
    eye_motion = build_eye_motion_plan(family if family != "calm" else "neutral", mode,
                                     intensity * 0.5, 0.2, {}, timeline_ms, rng=rng)
    eye_motion["amplitudeX"] = min(0.09, eye_motion["amplitudeX"])
    eye_motion["amplitudeY"] = min(0.035, eye_motion["amplitudeY"])
    if quiet:
        eye_motion["style"] = "soft_saccade"
        eye_motion["frequencyHz"] = 0.42
        eye_motion["intensity"] = 0.2

    idle_name = {"sad": "crying_idle", "gloomy": "gloomy_idle", "angry": "angry_glare_idle",
                 "shy": "shy_idle", "conflicted": "conflicted_idle", "surprised": "surprised_idle",
                 "soft_smile": "happy_idle", "closed_smile": "happy_idle"}.get(family, "neutral_idle")
    settle = deepcopy(params)
    baseline = {**BASE_POSE_PRESETS["calm_soft"], **MOTION_PARAM_DEFAULTS, "mouthOpenBias": 0.0}
    for key in ("mouthForm", "browLY", "browRY", "browLAngle", "browRAngle", "browLForm", "browRForm",
                "eyeBallX", "eyeBallY", "bodyAngleX", "bodyAngleY", "bodyAngleZ", "blushLevel"):
        settle[key] = baseline[key] + (settle[key] - baseline[key]) * 0.6
    settle.update({"eyeLOpen": max(0.78, settle["eyeLOpen"]), "eyeROpen": max(0.78, settle["eyeROpen"]),
                   "mouthOpenBias": 0.0, "physicsImpulse": 0.015, "headIntensity": 0.1})
    settle = _bounded(settle)
    idle = {"name": idle_name, "mode": "loop", "enterAfterMs": enter_ms, "loopIntervalMs": 6500,
            "interruptible": True,
            "source": {"actionEnterAfterMs": timeline_ms, "speakingEnterAfterMs": speaking_ms,
                       "postSpeechHoldMs": settle_ms},
            "settlePose": {"preset": f"rushia_{family}_rest", "params": settle, "durationSec": 12,
                           "bodyMotionProfile": {**profile, "speed": 0.62, "bobScale": 0.2}},
            "loopEvents": [_event("rushia_idle_attention", {"eyeBallX": 0.08, "bodyAngleZ": 0.025}, 1000)]}
    signature = {"signature_name": f"rushia_{family}"}
    carry = build_carry_state({**intent, "performance_mode": mode}, signature, params, 0.0)
    carry.update({"expressionFamily": family, "expressionVariant": variant,
                  "motionTheme": motion["theme"], "motionVariant": motion["variant"]})
    blink_style = intent.get("blink_style", "normal")
    blink_style = blink_style if blink_style in BLINK_STRATEGIES else "normal"
    # Reset a preceding shy/slow interval when the new turn requests normal blinking.
    commands = [{"action": "resume"}, {"action": "set_interval", "intervalMin": 2.5, "intervalMax": 5.0}]
    if deliberate_eye_close:
        commands.append({"action": "pause", "durationSec": 0.95})
    else:
        commands.extend(deepcopy(BLINK_STRATEGIES[blink_style]))
    return {"type": "expression_plan", "basePose": base, "microEvents": [], "sequence": sequence,
            "motionPlan": motion, "eyeMotionPlan": eye_motion, "idlePlan": idle,
            "blinkPlan": {"style": blink_style, "commands": commands}, "speakingRate": speaking_rate,
            "timingHints": {"holdMs": 1600, "basePoseDurationSec": 1.6, "sequenceSteps": len(sequence),
                            "settleMs": settle_ms},
            "modelHints": {"modelName": "Rushia", "preset": base["preset"], "variationRuleCount": len(variants)},
            "carryState": carry,
            "debug": {"intentPrimaryEmotion": emotion, "intentEmotion": emotion,
                      "intentPerformanceMode": mode, "originalPerformanceMode": original_mode,
                      "selectedBasePreset": base["preset"], "expressionFamily": family,
                      "expressionVariant": variant, "sourceTheme": guard.get("source_theme", "daily_talk"),
                      "guardActive": guard.get("must_preserve_theme", True), "modeDowngraded": mode != original_mode,
                      "arc": intent.get("arc", "steady"), "signature": signature["signature_name"],
                      "bodyMotionProfile": profile["style"], "bodyMotionProfileSource": "rushia_profile",
                      "motionTheme": motion["theme"], "motionVariant": motion["variant"],
                      "eyeMotionStyle": eye_motion["style"], "idlePlan": idle_name}}
