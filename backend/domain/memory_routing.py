"""JEV 只排除確定雜訊；單一記憶 agent 決定保存與操作。"""

import math
import re
from dataclasses import dataclass

POLICY_VERSION = "memory_agent_v3"
MEMORY_TYPES = frozenset({"profile", "preference", "project", "event", "none"})
ROUTES = frozenset({"none", "needs_context", "process"})
_NO_STORE = re.compile(r"不要(?:保存|記住|記錄)|別(?:保存|記住|記錄)|do not (?:save|remember)|don't (?:save|remember)", re.I)
_NEGATED_FORGET = re.compile(r"不要忘|別忘|不用刪|不要刪|don't forget|do not forget", re.I)
_FORGET = re.compile(
    r"^(?:(?:請|幫我|麻煩)(?:把|將)?[^。！？]{0,40}?(?:忘記|忘掉|刪除)|"
    r"(?:忘記|忘掉)(?:我|關於)|刪除(?:我|關於|記憶)|(?:把|將)[^。！？]{1,40}(?:忘記|忘掉|刪除))"
    r"|^(?:please )?(?:forget (?:my|about)|delete my memory)", re.I,
)
_REQUEST = re.compile(r"(?:幫我|請|麻煩).{0,20}(?:記住|記得|更正|更新|修改)|^(?:記住|更正|更新記憶|修改記憶)|不要忘|別忘|\bremember\b", re.I)


def instruction_policy(text: str) -> str:
    if _NO_STORE.search(text):
        return "no_store"
    if not _NEGATED_FORGET.search(text) and _FORGET.search(text):
        return "forget"
    if _REQUEST.search(text):
        return "remember"
    return "observe"


def forget_scope(text: str) -> str:
    """明確限定單一版本時，後端不得擴大為整件事。"""
    return "version" if re.search(r"(?:只|僅).{0,12}版本|(?:這個|最新|單一)版本|only.{0,20}version", text, re.I) else "fact"


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
