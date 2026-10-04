"""JEV 只排除確定雜訊；單一記憶 agent 決定保存與操作。"""

import math
import re
from dataclasses import dataclass

POLICY_VERSION = "memory_agent_v3"
MEMORY_TYPES = frozenset({"profile", "preference", "project", "event", "none"})
ROUTES = frozenset({"none", "needs_context", "process"})
_NO_STORE = re.compile(r"不要(?:保存|記住|記錄)|別(?:保存|記住|記錄)|do not (?:save|remember)|don't (?:save|remember)", re.I)
_NEGATED_FORGET = re.compile(r"不要忘|別忘|不用刪|不要刪|don't forget|do not forget", re.I)
_ZH_FORGET = re.compile(
    r"^(?:(?:(?:請|麻煩|拜託)(?:你)?(?:幫我)?|幫我)\s*(?:只|僅)?\s*"
    r"(?:(?:忘記|忘掉|刪除)\s*(?P<requested_target>.+)|"
    r"(?:把|將)\s*(?P<requested_object>.+?)\s*(?:忘記|忘掉|刪除)(?:掉)?)|"
    r"(?:只|僅)?\s*(?:(?:忘記|忘掉)\s*(?P<forget_target>(?:我|關於).+)|"
    r"刪除\s*(?P<delete_target>(?:我|關於|記憶).+)|"
    r"(?:把|將)\s*(?P<object_target>.+?)\s*(?:忘記|忘掉|刪除)(?:掉)?))"
    r"(?:吧|好嗎|謝謝)?[\s。！？]*$",
    re.I,
)
_EN_FORGET = re.compile(
    r"^(?:please\s+)?(?:forget\s+(?P<forget_target>my\s+.+|about\s+.+)"
    r"|delete\s+(?P<delete_target>my\s+memor(?:y|ies)(?:\s+about\s+.+)?))\s*[.!?]*$",
    re.I,
)
_NARRATED_FORGET = re.compile(
    r"(?:的(?:人|那個人|家夥)?是|的是|這件事是|(?:\bis|\bwas)\s+(?:what|something)\b)",
    re.I,
)
_TEMPORARY_FORGET = re.compile(r"(?:暫時|先).{0,8}(?:忘記|忘掉|刪除)|\b(?:temporarily|for now)\b", re.I)
_AMBIGUOUS_FORGET = re.compile(
    r"(?:[？?]|怎麼辦|該怎麼|為什麼|會(?:造成|發生|怎樣|如何)|是否|是不是|(?<!好)嗎(?:[。！？\s]*$)|"
    r"\b(?:was|is|are|were|would|could|should|might|may|if|because|when|why|how)\b)",
    re.I,
)
_REQUEST = re.compile(r"(?:幫我|請|麻煩).{0,20}(?:記住|記得|更正|更新|修改)|^(?:記住|更正|更新記憶|修改記憶)|不要忘|別忘|\bremember\b", re.I)


def _explicit_forget_request(text: str) -> bool:
    """只授權結構明確的當輪直接命令；引述、敘述或歧義句不開放破壞性操作。"""
    candidate = text.strip()
    ambiguity_input = re.sub(r"好嗎[？?]?$", "", candidate)
    if _TEMPORARY_FORGET.search(candidate) or _AMBIGUOUS_FORGET.search(ambiguity_input):
        return False
    match = _ZH_FORGET.match(candidate) or _EN_FORGET.match(candidate)
    if not match:
        return False
    target = next((value for value in match.groupdict().values() if value), "")
    return not _NARRATED_FORGET.search(target)


def instruction_policy(text: str) -> str:
    if _NO_STORE.search(text):
        return "no_store"
    if not _NEGATED_FORGET.search(text) and _explicit_forget_request(text):
        return "forget"
    if _REQUEST.search(text):
        return "remember"
    return "observe"


def forget_scope(text: str) -> str:
    """明確限定單一版本時，後端不得擴大為整件事。"""
    whole_fact = re.search(
        r"(?:全部|所有|整件事|整個事實|包含|連同).{0,12}(?:版本|紀錄|記憶|內容|資料|偏好)?|"
        r"\b(?:all|every|entire|including)\b",
        text,
        re.I,
    )
    version = re.search(
        r"(?:最新|最舊|目前|當前|現在|剛剛|上一(?:筆|個|次)|前一(?:筆|個|次))(?:的)?"
        r"(?:版本|這筆|這份|這次|那筆|記憶|內容|資料|紀錄|事實|偏好)?|"
        r"(?:這|那)(?:個|筆|份|次)(?:的)?(?:版本|記憶|內容|資料|紀錄|事實|偏好)?|"
        r"(?:只|僅).{0,20}(?:忘記|忘掉|刪除)|(?:單一|一個)(?:版本|記憶|內容|資料|紀錄|事實)|"
        r"\b(?:latest|current|this|one|single)(?:\s+\w+){0,4}\b",
        text,
        re.I,
    )
    return "fact" if whole_fact else "version" if version else "fact"


@dataclass(frozen=True)
class MemoryRouting:
    route: str | None
    confidence: float = 0.0
    explicit_request: bool = False
    error: str | None = None


def _number(value: object, maximum: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("JEV 數值型別錯誤")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= maximum:
        raise ValueError("JEV 數值超出範圍")
    return number


def route_memory(user_input: str, answers: object) -> MemoryRouting:
    policy = instruction_policy(user_input)
    if policy == "no_store":
        return MemoryRouting("none")
    if policy != "observe":
        return MemoryRouting(None, explicit_request=True)
    try:
        answer = answers["memory_noise"]
        if answer["choice"] not in {"noise", "review"}:
            raise ValueError("invalid noise choice")
        confidence = _number(answer["confidence"])
        return MemoryRouting("none" if answer["choice"] == "noise" and confidence >= 0.85 else None,
                             confidence=confidence)
    except (KeyError, TypeError, ValueError):
        return MemoryRouting(None, error="jev_invalid")
