"""短期對話中的長期記憶來源 provenance 契約。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from uuid import UUID

from domain.memory_routing import POLICY_VERSION, instruction_policy
from domain.memory_scope import message_id


MEMORY_SOURCE_FIELD = "memory_source"
MEMORY_SOURCE_VERSION = 1
_POLICIES = frozenset({"observe", "remember", "forget", "no_store"})


class MemoryEventReplay(ValueError):
    """相同 source identity 與內容已由 DB 接收，不重複執行副作用。"""


class MemoryEventConflict(ValueError):
    """相同 source identity 已綁定其他或無法驗證的內容。"""


@dataclass(frozen=True)
class MemorySourceMetadata:
    source_id: UUID
    occurred_at: datetime
    policy: str
    policy_version: str
    generation: int


def build_user_message(
    session_id: str,
    turn_id: str,
    content: str,
    *,
    timestamp: float | None = None,
    generation: int | None = None,
) -> dict:
    """由伺服器輸入邊界建立不可由 client 指定的來源 metadata。"""
    occurred_at = (
        datetime.now(timezone.utc)
        if timestamp is None
        else datetime.fromtimestamp(_valid_timestamp(timestamp), timezone.utc)
    )
    return {
        "role": "user",
        "content": content,
        MEMORY_SOURCE_FIELD: {
            "version": MEMORY_SOURCE_VERSION,
            "source_id": str(message_id(session_id, turn_id)),
            "occurred_at": occurred_at.isoformat(),
            "policy": instruction_policy(content),
            "policy_version": POLICY_VERSION,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "generation": generation,
        },
    }


def bind_source_generation(message: dict, generation: int) -> None:
    """記憶 job 建立後，將 DB generation 綁回同一份 server-side message。"""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("來源 generation 必須是非負整數")
    value = message.get(MEMORY_SOURCE_FIELD) if isinstance(message, dict) else None
    if not isinstance(value, dict):
        raise ValueError("缺少來源 metadata")
    value["generation"] = generation


def read_memory_source(message: object) -> MemorySourceMetadata | None:
    """驗證來源完整性；舊格式或內容不符時 fail closed。"""
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    value = message.get(MEMORY_SOURCE_FIELD)
    if not isinstance(content, str) or not isinstance(value, dict):
        return None
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != MEMORY_SOURCE_VERSION:
        return None
    policy = value.get("policy")
    policy_version = value.get("policy_version")
    generation = value.get("generation")
    if not isinstance(policy, str) or policy not in _POLICIES or policy_version != POLICY_VERSION:
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return None
    if value.get("content_sha256") != hashlib.sha256(content.encode("utf-8")).hexdigest():
        return None
    # 核對完整原文與接收時的政策；裁切 view 不得重新取得保存權限。
    if policy != instruction_policy(content):
        return None
    if not isinstance(value.get("source_id"), str) or not isinstance(value.get("occurred_at"), str):
        return None
    try:
        source_id = UUID(value["source_id"])
        occurred_at = datetime.fromisoformat(value["occurred_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        return None
    return MemorySourceMetadata(
        source_id=source_id,
        occurred_at=occurred_at.astimezone(timezone.utc),
        policy=policy,
        policy_version=policy_version,
        generation=generation,
    )


def _valid_timestamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("來源 timestamp 必須是有限數字")
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ValueError("來源 timestamp 必須是有限數字")
    return timestamp
