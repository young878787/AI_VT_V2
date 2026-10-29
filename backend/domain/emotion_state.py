"""露西亞固定角色卡與跨輪情緒（mood_v1）的封閉契約。"""

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
}

EMOTION_FIELDS = (
    "shy",
    "pleased",
    "genuinely_angry",
    "sad_or_hurt",
    "masking_positive_feeling",
    "wants_continue_interaction",
)

# masking_positive_feeling 沿用舊 key，語意為表達軸：0 撒嬌討回應 ↔ 1 嘴硬。
NEUTRAL_EMOTION_STATE = {
    "shy": 0.0,
    "pleased": 0.0,
    "genuinely_angry": 0.0,
    "sad_or_hurt": 0.0,
    "masking_positive_feeling": 0.5,
    "wants_continue_interaction": 0.5,
}

PRIMARY_EMOTION_FIELDS = ("shy", "pleased", "genuinely_angry", "sad_or_hurt")

MOOD_SCHEMA = "mood_v1"
MOOD_HISTORY_LIMIT = 3
MOOD_STREAK_LIMIT = 99

# 欄位: (增益 g, 半衰輪數 T½)；基線取自 NEUTRAL_EMOTION_STATE。
_MOOD_PARAMETERS = {
    "shy": (0.35, 1.5),
    "pleased": (0.35, 3.0),
    "genuinely_angry": (0.40, 4.0),
    "sad_or_hurt": (0.35, 3.0),
    "masking_positive_feeling": (0.30, 2.0),
    "wants_continue_interaction": (0.25, 4.0),
}
MOOD_GAIN = {field: gain for field, (gain, _) in _MOOD_PARAMETERS.items()}
MOOD_HALF_LIFE_TURNS = {field: half_life for field, (_, half_life) in _MOOD_PARAMETERS.items()}
MOOD_RETENTION = {field: 0.5 ** (1.0 / half_life) for field, half_life in MOOD_HALF_LIFE_TURNS.items()}
MOOD_IDLE_HALF_LIFE_SEC = 2 * 60 * 60
MOOD_DEAD_ZONE = 0.3
MOOD_STRONG_SCORE = 3.5
MOOD_FULL_REPEATS = 3
MOOD_REPEAT_DECAY = 0.7

# 被拉高的主情緒 → 被扣的主情緒與係數；開心＋害羞、難過＋生氣刻意不對沖。
MOOD_OPPOSITION = {
    "shy": {"genuinely_angry": 0.3},
    "pleased": {"genuinely_angry": 0.5, "sad_or_hurt": 0.5},
    "genuinely_angry": {"shy": 0.3, "pleased": 0.5},
    "sad_or_hurt": {"pleased": 0.5},
}

# (門檻, 級距索引)：微 ≥ 0.15、明顯 ≥ 0.35、強烈 ≥ 0.60。
MOOD_LEVELS = ((0.60, 2), (0.35, 1), (0.15, 0))
MOOD_TEXT = {
    "shy": ("有點不好意思", "明顯害羞", "害羞到有點慌張"),
    "pleased": ("心情不錯", "很開心", "非常開心、雀躍"),
    "genuinely_angry": ("有點不爽", "真的生氣了", "非常生氣，話變少、態度冷淡"),
    "sad_or_hurt": ("有點失落", "難過、覺得受傷", "非常難過"),
}
MOOD_NAMES = {"shy": "害羞", "pleased": "開心", "genuinely_angry": "生氣", "sad_or_hurt": "難過"}
MOOD_IMPACT_THRESHOLD = 0.12
MOOD_AXIS_HIGH = 0.65
MOOD_AXIS_LOW = 0.35


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


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_turn_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def validate_mood_record(value: object) -> dict | None:
    """驗證 mood_v1 紀錄；任一欄位不合法（含舊六欄格式）都使整份無效。"""
    if not isinstance(value, dict) or set(value) != {
        "schema", "mood", "streak", "updated_at", "last_turn_id", "history",
    }:
        return None
    if value["schema"] != MOOD_SCHEMA:
        return None
    mood = validate_emotion_state(value["mood"])
    if mood is None:
        return None
    streak = value["streak"]
    if not isinstance(streak, dict) or set(streak) != set(PRIMARY_EMOTION_FIELDS):
        return None
    for count in streak.values():
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MOOD_STREAK_LIMIT:
            return None
    if not _finite_number(value["updated_at"]) or not _valid_turn_id(value["last_turn_id"]):
        return None
    history = value["history"]
    if not isinstance(history, list) or len(history) > MOOD_HISTORY_LIMIT:
        return None
    validated_history = []
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"turn_id", "changes"}:
            return None
        changes = entry["changes"]
        if (not _valid_turn_id(entry["turn_id"]) or not isinstance(changes, dict)
                or not set(changes) <= set(EMOTION_FIELDS)):
            return None
        if any(not _finite_number(delta) or not -1.0 <= delta <= 1.0 for delta in changes.values()):
            return None
        validated_history.append({
            "turn_id": entry["turn_id"],
            "changes": {field: float(changes[field]) for field in EMOTION_FIELDS if field in changes},
        })
    return {
        "schema": MOOD_SCHEMA,
        "mood": mood,
        "streak": {field: streak[field] for field in PRIMARY_EMOTION_FIELDS},
        "updated_at": float(value["updated_at"]),
        "last_turn_id": value["last_turn_id"],
        "history": validated_history,
    }


def idle_decayed_mood(record: dict | None, message_timestamp: float) -> dict[str, float]:
    """① 閒置回落：依距離上次套用的秒數向基線衰減；無紀錄回傳基線。"""
    if record is None:
        return dict(NEUTRAL_EMOTION_STATE)
    gap = message_timestamp - record["updated_at"]
    if gap <= 0:
        return dict(record["mood"])
    factor = 0.5 ** (gap / MOOD_IDLE_HALF_LIFE_SEC)
    return {
        field: NEUTRAL_EMOTION_STATE[field] + (record["mood"][field] - NEUTRAL_EMOTION_STATE[field]) * factor
        for field in EMOTION_FIELDS
    }


def _score_answers(answers: object) -> dict[str, float] | None:
    """六欄都是合法 score answer 才回傳 score，否則整份無效。"""
    if not isinstance(answers, dict):
        return None
    parsed = {}
    for field in EMOTION_FIELDS:
        answer = answers.get(field)
        if not isinstance(answer, dict) or answer.get("type") != "score":
            return None
        score = answer.get("score")
        if not _finite_number(score) or not 0.0 <= score <= 4.0:
            return None
        parsed[field] = float(score)
    return parsed


def _jev_delta(field: str, score: float, streak: int) -> float:
    """③ JEV 變化：死區視為不變；主情緒的弱上升依連續次數遞減。

    JEV score 已是 0～4 的連續值、會因不確定而往 2 收斂，故不再以 confidence 門檻擋下。
    """
    if abs(score - 2.0) < MOOD_DEAD_ZONE:
        return 0.0
    direction = (score - 2.0) / 2.0
    if field in PRIMARY_EMOTION_FIELDS and 0.0 < direction and score < MOOD_STRONG_SCORE:
        direction *= MOOD_REPEAT_DECAY ** max(0, streak - (MOOD_FULL_REPEATS - 1))
    return MOOD_GAIN[field] * direction


def apply_mood_turn(
    record: dict | None,
    current_mood: dict[str, float],
    answers: object,
    turn_id: str,
    message_timestamp: float,
) -> tuple[dict, dict[str, float], str]:
    """②～⑥：以閒置回落後的 current_mood 套用本輪；回傳 (紀錄, JEV 造成的 changes, source)。"""
    if record is not None and record["last_turn_id"] == turn_id:
        return record, {}, "previous_fallback"
    mood = {
        field: NEUTRAL_EMOTION_STATE[field]
        + (current_mood[field] - NEUTRAL_EMOTION_STATE[field]) * MOOD_RETENTION[field]
        for field in EMOTION_FIELDS
    }
    previous_streak = record["streak"] if record is not None else dict.fromkeys(PRIMARY_EMOTION_FIELDS, 0)
    history = list(record["history"]) if record is not None else []
    parsed = _score_answers(answers)
    if parsed is None:
        next_record = {
            "schema": MOOD_SCHEMA, "mood": mood,
            "streak": dict.fromkeys(PRIMARY_EMOTION_FIELDS, 0),
            "updated_at": float(message_timestamp), "last_turn_id": turn_id, "history": history,
        }
        return next_record, {}, "previous_fallback" if record is not None else "neutral_fallback"

    raw_changes = {}
    streak = {}
    for field in EMOTION_FIELDS:
        value = mood[field]
        delta = _jev_delta(field, parsed[field], previous_streak.get(field, 0))
        if delta > 0:
            updated = value + delta * (1.0 - value)
        elif field in PRIMARY_EMOTION_FIELDS:
            updated = max(0.0, value + delta)
        else:
            updated = value + delta * value
        raw_changes[field] = updated - value
        mood[field] = updated
        if field in PRIMARY_EMOTION_FIELDS:
            streak[field] = min(MOOD_STREAK_LIMIT, previous_streak[field] + 1) if delta > 0 else 0
    # ⑤ 對沖只由主情緒在 ④ 的實際上升量觸發。
    for raised, targets in MOOD_OPPOSITION.items():
        rise = raw_changes[raised]
        if rise > 0:
            for target, coefficient in targets.items():
                mood[target] = max(0.0, mood[target] - coefficient * rise)

    changes = {field: round(delta, 2) for field, delta in raw_changes.items() if abs(delta) >= 0.01}
    if changes:
        history = (history + [{"turn_id": turn_id, "changes": changes}])[-MOOD_HISTORY_LIMIT:]
    next_record = {
        "schema": MOOD_SCHEMA, "mood": mood, "streak": streak,
        "updated_at": float(message_timestamp), "last_turn_id": turn_id, "history": history,
    }
    return next_record, changes, "jev"


def _mood_level(value: float) -> int | None:
    return next((level for threshold, level in MOOD_LEVELS if value >= threshold), None)


def build_mood_lines(mood: dict[str, float], mood_changes: dict[str, float] | None = None) -> list[str]:
    """Chat 提詞投影：主要情緒、表達方式、互動意願固定出現；輔助情緒、本輪影響有才列。"""
    ranked = sorted(PRIMARY_EMOTION_FIELDS, key=lambda field: -mood[field])
    first, second = ranked[0], ranked[1]
    first_level = _mood_level(mood[first])
    if first_level is None:
        lines = ["主要情緒：平穩"]
    else:
        lines = [f"主要情緒：{MOOD_TEXT[first][first_level]}"]
        second_level = _mood_level(mood[second])
        if second_level is not None and mood[second] >= mood[first] / 2:
            lines.append(f"輔助情緒：{MOOD_TEXT[second][second_level]}")
    impacts = {field: delta for field, delta in (mood_changes or {}).items()
               if field in PRIMARY_EMOTION_FIELDS and abs(delta) >= MOOD_IMPACT_THRESHOLD}
    if impacts:
        field = max(impacts, key=lambda name: abs(impacts[name]))
        name = MOOD_NAMES[field]
        lines.append(f"本輪影響：這句話讓你更{name}" if impacts[field] > 0
                     else f"本輪影響：你的{name}因這句話緩和下來")
    masking = mood["masking_positive_feeling"]
    if masking >= MOOD_AXIS_HIGH:
        lines.append("表達方式：嘴硬，不直接承認正面感受")
    elif masking <= MOOD_AXIS_LOW:
        lines.append("表達方式：坦率撒嬌，想討對方回應")
    else:
        lines.append("表達方式：照平常的樣子表達")
    interaction = mood["wants_continue_interaction"]
    if interaction >= MOOD_AXIS_HIGH:
        lines.append("互動意願：想繼續聊，可以主動延伸話題")
    elif interaction <= MOOD_AXIS_LOW:
        lines.append("互動意願：興致較低，回覆可以簡短")
    else:
        lines.append("互動意願：正常回應，不特別延伸也不刻意收尾")
    return lines
