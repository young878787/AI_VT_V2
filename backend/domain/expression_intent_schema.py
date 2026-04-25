ALLOWED_PRIMARY_EMOTIONS = {
    "neutral",
    "calm",
    "gentle",
    "playful",
    "teasing",
    "shy",
    "embarrassed",
    "annoyed",
    "angry",
    "surprised",
    "conflicted",
}

ALLOWED_ARCS = {
    "steady",
    "pop_then_settle",
    "pause_then_smirk",
    "widen_then_tease",
    "shrink_then_recover",
    "glare_then_flatten",
}

ALLOWED_BLINK_STYLES = {
    "normal",
    "focused_pause",
    "shy_fast",
    "teasing_pause",
    "surprised_hold",
    "sleepy_slow",
}

ALLOWED_ASYMMETRY_BIAS = {"auto", "none", "subtle", "strong"}

ALLOWED_TEMPOS = {"slow", "medium", "fast"}

DEFAULT_INTENT = {
    "primary_emotion": "calm",
    "secondary_emotion": "",
    "intensity": 0.35,
    "energy": 0.35,
    "dominance": 0.5,
    "playfulness": 0.3,
    "warmth": 0.5,
    "asymmetry_bias": "auto",
    "blink_style": "normal",
    "tempo": "medium",
    "arc": "steady",
    "hold_ms": 1600,
    "must_include": [],
    "avoid": [],
    "speaking_rate": 1.0,
}


def clamp_number(value: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(minimum, min(maximum, float(value)))


def normalize_expression_intent(raw_intent: dict, emotion_state: dict | None = None) -> dict:
    emotion_state = emotion_state or {}
    normalized = dict(DEFAULT_INTENT)
    normalized.update({key: value for key, value in raw_intent.items() if key in DEFAULT_INTENT})
    normalized["must_include"] = []
    normalized["avoid"] = []

    primary = raw_intent.get("primary_emotion")
    fallback_primary = emotion_state.get("primary_emotion")
    if primary in ALLOWED_PRIMARY_EMOTIONS:
        normalized["primary_emotion"] = primary
    elif fallback_primary in ALLOWED_PRIMARY_EMOTIONS:
        normalized["primary_emotion"] = fallback_primary
    else:
        normalized["primary_emotion"] = DEFAULT_INTENT["primary_emotion"]

    secondary = raw_intent.get("secondary_emotion", DEFAULT_INTENT["secondary_emotion"])
    normalized["secondary_emotion"] = secondary if secondary in ALLOWED_PRIMARY_EMOTIONS else ""

    normalized["intensity"] = clamp_number(
        raw_intent.get("intensity", emotion_state.get("intensity")),
        default=clamp_number(emotion_state.get("intensity"), DEFAULT_INTENT["intensity"], 0.0, 1.0),
        minimum=0.0,
        maximum=1.0,
    )
    normalized["energy"] = clamp_number(
        raw_intent.get("energy", emotion_state.get("energy")),
        default=clamp_number(emotion_state.get("energy"), DEFAULT_INTENT["energy"], 0.0, 1.0),
        minimum=0.0,
        maximum=1.0,
    )
    normalized["dominance"] = clamp_number(normalized.get("dominance"), DEFAULT_INTENT["dominance"], 0.0, 1.0)
    normalized["playfulness"] = clamp_number(normalized.get("playfulness"), DEFAULT_INTENT["playfulness"], 0.0, 1.0)
    normalized["warmth"] = clamp_number(normalized.get("warmth"), DEFAULT_INTENT["warmth"], 0.0, 1.0)

    if normalized.get("asymmetry_bias") not in ALLOWED_ASYMMETRY_BIAS:
        normalized["asymmetry_bias"] = DEFAULT_INTENT["asymmetry_bias"]
    if normalized.get("tempo") not in ALLOWED_TEMPOS:
        normalized["tempo"] = DEFAULT_INTENT["tempo"]

    if normalized.get("arc") not in ALLOWED_ARCS:
        normalized["arc"] = DEFAULT_INTENT["arc"]
    if normalized.get("blink_style") not in ALLOWED_BLINK_STYLES:
        normalized["blink_style"] = DEFAULT_INTENT["blink_style"]

    normalized["hold_ms"] = int(clamp_number(normalized.get("hold_ms"), DEFAULT_INTENT["hold_ms"], 300, 4000))
    normalized["speaking_rate"] = clamp_number(
        normalized.get("speaking_rate"),
        DEFAULT_INTENT["speaking_rate"],
        0.7,
        1.4,
    )
    normalized["must_include"] = list(raw_intent.get("must_include", [])) if isinstance(raw_intent.get("must_include"), list) else []
    normalized["avoid"] = list(raw_intent.get("avoid", [])) if isinstance(raw_intent.get("avoid"), list) else []
    return normalized
