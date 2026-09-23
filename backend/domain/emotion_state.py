"""露西亞固定角色卡與 JEV Emotion 的封閉契約。"""

import math


PERSONALITY = {
    "name": "露西亞",
    "traits": [
        "嘴硬",
        "容易害羞",
        "不喜歡直接承認親密情緒",
        "熟人面前喜歡吐槽",
        "真正生氣時反而話會變少",
    ],
}

# JEV emotion scoring gets a separate, evidence-neutral profile. Keep behavioral
# style cues here instead of sending the chat persona's emotional traits as priors.
CHARACTER_EXPRESSION_PROFILE = {
    "name": "露西亞",
    "style": [
        "傾向間接表達親密感",
        "面對親密情緒時可能否認或轉移話題",
    ],
    "usage": "只協助解讀當輪模糊線索，不可直接提高任何情緒分數。",
}

EMOTION_FIELDS = (
    "shy",
    "pleased",
    "genuinely_angry",
    "sad_or_hurt",
    "masking_positive_feeling",
    "wants_continue_interaction",
)

NEUTRAL_EMOTION_STATE = {
    "shy": 0.0,
    "pleased": 0.0,
    "genuinely_angry": 0.0,
    "sad_or_hurt": 0.0,
    "masking_positive_feeling": 0.0,
    "wants_continue_interaction": 0.5,
}


def validate_emotion_state(value: object) -> dict[str, float] | None:
    """完整驗證後才回傳副本；任何非法欄位都使整份 state 無效。"""
    if not isinstance(value, dict) or set(value) != set(EMOTION_FIELDS):
        return None
    state = {}
    for field in EMOTION_FIELDS:
        score = value[field]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0.0 <= score <= 1.0
        ):
            return None
        state[field] = float(score)
    return state


def state_from_jev_answers(answers: object) -> dict[str, float] | None:
    """System One 的六個 Noul answers 原子轉換成 Emotion State。"""
    if not isinstance(answers, dict) or set(answers) != set(EMOTION_FIELDS):
        return None
    scores = {}
    for field in EMOTION_FIELDS:
        answer = answers[field]
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            return None
        scores[field] = answer.get("noul")
    return validate_emotion_state(scores)


def resolve_emotion_state(
    answers: object, previous_state: object
) -> tuple[dict[str, float], str]:
    current = state_from_jev_answers(answers)
    if current is not None:
        return current, "jev"
    previous = validate_emotion_state(previous_state)
    if previous is not None:
        return previous, "previous_fallback"
    return dict(NEUTRAL_EMOTION_STATE), "neutral_fallback"
