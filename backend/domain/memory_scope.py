"""長期記憶的 owner 與 WebSocket ID 映射契約。"""

import re
from dataclasses import dataclass
from uuid import UUID, uuid5


MEMORY_NAMESPACE = UUID("d42b1c0f-e611-5bfa-8ef6-024a72029d36")
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")


@dataclass(frozen=True)
class MemoryScope:
    user_id: UUID
    character_id: UUID
    schema_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, UUID) or not isinstance(self.character_id, UUID):
            raise ValueError("MemoryScope owner 必須是 UUID")
        if not isinstance(self.schema_name, str) or not _SCHEMA_NAME.fullmatch(self.schema_name):
            raise ValueError("MemoryScope schema_name 必須是小寫 SQL identifier")


def conversation_id(session_id: str) -> UUID:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不得為空")
    return uuid5(MEMORY_NAMESPACE, session_id)


def message_id(session_id: str, turn_id: str) -> UUID:
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("turn_id 不得為空")
    return uuid5(conversation_id(session_id), turn_id)
