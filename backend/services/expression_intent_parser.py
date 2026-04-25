import json

from domain.expression_intent_schema import normalize_expression_intent as normalize_expression_intent_schema


PRIMARY_EMOTION_ALIASES = {
    "happy": "playful",
    "joyful": "playful",
    "cheerful": "playful",
    "smiling": "gentle",
    "soft_happy": "gentle",
}

SECONDARY_EMOTION_ALIASES = {
    "happy": "playful",
    "joyful": "playful",
    "cheerful": "playful",
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

    secondary_emotion = aliased.get("secondary_emotion")
    if isinstance(secondary_emotion, str):
        aliased["secondary_emotion"] = SECONDARY_EMOTION_ALIASES.get(secondary_emotion, secondary_emotion)

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
