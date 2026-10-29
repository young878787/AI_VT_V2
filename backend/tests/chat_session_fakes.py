"""In-memory Chat session repository double for WebSocket unit tests."""

from copy import deepcopy

from infrastructure.in_memory_chat_session_repository import InMemoryChatSessionRepository
from services.chat_session_service import ChatSessionService


class FakeChatSessionRepository(InMemoryChatSessionRepository):
    """固定 session id 與時間戳，並允許以既有訊息起始。"""

    def __init__(self, messages=None, summary: str = "", session_id: str = "server_session") -> None:
        super().__init__(session_id)
        self.messages = deepcopy(messages or [])
        self.summary = summary
        self.next_sequence = len(self.messages)
        for index, message in enumerate(self.messages):
            message.setdefault("sequence", index)
            if message.get("role") == "user":
                message.setdefault("turn_state", "completed")

    def _now(self) -> str:
        return "2026-10-05T00:00:00+00:00"

    def _new_session_id(self) -> str:
        return "replacement_session"


def make_chat_session_service(messages=None, summary: str = ""):
    return ChatSessionService(FakeChatSessionRepository(messages, summary))
