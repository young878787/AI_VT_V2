"""PostgreSQL 不可用時的單一 owner Chat session 存放；重啟即清空。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.config import CHAT_CONTEXT_RECENT_MESSAGES
from domain.emotion_state import validate_emotion_state
from infrastructure.chat_session_repository import (
    ChatSessionRepository,
    ChatSessionStaleError,
    ChatSessionTurnConflict,
    ChatSessionTurnReplay,
)


class InMemoryChatSessionRepository:
    """與 ChatSessionRepository 相同的 generation／turn 語意，但只保存在本程序。"""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid4().hex
        self.generation = 0
        self.revision = 0
        self.messages: list[dict] = []
        self.summary = ""
        self.summary_through_sequence = -1
        self.next_sequence = 0
        self.emotion_state = None
        self.deleted = False
        self.created_at = self.updated_at = self._now()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_session_id(self) -> str:
        return uuid4().hex

    def _bump(self) -> int:
        self.revision += 1
        self.updated_at = self._now()
        return self.revision

    def snapshot(self, *, context: bool = False, limit: int = CHAT_CONTEXT_RECENT_MESSAGES):
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
                if len(messages) + len(group) > limit:
                    break
                messages[0:0] = group
        return {
            "session_id": self.session_id, "generation": self.generation,
            "revision": self.revision, "messages": messages,
            "summary": self.summary, "summary_through_sequence": self.summary_through_sequence,
            "next_message_sequence": self.next_sequence,
            "uncompressed_message_count": len(messages),
            "emotion_state": deepcopy(self.emotion_state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    async def get_or_create_active(self) -> dict[str, Any]:
        if self.deleted:
            self.deleted = False
            self.session_id = self._new_session_id()
            self.created_at = self.updated_at = self._now()
        for item in self.messages:
            if item.get("role") == "user" and item.get("turn_state") == "pending":
                item["turn_state"] = "interrupted"
        return self.snapshot(context=True)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        return self.snapshot() if session_id == self.session_id else None

    async def load_context(self, session_id: str, *, limit: int = CHAT_CONTEXT_RECENT_MESSAGES):
        return self.snapshot(context=True, limit=limit) if session_id == self.session_id else None

    async def activate_for_test(self, session_id: str) -> dict[str, Any]:
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

    async def append_user(self, session_id: str, generation: int, message: dict) -> dict[str, Any]:
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
        return {"sequence": item["sequence"], "revision": self._bump(), "replayed": False}

    async def update_user_source(self, session_id: str, generation: int, turn_id: str, message: dict) -> int:
        self._check(session_id, generation)
        source = message.get("memory_source")
        for item in self.messages:
            if item.get("role") == "user" and item.get("turn_id") == turn_id and source:
                item["memory_source"] = deepcopy(source)
        return self._bump()

    async def finish_turn(self, session_id: str, generation: int, turn_id: str,
                          assistant_text: str | None = None, *, outcome: str = "completed") -> int:
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
        return self._bump()

    async def load_compression_prefix(
        self, session_id: str, generation: int, *, trigger_messages: int, keep_recent_messages: int,
        force: bool = False, max_messages: int = 64,
    ) -> dict[str, Any] | None:
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

    async def commit_summary(self, session_id: str, generation: int, expected_cursor: int,
                             summary: str, through_sequence: int) -> int:
        self._check(session_id, generation)
        if expected_cursor != self.summary_through_sequence:
            raise ChatSessionStaleError("摘要 cursor 已過期")
        self.summary = summary[:4000]
        self.summary_through_sequence = through_sequence
        return self._bump()

    async def update_emotion(self, session_id: str, generation: int, state: dict) -> int:
        self._check(session_id, generation)
        self.emotion_state = validate_emotion_state(state)
        return self._bump()

    async def reset(self, session_id: str, *, connection=None) -> dict[str, int]:
        self._check(session_id, self.generation)
        self.generation += 1
        self.messages = []
        self.summary = ""
        self.summary_through_sequence = -1
        self.next_sequence = 0
        self.emotion_state = None
        return {"generation": self.generation, "revision": self._bump()}

    async def list_sessions(self) -> list[dict[str, Any]]:
        if self.deleted:
            return []
        return [{"session_id": self.session_id, "status": "active", "message_count": len(self.messages),
                 "preview": next((m["content"] for m in self.messages if m["role"] == "user"), ""),
                 "updated_at": self.updated_at,
                 "has_summary": bool(self.summary), "has_emotion_state": self.emotion_state is not None,
                 "summary_through_sequence": self.summary_through_sequence}]

    async def load_session_messages(self, session_id: str, *, limit: int = 100, offset: int = 0):
        snapshot = self.snapshot()
        if snapshot is None or session_id != self.session_id:
            return None
        snapshot["message_count"] = len(self.messages)
        snapshot["limit"] = limit
        snapshot["offset"] = offset
        snapshot["messages"] = deepcopy(self.messages[offset:offset + limit])
        return snapshot

    async def delete(self, session_id: str) -> dict[str, Any]:
        deleted = int(not self.deleted and session_id == self.session_id)
        self.deleted = bool(deleted) or self.deleted
        return {"session_id": session_id, "deleted_sessions": deleted}

    async def delete_all(self) -> dict[str, int]:
        count = int(not self.deleted)
        self.deleted = True
        return {"session_count": count}

    @staticmethod
    def persistable_messages(messages: list[dict]) -> list[dict]:
        return ChatSessionRepository.persistable_messages(messages)

    def _check(self, session_id: str, generation: int) -> None:
        if self.deleted or session_id != self.session_id or generation != self.generation:
            raise ChatSessionStaleError("chat session 已刪除或重置")
