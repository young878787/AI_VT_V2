import json

from domain.expression_intent_schema import normalize_expression_intent as normalize_expression_intent_schema
from domain.expression_intent_schema import ALLOWED_EMOTIONS, ALLOWED_PERFORMANCE_MODES


PRIMARY_EMOTION_ALIASES = {
    "joyful": "happy",
    "cheerful": "happy",
    "smiling": "happy",
    "soft_happy": "happy",
    "calm": "neutral",
    "gentle": "neutral",
    "embarrassed": "shy",
    "annoyed": "angry",
}

SECONDARY_EMOTION_ALIASES = {
    "joyful": "happy",
    "cheerful": "happy",
    "calm": "neutral",
    "gentle": "neutral",
    "embarrassed": "shy",
    "annoyed": "angry",
}

PERFORMANCE_MODE_ALIASES = {
    "daily_talk": "bright_talk",
    "talk": "bright_talk",
    "natural": "smile",
    "funny_face": "goofy_face",
    "goofy": "goofy_face",
    "wink": "cheeky_wink",
    "flat": "deadpan",
    "dark": "gloomy",
    "unstable": "volatile",
    "breakdown": "meltdown",
    "cringe": "awkward",
    "tense": "tense_hold",
    "shock": "shock_recoil",
}

ARC_ALIASES = {
    "neutral_to_smile": "pause_then_smirk",
    "smile_then_brighten": "pop_then_settle",
    "widen_then_smile": "widen_then_tease",
    "tense_then_flat": "glare_then_flatten",
    "neutral_to_teasing": "widen_then_tease",
}


def _merge_emotion_state_hints(raw_intent: dict, emotion_state: dict | None) -> dict:
    if not emotion_state:
        return raw_intent

    merged = dict(raw_intent)
    if "emotion" not in merged and "primary_emotion" not in merged:
        primary = emotion_state.get("primary_emotion") or emotion_state.get("emotion")
        if primary:
            merged["emotion"] = primary
    if "secondary_emotion" not in merged and emotion_state.get("secondary_emotion"):
        merged["secondary_emotion"] = emotion_state["secondary_emotion"]
    if "tempo" not in merged and emotion_state.get("pace"):
        pace = str(emotion_state["pace"])
        merged["tempo"] = "fast" if pace == "medium_fast" else pace
    if "arc" not in merged and emotion_state.get("expression_arc"):
        merged["arc"] = emotion_state["expression_arc"]
    if "asymmetry_bias" not in merged and emotion_state.get("asymmetry_bias"):
        asymmetry = str(emotion_state["asymmetry_bias"])
        merged["asymmetry_bias"] = "subtle" if asymmetry == "slight" else asymmetry

    return merged


def _extract_first_json_object(raw_text: str) -> dict:
    decoder = json.JSONDecoder()
    text = raw_text or ""

    for index, char in enumerate(text):
        if char != "{":
            continue

        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue

        if isinstance(value, dict):
            return value

    return {}


def _apply_expression_aliases(raw_intent: dict) -> dict:
    aliased = dict(raw_intent)

    primary_emotion = aliased.get("primary_emotion")
    if isinstance(primary_emotion, str):
        aliased["primary_emotion"] = PRIMARY_EMOTION_ALIASES.get(primary_emotion, primary_emotion)

    emotion = aliased.get("emotion")
    if isinstance(emotion, str):
        aliased["emotion"] = PRIMARY_EMOTION_ALIASES.get(emotion, emotion)

    secondary_emotion = aliased.get("secondary_emotion")
    if isinstance(secondary_emotion, str):
        aliased["secondary_emotion"] = SECONDARY_EMOTION_ALIASES.get(secondary_emotion, secondary_emotion)

    performance_mode = aliased.get("performance_mode")
    if isinstance(performance_mode, str):
        aliased["performance_mode"] = PERFORMANCE_MODE_ALIASES.get(performance_mode, performance_mode)

    arc = aliased.get("arc")
    if isinstance(arc, str):
        aliased["arc"] = ARC_ALIASES.get(arc, arc)

    return aliased


def _infer_direct_expression_override(user_message: str | None) -> dict:
    text = (user_message or "").strip().lower()
    if not text:
        return {}

    angry_cues = (
        "生氣",
        "憤怒",
        "不爽",
        "火大",
        "爆氣",
        "怒",
        "兇",
        "瞪",
        "惱火",
    )
    goofy_cues = ("鬼臉", "搞怪", "做鬼臉", "扮鬼臉")
    sad_cues = ("難過", "哭", "委屈", "傷心", "沮喪")

    if any(cue in text for cue in angry_cues):
        performance_mode = "meltdown" if any(cue in text for cue in ("爆氣", "暴怒", "超生氣")) else "deadpan"
        return {
            "emotion": "angry",
            "performance_mode": performance_mode,
        }

    if any(cue in text for cue in goofy_cues):
        return {
            "emotion": "playful",
            "performance_mode": "goofy_face",
        }

    if any(cue in text for cue in sad_cues):
        return {
            "emotion": "sad",
            "performance_mode": "tense_hold",
        }

    return {}


def normalize_expression_intent(raw_intent: dict, emotion_state: dict | None, previous_state: dict | None) -> dict:
    del previous_state
    return normalize_expression_intent_schema(raw_intent, emotion_state=emotion_state)


def parse_expression_intent(
    raw_text: str,
    emotion_state: dict | None,
    previous_state: dict | None,
    user_message: str | None = None,
) -> dict:
    raw_intent = _merge_emotion_state_hints(_extract_first_json_object(raw_text), emotion_state)
    raw_intent = _apply_expression_aliases(raw_intent)
    direct_override = _infer_direct_expression_override(user_message)
    if direct_override:
        raw_intent = {
            **raw_intent,
            **direct_override,
        }
    return normalize_expression_intent(raw_intent, emotion_state, previous_state)
