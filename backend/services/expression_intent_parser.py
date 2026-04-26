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
}


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


def normalize_expression_intent(raw_intent: dict, emotion_state: dict | None, previous_state: dict | None) -> dict:
    del previous_state
    return normalize_expression_intent_schema(raw_intent, emotion_state=emotion_state)


def parse_expression_intent(raw_text: str, emotion_state: dict | None, previous_state: dict | None) -> dict:
    raw_intent = _apply_expression_aliases(_extract_first_json_object(raw_text))
    return normalize_expression_intent(raw_intent, emotion_state, previous_state)
