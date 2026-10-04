"""隔離聊天測試的固定路徑矩陣；正式 instance 忽略 client 的測試模式。"""

from enum import Enum
import os


class ChatTestMode(str, Enum):
    SHORT_ONLY = "short_only"
    MEMORY_SEED = "memory_seed"
    MEMORY_PROBE = "memory_probe"
    MIXED_READ = "mixed_read"
    MIXED_UPDATE = "mixed_update"

    @property
    def short_term(self) -> bool:
        return self != self.MEMORY_PROBE

    @property
    def memory_read(self) -> bool:
        return self in {self.MEMORY_PROBE, self.MIXED_READ, self.MIXED_UPDATE}

    @property
    def memory_write(self) -> bool:
        return self in {self.MEMORY_SEED, self.MIXED_UPDATE}


def resolve_test_mode(value: object) -> ChatTestMode | None:
    if os.getenv("AI_VT_TEST_MODE", "").lower() != "true" or value is None:
        return None
    return ChatTestMode(value)
