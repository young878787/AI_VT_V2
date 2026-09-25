"""JEV 長期記憶分類的確定性後端政策。"""

import math
import re
from dataclasses import dataclass


POLICY_VERSION = "memory_route_v1"
MEMORY_TYPES = frozenset({"profile", "preference", "project", "event", "special", "correction", "none"})
ROUTES = frozenset({"none", "buffer", "process"})
_EXPLICIT = re.compile(
    r"(?:請|幫我|麻煩)?(?:記住|記得|忘記|刪除記憶|更新記憶|修改記憶)"
    r"|\b(?:remember|forget|update my memory|delete my memory)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryRouting:
    route: str
    memory_type: str = "none"
    importance: float = 0.0
    explicit_memory: float = 0.0
    confidence: float = 0.0
    explicit_request: bool = False


def _number(value: object, maximum: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("JEV 數值型別錯誤")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= maximum:
        raise ValueError("JEV 數值超出範圍")
    return number


def _choice(answers: dict, key: str, allowed: frozenset[str]) -> tuple[str, float]:
    value = answers[key]
    if not isinstance(value, dict) or value.get("choice") not in allowed:
        raise ValueError(f"JEV {key} 無效")
    return value["choice"], _number(value["confidence"])


def route_memory(user_input: str, answers: object) -> MemoryRouting:
    """欄位必須完整有效；失敗時只有明確 user 指令可進 PROCESS。"""
    explicit_request = bool(_EXPLICIT.search(user_input))
    fallback = MemoryRouting("process" if explicit_request else "none", explicit_request=explicit_request)
    if not isinstance(answers, dict):
        return fallback
    try:
        route, confidence = _choice(answers, "memory_route", ROUTES)
        memory_type, _ = _choice(answers, "memory_type", MEMORY_TYPES)
        explicit = answers["explicit_memory"]
        importance = answers["importance"]
        if not isinstance(explicit, dict) or not isinstance(importance, dict):
            raise ValueError("JEV 欄位無效")
        explicit_score = _number(explicit["noul"])
        importance_score = _number(importance["score"], 4.0)
        _number(importance["confidence"])
    except (KeyError, TypeError, ValueError):
        return fallback
    if explicit_request or explicit_score >= 0.80 or (route == "process" and confidence >= 0.65):
        resolved = "process"
    elif route == "buffer" or importance_score >= 2.5:
        resolved = "buffer"
    else:
        resolved = "none"
    return MemoryRouting(resolved, memory_type, importance_score, explicit_score, confidence, explicit_request)
