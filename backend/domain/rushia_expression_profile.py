"""Rushia poses and bounded variations using the shared expression-plan contract."""

from copy import deepcopy
import math
import random

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_compiler_rules import MOTION_PARAM_DEFAULTS
from domain.expression_continuity import build_carry_state
from domain.expression_eye_motion_library import EYE_MOTION_STYLES, build_eye_motion_plan
from domain.expression_intent_schema import ALLOWED_ARCS
from domain.expression_motion_library import build_motion_plan
from domain.expression_presets import BASE_POSE_PRESETS
from domain.expression_visual_signature import resolve_effective_performance_mode
from domain.speech_segments import split_speech_segments


# Rushia has no separate smile-eye parameter. A brief closed-eye pose supplies
# that silhouette; base and idle poses always reopen the eyes.
_SOURCE_FAMILY_POSES = {
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
_SOURCE_FAMILY_VARIANTS = {
    "calm": [
        ("small_nod", {"bodyAngleY": 0.09, "headPitchOffset": -0.35, "browLY": 0.1, "browRY": 0.1}),
        ("quiet_glance", {"eyeBallX": -0.5, "bodyAngleZ": -0.045}),
        ("gentle_acknowledgement", {"browLY": 0.2, "browRY": 0.2, "mouthForm": 0.2}),
    ],
    "listening": [
        ("attentive_nod", {"bodyAngleY": 0.14, "eyeBallY": -0.05, "headPitchOffset": -0.4}),
        ("listen_left", {"bodyAngleZ": -0.09, "eyeBallX": -0.5, "browLY": 0.23}),
        ("listen_right", {"bodyAngleZ": 0.09, "eyeBallX": 0.5, "browRY": 0.23}),
    ],
    "thinking": [
        ("look_up_left", {"eyeBallX": -0.8, "eyeBallY": 0.32, "bodyAngleZ": -0.08}),
        ("look_up_right", {"eyeBallX": 0.8, "eyeBallY": 0.32, "bodyAngleZ": 0.08}),
        ("consider_then_return", {"eyeBallX": 0.72, "eyeBallY": -0.3,
                                  "bodyAngleY": -0.09, "browLY": 0.45, "browRY": -0.18}),
    ],
    "soft_smile": [
        ("warm_nod", {"bodyAngleY": 0.12, "mouthForm": 0.87, "headPitchOffset": -0.32}),
        ("smile_left", {"bodyAngleZ": -0.1, "blushLevel": 0.25}),
        ("smile_right", {"bodyAngleZ": 0.1, "browLY": 0.24, "browRY": 0.24}),
    ],
    "closed_smile": [
        ("closed_smile_nod", {"bodyAngleY": 0.1, "headPitchOffset": -0.3}),
        ("closed_smile_left", {"bodyAngleZ": -0.09}),
        ("closed_smile_right", {"bodyAngleZ": 0.09}),
    ],
    "playful": [("playful_peek", {"eyeBallX": -0.55, "bodyAngleZ": 0.1}),
                ("playful_wink_left", {"eyeLOpen": 0.0, "eyeROpen": 0.92, "eyeSync": False}),
                ("playful_wink_right", {"eyeLOpen": 0.92, "eyeROpen": 0.0, "eyeSync": False})],
    "teasing": [("tease_left", {"eyeBallX": -0.55, "bodyAngleZ": -0.1}),
                ("tease_right", {"eyeBallX": 0.55, "bodyAngleZ": 0.1})],
    "angry": [("firm_glare", {"browLY": -0.68, "browRY": -0.68, "bodyAngleY": 0.1}),
              ("restrained_turn", {"bodyAngleX": -0.12, "eyeBallX": 0.14})],
    "sad": [("lower_gaze", {"eyeBallY": -0.5, "bodyAngleY": -0.12}),
            ("sad_look_back", {"eyeBallX": 0.4, "browLY": 0.25, "browRY": 0.25})],
    "gloomy": [("quiet_sink", {"eyeBallY": -0.48, "bodyAngleY": -0.12}),
               ("quiet_side_glance", {"eyeBallX": -0.45, "bodyAngleZ": -0.055})],
    "shy": [("shy_look_away", {"eyeBallX": -0.65, "bodyAngleZ": -0.1, "blushLevel": 0.8}),
            ("shy_peek_back", {"eyeBallX": 0.45, "bodyAngleZ": 0.07, "blushLevel": 0.75})],
    "surprised": [("small_gasp", {"mouthOpenBias": 0.48, "bodyAngleY": -0.12}),
                  ("startled_recoil", {"mouthOpenBias": 0.66, "bodyAngleY": -0.2, "browLY": 0.9, "browRY": 0.9})],
    "conflicted": [("question_left", {"eyeBallX": -0.5, "bodyAngleZ": -0.1}),
                   ("question_right", {"eyeBallX": 0.5, "bodyAngleZ": 0.1})],
}

FAMILY_ALIASES = {"listening": "thinking", "teasing": "playful"}


def canonical_rushia_family(family):
    return FAMILY_ALIASES.get(family, family)


# The catalog exposes canonical families; each variant keeps its authored pose.
FAMILY_POSES = {name: pose for name, pose in _SOURCE_FAMILY_POSES.items() if name not in FAMILY_ALIASES}
FAMILY_VARIANTS = {name: list(variants) for name, variants in _SOURCE_FAMILY_VARIANTS.items()
                   if name not in FAMILY_ALIASES}
for _source, _canonical in FAMILY_ALIASES.items():
    FAMILY_VARIANTS[_canonical].extend(_SOURCE_FAMILY_VARIANTS[_source])
_VARIANT_SOURCE_FAMILIES = {variant: family for family, variants in _SOURCE_FAMILY_VARIANTS.items()
                          for variant, _patch in variants}

QUIET_FAMILIES = {"calm", "thinking", "soft_smile", "closed_smile"}
NEGATIVE_FAMILIES = {"sad", "gloomy", "angry", "conflicted"}
RUSHIA_IDLE_FAMILIES = {
    "neutral_idle": "calm", "happy_idle": "soft_smile", "crying_idle": "sad",
    "gloomy_idle": "gloomy", "angry_glare_idle": "angry", "shy_idle": "shy",
    "surprised_idle": "surprised", "conflicted_idle": "conflicted",
}


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


def _body_profile(family, energy=0.35):
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
    profile["speed"] = round(profile["speed"] * (1 + (energy - 0.35) * 0.45), 3)
    for key in ("swayScale", "bobScale", "twistScale", "headScale"):
        profile[key] = round(profile[key] * (1 + (energy - 0.35) * 0.65), 3)
    return profile


def _arc_sequence(reaction, params, arc, family):
    rest = _event("rushia_rest", {}, 1300)
    sequence = [reaction, rest]
    if arc == "pop_then_settle":
        reaction["patch"] = _bounded({**reaction["patch"],
                                     "browLY": params["browLY"] + 0.12,
                                     "browRY": params["browRY"] + 0.12,
                                     "bodyAngleY": params["bodyAngleY"] - 0.12})
        rest.update(_event("rushia_arc_settle", {"bodyAngleY": params["bodyAngleY"],
                                                "browLY": params["browLY"],
                                                "browRY": params["browRY"]}, 1100))
    elif arc == "pause_then_smirk":
        sequence.insert(0, _event("rushia_arc_pause", {"eyeBallX": 0.0, "bodyAngleZ": 0.0}, 720))
        if family in {"calm", "soft_smile", "closed_smile", "playful", "shy"}:
            reaction["patch"]["mouthForm"] = min(1.0, max(params["mouthForm"], 0.0) + 0.16)
    elif arc == "widen_then_tease":
        sequence.insert(0, _event("rushia_arc_widen", {
            "eyeLOpen": params["eyeLOpen"] + 0.15, "eyeROpen": params["eyeROpen"] + 0.15,
            "browLY": params["browLY"] + 0.1, "browRY": params["browRY"] + 0.1,
        }, 680))
    elif arc == "shrink_then_recover":
        sequence.insert(0, _event("rushia_arc_shrink", {
            "bodyAngleY": params["bodyAngleY"] - 0.12, "eyeBallY": params["eyeBallY"] - 0.18,
            "eyeLOpen": params["eyeLOpen"] - 0.08, "eyeROpen": params["eyeROpen"] - 0.08,
        }, 950))
        rest.update(_event("rushia_arc_recover", {"eyeBallY": params["eyeBallY"],
                                                 "bodyAngleY": params["bodyAngleY"]}, 1100))
    elif arc == "glare_then_flatten":
        sequence.insert(0, _event("rushia_arc_glare", {
            "browLY": params["browLY"] - 0.12, "browRY": params["browRY"] - 0.12,
            "eyeLOpen": params["eyeLOpen"] - 0.09, "eyeROpen": params["eyeROpen"] - 0.09,
            "bodyAngleY": params["bodyAngleY"] + 0.08,
        }, 900))
        rest.update(_event("rushia_arc_relax", {"browLY": params["browLY"] * 0.75,
                                               "browRY": params["browRY"] * 0.75,
                                               "bodyAngleY": params["bodyAngleY"]}, 1100))
    return sequence


def _ambient_states(settle, family, energy, rng):
    # Keep the current emotional face in all three existing ambient states.
    # Shared ambient templates contain neutral/smiling mouths and would erase it.
    direction = rng.choice((-1, 1))
    gaze = (0.22 + energy * 0.12) * direction
    lowered = family in {"sad", "gloomy", "shy"}
    patches = [
        ("ambient_idle_breath", {"eyeBallX": 0.0, "bodyAngleZ": 0.0,
                                 "breathLevel": 0.26 + energy * 0.08}),
        ("ambient_idle_look_around", {"eyeBallX": gaze, "bodyAngleZ": 0.045 * direction,
                                      "eyeBallY": settle["eyeBallY"] - (0.05 if lowered else 0.0)}),
        ("ambient_idle_active_shift", {"eyeBallX": -gaze * 0.65, "bodyAngleZ": -0.06 * direction,
                                       "bodyAngleY": settle["bodyAngleY"] + (-0.045 if lowered else 0.055)}),
    ]
    return [{"kind": kind, "params": _bounded({**settle, **patch})} for kind, patch in patches]


def _family(intent, emotion, mode, rng):
    guard = intent.get("topic_guard") or {}
    if guard.get("must_preserve_theme", True) and not guard.get("allow_style_override", False):
        if guard.get("source_theme") == "crying":
            return "sad"
        if guard.get("source_theme") == "gloomy":
            return "gloomy"
        if guard.get("source_theme") == "serious_argument" and emotion not in NEGATIVE_FAMILIES:
            return "thinking"
    if emotion in NEGATIVE_FAMILIES:
        return emotion
    requested = canonical_rushia_family(intent.get("expression_family"))
    if requested in FAMILY_POSES:
        return requested
    if emotion in {"neutral", "happy", "playful", "teasing"}:
        attitude_family = {
            "smug": "playful", "cheeky_wink": "playful", "goofy_face": "playful",
            "deadpan": "gloomy", "gloomy": "gloomy", "volatile": "conflicted",
            "meltdown": "angry", "shock_recoil": "surprised",
            "awkward": "thinking", "tense_hold": "thinking",
        }.get(mode)
        if attitude_family:
            return attitude_family
    if emotion == "happy":
        return rng.choice(["soft_smile", "closed_smile"]) if mode == "bright_talk" else "soft_smile"
    if emotion == "neutral":
        return "calm"
    canonical_emotion = canonical_rushia_family(emotion)
    return canonical_emotion if canonical_emotion in FAMILY_POSES else "calm"


def _speech_rhythm_events(text, timings, params, energy):
    texts = split_speech_segments(text)
    cues = []
    for timing in timings:
        if timing["id"] >= len(texts):
            continue
        segment = texts[timing["id"]].strip().strip('「」『』“”"')
        transition = segment.startswith(("不過", "但是", "其實", "然而", "可是"))
        pause = "…" in segment or "..." in segment
        delay_ms = 900 if pause else 750 if transition else 500
        if segment.endswith(("？", "?")):
            kind, patch = "rushia_speech_question", {"browLY": params["browLY"] + 0.09,
                                                    "browRY": params["browRY"] + 0.09,
                                                    "eyeBallY": params["eyeBallY"] + 0.04}
        elif segment.endswith(("！", "!")):
            kind, patch = "rushia_speech_emphasis", {"headPitchOffset": -0.4 - energy * 0.12}
        elif transition or pause:
            kind = "rushia_speech_transition" if transition else "rushia_speech_pause_return"
            patch = {"eyeBallX": 0.0, "eyeBallY": params["eyeBallY"], "headPitchOffset": -0.2}
        else:
            continue
        at_ms = timing["startMs"] + delay_ms
        # Avoid a gesture running into the next phrase or the final quiet tail.
        if at_ms + 1120 > timing["endMs"]:
            continue
        event = _event(kind, patch, 720)
        event["atMs"] = at_ms
        # Repeated punctuation should leave room for the shuffled small gestures.
        if not cues or (kind != cues[-1]["kind"] and at_ms - cues[-1]["atMs"] >= 2500):
            cues.append(event)
    return cues


def _speech_plan(plan, segments, timing_source, energy, rng, text):
    if timing_source not in {"audio", "estimated"} or not isinstance(segments, list) or not segments:
        raise ValueError("Speech requires segments and an audio or estimated timing source")
    timings = []
    previous_end = 0
    previous_id = -1
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Invalid speech segment")
        segment_id, start, end = segment.get("id"), segment.get("startMs"), segment.get("endMs")
        if (isinstance(segment_id, bool) or not isinstance(segment_id, int) or segment_id <= previous_id
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in (start, end))
                or start < previous_end or end <= start):
            raise ValueError("Speech segments must have increasing IDs and finite nonoverlapping times")
        timings.append({"id": segment_id, "startMs": start, "endMs": end})
        previous_id, previous_end = segment_id, end
    duration_ms = timings[-1]["endMs"]
    params = plan["basePose"]["params"]
    family = plan["debug"]["expressionFamily"]
    lowered = family in {"sad", "gloomy", "shy"}
    gestures = [
        ("rushia_speech_nod", {"headPitchOffset": -0.3 - energy * 0.15}),
        ("rushia_speech_glance_left", {"eyeBallX": -0.22, "bodyAngleZ": -0.035}),
        ("rushia_speech_glance_right", {"eyeBallX": 0.22, "bodyAngleZ": 0.035}),
        ("rushia_speech_attention", {"browLY": params["browLY"] + 0.05,
                                     "browRY": params["browRY"] + 0.05,
                                     "eyeBallY": params["eyeBallY"] + (-0.05 if lowered else 0.04)}),
    ]
    # Keep the current face; punctuation boundaries time small gestures, not
    # inferred new emotions. Never repeat the initial wink or open-mouth shock.
    events = []
    choices = []
    at_ms = rng.randint(750, 1100)
    last_kind = None
    while at_ms + 1300 <= duration_ms:
        if not choices:
            choices = list(gestures)
            rng.shuffle(choices)
            if choices[-1][0] == last_kind:
                choices[0], choices[-1] = choices[-1], choices[0]
        kind, patch = choices.pop()
        nearby = [timing["startMs"] + 250 for timing in timings
                  if abs(timing["startMs"] + 250 - at_ms) <= 400]
        if nearby:
            candidate = min(nearby, key=lambda point: abs(point - at_ms))
            if (not events or candidate - events[-1]["atMs"] >= 2500) and candidate + 1300 <= duration_ms:
                at_ms = candidate
        event = _event(kind, patch, 850 if "glance" in kind else 720)
        event["atMs"] = round(at_ms, 3)
        events.append(event)
        last_kind = kind
        at_ms += round(rng.uniform(3300, 4600) * (1 - energy * 0.2))
    cues = _speech_rhythm_events(text, timings, params, energy)
    if cues:
        events = [event for event in events if all(abs(event["atMs"] - cue["atMs"]) >= 2500 for cue in cues)]
        events = sorted(events + cues, key=lambda event: event["atMs"])
    plan["stage"] = "speech"
    plan["speech"] = {"durationMs": duration_ms, "timingSource": timing_source, "segments": timings}
    plan["sequence"] = []
    plan["microEvents"] = events
    plan.pop("motionPlan", None)
    plan["eyeMotionPlan"]["durationMs"] = duration_ms
    plan["basePose"]["durationSec"] = min(1.6, duration_ms / 1000)
    plan["blinkPlan"]["commands"] = [command for command in plan["blinkPlan"]["commands"]
                                       if command["action"] in {"resume", "set_interval"}]
    idle = plan["idlePlan"]
    idle["source"].update({"actionEnterAfterMs": max((event["atMs"] + event["durationMs"] for event in events), default=0),
                           "speakingEnterAfterMs": duration_ms})
    idle["enterAfterMs"] = duration_ms + idle["source"]["postSpeechHoldMs"]
    idle["ambientEnterAfterMs"] = idle["enterAfterMs"] + 900
    plan["timingHints"].update({"holdMs": plan["basePose"]["durationSec"] * 1000,
                                "basePoseDurationSec": plan["basePose"]["durationSec"], "sequenceSteps": 0})
    return plan


def build_rushia_expression_plan(intent, previous_state, *, seed=None, debug_overrides=None,
                                speech_segments=None, speech_timing_source="audio"):
    debug_overrides = debug_overrides or {}
    for key, allowed in (("eyeMotionStyle", EYE_MOTION_STYLES), ("blinkStyle", BLINK_STRATEGIES),
                         ("idleStyle", RUSHIA_IDLE_FAMILIES)):
        if key in debug_overrides and debug_overrides[key] not in allowed:
            raise ValueError(f"Unknown debug {key}: {debug_overrides[key]!r}")
    rng = random.Random(seed)
    emotion = intent.get("emotion", intent.get("primary_emotion", "neutral"))
    emotion = emotion if emotion in {*_SOURCE_FAMILY_POSES, "happy", "neutral"} else "neutral"
    original_mode = intent.get("performance_mode", "smile")
    guard = intent.get("topic_guard")
    guard = guard if isinstance(guard, dict) else {}
    intent = {**intent, "emotion": emotion, "topic_guard": guard}
    mode = resolve_effective_performance_mode(emotion, original_mode, guard)
    continuing_speech = (speech_segments is not None and isinstance(previous_state, dict)
                         and previous_state.get("emotion") == emotion
                         and previous_state.get("performanceMode") == mode)
    if continuing_speech and previous_state.get("expressionFamily") in FAMILY_POSES:
        intent["expression_family"] = previous_state["expressionFamily"]
    family = _family(intent, emotion, mode, rng)
    previous_variant = previous_state.get("expressionVariant") if isinstance(previous_state, dict) else None
    variants = FAMILY_VARIANTS[family]
    if mode == "cheeky_wink" and family == "playful":
        variants = [item for item in variants if "wink" in item[0]]
    requested_variant = debug_overrides.get("expressionVariant")
    if continuing_speech and requested_variant is None and any(item[0] == previous_variant for item in variants):
        requested_variant = previous_variant
    if requested_variant is not None:
        selected = next((item for item in FAMILY_VARIANTS[family] if item[0] == requested_variant), None)
        if selected is None:
            raise ValueError(f"Expression variant {requested_variant!r} does not belong to resolved family {family!r}")
        variant, accent = selected
    else:
        variant, accent = rng.choice([item for item in variants if item[0] != previous_variant] or variants)
    source_family = _VARIANT_SOURCE_FAMILIES[variant]
    intensity = _number(intent.get("intensity"), 0.35)
    energy = _number(intent.get("energy"), 0.35)
    arc = intent.get("arc", "steady")
    arc = arc if isinstance(arc, str) and arc in ALLOWED_ARCS else "steady"
    hold_ms = int(_number(intent.get("hold_ms"), 1600, 300, 4000))
    strength = 0.75 + intensity * 0.3
    params = {**deepcopy(BASE_POSE_PRESETS["calm_soft"]), **MOTION_PARAM_DEFAULTS,
              "headIntensity": 0.14, "breathLevel": 0.22, "physicsImpulse": 0.03,
              "eyeLSmile": 0.0, "eyeRSmile": 0.0, "mouthOpenBias": 0.0,
              # Values are authored per eye/brow; legacy eyeSync also mirrors
              # the right brow angle and would overwrite Rushia's native pose.
              "eyeSync": False}
    for key, value in _SOURCE_FAMILY_POSES[source_family].items():
        if key == "eyeSync":
            params[key] = value
        else:
            params[key] += (value - params[key]) * strength
    if emotion == "neutral" and mode == "deadpan" and family == "gloomy":
        params.update({"mouthForm": -0.08, "eyeLOpen": 0.72, "eyeROpen": 0.72,
                       "browLY": -0.08, "browRY": -0.08})
    params = _bounded(params)
    quiet = family in QUIET_FAMILIES
    profile = _body_profile(family, energy)
    base = {"preset": f"rushia_{source_family}", "params": params, "durationSec": hold_ms / 1000,
            "bodyMotionProfile": profile}

    accent = deepcopy(accent)
    for key in ("bodyAngleX", "bodyAngleY", "bodyAngleZ"):
        if key in accent:
            accent[key] = params[key] + (accent[key] - params[key]) * (1 + (energy - 0.35) * 0.6)
    if family == "closed_smile":
        accent.update({"eyeLOpen": 0.0, "eyeROpen": 0.0, "eyeSync": False, "mouthForm": 0.95})
    tempo_scale = 1 - (energy - 0.35) * 0.22
    reaction_ms = round((680 if source_family in {"closed_smile", "surprised", "playful"} else 1000) * tempo_scale)
    reaction = _event(f"rushia_{variant}", accent, reaction_ms)
    if any(accent.get(key, 0) != 0 for key in ("eyeBallX", "eyeBallY")):
        # A clear glance needs a short held target before the smooth return.
        reaction.update({"durationMs": max(1300, round(1300 * tempo_scale)), "fadeInMs": 120, "fadeOutMs": 320})
    deliberate_eye_close = accent.get("eyeLOpen") == 0.0 or accent.get("eyeROpen") == 0.0
    if deliberate_eye_close:
        # At the frontend's exponential smoothing rate (8/s), the longer
        # plateau reaches < 0.02 openness before the eyes begin reopening.
        reaction.update({"durationMs": 780, "fadeInMs": 100, "fadeOutMs": 150})
    # A quiet gap is part of the timeline. It prevents repeated emphases from
    # becoming a continuous oscillation even without generated dialogue text.
    sequence = _arc_sequence(reaction, params, arc, family)
    if family == "thinking":
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
    enter_ms = max(timeline_ms, speaking_ms, hold_ms) + settle_ms

    motion_theme = {
        "soft_smile": "happy_bright_talk", "closed_smile": "happy_bright_talk",
        "playful": "playful_tease", "angry": "angry_tension",
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
        motion = build_motion_plan(family, mode, intensity, energy, 0.2, motion_intent, previous_state, rng=rng)
        motion["durationMs"] = min(timeline_ms, 3200)
        motion["body"]["spring"] = min(motion["body"]["spring"], 0.25)
    eye_override = debug_overrides.get("eyeMotionStyle")
    eye_intent = {}
    if eye_override is not None:
        eye_intent["eye_motion_style"] = eye_override
    elif quiet:
        eye_intent["eye_motion_style"] = "soft_saccade"
    eye_motion = build_eye_motion_plan("neutral" if quiet else family, mode,
                                     intensity * 0.5, 0.2, eye_intent, timeline_ms, rng=rng)
    if eye_override is None:
        eye_motion["amplitudeX"] = min(0.09, eye_motion["amplitudeX"])
        eye_motion["amplitudeY"] = min(0.035, eye_motion["amplitudeY"])
    if quiet and eye_override is None:
        eye_motion["style"] = "soft_saccade"
        eye_motion["frequencyHz"] = 0.42
        eye_motion["intensity"] = 0.2

    idle_name = {"sad": "crying_idle", "gloomy": "gloomy_idle", "angry": "angry_glare_idle",
                 "shy": "shy_idle", "conflicted": "conflicted_idle", "surprised": "surprised_idle",
                 "soft_smile": "happy_idle", "closed_smile": "happy_idle"}.get(family, "neutral_idle")
    requested_idle = debug_overrides.get("idleStyle")
    idle_family = RUSHIA_IDLE_FAMILIES[requested_idle] if requested_idle else family
    if requested_idle:
        idle_name = requested_idle
    settle = deepcopy(params)
    if idle_family != family:
        settle = {**deepcopy(BASE_POSE_PRESETS["calm_soft"]), **MOTION_PARAM_DEFAULTS,
                  **FAMILY_POSES[idle_family], "eyeSync": False,
                  "eyeLSmile": 0.0, "eyeRSmile": 0.0, "mouthOpenBias": 0.0}
    baseline = {**BASE_POSE_PRESETS["calm_soft"], **MOTION_PARAM_DEFAULTS, "mouthOpenBias": 0.0}
    for key in ("mouthForm", "browLY", "browRY", "browLAngle", "browRAngle", "browLForm", "browRForm",
                "eyeBallX", "eyeBallY", "bodyAngleX", "bodyAngleY", "bodyAngleZ", "blushLevel"):
        settle[key] = baseline[key] + (settle[key] - baseline[key]) * 0.6
    settle.update({"eyeLOpen": max(0.78, settle["eyeLOpen"]), "eyeROpen": max(0.78, settle["eyeROpen"]),
                   "mouthOpenBias": 0.0, "physicsImpulse": 0.015, "headIntensity": 0.1})
    if settle["mouthForm"] < 0:
        # The existing ambient renderer jitters mouthForm by +/- 0.04.
        settle["mouthForm"] = min(-0.06, settle["mouthForm"])
    settle = _bounded(settle)
    ambient_interval_ms = round(rng.randint(4600, 6200) * (1 - (energy - 0.35) * 0.18))
    idle = {"name": idle_name, "mode": "loop", "enterAfterMs": enter_ms,
            "loopIntervalMs": ambient_interval_ms, "ambientEnterAfterMs": enter_ms + 900,
            "ambientSwitchIntervalMs": ambient_interval_ms,
            "interruptible": True,
            "source": {"actionEnterAfterMs": timeline_ms, "speakingEnterAfterMs": speaking_ms,
                       "postSpeechHoldMs": settle_ms},
            "settlePose": {"preset": f"rushia_{idle_family}_rest", "params": settle, "durationSec": 12,
                           "bodyMotionProfile": {**_body_profile(idle_family), "speed": 0.62, "bobScale": 0.2}},
            "loopEvents": [],
            "ambientPlan": {"states": _ambient_states(settle, idle_family, energy, rng)}}
    signature = {"signature_name": f"rushia_{family}"}
    carry = build_carry_state({**intent, "performance_mode": mode}, signature, params, 0.0)
    carry.update({"expressionFamily": family, "expressionVariant": variant,
                  "motionTheme": motion["theme"], "motionVariant": motion["variant"]})
    blink_style = debug_overrides.get("blinkStyle", intent.get("blink_style", "normal"))
    blink_style = blink_style if blink_style in BLINK_STRATEGIES else "normal"
    # Reset a preceding shy/slow interval when the new turn requests normal blinking.
    commands = [{"action": "resume"}, {"action": "set_interval", "intervalMin": 2.5, "intervalMax": 5.0}]
    if "blinkStyle" in debug_overrides:
        commands.extend(deepcopy(BLINK_STRATEGIES[blink_style]))
    if deliberate_eye_close:
        reaction_index = sequence.index(reaction)
        reaction_start_ms = _timeline_ms(sequence[:reaction_index])
        if reaction_index:
            reaction_start_ms -= min(sequence[reaction_index - 1]["fadeOutMs"], reaction["fadeInMs"])
        commands.append({"action": "pause", "durationSec": round((reaction_start_ms + 950) / 1000, 3)})
    elif "blinkStyle" not in debug_overrides:
        commands.extend(deepcopy(BLINK_STRATEGIES[blink_style]))
    plan = {"type": "expression_plan", "basePose": base, "microEvents": [], "sequence": sequence,
            "motionPlan": motion, "eyeMotionPlan": eye_motion, "idlePlan": idle,
            "blinkPlan": {"style": blink_style, "commands": commands}, "speakingRate": speaking_rate,
            "timingHints": {"holdMs": hold_ms, "basePoseDurationSec": hold_ms / 1000, "sequenceSteps": len(sequence),
                            "settleMs": settle_ms},
            "modelHints": {"modelName": "Rushia", "preset": base["preset"], "variationRuleCount": len(variants)},
            "carryState": carry,
            "debug": {"intentPrimaryEmotion": emotion, "intentEmotion": emotion,
                      "intentPerformanceMode": mode, "originalPerformanceMode": original_mode,
                      "selectedBasePreset": base["preset"], "expressionFamily": family,
                      "expressionVariant": variant, "sourceTheme": guard.get("source_theme", "daily_talk"),
                      "guardActive": guard.get("must_preserve_theme", True), "modeDowngraded": mode != original_mode,
                      "arc": arc, "signature": signature["signature_name"],
                      "bodyMotionProfile": profile["style"], "bodyMotionProfileSource": "rushia_profile",
                      "motionTheme": motion["theme"], "motionVariant": motion["variant"],
                      "eyeMotionStyle": eye_motion["style"], "idlePlan": idle_name}}
    if speech_segments is not None:
        return _speech_plan(plan, speech_segments, speech_timing_source, energy, rng, spoken_text)
    return plan
