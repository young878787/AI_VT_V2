"""JEV Emotion / Action questions 與 Action answers → expression intent。"""
import math

from domain.emotion_state import CHARACTER_EXPRESSION_PROFILE, EMOTION_FIELDS, PERSONALITY
from domain.expression_intent_schema import (
    ALLOWED_ARCS,
    ALLOWED_EMOTIONS,
    ALLOWED_PERFORMANCE_MODES,
)

CONFIDENCE_THRESHOLD = 0.5
NOUL_GOOFY_THRESHOLD = 0.7
NOUL_BLINK_THRESHOLD = 0.6
JEV_DECISION_CRITERIA_VERSION = "joint_two_axis_v3"

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
    "smile": "自然、友善地互動；沒有更明顯態度時使用",
    "bright_talk": "開朗熱情、主動帶動氣氛",
    "goofy_face": "刻意裝傻、搞怪或做鬼臉",
    "cheeky_wink": "調皮玩鬧，以眨眼逗對方",
    "smug": "得意、壞笑或帶自信地逗弄對方",
    "deadpan": "冷淡、平靜或面無表情地回應",
    "gloomy": "消沉壓低、帶沉重氣氛互動",
    "volatile": "態度搖擺、情緒表現不穩定",
    "meltdown": "失控爆發；只在當輪有強烈爆發證據時使用",
    "awkward": "彆扭、害羞或不知道如何回應",
    "tense_hold": "壓著情緒、克制反應",
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


_EMOTION_QUESTIONS = {
    "shy": "露西亞此刻是否明顯害羞或不好意思？",
    "pleased": "露西亞是否因目前互動感到開心、滿足或被取悅？",
    "genuinely_angry": "露西亞是否真的生氣，而非嘴硬、吐槽或假裝不耐煩？",
    "sad_or_hurt": "露西亞是否難過、失落、受傷或感到被冷落？",
    "masking_positive_feeling": "露西亞是否正在掩飾喜歡、開心或親近等正面感受？",
    "wants_continue_interaction": "露西亞是否希望目前話題或親密互動繼續？",
}

_EMOTION_EVALUATION_RULES = (
    "你是情緒狀態估計器，只估計露西亞此刻的內在情緒。",
    "以 current_user_input 的當輪可觀察線索為最高優先；recent_dialogue 只用來解讀當輪線索。",
    "previous_emotion_state 只供連續性參考，不代表本輪仍有相同情緒；沒有當輪支持證據時，降低對應分數，不沿用舊分數。",
    "character_expression_profile 只可協助解讀模糊線索；不能獨立構成情緒證據，也不能直接提高任何情緒分數。",
    "明確否認某種情緒通常是該情緒的反向證據；只有強烈、可觀察的相反線索才可推翻。",
    "判斷 masking_positive_feeling 時，否認本身不足以提高分數；必須同時有可觀察的正面感受及掩飾行為。",
    "relevant_memory 只能協助理解當輪提及的人事物，不能單獨作為情緒證據。",
)


def build_emotion_questions() -> dict:
    assert set(_EMOTION_QUESTIONS) == set(EMOTION_FIELDS)
    rules = "\n".join(f"- {rule}" for rule in _EMOTION_EVALUATION_RULES)
    return {
        field: {
            "type": "noul",
            "instructions": f"{question}\n\n判斷規則：\n{rules}\n不輸出理由。",
            "criteria": {"true": "符合", "false": "不符合"},
        }
        for field, question in _EMOTION_QUESTIONS.items()
    }


def build_emotion_context(
    user_message: str,
    chat_history: list[dict],
    previous_emotion_state: dict | None,
    relevant_memory: str = "",
) -> dict:
    """只傳最近 8 輪已完成的真實對話，不含本輪 user 訊息。"""
    dialogue = [
        {"role": msg["role"], "text": msg["content"][:500]}
        for msg in chat_history
        if isinstance(msg, dict)
        and msg.get("role") in ("user", "assistant")
        and isinstance(msg.get("content"), str)
    ][-16:]
    context = {
        "character_expression_profile": CHARACTER_EXPRESSION_PROFILE,
        "recent_dialogue": dialogue,
        "current_user_input": user_message[:4000],
    }
    if previous_emotion_state is not None:
        context["previous_emotion_state"] = previous_emotion_state
    if relevant_memory:
        context["relevant_memory"] = relevant_memory[:800]
    return context


def build_action_questions() -> dict:
    """與 Emotion State 同次送出的兩軸表演問題。"""
    return {
        "base_emotion": {
            "type": "choice",
            "instructions": (
                "選擇這一輪實際呈現給使用者看的基礎情緒底色，並與本次六欄情緒評分保持一致。"
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
                "參考本次六欄情緒評分與 `current_user_input` 的語氣力度。"
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
    """分類只以當輪輸入與有界近期對話為證據。"""
    evidence = (
        "只能使用 current_user_input 與 recent_dialogue 作為記憶分類證據。"
        "persona、即時情緒、expression state 與 relevant_memory 不得單獨構成證據。"
        "只判斷是否交給長期記憶流程，不決定資料庫動作。"
    )
    return {
        "memory_route": {
            "type": "choice", "instructions": evidence + "選擇 none、buffer 或 process。",
            "criteria": {
                "none": "寒暄、一般問答或沒有長期價值",
                "buffer": "可能有價值但片面、未確認或缺少上下文",
                "process": "足以形成候選，或使用者明確要求記住、修改、忘記",
            },
        },
        "memory_type": {
            "type": "choice", "instructions": evidence + "選擇最合適的候選類型。",
            "criteria": {
                "profile": "使用者的穩定資料", "preference": "偏好", "project": "進行中的專案",
                "event": "事件", "special": "特殊且重要的資訊", "correction": "對先前資訊的更正",
                "none": "沒有可分類的資訊",
            },
        },
        "explicit_memory": {
            "type": "noul", "instructions": evidence + "使用者是否明確要求記住、修改或忘記記憶？",
            "criteria": {"true": "明確要求", "false": "沒有明確要求"},
        },
        "importance": {
            "type": "score", "instructions": evidence + "評估長期保存價值，0 無、1 短暫、2 普通、3 未來有用、4 重要。",
            "criteria": ["無長期價值", "短暫資訊", "普通資訊", "未來很可能有用", "重要且應長期保存"],
        },
    }


def build_jev_questions() -> dict:
    """同一次 JEV 呼叫產生情緒、表演及記憶分類。"""
    return {**build_emotion_questions(), **build_action_questions(), **build_memory_questions()}


def build_jev_context(
    user_message: str,
    chat_history: list[dict],
    previous_emotion_state: dict | None,
    previous_expression_carry_state: dict | None,
    relevant_memory: str = "",
    current_action: dict | None = None,
) -> dict:
    """建立單次 JEV 共用 context，將情緒證據與互動人格分欄。"""
    state = build_emotion_context(
        user_message, chat_history, previous_emotion_state, relevant_memory,
    )
    state["interaction_personality"] = PERSONALITY
    if previous_expression_carry_state is not None:
        state["previous_expression_carry_state"] = previous_expression_carry_state
    if current_action is not None:
        state["current_action"] = current_action
    return state


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
