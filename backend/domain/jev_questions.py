"""JEV 情緒／動作／記憶粗篩三角色的 context、questions 與 Action answers → expression intent。"""
import hashlib
import json
import math

from domain.emotion_state import (
    CHARACTER_EXPRESSION_PROFILE,
    EMOTION_FIELDS,
    MOOD_HISTORY_LIMIT,
    PERSONALITY,
)
from domain.expression_intent_schema import (
    ALLOWED_ARCS,
    ALLOWED_EMOTIONS,
    ALLOWED_PERFORMANCE_MODES,
)

CONFIDENCE_THRESHOLD = 0.5
NOUL_GOOFY_THRESHOLD = 0.7
NOUL_BLINK_THRESHOLD = 0.6
JEV_DECISION_CRITERIA_VERSION = "split_roles_v1"
# 三角色共用的近期對話：最近 3 輪（user／assistant 各一則）。
JEV_RECENT_MESSAGES = 6
JEV_ROLES = frozenset({"emotion", "action", "memory"})

_EMOTION_DESC = {
    "neutral": "日常平靜、友善或只有輕微正向感受；當輪沒有足夠明確的其他主表情線索",
    "happy": "當輪有明確的開心、喜悅或被取悅線索；單純友善、想繼續聊天或輕微滿足不足以選此項",
    "playful": "調皮、玩鬧、想逗對方",
    "teasing": "挑釁、壞笑、得理不饒人",
    "angry": "生氣、憤怒、不滿",
    "sad": "悲傷、難過、受傷",
    "gloomy": "陰沉、低落、提不起勁",
    "shy": "害羞、內斂、不好意思",
    "surprised": "驚訝、震驚、措手不及",
    "conflicted": "矛盾、拉扯、左右為難",
}

_BASE_EMOTION_DESC = {
    key: value for key, value in _EMOTION_DESC.items()
    if key not in {"playful", "teasing"}
}

_INTERACTION_ATTITUDE_DESC = {
    "smile": "放鬆、友善地微笑互動；當輪沒有需要收起笑意的線索時使用",
    "bright_talk": "開朗熱情、主動帶動氣氛",
    "goofy_face": "刻意裝傻、搞怪或做鬼臉",
    "cheeky_wink": "調皮玩鬧，以眨眼逗對方",
    "smug": "得意、壞笑或帶自信地逗弄對方",
    "deadpan": "冷淡、平靜或面無表情地回應",
    "gloomy": "消沉壓低、帶沉重氣氛互動",
    "volatile": "態度搖擺、情緒表現不穩定",
    "meltdown": "失控爆發；只在當輪有強烈爆發證據時使用",
    "awkward": "彆扭、害羞、不知道如何回應；或察覺自己沒理解對方時收起笑意、帶歉意地傾聽",
    "tense_hold": "壓著不滿或強烈情緒、克制反應",
    "shock_recoil": "受到突然衝擊而明顯退縮或震驚",
}

_ARC_DESC = {
    "steady": "整段維持同一個表情",
    "pop_then_settle": "先爆開（驚/彈）再收斂回穩",
    "pause_then_smirk": "先停頓一下再露出壞笑",
    "widen_then_tease": "先瞪大再轉成調皮",
    "shrink_then_recover": "先退縮變小再慢慢恢復",
    "glare_then_flatten": "先瞪著對方再放平",
}

_INTENSITY_LEVELS = [
    "0 幾乎沒有表情，語氣平淡",
    "1 輕微，臉上有一點但不明顯",
    "2 中等，清楚的臉部變化",
    "3 強烈，很明顯的表情",
    "4 爆發，表情全開、誇張",
]

_ENERGY_LEVELS = [
    "0 低沉無力，快睡著",
    "1 偏低，安靜慢節奏",
    "2 中等，正常對話節奏",
    "3 偏高，活潑有精神",
    "4 高漲，興奮蹦跳",
]


def _assert_whitelist_sync() -> None:
    """確保描述表 keys 與 schema 白名單一致（import-time 檢查，避免漂移）。"""
    if set(_EMOTION_DESC) != ALLOWED_EMOTIONS:
        raise RuntimeError(
            f"EMOTION_CRITERIA 與 ALLOWED_EMOTIONS 不同步: "
            f"{set(_EMOTION_DESC) ^ ALLOWED_EMOTIONS}"
        )
    if not set(_BASE_EMOTION_DESC) < ALLOWED_EMOTIONS:
        raise RuntimeError("BASE_EMOTION_CRITERIA 必須是 ALLOWED_EMOTIONS 的真子集")
    if set(_INTERACTION_ATTITUDE_DESC) != ALLOWED_PERFORMANCE_MODES:
        raise RuntimeError("INTERACTION_ATTITUDE_CRITERIA 與 performance modes 不同步")
    if set(_ARC_DESC) != ALLOWED_ARCS:
        raise RuntimeError(
            f"ARC_CRITERIA 與 ALLOWED_ARCS 不同步: {set(_ARC_DESC) ^ ALLOWED_ARCS}"
        )


_assert_whitelist_sync()

EMOTION_CRITERIA = dict(_EMOTION_DESC)
BASE_EMOTION_CRITERIA = dict(_BASE_EMOTION_DESC)
INTERACTION_ATTITUDE_CRITERIA = dict(_INTERACTION_ATTITUDE_DESC)
ARC_CRITERIA = dict(_ARC_DESC)
INTENSITY_LEVELS = list(_INTENSITY_LEVELS)
ENERGY_LEVELS = list(_ENERGY_LEVELS)


# 2 為不變，對應後端 (score - 2) / 2；表達軸（masking_positive_feeling）為撒嬌 0 ↔ 嘴硬 4。
_EMOTION_QUESTIONS = {
    "shy": ("這句話讓你害羞嗎？", (
        "0 原本的害羞被打消", "1 害羞少一點", "2 沒影響", "3 有點害羞", "4 很害羞、很不好意思",
    )),
    "pleased": ("這句話讓你開心嗎？", (
        "0 開心被打消（被潑冷水、被否定）", "1 開心少一點", "2 沒影響", "3 有點開心",
        "4 很開心（被稱讚、被重視）",
    )),
    "genuinely_angry": ("這句話讓你真的生氣嗎？你自己嘴硬、吐槽不算生氣。", (
        "0 氣消了（對方道歉、解釋清楚）", "1 氣消一點", "2 沒影響", "3 有點不爽",
        "4 很生氣（被罵、被看不起、被冒犯）",
    )),
    "sad_or_hurt": ("這句話讓你難過嗎？", (
        "0 難過被撫平（被安慰、被在乎）", "1 難過少一點", "2 沒影響", "3 有點受傷、失落",
        "4 很受傷（被嫌棄、被冷落）",
    )),
    "masking_positive_feeling": ("聽到這句話，你會撒嬌還是嘴硬？", (
        "0 很想撒嬌：開心、被寵，或對方敷衍你（只回「喔」「嗯」）時黏過去討回應", "1 有點想撒嬌",
        "2 跟平常一樣", "3 有點嘴硬", "4 很嘴硬：生氣或受傷時逞強、假裝不在意",
    )),
    "wants_continue_interaction": ("聽完這句話，你想把話題聊下去嗎？", (
        "0 想敷衍帶過、結束話題：你在生氣、受傷，或對方一直講你沒興趣的事", "1 回短一點就好",
        "2 照常回應", "3 想多聊一點", "4 很想延伸話題、主動多講：開心、害羞時",
    )),
}

_EMOTION_ROLE = (
    "你是露西亞，死靈法師青少女。對方剛對你說了 current_user_input，前面的對話在 recent_dialogue。\n"
    "current_mood 是你現在的情緒（0～1）；mood_history 是前幾輪各讓情緒變了多少，turns_ago=1 是上一輪。\n"
    "只看對方這句話給 0～4 分：2 是沒影響，3～4 往選項 4 的方向，0～1 往選項 0 的方向，兩邊都會用到。"
    "跟這題無關就給 2。前幾輪已經反應過的事再講一次，不再加分。"
)


def build_emotion_questions() -> dict:
    assert set(_EMOTION_QUESTIONS) == set(EMOTION_FIELDS)
    return {
        field: {
            "type": "score",
            "instructions": f"{_EMOTION_ROLE}\n\n{question}",
            "criteria": list(levels),
        }
        for field, (question, levels) in _EMOTION_QUESTIONS.items()
    }


def build_recent_dialogue(chat_history: list[dict]) -> list[dict]:
    """三角色共用：最近 3 輪已完成的真實對話，不含本輪 user 訊息。"""
    return [
        {"role": msg["role"], "text": msg["content"][:500]}
        for msg in chat_history
        if isinstance(msg, dict)
        and msg.get("role") in ("user", "assistant")
        and isinstance(msg.get("content"), str)
    ][-JEV_RECENT_MESSAGES:]


def build_emotion_context(
    user_message: str,
    chat_history: list[dict],
    current_mood: dict[str, float],
    mood_history: list[dict] | None = None,
) -> dict:
    """情緒角色：證據中立 profile、近期對話、閒置回落後的情緒值與最近 ≤3 輪變化。

    turns_ago 依 turn_id 對應 chat_history 的實際 user 輪次；找不到對應輪次的紀錄不送出。
    """
    user_turn_ids = [msg.get("turn_id") for msg in chat_history
                     if isinstance(msg, dict) and msg.get("role") == "user"]
    entries = []
    for entry in list(mood_history or [])[-MOOD_HISTORY_LIMIT:]:
        if entry["turn_id"] in user_turn_ids:
            turns_ago = len(user_turn_ids) - user_turn_ids.index(entry["turn_id"])
            entries.append({"turns_ago": turns_ago, "changes": dict(entry["changes"])})
    return {
        "character_expression_profile": CHARACTER_EXPRESSION_PROFILE,
        "recent_dialogue": build_recent_dialogue(chat_history),
        "current_user_input": user_message[:4000],
        "current_mood": {field: round(current_mood[field], 2) for field in EMOTION_FIELDS},
        "mood_history": entries,
    }


def build_action_questions() -> dict:
    """動作角色的兩軸表演問題；不引用情緒角色的輸出。"""
    return {
        "base_emotion": {
            "type": "choice",
            "instructions": (
                "選擇這一輪實際呈現給使用者看的基礎情緒底色。"
                "這一題與 `interaction_attitude` 必須聯合判斷：先決定基礎情緒，"
                "再選能一致呈現它的互動態度。`interaction_personality` 不能獨立構成情緒證據。"
                "使用者描述自己的情緒不代表露西亞必然具有相同情緒；應判斷露西亞當輪要呈現的反應。"
                "使用者明確難過且露西亞正在同理安慰時可選 sad；使用者對第三方生氣時，"
                "除非露西亞也有明確憤怒反應，否則不要直接選 angry。"
                "使用者明確表示這段共同互動很開心時可選 happy。"
                "一般友善、輕微正向或想繼續互動仍選 neutral；"
                "只有當輪明確喜悅才選 happy。"
                "playful 與 teasing 是互動態度，不是基礎情緒。"
            ),
            "criteria": dict(BASE_EMOTION_CRITERIA),
        },
        "interaction_attitude": {
            "type": "choice",
            "instructions": (
                "選擇角色當輪面對使用者的可見互動態度，並與 `base_emotion` 聯合判斷。"
                "態度不得取代或反轉基礎情緒。`current_user_input` 的明確表演請求是態度證據："
                "要求開玩笑或調皮回應時優先考慮 cheeky_wink，要求搞怪或鬼臉時選 goofy_face，"
                "要求有趣、活潑地說明時考慮 bright_talk，明確逗弄或得意時考慮 smug。"
                "若使用者否定角色剛才的理解或安慰，且當輪尚未說明是玩笑，應先認真傾聽、"
                "收起笑意；角色需要承認可能沒理解對方時優先考慮 awkward。"
                "單獨的『哼』、嘴硬語氣或固定人格不足以把指責判成玩笑。"
                "使用者當輪明確澄清是在開玩笑時，再依玩鬧線索考慮 cheeky_wink。"
                "只有沒有這些差異化線索時才選 smile。"
                "強烈模式必須有當輪明確證據，不可只因固定人格而選擇。"
                "一致例：happy+bright_talk、shy+awkward、angry+tense_hold。"
                "若沒有刻意反差的明確線索，避免 happy+meltdown、sad+bright_talk 等衝突組合。"
            ),
            "criteria": dict(INTERACTION_ATTITUDE_CRITERIA),
        },
        "arc": {
            "type": "choice",
            "instructions": (
                "根據 `current_user_input` 語氣的轉折程度：這個表情在這一輪應該怎麼變化？"
                "語氣單純就 steady，有戲劇轉折就選對應弧線。"
            ),
            "criteria": dict(ARC_CRITERIA),
        },
        "intensity": {
            "type": "score",
            "instructions": (
                "角色此刻情緒表達的強度應該多強？"
                "參考 `current_user_input` 的語氣力度與 `recent_dialogue` 的脈絡。"
            ),
            "criteria": list(INTENSITY_LEVELS),
        },
        "energy": {
            "type": "score",
            "instructions": (
                "角色此刻的整體精神能量應該多高？"
                "參考 `current_user_input` 的節奏與 `interaction_personality` 的人格傾向。"
            ),
            "criteria": list(ENERGY_LEVELS),
        },
        "wants_goofy": {
            "type": "noul",
            "instructions": (
                "使用者是否在明確要求角色做鬼臉、裝傻、搞笑反應？"
                "（如訊息中出現搞笑指令或明顯玩鬧邀請）"
            ),
            "criteria": {
                "true": "訊息中有明確的搞笑/鬼臉要求或明顯玩鬧邀請",
                "false": "一般對話或非搞笑訴求",
            },
        },
        "needs_special_blink": {
            "type": "noul",
            "instructions": (
                "這一輪是否需要特殊的眨眼表演（凝視不眨眼、害羞狂眨、驚訝後瞬眼等）？"
                "一般聊天回答不需要。"
            ),
            "criteria": {
                "true": "情境明確需要特殊眨眼表演",
                "false": "一般眨眼即可",
            },
        },
    }


def build_memory_questions() -> dict:
    """記憶只做確定雜訊粗篩；不分類、不決定保存價值。"""
    return {"memory_noise": {
        "type": "choice",
        "instructions": (
            "只能使用 current_user_input 與 recent_dialogue 作為證據。"
            "persona、情緒與 relevant_memory 不得單獨構成記憶證據。"
            "只有確定整輪都是無持久資訊的即時反應或已知遊戲報位才選 noise。"
            "遊戲詞彙、粗話、要死了不是黑名單；混有偏好、生活事實、事件、"
            "指代、更正、記住、遺忘或禁止保存請求，或不確定時，一律選 review。"
        ),
        "criteria": {"noise": "確定只有即時雜訊", "review": "其餘或不確定，交由單一 Memory Agent"},
    }}


def build_role_questions() -> dict[str, dict]:
    """三角色各自獨立呼叫 JEV 的題目。"""
    return {
        "emotion": build_emotion_questions(),
        "action": build_action_questions(),
        "memory": build_memory_questions(),
    }


def question_hash(questions: dict) -> str:
    return hashlib.sha256(
        json.dumps(questions, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def build_action_context(
    user_message: str,
    chat_history: list[dict],
    previous_expression_carry_state: dict | None,
    current_action: dict | None = None,
) -> dict:
    """動作角色：互動人格、上一輪表情延續狀態與播放中動作。"""
    context = {
        "current_user_input": user_message[:4000],
        "recent_dialogue": build_recent_dialogue(chat_history),
        "interaction_personality": PERSONALITY,
    }
    if previous_expression_carry_state is not None:
        context["previous_expression_carry_state"] = previous_expression_carry_state
    if current_action is not None:
        context["current_action"] = current_action
    return context


def build_memory_context(user_message: str, chat_history: list[dict]) -> dict:
    """記憶粗篩角色：只含當輪輸入與近期對話。"""
    return {
        "current_user_input": user_message[:4000],
        "recent_dialogue": build_recent_dialogue(chat_history),
    }


def map_answers_to_intent(answers: dict) -> dict:
    """Jev answers → raw intent dict。

    低 confidence 欄位直接不填，交給 normalize_expression_intent()
    用 DEFAULT_INTENT 兜底；主表情無效時由呼叫端回退上一輪表情。
    """
    intent: dict = {}

    base_emotion = answers.get("base_emotion") or {}
    if base_emotion.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        choice = base_emotion.get("choice")
        if choice in BASE_EMOTION_CRITERIA:
            intent["emotion"] = choice

    attitude = answers.get("interaction_attitude") or {}
    if attitude.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        choice = attitude.get("choice")
        if choice in INTERACTION_ATTITUDE_CRITERIA:
            intent["performance_mode"] = choice

    arc = answers.get("arc") or {}
    if arc.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        intent["arc"] = arc.get("choice")

    # Score 0–4 → 0.0–1.0（score 可落在兩級之間，如 2.4 → 0.6）
    intensity = answers.get("intensity") or {}
    if intensity.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        score = intensity.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            intent["intensity"] = round(max(0.0, min(1.0, float(score) / 4.0)), 3)

    energy = answers.get("energy") or {}
    if energy.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        score = energy.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            intent["energy"] = round(max(0.0, min(1.0, float(score) / 4.0)), 3)

    # Noul 觸發器：超過閾值才影響 intent（Noul 無 confidence 欄位）
    goofy = answers.get("wants_goofy") or {}
    if float(goofy.get("noul", 0.0)) > NOUL_GOOFY_THRESHOLD:
        intent["must_include"] = ["goofy_eye_cross_bias"]
        # performance_mode 強制候選交給規則層決定，避免覆蓋高信心 Choice

    blink = answers.get("needs_special_blink") or {}
    if float(blink.get("noul", 0.0)) > NOUL_BLINK_THRESHOLD:
        intent["blink_style"] = _map_special_blink(intent.get("emotion"))

    return intent


def _map_special_blink(emotion: str | None) -> str:
    """特殊眨眼需求 → blink_style 情緒映射（blink_control_hints 縮編版）。"""
    mapping = {
        "shy": "shy_fast",
        "teasing": "teasing_pause",
        "surprised": "surprised_hold",
        "gloomy": "sleepy_slow",
        "sad": "sleepy_slow",
        "conflicted": "focused_pause",
    }
    if emotion in mapping:
        return mapping[emotion]
    return "focused_pause"
