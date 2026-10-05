"""Owner-scoped PostgreSQL storage for bounded Chat session state."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from core.config import CHAT_SESSION_MAX_MESSAGES
from core.utils import get_msg_field, normalize_session_id
from domain.emotion_state import validate_emotion_state
from domain.memory_scope import MemoryScope
from domain.memory_source import MEMORY_SOURCE_FIELD, read_memory_source


class ChatSessionStaleError(RuntimeError):
    """The caller is attempting to write a deleted or reset session generation."""


class ChatSessionRepository:
    def __init__(self, pool: AsyncConnectionPool, scope: MemoryScope) -> None:
        self.pool = pool
        self.scope = scope

    async def _set_search_path(self, connection) -> None:
        await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(
            sql.Identifier(self.scope.schema_name)
        ))

    @property
    def owner(self) -> tuple:
        return self.scope.user_id, self.scope.character_id

    async def get_or_create_active(self) -> dict[str, Any]:
        """Serialize owner selection so concurrent connects cannot create two active sessions."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"chat:{self.scope.user_id}:{self.scope.character_id}",),
                )
                row = await (await connection.execute(
                    """SELECT id, generation, revision, summary, emotion_state, created_at, updated_at
                    FROM chat_sessions WHERE user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", self.owner,
                )).fetchone()
                if row is None:
                    session_id = uuid4().hex
                    row = await (await connection.execute(
                        """INSERT INTO chat_sessions (id, user_id, character_id)
                        VALUES (%s, %s, %s)
                        RETURNING id, generation, revision, summary, emotion_state, created_at, updated_at""",
                        (session_id, *self.owner),
                    )).fetchone()
                return await self._snapshot(connection, row)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """SELECT id, generation, revision, summary, emotion_state, created_at, updated_at
                    FROM chat_sessions WHERE id = %s AND user_id = %s AND character_id = %s""",
                    (session_id, *self.owner),
                )).fetchone()
                return await self._snapshot(connection, row) if row else None

    async def activate_for_test(self, session_id: str) -> dict[str, Any]:
        """Select a deterministic isolated session; callers must enforce test-instance policy."""
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 test session alias")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"chat:{self.scope.user_id}:{self.scope.character_id}",),
                )
                await connection.execute(
                    """UPDATE chat_sessions SET status = 'closed', updated_at = now()
                    WHERE user_id = %s AND character_id = %s AND status = 'active' AND id <> %s""",
                    (*self.owner, session_id),
                )
                row = await (await connection.execute(
                    """INSERT INTO chat_sessions (id,user_id,character_id,status)
                    VALUES (%s,%s,%s,'active')
                    ON CONFLICT (id,user_id,character_id) DO UPDATE
                    SET status = 'active', updated_at = now()
                    RETURNING id,generation,revision,summary,emotion_state,created_at,updated_at""",
                    (session_id, *self.owner),
                )).fetchone()
                return await self._snapshot(connection, row)

    async def _snapshot(self, connection, row) -> dict[str, Any]:
        session_id, generation, revision, summary, emotion, created_at, updated_at = row
        cursor = await connection.execute(
            """SELECT turn_id, role, content, status, memory_source, created_at
            FROM chat_messages WHERE session_id = %s AND user_id = %s AND character_id = %s
            AND generation = %s ORDER BY sequence""",
            (session_id, *self.owner, generation),
        )
        messages = []
        for turn_id, role, content, status, source, message_created_at in await cursor.fetchall():
            item = {"role": role, "content": content}
            if turn_id is not None:
                item["turn_id"] = turn_id
            if role == "assistant" and status == "interrupted":
                item["status"] = status
            if role == "user" and source is not None:
                candidate = {**item, MEMORY_SOURCE_FIELD: source}
                if read_memory_source(candidate) is not None:
                    item[MEMORY_SOURCE_FIELD] = source
            item["created_at"] = message_created_at.isoformat()
            messages.append(item)
        return {
            "session_id": session_id,
            "generation": generation,
            "revision": revision,
            "summary": summary,
            "emotion_state": validate_emotion_state(emotion),
            "messages": messages,
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
        }

    async def replace_messages(self, session_id: str, generation: int, messages: list[dict]) -> int:
        persisted = self.persistable_messages(messages)[-CHAT_SESSION_MAX_MESSAGES:]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """SELECT generation FROM chat_sessions
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", (session_id, *self.owner),
                )).fetchone()
                if row is None or row[0] != generation:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                await connection.execute(
                    """DELETE FROM chat_messages WHERE session_id = %s AND user_id = %s
                    AND character_id = %s AND generation = %s""",
                    (session_id, *self.owner, generation),
                )
                for sequence, item in enumerate(persisted):
                    await connection.execute(
                        """INSERT INTO chat_messages
                        (session_id,user_id,character_id,generation,sequence,turn_id,role,content,status,memory_source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (session_id, *self.owner, generation, sequence, item.get("turn_id"),
                         item["role"], item["content"], item.get("status", "complete"),
                         Jsonb(item[MEMORY_SOURCE_FIELD]) if item.get(MEMORY_SOURCE_FIELD) else None),
                    )
                revision = (await (await connection.execute(
                    """UPDATE chat_sessions SET revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s RETURNING revision""",
                    (session_id, *self.owner),
                )).fetchone())[0]
                return revision

    async def update_emotion(self, session_id: str, generation: int, state: dict) -> int:
        validated = validate_emotion_state(state)
        if validated is None:
            raise ValueError("無效的 Emotion State")
        return await self._update_state(session_id, generation, "emotion_state", Jsonb(validated))

    async def commit_summary(self, session_id: str, generation: int, expected_revision: int,
                             summary: str, messages: list[dict]) -> int:
        """Commit summary only when the history used to generate it is still current."""
        persisted = self.persistable_messages(messages)[-CHAT_SESSION_MAX_MESSAGES:]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """SELECT generation, revision FROM chat_sessions
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", (session_id, *self.owner),
                )).fetchone()
                if row != (generation, expected_revision):
                    raise ChatSessionStaleError("摘要基準已過期")
                await connection.execute(
                    """UPDATE chat_sessions SET summary = %s, revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s""",
                    (summary[:4000], session_id, *self.owner),
                )
                await connection.execute(
                    """DELETE FROM chat_messages WHERE session_id = %s AND user_id = %s
                    AND character_id = %s AND generation = %s""",
                    (session_id, *self.owner, generation),
                )
                for sequence, item in enumerate(persisted):
                    await connection.execute(
                        """INSERT INTO chat_messages
                        (session_id,user_id,character_id,generation,sequence,turn_id,role,content,status,memory_source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (session_id, *self.owner, generation, sequence, item.get("turn_id"), item["role"],
                         item["content"], item.get("status", "complete"),
                         Jsonb(item[MEMORY_SOURCE_FIELD]) if item.get(MEMORY_SOURCE_FIELD) else None),
                    )
                return expected_revision + 1

    async def _update_state(self, session_id: str, generation: int, column: str, value) -> int:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    sql.SQL("""UPDATE chat_sessions SET {} = %s, revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' AND generation = %s RETURNING revision""").format(sql.Identifier(column)),
                    (value, session_id, *self.owner, generation),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                return row[0]

    async def reset(self, session_id: str, *, connection=None) -> dict[str, int]:
        async def execute(active_connection):
            await self._set_search_path(active_connection)
            row = await (await active_connection.execute(
                """UPDATE chat_sessions SET generation = generation + 1, revision = revision + 1,
                summary = '', emotion_state = NULL, updated_at = now()
                WHERE id = %s AND user_id = %s AND character_id = %s AND status = 'active'
                RETURNING generation, revision""", (session_id, *self.owner),
            )).fetchone()
            if row is None:
                raise ChatSessionStaleError("chat session 已刪除")
            await active_connection.execute(
                "DELETE FROM chat_messages WHERE session_id = %s AND user_id = %s AND character_id = %s",
                (session_id, *self.owner),
            )
            return {"generation": row[0], "revision": row[1]}
        if connection is not None:
            return await execute(connection)
        async with self.pool.connection() as own_connection:
            async with own_connection.transaction():
                return await execute(own_connection)

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    """SELECT s.id, s.status, s.updated_at, s.summary <> '', s.emotion_state IS NOT NULL,
                    count(m.sequence), COALESCE((array_agg(m.content ORDER BY m.sequence)
                        FILTER (WHERE m.role = 'user'))[1], '')
                    FROM chat_sessions s LEFT JOIN chat_messages m
                    ON (m.session_id,m.user_id,m.character_id,m.generation) =
                       (s.id,s.user_id,s.character_id,s.generation)
                    WHERE s.user_id = %s AND s.character_id = %s
                    GROUP BY s.id,s.user_id,s.character_id,s.status,s.updated_at,s.summary,s.emotion_state
                    ORDER BY s.updated_at DESC""", self.owner,
                )
                return [{"session_id": row[0], "status": row[1], "updated_at": row[2].isoformat(),
                         "has_summary": row[3], "has_emotion_state": row[4],
                         "message_count": row[5], "preview": row[6][:160]} for row in await cursor.fetchall()]

    async def delete(self, session_id: str) -> dict[str, Any]:
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    "DELETE FROM chat_sessions WHERE id = %s AND user_id = %s AND character_id = %s",
                    (session_id, *self.owner),
                )
                return {"session_id": session_id, "deleted_sessions": cursor.rowcount}

    async def delete_all(self) -> dict[str, int]:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    "DELETE FROM chat_sessions WHERE user_id = %s AND character_id = %s", self.owner,
                )
                return {"session_count": cursor.rowcount}

    @staticmethod
    def persistable_messages(messages: list[dict]) -> list[dict]:
        persisted = []
        for message in messages:
            role = get_msg_field(message, "role", "")
            content = get_msg_field(message, "content", "")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
                continue
            item = {"role": role, "content": content}
            turn_id = message.get("turn_id") if isinstance(message, dict) else None
            if isinstance(turn_id, str) and 0 < len(turn_id) <= 128:
                item["turn_id"] = turn_id
            if role == "assistant" and message.get("status") == "interrupted":
                item["status"] = "interrupted"
            if role == "user" and read_memory_source(message) is not None:
                item[MEMORY_SOURCE_FIELD] = dict(message[MEMORY_SOURCE_FIELD])
            persisted.append(item)
        return persisted
