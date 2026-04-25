import json

from domain.expression_intent_schema import normalize_expression_intent


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


def parse_expression_intent(raw_text: str, emotion_state: dict | None, previous_state: dict | None) -> dict:
    del previous_state
    raw_intent = _extract_first_json_object(raw_text)
    return normalize_expression_intent(raw_intent, emotion_state=emotion_state)
