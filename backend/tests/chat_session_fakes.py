"""In-memory Chat session repository double for WebSocket unit tests."""

from copy import deepcopy

from domain.emotion_state import validate_emotion_state
from infrastructure.chat_session_repository import (
    ChatSessionRepository,
    ChatSessionStaleError,
    ChatSessionTurnConflict,
    ChatSessionTurnReplay,
)
from services.chat_session_service import ChatSessionService


class FakeChatSessionRepository:
    def __init__(self, messages=None, summary: str = "", session_id: str = "server_session") -> None:
        self.session_id = session_id
        self.generation = 0
        self.revision = 0
        self.messages = deepcopy(messages or [])
        self.summary = summary
        self.summary_through_sequence = -1
        self.next_sequence = len(self.messages)
        self.emotion_state = None
        self.deleted = False
        for index, message in enumerate(self.messages):
            message.setdefault("sequence", index)
            if message.get("role") == "user":
                message.setdefault("turn_state", "completed")

    def snapshot(self, *, context: bool = False):
        if self.deleted:
            return None
        messages = deepcopy(self.messages)
        if context:
            eligible = [
                item for item in messages
                if item.get("sequence", -1) > self.summary_through_sequence
                and not (item.get("role") == "user" and item.get("turn_state") == "pending")
            ]
            groups = []
            current_key = object()
            for item in eligible:
                key = item.get("turn_id") or ("sequence", item.get("sequence"))
                if key != current_key:
                    groups.append([])
                    current_key = key
                groups[-1].append(item)
            messages = []
            for group in reversed(groups):
                if len(messages) + len(group) > 24:
                    break
                messages[0:0] = group
        return {
            "session_id": self.session_id, "generation": self.generation,
            "revision": self.revision, "messages": messages,
            "summary": self.summary, "summary_through_sequence": self.summary_through_sequence,
            "next_message_sequence": self.next_sequence,
            "uncompressed_message_count": len(messages),
            "emotion_state": deepcopy(self.emotion_state),
            "created_at": "2026-10-05T00:00:00+00:00",
            "updated_at": "2026-10-05T00:00:00+00:00",
        }

    async def get_or_create_active(self):
        if self.deleted:
            self.deleted = False
            self.session_id = "replacement_session"
        for item in self.messages:
            if item.get("role") == "user" and item.get("turn_state") == "pending":
                item["turn_state"] = "interrupted"
        return self.snapshot(context=True)

    async def load(self, session_id):
        return self.snapshot() if session_id == self.session_id else None

    async def load_context(self, session_id, *, limit=24):
        return self.snapshot(context=True) if session_id == self.session_id else None

    async def activate_for_test(self, session_id):
        self.session_id = session_id
        self.generation = 0
        self.revision = 0
        self.messages = []
        self.summary = ""
        self.summary_through_sequence = -1
        self.next_sequence = 0
        self.emotion_state = None
        self.deleted = False
        return self.snapshot(context=True)

    async def append_user(self, session_id, generation, message):
        self._check(session_id, generation)
        existing = next((item for item in self.messages
                         if item.get("role") == "user" and item.get("turn_id") == message["turn_id"]), None)
        if existing is not None:
            if existing["content"] == message["content"]:
                raise ChatSessionTurnReplay("turn_id 與內容已接收")
            raise ChatSessionTurnConflict("同一 turn_id 不可對應不同內容")
        item = deepcopy(message)
        item["sequence"] = self.next_sequence
        item["turn_state"] = "pending"
        self.next_sequence += 1
        self.messages.append(item)
        self.revision += 1
        return {"sequence": item["sequence"], "revision": self.revision, "replayed": False}

    async def update_user_source(self, session_id, generation, turn_id, message):
        self._check(session_id, generation)
        source = message.get("memory_source")
        for item in self.messages:
            if item.get("role") == "user" and item.get("turn_id") == turn_id and source:
                item["memory_source"] = deepcopy(source)
        self.revision += 1
        return self.revision

    async def finish_turn(self, session_id, generation, turn_id, assistant_text=None, *, outcome="completed"):
        self._check(session_id, generation)
        user = next((item for item in self.messages
                     if item.get("role") == "user" and item.get("turn_id") == turn_id), None)
        if user is None:
            raise ChatSessionStaleError("找不到目前 user turn")
        if user.get("turn_state") != "pending":
            return self.revision
        if assistant_text:
            self.messages.append({
                "role": "assistant", "content": assistant_text, "turn_id": turn_id,
                "status": "complete" if outcome == "completed" else "interrupted",
                "sequence": self.next_sequence,
            })
            self.next_sequence += 1
        user["turn_state"] = outcome
        self.revision += 1
        return self.revision

    async def load_compression_prefix(
        self, session_id, generation, *, trigger_messages, keep_recent_messages, force=False, max_messages=64,
    ):
        self._check(session_id, generation)
        eligible = [item for item in self.messages
                    if item.get("sequence", -1) > self.summary_through_sequence
                    and not (item.get("role") == "user" and item.get("turn_state") == "pending")]
        if len(eligible) < trigger_messages and not force:
            return None
        selected = eligible[:-keep_recent_messages]
        if not selected:
            return None
        return {
            "previous_summary": self.summary,
            "cursor": self.summary_through_sequence,
            "through_sequence": selected[-1]["sequence"],
            "messages": deepcopy(selected),
            "eligible_message_count": len(eligible),
        }

    async def commit_summary(self, session_id, generation, expected_cursor, summary, through_sequence):
        self._check(session_id, generation)
        if expected_cursor != self.summary_through_sequence:
            raise ChatSessionStaleError("摘要 cursor 已過期")
        self.summary = summary[:4000]
        self.summary_through_sequence = through_sequence
        self.revision += 1
        return self.revision

    async def update_emotion(self, session_id, generation, state):
        self._check(session_id, generation)
        self.emotion_state = validate_emotion_state(state)
        self.revision += 1
        return self.revision

    async def reset(self, session_id, connection=None):
        self._check(session_id, self.generation)
        self.generation += 1
        self.revision += 1
        self.messages = []
        self.summary = ""
        self.summary_through_sequence = -1
        self.next_sequence = 0
        self.emotion_state = None
        return {"generation": self.generation, "revision": self.revision}

    async def list_sessions(self):
        if self.deleted:
            return []
        return [{"session_id": self.session_id, "status": "active", "message_count": len(self.messages),
                 "preview": next((m["content"] for m in self.messages if m["role"] == "user"), ""),
                 "updated_at": "2026-10-05T00:00:00+00:00",
                 "has_summary": bool(self.summary), "has_emotion_state": self.emotion_state is not None,
                 "summary_through_sequence": self.summary_through_sequence}]

    async def load_session_messages(self, session_id, *, limit=100, offset=0):
        snapshot = self.snapshot()
        if snapshot is None or session_id != self.session_id:
            return None
        snapshot["message_count"] = len(self.messages)
        snapshot["limit"] = limit
        snapshot["offset"] = offset
        snapshot["messages"] = deepcopy(self.messages[offset:offset + limit])
        return snapshot

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
