"""In-memory Chat session repository double for WebSocket unit tests."""

from copy import deepcopy

from domain.emotion_state import validate_emotion_state
from infrastructure.chat_session_repository import ChatSessionRepository, ChatSessionStaleError
from services.chat_session_service import ChatSessionService


class FakeChatSessionRepository:
    def __init__(self, messages=None, summary: str = "", session_id: str = "server_session") -> None:
        self.session_id = session_id
        self.generation = 0
        self.revision = 0
        self.messages = deepcopy(messages or [])
        self.summary = summary
        self.emotion_state = None
        self.deleted = False

    def snapshot(self):
        if self.deleted:
            return None
        return {
            "session_id": self.session_id, "generation": self.generation,
            "revision": self.revision, "messages": deepcopy(self.messages),
            "summary": self.summary, "emotion_state": deepcopy(self.emotion_state),
            "created_at": "2026-10-05T00:00:00+00:00",
            "updated_at": "2026-10-05T00:00:00+00:00",
        }

    async def get_or_create_active(self):
        if self.deleted:
            self.deleted = False
            self.session_id = "replacement_session"
        return self.snapshot()

    async def load(self, session_id):
        return self.snapshot() if session_id == self.session_id else None

    async def activate_for_test(self, session_id):
        self.session_id = session_id
        self.generation = 0
        self.revision = 0
        self.messages = []
        self.summary = ""
        self.emotion_state = None
        self.deleted = False
        return self.snapshot()

    async def replace_messages(self, session_id, generation, messages):
        self._check(session_id, generation)
        self.messages = deepcopy(ChatSessionRepository.persistable_messages(messages)[-20:])
        self.revision += 1
        return self.revision

    async def update_emotion(self, session_id, generation, state):
        self._check(session_id, generation)
        self.emotion_state = validate_emotion_state(state)
        self.revision += 1
        return self.revision

    async def commit_summary(self, session_id, generation, expected_revision, summary, messages):
        self._check(session_id, generation)
        if expected_revision != self.revision:
            raise ChatSessionStaleError("摘要基準已過期")
        self.summary = summary[:4000]
        self.messages = deepcopy(ChatSessionRepository.persistable_messages(messages)[-20:])
        self.revision += 1
        return self.revision

    async def reset(self, session_id, connection=None):
        self._check(session_id, self.generation)
        self.generation += 1
        self.revision += 1
        self.messages = []
        self.summary = ""
        self.emotion_state = None
        return {"generation": self.generation, "revision": self.revision}

    async def list_sessions(self):
        if self.deleted:
            return []
        return [{"session_id": self.session_id, "status": "active", "message_count": len(self.messages),
                 "preview": next((m["content"] for m in self.messages if m["role"] == "user"), ""),
                 "updated_at": "2026-10-05T00:00:00+00:00",
                 "has_summary": bool(self.summary), "has_emotion_state": self.emotion_state is not None}]

    async def delete(self, session_id):
        deleted = int(not self.deleted and session_id == self.session_id)
        self.deleted = bool(deleted) or self.deleted
        return {"session_id": session_id, "deleted_sessions": deleted}

    async def delete_all(self):
        count = int(not self.deleted)
        self.deleted = True
        return {"session_count": count}

    @staticmethod
    def persistable_messages(messages):
        return ChatSessionRepository.persistable_messages(messages)

    def _check(self, session_id, generation):
        if self.deleted or session_id != self.session_id or generation != self.generation:
            raise ChatSessionStaleError("chat session 已刪除或重置")


def make_chat_session_service(messages=None, summary: str = ""):
    return ChatSessionService(FakeChatSessionRepository(messages, summary))
